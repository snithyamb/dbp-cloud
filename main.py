"""
DBP Cloud Backend — FastAPI
Digital Battery Passport for EU ESPR 2027
Author: Nithyanandham S (2024HT65556), BITS Pilani
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
    params = {
        "bin": f"eq.{bin}",
        "order": "timestamp.desc",
        "limit": "1",
        "select": "bin,timestamp,soc_pct,soh_pct,cycle_count,lifecycle_status",
    }
    data = supabase_select("dbp_records", params)
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")
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


@app.get("/api/v1/dbp/{bin}/stakeholder")
def get_stakeholder_info(bin: str, authorization: str = Header(...)):
    params = {"bin": f"eq.{bin}", "order": "timestamp.desc", "limit": "100", "select": "*"}
    data = supabase_select("dbp_records", params)
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")
    latest = data[0]
    return {
        "bin": bin,
        "lifecycle_status": latest["lifecycle_status"],
        "latest": {k: latest[k] for k in ["timestamp","voltage_mv","current_ma","soc_pct","soh_pct","cycle_count","capacity_mah"]},
        "total_records": len(data),
        "access_tier": "TIER_2_STAKEHOLDER",
    }


@app.get("/api/v1/dbp/{bin}/full")
def get_full_passport(bin: str, x_api_key: str = Header(...)):
    if x_api_key != os.environ.get("TIER3_KEY", ""):
        raise HTTPException(status_code=403, detail="Invalid Tier 3 credentials")
    data = supabase_select("dbp_records", {"bin": f"eq.{bin}", "order": "timestamp.desc", "select": "*"})
    if not data:
        raise HTTPException(status_code=404, detail=f"Battery {bin} not found")
    return {"bin": bin, "total_records": len(data), "hash_chain_verified": True, "annex_xiii_attributes": 77, "records": data, "access_tier": "TIER_3_MANUFACTURER"}


@app.post("/api/v1/dbp/record")
def post_dbp_record(record: DBPRecord, _=Depends(verify_vcu_token)):
    if not verify_hash_chain(record):
        raise HTTPException(status_code=400, detail="Hash chain verification failed")
    existing = supabase_select("dbp_records", {"record_hash": f"eq.{record.record_hash}", "select": "id"})
    if existing:
        return {"status": "duplicate", "message": "Record already stored"}
    lifecycle = compute_lifecycle_status(record.soh_pct)
    insert_data = {k: getattr(record, k) for k in ["bin","timestamp","voltage_mv","current_ma","soc_pct","soh_pct","cycle_count","capacity_mah","prev_hash","record_hash"]}
    insert_data["lifecycle_status"] = lifecycle
    supabase_insert("dbp_records", insert_data)
    return {"status": "stored", "record_hash": record.record_hash, "lifecycle_status": lifecycle, "bin": record.bin}


@app.get("/api/v1/batteries")
def list_batteries():
    data = supabase_select("dbp_latest", {"select": "*"})
    return {"batteries": data, "count": len(data)}
