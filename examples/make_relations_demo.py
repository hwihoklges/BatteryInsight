"""Create 12 explicitly synthetic cell tests for the complete update workflow."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def make_relations_demo(destination: Path, count=12) -> Path:
    destination = destination.resolve()
    inbox = destination / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    mapping = {
        "cycle": {"column": "cycle"}, "step_id": {"column": "step"},
        "time_s": {"column": "time", "unit": "s"}, "step_time_s": {"column": "step_time", "unit": "s"},
        "voltage_v": {"column": "voltage", "unit": "V"}, "current_a": {"column": "current", "unit": "A"},
        "capacity_ah": {"column": "capacity", "unit": "Ah"}, "temperature_c": {"column": "temperature", "unit": "degC"},
        "soc_pct": {"column": "soc", "unit": "%"}, "mode": {"column": "mode"}, "end_flag": {"column": "end_flag"},
    }
    pipeline = json.loads(Path(__file__).with_name("run.template.json").read_text(encoding="utf-8"))
    pipeline["sources"] = [{"path": "overridden_from_registration", "cell_id": "synthetic", "test_id": "synthetic",
                            "test_type": "Cycle", "current_polarity": "charge_positive", "columns": mapping}]
    pipeline["chunk_rows"] = 5000
    (destination / "pipeline.json").write_text(json.dumps(pipeline, indent=2), encoding="utf-8")
    registrations = []
    for index in range(count):
        group = index // max(1, count // 3)
        shift = .045 * group + .007 * (index % 4)
        decay = .003 + group * .006 + .001 * (index % 4)
        name = f"SYNTHETIC_{index+1:02d}"
        rows, start = [], 0.
        for cycle in range(1, 9):
            efficiency = .999 - decay * (cycle - 1)
            for step, current, duration in [(1, 0., 60.), (2, 1., 3600.), (3, 0., 60.), (4, -1., 3600. * efficiency)]:
                points = 61 if current else 3
                for j in range(points):
                    u = j / (points - 1)
                    t = duration * u
                    if current > 0:
                        voltage = 3.05 + shift + (.96 + .004 * index) * (u + (.035 + .002 * index) * math.sin(2 * math.pi * u))
                        soc = 100 * u
                    elif current < 0:
                        voltage = 4.04 + shift - (.98 + .006 * index + .001 * cycle) * (u + (.045 + .003 * index) * math.sin(2 * math.pi * u))
                        soc = 100 * (1 - u)
                    else:
                        voltage, soc = (3.05 + shift, 0.) if step == 1 else (4.04 + shift, 100.)
                    rows.append([cycle, step, start + t, t, voltage, current, abs(current) * t / 3600,
                                 "CC" if current else "rest", "complete" if j == points - 1 else "", 25., soc])
                start += duration + 1
        with (inbox / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cycle", "step", "time", "step_time", "voltage", "current", "capacity", "mode", "end_flag", "temperature", "soc"])
            writer.writerows(rows)
        design = index % 3
        registrations.append({"experiment_id": name, "label": f"가상 설계 {chr(65 + design)} · 반응 {chr(65 + group)} · 셀 {index+1:02d}",
                              "cell_id": name, "test_id": name + "_CYCLE", "raw_path": name + ".csv",
                              "cohort": "SYNTHETIC_MATCHED_25C", "protocol": "SYNTHETIC_CYCLE", "chemistry": f"SYNTHETIC_FAMILY_{design}",
                              "temperature_c": 25, "test_type": "Cycle", "include": "true"})
        # Deliberately independent synthetic design and response groupings.
        # These labels and values are invented examples, never measured recipes.
        entry = registrations[-1]
        entry["design_group"] = "SYNTHETIC_DESIGNS"
        for electrode in ("cathode", "anode"):
            entry[f"{electrode}_active_material"] = f"SYNTHETIC_{electrode.upper()}_ACTIVE_{design}"
            entry[f"{electrode}_solid_electrolyte"] = f"SYNTHETIC_SE_{design % 2}"
            entry[f"{electrode}_binder"] = "SYNTHETIC_BINDER"
            entry[f"{electrode}_conductive_additive"] = "SYNTHETIC_CARBON"
            active = (84 - 2 * design) if electrode == "cathode" else (70 - 5 * design)
            entry[f"{electrode}_active_wt_pct"] = active
            entry[f"{electrode}_solid_electrolyte_wt_pct"] = 97 - active
            entry[f"{electrode}_conductive_wt_pct"] = 1
            entry[f"{electrode}_binder_wt_pct"] = 2
            entry[f"{electrode}_other_wt_pct"] = 0
        entry["cathode_areal_capacity_mAh_cm2"] = round(4.5 + .7 * design + .03 * (index // 3), 3)
        entry["anode_areal_capacity_mAh_cm2"] = round(5.0 + .8 * design + .04 * (index // 3), 3)

    with (destination / "experiments.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(registrations[0]))
        writer.writeheader()
        writer.writerows(registrations)
    relation = json.loads(Path(__file__).with_name("relations.template.json").read_text(encoding="utf-8"))
    relation.update(inbox="inbox", registrations="experiments.csv", pipeline_template="pipeline.json", settle_seconds=0, is_demo=True)
    relation["comparison"].update(life_min=1, life_max=8, min_points=8)
    result = destination / "relations.json"
    result.write_text(json.dumps(relation, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(make_relations_demo(args.out))
