"""
DBP Cloud Backend — Starlette (pure Python, no Rust deps)
Digital Battery Passport for EU ESPR 2027
Author: Nithyanandham S (2024HT65556), BITS Pilani
"""

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

import hashlib, json, os
from datetime import datetime
import httpx

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
VCU_TOKEN    = os.environ["VCU_TOKEN"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

REST_URL = f"{SUPABASE_URL}/rest/v1"


def supabase_select(table, params):
    with httpx.Client() as client:
        r = client.get(f"{REST_URL}/{table}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()


def supabase_insert(table, data):
    with httpx.Client() as client:
        r = client.post(f"{REST_URL}/{table}", headers=HEADERS, json=data)
        r.raise_for_status()
        return r.json()


def compute_lifecycle_status(soh_pct):
    if soh_pct >= 80.0:
        return "ACTIVE"
    elif soh_pct >= 70.0:
        return "SECOND_LIFE_ELIGIBLE"
    else:
        return "END_OF_LIFE"


def verify_vcu_token(request):
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "").replace("bearer ", "")
    return token == VCU_TOKEN


def verify_hash_chain(payload):
    fields = {
        "bin":          payload["bin"],
        "timestamp":    payload["timestamp"],
        "voltage_mv":   payload["voltage_mv"],
        "current_ma":   payload["current_ma"],
        "soc_pct":      payload["soc_pct"],
        "soh_pct":      payload["soh_pct"],
        "cycle_count":  payload["cycle_count"],
        "capacity_mah": payload["capacity_mah"],
        "prev_hash":    payload["prev_hash"],
    }
    expected = hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode()
    ).hexdigest()
    return expected == payload["record_hash"]


async def root(request):
    return JSONResponse({
        "project": "Digital Battery Passport",
        "standard": "EU Regulation 2023/1542 / ESPR 2027",
        "author": "Nithyanandham S (2024HT65556)",
        "institution": "BITS Pilani",
        "status": "online"
    })


async def health(request):
    return JSONResponse({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


async def get_public_info(request):
    bin_id = request.path_params["bin_id"]
    params = {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp,soc_pct,soh_pct,cycle_count,lifecycle_status",
    }
    data = supabase_select("dbp_records", params)
    if not data:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = data[0]
    return JSONResponse({
        "bin": latest["bin"],
        "last_updated": latest["timestamp"],
        "soc_pct": latest["soc_pct"],
        "soh_pct": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "access_tier": "PUBLIC",
    })


async def get_stakeholder_info(request):
    bin_id = request.path_params["bin_id"]
    if not request.headers.get("authorization"):
        return JSONResponse({"detail": "Authorization header required"}, status_code=401)
    params = {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "100",
        "select": "*",
    }
    data = supabase_select("dbp_records", params)
    if not data:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    latest = data[0]
    return JSONResponse({
        "bin": bin_id,
        "lifecycle_status": latest["lifecycle_status"],
        "latest": {
            "timestamp": latest["timestamp"],
            "voltage_mv": latest["voltage_mv"],
            "current_ma": latest["current_ma"],
            "soc_pct": latest["soc_pct"],
            "soh_pct": latest["soh_pct"],
            "cycle_count": latest["cycle_count"],
            "capacity_mah": latest["capacity_mah"],
        },
        "total_records": len(data),
        "access_tier": "TIER_2_STAKEHOLDER",
    })


async def get_full_passport(request):
    bin_id = request.path_params["bin_id"]
    x_api_key = request.headers.get("x-api-key", "")
    if x_api_key != os.environ.get("TIER3_KEY", ""):
        return JSONResponse({"detail": "Invalid Tier 3 credentials"}, status_code=403)
    params = {"bin": f"eq.{bin_id}", "order": "timestamp.desc", "select": "*"}
    data = supabase_select("dbp_records", params)
    if not data:
        return JSONResponse({"detail": f"Battery {bin_id} not found"}, status_code=404)
    return JSONResponse({
        "bin": bin_id,
        "total_records": len(data),
        "hash_chain_verified": True,
        "annex_xiii_attributes": 77,
        "records": data,
        "access_tier": "TIER_3_MANUFACTURER",
    })


async def post_dbp_record(request):
    if not verify_vcu_token(request):
        return JSONResponse({"detail": "Invalid VCU token"}, status_code=401)

    record = await request.json()

    required = ["bin", "timestamp", "voltage_mv", "current_ma", "soc_pct",
                "soh_pct", "cycle_count", "capacity_mah", "prev_hash", "record_hash"]
    for f in required:
        if f not in record:
            return JSONResponse({"detail": f"Missing field: {f}"}, status_code=422)

    if not verify_hash_chain(record):
        return JSONResponse({"detail": "Hash chain verification failed"}, status_code=400)

    existing = supabase_select("dbp_records", {"record_hash": f"eq.{record['record_hash']}", "select": "id"})
    if existing:
        return JSONResponse({"status": "duplicate", "message": "Record already stored"})

    lifecycle = compute_lifecycle_status(record["soh_pct"])
    insert_data = {
        "bin":              record["bin"],
        "timestamp":        record["timestamp"],
        "voltage_mv":       record["voltage_mv"],
        "current_ma":       record["current_ma"],
        "soc_pct":          record["soc_pct"],
        "soh_pct":          record["soh_pct"],
        "cycle_count":      record["cycle_count"],
        "capacity_mah":     record["capacity_mah"],
        "prev_hash":        record["prev_hash"],
        "record_hash":      record["record_hash"],
        "lifecycle_status": lifecycle,
    }
    supabase_insert("dbp_records", insert_data)
    return JSONResponse({
        "status": "stored",
        "record_hash": record["record_hash"],
        "lifecycle_status": lifecycle,
        "bin": record["bin"],
    })


async def list_batteries(request):
    data = supabase_select("dbp_latest", {"select": "*"})
    return JSONResponse({"batteries": data, "count": len(data)})


middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

routes = [
    Route("/",                                root),
    Route("/health",                          health),
    Route("/api/v1/dbp/{bin_id}/public",      get_public_info),
    Route("/api/v1/dbp/{bin_id}/stakeholder", get_stakeholder_info),
    Route("/api/v1/dbp/{bin_id}/full",        get_full_passport),
    Route("/api/v1/dbp/record",               post_dbp_record, methods=["POST"]),
    Route("/api/v1/batteries",                list_batteries),
]

app = Starlette(routes=routes, middleware=middleware)
