# Digital Battery Passport (DBP) – open reference implementation

M.Tech dissertation, BITS Pilani WILP (AELZG628T) – *Develop a VCU based Digital Battery Passport for tracking battery usage in accordance with EU ESPR 2027* – Nithyanandham S (2024HT65556).

An open, low-cost implementation of a battery passport for Regulation (EU) 2023/1542: vehicle-side data collection, a tamper-evident cloud store with three access tiers, and tools that let an auditor verify the data without trusting the cloud operator or the battery maker.

| | |
|---|---|
| Portal (GitHub Pages) | https://snithyamb.github.io/dbp-cloud/ |
| API (Render) | https://dbp-cloud.onrender.com/health |
| Database | Supabase PostgreSQL (append-only `dbp_records`) |

> The free Render instance sleeps when idle; the first request can take about 50 s.

## What is in this repository

| Path | Content |
|---|---|
| `main.py` | DBP API (Python / Starlette): record ingest and validation, canonical JSON, SHA-256 hash chain, Tier 1/2/3 endpoints, paged `/chain` for full verification, QR codes |
| `docs/index.html` | Portal: public / OEM / regulator views, live update, full hash-chain verification from GENESIS in the browser |
| `supabase_migration_v3_vcu_ingest.sql` | Database schema and append-only triggers (`dbp_records_no_update`, `dbp_records_no_truncate`) |
| `vcu_simulator.py` | Python VCU simulator: pushes a degrading battery history to the API (standard library only) |
| `esp8266/dbp_esp8266.ino` | ESP8266 firmware: battery model + BMS + VCU + cloud gateway in one MCU, with the trust features below |
| `esp8266/secrets.h.example` | Template for Wi-Fi name / password and VCU token – copy to `secrets.h` (never committed) |
| `tools/verify_device_chain.py` | Independent auditor tool: checks device signatures, device chain, gaps, signed-vs-stored values and SoH algorithm |

## How the data is protected

1. **Server hash chain.** Every accepted record is serialised to canonical JSON (sorted keys, JavaScript number format) including `prev_hash`; `record_hash = SHA-256(canonical)`. The first record of each battery points to `GENESIS`. The database rejects UPDATE, DELETE and TRUNCATE.
2. **Device signatures.** The ESP8266 creates its own ECDSA P-256 key on first boot, publishes the public key once (`DEVICE_KEY` event) and signs every record together with a sequence number and the previous device hash – so records can wait offline and still be verified later.
3. **SoH algorithm fingerprint.** Each SoH value carries the SHA-256 of the algorithm name, version and calibration that produced it; any change is logged as a `SOH_ALGORITHM` event.
4. **Independent SoH cross-check.** The VCU measures capacity over every full cycle and raises `SOH_CROSSCHECK_FAIL` if the BMS SoH differs by more than 3 %.

## Quick start

**API**: deploy `main.py` on Render (or any ASGI host) with the environment variables `SUPABASE_URL`, `SUPABASE_KEY`, `VCU_TOKEN`, `TIER3_KEY`, `SESSION_SECRET`, `OEM_PASSWORD`, `REGULATOR_PASSWORD`; run the SQL migration in Supabase.

**ESP8266** (Arduino IDE 2.x, esp8266 core 3.1.2, NodeMCU 1.0, 160 MHz):

1. Open `esp8266/dbp_esp8266.ino`.
2. Copy `secrets.h.example` to `secrets.h` in the same folder and fill in your values.
3. Upload, then open the Serial Monitor at 115200 baud. Commands: `s` status, `k` device public key, `h` toggle a +5 % SoH manipulation (demonstration).

The firmware writes only to the test battery `DBP-2024HT65556-003`; it refuses to compile for the real battery `-001`.

**Auditor tool**:

```bash
pip install cryptography
python tools/verify_device_chain.py --bin DBP-2024HT65556-003 --api-key <TIER3_KEY>
python tools/verify_device_chain.py --file full.json
```

## Limitations

The ESP8266 battery is simulated (13S, 48 V, 60 Ah NMC model at 60x speed). Tier 2/3 use prototype accounts with HMAC-signed sessions instead of OAuth 2.0 / X.509, and the device key is kept in ordinary flash. See the dissertation report for details and future work.
