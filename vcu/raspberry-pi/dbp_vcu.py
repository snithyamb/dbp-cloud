#!/usr/bin/env python3
"""
dbp_vcu.py — Raspberry Pi VCU gateway for the Digital Battery Passport.

  STM32 BMS ──CAN 500 kbit/s──▶ MCP2515 HAT ──can0──▶ this program ──HTTPS──▶ /api/v1/dbp/record

What it does
  1. Receives BMS CAN frames and decodes them with YOUR DBC file (cantools).
  2. Computes the Annex IV / VII metrics the BMS does not send:
       energy & capacity throughput, depth of discharge, round-trip efficiency,
       SOCE estimate, internal resistance (needs live pack voltage), self-discharge,
       time at extreme temperature, remaining capacity / useful life.
  3. Detects events (deep discharge, overcharge, over-temperature, crash) and
     keeps non-resettable counters on disk.
  4. Every `telemetry_interval_s` (and at every cycle end) queues a TELEMETRY
     record; events are queued immediately as EVENT records.
  5. A sender thread posts the queue in order. If the network or server is down,
     records wait in a SQLite outbox on the SD card and are sent later, in order.
     The server hash-chains every record it accepts.

Run
  VCU_TOKEN=... python3 dbp_vcu.py --config /etc/dbp-vcu/vcu_config.json
  python3 dbp_vcu.py --config vcu_config.json --dry-run      # print records, send nothing

Dependencies: python3-can, cantools  (pip install python-can cantools)
"""
import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import can
import cantools

log = logging.getLogger("dbp-vcu")

API_LIMITS = {  # must match the server's VCU_NUMERIC ranges
    "soc_pct": (0, 100), "soh_pct": (0, 120), "soce_pct": (0, 120),
    "capacity_fade_pct": (-20, 100), "power_fade_pct": (-20, 100),
    "round_trip_efficiency_pct": (0, 100), "rte_fade_pct": (-100, 100),
    "self_discharge_pct_per_month": (0, 100), "depth_of_discharge_pct": (0, 100),
    "temp_celsius": (-60, 150), "current_a": (-5000, 5000), "pack_voltage_v": (0, 2000),
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clamp(name, v):
    lo, hi = API_LIMITS.get(name, (None, None))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ── Persistent state (non-resettable counters live here) ─────────────────────

class State:
    FIELDS = dict(
        cycle_count=0, energy_throughput_kwh=0.0, energy_charged_kwh=0.0,
        capacity_throughput_ah=0.0, time_extreme_temp_h=0.0, time_charging_extreme_temp_h=0.0,
        deep_discharge_events=0, overcharge_events=0, accident_events=0,
        lifecycle_status="ORIGINAL", resistance_mohm=None, last_rte_pct=None,
        last_dod_pct=None, last_soce_pct=None, self_discharge_pct_per_month=None,
    )

    def __init__(self, path):
        self.path = path
        self.__dict__.update(self.FIELDS)
        if os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
            self.__dict__.update({k: v for k, v in saved.items() if k in self.FIELDS})
            log.info("state loaded: cycle %s, %.3f kWh", self.cycle_count, self.energy_throughput_kwh)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: getattr(self, k) for k in self.FIELDS}, f)
        os.replace(tmp, self.path)          # atomic on the same filesystem


# ── Outbox: ordered, durable upload queue ────────────────────────────────────

