#!/usr/bin/env python3
"""
fake_bms.py — pretends to be the STM32 BMS on a CAN bus, using the frames in a DBC.

Use it to test dbp_vcu.py on your desk before the real BMS is connected:

  # on the Pi (or any Linux PC) with a virtual CAN interface
  sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up
  python3 fake_bms.py --dbc bms_example.dbc --channel vcan0            # terminal 1
  python3 dbp_vcu.py --config vcu_config.json --channel vcan0 --dry-run # terminal 2

It cycles SoC 90 % → 4 % → 95 % every --cycle-seconds, increments the cycle count
at the end of each charge, and (with --phase2) also sends the proposed
BMS_Thermal_Fault frame. --hot pushes Temp_Max above 45 °C during discharge.
"""
import argparse
import math
import time

import can
import cantools


def simulate(bus, db, cycle_seconds=60.0, run_seconds=None, phase2=True, hot=False,
             start_cycle=0, soh=96.0, stop=None):
    st_msg = db.get_message_by_name("BMS_Static")
    dy_msg = db.get_message_by_name("BMS_Dynamic")
    th_msg = db.get_message_by_name("BMS_Thermal_Fault") if phase2 else None
    t0 = time.monotonic()
    cycle, last_static, last_phase = start_cycle, 0.0, None
    while True:
        now = time.monotonic() - t0
        if run_seconds and now >= run_seconds or stop is not None and stop.is_set():
            return cycle
        phase = (now % cycle_seconds) / cycle_seconds          # 0…1
        if phase < 0.5:                                        # discharge 90 → 4 %
            soc, cur = 90 - 86 * (phase / 0.5), 25.0
        else:                                                  # charge 4 → 95 %
            soc, cur = 4 + 91 * ((phase - 0.5) / 0.5), -20.0
        if last_phase is not None and phase < last_phase:      # wrapped → charge finished
            cycle += 1
        last_phase = phase
        cur += 2 * math.sin(now * 3)                           # ripple
        ocv = 44.0 + 10.0 * soc / 100                          # crude OCV curve, 13S Li-ion
        v_pack = ocv - cur * 0.05                              # 50 mΩ
        temp = (52 if hot and cur > 0 else 31) + 3 * math.sin(now / 5)

        if now - last_static >= 1.0:                           # 1 Hz static frame
            bus.send(can.Message(arbitration_id=st_msg.frame_id, is_extended_id=False, data=st_msg.encode(
                {"Pack_Voltage_Nominal": 48.0, "Rated_Capacity": 60.0, "BIN_Serial": 65556})))
            last_static = now
        bus.send(can.Message(arbitration_id=dy_msg.frame_id, is_extended_id=False, data=dy_msg.encode(
            {"Pack_Current": round(cur, 1), "SoC": round(soc * 2) / 2,
             "SoH": round(soh * 2) / 2, "Cycle_Count": cycle})))
        if th_msg:
            bus.send(can.Message(arbitration_id=th_msg.frame_id, is_extended_id=False, data=th_msg.encode(
                {"Pack_Voltage": round(v_pack, 2), "Temp_Max": round(temp), "Temp_Min": round(temp - 4),
                 "Cell_V_Min": round(v_pack / 13 - 0.02, 2), "Cell_V_Max": round(v_pack / 13 + 0.02, 2),
                 "Fault_Overvoltage": 0, "Fault_Undervoltage": 0, "Fault_Overtemp": 0,
                 "Fault_Overcurrent": 0, "Fault_Crash": 0})))
        time.sleep(0.1)                                        # 10 Hz dynamic frames


def main():
    ap = argparse.ArgumentParser(description="Fake STM32 BMS CAN sender")
    ap.add_argument("--dbc", default="bms_example.dbc")
    ap.add_argument("--interface", default="socketcan")
    ap.add_argument("--channel", default="vcan0")
    ap.add_argument("--cycle-seconds", type=float, default=60.0)
    ap.add_argument("--start-cycle", type=int, default=0)
    ap.add_argument("--no-phase2", action="store_true", help="only send today's two frames")
    ap.add_argument("--hot", action="store_true", help="Temp_Max > 45 °C while discharging")
    a = ap.parse_args()
    bus = can.Bus(interface=a.interface, channel=a.channel)
    print(f"fake BMS sending on {a.channel}; Ctrl+C to stop")
    try:
        simulate(bus, cantools.database.load_file(a.dbc), a.cycle_seconds,
                 phase2=not a.no_phase2, hot=a.hot, start_cycle=a.start_cycle)
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
