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


# ââ Supabase helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
            params={"on_conflict": on_conflict},   ← ADD THIS LINE
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()


# ââ Hash chain ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def compute_record_hash(record):
    """SHA-256 hash of a record's key fields, deterministically sorted."""
    fields = {k: record.get(k) for k in sorted([
        "bin", "timestamp", "soc_pct", "soh_pct", "cycle_count", "prev_hash"
    ])}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


# ââ Field sets per Annex XIII tier âââââââââââââââââââââââââââââââââââââââââââ
#
#  TIER 1  PUBLIC       â no auth required (Art. 77(1))
#  TIER 2  STAKEHOLDER  â VCU_TOKEN bearer auth (Art. 77(2))
#  TIER 3  REGULATOR    â TIER3_KEY header (Art. 77(3))
#
# Static fields are read from battery_passport.
# Dynamic fields are read from the latest dbp_records row.

# Annex XIII Â§1 â General / manufacturer (all public)
STATIC_PUBLIC = [
    "manufacturer",             # Â§1(a)
    "manufacturer_address",     # Â§1(b)
    "manufacturer_country",     # Â§1(b)
    "manufacturer_url",         # Â§1(c)
    "manufacturer_email",       # Â§1(c)
    "operator_id",              # Â§1(d)
    "manufacture_location",     # Â§1(e)
    "battery_category",         # Â§1(g)  EV / LMT / Industrial
    "chemistry",                # Â§1(h)
    "nominal_voltage_v",        # Â§1(i)  (alias: voltage_v for legacy)
    "capacity_ah",              # Â§1(j)  rated capacity
    "capacity_kwh",             # derived / legacy
    "weight_kg",                # Â§1(k)
    "weight_tolerance_kg",      # Â§1(k)
    "manufacture_date",         # Â§1(l)
    "country_of_origin",        # Â§1(m)
    "qr_code_url",              # Â§1(n)
    # Â§2 Carbon footprint â public disclosure (Art. 7)
    "carbon_footprint_kg_co2_per_kwh",   # Â§2(a)
    "carbon_footprint_breakdown_json",   # Â§2(b)
    "carbon_footprint_study_url",        # Â§2(d)
    "cf_verification_body",              # Â§2(e)
    "cf_verification_report_url",        # Â§2(e)
    # Â§5 BoL performance â public
    "min_voltage_v",                        # Â§5(b)
    "max_voltage_v",                        # Â§5(b)
    "power_capability_w",                   # Â§5(c)
    "expected_lifetime_cycles",             # Â§5(d)
    "expected_lifetime_years",              # Â§5(e)
    "initial_round_trip_efficiency_pct",    # Â§5(g)
    "round_trip_efficiency_50pct_cycle_pct",# Â§5(h)
    "initial_internal_resistance_mohm",     # Â§5(i)
    "temp_storage_min_c",                   # Â§5(q)
    "temp_storage_max_c",                   # Â§5(q)
    "thermal_management_type",              # Â§5(r)
    # Â§6 End-of-life â public
    "dismantling_instructions_url",  # Â§6(a)
    "safety_handling_url",           # Â§6(b)
    "hazard_class",                  # Â§6(b)
    "takeback_scheme_url",           # Â§6(c)
    "second_life_potential",         # Â§6(e)
    "second_life_assessment_url",    # Â§6(e)
    "spare_parts_available_until",   # Â§6(f)
    "spare_parts_url",               # Â§6(f)
    "eou_guidance_url",              # Â§6(g)
    "label_meanings_url",            # Â§6(h)
]

# Annex XIII Â§3+Â§4 â supply chain + materials (Stakeholder tier)
STATIC_STAKEHOLDER = [
    # Â§3 Due diligence
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
    # Â§4 Materials
    "bom_json",
    "cobalt_pct",
    "lithium_pct",
    "nickel_pct",
    "graphite_pct",
    "hazardous_substances_json",
    "art6_compliant",
    "restriction_declaration_url",
    # Â§5 additional
    "cycle_life_test_c_rate",
    "test_conditions_url",
    "thermal_management_spec_url",
    # Â§6 additional
    "eol_environmental_impact_url",
]

# Regulator-only static fields
STATIC_REGULATOR = [
    "recycled_cobalt_pct",    # Â§4(e) â deferred to 2028 but store-ready
    "recycled_lithium_pct",   # Â§4(f)
    "recycled_nickel_pct",    # Â§4(g)
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
DYNAMIC_STAKEHOLDER = ["temp_celsius", "current_a", "depth_of_discharge_pct"]  # Â§5(j,k,l) real-time
DYNAMIC_REGULATOR   = []  # raw_telemetry + hash chain returned separately


def _pick(d, keys):
    """Return a dict of keysâvalues, skipping None values."""
    return {k: v for k in keys if (v := d.get(k)) is not None}


# ââ API endpoints âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def get_public_info(request):
    """
    Tier 1 â Public (no auth).
    Annex XIII Â§1, Â§2, Â§5(b-j), Â§6 static fields + SoC/SoH/cycles.
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
        # Â§1 identity
        **_pick(static, STATIC_PUBLIC),
        # Â§5 derived dynamic
        "remaining_capacity_ah": remaining_capacity_ah,   # Â§5(m)
        # dynamic telemetry
        **_pick(latest, DYNAMIC_PUBLIC),
    })


async def get_stakeholder_info(request):
    """
    Tier 2 â Authorised Stakeholder (Bearer VCU_TOKEN).
    Adds Â§3 supply chain, Â§4 materials, real-time temp/current/DoD.
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
        # Â§5 derived
        "remaining_capacity_ah": remaining_capacity_ah,  # Â§5(m)
        "remaining_power_w": remaining_power_w,          # Â§5(n)
        # dynamic telemetry
        **_pick(latest, DYNAMIC_PUBLIC),
        **_pick(latest, DYNAMIC_STAKEHOLDER),
    })


async def get_full_passport(request):
    """
    Tier 3 â Regulator (x-api-key: TIER3_KEY).
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
        # Â§5 derived
        "remaining_capacity_ah": remaining_capacity_ah,  # Â§5(m)
        "remaining_power_w": remaining_power_w,          # Â§5(n)
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


# ââ Routes ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
