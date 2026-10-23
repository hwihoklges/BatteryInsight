from pathlib import Path
import time
import tkinter as tk

from battery_insight.desktop import BatteryInsightApp
from battery_insight.traceability_demo import create_demo
from battery_insight.traceability_ui import TraceWindow, retained_evidence


def pump(root, app):
    deadline = time.monotonic() + 40
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(.02)
    assert not app.busy


def test_real_trace_window_loads_reviewed_ppt_and_opens_verified_raw_graph(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    create_demo(project)
    errors = []
    monkeypatch.setattr("battery_insight.desktop.messagebox.showerror", lambda *args, **kw: errors.append(args))
    root = tk.Tk()
    root.withdraw()
    app = BatteryInsightApp(root, project)
    opened = []
    monkeypatch.setattr(app, "open_path", lambda path: opened.append(Path(path)))
    window = TraceWindow(app)
    try:
        pump(root, app)
        assert len(window.entities.get_children()) == 6
        assert len(window.records.get_children()) == 2 and len(window.edges.get_children()) == 7
        assert all(row["state"] == "accepted" for row in window.by_proposal.values())
        window.raw_choice.set(next(iter(window.dataset_labels)))
        window.open_graph()
        pump(root, app)
        assert opened and opened[0].is_file() and not errors
        assert "verified_now" in opened[0].read_text(encoding="utf-8")
        entity = next(iter(window.by_entity.values()))
        editor = window.editor("entity", entity)
        root.update_idletasks()
        assert editor.winfo_exists()
        editor.destroy()
    finally:
        if app.worker and app.worker.is_alive():
            app.worker.join(30)
        app.busy = False
        window.window.destroy()
        app.close()


def test_value_level_header_cell_proof_survives_review_but_changed_value_rebinds():
    old = {"name": "old", "parameters": {"pressing_pressure": {"value": 100, "unit": "MPa"}},
           "field_evidence": {"name": ["header", "value"], "parameters.pressing_pressure": ["pheader", "pvalue"]}}
    new = {**old, "name": "edited", "evidence_ids": ["header", "value", "pheader", "pvalue", "new"]}
    refs = retained_evidence(old, new, "entity")
    assert refs["parameters.pressing_pressure"] == ["pheader", "pvalue"]
    assert refs["name"] == new["evidence_ids"]
