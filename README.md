# Digital Battery Passport (DBP) – open reference implementation

M.Tech dissertation, BITS Pilani WILP (AELZG628T) – *Develop a VCU Based Digital Battery Passport for Tracking Battery Usage in Accordance with EU ESPR 2027* – Nithyanandham S (2024HT65556).

An open, low-cost implementation of a battery passport for Regulation (EU) 2023/1542: vehicle-side data collection, a tamper-evident cloud store with three access tiers, and tools that let an auditor verify the data without trusting the cloud operator or the battery maker.

| | |
|---|---|
| Final report | [`report/2024ht65556.pdf`](report/2024ht65556.pdf) |
| Portal (GitHub Pages) | https://snithyamb.github.io/dbp-cloud/ |
| API (Render) | https://dbp-cloud.onrender.com/health |
| Database | Supabase PostgreSQL (append-only `dbp_records`) |

> The free Render instance sleeps when idle; the first request can take about 50 s.

## Repository structure

```
dbp-cloud/
├── report/
│   └── 2024ht65556.pdf            Final dissertation report
├── main.py                        Cloud API (Render deploys from the repository root)
├── requirements.txt, runtime.txt, render.yaml
├── docs/
│   └── index.html                 Passport portal (GitHub Pages), Tier 1/2/3 views, full chain check
├── database/
│   └── supabase_migration_v3_vcu_ingest.sql   Schema and append-only triggers
├── firmware/
│   └── dbp_esp8266/               ESP8266: battery model + BMS + VCU + gateway in one MCU
│       ├── dbp_esp8266.ino
│       └── secrets.h.example      Copy to secrets.h (never committed)
├── vcu/
│   ├── raspberry-pi/              Path A: Raspberry Pi VCU reading BMS CAN frames
│   │   ├── dbp_vcu.py, fake_bms.py, bms_example.dbc
│   │   └── vcu_config.example.json, dbp-vcu.service, dbp-vcu.env.example, 80-can.network
│   └── simulator/
│       └── vcu_simulator.py       VCU simulator: pushes a degrading battery history to the API
├── simulink/
│   ├── New_BMS_2.slx              Simulink BMS model (SoC, SoH, cycle count)
│   ├── New_VCU.slx                Simulink VCU model
│   └── DBP_RunAll.m               Script that sets up and runs the models
├── tools/
│   └── verify_device_chain.py     Independent auditor tool
└── design/                        Architecture, block diagram, SysML/UML and stakeholder diagrams (draw.io)
```

## How the data is protected

1. **Server hash chain.** Every accepted record is serialised to canonical JSON (sorted keys, JavaScript number format) including `prev_hash`; `record_hash = SHA-256(canonical)`. The first record of each battery points to `GENESIS`. The database rejects UPDATE, DELETE and TRUNCATE.
2. **Full-chain check in the portal.** The browser re-hashes every record from GENESIS and names any problem: hash mismatch, broken link, or a stored value that differs from the hashed payload.
3. **Device signatures.** The ESP8266 creates its own ECDSA P-256 key on first boot, publishes the public key once (`DEVICE_KEY` event) and signs every record together with a sequence number and the previous device hash – so records can wait offline and still be verified later.
4. **SoH algorithm fingerprint.** Each SoH value carries the SHA-256 of the algorithm name, version and calibration that produced it; any change is logged as a `SOH_ALGORITHM` event.
5. **Independent SoH cross-check.** The VCU measures capacity over every full cycle and raises `SOH_CROSSCHECK_FAIL` if the BMS SoH differs by more than 3 %.

## Quick start

**API**: deploy `main.py` on Render (or any ASGI host) with the environment variables `SUPABASE_URL`, `SUPABASE_KEY`, `VCU_TOKEN`, `TIER3_KEY`, `SESSION_SECRET`, `OEM_PASSWORD`, `REGULATOR_PASSWORD`; run `database/supabase_migration_v3_vcu_ingest.sql` in Supabase.

**ESP8266** (Arduino IDE 2.x, esp8266 core 3.1.2, NodeMCU 1.0, 160 MHz):

1. Open `firmware/dbp_esp8266/dbp_esp8266.ino`.
2. Copy `secrets.h.example` to `secrets.h` in the same folder and fill in your values.
3. Upload, then open the Serial Monitor at 115200 baud. Commands: `s` status, `k` device public key, `h` toggle a +5 % SoH manipulation (demonstration).

The firmware writes only to the test battery `DBP-2024HT65556-003`; it refuses to compile for the real battery `-001`.

**Raspberry Pi VCU**: see the header of `vcu/raspberry-pi/dbp_vcu.py`; `fake_bms.py` generates BMS CAN frames for a bench test without hardware.

**VCU simulator**: `python vcu/simulator/vcu_simulator.py --dry-run --ticks 5`

**Auditor tool**:

```bash
pip install cryptography
python tools/verify_device_chain.py --bin DBP-2024HT65556-003 --api-key <TIER3_KEY>
python tools/verify_device_chain.py --file full.json
```

## Limitations

The ESP8266 battery is simulated (13S, 48 V, 60 Ah NMC model at 60x speed). Tier 2/3 use prototype accounts with HMAC-signed sessions instead of OAuth 2.0 / X.509, and the device key is kept in ordinary flash. See the report for details and future work.
