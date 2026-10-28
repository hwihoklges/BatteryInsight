"""One offline entry point for conversion, classification, evidence and analysis."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import __version__
from . import library
from .folder_queue import FolderQueue, run_folders

LABELS = {"sample_name": "샘플 이름", "physical_cell_id": "물리 셀 ID", "test_type": "시험 종류",
    "test_date": "시험 날짜 YYYY-MM-DD", "date_kind": "날짜 의미(test_start 등)", "set_no": "세트 번호",
    "namespace": "세트 구분 범위/프로젝트", "channel_id": "채널", "design_id": "설계 ID", "batch_id": "배치 ID",
    "rpt_stage": "RPT 시점", "voltage_unit": "확인한 원본 전압 단위", "capacity_unit": "확인한 원본 용량 단위",
    "temperature_c": "온도 °C", "pressure_mpa": "구동압 MPa", "protocol": "시험 프로토콜", "chemistry": "전지 계열",
    "cathode_active_material": "양극 활물질", "anode_active_material": "음극 활물질",
    "cathode_active_mass_loading_mg_cm2": "양극 활물질 로딩 mg/cm²", "anode_active_mass_loading_mg_cm2": "음극 활물질 로딩 mg/cm²"}
FIRST_FIELDS = list(LABELS)


class BatteryInsightApp:
    def __init__(self, root, project=None):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.worker = None
        self.controls = []
        self.servers = {}
        self.rows = []
        self.evidence_rows = {}
        self.project = tk.StringVar(value=str(Path(project).resolve() if project else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BatteryInsight/Project"))
        self.status = tk.StringVar(value="프로젝트를 선택하고 CYC 또는 Parquet 폴더를 등록하세요.")
        self.filter = tk.StringVar()
        self.auto = tk.BooleanVar(value=False)
        root.title(f"BatteryInsight {__version__} · 통합 데이터 작업실")
        root.geometry("1160x790")
        root.minsize(850, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("맑은 고딕", 10))
        style.configure("Title.TLabel", font=("맑은 고딕", 20, "bold"))
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="BatteryInsight", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="CYC → Parquet → 실험·설계 분류 → 장표 연결 → 다차원 비교 · 로컬 실행 / AI 호출 없음").pack(anchor="w", pady=(3, 10))
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="프로젝트").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.project)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        self.controls.append(entry)
        self.button(bar, "새 프로젝트 / 열기", self.choose_project).pack(side="left")
        self.button(bar, "폴더 열기", lambda: self.open_path(self.project.get())).pack(side="left", padx=(6, 0))
        book = ttk.Notebook(outer)
        book.pack(fill="both", expand=True)
        self.book = book
        self.data_tab = ttk.Frame(book, padding=14)
        self.docs_tab = ttk.Frame(book, padding=14)
        self.analysis_tab = ttk.Frame(book, padding=14)
        self.log_tab = ttk.Frame(book, padding=14)
        for frame, text in ((self.data_tab, "데이터·실험 분류"), (self.docs_tab, "장표·샘플 연결"), (self.analysis_tab, "지표·관계망·분석"), (self.log_tab, "실행 기록")):
            book.add(frame, text=text)
        self._data_widgets()
        self._document_widgets()
        self._analysis_widgets()
        self.log = tk.Text(self.log_tab, wrap="word", state="disabled", font=("맑은 고딕", 10))
        self.log.pack(fill="both", expand=True)
        ttk.Label(outer, textvariable=self.status, wraplength=1100).pack(anchor="w", pady=(10, 0))
        self.poll_id = root.after(100, self.poll)
        self.watch_id = root.after(30000, self.watch)

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command, padding=(9, 5))
        self.controls.append(button)
        return button

    def _data_widgets(self):
        frame = self.data_tab
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        for text, command in (("CYC 폴더 대기 목록에 추가", self.convert), ("Parquet 폴더 등록", self.scan),
                              ("변경 반영", self.refresh_all), ("변환 진단", self.diagnose_conversions),
                              ("선택 실험 정보", self.edit_metadata), ("표 새로고침", self.reload)):
            self.button(actions, text, command).pack(side="left", padx=(0, 5))
        ttk.Label(frame, text="원본 데이터는 보존합니다. 파일명 후보는 미확인 정보이며, 확인한 설계·조건·단위만 비교에 사용합니다.", wraplength=1050).pack(anchor="w", pady=8)
        self.conversion_queue = FolderQueue(frame, on_start=self.start_conversion,
                                            notify=lambda message: self.status.set(message))
        self.conversion_queue.pack(fill="x", pady=(0, 8))
        filters = ttk.Frame(frame)
        filters.pack(fill="x")
        ttk.Label(filters, text="검색").pack(side="left")
        search = ttk.Entry(filters, textvariable=self.filter)
        search.pack(side="left", fill="x", expand=True, padx=6)
        search.bind("<KeyRelease>", lambda event: self.render_rows())
        ttk.Checkbutton(filters, text="30초마다 변경 반영", variable=self.auto).pack(side="right")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=("status", "sample", "type", "design", "batch", "rows", "path"), show="headings", selectmode="browse")
        for key, label, width in (("status", "확인 상태", 90), ("sample", "샘플", 110), ("type", "시험", 65), ("design", "설계", 100), ("batch", "배치", 85), ("rows", "기록 수", 85), ("path", "파일", 440)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key == "path")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda event: self.edit_metadata())
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x")
        for text, command in (("등록표 CSV 내보내기", self.export_registration), ("등록표 CSV/Excel 가져오기", self.import_registration),
                              ("분류 규칙", self.rules_dialog), ("근거·후보 보기", self.show_selected)):
            self.button(bottom, text, command).pack(side="left", padx=(0, 6))

    def _document_widgets(self):
        frame = self.docs_tab
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        for text, command in (("PPTX / PDF / Excel 추가", self.add_documents), ("근거 목록 갱신", self.reload_documents),
                              ("선택 근거로 실험 등록", self.record_dialog), ("선택 데이터와 장표 기록 연결", self.link_dialog),
                              ("장표 검토 화면", lambda: self.open_path(self.project_root() / "knowledge/review/index.html"))):
            self.button(actions, text, command).pack(side="left", padx=(0, 5))
        ttk.Label(frame, text="장표의 날짜·세트·샘플 정보를 확인해 등록합니다. 이미지 표·그래프는 자동 확정하지 않습니다.", wraplength=1050).pack(anchor="w", pady=10)
        self.button(frame, "PPT · 공정부터 raw까지 추적", self.open_traceability).pack(anchor="w", pady=(0, 8))
        self.doc_tree = ttk.Treeview(frame, columns=("document", "locator", "text"), show="headings")
        for key, title, width in (("document", "문서/근거", 250), ("locator", "위치", 130), ("text", "텍스트·확인할 내용", 650)):
            self.doc_tree.heading(key, text=title)
            self.doc_tree.column(key, width=width)
        self.doc_tree.pack(fill="both", expand=True)
        self.doc_tree.bind("<Double-1>", lambda event: self.show_evidence())

    def _analysis_widgets(self):
        frame = self.analysis_tab
        ttk.Label(frame, text="설계·전기화학 관계망 / 레이더 / 조건 통제 비교 / 셀 평균 / 상관 / 궤적", font=("맑은 고딕", 14, "bold")).pack(anchor="w", pady=8)
        ttk.Label(frame, text="비교 작업실에서 기능과 지표를 각각 켜고 끌 수 있습니다. 셀 평균은 같은 설계·배치·확인된 시험 조건·관측 구간에서 물리 셀마다 동일한 비중으로 계산합니다.", wraplength=1000).pack(anchor="w", pady=8)
        for text, command in (("설계 비교 DB 갱신 (파생 사이클·지표·관계망)", self.build_research_db),
                              ("설계 관계망 열기", lambda: self.open_path(self.project_root() / "db/network.html")),
                              ("설계 확정표 열기 (design_map.csv)", lambda: self.open_path(self.project_root() / "db/design_map.csv")),
                              ("현재 Parquet DB로 비교 작업실 열기", self.open_library_workbench),
                              ("지표 사전·중복 정리 결과", self.open_catalog),
                              ("선택 데이터에 기존 AIM/외부 지표 연결", self.import_features),
                              ("기존 raw DB / 분석 설정의 작업실 열기", self.open_legacy),
                              ("기존 원본 분석 설정 실행", self.run_pipeline),
                              ("통합 사용 안내", lambda: self.open_path(Path(__file__).parent / "resources/통합사용안내.html"))):
            self.button(frame, text, command).pack(anchor="w", pady=7)
        ttk.Label(frame, text="CYC의 원본 단위를 확인하면 전압·용량 프로파일 지표를 계산합니다. 전류·CC/CV 정보가 필요한 지표는 해당 정보를 포함한 기존 분석 설정이나 검증된 외부 지표로 연결합니다. 공식 AIM 정의가 없는 항목은 미설정 상태로 남습니다.", wraplength=1000).pack(anchor="w", pady=16)

    def project_root(self):
        return Path(self.project.get()).expanduser().resolve()

    def choose_project(self):
        path = filedialog.askdirectory(parent=self.root, title="BatteryInsight 프로젝트 폴더 선택")
        if path:
            self.project.set(path)
            self.reload()
            self.reload_documents()

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            raise ValueError("먼저 데이터·실험 분류 탭에서 파일을 선택하세요.")
        return library.get_dataset(self.project_root(), selection[0])

    def task(self, title, function, after=None):
        if self.busy:
            return
        self.busy = True
        self.status.set(title)
        for control in self.controls:
            control.configure(state="disabled")
        self.conversion_queue.set_busy(True)
        def run():
            try:
                result = function()
                self.events.put(("done", (title, result, after)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        self.worker = threading.Thread(target=run, daemon=False, name="battery-insight-task")
        self.worker.start()

    def progress(self, event):
        self.events.put(("progress", event))

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                if isinstance(value, dict):
                    self.conversion_queue.update_event(value)
                    value = value.get("message") or json.dumps(value, ensure_ascii=False)
                self.status.set(str(value))
                self.write_log(value)
                continue
            self.busy = False
            for control in self.controls:
                control.configure(state="normal")
            self.conversion_queue.set_busy(False)
            if kind == "error":
                self.status.set("작업 오류: " + value)
                self.write_log(value)
                messagebox.showerror("BatteryInsight", value, parent=self.root)
            else:
                title, result, after = value
                partial = isinstance(result, dict) and result.get("status") in {"failed", "partial_failure"}
                partial = partial or isinstance(result, list) and any(isinstance(item, dict) and item.get("status") == "failed" for item in result)
                self.status.set(title + (" · 일부 실패 — 실행 기록을 확인하세요." if partial else " · 완료"))
                self.write_log({"task": title, "result": result})
                self.reload()
                if partial:
                    messagebox.showwarning("일부 작업 실패", "실패 항목과 사유는 실행 기록 탭에서 확인하세요.", parent=self.root)
                if after:
                    after(result)
        self.poll_id = self.root.after(100, self.poll)

    def write_log(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        self.log.configure(state="normal")
        self.log.insert("end", text[:20000] + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def reload(self):
        self.rows = library.library_rows(self.project_root())
        self.render_rows()

    def render_rows(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        query = self.filter.get().strip().casefold()
        for row in self.rows:
            if query and query not in json.dumps(row, ensure_ascii=False).casefold():
                continue
            meta = row["metadata"]
            values = (row["review_status"], meta.get("sample_name", ""), meta.get("test_type", ""), meta.get("design_id", ""), meta.get("batch_id", ""), f"{row['rows']:,}", row["relative_path"])
            self.tree.insert("", "end", iid=row["dataset_id"], values=values)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected)

    def convert(self):
        self.conversion_queue.choose_one()

    def start_conversion(self):
        if self.busy:
            return
        sources = tuple(self.conversion_queue.sources)
        if not sources:
            messagebox.showinfo("변환 대기 목록", "먼저 변환할 폴더를 추가하세요.", parent=self.root)
            return
        project = self.project_root()
        if any(project == source or project.is_relative_to(source) for source in sources):
            messagebox.showerror("저장 위치 확인", "프로젝트 폴더는 모든 원본 폴더 바깥에 지정하세요.", parent=self.root)
            return
        def convert(source, *, progress):
            return library.convert_and_register(project, source, progress=progress)
        self.task("CYC 폴더 순차 변환·DB 등록", lambda: run_folders(sources, convert, progress=self.progress))

    def scan(self):
        source = filedialog.askdirectory(parent=self.root, title="변환된 Parquet 폴더 선택")
        if source:
            project = self.project_root()
            self.task("Parquet 등록", lambda: library.scan_folder(project, source, progress=self.progress))

    def refresh_all(self):
        project = self.project_root()
        self.task("Parquet 변경 반영·분류 규칙 적용", lambda: library.refresh_project(project, progress=self.progress, convert_sources=False))

    def diagnose_conversions(self):
        project = self.project_root()
        self.task("CYC 변환 기록 진단", lambda: library.diagnose_conversions(project),
                  after=lambda result: self.open_path(result["outputs"]["html"]))

    def watch(self):
        if self.auto.get() and not self.busy:
            self.refresh_all()
        self.watch_id = self.root.after(30000, self.watch)

    def show_selected(self):
        try:
            row = self.selected()
            self.text_dialog("등록 정보·미확인 후보", json.dumps(row, ensure_ascii=False, indent=2))
        except Exception as exc:
            messagebox.showerror("파일 선택", str(exc), parent=self.root)

    def text_dialog(self, title, text, save=None):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("850x650")
        box = tk.Text(window, wrap="word", font=("맑은 고딕", 10))
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", text)
        if save:
            def commit():
                try:
                    result = save(box.get("1.0", "end-1c"))
                    self.write_log(result)
                    window.destroy()
                    self.reload()
                    self.reload_documents()
                except Exception as exc:
                    messagebox.showerror("확인", str(exc), parent=window)
            ttk.Button(window, text="내용을 확인하고 저장", command=commit).pack(pady=(0, 12))
        return window

    def edit_metadata(self):
        try:
            row = self.selected()
        except Exception as exc:
            messagebox.showerror("파일 선택", str(exc), parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("실험·설계·단위 확인")
        window.geometry("780x730")
        ttk.Label(window, text=row["relative_path"], wraplength=730).pack(anchor="w", padx=16, pady=8)
        ttk.Label(window, text="파일명 후보(자동 확정 아님): " + json.dumps(row["hints"], ensure_ascii=False), wraplength=730).pack(anchor="w", padx=16)
        body = ttk.Frame(window)
        body.pack(fill="both", expand=True, padx=16, pady=8)
        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        form = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        variables = {}
        fields = FIRST_FIELDS + sorted(library.FIELDS - set(FIRST_FIELDS) - {"include"})
        choices = {"test_type": sorted(library.TEST_TYPES), "voltage_unit": ["", "V", "mV"], "capacity_unit": ["", "Ah", "mAh", "uAh"], "date_kind": ["", "test_start", "test_end", "manufacture", "report", "unknown"]}
        for index, field in enumerate(fields):
            ttk.Label(form, text=LABELS.get(field, field), width=43).grid(row=index, column=0, sticky="w", pady=3)
            variable = tk.StringVar(value="" if row["metadata"].get(field) is None else str(row["metadata"][field]))
            variables[field] = variable
            widget = ttk.Combobox(form, textvariable=variable, values=choices[field], state="readonly", width=35) if field in choices else ttk.Entry(form, textvariable=variable, width=38)
            widget.grid(row=index, column=1, sticky="ew", pady=3)
        confirmed = tk.BooleanVar(value=False)
        ttk.Checkbutton(window, text="선택 파일의 정보와 단위 근거를 확인했습니다.", variable=confirmed).pack(anchor="w", padx=16)
        def save():
            if not confirmed.get():
                messagebox.showinfo("근거 확인", "정보를 확인한 뒤 체크해 주세요.", parent=window)
                return
            try:
                metadata = {key: variable.get().strip() for key, variable in variables.items()}
                library.assign_metadata(self.project_root(), row["dataset_id"], metadata, expected_sha256=row["sha256"])
                window.destroy()
                self.reload()
            except Exception as exc:
                messagebox.showerror("저장", str(exc), parent=window)
        ttk.Button(window, text="확인한 정보 저장", command=save).pack(pady=12)

    def export_registration(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", initialfile="experiment_registry.csv")
        if path:
            try:
                library.export_registration(self.project_root(), path)
                self.status.set("등록표 저장: " + path)
            except Exception as exc:
                messagebox.showerror("등록표", str(exc), parent=self.root)

    def import_registration(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("등록표", "*.csv *.xlsx *.xlsm")])
        if path:
            self.task("확인된 등록표 적용", lambda: library.import_registration(self.project_root(), path))

    def rules_dialog(self):
        template = [{"rule_id": "reviewed_cycle_folder", "path_glob": "Cycle/*", "metadata": {"test_type": "Cycle"}}]
        self.text_dialog("확인한 폴더 규칙 · 물리 셀/샘플은 복제하지 않음", json.dumps(template, ensure_ascii=False, indent=2),
            lambda text: {"rules": library.set_rules(self.project_root(), json.loads(text)), "result": library.apply_rules(self.project_root())})

    def add_documents(self):
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=[("장표", "*.pptx *.pdf *.xlsx *.xlsm")])
        if paths:
            self.task("장표 근거 등록", lambda: library.import_documents(self.project_root(), paths), after=lambda result: self.reload_documents())

    def open_traceability(self):
        from .traceability_ui import TraceWindow
        selected = self.tree.selection()
        TraceWindow(self, selected[0] if selected else None)

    def reload_documents(self):
        from .knowledge_registry import KnowledgeRegistry
        path = self.project_root() / "knowledge/knowledge.sqlite3"
        self.doc_tree.delete(*self.doc_tree.get_children())
        self.evidence_rows = {}
        if path.exists():
            with KnowledgeRegistry(path, read_only=True) as registry:
                evidence = registry.list_evidence()
            for row in evidence:
                self.evidence_rows[row["evidence_id"]] = row
                text = json.dumps(row, ensure_ascii=False)
                self.doc_tree.insert("", "end", iid=row["evidence_id"], values=(row.get("document_id"), row.get("locator"), text[:500]))

    def show_evidence(self):
        selected = self.doc_tree.selection()
        if selected:
            self.text_dialog("장표 근거 · 문서 내용은 데이터로만 취급", json.dumps(self.evidence_rows[selected[0]], ensure_ascii=False, indent=2))

    def record_dialog(self):
        from .knowledge_registry import KnowledgeRegistry
        from .knowledge_report import write_review
        selected = self.doc_tree.selection()
        if not selected:
            messagebox.showinfo("장표 근거", "장표를 추가하고 근거 하나를 선택하세요.", parent=self.root)
            return
        template = {"status": "draft", "sample_id": "", "sample_name": "", "experiment_name": "", "test_date": "",
            "date_kind": "test_start", "namespace": "", "set_no": "", "test_type": "Unknown", "channel_id": "",
            "design_id": "", "batch_id": "", "metadata": {}, "evidence_ids": list(selected), "notes": ""}
        def save(text):
            record = json.loads(text)
            with KnowledgeRegistry(self.project_root() / "knowledge/knowledge.sqlite3") as registry:
                result = registry.upsert_records([record], reviewer=os.environ.get("USERNAME", "local_user"), reason="장표 근거 직접 확인")
                write_review(registry.snapshot(), self.project_root() / "knowledge/review")
            return result
        self.text_dialog("장표 실험 기록 · 확인 후 status를 confirmed로 지정", json.dumps(template, ensure_ascii=False, indent=2), save)

    def link_dialog(self):
        from .knowledge_registry import KnowledgeRegistry
        try:
            row = self.selected()
            path = self.project_root() / "knowledge/knowledge.sqlite3"
            with KnowledgeRegistry(path, read_only=True) as registry:
                records = registry.list_records("confirmed")
            candidates = {record["record_id"] for record in library.knowledge_candidates(self.project_root(), row["dataset_id"])}
            window = tk.Toplevel(self.root)
            window.title("확인된 장표 기록 연결")
            ttk.Label(window, text="선택 데이터: " + row["relative_path"], wraplength=800).pack(padx=16, pady=10)
            labels = {record["record_id"] + " | " + record["sample_name"] + (" | 키 일치 후보" if record["record_id"] in candidates else " | 직접 확인 필요"): record["record_id"] for record in records}
            selection = tk.StringVar()
            box = ttk.Combobox(window, textvariable=selection, values=list(labels), state="readonly", width=100)
            box.pack(padx=16, pady=10)
            def save():
                try:
                    library.link_record(self.project_root(), row["dataset_id"], labels[selection.get()])
                    window.destroy()
                    self.reload()
                except Exception as exc:
                    messagebox.showerror("연결", str(exc), parent=window)
            ttk.Button(window, text="선택 기록의 대응을 확인하고 연결", command=save).pack(pady=12)
            def unlink():
                library.unlink_record(self.project_root(), row["dataset_id"])
                window.destroy()
                self.reload()
            ttk.Button(window, text="기존 연결 해제 · 실험 정보 재확인", command=unlink).pack(pady=(0, 12))
        except Exception as exc:
            messagebox.showerror("장표 연결", str(exc), parent=self.root)

    def start_workbench(self, config, output):
        from .workbench_server import Workbench, make_server
        key = str(Path(config).resolve())
        if key not in self.servers:
            controller = Workbench(config, output)
            server = make_server(controller, port=0)
            threading.Thread(target=server.serve_forever, daemon=True, name="battery-insight-local-http").start()
            self.servers[key] = (server, controller)
        else:
            self.servers[key][1].refresh()
        return "http://127.0.0.1:" + str(self.servers[key][0].server_port) + "/"

    def open_library_workbench(self):
        from .library_workbench import workbench_config
        project = self.project_root()
        self.task("Parquet 비교 작업실", lambda: self.start_workbench(workbench_config(project), project / "workbench"), after=lambda address: webbrowser.open(address))

    def build_research_db(self):
        from .db_build import build_database
        project = self.project_root()
        self.task("설계 비교 DB 갱신", lambda: build_database(project, progress=self.progress),
                  after=lambda result: self.open_path(project / "db/network.html"))

    def open_catalog(self):
        root = Path(__file__).parent / "resources/catalog"
        self.open_path(root / "metrics_catalog.html")

    def import_features(self):
        try:
            row = self.selected()
        except Exception as exc:
            messagebox.showerror("파일 선택", str(exc), parent=self.root)
            return
        path = filedialog.askopenfilename(parent=self.root, title="검증된 외부 지표 매핑 JSON", filetypes=[("JSON", "*.json")])
        if path:
            self.task("AIM/외부 지표 연결", lambda: library.import_feature_export(self.project_root(), row["dataset_id"], path))

    def open_legacy(self):
        path = filedialog.askopenfilename(parent=self.root, title="기존 비교 작업실 설정 JSON", filetypes=[("JSON", "*.json")])
        if path:
            output = self.project_root() / "legacy_workbenches" / library.identity(str(Path(path).resolve()))[:16]
            self.task("기존 비교 작업실", lambda: self.start_workbench(path, output), after=lambda address: webbrowser.open(address))

    def run_pipeline(self):
        path = filedialog.askopenfilename(parent=self.root, title="기존 원본 분석 설정 JSON", filetypes=[("JSON", "*.json")])
        if path:
            from .pipeline import run_pipeline
            output = self.project_root() / "analysis_lake"
            self.task("원본 지표·이상 분석", lambda: str(run_pipeline(path, output)))

    def open_path(self, path):
        path = Path(path).resolve()
        if not path.exists():
            messagebox.showinfo("경로", "아직 생성되지 않았습니다: " + str(path), parent=self.root)
        elif os.name == "nt":
            os.startfile(path)
        else:
            webbrowser.open(path.as_uri())

    def close(self):
        if self.busy:
            messagebox.showinfo("작업 중", "현재 작업이 끝난 후 닫아 주세요.", parent=self.root)
            return
        for server, controller in self.servers.values():
            server.shutdown()
            server.server_close()
        self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.watch_id)
        self.root.destroy()


def launch(project=None):
    root = tk.Tk()
    BatteryInsightApp(root, project)
    root.mainloop()
    return 0


def check_gui(project):
    from .traceability_ui import TraceWindow
    from .copilot_ui import CopilotWindow
    root = tk.Tk()
    root.withdraw()
    app = BatteryInsightApp(root, project)
    try:
        root.update_idletasks()
        checks = {"hidden": root.state() == "withdrawn", "four_integrated_tabs": len(app.book.tabs()) == 4,
                  "no_llm_configuration": True, "idle": not app.busy, "project_selected": app.project_root() == Path(project).resolve(),
                  "queue_initially_empty": not app.conversion_queue.sources,
                  "explicit_start_button": app.conversion_queue.start_button.cget("text") == "시작"}
        checks["traceability_window_module_available"] = callable(TraceWindow)
        checks["company_copilot_window_module_available"] = callable(CopilotWindow)
        return {"status": "success" if all(checks.values()) else "failed", "checks": checks}
    finally:
        app.close()