class Outbox:
    def __init__(self, path, url, token, dry_run=False):
        self.url, self.token, self.dry_run = url, token, dry_run
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS dead (id INTEGER PRIMARY KEY, body TEXT, status INT, error TEXT, created REAL)")
        self.db.commit()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.sent = 0

    def put(self, record):
        if self.dry_run:
            print(json.dumps(record))
            return
        with self.lock:
            self.db.execute("INSERT INTO q (body, created) VALUES (?, ?)", (json.dumps(record), time.time()))
            self.db.commit()
        self.wake.set()

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]

    def _post(self, body):
        req = urllib.request.Request(self.url, data=body.encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=75) as r:
            return r.status, r.read().decode(errors="replace")

    def run(self):
        backoff = 5
        while not self.stop.is_set():
            with self.lock:
                row = self.db.execute("SELECT id, body FROM q ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.wake.wait(5); self.wake.clear(); continue
            rid, body = row
            try:
                status, text = self._post(body)
                with self.lock:
                    self.db.execute("DELETE FROM q WHERE id=?", (rid,)); self.db.commit()
                self.sent += 1
                backoff = 5
                log.info("sent #%d (%s) hash=%s", rid, json.loads(body).get("record_type", "TELEMETRY"),
                         json.loads(text).get("record_hash", "")[:16])
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:500]
                if e.code == 409 and "Chain head moved" in text:
                    time.sleep(1); continue
                if 400 <= e.code < 500 and e.code not in (401, 408, 429):
                    # the record itself is wrong: park it so it cannot block the queue
                    with self.lock:
                        self.db.execute("INSERT INTO dead (body, status, error, created) VALUES (?,?,?,?)",
                                        (body, e.code, text, time.time()))
                        self.db.execute("DELETE FROM q WHERE id=?", (rid,)); self.db.commit()
                    log.error("record #%d rejected %d: %s (moved to dead-letter table)", rid, e.code, text)
                    continue
                log.warning("server %d, retry in %ds: %s", e.code, backoff, text[:200])
                self.stop.wait(backoff); backoff = min(backoff * 2, 120)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log.warning("network error (%s); %d queued; retry in %ds", e, self.pending(), backoff)
                self.stop.wait(backoff); backoff = min(backoff * 2, 120)


# ── Signal processing ────────────────────────────────────────────────────────

