/*
  dbp_esp8266 — Digital Battery Passport: BMS + VCU + cloud gateway in ONE ESP8266
  ─────────────────────────────────────────────────────────────────────────────────
  No external BMS and no CAN bus. The ESP8266 runs all three controllers:

    ┌──────────────── ESP8266 (NodeMCU) ────────────────┐
    │ BATTERY MODEL  13S 48 V 60 Ah pack (OCV + R0, thermal, ageing)
    │ BMS            coulomb-counting SoC, SoH, cycle count, protections
    │ VCU            Annex IV/VII metrics, events, non-resettable counters
    │ GATEWAY        flash outbox → HTTPS + VCU token → Render API
    └───────────────────────────────────────────────────┘
               → Supabase (append-only, SHA-256 hash chain) → passport portal

  BEFORE UPLOADING: fill in the secrets.h tab (Wi-Fi name, Wi-Fi password, VCU token).
  Board: Tools → Board → esp8266 → NodeMCU 1.0 (ESP-12E Module). Serial Monitor: 115200 baud.

  Serial commands (type a letter + Enter):
    s  status report          o  inject overcharge (cell > 4.25 V)
    x  inject crash flag      t  inject over-temperature (60 °C for 2 sim-minutes)
    k  print the device public key
    h  toggle a MANIPULATED SoH algorithm (+5 %) to demonstrate that the
       fingerprint changes and the VCU cross-check catches it

  v2 — trust features (supervisor review, 24 Oct 2026):
    • SoH algorithm fingerprint: SHA-256 of the algorithm id, version and the calibration
      actually running, sent with every record; any change is declared as an event.
    • Independent SoH cross-check: the VCU measures capacity over each full cycle
      (Ah discharged ÷ DoD) and compares it with the BMS SoH; > 3 % deviation = event.
    • Device-side hash chain + ECDSA P-256 signature on every record, so records stored
      offline (no telematics) stay tamper-evident until they are synced.
*/
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecureBearSSL.h>
#include <LittleFS.h>
#include <time.h>
#include <sys/time.h>
#include <bearssl/bearssl.h>
#include <StackThunk.h>

// ═════════════════ 1. YOUR SETTINGS — in the secrets.h tab (Wi-Fi + VCU token) ═
// Kept in a separate file so firmware updates never overwrite them.
#include "secrets.h"

// ═════════════════ 2. Identity (records can never be deleted) ════════════════
#define DBP_BIN   "DBP-2024HT65556-003"        // TEST battery. The real one is -001.
#define VCU_ID    "ESP8266-BMS-VCU-01"
#define API_URL   "https://dbp-cloud.onrender.com/api/v1/dbp/record"

// ═════════════════ 3. Battery ratings (13S Li-ion, 48 V 60 Ah) ═══════════════
#define CELLS_SERIES             13
#define RATED_CAPACITY_AH        60.0f
#define NOMINAL_VOLTAGE_V        48.0f
#define RATED_ENERGY_KWH         2.88f
#define RATED_POWER_W            3000.0f
#define INITIAL_RESISTANCE_MOHM  50.0f
#define INITIAL_RTE_PCT          97.5f
#define EXPECTED_LIFETIME_CYCLES 1000.0f
#define EOL_SOH_PCT              80.0f

// ═════════════════ 4. Limits ════════════════════════════════════════════════
#define EXTREME_HOT_C          45.0f
#define EXTREME_COLD_C        -10.0f
#define DEEP_DISCHARGE_SOC_PCT  5.0f
#define OVERCHARGE_CELL_V       4.25f
#define REST_CURRENT_A          0.5f
#define REST_MIN_FOR_SELF_DISCHARGE 30.0f      // simulated minutes

// ═════════════════ 5. Simulation and upload ══════════════════════════════════
#define SIM_SPEED              60      // 1 real second = 60 simulated seconds (one cycle ≈ 7 real minutes)
#define AMBIENT_C              30.0f   // Chennai
#define TELEMETRY_INTERVAL_S   60      // real seconds between TELEMETRY records
#define OUTBOX_MAX_RECORDS     1200    // ≈ 20 h of telemetry kept while offline
#define SOH_MISMATCH_LIMIT_PCT 3.0f    // BMS SoH vs VCU-measured SoH

static_assert(__builtin_strcmp(DBP_BIN, "DBP-2024HT65556-001") != 0,
              "This build simulates the battery: do not write it into the real battery's passport (-001).");

