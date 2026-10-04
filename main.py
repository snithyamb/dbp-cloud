import os
import re
import hashlib
import hmac
import base64
import time
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
VCU_TOKEN    = os.environ.get("VCU_TOKEN", "")
TIER3_KEY    = os.environ.get("TIER3_KEY", "")      # machine access to /full and /provision — never put in the web page

# Portal login (server-side). Passwords and the signing secret live only in Render env vars.
SESSION_SECRET     = os.environ.get("SESSION_SECRET", "")
OEM_PASSWORD       = os.environ.get("OEM_PASSWORD", "")
REGULATOR_PASSWORD = os.environ.get("REGULATOR_PASSWORD", "")
SESSION_TTL_S      = 3600

PORTAL_USERS = {
    "oem@dbp.eu":       {"tier": 2, "password": OEM_PASSWORD},
    "regulator@eu.gov": {"tier": 3, "password": REGULATOR_PASSWORD},
}

PUBLIC_PORTAL_URL = os.environ.get("PUBLIC_PORTAL_URL", "https://snithyamb.github.io/dbp-cloud/")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


class SupabaseError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Supabase {status}: {body}")
        self.status, self.body = status, body


# ── Supabase helpers ──────────────────────────────────────────────────────────

def supabase_select(table, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    with httpx.Client() as client:
        r = client.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json()


def supabase_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    with httpx.Client() as client:
        r = client.post(
            url,
            headers={**HEADERS, "Prefer": "return=representation"},
            json=data,
            timeout=10,
        )
        if r.status_code >= 400:
            raise SupabaseError(r.status_code, r.text)
        return r.json()


def supabase_upsert(table, data, on_conflict="bin"):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    with httpx.Client() as client:
        r = client.post(
            url,
            headers={
                **HEADERS,
                "Prefer": "return=representation,resolution=merge-duplicates",
            },
            params={"on_conflict": on_conflict},
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()


# ── Sessions (HMAC-signed, expiring) ──────────────────────────────────────────

def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def issue_session(email, tier):
    payload = _b64(json.dumps({"sub": email, "tier": tier,
                               "exp": int(time.time()) + SESSION_TTL_S}).encode())
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def session_tier(token):
    """Return the tier carried by a valid, unexpired session token, else 0."""
    if not SESSION_SECRET or not token or "." not in token:
        return 0
    payload, sig = token.rsplit(".", 1)
    good = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return 0
    if data.get("exp", 0) < time.time():
        return 0
    return int(data.get("tier", 0))


def request_tier(request):
    """Tier granted to this request: session bearer token, or TIER3_KEY for machine clients."""
    if TIER3_KEY and hmac.compare_digest(request.headers.get("x-api-key", ""), TIER3_KEY):
        return 3
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return session_tier(auth[len("Bearer "):].strip())
    return 0


async def login(request):
    """POST /api/v1/auth/login  {email, password} → {token, tier, expires_in}"""
    if not SESSION_SECRET:
        return JSONResponse({"detail": "Login not configured"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    user = PORTAL_USERS.get(email)
    if not user or not user["password"] or not hmac.compare_digest(password, user["password"]):
        return JSONResponse({"detail": "Invalid email or password"}, status_code=401)
    return JSONResponse({"token": issue_session(email, user["tier"]),
                         "tier": user["tier"], "expires_in": SESSION_TTL_S})


# ── Canonical JSON + hash chain (Art. 10, SyRS-I01/I02/I04) ──────────────────
#
# Every VCU record is serialised to ONE canonical string (sorted keys, no
# whitespace, numbers formatted exactly like JavaScript). That string is stored
# verbatim in dbp_records.canonical and record_hash = SHA-256(canonical).
# canonical includes prev_hash = record_hash of the previous record for the
# same battery, so every field of every record is covered by the chain.

def _js_number(x):
    """Format a number exactly like JavaScript Number.prototype.toString."""
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        return str(x)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("non-finite number")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()          # shortest round-trip digits
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    exp = t.exponent + (len(t.digits) - len(digits))
    k = len(digits)
    n = exp + k                                   # position of decimal point
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))
    return sign + s


def canonical_json(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return _js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(canonical_json(i) for i in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + canonical_json(v[k])
                              for k in sorted(v, key=str)) + "}"
    raise TypeError(f"cannot canonicalise {type(v).__name__}")


def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def legacy_record_hash(record):
    """Hash used for records written before full-record hashing (6 fields only)."""
    fields = {k: record.get(k) for k in ["bin", "timestamp", "soc_pct", "soh_pct", "cycle_count", "prev_hash"]}
    fields = {k: (int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in fields.items()}
    return sha256_hex(json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def chain_hash_of(record):
    """The hash the next record must carry as prev_hash."""
    return record.get("record_hash") or legacy_record_hash(record)


# Kept for compatibility with earlier callers/tests.
compute_record_hash = legacy_record_hash


# ── VCU payload schema (Annex XIII §5 / Annex IV / Annex VII dynamic data) ───
#
# name: (kind, min, max, monotonic)   kind ∈ num | int
VCU_NUMERIC = {
    # state
    "soc_pct":                       ("num", 0, 100, False),   # Annex XIII 4(d)
    "soh_pct":                       ("num", 0, 120, False),   # Annex VII A
    "soce_pct":                      ("num", 0, 120, False),   # Annex VII A, GTR 22 (EV)
    "remaining_capacity_ah":         ("num", 0, None, False),  # Annex VII A(1)
    "capacity_fade_pct":             ("num", -20, 100, False), # Annex IV A(1)
    "remaining_power_w":             ("num", 0, None, False),  # Annex XIII 5(n)
    "power_fade_pct":                ("num", -20, 100, False), # Annex IV A(2)
    "internal_resistance_mohm":      ("num", 0, None, False),  # Annex IV A(3)
    "internal_resistance_increase_pct": ("num", -100, None, False),
    "round_trip_efficiency_pct":     ("num", 0, 100, False),   # Annex IV A(4)
    "rte_fade_pct":                  ("num", -100, 100, False),
    "self_discharge_pct_per_month":  ("num", 0, 100, False),   # Annex VII A(4)
    "remaining_life_cycles":         ("num", 0, None, False),  # RUL estimate (VCU)
    "remaining_life_years":          ("num", 0, None, False),
    # live measurements
    "pack_voltage_v":                ("num", 0, 2000, False),
    "current_a":                     ("num", -5000, 5000, False),
    "temp_celsius":                  ("num", -60, 150, False), # Annex XIII 4(d)
    "depth_of_discharge_pct":        ("num", 0, 100, False),
    # cumulative — must never decrease (SyRS-D04/D05/D06/D12/D13/D14)
    "cycle_count":                   ("int", 0, None, True),   # Annex VII B(5)
    "energy_throughput_kwh":         ("num", 0, None, True),   # Annex VII B(2)
    "capacity_throughput_ah":        ("num", 0, None, True),   # Annex VII B(3)
    "time_extreme_temp_h":           ("num", 0, None, True),   # Annex VII B(4)
    "time_charging_extreme_temp_h":  ("num", 0, None, True),   # Annex VII B(4)
    "deep_discharge_events":         ("int", 0, None, True),   # Annex VII B(4)
    "overcharge_events":             ("int", 0, None, True),   # Annex VII B(4)
    "accident_events":               ("int", 0, None, True),   # Annex XIII 4(d)
}
LIFECYCLE_STATES = ["ORIGINAL", "REPURPOSED", "REMANUFACTURED", "WASTE"]   # Annex XIII 4(c)
RECORD_TYPES     = ["TELEMETRY", "EVENT"]
EVENT_TYPES      = ["DEEP_DISCHARGE", "OVERCHARGE", "ACCIDENT", "THERMAL_EVENT",
                    "STATUS_CHANGE", "MAINTENANCE", "OTHER"]
VCU_TEXT = {"lifecycle_status", "record_type", "event_type", "event_detail", "vcu_id", "timestamp"}
VCU_ALLOWED = set(VCU_NUMERIC) | VCU_TEXT | {"bin", "raw_telemetry"}
MONOTONIC = [k for k, v in VCU_NUMERIC.items() if v[3]]

# Columns of dbp_records written from a VCU record (canonical holds all of them).
RECORD_COLUMNS = (set(VCU_NUMERIC) | {"bin", "timestamp", "lifecycle_status", "record_type",
                  "event_type", "event_detail", "raw_telemetry"})


def _parse_ts(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset (e.g. 2026-10-04T10:00:00Z)")
    return dt


def validate_vcu_payload(body, prev):
    """Return (clean_record, errors). prev = latest stored record for this battery or None."""
    errors = []
    if not isinstance(body, dict):
        return None, ["body must be a JSON object"]
    unknown = sorted(set(body) - VCU_ALLOWED)
    if unknown:
        errors.append(f"unknown fields: {', '.join(unknown)}")

    bin_id = body.get("bin")
    if not isinstance(bin_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,64}", bin_id):
        errors.append("bin is required (3–64 chars: letters, digits, . _ : -)")

    rec = {"bin": bin_id}
    for k, (kind, lo, hi, _) in VCU_NUMERIC.items():
        if k not in body or body[k] is None:
            continue
        v = body[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            errors.append(f"{k} must be a number")
            continue
        if kind == "int":
            if isinstance(v, float) and not v.is_integer():
                errors.append(f"{k} must be an integer")
                continue
            v = int(v)
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            errors.append(f"{k}={v} out of range [{lo}, {hi if hi is not None else '∞'}]")
            continue
        rec[k] = v

    # timestamp (VCU clock, must carry a timezone)
    ts = body.get("timestamp")
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if not isinstance(ts, str):
        errors.append("timestamp must be an ISO 8601 string")
    else:
        try:
            ts_dt = _parse_ts(ts)
            rec["timestamp"] = ts
        except ValueError as e:
            errors.append(f"timestamp invalid: {e}")
            ts_dt = None

    rtype = body.get("record_type", "TELEMETRY")
    if rtype not in RECORD_TYPES:
        errors.append(f"record_type must be one of {RECORD_TYPES}")
    rec["record_type"] = rtype
    if rtype == "EVENT":
        if body.get("event_type") not in EVENT_TYPES:
            errors.append(f"EVENT records need event_type in {EVENT_TYPES}")
    elif body.get("event_type") is not None:
        errors.append("event_type is only allowed on EVENT records")
    if body.get("event_type") is not None:
        rec["event_type"] = body["event_type"]
    if body.get("event_detail") is not None:
        if not isinstance(body["event_detail"], str) or len(body["event_detail"]) > 500:
            errors.append("event_detail must be a string ≤ 500 chars")
        else:
            rec["event_detail"] = body["event_detail"]

    status = body.get("lifecycle_status")
    if status is not None:
        if status not in LIFECYCLE_STATES:
            errors.append(f"lifecycle_status must be one of {LIFECYCLE_STATES}")
        else:
            rec["lifecycle_status"] = status

    vcu_id = body.get("vcu_id", "VCU")
    if not isinstance(vcu_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", vcu_id):
        errors.append("vcu_id must be 1–64 chars: letters, digits, . _ : -")
    rec["data_provider"] = vcu_id if isinstance(vcu_id, str) else "VCU"

    if "raw_telemetry" in body and body["raw_telemetry"] is not None:
        raw = body["raw_telemetry"]
        if not isinstance(raw, dict):
            errors.append("raw_telemetry must be a JSON object")
        else:
            try:
                if len(canonical_json(raw)) > 8000:
                    errors.append("raw_telemetry too large (max 8000 chars canonical)")
                else:
                    rec["raw_telemetry"] = raw
            except (TypeError, ValueError) as e:
                errors.append(f"raw_telemetry: {e}")

    if len([k for k in VCU_NUMERIC if k in rec]) == 0 and rtype == "TELEMETRY":
        errors.append("TELEMETRY record carries no measurements")

    # Rules against the previous record (non-resettable counters, ordering, terminal state)
    if prev and not errors:
        for k in MONOTONIC:
            if k in rec and prev.get(k) is not None and rec[k] < prev[k]:
                errors.append(f"{k} cannot decrease ({prev[k]} → {rec[k]})")
        if prev.get("lifecycle_status") == "WASTE" and rec.get("lifecycle_status", "WASTE") != "WASTE":
            errors.append("battery is WASTE; status cannot change")
        try:
            if prev.get("timestamp") and ts_dt and ts_dt < _parse_ts(prev["timestamp"]):
                errors.append("timestamp is earlier than the previous record")
        except ValueError:
            pass

    return (None if errors else rec), errors


# ── Field sets per Annex XIII tier ───────────────────────────────────────────
#
#  TIER 1  PUBLIC       — no auth (Art. 77; Annex XIII "public" data)
#  TIER 2  STAKEHOLDER  — interested persons: session (oem) or TIER3_KEY
#  TIER 3  REGULATOR    — session (regulator) or TIER3_KEY
#
# Static fields are read from battery_passport; dynamic fields from the latest
# dbp_records row (in-use data is for interested persons, so Tier 2+).

STATIC_PUBLIC = [
    "manufacturer", "manufacturer_address", "manufacturer_country", "manufacturer_url",
    "manufacturer_email", "operator_id", "manufacture_location",
    "model",                    # §1(f) commercial designation
    "battery_category", "chemistry", "nominal_voltage_v", "capacity_ah", "capacity_kwh",
    "weight_kg", "weight_tolerance_kg", "manufacture_date", "country_of_origin", "qr_code_url",
    # §2 carbon footprint
    "carbon_footprint_kg_co2_per_kwh", "carbon_footprint_breakdown_json",
    "carbon_footprint_study_url", "cf_verification_body", "cf_verification_report_url",
    # §3 due-diligence report is public (Art. 52)
    "due_diligence_policy_url", "annual_due_diligence_report_url",
    # §4 critical raw materials + recycled content are public
    "cobalt_pct", "lithium_pct", "nickel_pct", "graphite_pct",
    "recycled_cobalt_pct", "recycled_lithium_pct", "recycled_nickel_pct",
    # §5 beginning-of-life performance
    "min_voltage_v", "max_voltage_v", "power_capability_w", "expected_lifetime_cycles",
    "expected_lifetime_years", "initial_round_trip_efficiency_pct",
    "round_trip_efficiency_50pct_cycle_pct", "initial_internal_resistance_mohm",
    "capacity_exhaustion_threshold_pct", "temp_storage_min_c", "temp_storage_max_c",
    "temp_operating_min_c", "temp_operating_max_c", "thermal_management_type",
    # §6 end of life
    "dismantling_instructions_url", "safety_handling_url", "hazard_class", "takeback_scheme_url",
    "second_life_potential", "second_life_assessment_url", "spare_parts_available_until",
    "spare_parts_url", "eou_guidance_url", "label_meanings_url",
]

STATIC_STAKEHOLDER = [
    "cobalt_country_of_origin", "cobalt_supplier_audit_url",
    "graphite_country_of_origin", "graphite_supplier_audit_url",
    "lithium_country_of_origin", "lithium_supplier_audit_url",
    "nickel_country_of_origin", "nickel_supplier_audit_url",
    "notified_body_name", "audit_certificate_url", "grievance_mechanism_url",
    "risk_coverage_statement",
    "bom_json", "hazardous_substances_json", "art6_compliant", "restriction_declaration_url",
    "cycle_life_test_c_rate", "test_conditions_url", "thermal_management_spec_url",
    "eol_environmental_impact_url",
]

STATIC_REGULATOR = [
    "co2_footprint_kg", "recycled_content_pct", "supply_chain_origin",
    "certification_ref", "eol_plan", "audit_trail_ref",
]

DYNAMIC_PUBLIC = ["lifecycle_status"]                     # battery status is public
DYNAMIC_STAKEHOLDER = list(VCU_NUMERIC) + ["record_type", "data_provider"]


def _pick(d, keys):
    """Return a dict of keys→values, skipping None values."""
    return {k: v for k in keys if (v := d.get(k)) is not None}


def _latest(bin_id, select="*"):
    rows = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}", "order": "id.desc", "limit": "1", "select": select,
    })
    return rows[0] if rows else None


def _latest_status(bin_id):
    rows = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}", "lifecycle_status": "not.is.null",
        "order": "id.desc", "limit": "1", "select": "lifecycle_status",
    })
    return rows[0]["lifecycle_status"] if rows else None


def _derived(static, latest):
    """VCU-supplied values win; otherwise derive from SoH (Annex VII A)."""
    out = {}
    cap, power, soh = static.get("capacity_ah"), static.get("power_capability_w"), latest.get("soh_pct")
    out["remaining_capacity_ah"] = latest.get("remaining_capacity_ah") if latest.get("remaining_capacity_ah") is not None else (
        round(cap * soh / 100, 3) if cap and soh else None)
    out["remaining_power_w"] = latest.get("remaining_power_w") if latest.get("remaining_power_w") is not None else (
        round(power * soh / 100, 2) if power and soh else None)
    out["capacity_fade_pct"] = latest.get("capacity_fade_pct") if latest.get("capacity_fade_pct") is not None else (
        round(100 - soh, 3) if soh is not None else None)
    return out


# ── API endpoints ─────────────────────────────────────────────────────────────

async def get_public_info(request):
    """Tier 1 — Public (no auth): static public fields + battery status."""
    bin_id = request.path_params["bin_id"]
    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    latest = _latest(bin_id, "bin,timestamp")
    if not passport and not latest:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    static = passport[0] if passport else {}
    return JSONResponse({
        "bin_id": bin_id,
        "recorded_at": latest["timestamp"] if latest else None,
        "access_tier": "PUBLIC",
        **_pick(static, STATIC_PUBLIC),
        "lifecycle_status": _latest_status(bin_id),
        "qr_svg_url": f"/api/v1/dbp/{bin_id}/qr",
    })


async def get_stakeholder_info(request):
    """Tier 2 — interested persons: adds supply chain, materials and all VCU in-use data."""
    bin_id = request.path_params["bin_id"]
    if request_tier(request) < 2:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    latest = _latest(bin_id)
    if not latest:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    static = passport[0] if passport else {}

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "access_tier": "STAKEHOLDER",
        **_pick(static, STATIC_PUBLIC),
        **_pick(static, STATIC_STAKEHOLDER),
        **_pick(latest, DYNAMIC_STAKEHOLDER),
        **{k: v for k, v in _derived(static, latest).items() if v is not None},
        "lifecycle_status": _latest_status(bin_id),
        "record_hash": latest.get("record_hash"),
    })


