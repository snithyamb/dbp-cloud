import os
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
VCU_TOKEN = os.environ.get("VCU_TOKEN", "")
TIER3_KEY = os.environ.get("TIER3_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def supabase_select(table, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    with httpx.Client() as client:
        r = client.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json()


def supabase_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    with httpx.Client() as client:
        r = client.post(url, headers={**HEADERS, "Prefer": "return=representation"}, json=data, timeout=10)
        r.raise_for_status()
        return r.json()


async def get_public_info(request):
    bin_id = request.path_params["bin_id"]

    telemetry = supabase_select("dbp_records", {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp,soc_pct,soh_pct,cycle_count,lifecycle_status",
    })
    if not telemetry:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = telemetry[0]

    passport = supabase_select("battery_passport", {
        "bin": f"eq.{bin_id}",
        "select": "chemistry,manufacturer,capacity_kwh,voltage_v,manufacture_date",
    })
    static = passport[0] if passport else {}

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "state_of_charge": latest["soc_pct"],
        "state_of_health": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "chemistry": static.get("chemistry"),
        "manufacturer": static.get("manufacturer"),
        "capacity_kwh": static.get("capacity_kwh"),
        "voltage_v": static.get("voltage_v"),
        "manufacture_date": static.get("manufacture_date"),
        "access_tier": "PUBLIC",
    })


async def get_stakeholder_info(request):
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

    passport = supabase_select("battery_passport", {
        "bin": f"eq.{bin_id}",
    })
    static = passport[0] if passport else {}

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "state_of_charge": latest["soc_pct"],
        "state_of_health": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "chemistry": static.get("chemistry"),
        "manufacturer": static.get("manufacturer"),
        "capacity_kwh": static.get("capacity_kwh"),
        "voltage_v": static.get("voltage_v"),
        "manufacture_date": static.get("manufacture_date"),
        "temp_celsius": static.get("temp_celsius"),
        "current_a": static.get("current_a"),
        "cell_count": static.get("cell_count"),
        "weight_kg": static.get("weight_kg"),
        "max_charge_rate_kw": static.get("max_charge_rate_kw"),
        "depth_of_discharge_pct": static.get("depth_of_discharge_pct"),
        "access_tier": "STAKEHOLDER",
    })


async def get_full_passport(request):
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

    passport = supabase_select("battery_passport", {
        "bin": f"eq.{bin_id}",
    })
    static = passport[0] if passport else {}

    return JSONResponse({
        "bin_id": latest["bin"],
        "recorded_at": latest["timestamp"],
        "state_of_charge": latest["soc_pct"],
        "state_of_health": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "chemistry": static.get("chemistry"),
        "manufacturer": static.get("manufacturer"),
        "capacity_kwh": static.get("capacity_kwh"),
        "voltage_v": static.get("voltage_v"),
        "manufacture_date": static.get("manufacture_date"),
        "temp_celsius": static.get("temp_celsius"),
        "current_a": static.get("current_a"),
        "cell_count": static.get("cell_count"),
        "weight_kg": static.get("weight_kg"),
        "max_charge_rate_kw": static.get("max_charge_rate_kw"),
        "depth_of_discharge_pct": static.get("depth_of_discharge_pct"),
        "raw_telemetry": telemetry,
        "access_tier": "REGULATOR",
    })


async def post_dbp_record(request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {VCU_TOKEN}":
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    body = await request.json()
    result = supabase_insert("dbp_records", body)
    return JSONResponse({"status": "ok", "data": result}, status_code=201)


async def list_batteries(request):
    data = supabase_select("dbp_records", {
        "select": "bin",
        "order": "timestamp.desc",
    })
    bins = list(dict.fromkeys(r["bin"] for r in data))
    return JSONResponse({"batteries": bins})


async def health(request):
    return JSONResponse({"status": "ok"})


routes = [
    Route("/", health),
    Route("/health", health),
    Route("/api/v1/dbp/{bin_id}/public", get_public_info),
    Route("/api/v1/dbp/{bin_id}/stakeholder", get_stakeholder_info),
    Route("/api/v1/dbp/{bin_id}/full", get_full_passport),
    Route("/api/v1/dbp/record", post_dbp_record, methods=["POST"]),
    Route("/api/v1/batteries", list_batteries),
]

app = Starlette(routes=routes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
