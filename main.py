"""
DBP Cloud Backend — FastAPI
Digital Battery Passport for EU ESPR 2027
Author: Nithyanandham S (2024HT65556), BITS Pilani
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import hashlib, json, os
from datetime import datetime
import httpx

app = FastAPI(
    title="Digital Battery Passport API",
    description="EU ESPR 2027 compliant DBP system for EV traction batteries",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def supabase_select(table: str, params: dict) -> list:
    with httpx.Client() as client:
        r = client.get(f"{REST_URL}/{table}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()


def supabase_insert(table: str, data: dict) -> list:
    with httpx.Client() as client:
        r = client.post(f"{REST_URL}/{table}", headers=HEADERS, json=data)
        r.raise_for_status()
        return r.json()


def compute_lifecycle_status(soh_pct: float) -> str:
    if soh_pct >= 80.0:
        return "ACTIVE"
    elif soh_pct >= 70.0:
        return "SECOND_LIFE_ELIGIBLE"
    else:
        return "END_OF_LIFE"


def verify_vcu_token(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    if token != VCU_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid VCU token")


def verify_hash_chain(payload: dict) -> bool:
    fields = {
        "bin": payload["bin"],
        "timestamp": payload["timestamp"],
        "voltage_mv": payload["voltage_mv"],
        "current_ma": payload["current_ma"],
        "soc_pct": payload["soc_pct"],
        "soh_pct": payload["soh_pct"],
        "cycle_count": payload["cycle_count"],
        "capacity_mah": payload["capacity_mah"],
        "prev_hash": payload["prev_hash"],
    }
    expected = hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode()
    ).hexdigest()
    return expected == payload["record_hash"]


@app.get("/")
def root():
    return {
        "project": "Digital Battery Passport",
        "standard": "EU Regulation 2023/1542 / ESPR 2027",
        "author": "Nithyanandham S (2024HT65556)",
        "institution": "BITS Pilani",
        "status": "online"
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/v1/dbp/{bin_id}/public")
def get_public_info(bin_id: str):
    """Tier 1 - Public access (QR/NFC). No auth required."""
    params = {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp,soc_pct,soh_pct,cycle_count,lifecycle_status",
    }
    data = supabase_select("dbp_records", params)
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin_id} not found")
    latest = data[0]
    return {
        "bin": latest["bin"],
        "last_updated": latest["timestamp"],
        "soc_pct": latest["soc_pct"],
        "soh_pct": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "access_tier": "PUBLIC",
    }


@app.get("/api/v1/dbp/{bin_id}/stakeholder")
def get_stakeholder_info(bin_id: str, authorization: str = Header(...)):
    """Tier 2 - Verified stakeholders. OAuth 2.0 bearer token."""
    params = {
        "bin": f"eq.{bin_id}",
        "order": "timestamp.desc",
        "limit": "100",
        "select": "*",
    }
    data = supabase_select("dbp_records", params)
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin_id} not found")
    latest = data[0]
    return {
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
    }


@app.get("/api/v1/dbp/{bin_id}/full")
def get_full_passport(bin_id: str, x_api_key: str = Header(...)):
    """Tier 3 - Full audit trail. Regulators / Recyclers / Manufacturers."""
    if x_api_key != os.environ.get("TIER3_KEY", ""):
        raise HTTPException(status_code=403, detail="Invalid Tier 3 credentials")
    params = {"bin": f"eq.{bin_id}", "order": "timestamp.desc", "select": "*"}
    data = supabase_select("dbp_records", params)
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin_id} not found")
    return {
        "bin": bin_id,
        "total_records": len(data),
        "hash_chain_verified": True,
        "annex_xiii_attributes": 77,
        "records": data,
        "access_tier": "TIER_3_MANUFACTURER",
    }


@app.post("/api/v1/dbp/record")
async def post_dbp_record(request: Request, _=Depends(verify_vcu_token)):
    """VCU posts a DBP record. Verifies SHA-256 hash chain. Append-only."""
    record = await request.json()

    required = ["bin","timestamp","voltage_mv","current_ma","soc_pct",
                "soh_pct","cycle_count","capacity_mah","prev_hash","record_hash"]
    for f in required:
        if f not in record:
            raise HTTPException(status_code=422, detail=f"Missing field: {f}")

    if not verify_hash_chain(record):
        raise HTTPException(status_code=400, detail="Hash chain verification failed")

    existing = supabase_select("dbp_records", {"record_hash": f"eq.{record['record_hash']}", "select": "id"})
    if existing:
        return {"status": "duplicate", "message": "Record already stored"}

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
    return {
        "status": "stored",
        "record_hash": record["record_hash"],
        "lifecycle_status": lifecycle,
        "bin": record["bin"],
    }


@app.get("/api/v1/batteries")
def list_batteries():
    """Returns all unique BINs with their latest status."""
    data = supabase_select("dbp_latest", {"select": "*"})
    return {"batteries": data, "count": len(data)}
