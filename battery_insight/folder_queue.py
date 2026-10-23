"""Explicit-start, sequential folder conversion shared by both desktop apps."""
from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def run_folders(sources, convert, *, progress=None):
    """Run one folder at a time; a failed folder cannot discard later jobs."""
    sources = tuple(Path(source).resolve() for source in sources)
    results = []
    for index, source in enumerate(sources, 1):
        context = {"queue_index": index, "queue_total": len(sources), "queue_source": str(source)}

        def emit(event):
            if progress:
                progress({**event, **context})

        emit({"queue_state": "running", "message": f"[{index}/{len(sources)} 폴더] 시작: {source}"})
        try:
            result = convert(source, progress=emit)
        except Exception as exc:
            result = {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        results.append({"source": str(source), **result})
        emit({"queue_state": result.get("status", "failed"), "result": result,
              "message": f"[{index}/{len(sources)} 폴더] {result.get('status', 'failed')}: {source}" +
                         (" · " + result["error"] if result.get("error") else "")})
    summaries = [item.get("conversion", item).get("summary", {}) for item in results]
    failed_folders = sum(item.get("status") != "success" for item in results)
    any_complete = any(item.get("complete", 0) for item in summaries) or any(item.get("status") == "success" for item in results)
    return {"status": "success" if not failed_folders else "partial_failure" if any_complete else "failed",
            "folders_total": len(results), "folders_failed": failed_folders, "folders": results,
            "summary": {name: sum(item.get(name, 0) for item in summaries)
                        for name in ("files_total", "complete", "failed", "rows", "reused")}}


class FolderQueue(ttk.LabelFrame):
    """Selecting or rearranging folders only changes the list, never starts IO."""

    def __init__(self, parent, *, on_start, notify=None):
        super().__init__(parent, text="변환 대기 목록 · 폴더 추가 → 순서 확인 → 시작", padding=8)
        self.notify = notify or (lambda message: None)
        self.sources = []
        self.busy = False
        self.controls = []
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        for label, action in (("폴더 추가", self.choose_one), ("여러 하위 폴더 선택", self.choose_many),
                              ("선택 제거", self.remove_selected), ("목록 비우기", self.clear),
                              ("위로", lambda: self.move(-1)), ("아래로", lambda: self.move(1))):
            button = ttk.Button(bar, text=label, command=action)
            button.pack(side="left", padx=(0, 4))
            self.controls.append(button)
        self.start_button = ttk.Button(bar, text="시작", command=on_start)
        self.start_button.pack(side="right", padx=(8, 0))
        self.controls.append(self.start_button)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(body, columns=("order", "status", "source"), show="headings", height=4,
                                 selectmode="extended")
        for key, label, width in (("order", "순서", 45), ("status", "상태", 95), ("source", "원본 폴더", 600)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key == "source")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        ttk.Label(self, text="폴더를 선택해도 변환하지 않습니다. 시작을 누르면 위에서 아래로 한 폴더씩 변환합니다.").pack(anchor="w", pady=(5, 0))

    def add(self, paths):
        if self.busy:
            return
        skipped = []
        for value in paths:
            source = Path(value).expanduser().resolve()
            if not source.is_dir():
                skipped.append(f"폴더 없음: {source}")
                continue
            if any(source == old or source.is_relative_to(old) or old.is_relative_to(source) for old in self.sources):
                skipped.append(f"중복·상하위 폴더 제외: {source}")
                continue
            self.sources.append(source)
            self.tree.insert("", "end", iid=str(source), values=(len(self.sources), "대기", str(source)))
        self.notify(f"대기 목록 {len(self.sources)}개 · 시작 버튼을 눌러 변환하세요." + ("\n" + "\n".join(skipped) if skipped else ""))

    def choose_one(self):
        if self.busy:
            return
        source = filedialog.askdirectory(parent=self.winfo_toplevel(), title="대기 목록에 추가할 CYC 원본 폴더")
        if source:
            self.add([source])

    def choose_many(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(parent=self.winfo_toplevel(), title="여러 실험 폴더가 들어 있는 상위 폴더 선택")
        if not selected:
            return
        parent = Path(selected).resolve()
        try:
            children = sorted((path for path in parent.iterdir() if path.is_dir() and not path.is_symlink() and not path.is_junction()),
                              key=lambda path: path.name.casefold())
        except OSError as exc:
            messagebox.showerror("폴더 목록", str(exc), parent=self.winfo_toplevel())
            return
        if not children:
            messagebox.showinfo("하위 폴더 없음", "하위 폴더가 없습니다. ‘폴더 추가’에서 이 폴더를 선택하세요.", parent=self.winfo_toplevel())
            return
        window = tk.Toplevel(self)
        window.title("여러 폴더 선택 · Ctrl/Shift로 복수 선택")
        window.geometry("800x440")
        window.transient(self.winfo_toplevel())
        window.grab_set()
        ttk.Label(window, text=str(parent), wraplength=760).pack(anchor="w", padx=12, pady=10)
        choices = ttk.Treeview(window, columns=("name",), show="headings", selectmode="extended")
        choices.heading("name", text="추가할 하위 폴더 선택 (각 폴더의 모든 하위 폴더도 변환)")
        choices.pack(fill="both", expand=True, padx=12)
        for index, child in enumerate(children):
            choices.insert("", "end", iid=str(index), values=(child.name,))
        controls = ttk.Frame(window)
        controls.pack(fill="x", padx=12, pady=12)
        ttk.Button(controls, text="전체 선택", command=lambda: choices.selection_set(choices.get_children())).pack(side="left")

        def accept():
            picked = [children[int(item)] for item in choices.selection()]
            if picked:
                self.add(picked)
                window.destroy()

        ttk.Button(controls, text="선택 폴더를 대기 목록에 추가", command=accept).pack(side="right")
        ttk.Button(controls, text="취소", command=window.destroy).pack(side="right", padx=6)

    def remove_selected(self):
        if self.busy:
            return
        selected = set(self.tree.selection())
        self.sources[:] = [source for source in self.sources if str(source) not in selected]
        self.tree.delete(*selected)
        self._number()

    def clear(self):
        if not self.busy:
            self.sources.clear()
            self.tree.delete(*self.tree.get_children())

    def move(self, offset):
        if self.busy:
            return
        selected = self.tree.selection()
        if len(selected) != 1:
            return
        index = self.sources.index(Path(selected[0]))
        target = index + offset
        if 0 <= target < len(self.sources):
            self.sources[index], self.sources[target] = self.sources[target], self.sources[index]
            self.tree.move(selected[0], "", target)
            self._number()

    def _number(self):
        for index, source in enumerate(self.sources, 1):
            self.tree.set(str(source), "order", index)

    def set_busy(self, value):
        self.busy = value
        for control in self.controls:
            control.configure(state="disabled" if value else "normal")

    def update_event(self, event):
        source = event.get("queue_source")
        if source and self.tree.exists(source) and event.get("queue_state"):
            status = {"running": "변환 중", "success": "완료", "partial_failure": "일부 실패", "failed": "실패"}
            self.tree.set(source, "status", status.get(event["queue_state"], event["queue_state"]))