class VCU:
    def __init__(self, cfg, state, outbox):
        self.cfg, self.st, self.out = cfg, state, outbox
        self.bat, self.lim, self.up = cfg["battery"], cfg["limits"], cfg["upload"]
        self.dis_positive = cfg.get("current_positive_is_discharge", True)
        self.v = {}            # latest decoded value per logical signal
        self.t = {}            # time it arrived
        self.frames = 0
        self.last_current_t = None
        self.last_iv = None    # (t, I, V) for resistance steps
        self.cycle_e_dis = self.cycle_e_chg = self.cycle_ah_dis = 0.0
        self.cycle_soc_min, self.cycle_soc_max = 100.0, 0.0
        self.bms_cycle_seen = None
        self.warned_cycle = None
        self.rest = None       # (t_start, soc_start)
        self.flags = {"deep": False, "over": False, "hot": False, "crash": False}
        self.last_telemetry = time.monotonic()
        self.send_now = False

    # helpers
    def fresh(self, name, max_age=2.0):
        return name in self.v and time.monotonic() - self.t[name] <= max_age

    def volts(self):
        if self.fresh("pack_voltage_v"):
            return self.v["pack_voltage_v"], "live"
        return self.v.get("nominal_voltage_v", self.bat["nominal_voltage_v"]), "nominal"

    def on_signals(self, values):
        now = time.monotonic()
        for k, val in values.items():
            self.v[k], self.t[k] = float(val), now
        if "current_a" in values:
            self._integrate(now)
        if "pack_voltage_v" in values:
            self._resistance(now)
        if "cycle_count" in values:
            self._cycle(int(values["cycle_count"]))
        if "soc_pct" in values:
            soc = self.v["soc_pct"]
            self.cycle_soc_min = min(self.cycle_soc_min, soc)
            self.cycle_soc_max = max(self.cycle_soc_max, soc)
        self._events()
        self._maybe_queue_telemetry()

    def _integrate(self, now):
        i = self.v["current_a"] if self.dis_positive else -self.v["current_a"]   # + = discharge
        if self.last_current_t is not None:
            dt = min(max(now - self.last_current_t, 0.0), 1.0)                   # cap gaps (outages)
            v, _ = self.volts()
            e_kwh = v * abs(i) * dt / 3.6e6
            if i > 0:                                  # discharge: Annex VII B(2)/(3)
                self.st.energy_throughput_kwh += e_kwh
                self.st.capacity_throughput_ah += abs(i) * dt / 3600
                self.cycle_e_dis += e_kwh
                self.cycle_ah_dis += abs(i) * dt / 3600
            elif i < 0:
                self.st.energy_charged_kwh += e_kwh
                self.cycle_e_chg += e_kwh
            # extreme temperature exposure: Annex VII B(4)
            if self.fresh("temp_max_c") or self.fresh("temp_min_c"):
                hot = self.v.get("temp_max_c", -999) > self.lim["extreme_hot_c"]
                cold = self.v.get("temp_min_c", 999) < self.lim["extreme_cold_c"]
                if hot or cold:
                    self.st.time_extreme_temp_h += dt / 3600
                    if i < 0:
                        self.st.time_charging_extreme_temp_h += dt / 3600
        self.last_current_t = now
        self._self_discharge(now, i)

    def _resistance(self, now):
        """R = |ΔV / ΔI| across a current step. Runs when a pack-voltage frame arrives and pairs it
        with the current received just before it (same BMS sample), so V and I are never mismatched."""
        if not self.fresh("current_a", 0.15):
            self.last_iv = None
            return
        i, v = self.v["current_a"], self.v["pack_voltage_v"]
        if self.last_iv:
            t0, i0, v0 = self.last_iv
            if now - t0 <= 0.3 and abs(i - i0) >= 5.0:
                r = abs((v - v0) / (i - i0)) * 1000.0
                r0 = self.bat["initial_resistance_mohm"]
                if 0.2 * r0 < r < 10 * r0:              # reject steps where OCV also moved
                    old = self.st.resistance_mohm
                    self.st.resistance_mohm = r if old is None else 0.9 * old + 0.1 * r
        self.last_iv = (now, i, v)

    def _self_discharge(self, now, i):
        """SoC lost while resting (|I| below threshold), scaled to %/month: Annex VII A(4)."""
        if abs(i) > self.lim["rest_current_a"] or "soc_pct" not in self.v:
            self.rest = None
            return
        if self.rest is None:
            self.rest = (now, self.v["soc_pct"])
            return
        hours = (now - self.rest[0]) / 3600
        if hours * 60 >= self.lim["rest_minutes_for_self_discharge"]:
            drop = self.rest[1] - self.v["soc_pct"]
            if drop > 0:
                self.st.self_discharge_pct_per_month = round(drop / hours * 720, 3)
            self.rest = (now, self.v["soc_pct"])

    def _cycle(self, bms_count):
        if self.bms_cycle_seen is None:
            self.bms_cycle_seen = bms_count
        if bms_count < self.st.cycle_count and self.warned_cycle != bms_count:
            self.warned_cycle = bms_count
            log.warning("BMS cycle count %d < stored %d — keeping stored (non-resettable). "
                        "Was the BMS EEPROM erased or the battery swapped?", bms_count, self.st.cycle_count)
        if bms_count > self.st.cycle_count:
            completed = bms_count > self.bms_cycle_seen
            self.st.cycle_count = bms_count
            if completed:
                self._finish_cycle()
        self.bms_cycle_seen = bms_count

    def _finish_cycle(self):
        # only a real cycle (≥ 30 % of rated Ah discharged) gives meaningful RTE / SOCE
        real = self.cycle_ah_dis >= 0.3 * self.bat["rated_capacity_ah"]
        if real and self.cycle_e_dis > 0 and self.cycle_e_chg > 0:
            self.st.last_rte_pct = round(min(100.0, self.cycle_e_dis / self.cycle_e_chg * 100), 3)
        dod = self.cycle_soc_max - self.cycle_soc_min
        if dod > 0:
            self.st.last_dod_pct = round(dod, 2)
            if real and dod >= 50:          # SOCE: usable energy vs rated energy
                usable = self.cycle_e_dis / (dod / 100)
                self.st.last_soce_pct = round(usable / self.bat["rated_energy_kwh"] * 100, 3)
        log.info("cycle %d done: %.1f Ah / %.3f kWh out, %.3f kWh in, DoD %s%%, RTE %s%%%s",
                 self.st.cycle_count, self.cycle_ah_dis, self.cycle_e_dis, self.cycle_e_chg,
                 self.st.last_dod_pct, self.st.last_rte_pct, "" if real else " (partial cycle: RTE/SOCE not updated)")
        self.cycle_e_dis = self.cycle_e_chg = self.cycle_ah_dis = 0.0
        self.cycle_soc_min, self.cycle_soc_max = 100.0, 0.0
        if self.up.get("send_on_cycle_end", True):
            self.send_now = True

    def _events(self):
        soc = self.v.get("soc_pct")
        deep = (soc is not None and soc <= self.lim["deep_discharge_soc_pct"]) or \
               (self.fresh("cell_v_min") and self.v["cell_v_min"] < self.lim["deep_discharge_cell_v"])
        over = self.fresh("cell_v_max") and self.v["cell_v_max"] > self.lim["overcharge_cell_v"]
        hot = self.fresh("temp_max_c") and self.v["temp_max_c"] > self.lim["extreme_hot_c"]
        crash = self.fresh("fault_crash") and self.v["fault_crash"] >= 1
        if deep and not self.flags["deep"]:
            self.st.deep_discharge_events += 1
            self._event("DEEP_DISCHARGE", f"SoC {soc}% / min cell {self.v.get('cell_v_min', 'n/a')} V")
        if over and not self.flags["over"]:
            self.st.overcharge_events += 1
            self._event("OVERCHARGE", f"Max cell {self.v['cell_v_max']:.2f} V")
        if hot and not self.flags["hot"]:
            self._event("THERMAL_EVENT", f"Pack temperature {self.v['temp_max_c']:.0f} °C")
        if crash and not self.flags["crash"]:
            self.st.accident_events += 1
            self._event("ACCIDENT", "BMS crash / isolation fault flag set")
        self.flags.update(deep=deep, over=over, hot=hot, crash=crash)

    def set_status(self, status, reason):
        if status != self.st.lifecycle_status:
            self.st.lifecycle_status = status
            self._event("STATUS_CHANGE", reason)

    # records
    def _counters(self):
        return {
            "cycle_count": int(self.st.cycle_count),
            "deep_discharge_events": int(self.st.deep_discharge_events),
            "overcharge_events": int(self.st.overcharge_events),
            "accident_events": int(self.st.accident_events),
            "lifecycle_status": self.st.lifecycle_status,
        }

    def _event(self, etype, detail):
        rec = {"bin": self.cfg["bin"], "vcu_id": self.cfg["vcu_id"], "timestamp": utc_now_iso(),
               "record_type": "EVENT", "event_type": etype, "event_detail": detail[:500], **self._counters()}
        log.info("EVENT %s: %s", etype, detail)
        self.out.put(rec)
        self.st.save()

    def _maybe_queue_telemetry(self):
        due = time.monotonic() - self.last_telemetry >= self.up["telemetry_interval_s"]
        if not (due or self.send_now):
            return
        if not (self.fresh("soc_pct", self.up["bms_silence_timeout_s"]) and
                self.fresh("current_a", self.up["bms_silence_timeout_s"])):
            return                                       # never upload stale data
        self.out.put(self.telemetry())
        self.st.save()
        self.last_telemetry = time.monotonic()
        self.send_now = False

    def telemetry(self):
        b, st = self.bat, self.st
        v, vsrc = self.volts()
        soh = self.v.get("soh_pct")
        rec = {"bin": self.cfg["bin"], "vcu_id": self.cfg["vcu_id"], "timestamp": utc_now_iso(),
               "record_type": "TELEMETRY", **self._counters(),
               "soc_pct": round(clamp("soc_pct", self.v["soc_pct"]), 2),
               "current_a": round(clamp("current_a", self.v["current_a"]), 2),
               "pack_voltage_v": round(clamp("pack_voltage_v", v), 2),
               "energy_throughput_kwh": round(st.energy_throughput_kwh, 4),
               "capacity_throughput_ah": round(st.capacity_throughput_ah, 3),
               "time_extreme_temp_h": round(st.time_extreme_temp_h, 4),
               "time_charging_extreme_temp_h": round(st.time_charging_extreme_temp_h, 4)}
        if soh is not None:
            fade_rate = (100 - soh) / st.cycle_count if st.cycle_count >= 50 and soh < 100 else \
                        (100 - b["eol_soh_pct"]) / b["expected_lifetime_cycles"]
            rul = max(0.0, (soh - b["eol_soh_pct"]) / fade_rate) if fade_rate > 0 else None
            rec.update({"soh_pct": round(clamp("soh_pct", soh), 2),
                        "remaining_capacity_ah": round(b["rated_capacity_ah"] * soh / 100, 3),
                        "capacity_fade_pct": round(clamp("capacity_fade_pct", 100 - soh), 3)})
            if rul is not None:
                rec["remaining_life_cycles"] = round(rul)
        if self.fresh("temp_max_c"):
            rec["temp_celsius"] = round(clamp("temp_celsius", self.v["temp_max_c"]), 1)
        if st.last_dod_pct is not None:
            rec["depth_of_discharge_pct"] = round(clamp("depth_of_discharge_pct", st.last_dod_pct), 2)
        if st.last_rte_pct is not None:
            rec["round_trip_efficiency_pct"] = round(clamp("round_trip_efficiency_pct", st.last_rte_pct), 3)
            rec["rte_fade_pct"] = round(clamp("rte_fade_pct", b["initial_rte_pct"] - st.last_rte_pct), 3)
        if st.last_soce_pct is not None:
            rec["soce_pct"] = round(clamp("soce_pct", st.last_soce_pct), 3)
        if st.self_discharge_pct_per_month is not None:
            rec["self_discharge_pct_per_month"] = round(clamp("self_discharge_pct_per_month",
                                                              st.self_discharge_pct_per_month), 3)
        if st.resistance_mohm is not None:
            r0 = b["initial_resistance_mohm"]
            rec["internal_resistance_mohm"] = round(st.resistance_mohm, 3)
            rec["internal_resistance_increase_pct"] = round((st.resistance_mohm - r0) / r0 * 100, 2)
            p = b["rated_power_w"] * r0 / st.resistance_mohm         # P_max ∝ 1/R
            rec["remaining_power_w"] = round(min(p, b["rated_power_w"]), 1)
            rec["power_fade_pct"] = round(clamp("power_fade_pct", (1 - rec["remaining_power_w"] / b["rated_power_w"]) * 100), 3)
        rec["raw_telemetry"] = {"voltage_source": vsrc, "frames_rx": self.frames,
                                "energy_charged_kwh": round(st.energy_charged_kwh, 4),
                                **{k: round(self.v[k], 3) for k in ("temp_min_c", "cell_v_min", "cell_v_max")
                                   if self.fresh(k)}}
        return rec