// ═════════════════ trusted root certificates (Let's Encrypt + Google Trust) ═══
static const char TRUSTED_ROOTS[] PROGMEM = R"PEM(
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFfLn
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIFVzCCAz+gAwIBAgINAgPlk28xsBNJiGuiFzANBgkqhkiG9w0BAQwFADBHMQsw
CQYDVQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2VzIExMQzEU
MBIGA1UEAxMLR1RTIFJvb3QgUjEwHhcNMTYwNjIyMDAwMDAwWhcNMzYwNjIyMDAw
MDAwWjBHMQswCQYDVQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZp
Y2VzIExMQzEUMBIGA1UEAxMLR1RTIFJvb3QgUjEwggIiMA0GCSqGSIb3DQEBAQUA
A4ICDwAwggIKAoICAQC2EQKLHuOhd5s73L+UPreVp0A8of2C+X0yBoJx9vaMf/vo
27xqLpeXo4xL+Sv2sfnOhB2x+cWX3u+58qPpvBKJXqeqUqv4IyfLpLGcY9vXmX7w
Cl7raKb0xlpHDU0QM+NOsROjyBhsS+z8CZDfnWQpJSMHobTSPS5g4M/SCYe7zUjw
TcLCeoiKu7rPWRnWr4+wB7CeMfGCwcDfLqZtbBkOtdh+JhpFAz2weaSUKK0Pfybl
qAj+lug8aJRT7oM6iCsVlgmy4HqMLnXWnOunVmSPlk9orj2XwoSPwLxAwAtcvfaH
szVsrBhQf4TgTM2S0yDpM7xSma8ytSmzJSq0SPly4cpk9+aCEI3oncKKiPo4Zor8
Y/kB+Xj9e1x3+naH+uzfsQ55lVe0vSbv1gHR6xYKu44LtcXFilWr06zqkUspzBmk
MiVOKvFlRNACzqrOSbTqn3yDsEB750Orp2yjj32JgfpMpf/VjsPOS+C12LOORc92
wO1AK/1TD7Cn1TsNsYqiA94xrcx36m97PtbfkSIS5r762DL8EGMUUXLeXdYWk70p
aDPvOmbsB4om3xPXV2V4J95eSRQAogB/mqghtqmxlbCluQ0WEdrHbEg8QOB+DVrN
VjzRlwW5y0vtOUucxD/SVRNuJLDWcfr0wbrM7Rv1/oFB2ACYPTrIrnqYNxgFlQID
AQABo0IwQDAOBgNVHQ8BAf8EBAMCAYYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4E
FgQU5K8rJnEaK0gnhS9SZizv8IkTcT4wDQYJKoZIhvcNAQEMBQADggIBAJ+qQibb
C5u+/x6Wki4+omVKapi6Ist9wTrYggoGxval3sBOh2Z5ofmmWJyq+bXmYOfg6LEe
QkEzCzc9zolwFcq1JKjPa7XSQCGYzyI0zzvFIoTgxQ6KfF2I5DUkzps+GlQebtuy
h6f88/qBVRRiClmpIgUxPoLW7ttXNLwzldMXG+gnoot7TiYaelpkttGsN/H9oPM4
7HLwEXWdyzRSjeZ2axfG34arJ45JK3VmgRAhpuo+9K4l/3wV3s6MJT/KYnAK9y8J
ZgfIPxz88NtFMN9iiMG1D53Dn0reWVlHxYciNuaCp+0KueIHoI17eko8cdLiA6Ef
MgfdG+RCzgwARWGAtQsgWSl4vflVy2PFPEz0tv/bal8xa5meLMFrUKTX5hgUvYU/
Z6tGn6D/Qqc6f1zLXbBwHSs09dR2CQzreExZBfMzQsNhFRAbd03OIozUhfJFfbdT
6u9AWpQKXCBfTkBdYiJ23//OYb2MI3jSNwLgjt7RETeJ9r/tSQdirpLsQBqvFAnZ
0E6yove+7u7Y/9waLd64NnHi/Hm3lCXRSHNboTXns5lndcEZOitHTtNCjv0xyBZm
2tIMPNuzjsmhDYAPexZ3FL//2wmUspO8IFgV6dtxQ/PeEMMA3KgqlbbC1j+Qa3bb
bP6MvPJwNQzcmRk13NfIRmPVNnGuV/u3gm3c
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICCTCCAY6gAwIBAgINAgPlwGjvYxqccpBQUjAKBggqhkjOPQQDAzBHMQswCQYD
VQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2VzIExMQzEUMBIG
A1UEAxMLR1RTIFJvb3QgUjQwHhcNMTYwNjIyMDAwMDAwWhcNMzYwNjIyMDAwMDAw
WjBHMQswCQYDVQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2Vz
IExMQzEUMBIGA1UEAxMLR1RTIFJvb3QgUjQwdjAQBgcqhkjOPQIBBgUrgQQAIgNi
AATzdHOnaItgrkO4NcWBMHtLSZ37wWHO5t5GvWvVYRg1rkDdc/eJkTBa6zzuhXyi
QHY7qca4R9gq55KRanPpsXI5nymfopjTX15YhmUPoYRlBtHci8nHc8iMai/lxKvR
HYqjQjBAMA4GA1UdDwEB/wQEAwIBhjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQW
BBSATNbrdP9JNqPV2Py1PsVq8JQdjDAKBggqhkjOPQQDAwNpADBmAjEA6ED/g94D
9J+uHXqnLrmvT/aDHQ4thQEd0dlq7A/Cr8deVl5c1RxYIigL9zC2L7F8AjEA8GE8
p/SgguMh1YQdc4acLa/KNJvxn7kjNuK8YAOdgLOaVsjh4rsUecrNIdSUtUlD
-----END CERTIFICATE-----
)PEM";

// ═════════════════ persistent state (flash; survives power loss) ═════════════
struct Persist {
  uint32_t magic;
  char     bin[40];
  // battery model + BMS
  float    soc_pct, soh_pct, temp_c;
  uint8_t  phase; float phase_time_s; uint8_t deep_every;
  // VCU non-resettable counters
  uint32_t cycle_count, deep_events, over_events, accident_events, next_seq;
  double   energy_kwh, energy_chg_kwh, capacity_ah, t_extreme_h, t_chg_extreme_h, efc;
  float    resistance_mohm, rte_pct, dod_pct, soce_pct, self_dis_pct_month;      // NAN = not yet measured
};
static const uint32_t MAGIC = 0xDB9A1001;
Persist st;

enum Phase : uint8_t { P_DISCHARGE, P_REST_AFTER_DIS, P_CHARGE, P_REST_AFTER_CHG };
const char* PHASE_NAME[] = {"DISCHARGE", "REST", "CHARGE", "REST"};

void resetState() {
  memset(&st, 0, sizeof(st));
  st.magic = MAGIC; strncpy(st.bin, DBP_BIN, sizeof(st.bin) - 1);
  st.soc_pct = 90; st.soh_pct = 100; st.temp_c = AMBIENT_C; st.phase = P_DISCHARGE;
  st.resistance_mohm = st.rte_pct = st.dod_pct = st.soce_pct = st.self_dis_pct_month = NAN;
  st.next_seq = 1;
}

