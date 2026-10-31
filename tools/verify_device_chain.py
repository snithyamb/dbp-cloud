#!/usr/bin/env python3
"""
verify_device_chain.py — independent auditor check of DBP records signed by the vehicle.

The ESP8266 (BMS + VCU) signs every record with its own ECDSA P-256 key and chains it to
the previous record *on the device*, before it is ever uploaded. This script checks, for the
records of one battery:

  1. hash      SHA-256(payload) equals the hash the device stored
  2. signature the ECDSA signature verifies with the device public key
  3. chain     each payload names the previous record's hash; sequence gaps are reported
               (a gap = a record the device signed that never reached the cloud)
  4. values    the signed payload matches the values stored in the database
               (cycle count, events, SoC, SoH, energy, capacity, timestamp)
  5. SoH       which SoH algorithm (fingerprint) produced each value, and the result of the
               VCU's independent capacity cross-check

Input: the Tier 3 response of /api/v1/dbp/<BIN>/full, either saved to a file or fetched live.

  python verify_device_chain.py --file full.json
  python verify_device_chain.py --bin DBP-2024HT65556-003 --api-key <TIER3_KEY>
  python verify_device_chain.py --bin DBP-2024HT65556-003 --session <Bearer token from the portal>

The public key is taken from the DEVICE_KEY event if it is among the records; otherwise pass
--pubkey <130 hex chars> (type  k  in the ESP8266 Serial Monitor to print it).

Requires: pip install cryptography
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, encode_dss_signature

API = "https://dbp-cloud.onrender.com/api/v1/dbp/{}/full"
FIELDS = ["seq", "timestamp", "record_type", "event_type", "cycle_count", "deep_discharge_events",
          "overcharge_events", "accident_events", "soc_pct", "soh_pct", "energy_throughput_kwh",
          "capacity_throughput_ah", "soh_algo16", "prev"]


def load(args):
    if args.file:
        return json.load(open(args.file, encoding="utf-8"))
    headers = {}
    if args.api_key:
        headers["x-api-key"] = args.api_key
    if args.session:
        headers["Authorization"] = "Bearer " + args.session
    req = urllib.request.Request(API.format(args.bin), headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def num_eq(a, b):
    return a is not None and abs(float(a) - float(b)) < 1e-6


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file"); ap.add_argument("--bin"); ap.add_argument("--api-key"); ap.add_argument("--session")
    ap.add_argument("--pubkey", help="device public key, 130 hex chars (04…)")
    a = ap.parse_args()
    if not a.file and not a.bin:
        ap.error("give --file or --bin")
    data = load(a)
    records = data.get("raw_telemetry", data if isinstance(data, list) else [])
    signed = [r for r in records if isinstance(r.get("raw_telemetry"), dict) and "dev" in r["raw_telemetry"]]
    print(f"Battery {data.get('bin_id', '?')}: {len(records)} records in window, {len(signed)} signed by the device")
    if not signed:
        print("No device-signed records (firmware v2 not running yet, or older records only).")
        return 1

    pub_hex = a.pubkey
    for r in records:
        d = r.get("event_detail") or ""
        if d.startswith("DEVICE_KEY") and " pub " in d:
            pub_hex = pub_hex or d.split(" pub ")[1].strip()
    if not pub_hex:
        print("Public key not found in this window — pass --pubkey (type k in the Serial Monitor).")
        return 1
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(pub_hex))
    key_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:16]
    print(f"Device key id {key_id}\n")

    signed.sort(key=lambda r: r["raw_telemetry"]["dev"]["seq"])
    ok = bad = gaps = 0
    prev_hash, prev_seq = None, None
    algos, checks = {}, []
    for r in signed:
        dev = r["raw_telemetry"]["dev"]
        p = dict(zip(FIELDS, dev["payload"].split("|")))
        problems = []
        if hashlib.sha256(dev["payload"].encode()).hexdigest() != dev["hash"]:
            problems.append("hash mismatch")
        try:
            sig = bytes.fromhex(dev["sig"])
            pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                       bytes.fromhex(dev["hash"]), ec.ECDSA(Prehashed(hashes.SHA256())))
        except (InvalidSignature, ValueError):
            problems.append("BAD SIGNATURE")
        if dev.get("key_id") != key_id:
            problems.append("signed with a different key")
        seq = int(p["seq"])
        if prev_seq is not None:
            if seq == prev_seq + 1 and p["prev"] != prev_hash:
                problems.append("chain broken (prev hash does not match)")
            elif seq > prev_seq + 1:
                gaps += seq - prev_seq - 1
                print(f"  ⚠ gap: device records {prev_seq + 1}–{seq - 1} are missing from the cloud")
        # signed values vs stored values
        if ts(p["timestamp"]) != ts(r["timestamp"]):
            problems.append("timestamp differs")
        if p["record_type"] != r.get("record_type"):
            problems.append("record_type differs")
        for f in ("cycle_count", "deep_discharge_events", "overcharge_events", "accident_events"):
            if r.get(f) is not None and int(p[f]) != int(r[f]):
                problems.append(f"{f} differs ({p[f]} signed, {r[f]} stored)")
        for f in ("soc_pct", "soh_pct", "energy_throughput_kwh", "capacity_throughput_ah"):
            if p[f] != "-" and r.get(f) is not None and not num_eq(r[f], p[f]):
                problems.append(f"{f} differs ({p[f]} signed, {r[f]} stored)")
        sa = r["raw_telemetry"].get("soh_algo")
        if sa:
            if sa["hash"][:16] != p["soh_algo16"]:
                problems.append("SoH algorithm hash differs from signed one")
            algos.setdefault(sa["hash"], [sa["id"], sa["version"], 0])[2] += 1
            if sa.get("crosscheck") in ("PASS", "FAIL"):
                checks.append((seq, sa["crosscheck"], sa.get("vcu_measured_soh_pct"), r.get("soh_pct"), sa.get("deviation_pct")))
        label = r.get("event_type") or r.get("record_type")
        if problems:
            bad += 1
            print(f"  ✗ seq {seq:>5}  {label:<15} {'; '.join(problems)}")
        else:
            ok += 1
        prev_hash, prev_seq = dev["hash"], seq

    print(f"\nDevice signatures: {ok} valid, {bad} with problems, {gaps} missing record(s)")
    print("\nSoH algorithms seen (fingerprint → records):")
    for h, (i, v, n) in algos.items():
        print(f"  {i} v{v}  {h[:16]}…  {n} record(s)")
    if len(algos) > 1:
        print("  ⚠ the SoH algorithm/calibration changed inside this window — see SOH_ALGORITHM events")
    for r in records:
        if (r.get("event_detail") or "").startswith(("SOH_ALGORITHM", "SOH_CROSSCHECK_FAIL")):
            print(f"  event {r['timestamp'][:19]}  {r['event_detail'][:110]}")
    if checks:
        last = checks[-1]
        fails = sum(1 for c in checks if c[1] == "FAIL")
        print(f"\nVCU SoH cross-check: {len(checks)} telemetry records with a result, {fails} FAIL; "
              f"latest: BMS {last[3]} % vs VCU-measured {last[2]} % (deviation {last[4]} %) → {last[1]}")
    verdict = "PASS" if bad == 0 else "FAIL"
    print(f"\nVERDICT: {verdict}" + (" (with gaps)" if gaps else ""))
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
