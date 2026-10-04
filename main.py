import os
import hashlib
import json
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
VCU_TOKEN    = os.environ.get("VCU_TOKEN", "")
TIER3_KEY    = os.environ.get("TIER3_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


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
        r.raise_for_status()
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


# ── Hash chain ────────────────────────────────────────────────────────────────

def _js_number(v):
    """Render floats the way JavaScript's JSON.stringify does (98.0 -> 98)."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def compute_record_hash(record):
    """
    SHA-256 of a record's key fields, deterministically sorted.
    Canonical form is byte-identical to the browser's
    JSON.stringify(fields, sortedKeys) so the portal can verify the chain.
    """
    fields = {k: _js_number(record.get(k)) for k in sorted([
        "bin", "timestamp", "soc_pct", "soh_pct", "cycle_count", "prev_hash"
    ])}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Field sets per Annex XIII tier ───────────────────────────────────────────
#
#  TIER 1  PUBLIC       — no auth required (Art. 77(1))
#  TIER 2  STAKEHOLDER  — VCU_TOKEN bearer auth (Art. 77(2))
#  TIER 3  REGULATOR    — TIER3_KEY header (Art. 77(3))
#
# Static fields are read from battery_passport.
# Dynamic fields are read from the latest dbp_records row.

# Annex XIII §1 — General / manufacturer (all public)
STATIC_PUBLIC = [
    "manufacturer",             # §1(a)
    "manufacturer_address",     # §1(b)
    "manufacturer_country",     # §1(b)
    "manufacturer_url",         # §1(c)
    "manufacturer_email",       # §1(c)
    "operator_id",              # §1(d)
    "manufacture_location",     # §1(e)
    "battery_category",         # §1(g)  EV / LMT / Industrial
    "chemistry",                # §1(h)
    "nominal_voltage_v",        # §1(i)  (alias: voltage_v for legacy)
    "capacity_ah",              # §1(j)  rated capacity
    "capacity_kwh",             # derived / legacy
    "weight_kg",                # §1(k)
    "weight_tolerance_kg",      # §1(k)
    "manufacture_date",         # §1(l)
    "country_of_origin",        # §1(m)
    "qr_code_url",              # §1(n)
    # §2 Carbon footprint — public disclosure (Art. 7)
    "carbon_footprint_kg_co2_per_kwh",   # §2(a)
    "carbon_footprint_breakdown_json",   # §2(b)
    "carbon_footprint_study_url",        # §2(d)
    "cf_verification_body",              # §2(e)
    "cf_verification_report_url",        # §2(e)
    # §5 BoL performance — public
    "min_voltage_v",                        # §5(b)
    "max_voltage_v",                        # §5(b)
    "power_capability_w",                   # §5(c)
    "expected_lifetime_cycles",             # §5(d)
    "expected_lifetime_years",              # §5(e)
    "initial_round_trip_efficiency_pct",    # §5(g)
    "round_trip_efficiency_50pct_cycle_pct",# §5(h)
    "initial_internal_resistance_mohm",     # §5(i)
    "temp_storage_min_c",                   # §5(q)
    "temp_storage_max_c",                   # §5(q)
    "thermal_management_type",              # §5(r)
    # §6 End-of-life — public
    "dismantling_instructions_url",  # §6(a)
    "safety_handling_url",           # §6(b)
    "hazard_class",                  # §6(b)
    "takeback_scheme_url",           # §6(c)
    "second_life_potential",         # §6(e)
    "second_life_assessment_url",    # §6(e)
    "spare_parts_available_until",   # §6(f)
    "spare_parts_url",               # §6(f)
    "eou_guidance_url",              # §6(g)
    "label_meanings_url",            # §6(h)
]

# Annex XIII §3+§4 — supply chain + materials (Stakeholder tier)
STATIC_STAKEHOLDER = [
    # §3 Due diligence
    "due_diligence_policy_url",
    "cobalt_country_of_origin",
    "cobalt_supplier_audit_url",
    "graphite_country_of_origin",
    "graphite_supplier_audit_url",
    "lithium_country_of_origin",
    "lithium_supplier_audit_url",
    "nickel_country_of_origin",
    "nickel_supplier_audit_url",
    "notified_body_name",
    "audit_certificate_url",
    "grievance_mechanism_url",
    "annual_due_diligence_report_url",
    "risk_coverage_statement",
    # §4 Materials
    "bom_json",
    "cobalt_pct",
    "lithium_pct",
    "nickel_pct",
    "graphite_pct",
    "hazardous_substances_json",
    "art6_compliant",
    "restriction_declaration_url",
    # §5 additional
    "cycle_life_test_c_rate",
    "test_conditions_url",
    "thermal_management_spec_url",
    # §6 additional
    "eol_environmental_impact_url",
]

# Regulator-only static fields
STATIC_REGULATOR = [
    "recycled_cobalt_pct",    # §4(e) — deferred to 2028 but store-ready
    "recycled_lithium_pct",   # §4(f)
    "recycled_nickel_pct",    # §4(g)
    # legacy regulator fields
    "co2_footprint_kg",
    "recycled_content_pct",
    "supply_chain_origin",
    "certification_ref",
    "eol_plan",
    "audit_trail_ref",
]

# Dynamic fields from dbp_records (telemetry)
DYNAMIC_PUBLIC = ["soc_pct", "soh_pct", "cycle_count", "lifecycle_status"]
DYNAMIC_STAKEHOLDER = ["temp_celsius", "current_a", "depth_of_discharge_pct"]  # §5(j,k,l) real-time
DYNAMIC_REGULATOR   = []  # raw_telemetry + hash chain returned separately


def _pick(d, keys):
    """Return a dict of keys→values, skipping None values."""
    return {k: v for k in keys if (v := d.get(k)) is not None}


# ── API endpoints ─────────────────────────────────────────────────────────────

async def get_public_info(request):
    """
    Tier 1 — Public (no auth).
    Annex XIII §1, §2, §5(b-j), §6 static fields + SoC/SoH/cycles.
    """
    bin_id = request.path_params["bin_id"]

    telemetry = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp," + ",".join(DYNAMIC_PUBLIC),
    })
    if not telemetry:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = telemetry[0]

    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    static = passport[0] if passport else {}

    # Remaining capacity derived if not explicit
    capacity_ah = static.get("capacity_ah")
    soh = latest.get("soh_pct")
    remaining_capacity_ah = round(capacity_ah * soh / 100, 3) if capacity_ah and soh else None

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "access_tier": "PUBLIC",
        # §1 identity
        **_pick(static, STATIC_PUBLIC),
        # §5 derived dynamic
        "remaining_capacity_ah": remaining_capacity_ah,   # §5(m)
        # dynamic telemetry
        **_pick(latest, DYNAMIC_PUBLIC),
    })


async def get_stakeholder_info(request):
    """
    Tier 2 — Authorised Stakeholder (Bearer VCU_TOKEN).
    Adds §3 supply chain, §4 materials, real-time temp/current/DoD.
    """
    bin_id = request.path_params["bin_id"]

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    telemetry = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "*",
    })
    if not telemetry:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = telemetry[0]

    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    static = passport[0] if passport else {}

    capacity_ah = static.get("capacity_ah")
    power_w     = static.get("power_capability_w")
    soh         = latest.get("soh_pct")
    remaining_capacity_ah  = round(capacity_ah * soh / 100, 3) if capacity_ah and soh else None
    remaining_power_w      = round(power_w * soh / 100, 2)    if power_w and soh else None

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "access_tier": "STAKEHOLDER",
        # Tier 1 fields
        **_pick(static, STATIC_PUBLIC),
        # Tier 2 static additions
        **_pick(static, STATIC_STAKEHOLDER),
        # §5 derived
        "remaining_capacity_ah": remaining_capacity_ah,  # §5(m)
        "remaining_power_w": remaining_power_w,          # §5(n)
        # dynamic telemetry
        **_pick(latest, DYNAMIC_PUBLIC),
        **_pick(latest, DYNAMIC_STAKEHOLDER),
    })


async def get_full_passport(request):
    """
    Tier 3 — Regulator (x-api-key: TIER3_KEY).
    Full passport: all static fields + telemetry history + hash chain.
    """
    bin_id = request.path_params["bin_id"]

    api_key = request.headers.get("x-api-key", "")
    if not TIER3_KEY or api_key != TIER3_KEY:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    telemetry = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "50",
        "select": "*",
    })
    if not telemetry:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = telemetry[0]

    passport = supabase_select("battery_passport", {"bin": f"eq.{bin_id}"})
    static = passport[0] if passport else {}

    capacity_ah = static.get("capacity_ah")
    power_w     = static.get("power_capability_w")
    soh         = latest.get("soh_pct")
    remaining_capacity_ah  = round(capacity_ah * soh / 100, 3) if capacity_ah and soh else None
    remaining_power_w      = round(power_w * soh / 100, 2)    if power_w and soh else None

    latest_hash  = compute_record_hash(latest)
    genesis_hash = telemetry[-1].get("prev_hash") if telemetry else None

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "access_tier": "REGULATOR",
        # All static tiers
        **_pick(static, STATIC_PUBLIC),
        **_pick(static, STATIC_STAKEHOLDER),
        **_pick(static, STATIC_REGULATOR),
        # §5 derived
        "remaining_capacity_ah": remaining_capacity_ah,  # §5(m)
        "remaining_power_w": remaining_power_w,          # §5(n)
        # Dynamic telemetry
        **_pick(latest, DYNAMIC_PUBLIC),
        **_pick(latest, DYNAMIC_STAKEHOLDER),
        # Hash chain (Art. 10)
        "hash_chain_depth": len(telemetry),
        "genesis_hash": genesis_hash,
        "latest_hash": latest_hash,
        # Full telemetry history
        "raw_telemetry": telemetry,
    })


async def post_dbp_record(request):
    """
    POST /api/v1/dbp/record
    VCU telemetry push. Attaches prev_hash before insert.
    """
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {VCU_TOKEN}":
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    body = await request.json()

    last = supabase_select("dbp_records", {
        "bin": f"eq.{body.get('bin', '')}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp,soc_pct,soh_pct,cycle_count,prev_hash",
    })

    body["prev_hash"] = compute_record_hash(last[0]) if last else "GENESIS"
    result = supabase_insert("dbp_records", body)
    return JSONResponse({"status": "ok", "data": result}, status_code=201)


async def provision_passport(request):
    """
    POST /api/v1/dbp/provision
    Admin endpoint to set / update static battery_passport fields.
    Auth: same TIER3_KEY as regulator.

    Body: { "bin": "BIN-001", <any static fields> }
    All Annex XIII static fields accepted.
    """
    api_key = request.headers.get("x-api-key", "")
    if not TIER3_KEY or api_key != TIER3_KEY:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    body = await request.json()
    if "bin" not in body:
        return JSONResponse({"detail": "'bin' field required"}, status_code=400)

    # Allow any of the known static fields
    allowed = set(
        STATIC_PUBLIC + STATIC_STAKEHOLDER + STATIC_REGULATOR
        + ["bin", "capacity_ah", "nominal_voltage_v", "voltage_v"]
    )
    payload = {k: v for k, v in body.items() if k in allowed}

    result = supabase_upsert("battery_passport", payload)
    return JSONResponse({"status": "ok", "data": result}, status_code=200)


async def list_batteries(request):
    data = supabase_select("dbp_records", {
        "select": "bin",
        "order": "timestamp.desc",
    })
    bins = list(dict.fromkeys(r["bin"] for r in data))
    return JSONResponse({"batteries": bins})


async def health(request):
    return JSONResponse({"status": "ok", "annex_xiii_fields": "v2"})


# ── Routes ────────────────────────────────────────────────────────────────────

routes = [
    Route("/",                              health),
    Route("/health",                        health),
    Route("/api/v1/dbp/{bin_id}/public",    get_public_info),
    Route("/api/v1/dbp/{bin_id}/stakeholder", get_stakeholder_info),
    Route("/api/v1/dbp/{bin_id}/full",      get_full_passport),
    Route("/api/v1/dbp/record",             post_dbp_record,    methods=["POST"]),
    Route("/api/v1/dbp/provision",          provision_passport, methods=["POST"]),
    Route("/api/v1/batteries",              list_batteries),
]

app = Starlette(routes=routes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