void loadState() {
  File f = LittleFS.open("/state.bin", "r");
  if (f && f.size() == sizeof(Persist)) {
    f.read((uint8_t*)&st, sizeof(st)); f.close();
    if (st.magic == MAGIC && strcmp(st.bin, DBP_BIN) == 0) {
      Serial.printf("[state] resumed: cycle %u, SoC %.1f%%, SoH %.2f%%, %.3f kWh\n",
                    st.cycle_count, st.soc_pct, st.soh_pct, st.energy_kwh);
      return;
    }
  } else if (f) f.close();
  Serial.println("[state] new battery state for " DBP_BIN);
  resetState();
}

void saveState() {                         // write-then-rename: a power cut never corrupts it
  File f = LittleFS.open("/state.tmp", "w");
  if (!f) return;
  f.write((const uint8_t*)&st, sizeof(st)); f.close();
  LittleFS.rename("/state.tmp", "/state.bin");
}

// ═════════════════ BATTERY MODEL ═════════════════════════════════════════════
// Cell open-circuit voltage for NMC chemistry, SoC 0…100 %
float cellOcv(float soc) {
  static const float S[] = {0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100};
  static const float V[] = {3.00f, 3.30f, 3.45f, 3.55f, 3.61f, 3.66f, 3.71f, 3.78f, 3.86f, 3.94f, 4.05f, 4.18f};
  soc = constrain(soc, 0.0f, 100.0f);
  for (int k = 1; k < 12; k++)
    if (soc <= S[k]) return V[k - 1] + (V[k] - V[k - 1]) * (soc - S[k - 1]) / (S[k] - S[k - 1]);
  return V[11];
}
float packResistanceOhm() { return INITIAL_RESISTANCE_MOHM / 1000.0f * (1.0f + 2.5f * (100.0f - st.soh_pct) / 100.0f); }

// live values produced by model + BMS every tick
float I_A = 0, V_pack = 0, cellMin = 0, cellMax = 0, tMax = 0, tMin = 0;
bool  faultOverV = false, faultUnderV = false, faultOverT = false, faultCrash = false;
float injectOverchargeS = 0, injectHotS = 0; bool injectCrash = false;

float driveCurrent(float t) {              // urban-style load profile, 0.3–0.6 C
  return 22.0f + 8.0f * sinf(t / 240.0f) + 5.0f * sinf(t / 37.0f) + (fmodf(t, 900) < 60 ? 10.0f : 0.0f);
}

void modelStep(float dt) {                 // dt in simulated seconds
  st.phase_time_s += dt;
  float soc = st.soc_pct;
  switch (st.phase) {
    case P_DISCHARGE: {
      float floorSoc = (st.deep_every % 5 == 4) ? 4.0f : 12.0f;   // every 5th cycle goes deep
      I_A = driveCurrent(st.phase_time_s);
      if (soc <= floorSoc) { st.phase = P_REST_AFTER_DIS; st.phase_time_s = 0; I_A = 0; }
      break; }
    case P_REST_AFTER_DIS:
      I_A = 0;
      if (st.phase_time_s >= 40 * 60) { st.phase = P_CHARGE; st.phase_time_s = 0; }
      break;
    case P_CHARGE:
      I_A = soc < 90 ? -15.0f : -15.0f * max(0.2f, (100 - soc) / 10.0f);  // CC then taper (CV-like)
      if (soc >= 99.5f) { st.phase = P_REST_AFTER_CHG; st.phase_time_s = 0; I_A = 0; st.deep_every++; }
      break;
    case P_REST_AFTER_CHG:
      I_A = 0;
      if (st.phase_time_s >= 60 * 60) { st.phase = P_DISCHARGE; st.phase_time_s = 0; }
      break;
  }
  float capAh = RATED_CAPACITY_AH * st.soh_pct / 100.0f;
  float dSoc = -I_A * dt / 3600.0f / capAh * 100.0f;               // coulomb counting
  if (I_A == 0) dSoc -= 2.0f / (30 * 24 * 3600.0f) * dt;           // 2 %/month self-discharge at rest
  st.soc_pct = constrain(soc + dSoc, 0.0f, 100.0f);
  // ageing: 0.02 % SoH per equivalent full cycle  (→ 80 % after 1000 cycles)
  float efc = fabsf(I_A) * dt / 3600.0f / (2 * RATED_CAPACITY_AH);
  st.efc += efc; st.soh_pct = max(50.0f, st.soh_pct - 0.021f * efc);   // true cell ageing
  // thermal: I²R heating, convective cooling, 20 kJ/K thermal mass
  float R = packResistanceOhm();
  st.temp_c += (I_A * I_A * R - 3.0f * (st.temp_c - AMBIENT_C)) / 20000.0f * dt;
  // terminal voltage
  float ocv = cellOcv(st.soc_pct) * CELLS_SERIES;
  V_pack = ocv - I_A * R;
  float cell = V_pack / CELLS_SERIES;
  cellMin = cell - 0.015f; cellMax = cell + 0.015f;
  tMax = st.temp_c + 1.5f; tMin = st.temp_c - 1.5f;
  // fault injections from the serial console
  if (injectOverchargeS > 0) { cellMax = 4.30f; injectOverchargeS -= dt; }
  if (injectHotS > 0)        { tMax = 60.0f;   injectHotS -= dt; }
}

