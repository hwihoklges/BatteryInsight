from pathlib import Path
import json
import struct
import time
import tkinter as tk
from urllib.request import urlopen

import pytest

from battery_insight.desktop import BatteryInsightApp
from battery_insight.library import convert_and_register, library_rows, assign_metadata
from battery_insight.library_workbench import workbench_config
from cyc2parquet.selftest import fixture_folder
from battery_insight.workbench_server import Workbench


def test_real_gui_worker_conversion_and_local_workbench(tmp_path, monkeypatch):
    raw, project = tmp_path / "raw", tmp_path / "project"
    fixture_folder(raw)
    messages = []
    monkeypatch.setattr("battery_insight.desktop.messagebox.showerror", lambda *a, **k: messages.append(a))
    root = tk.Tk()
    root.withdraw()
    app = BatteryInsightApp(root, project)
    try:
        app.task("native conversion", lambda: convert_and_register(project, raw, progress=app.progress))
        deadline = time.monotonic() + 30
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        assert not app.busy and not messages
        assert len(app.tree.get_children()) == 3
        app.tree.selection_set(app.rows[0]["dataset_id"])
        assert app.selected()["rows"] > 0
        address = app.start_workbench(workbench_config(project), project / "workbench")
        with urlopen(address + "api/state", timeout=10) as reply:
            payload = json.load(reply)
        assert payload["bundle"]["source_mode"] == "parquet_library"
        assert len(payload["bundle"]["experiments"]) == 3
    finally:
        if app.worker and app.worker.is_alive():
            app.worker.join(30)
        app.busy = False
        app.close()


def test_equal_cell_mean_not_equal_file_mean(tmp_path):
    raw, project = tmp_path / "raw", tmp_path / "project"
    fixture_folder(raw)
    content = bytearray((raw / "sample.cyc").read_bytes())
    (raw / "repeat.cyc").write_bytes(content)
    (raw / "repeat.cts").write_bytes((raw / "sample.cts").read_bytes())
    for offset in range(392, len(content) - 12, 120):
        voltage = struct.unpack_from("<f", content, offset + 28)[0]
        struct.pack_into("<f", content, offset + 28, voltage + 1000)
    (raw / "B.cyc").write_bytes(content)
    (raw / "B.cts").write_bytes((raw / "sample.cts").read_bytes())
    convert_and_register(project, raw)
    for row in library_rows(project):
        if row["relative_path"] in {"sample.parquet", "repeat.parquet", "B.parquet"}:
            assign_metadata(project, row["dataset_id"], {"physical_cell_id": "B" if row["relative_path"] == "B.parquet" else "A",
                "design_id": "D", "batch_id": "batch", "test_type": "Cycle", "protocol": "P", "chemistry": "C", "temperature_c": 25,
                "voltage_unit": "mV", "capacity_unit": "uAh"})
    controller = Workbench(workbench_config(project), project / "workbench")
    options = {**controller.options, "summary": "snapshot", "life_min": 1, "life_max": 1, "min_points": 1,
               "windows": [{"label": "one", "life_min": 1, "life_max": 1}], "enabled_modules": ["replicates"]}
    controller.compare(options)
    mean = next(row for row in controller.analysis["replicates"] if row["metric_id"] == "profile.mean_voltage_v")
    assert mean["n_cells"] == 2 and mean["n_tests"] == 3
    expected_a = (3100.125 + 3200.25 + 3300.5) / 3 / 1000
    assert mean["mean"] == pytest.approx(expected_a + .5)
