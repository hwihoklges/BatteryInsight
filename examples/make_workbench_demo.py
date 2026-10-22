"""Synthetic workflow fixture: CC/CV, a measured-SOC pulse, and repeated designs.

Invented signals exercise the software. They are not a calibrated cell model.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from make_relations_demo import make_relations_demo


def make_workbench_demo(destination: Path):
    config_path = make_relations_demo(destination)
    destination = config_path.parent
    registry_path = destination / "experiments.csv"
    with registry_path.open(encoding="utf-8-sig", newline="") as handle:
        registrations = list(csv.DictReader(handle))
    for index, entry in enumerate(registrations):
        material = index % 2
        loading = (index // 2) % 2
        response = index // 4
        entry.update(design_id=f"SYNTHETIC_RECIPE_{material}_{loading}", batch_id="SYNTHETIC_BATCH",
                     label=f"가상 설계 {chr(65+material)}{loading} · 반응 {chr(65+response)} · 셀 {index+1:02d}",
                     chemistry=f"SYNTHETIC_CHEMISTRY_{material}", pressure_mpa="1.0",
                     charge_rate_c="1.0", discharge_rate_c="1.0", voltage_min_v="3.0", voltage_max_v="4.2",
                     formation_id="SYNTHETIC_FORMATION", cathode_areal_capacity_mAh_cm2=4.5 + .6 * loading,
                     anode_areal_capacity_mAh_cm2=5.8)
        for electrode in ("cathode", "anode"):
            entry[f"{electrode}_active_material"] = f"SYNTHETIC_{electrode.upper()}_{material}"
            entry[f"{electrode}_solid_electrolyte"] = f"SYNTHETIC_SE_{material}"
            active = (84 - 2 * material) if electrode == "cathode" else (70 - 5 * material)
            for name, value in {"active": active, "solid_electrolyte": 97-active, "conductive": 1, "binder": 2, "other": 0}.items():
                entry[f"{electrode}_{name}_wt_pct"] = value
        rows, clock = [], 0.
        shift = .035 * response + .002 * index
        for cycle in range(1, 9):
            efficiency = .998 - (.003 + .004 * response) * (cycle - 1)
            r0 = .025 + .008 * response + .001 * cycle
            r1 = .012 + .001 * index
            total_discharge = 3600 * efficiency

            def append(step, times, current_fn, voltage_fn, soc_fn, mode):
                nonlocal clock
                capacity, previous_i, previous_t = 0., None, None
                for number, t in enumerate(times):
                    current = current_fn(t)
                    if previous_t is not None:
                        capacity += (abs(previous_i) + abs(current)) * .5 * (t-previous_t) / 3600
                    rows.append([cycle, step, clock+t, t, voltage_fn(t), current, capacity, mode,
                                 "complete" if number == len(times)-1 else "", 25., soc_fn(t)])
                    previous_i, previous_t = current, t
                clock += times[-1] + 1

            def voltage_soc(soc):
                u = 1 - soc / 100
                return 4.05 + shift - 1.0 * (u + (.04 + .002 * index) * math.sin(2 * math.pi * u))

            append(1, list(range(11)), lambda t: 0, lambda t: 3.05+shift, lambda t: 0, "rest")
            append(2, [i*60. for i in range(61)], lambda t: 1.,
                   lambda t: 3.05+shift+(t/3600+.035*math.sin(2*math.pi*t/3600)), lambda t: 100*t/3600, "CC")
            cv_duration = 240. + 20 * response + 3 * cycle
            append(3, [cv_duration*i/30 for i in range(31)], lambda t: math.exp(-t/100),
                   lambda t: 4.05+shift, lambda t: 100., "CV")
            append(4, list(range(11)), lambda t: 0., lambda t: voltage_soc(100), lambda t: 100., "rest")
            early = .2 * total_discharge
            append(5, [early*i/20 for i in range(21)], lambda t: -1.,
                   lambda t: voltage_soc(100-20*t/early), lambda t: 100-20*t/early, "CC")
            append(6, list(range(11)), lambda t: 0., lambda t: voltage_soc(80), lambda t: 80., "rest")
            append(7, [0., .1] + list(range(1, 61)), lambda t: -1.,
                   lambda t: voltage_soc(80)-r0-r1*(1-math.exp(-t/8)), lambda t: 80-100*t/total_discharge, "CC")
            rest_duration = .8 * total_discharge - 60
            start_soc = 80 - 6000/total_discharge
            append(8, [rest_duration*i/60 for i in range(61)], lambda t: -1.,
                   lambda t: voltage_soc(start_soc*(1-t/rest_duration)), lambda t: start_soc*(1-t/rest_duration), "CC")
            append(9, list(range(61)), lambda t: 0.,
                   lambda t: voltage_soc(0)+.015*(1-math.exp(-t/12)), lambda t: 0., "rest")
        with (destination / "inbox" / entry["raw_path"]).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["cycle", "step", "time", "step_time", "voltage", "current", "capacity", "mode", "end_flag", "temperature", "soc"])
            writer.writerows(rows)
    with registry_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(registrations[0]))
        writer.writeheader()
        writer.writerows(registrations)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["comparison"]["min_points"] = 3
    config["workbench"] = {"metric_packs": ["cc_cv", "soc_dcir", "rest"]}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(make_workbench_demo(args.out))