// ═════════════════ crypto helpers (BearSSL, run on the TLS helper stack) ═════
// br_ec_keygen / br_ec_compute_pub read byte tables from flash (LoadStoreError on the ESP8266),
// so the key is drawn directly and the public point comes from the curve's mulgen().
extern "C" size_t dbp_p256_mulgen(uint8_t* R, const uint8_t* x, size_t xlen) {
  return br_ec_p256_m15.mulgen(R, x, xlen, BR_EC_secp256r1);
}
extern "C" {
  make_stack_thunk(br_ecdsa_i15_sign_raw);
  make_stack_thunk(dbp_p256_mulgen);
  size_t thunk_br_ecdsa_i15_sign_raw(const br_ec_impl*, const br_hash_class*, const void*, const br_ec_private_key*, void*);
  size_t thunk_dbp_p256_mulgen(uint8_t*, const uint8_t*, size_t);
}
void toHex(const uint8_t* b, size_t n, char* out) {
  static const char* H = "0123456789abcdef";
  for (size_t i = 0; i < n; i++) { out[2 * i] = H[b[i] >> 4]; out[2 * i + 1] = H[b[i] & 15]; }
  out[2 * n] = 0;
}
void sha256(const String& s, uint8_t out[32]) {
  br_sha256_context c; br_sha256_init(&c); br_sha256_update(&c, s.c_str(), s.length()); br_sha256_out(&c, out);
}

// ═════════════════ BMS SoH algorithm (fingerprinted) ═════════════════════════
// Model-based SoH: 100 % − k × equivalent full cycles. The calibration that is actually
// running is hashed; the hash travels with every record, so an auditor can see which
// algorithm produced each value and whether it was ever changed.
#define SOH_ALGO_ID      "DBP-SOH-EFC"
#define SOH_ALGO_VERSION "1.0.0"
float SOH_K_FADE = 0.020f;          // % SoH per equivalent full cycle (from cell datasheet)
float SOH_OFFSET = 0.0f;            // must be 0 — 'h' sets +5 to simulate a manipulated BMS
char  sohAlgoHash[65];
String sohAlgoDescriptor() {
  return String(SOH_ALGO_ID) + "|" + SOH_ALGO_VERSION + "|k_fade=" + String(SOH_K_FADE, 4) +
         "|eol=" + String(EOL_SOH_PCT, 1) + "|rated_ah=" + String(RATED_CAPACITY_AH, 1) + "|offset=" + String(SOH_OFFSET, 2);
}
void computeSohFingerprint() { uint8_t h[32]; sha256(sohAlgoDescriptor(), h); toHex(h, 32, sohAlgoHash); }
float bmsSoh() { return constrain(100.0f - SOH_K_FADE * (float)st.efc + SOH_OFFSET, 0.0f, 120.0f); }

// ═════════════════ BMS ═══════════════════════════════════════════════════════
long bmsCycleSeen = -1;
uint8_t lastPhase = 255;

void bmsStep() {
  faultOverV = cellMax > OVERCHARGE_CELL_V;
  faultUnderV = cellMin < 2.8f;
  faultOverT = tMax > 55.0f;
  faultCrash = injectCrash;
  // a cycle = one discharge followed by a completed charge
  if (lastPhase == P_CHARGE && st.phase == P_REST_AFTER_CHG) st.cycle_count++;
  lastPhase = st.phase;
}

// ═════════════════ device identity, device-side hash chain, SoH cross-check ═══
struct DevState {
  uint32_t magic, seq;
  char     prev[65];                 // hash of the previous record this device signed
  uint8_t  key[32]; bool haveKey; bool keyDeclared;
  char     declaredAlgo[65];         // last SoH algorithm hash announced to the cloud
  float    vcuSoh, sohDev;           // last VCU-measured SoH and BMS − VCU deviation (NAN = pending)
};
static const uint32_t DEV_MAGIC = 0xDEB10001;
DevState dv;
br_ec_private_key devSk; uint8_t devPub[65]; char devPubHex[131], devKeyId[17];

void saveDev() {
  File f = LittleFS.open("/dev.tmp", "w"); if (!f) return;
  f.write((const uint8_t*)&dv, sizeof(dv)); f.close(); LittleFS.rename("/dev.tmp", "/dev.bin");
}
void loadDevIdentity() {
  File f = LittleFS.open("/dev.bin", "r");
  bool ok = f && f.size() == sizeof(DevState) && f.read((uint8_t*)&dv, sizeof(dv)) == sizeof(dv) && dv.magic == DEV_MAGIC;
  if (f) f.close();
  if (!ok) { memset(&dv, 0, sizeof(dv)); dv.magic = DEV_MAGIC; strcpy(dv.prev, "DEVICE-GENESIS"); dv.vcuSoh = dv.sohDev = NAN; }
  stack_thunk_add_ref();
  if (!dv.haveKey) {                                     // first boot: create the device key pair
    br_hmac_drbg_context rng; uint8_t seed[48];
    for (int i = 0; i < 48; i++) seed[i] = (uint8_t)(RANDOM_REG32 ^ micros());
    br_hmac_drbg_init(&rng, &br_sha256_vtable, seed, sizeof(seed));
    br_hmac_drbg_generate(&rng, dv.key, 32);
    dv.key[0] &= 0x7F;                                   // guarantees 0 < key < curve order n
    dv.key[31] |= 0x01;
    dv.haveKey = true; dv.keyDeclared = false;
    saveDev();
    Serial.println("[key] new device key pair generated");
  }
  devSk.curve = BR_EC_secp256r1; devSk.x = dv.key; devSk.xlen = 32;
  size_t plen = thunk_dbp_p256_mulgen(devPub, dv.key, 32);
  if (plen != 65) Serial.println("[key] public key computation failed");
  stack_thunk_del_ref();
  toHex(devPub, 65, devPubHex);
  uint8_t h[32]; br_sha256_context c; br_sha256_init(&c); br_sha256_update(&c, devPub, 65); br_sha256_out(&c, h);
  toHex(h, 8, devKeyId);
  Serial.printf("[key] device key id %s (seq %u)\n", devKeyId, dv.seq);
}

