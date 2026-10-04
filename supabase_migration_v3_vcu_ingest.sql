-- ============================================================
-- DBP migration v3 — VCU ingest, full-record hashing, append-only
-- Safe to run more than once. Run in Supabase SQL Editor.
-- ============================================================

-- 1. Dynamic data the VCU may send (Annex XIII §4-5, Annex IV, Annex VII)
ALTER TABLE dbp_records
  ADD COLUMN IF NOT EXISTS soce_pct                         NUMERIC,
  ADD COLUMN IF NOT EXISTS remaining_capacity_ah            NUMERIC,
  ADD COLUMN IF NOT EXISTS capacity_fade_pct                NUMERIC,
  ADD COLUMN IF NOT EXISTS remaining_power_w                NUMERIC,
  ADD COLUMN IF NOT EXISTS power_fade_pct                   NUMERIC,
  ADD COLUMN IF NOT EXISTS internal_resistance_mohm         NUMERIC,
  ADD COLUMN IF NOT EXISTS internal_resistance_increase_pct NUMERIC,
  ADD COLUMN IF NOT EXISTS round_trip_efficiency_pct        NUMERIC,
  ADD COLUMN IF NOT EXISTS rte_fade_pct                     NUMERIC,
  ADD COLUMN IF NOT EXISTS self_discharge_pct_per_month     NUMERIC,
  ADD COLUMN IF NOT EXISTS remaining_life_cycles            NUMERIC,
  ADD COLUMN IF NOT EXISTS remaining_life_years             NUMERIC,
  ADD COLUMN IF NOT EXISTS pack_voltage_v                   NUMERIC,
  ADD COLUMN IF NOT EXISTS current_a                        NUMERIC,
  ADD COLUMN IF NOT EXISTS temp_celsius                     NUMERIC,
  ADD COLUMN IF NOT EXISTS depth_of_discharge_pct           NUMERIC,
  ADD COLUMN IF NOT EXISTS energy_throughput_kwh            NUMERIC,
  ADD COLUMN IF NOT EXISTS capacity_throughput_ah           NUMERIC,
  ADD COLUMN IF NOT EXISTS time_extreme_temp_h              NUMERIC,
  ADD COLUMN IF NOT EXISTS time_charging_extreme_temp_h     NUMERIC,
  ADD COLUMN IF NOT EXISTS deep_discharge_events            INTEGER,
  ADD COLUMN IF NOT EXISTS overcharge_events                INTEGER,
  ADD COLUMN IF NOT EXISTS accident_events                  INTEGER,
  ADD COLUMN IF NOT EXISTS record_type                      TEXT,
  ADD COLUMN IF NOT EXISTS event_type                       TEXT,
  ADD COLUMN IF NOT EXISTS event_detail                     TEXT,
  ADD COLUMN IF NOT EXISTS raw_telemetry                    JSONB,
  ADD COLUMN IF NOT EXISTS data_provider                    TEXT,     -- writer identity (SyRS-I03)
  ADD COLUMN IF NOT EXISTS received_at                      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS canonical                        TEXT,     -- exact hashed payload
  ADD COLUMN IF NOT EXISTS record_hash                      TEXT;     -- SHA-256(canonical)

-- 2. One successor per record: a second record claiming the same predecessor is
--    rejected, so the chain can never fork.
CREATE UNIQUE INDEX IF NOT EXISTS dbp_records_chain_link ON dbp_records (bin, prev_hash);

-- 3. Append-only: no role (including service_role / admin) may change or delete
--    a written record. Applies to every client because it is a table trigger.
CREATE OR REPLACE FUNCTION dbp_records_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'dbp_records is append-only (EU 2023/1542 Art. 77(4)); % is not allowed', TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS dbp_records_no_update ON dbp_records;
CREATE TRIGGER dbp_records_no_update BEFORE UPDATE OR DELETE ON dbp_records
  FOR EACH ROW EXECUTE FUNCTION dbp_records_append_only();

DROP TRIGGER IF EXISTS dbp_records_no_truncate ON dbp_records;
CREATE TRIGGER dbp_records_no_truncate BEFORE TRUNCATE ON dbp_records
  FOR EACH STATEMENT EXECUTE FUNCTION dbp_records_append_only();

-- 4. Static passport fields added in this version
ALTER TABLE battery_passport
  ADD COLUMN IF NOT EXISTS model                             TEXT,     -- Annex XIII §1(f)
  ADD COLUMN IF NOT EXISTS capacity_exhaustion_threshold_pct NUMERIC,  -- Annex XIII 1(k)
  ADD COLUMN IF NOT EXISTS temp_operating_min_c              NUMERIC,
  ADD COLUMN IF NOT EXISTS temp_operating_max_c              NUMERIC,
  ADD COLUMN IF NOT EXISTS weight_tolerance_kg               NUMERIC,
  ADD COLUMN IF NOT EXISTS cobalt_country_of_origin          TEXT,
  ADD COLUMN IF NOT EXISTS lithium_country_of_origin         TEXT,
  ADD COLUMN IF NOT EXISTS nickel_country_of_origin          TEXT,
  ADD COLUMN IF NOT EXISTS graphite_country_of_origin        TEXT;

-- 5. Illustrative demo values for DBP-2024HT65556-001 (fictitious manufacturer)
UPDATE battery_passport SET
  model = 'HyPack EV-48',
  capacity_exhaustion_threshold_pct = 80,
  temp_operating_min_c = -20,
  temp_operating_max_c = 55,
  weight_tolerance_kg = 2.5,
  cobalt_country_of_origin = 'CD',
  lithium_country_of_origin = 'AU',
  nickel_country_of_origin = 'ID',
  graphite_country_of_origin = 'CN'
WHERE bin = 'DBP-2024HT65556-001';

-- 6. Check: should list the trigger and the new columns
SELECT tgname FROM pg_trigger WHERE tgrelid = 'dbp_records'::regclass AND NOT tgisinternal;

-- 7. Event records carry no SoC/SoH: drop leftover NOT NULL rules (all columns except id, bin)
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT attname FROM pg_attribute
           WHERE attrelid = 'dbp_records'::regclass AND attnum > 0 AND NOT attisdropped
             AND attnotnull AND attname NOT IN ('id', 'bin')
  LOOP
    EXECUTE format('ALTER TABLE dbp_records ALTER COLUMN %I DROP NOT NULL', c.attname);
  END LOOP;
END $$;