async def get_full_passport(request):
    """Tier 3 — Regulator: everything + last 50 records with canonical payloads and hashes."""
    bin_id = request.path_params["bin_id"]
    if request_tier(request) < 3:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    telemetry = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}", "order": "id.desc", "limit": "50", "select": "*",
    })
    if not telemetry:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = telemetry[0]
    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    static = passport[0] if passport else {}

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "access_tier": "REGULATOR",
        **_pick(static, STATIC_PUBLIC),
        **_pick(static, STATIC_STAKEHOLDER),
        **_pick(static, STATIC_REGULATOR),
        **_pick(latest, DYNAMIC_STAKEHOLDER),
        **{k: v for k, v in _derived(static, latest).items() if v is not None},
        "lifecycle_status": _latest_status(bin_id),
        "hash_chain_depth": len(telemetry),
        "latest_hash": chain_hash_of(latest),
        "raw_telemetry": telemetry,
    })


async def post_dbp_record(request):
    """
    POST /api/v1/dbp/record   (Authorization: Bearer <VCU_TOKEN>)
    VCU pushes a TELEMETRY or EVENT record. The server validates it, builds the
    canonical payload (all fields + prev_hash), hashes it and appends it.
    Records are append-only: the database rejects UPDATE and DELETE.
    """
    auth = request.headers.get("Authorization", "")
    if not VCU_TOKEN or not hmac.compare_digest(auth, f"Bearer {VCU_TOKEN}"):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    bin_id = body.get("bin") if isinstance(body, dict) else None
    prev = _latest(bin_id) if isinstance(bin_id, str) and bin_id else None
    rec, errors = validate_vcu_payload(body, prev)
    if errors:
        status = 409 if any(("cannot decrease" in e) or ("status cannot change" in e) or ("earlier than the previous" in e) for e in errors) else 400
        return JSONResponse({"detail": "Record rejected", "errors": errors}, status_code=status)

    rec["prev_hash"] = chain_hash_of(prev) if prev else "GENESIS"
    rec["received_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    canonical = canonical_json(rec)
    record_hash = sha256_hex(canonical)

    row = {k: v for k, v in rec.items() if k in RECORD_COLUMNS}
    row.update({"prev_hash": rec["prev_hash"], "data_provider": rec["data_provider"],
                "received_at": rec["received_at"], "canonical": canonical, "record_hash": record_hash})
    try:
        result = supabase_insert("dbp_records", row)
    except SupabaseError as e:
        if e.status == 409:   # unique (bin, prev_hash): another record was appended first
            return JSONResponse({"detail": "Chain head moved; retry"}, status_code=409)
        return JSONResponse({"detail": "Storage error", "error": e.body[:300]}, status_code=502)
    return JSONResponse({"status": "ok", "record_hash": record_hash,
                         "prev_hash": rec["prev_hash"], "data": result}, status_code=201)


async def vcu_schema(request):
    """GET /api/v1/dbp/schema — machine-readable description of what the VCU may send."""
    return JSONResponse({
        "endpoint": "POST /api/v1/dbp/record",
        "auth": "Authorization: Bearer <VCU_TOKEN>",
        "required": ["bin"],
        "numeric_fields": {k: {"type": v[0], "min": v[1], "max": v[2], "non_decreasing": v[3]}
                           for k, v in VCU_NUMERIC.items()},
        "lifecycle_status": LIFECYCLE_STATES,
        "record_type": RECORD_TYPES,
        "event_type": EVENT_TYPES,
        "other_fields": {"timestamp": "ISO 8601 with offset (default: server time)",
                         "vcu_id": "writer identity (default 'VCU')",
                         "event_detail": "text ≤ 500", "raw_telemetry": "JSON object ≤ 8000 chars"},
        "hashing": "record_hash = SHA-256(canonical JSON of all accepted fields + prev_hash + received_at)",
    })


async def get_qr(request):
    """GET /api/v1/dbp/{bin}/qr — SVG QR code (ISO/IEC 18004) linking to the public passport."""
    import segno
    bin_id = request.path_params["bin_id"]
    if not re.fullmatch(r"[A-Za-z0-9._:-]{3,64}", bin_id):
        return JSONResponse({"detail": "invalid BIN"}, status_code=400)
    url = f"{PUBLIC_PORTAL_URL}?bin={bin_id}"
    qr = segno.make(url, error="m")
    import io
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


async def provision_passport(request):
    """
    POST /api/v1/dbp/provision   (x-api-key: TIER3_KEY)
    Admin endpoint to set / update static battery_passport fields.
    """
    api_key = request.headers.get("x-api-key", "")
    if not TIER3_KEY or not hmac.compare_digest(api_key, TIER3_KEY):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    body = await request.json()
    if "bin" not in body:
        return JSONResponse({"detail": "'bin' field required"}, status_code=400)

    allowed = set(STATIC_PUBLIC + STATIC_STAKEHOLDER + STATIC_REGULATOR + ["bin", "voltage_v"])
    payload = {k: v for k, v in body.items() if k in allowed}

    result = supabase_upsert("battery_passport", payload)
    return JSONResponse({"status": "ok", "data": result}, status_code=200)


async def list_batteries(request):
    data = supabase_select("dbp_records", {"select": "bin", "order": "id.desc"})
    bins = list(dict.fromkeys(r["bin"] for r in data))
    return JSONResponse({"batteries": bins})


async def health(request):
    return JSONResponse({"status": "ok", "annex_xiii_fields": "v3", "vcu_ingest": "v2-full-hash"})


# ── Routes ────────────────────────────────────────────────────────────────────

routes = [
    Route("/",                                health),
    Route("/health",                          health),
    Route("/api/v1/dbp/schema",               vcu_schema),
    Route("/api/v1/dbp/record",               post_dbp_record,    methods=["POST"]),
    Route("/api/v1/dbp/provision",            provision_passport, methods=["POST"]),
    Route("/api/v1/auth/login",               login,              methods=["POST"]),
    Route("/api/v1/dbp/{bin_id}/public",      get_public_info),
    Route("/api/v1/dbp/{bin_id}/stakeholder", get_stakeholder_info),
    Route("/api/v1/dbp/{bin_id}/full",        get_full_passport),
    Route("/api/v1/dbp/{bin_id}/qr",          get_qr),
    Route("/api/v1/batteries",                list_batteries),
]

app = Starlette(routes=routes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