// Signs one record: the payload is a fixed-format text of the key values, chained to the
// previous record by hash. Returns the JSON object that goes into raw_telemetry.dev
String signRecord(const String& ts, const char* type, const char* eventType, const String& socStr, const String& sohStr) {
  dv.seq++;
  String payload = String(dv.seq) + "|" + ts + "|" + type + "|" + (eventType ? eventType : "-") + "|" +
                   String(st.cycle_count) + "|" + String(st.deep_events) + "|" + String(st.over_events) + "|" +
                   String(st.accident_events) + "|" + socStr + "|" + sohStr + "|" + String(st.energy_kwh, 4) + "|" +
                   String(st.capacity_ah, 3) + "|" + String(sohAlgoHash).substring(0, 16) + "|" + dv.prev;
  uint8_t h[32]; sha256(payload, h);
  uint8_t sig[64]; size_t n = 0;
  stack_thunk_add_ref();
  n = thunk_br_ecdsa_i15_sign_raw(&br_ec_p256_m15, &br_sha256_vtable, h, &devSk, sig);
  stack_thunk_del_ref();
  char hh[65], sh[129]; toHex(h, 32, hh); toHex(sig, n == 64 ? 64 : 0, sh);
  strcpy(dv.prev, hh);
  saveDev();
  String j = "{\"seq\":"; j += dv.seq; j += ",\"key_id\":\""; j += devKeyId;
  j += "\",\"payload\":\""; j += payload; j += "\",\"hash\":\""; j += hh; j += "\",\"sig\":\""; j += sh; j += "\"}";
  return j;
}

// ═════════════════ VCU (same algorithms as dbp_vcu.py on the Raspberry Pi) ═══
double cycEdis = 0, cycEchg = 0, cycAhDis = 0;
float  cycSocMin = 100, cycSocMax = 0;
uint32_t vcuCycleSeen = 0; bool vcuInit = false;
bool flagDeep = false, flagOver = false, flagHot = false, flagCrash = false;
bool resting = false; double restStartS = 0; float restSoc = 0;
float prevI = NAN, prevV = NAN;
double simClockS = 0;
bool sendNow = false;

void queueEvent(const char* type, const String& detail);

bool cycleSeenFromStart = false;     // first cycle after boot is only partly observed

void finishCycle() {
  bool real = cycAhDis >= 0.3 * RATED_CAPACITY_AH;
  float dodNow = cycSocMax - cycSocMin;
  if (real && dodNow >= 50 && cycleSeenFromStart) {     // independent capacity measurement
    float capMeasured = cycAhDis / (dodNow / 100.0f);
    dv.vcuSoh = capMeasured / RATED_CAPACITY_AH * 100.0f;
    dv.sohDev = bmsSoh() - dv.vcuSoh;
    saveDev();
    Serial.printf("[soh] BMS %.2f%%  VCU-measured %.2f%%  deviation %+.2f%%  -> %s\n", bmsSoh(), dv.vcuSoh, dv.sohDev,
                  fabsf(dv.sohDev) > SOH_MISMATCH_LIMIT_PCT ? "FAIL" : "PASS");
    if (fabsf(dv.sohDev) > SOH_MISMATCH_LIMIT_PCT)
      queueEvent("OTHER", "SOH_CROSSCHECK_FAIL BMS " + String(bmsSoh(), 2) + "% vs VCU-measured " + String(dv.vcuSoh, 2) +
                 "% (dev " + String(dv.sohDev, 2) + "%, limit " + String(SOH_MISMATCH_LIMIT_PCT, 1) + "%) algo " +
                 String(sohAlgoHash).substring(0, 16));
  }
  cycleSeenFromStart = true;
  if (real && cycEdis > 0 && cycEchg > 0) st.rte_pct = min(100.0, cycEdis / cycEchg * 100);
  float dod = cycSocMax - cycSocMin;
  if (dod > 0) {
    st.dod_pct = dod;
    if (real && dod >= 50) st.soce_pct = (cycEdis / (dod / 100)) / RATED_ENERGY_KWH * 100;
  }
  Serial.printf("[cycle] #%u complete: %.1f Ah / %.3f kWh out, %.3f kWh in, DoD %.1f%%, RTE %.2f%%\n",
                st.cycle_count, cycAhDis, cycEdis, cycEchg, dod, st.rte_pct);
  cycEdis = cycEchg = cycAhDis = 0; cycSocMin = 100; cycSocMax = 0;
  sendNow = true;
}

void vcuStep(float dt) {
  simClockS += dt;
  float i = I_A;                                    // + = discharge
  double e = V_pack * fabsf(i) * dt / 3.6e6;        // kWh
  if (i > 0) { st.energy_kwh += e; st.capacity_ah += i * dt / 3600; cycEdis += e; cycAhDis += i * dt / 3600; }
  else if (i < 0) { st.energy_chg_kwh += e; cycEchg += e; }
  if (tMax > EXTREME_HOT_C || tMin < EXTREME_COLD_C) {
    st.t_extreme_h += dt / 3600; if (i < 0) st.t_chg_extreme_h += dt / 3600;
  }
  cycSocMin = min(cycSocMin, st.soc_pct); cycSocMax = max(cycSocMax, st.soc_pct);
  // internal resistance from ΔV/ΔI on a current step
  if (!isnan(prevI) && fabsf(i - prevI) >= 5.0f) {
    float r = fabsf((V_pack - prevV) / (i - prevI)) * 1000.0f;
    if (r > 0.2f * INITIAL_RESISTANCE_MOHM && r < 10 * INITIAL_RESISTANCE_MOHM)
      st.resistance_mohm = isnan(st.resistance_mohm) ? r : 0.9f * st.resistance_mohm + 0.1f * r;
  }
  prevI = i; prevV = V_pack;
  // self-discharge measured during rest
  if (fabsf(i) > REST_CURRENT_A) resting = false;
  else if (!resting) { resting = true; restStartS = simClockS; restSoc = st.soc_pct; }
  else if (simClockS - restStartS >= REST_MIN_FOR_SELF_DISCHARGE * 60) {
    float hours = (simClockS - restStartS) / 3600.0f, drop = restSoc - st.soc_pct;
    if (drop > 0) st.self_dis_pct_month = drop / hours * 720;
    restStartS = simClockS; restSoc = st.soc_pct;
  }
  // cycle end → RTE, DoD, SOCE
  if (!vcuInit) { vcuCycleSeen = st.cycle_count; vcuInit = true; }
  if (st.cycle_count > vcuCycleSeen) { vcuCycleSeen = st.cycle_count; finishCycle(); }
  // events (rising edge only)
  bool deep = st.soc_pct <= DEEP_DISCHARGE_SOC_PCT || faultUnderV;
  bool over = faultOverV, hot = tMax > EXTREME_HOT_C, crash = faultCrash;
  if (deep && !flagDeep)   { st.deep_events++;     queueEvent("DEEP_DISCHARGE", "SoC " + String(st.soc_pct, 1) + "%, min cell " + String(cellMin, 2) + " V"); }
  if (over && !flagOver)   { st.over_events++;     queueEvent("OVERCHARGE", "Max cell " + String(cellMax, 2) + " V"); }
  if (hot && !flagHot)     {                       queueEvent("THERMAL_EVENT", "Pack temperature " + String(tMax, 1) + " C"); }
  if (crash && !flagCrash) { st.accident_events++; queueEvent("ACCIDENT", "Crash flag set (injected from serial console)"); }
  flagDeep = deep; flagOver = over; flagHot = hot; flagCrash = crash;
  injectCrash = false;
}

