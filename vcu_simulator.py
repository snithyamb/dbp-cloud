#!/usr/bin/env python3
"""
VCU simulator for the Digital Battery Passport (EU 2023/1542).

Pushes realistic, slowly degrading telemetry for one battery to
POST /api/v1/dbp/record, one record per simulated day, plus EVENT records
for deep-discharge / overcharge / thermal events. Every record is hashed and
chained by the server; the script prints each record_hash it gets back.

Accelerated time: by default 1 tick = 1 day of use, one tick every 5 s
(supervisor's "5 seconds = 1 day").

Standard library only — no pip install needed.

Usage
-----
  # see what would be sent, nothing is uploaded
  python vcu_simulator.py --dry-run --ticks 5

  # real run (token from the Render VCU_TOKEN setting)
  set VCU_TOKEN=...            (Windows)      export VCU_TOKEN=...   (Mac/Linux)
  python vcu_simulator.py --bin DBP-2024HT65556-002 --ticks 60

Records cannot be edited or deleted once accepted, so test against a
separate battery ID (default DBP-2024HT65556-002), not the main passport.
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ── Battery rating (matches the Hyterra demo passport) ───────────────────────
RATED_AH        = 120.0
RATED_KWH       = 48.0
RATED_POWER_W   = 150_000.0
V_MIN, V_MAX    = 280.0, 420.0
R0_MOHM         = 0.45
RTE0_PCT        = 94.2
LIFE_CYCLES     = 1500          # cycles to 80 % SoH
EOL_SOH         = 80.0
EXTREME_HOT_C   = 45.0


class Battery:
    """Simple empirical ageing model — good enough for a demo, not a physics model."""

    def __init__(self, start_cycle, seed):
        self.rng = random.Random(seed)
        self.cycle = start_cycle
        self.energy_kwh = 0.0
        self.capacity_ah = 0.0
        self.t_extreme_h = 0.0
        self.t_charge_extreme_h = 0.0
        self.deep = 0
        self.over = 0
        self.accidents = 0
        self.status = "ORIGINAL"
        # pre-age the pack if we start mid-life
        self.fade_pct = (100 - EOL_SOH) * start_cycle / LIFE_CYCLES

    def step(self, day_index):
        """Advance one day of use. Returns (telemetry_dict, list_of_event_dicts)."""
        r = self.rng
        events = []

        # one equivalent full cycle per day, with day-to-day depth of discharge
        dod = r.uniform(35, 85)
        self.cycle += 1
        # capacity fade: linear trend + small noise (≈ 20 % over LIFE_CYCLES)
        self.fade_pct += (100 - EOL_SOH) / LIFE_CYCLES * r.uniform(0.8, 1.25)
        soh = max(0.0, 100.0 - self.fade_pct)

        usable_kwh = RATED_KWH * soh / 100
        usable_ah = RATED_AH * soh / 100
        self.energy_kwh += usable_kwh * dod / 100
        self.capacity_ah += usable_ah * dod / 100

        # thermal: seasonal ambient + driving load
        ambient = 27 + 9 * math.sin(2 * math.pi * day_index / 365) + r.gauss(0, 2)
        temp = ambient + r.uniform(3, 12)
        if temp > EXTREME_HOT_C:
            self.t_extreme_h += r.uniform(0.2, 1.0)
            events.append(("THERMAL_EVENT", f"Pack temperature {temp:.1f} °C above {EXTREME_HOT_C} °C"))
        if temp > EXTREME_HOT_C and r.random() < 0.5:
            self.t_charge_extreme_h += r.uniform(0.1, 0.5)

        # rare abuse events
        if r.random() < 0.02:
            self.deep += 1
            events.append(("DEEP_DISCHARGE", f"Min cell voltage {r.uniform(2.3, 2.49):.2f} V"))
        if r.random() < 0.01:
            self.over += 1
            events.append(("OVERCHARGE", f"Max cell voltage {r.uniform(4.26, 4.32):.2f} V"))

        # second life once SoH crosses the exhaustion threshold
        if soh < EOL_SOH and self.status == "ORIGINAL":
            self.status = "REPURPOSED"
            events.append(("STATUS_CHANGE", f"SoH {soh:.1f} % < {EOL_SOH} %: ORIGINAL → REPURPOSED"))

        soc = r.uniform(20, 95)
        r_mohm = R0_MOHM * (1 + 0.6 * self.fade_pct / (100 - EOL_SOH))
        rte = RTE0_PCT - 0.15 * self.fade_pct
        power_fade = 1.2 * self.fade_pct
        fade_per_cycle = (100 - EOL_SOH) / LIFE_CYCLES
        rul = max(0.0, (soh - EOL_SOH) / fade_per_cycle)
        discharging = r.random() < 0.7

        telemetry = {
            "soc_pct": round(soc, 2),
            "soh_pct": round(soh, 3),
            "soce_pct": round(max(0.0, soh - r.uniform(0.2, 0.6)), 3),
            "remaining_capacity_ah": round(usable_ah, 3),
            "capacity_fade_pct": round(self.fade_pct, 3),
            "remaining_power_w": round(RATED_POWER_W * (1 - power_fade / 100), 1),
            "power_fade_pct": round(power_fade, 3),
            "internal_resistance_mohm": round(r_mohm, 4),
            "internal_resistance_increase_pct": round((r_mohm - R0_MOHM) / R0_MOHM * 100, 2),
            "round_trip_efficiency_pct": round(rte, 2),
            "rte_fade_pct": round(RTE0_PCT - rte, 3),
            "self_discharge_pct_per_month": round(1.5 + 0.02 * self.fade_pct + r.uniform(-0.1, 0.1), 2),
            "remaining_life_cycles": round(rul),
            "remaining_life_years": round(rul / 365, 2),
            "pack_voltage_v": round(V_MIN + (V_MAX - V_MIN) * soc / 100 + r.gauss(0, 1.5), 1),
            "current_a": round(-r.uniform(20, 140) if discharging else r.uniform(10, 90), 1),
            "temp_celsius": round(temp, 1),
            "depth_of_discharge_pct": round(dod, 1),
            "cycle_count": self.cycle,
            "energy_throughput_kwh": round(self.energy_kwh, 3),
            "capacity_throughput_ah": round(self.capacity_ah, 2),
            "time_extreme_temp_h": round(self.t_extreme_h, 2),
            "time_charging_extreme_temp_h": round(self.t_charge_extreme_h, 2),
            "deep_discharge_events": self.deep,
            "overcharge_events": self.over,
            "accident_events": self.accidents,
            "lifecycle_status": self.status,
            "raw_telemetry": {
                "cell_v_min": round(3.0 + soc / 100 * 1.15 - r.uniform(0, 0.02), 3),
                "cell_v_max": round(3.0 + soc / 100 * 1.15 + r.uniform(0, 0.02), 3),
                "cell_t_min_c": round(temp - r.uniform(0.5, 2), 1),
                "cell_t_max_c": round(temp + r.uniform(0.5, 2), 1),
                "bms_fault_codes": [],
                "odometer_km": round(day_index * r.uniform(40, 70)),
            },
        }
        return telemetry, events


def post(url, token, payload, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def send(url, token, payload, dry_run, timeout=75, retries=4):
    """POST with retries for Render cold starts and chain-head races."""
    if dry_run:
        print("  DRY-RUN", json.dumps(payload)[:160], "…")
        return True
    for attempt in range(1, retries + 1):
        try:
            status, body = post(url, token, payload, timeout)
            print(f"  {status}  hash={body.get('record_hash', '')[:16]}…  prev={str(body.get('prev_hash', ''))[:16]}…")
            return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 409 and "Chain head moved" in detail and attempt < retries:
                time.sleep(1)
                continue
            print(f"  REJECTED {e.code}: {detail}")
            if e.code in (400, 401, 409):
                return False            # fix the data / token; retrying won't help
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  network error ({e}); server may be waking up — retry {attempt}/{retries}")
            time.sleep(10)
    return False


def main():
    ap = argparse.ArgumentParser(description="DBP VCU simulator")
    ap.add_argument("--url", default="https://dbp-cloud.onrender.com/api/v1/dbp/record")
    ap.add_argument("--bin", default="DBP-2024HT65556-002")
    ap.add_argument("--vcu-id", default="VCU-SIM-01")
    ap.add_argument("--ticks", type=int, default=30, help="number of simulated days")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between days (5 s = 1 day)")
    ap.add_argument("--start-cycle", type=int, default=0,
                    help="cycle count to start from (must be ≥ the battery's last stored count)")
    ap.add_argument("--start-date", default="2026-01-01", help="simulated calendar date of day 1")
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--dry-run", action="store_true", help="print payloads, send nothing")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore saved progress for this battery (only for a NEW battery ID)")
    a = ap.parse_args()

    token = os.environ.get("VCU_TOKEN", "")
    if not a.dry_run and not token:
        sys.exit("Set the VCU_TOKEN environment variable (value from Render → Environment).")

    bat = Battery(a.start_cycle, a.seed)
    day0 = datetime.fromisoformat(a.start_date).replace(tzinfo=timezone.utc)
    first_day = 1
    # Counters on the server can never go down, so a restart must continue
    # where the last run stopped. Progress is kept in a small file per battery.
    state_file = f".vcu_sim_state_{a.bin}.json"
    if not a.dry_run and not a.fresh and os.path.exists(state_file):
        saved = json.load(open(state_file))
        rng_state = saved.pop("rng")
        bat.__dict__.update({k: v for k, v in saved.items() if k != "day"})
        bat.rng.setstate((rng_state[0], tuple(rng_state[1]), rng_state[2]))
        first_day = saved["day"] + 1
        print(f"Resuming {a.bin} from day {saved['day']} (cycle {bat.cycle}) — saved in {state_file}")
    print(f"Simulating {a.ticks} days for {a.bin} → {a.url if not a.dry_run else 'dry run'}")

    for d in range(first_day, first_day + a.ticks):
        ts = day0 + timedelta(days=d, hours=18)
        telemetry, events = bat.step(d)

        for i, (etype, detail) in enumerate(events):
            ev = {"bin": a.bin, "vcu_id": a.vcu_id, "record_type": "EVENT", "event_type": etype,
                  "event_detail": detail, "timestamp": (ts - timedelta(hours=2, minutes=-i)).isoformat(),
                  "cycle_count": telemetry["cycle_count"],
                  "deep_discharge_events": telemetry["deep_discharge_events"],
                  "overcharge_events": telemetry["overcharge_events"],
                  "lifecycle_status": telemetry["lifecycle_status"]}
            print(f"day {d:4d}  EVENT {etype}")
            if not send(a.url, token, ev, a.dry_run):
                sys.exit(1)

        rec = {"bin": a.bin, "vcu_id": a.vcu_id, "timestamp": ts.isoformat(), **telemetry}
        print(f"day {d:4d}  cycle {telemetry['cycle_count']:5d}  SoH {telemetry['soh_pct']:6.2f} %  "
              f"SoC {telemetry['soc_pct']:5.1f} %  T {telemetry['temp_celsius']:5.1f} °C")
        if not send(a.url, token, rec, a.dry_run):
            sys.exit(1)
        if not a.dry_run:
            state = {k: v for k, v in bat.__dict__.items() if k != "rng"}
            state.update(day=d, rng=bat.rng.getstate())
            json.dump(state, open(state_file, "w"))
        if d < first_day + a.ticks - 1:
            time.sleep(a.interval if not a.dry_run else 0)

    print("Done. Look up", a.bin, "in the portal (OEM or Regulator) to see the data and the hash chain.")


if __name__ == "__main__":
    main()