# ── CAN plumbing ─────────────────────────────────────────────────────────────

def build_decoder(dbc_path, signal_map):
    db = cantools.database.load_file(dbc_path)
    by_frame = {}
    for logical, ref in signal_map.items():
        msg_name, sig_name = ref.split(".", 1)
        try:
            msg = db.get_message_by_name(msg_name)
            msg.get_signal_by_name(sig_name)
        except KeyError:
            log.warning("signal %s (%s) not in DBC — feature disabled", ref, logical)
            continue
        by_frame.setdefault(msg.frame_id, (msg, []))[1].append((sig_name, logical))
    if not by_frame:
        raise SystemExit("none of the configured signals exist in the DBC")
    return by_frame


def wait_for_clock(required):
    if not required:
        return
    while not (os.path.exists("/run/systemd/timesync/synchronized") or datetime.now().year >= 2026 and
               os.environ.get("DBP_TRUST_CLOCK") == "1"):
        log.warning("waiting for NTP time sync (records need a correct UTC timestamp)…")
        time.sleep(10)


def main():
    ap = argparse.ArgumentParser(description="DBP VCU gateway")
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print records instead of uploading")
    ap.add_argument("--channel", help="override CAN channel (e.g. vcan0)")
    ap.add_argument("--dbc", help="override the DBC file from the config")
    ap.add_argument("--state-dir", help="override state_dir (keep test runs away from real counters)")
    ap.add_argument("--interface", help="override python-can interface (socketcan, virtual, …)")
    ap.add_argument("--run-seconds", type=float, help="stop after N seconds (testing)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = json.load(open(a.config))
    if a.dbc:
        cfg["dbc_file"] = a.dbc
    if a.state_dir:
        cfg["state_dir"] = a.state_dir
    if a.dry_run:                       # never touch the real counters / outbox in a dry run
        import tempfile
        cfg["state_dir"] = tempfile.mkdtemp(prefix="dbp-vcu-dry-")
    os.makedirs(cfg["state_dir"], exist_ok=True)
    token = os.environ.get("VCU_TOKEN", "")
    if not a.dry_run and not token:
        raise SystemExit("VCU_TOKEN not set (put it in /etc/dbp-vcu/dbp-vcu.env)")
    wait_for_clock(cfg["upload"].get("require_time_sync", True) and not a.dry_run)

    # one counter file per battery: moving the Pi to another BIN never carries counters across
    state = State(os.path.join(cfg["state_dir"], f"state_{cfg['bin']}.json"))
    outbox = Outbox(os.path.join(cfg["state_dir"], "outbox.db"), cfg["api_url"], token, a.dry_run)
    vcu = VCU(cfg, state, outbox)
    decoders = build_decoder(cfg["dbc_file"], cfg["signals"])
    bus = can.Bus(interface=a.interface or cfg["can_interface"], channel=a.channel or cfg["can_channel"],
                  receive_own_messages=False)
    log.info("listening on %s, %d frame ids decoded, uploading to %s",
             a.channel or cfg["can_channel"], len(decoders), cfg["api_url"])

    sender = threading.Thread(target=outbox.run, daemon=True)
    if not a.dry_run:
        sender.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    t_end = time.monotonic() + a.run_seconds if a.run_seconds else None
    last_rx = time.monotonic()

    while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
        msg = bus.recv(timeout=0.5)
        if msg is None:
            if time.monotonic() - last_rx > cfg["upload"]["bms_silence_timeout_s"]:
                log.warning("no BMS frames for %.0f s — check wiring / termination / bitrate",
                            time.monotonic() - last_rx)
                last_rx = time.monotonic()
            continue
        entry = decoders.get(msg.arbitration_id)
        if not entry or msg.is_error_frame:
            continue
        last_rx = time.monotonic()
        dbmsg, sigs = entry
        try:
            decoded = dbmsg.decode(msg.data, decode_choices=False)
        except Exception as e:                            # malformed frame: skip, don't crash
            log.debug("decode error 0x%X: %s", msg.arbitration_id, e)
            continue
        vcu.frames += 1
        vcu.on_signals({logical: decoded[s] for s, logical in sigs if s in decoded})

    state.save()
    outbox.stop.set()
    bus.shutdown()
    log.info("stopped; %d records sent, %d still queued", outbox.sent, outbox.pending())


if __name__ == "__main__":
    main()