// ═════════════════ GATEWAY: records, outbox, HTTPS ═══════════════════════════
time_t lastTs = 0; uint32_t lastTsMs = 0; bool timeOk = false;
uint32_t queued = 0, sentOk = 0, nextSendMs = 0, backoffS = 5; int lastHttp = 0;

String isoNow() {
  struct timeval tv; gettimeofday(&tv, nullptr);
  time_t s = tv.tv_sec; uint32_t ms = tv.tv_usec / 1000;
  if (s < lastTs || (s == lastTs && ms < lastTsMs)) { s = lastTs; ms = lastTsMs; }
  lastTs = s; lastTsMs = ms;
  struct tm t; gmtime_r(&s, &t); char b[48];
  snprintf(b, sizeof(b), "%04d-%02d-%02dT%02d:%02d:%02d.%03uZ", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
           t.tm_hour, t.tm_min, t.tm_sec, ms);
  return String(b);
}
float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
void jNum(String& j, const char* k, double v, int dec) { if (isnan(v) || isinf(v)) return; j += ",\""; j += k; j += "\":"; j += String(v, dec); }
void jInt(String& j, const char* k, long v) { j += ",\""; j += k; j += "\":"; j += String(v); }
void jStr(String& j, const char* k, const String& v) {
  j += ",\""; j += k; j += "\":\"";
  for (char c : v) { if (c == '"' || c == '\\') j += '\\'; j += c; }
  j += '"';
}

String signRecord(const String&, const char*, const char*, const String&, const String&);
void enqueue(const String& body, bool isEvent) {
  if (!isEvent && queued >= OUTBOX_MAX_RECORDS) { Serial.println("[outbox] full — telemetry skipped"); return; }
  char name[24]; snprintf(name, sizeof(name), "/q/%010u.json", st.next_seq++);
  File f = LittleFS.open(name, "w"); if (!f) { Serial.println("[outbox] write failed"); return; }
  f.print(body); f.close(); queued++; saveState();
}

String lastRecordTs;
String recordHead(const char* type) {
  String j; j.reserve(1200);
  j = "{\"bin\":\"" DBP_BIN "\",\"vcu_id\":\"" VCU_ID "\"";
  lastRecordTs = isoNow();
  jStr(j, "timestamp", lastRecordTs); jStr(j, "record_type", type);
  jInt(j, "cycle_count", st.cycle_count);
  jInt(j, "deep_discharge_events", st.deep_events);
  jInt(j, "overcharge_events", st.over_events);
  jInt(j, "accident_events", st.accident_events);
  jStr(j, "lifecycle_status", "ORIGINAL");
  return j;
}

void queueEvent(const char* type, const String& detail) {
  Serial.printf("[event] %s: %s\n", type, detail.c_str());
  if (!timeOk) { saveState(); return; }               // counted; uploaded with the next telemetry
  String j = recordHead("EVENT");
  jStr(j, "event_type", type); jStr(j, "event_detail", detail.substring(0, 480));
  j += ",\"raw_telemetry\":{\"dev\":"; j += signRecord(lastRecordTs, "EVENT", type, "-", "-"); j += "}";
  j += "}"; enqueue(j, true);
}

