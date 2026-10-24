from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk

from battery_insight.desktop import BatteryInsightApp
from battery_insight.folder_queue import run_folders
from battery_insight.library import library_rows
from cyc2parquet.selftest import fixture_folder


def test_folder_failure_continues_to_later_folders_in_order(tmp_path):
    sources = [tmp_path / name for name in ("bad", "good", "partial")]
    for source in sources:
        source.mkdir()
    calls, events = [], []

    def convert(source, *, progress):
        calls.append(source)
        if source.name == "bad":
            raise ValueError("bad folder")
        return {"status": "success" if source.name == "good" else "partial_failure",
                "summary": {"files_total": 2, "complete": 1, "failed": 1, "rows": 7}}

    result = run_folders(sources, convert, progress=events.append)
    assert calls == sources
    assert result["status"] == "partial_failure" and result["summary"]["rows"] == 14
    assert [event["queue_state"] for event in events] == ["running", "failed", "running", "success", "running", "partial_failure"]


def test_gui_selection_and_multi_selection_wait_for_explicit_start(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    first, second = raw / "first", raw / "second"
    fixture_folder(first)
    fixture_folder(second)
    project = tmp_path / "project"
    errors = []
    monkeypatch.setattr("battery_insight.folder_queue.filedialog.askdirectory", lambda **kwargs: str(raw))
    monkeypatch.setattr("battery_insight.desktop.messagebox.showerror", lambda *args, **kwargs: errors.append(args))
    root = tk.Tk()
    root.withdraw()
    app = BatteryInsightApp(root, project)
    try:
        panel = app.conversion_queue
        panel.choose_many()
        picker = next(child for child in panel.winfo_children() if isinstance(child, tk.Toplevel))
        choices = next(child for child in picker.winfo_children() if isinstance(child, ttk.Treeview))
        choices.selection_set(choices.get_children())
        for frame in picker.winfo_children():
            for button in frame.winfo_children():
                if "text" in button.keys() and button.cget("text") == "선택 폴더를 대기 목록에 추가":
                    button.invoke()
                    break
        assert panel.sources == [first.resolve(), second.resolve()]
        root.update()
        assert not app.busy and app.worker is None
        assert not (project / "converted").exists()
        panel.add([first, first / "하위 폴더"])
        assert len(panel.sources) == 2
        panel.tree.selection_set(str(first.resolve()))
        panel.move(1)
        assert panel.sources == [second.resolve(), first.resolve()]
        panel.start_button.invoke()
        assert app.busy and panel.busy
        deadline = time.monotonic() + 40
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        assert not app.busy and not errors
        assert len(library_rows(project)) == 6
        assert [panel.tree.set(str(source), "status") for source in panel.sources] == ["완료", "완료"]
    finally:
        if app.worker and app.worker.is_alive():
            app.worker.join(30)
        app.busy = False
        app.close()
