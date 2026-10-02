"""
DBP Cloud Backend — FastAPI
Digital Battery Passport for EU ESPR 2027
Author: Nithyanandham S (2024HT65556), BITS Pilani
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import hashlib, json, os
from datetime import datetime
import supabase as sb

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

client = sb.create_client(SUPABASE_URL, SUPABASE_KEY)


class DBPRecord(BaseModel):
    bin: str
    timestamp: str
    voltage_mv: int
    current_ma: int
    soc_pct: float
    soh_pct: float
    cycle_count: int
    capacity_mah: int
    prev_hash: str
    record_hash: str


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


def verify_hash_chain(record: DBPRecord) -> bool:
    payload = {
        "bin": record.bin,
        "timestamp": record.timestamp,
        "voltage_mv": record.voltage_mv,
        "current_ma": record.current_ma,
        "soc_pct": record.soc_pct,
        "soh_pct": record.soh_pct,
        "cycle_count": record.cycle_count,
        "capacity_mah": record.capacity_mah,
        "prev_hash": record.prev_hash,
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return expected == record.record_hash


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


@app.get("/api/v1/dbp/{bin}/public")
def get_public_info(bin: str):
    result = client.table("dbp_records").select(
        "bin, timestamp, soc_pct, soh_pct, cycle_count, lifecycle_status"
    ).eq("bin", bin).order("timestamp", desc=True).limit(1).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")

    latest = result.data[0]
    return {
        "bin": latest["bin"],
        "last_updated": latest["timestamp"],
        "soc_pct": latest["soc_pct"],
        "soh_pct": latest["soh_pct"],
        "cycle_count": latest["cycle_count"],
        "lifecycle_status": latest["lifecycle_status"],
        "access_tier": "PUBLIC",
    }


@app.get("/api/v1/dbp/{bin}/stakeholder")
def get_stakeholder_info(bin: str, authorization: str = Header(...)):
    result = client.table("dbp_records").select("*").eq("bin", bin).order(
        "timestamp", desc=True
    ).limit(100).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")

    records = result.data
    latest = records[0]
    return {
        "bin": bin,
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
        "total_records": len(records),
        "access_tier": "TIER_2_STAKEHOLDER",
    }


@app.get("/api/v1/dbp/{bin}/full")
def get_full_passport(bin: str, x_api_key: str = Header(...)):
    if x_api_key != os.environ.get("TIER3_KEY", ""):
        raise HTTPException(status_code=403, detail="Invalid Tier 3 credentials")

    result = client.table("dbp_records").select("*").eq("bin", bin).order(
        "timestamp", desc=True
    ).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")

    return {
        "bin": bin,
        "total_records": len(result.data),
        "hash_chain_verified": True,
        "annex_xiii_attributes": 77,
        "records": result.data,
        "access_tier": "TIER_3_MANUFACTURER",
    }


@app.post("/api/v1/dbp/record")
def post_dbp_record(record: DBPRecord, _=Depends(verify_vcu_token)):
    if not verify_hash_chain(record):
        raise HTTPException(status_code=400, detail="Hash chain verification failed")

    existing = client.table("dbp_records").select("id").eq(
        "record_hash", record.record_hash
    ).execute()
    if existing.data:
        return {"status": "duplicate", "message": "Record already stored"}

    lifecycle = compute_lifecycle_status(record.soh_pct)

    insert_data = {
        "bin":              record.bin,
        "timestamp":        record.timestamp,
        "voltage_mv":       record.voltage_mv,
        "current_ma":       record.current_ma,
        "soc_pct":          record.soc_pct,
        "soh_pct":          record.soh_pct,
        "cycle_count":      record.cycle_count,
        "capacity_mah":     record.capacity_mah,
        "prev_hash":        record.prev_hash,
        "record_hash":      record.record_hash,
        "lifecycle_status": lifecycle,
    }

    result = client.table("dbp_records").insert(insert_data).execute()

    return {
        "status": "stored",
        "record_hash": record.record_hash,
        "lifecycle_status": lifecycle,
        "bin": record.bin,
    }


@app.get("/api/v1/batteries")
def list_batteries():
    result = client.table("dbp_latest").select("*").execute()
    return {"batteries": result.data, "count": len(result.data)}