void queueTelemetry() {
  float soh = bmsSoh();
  float fade = (st.cycle_count >= 50 && soh < 100) ? (100 - soh) / st.cycle_count : (100 - EOL_SOH_PCT) / EXPECTED_LIFETIME_CYCLES;
  String j = recordHead("TELEMETRY");
  jNum(j, "soc_pct", clampf(st.soc_pct, 0, 100), 2);
  jNum(j, "soh_pct", clampf(soh, 0, 120), 2);
  jNum(j, "current_a", I_A, 2);
  jNum(j, "pack_voltage_v", V_pack, 2);
  jNum(j, "temp_celsius", clampf(tMax, -60, 150), 1);
  jNum(j, "energy_throughput_kwh", st.energy_kwh, 4);
  jNum(j, "capacity_throughput_ah", st.capacity_ah, 3);
  jNum(j, "time_extreme_temp_h", st.t_extreme_h, 4);
  jNum(j, "time_charging_extreme_temp_h", st.t_chg_extreme_h, 4);
  jNum(j, "remaining_capacity_ah", RATED_CAPACITY_AH * soh / 100, 3);
  jNum(j, "capacity_fade_pct", clampf(100 - soh, -20, 100), 3);
  if (fade > 0) jInt(j, "remaining_life_cycles", lroundf(max(0.0f, (soh - EOL_SOH_PCT) / fade)));
  jNum(j, "depth_of_discharge_pct", st.dod_pct, 2);
  if (!isnan(st.rte_pct)) {
    jNum(j, "round_trip_efficiency_pct", clampf(st.rte_pct, 0, 100), 3);
    jNum(j, "rte_fade_pct", clampf(INITIAL_RTE_PCT - st.rte_pct, -100, 100), 3);
  }
  if (!isnan(st.soce_pct)) jNum(j, "soce_pct", clampf(st.soce_pct, 0, 120), 3);
  if (!isnan(st.self_dis_pct_month)) jNum(j, "self_discharge_pct_per_month", clampf(st.self_dis_pct_month, 0, 100), 3);
  if (!isnan(st.resistance_mohm)) {
    float r0 = INITIAL_RESISTANCE_MOHM, p = min(RATED_POWER_W, RATED_POWER_W * r0 / st.resistance_mohm);
    jNum(j, "internal_resistance_mohm", st.resistance_mohm, 3);
    jNum(j, "internal_resistance_increase_pct", (st.resistance_mohm - r0) / r0 * 100, 2);
    jNum(j, "remaining_power_w", p, 1);
    jNum(j, "power_fade_pct", clampf((1 - p / RATED_POWER_W) * 100, -20, 100), 3);
  }
  j += ",\"raw_telemetry\":{\"source\":\"esp8266-integrated-bms-vcu\",\"simulated\":true";
  j += ",\"sim_speed\":"; j += SIM_SPEED;
  j += ",\"phase\":\""; j += PHASE_NAME[st.phase]; j += "\"";
  j += ",\"temp_min_c\":"; j += String(tMin, 1);
  j += ",\"cell_v_min\":"; j += String(cellMin, 3);
  j += ",\"cell_v_max\":"; j += String(cellMax, 3);
  j += ",\"energy_charged_kwh\":"; j += String(st.energy_chg_kwh, 4);
  j += ",\"equivalent_full_cycles\":"; j += String(st.efc, 2);
  j += ",\"fault_bits\":"; j += (faultOverV ? 1 : 0) | (faultUnderV ? 2 : 0) | (faultOverT ? 4 : 0) | (faultCrash ? 16 : 0);
  j += ",\"soh_algo\":{\"id\":\"" SOH_ALGO_ID "\",\"version\":\"" SOH_ALGO_VERSION "\",\"hash\":\""; j += sohAlgoHash;
  j += "\",\"crosscheck\":\""; j += isnan(dv.sohDev) ? "PENDING" : (fabsf(dv.sohDev) > SOH_MISMATCH_LIMIT_PCT ? "FAIL" : "PASS");
  j += "\""; if (!isnan(dv.vcuSoh)) { j += ",\"vcu_measured_soh_pct\":"; j += String(dv.vcuSoh, 2);
                                    j += ",\"deviation_pct\":"; j += String(dv.sohDev, 2); }
  j += "}";
  j += ",\"dev\":"; j += signRecord(lastRecordTs, "TELEMETRY", nullptr, String(clampf(st.soc_pct, 0, 100), 2), String(clampf(soh, 0, 120), 2));
  j += "}}";
  enqueue(j, false);
}

BearSSL::WiFiClientSecure tls;
BearSSL::X509List trust(TRUSTED_ROOTS);
BearSSL::Session tlsSession;
HTTPClient http;

void serviceOutbox() {
  if (!timeOk || WiFi.status() != WL_CONNECTED || millis() < nextSendMs || queued == 0) return;
  Dir dir = LittleFS.openDir("/q"); String first;
  while (dir.next()) if (first == "" || dir.fileName() < first) first = dir.fileName();
  if (first == "") { queued = 0; return; }
  String path = "/q/" + first;
  File f = LittleFS.open(path, "r"); String body = f.readString(); f.close();
  tls.setX509Time(time(nullptr));
  if (!http.begin(tls, API_URL)) { nextSendMs = millis() + 5000; return; }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " VCU_TOKEN);
  int code = http.POST(body);
  String resp = code > 0 ? http.getString() : http.errorToString(code);
  http.end(); lastHttp = code;
  if (code == 200 || code == 201) {
    LittleFS.remove(path); queued--; sentOk++; backoffS = 5; nextSendMs = millis() + 200;
    int h = resp.indexOf("\"record_hash\":\"");
    Serial.printf("[sent] %s  hash=%s  (%u queued)\n", first.c_str(), h >= 0 ? resp.substring(h + 15, h + 31).c_str() : "?", queued);
    return;
  }
  if (code == 409 && resp.indexOf("Chain head moved") >= 0) { nextSendMs = millis() + 1000; return; }
  if (code >= 400 && code < 500 && code != 401 && code != 408 && code != 429) {
    Serial.printf("[rejected %d] %s\n  -> moved to /dead so it cannot block the queue\n", code, resp.c_str());
    if (resp.indexOf("cannot decrease") >= 0) Serial.println("  hint: this BIN already holds higher counters. Use a fresh test BIN.");
    LittleFS.rename(path, "/dead/" + first); queued--;
    return;
  }
  Serial.printf("[retry in %us] HTTP %d: %s\n", backoffS, code, resp.substring(0, 160).c_str());
  if (code == 401) Serial.println("  hint: VCU_TOKEN in secrets.h does not match VCU_TOKEN on Render");
  if (code == 500 || code == 502) Serial.println("  hint: API is up but the database is not answering — is Supabase paused?");
  if (code < 0) Serial.println("  hint: no connection — Wi-Fi, or Render waking up after idle (can take ~1 minute)");
  nextSendMs = millis() + backoffS * 1000; backoffS = min(backoffS * 2, (uint32_t)120);
}

