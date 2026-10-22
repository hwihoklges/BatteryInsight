"""Generate labelled synthetic data, never a substitute for measured validation."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path


def make_demo(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    start = 0.
    for cycle in range(1, 9):
        for step, current, duration in [(1, 0., 60.), (2, 1., 3600.), (3, 0., 60.), (4, -1., 3600. * (1 - .04 * max(cycle - 3, 0)))]:
            count = max(2, int(duration / 60) + 1)
            for j in range(count):
                t = duration * j / (count - 1)
                v = 3. + t / duration if current > 0 else 4. - t / duration if current < 0 else (3. if step == 1 else 4.)
                rows.append([cycle, step, start + t, t, v, current, abs(current) * t / 3600.,
                             "CC" if current else "rest", "complete" if j == count - 1 else "", 25.])
            start += duration + 1.
    raw_path = destination / "SYNTHETIC_demo.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["cycle", "step", "time", "step_time", "voltage", "current", "capacity", "mode", "end_flag", "temperature"])
        writer.writerows(rows)
    config = json.loads((Path(__file__).with_name("run.template.json")).read_text(encoding="utf-8"))
    config["chunk_rows"] = 73
    config["sources"] = [{"path": raw_path.name, "cell_id": "SYNTHETIC_CELL", "test_id": "SYNTHETIC_TEST",
                          "test_type": "Cycle", "current_polarity": "charge_positive", "columns": {
                              "cycle": {"column": "cycle"}, "step_id": {"column": "step"},
                              "time_s": {"column": "time", "unit": "s"}, "step_time_s": {"column": "step_time", "unit": "s"},
                              "voltage_v": {"column": "voltage", "unit": "V"}, "current_a": {"column": "current", "unit": "A"},
                              "capacity_ah": {"column": "capacity", "unit": "Ah"}, "temperature_c": {"column": "temperature", "unit": "degC"},
                              "mode": {"column": "mode"}, "end_flag": {"column": "end_flag"}}}]
    config["diagnostics"]["baseline_window"] = 3
    config_path = destination / "demo.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    print(make_demo(parser.parse_args().out))