// ═════════════════ status, setup, loop ═══════════════════════════════════════
void printStatus() {
  Serial.println(F("──────── ESP8266 BMS + VCU status ────────"));
  Serial.printf("BIN %s | Wi-Fi %s (%d dBm) | time %s\n", DBP_BIN,
                WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "not connected", WiFi.RSSI(),
                timeOk ? isoNow().c_str() : "not synced");
  Serial.printf("BMS  phase %s | SoC %.1f%% | SoH %.2f%% | I %.1f A | V %.2f V | T %.1f C | cells %.3f-%.3f V\n",
                PHASE_NAME[st.phase], st.soc_pct, st.soh_pct, I_A, V_pack, tMax, cellMin, cellMax);
  Serial.printf("VCU  cycles %u | %.3f kWh | %.1f Ah | R %.1f mOhm | RTE %.2f%% | events deep/over/acc %u/%u/%u\n",
                st.cycle_count, st.energy_kwh, st.capacity_ah, st.resistance_mohm, st.rte_pct,
                st.deep_events, st.over_events, st.accident_events);
  Serial.printf("SOH  algo %s v%s hash %.16s | BMS %.2f%% | VCU-measured %.2f%% | dev %+.2f%%\n", SOH_ALGO_ID, SOH_ALGO_VERSION,
                sohAlgoHash, bmsSoh(), dv.vcuSoh, dv.sohDev);
  Serial.printf("SIGN key %s | device seq %u | prev %.16s\n", devKeyId, dv.seq, dv.prev);
  Serial.printf("GW   queued %u | sent %u | last HTTP %d | free heap %u\n", queued, sentOk, lastHttp, ESP.getFreeHeap());
}

uint32_t lastTickMs = 0, lastTelemetryMs = 0, lastPrintMs = 0;

void setup() {
  Serial.begin(115200); delay(300);
  Serial.println(F("\n\n=== DBP ESP8266: battery model + BMS + VCU + cloud gateway ==="));
  if (!LittleFS.begin()) { Serial.println("[fs] formatting…"); LittleFS.format(); LittleFS.begin(); }
  LittleFS.mkdir("/q"); LittleFS.mkdir("/dead");
  loadState();
  computeSohFingerprint();
  loadDevIdentity();
  Serial.printf("[soh] algorithm %s v%s fingerprint %s\n", SOH_ALGO_ID, SOH_ALGO_VERSION, sohAlgoHash);
  { Dir d = LittleFS.openDir("/q"); while (d.next()) queued++; }
  if (queued) Serial.printf("[outbox] %u records waiting from before\n", queued);
  if (String(WIFI_PASS) == "your-wifi-password" || String(VCU_TOKEN).startsWith("paste-")) Serial.println("!!! Edit WIFI_SSID / WIFI_PASS / VCU_TOKEN in the secrets.h tab !!!");
  WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  tls.setTrustAnchors(&trust); tls.setSession(&tlsSession); tls.setTimeout(15000);
  http.setReuse(true); http.setTimeout(20000);
  lastTickMs = lastTelemetryMs = millis();
  modelStep(0); bmsStep();
}

void loop() {
  uint32_t now = millis();
  if (now - lastTickMs >= 100) {                         // 10 Hz control loop
    float simDt = min(now - lastTickMs, (uint32_t)5000) / 1000.0f * SIM_SPEED;
    lastTickMs = now;
    while (simDt > 0) {                                  // sub-steps of ≤ 6 simulated seconds
      float dt = min(simDt, 6.0f);
      modelStep(dt); bmsStep(); vcuStep(dt);
      simDt -= dt;
    }
  }
  if (!timeOk && time(nullptr) > 1767225600) {           // NTP answered (after 2026-01-01)
    timeOk = true; tls.setX509Time(time(nullptr));
    Serial.printf("[time] synced %s | Wi-Fi %s\n", isoNow().c_str(), WiFi.localIP().toString().c_str());
  }
  if (timeOk && !dv.keyDeclared) {                      // publish the public key once (hash-chained in the cloud)
    queueEvent("OTHER", String("DEVICE_KEY P-256 id ") + devKeyId + " pub " + devPubHex);
    dv.keyDeclared = true; saveDev();
  }
  if (timeOk && strcmp(dv.declaredAlgo, sohAlgoHash) != 0) {   // any SoH algorithm/calibration change is declared
    queueEvent("OTHER", "SOH_ALGORITHM " + sohAlgoDescriptor() + " sha256 " + sohAlgoHash);
    strcpy(dv.declaredAlgo, sohAlgoHash); saveDev();
  }
  if (timeOk && (now - lastTelemetryMs >= TELEMETRY_INTERVAL_S * 1000UL || sendNow)) {
    queueTelemetry(); lastTelemetryMs = now; sendNow = false;
  }
  if (now - lastPrintMs >= 15000) {
    lastPrintMs = now;
    Serial.printf("[bms] %-9s SoC %5.1f%%  I %6.1f A  V %5.2f V  T %4.1f C  cycles %u\n",
                  PHASE_NAME[st.phase], st.soc_pct, I_A, V_pack, tMax, st.cycle_count);
  }
  serviceOutbox();
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 's') printStatus();
    else if (c == 'o') { injectOverchargeS = 120; Serial.println("[inject] overcharge for 2 sim-minutes"); }
    else if (c == 't') { injectHotS = 120;        Serial.println("[inject] over-temperature for 2 sim-minutes"); }
    else if (c == 'x') { injectCrash = true;      Serial.println("[inject] crash flag"); }
    else if (c == 'k') { Serial.printf("[key] id %s\n[key] public key (P-256, uncompressed) %s\n", devKeyId, devPubHex); }
    else if (c == 'h') {
      SOH_OFFSET = SOH_OFFSET == 0 ? 5.0f : 0.0f; computeSohFingerprint();
      Serial.printf("[inject] SoH algorithm %s: offset %+.1f%%, new fingerprint %.16s\n",
                    SOH_OFFSET ? "MANIPULATED" : "restored", SOH_OFFSET, sohAlgoHash);
    }
  }
}
