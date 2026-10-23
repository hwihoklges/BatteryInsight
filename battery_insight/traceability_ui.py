"""Native evidence review forms and manufacturing/test linkage workbench."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import library
from . import traceability as trace
from .knowledge_registry import KnowledgeRegistry, TEXT_FIELDS, NUMERIC_METADATA_FIELDS

FIELDS = {"entity_id": "이력 ID", "kind": "항목 종류", "name": "항목명", "process_type": "공정 종류", "performed_on": "공정·제조일 YYYY-MM-DD",
          "status": "검토 상태", "sample_id": "샘플 ID", "sample_name": "샘플명", "physical_cell_id": "물리 셀 ID",
          "experiment_name": "실험·시험명", "test_date": "시험 날짜 YYYY-MM-DD", "date_kind": "날짜 의미",
          "manufacture_date": "제조일 YYYY-MM-DD", "report_date": "보고일 YYYY-MM-DD", "test_end_date": "시험 종료일 YYYY-MM-DD",
          "set_no": "세트번호 (문자열)", "namespace": "번호 사용 범위·프로젝트", "test_type": "시험 종류", "rpt_stage": "RPT 시점",
          "channel_id": "채널 ID", "design_id": "설계 ID", "batch_id": "제조 배치", "record_id": "시험 기록 ID"}
ATTR_LABELS = {"physical_cell_id": "물리 셀 ID", "lot_id": "로트 ID", "sample_id": "샘플 ID", "design_id": "설계 ID", "batch_id": "제조 배치",
    "material": "소재명", "supplier": "공급사", "process_run_id": "공정 실행 ID", "electrode_side": "양극·음극 구분",
    "fabrication_method": "습식·건식", "composition_basis": "조성 기준", "current_collector": "집전체", "equipment_id": "설비 ID", "operator": "작업자"}
PARAM_LABELS = {"pressing_pressure": "압착압", "mixing_time": "배합시간", "mixing_speed": "배합속도", "drying_temperature": "건조온도",
    "drying_time": "건조시간", "mass_loading": "로딩", "active_fraction": "활물질 비율", "film_thickness": "막두께", "np_ratio": "NP 비"}
META_LABELS = {"pressure_mpa": "구동압(MPa)", "temperature_c": "시험온도(°C)", "protocol": "프로토콜", "chemistry": "전지 계열",
    "cathode_active_material": "양극 활물질", "anode_active_material": "음극 활물질",
    "cathode_active_mass_loading_mg_cm2": "양극 활물질 로딩(mg/cm²)", "anode_active_mass_loading_mg_cm2": "음극 활물질 로딩(mg/cm²)"}


def _pairs(box, labels):
    reverse = {value: key for key, value in labels.items()}
    result = {}
    for line in box.get("1.0", "end-1c").splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("한 줄에 항목 = 값 형식으로 입력하세요.")
        key, value = (part.strip() for part in line.split("=", 1))
        if value:
            key = reverse.get(key, key)
            if key in result:
                raise ValueError("같은 항목을 두 번 입력했습니다: " + key)
            result[key] = value
    return result


def retained_evidence(old, new, target):
    """Keep precise header/value references when a reviewed value is unchanged."""
    values, previous = {}, {}
    if target == "entity":
        for key in ("name", "process_type", "performed_on"):
            if new.get(key):
                values[key], previous[key] = new[key], old.get(key)
        for section in ("attributes", "parameters"):
            for key, value in new.get(section, {}).items():
                values[section + "." + key] = value
                previous[section + "." + key] = old.get(section, {}).get(key)
    else:
        for key in TEXT_FIELDS:
            if new.get(key):
                values[key], previous[key] = new[key], old.get(key)
        for key, value in new.get("metadata", {}).items():
            values[key], previous[key] = value, old.get("metadata", {}).get(key)
    result = {}
    for key, value in values.items():
        ids = old.get("field_evidence", {}).get(key, [])
        result[key] = ids if value == previous.get(key) and ids and set(ids) <= set(new["evidence_ids"]) else new["evidence_ids"]
    return result


class TraceWindow:
    def __init__(self, app, dataset_id=None):
        self.app, self.project = app, app.project_root()
        self.window = tk.Toplevel(app.root)
        self.window.title("PPT · 공정부터 raw까지 추적 — " + str(self.project))
        self.window.geometry("1120x" + str(min(760, self.window.winfo_screenheight() - 100)))
        self.controls, self.snap = [], {}
        self.by_proposal, self.by_entity, self.by_record, self.by_edge, self.dataset_labels = ({ } for _ in range(5))
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="PPT 근거 → 소재·공정·셀 → 시험 → raw/Parquet", font=("맑은 고딕", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="이 프로젝트: " + str(self.project), wraplength=1050).pack(anchor="w", pady=7)
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        for text, command in (("PPT 정보 후보 추출", self.extract), ("소재·공정·셀 추가", lambda: self.editor("entity", {})),
                              ("시험 기록 추가", lambda: self.editor("record", {})), ("이력 연결 추가", self.edge_editor),
                              ("추적 지도 열기", self.open_graph), ("새로고침", self.refresh)):
            self.button(bar, text, command).pack(side="left", padx=(0, 5))
        advanced = ttk.Frame(outer)
        advanced.pack(fill="x", pady=7)
        for text, command in (("등록 묶음 JSON 가져오기", self.import_bundle), ("이력 JSON 내보내기", self.export),
                              ("선택 항목 수정 이력", self.history), ("합성 예제를 새 프로젝트로 만들기", self.demo)):
            self.button(advanced, text, command).pack(side="left", padx=(0, 5))
        self.button(advanced, "회사 Copilot CLI", self.open_copilot).pack(side="left", padx=(0, 5))
        self.book = ttk.Notebook(outer)
        self.book.pack(fill="both", expand=True)
        self.proposal_tab, self.entity_tab, self.raw_tab = (ttk.Frame(self.book, padding=10) for _ in range(3))
        for frame, title in ((self.proposal_tab, "PPT 검토 대기"), (self.entity_tab, "공정·셀 이력"), (self.raw_tab, "시험과 raw 대응")):
            self.book.add(frame, text=title)
        self.proposals = self.table(self.proposal_tab, [("state", "상태", 90), ("target", "종류", 90), ("location", "슬라이드·표", 220), ("title", "확인할 항목", 500)])
        pbar = ttk.Frame(self.proposal_tab)
        pbar.pack(fill="x", pady=8)
        self.button(pbar, "선택 후보의 값·근거 검토", self.edit_proposal).pack(side="left")
        self.button(pbar, "선택 후보 제외", self.dismiss).pack(side="left", padx=8)
        self.proposals.bind("<Double-1>", lambda event: self.edit_proposal())
        self.entities = self.table(self.entity_tab, [("kind", "종류", 120), ("status", "상태", 95), ("date", "공정·제조일", 110), ("name", "항목명·식별자", 600)])
        self.entities.bind("<Double-1>", lambda event: self.edit_entity())
        self.edges = self.table(self.entity_tab, [("status", "상태", 95), ("relation", "연결", 130), ("source", "시작", 300), ("target", "다음", 300)], height=5)
        self.edges.bind("<Double-1>", lambda event: self.edit_edge())
        self.edges.bind("<<TreeviewSelect>>", lambda event: self.entities.selection_remove(self.entities.selection()) if self.edges.selection() else None)
        self.entities.bind("<<TreeviewSelect>>", lambda event: self.edges.selection_remove(self.edges.selection()) if self.entities.selection() else None)
        self.records = self.table(self.raw_tab, [("status", "상태", 90), ("sample", "샘플·물리 셀", 220), ("name", "시험", 240), ("date", "시험일", 110), ("set", "세트·범위·RPT", 300)])
        self.records.bind("<Double-1>", lambda event: self.edit_record())
        selector = ttk.Frame(self.raw_tab)
        selector.pack(fill="x", pady=8)
        ttk.Label(selector, text="대응 raw/Parquet").pack(side="left")
        self.dataset = tk.StringVar(value=dataset_id or "")
        self.raw_choice = ttk.Combobox(selector, state="readonly", width=70)
        self.raw_choice.pack(side="left", fill="x", expand=True, padx=8)
        self.button(selector, "선택 시험과 대응 확인·연결", self.link_raw).pack(side="left")
        self.raw_summary = tk.StringVar()
        self.raw_choice.bind("<<ComboboxSelected>>", lambda event: self.show_raw_summary())
        ttk.Label(self.raw_tab, textvariable=self.raw_summary, wraplength=1000).pack(anchor="w", pady=5)
        ttk.Label(self.raw_tab, text="샘플명이 같아도 다른 물리 셀일 수 있습니다. 날짜·세트·채널·RPT 시점과 장표 근거를 확인한 뒤 연결하세요.").pack(anchor="w")
        self.refresh()

    def button(self, parent, text, command):
        widget = ttk.Button(parent, text=text, command=command)
        self.controls.append(widget)
        return widget

    @staticmethod
    def table(parent, columns, height=10):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True, pady=5)
        tree = ttk.Treeview(container, columns=[item[0] for item in columns], show="headings", height=height, selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key == columns[-1][0])
        tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(container, command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def perform(self, title, function, after=None):
        if self.app.busy:
            messagebox.showinfo("작업 중", "현재 작업이 끝나면 실행하세요.", parent=self.window)
            return
        for control in self.controls:
            if control.winfo_exists():
                control.configure(state="disabled")
        def run():
            try:
                return {"value": function()}
            except Exception as exc:
                return {"trace_error": str(exc)}
        def done(result):
            if not self.window.winfo_exists():
                return
            for control in self.controls:
                if control.winfo_exists():
                    control.configure(state="normal")
            if "trace_error" in result:
                self.app.status.set(title + " · 오류")
                messagebox.showerror(title, result["trace_error"], parent=self.window)
            elif after:
                after(result["value"])
        self.app.task(title, run, after=done)

    def refresh(self):
        self.perform("공정·시험 이력 읽기", lambda: trace.snapshot(self.project), after=self.render)

    def render(self, snap):
        self.snap = snap
        for tree in (self.proposals, self.entities, self.edges, self.records):
            tree.delete(*tree.get_children())
        self.by_proposal = {row["proposal_id"]: row for row in snap["proposals"]}
        self.by_entity = {row["entity_id"]: row for row in snap["entities"]}
        self.by_record = {row["record_id"]: row for row in snap["records"]}
        self.by_edge = {row["edge_id"]: row for row in snap["edges"]}
        for row in snap["proposals"]:
            self.proposals.insert("", "end", iid=row["proposal_id"], values=(row["state"], "시험" if row["target"] == "record" else "공정·소재·셀", row["location"], row["title"]))
        for row in snap["entities"]:
            self.entities.insert("", "end", iid=row["entity_id"], values=(trace.KINDS[row["kind"]], row["effective_status"], row["performed_on"], row["name"]))
        for row in snap["edges"]:
            source = self.by_entity.get(row["source_id"], {})
            target = (self.by_entity if row["target_kind"] == "entity" else self.by_record).get(row["target_id"], {})
            self.edges.insert("", "end", iid=row["edge_id"], values=(row["effective_status"], trace.RELATIONS[row["relation"]], source.get("name", row["source_id"]), target.get("name") or target.get("experiment_name") or row["target_id"]))
        for row in snap["records"]:
            self.records.insert("", "end", iid=row["record_id"], values=(row["status"], (row.get("sample_name") or "") + " / " + (row.get("physical_cell_id") or "물리 셀 미확인"), row.get("experiment_name"), row.get("test_date"), " / ".join(row.get(key) or "" for key in ("set_no", "namespace", "rpt_stage"))))
        self.dataset_labels = {}
        for row in snap["datasets"]:
            path = Path(row["path"])
            display = str(path.relative_to(self.project)) if path.is_relative_to(self.project) else str(path)
            self.dataset_labels[display + " [" + row["dataset_id"][-8:] + "]"] = row["dataset_id"]
        self.raw_choice.configure(values=list(self.dataset_labels))
        selected = next((key for key, value in self.dataset_labels.items() if value == self.dataset.get()), None)
        if selected:
            self.raw_choice.set(selected)
            self.show_raw_summary()

    def show_raw_summary(self):
        key = self.dataset_labels.get(self.raw_choice.get())
        row = next((item for item in self.snap.get("datasets", []) if item["dataset_id"] == key), None)
        if row:
            metadata = row["metadata"] if row["metadata_current"] else {}
            known = " / ".join(str(metadata.get(field) or "미확인") for field in ("sample_name", "physical_cell_id", "test_date", "set_no", "rpt_stage"))
            hints = row.get("hints", {})
            self.raw_summary.set(f"{row['rows']:,}행 · 확인 정보: {known}\n파일명 후보(미확인): 날짜 {hints.get('date_candidates', [])}, 세트 {hints.get('set_candidates', [])}, 채널 {hints.get('channel_candidates', [])}")

    def extract(self):
        from .slide_candidates import extract_proposals
        self.perform("PPT의 명시된 표·항목을 검토 대기로 추출", lambda: extract_proposals(self.project), after=lambda result: self.refresh())

    def open_copilot(self):
        from .copilot_ui import CopilotWindow
        try:
            CopilotWindow(self)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Copilot 설정 읽기", str(exc), parent=self.window)

    @staticmethod
    def selected(tree, mapping):
        values = tree.selection()
        return mapping.get(values[0]) if values else None

    def edit_proposal(self):
        item = self.selected(self.proposals, self.by_proposal)
        if item:
            payload = item["payload"]
            current = (self.by_entity if item["target"] == "entity" else self.by_record).get(payload.get("entity_id") or payload.get("record_id"))
            self.editor(item["target"], current or payload, proposal=item)

    def dismiss(self):
        from .slide_candidates import review_proposal
        item = self.selected(self.proposals, self.by_proposal)
        if item:
            self.perform("PPT 후보 제외", lambda: review_proposal(self.project, item["proposal_id"], action="dismissed"), after=lambda result: self.refresh())

    def edit_entity(self):
        row = self.selected(self.entities, self.by_entity)
        if row:
            self.editor("entity", row)

    def edit_record(self):
        row = self.selected(self.records, self.by_record)
        if row:
            self.editor("record", row)

    def editor(self, target, payload, proposal=None):
        if self.app.busy:
            return
        window = tk.Toplevel(self.window)
        window.title("PPT 원문과 값을 확인하고 등록")
        window.geometry("940x" + str(min(840, window.winfo_screenheight() - 100)))
        body = ttk.Frame(window)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        form = ttk.Frame(canvas, padding=8)
        item_id = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item_id, width=event.width))
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        if proposal:
            ttk.Label(form, text="\n".join(proposal["warnings"]) or "표의 값과 근거를 대조하세요.", wraplength=850).pack(anchor="w", pady=7)
            ttk.Label(form, text="장표 원문 값 · 자동 입력하지 않은 항목도 확인하세요.").pack(anchor="w")
            original = tk.Text(form, height=5, wrap="word", font=("맑은 고딕", 10))
            original.pack(fill="x", pady=5)
            original.insert("1.0", "\n".join(row["header"] + " = " + row["value"] for row in proposal["raw_values"]))
            original.configure(state="disabled")
        entries = {}
        keys = ("entity_id", "kind", "name", "process_type", "performed_on", "status") if target == "entity" else ("record_id", *TEXT_FIELDS, "status")
        for key in keys:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=FIELDS.get(key, key), width=30).pack(side="left")
            variable = tk.StringVar(value=payload.get(key) or ("draft" if key == "status" else "process_run" if key == "kind" else "unknown" if key == "date_kind" else ""))
            choices = list(trace.KINDS) if key == "kind" else ["draft", "confirmed", "retired"] if key == "status" and target == "entity" else ["draft", "confirmed"] if key == "status" else ["unknown", "test_start", "test_end", "manufacture", "report"] if key == "date_kind" else None
            widget = ttk.Combobox(row, textvariable=variable, values=choices, state="readonly") if choices else ttk.Entry(row, textvariable=variable)
            widget.pack(side="left", fill="x", expand=True)
            if key in {"entity_id", "record_id"} and payload.get(key):
                widget.configure(state="readonly")
            if key == "kind" and payload.get("revision"):
                widget.configure(state="disabled")
            entries[key] = variable
        def area(title, text, height=5):
            ttk.Label(form, text=title, wraplength=850).pack(anchor="w", pady=(12, 3))
            box = tk.Text(form, height=height, wrap="word", font=("맑은 고딕", 10))
            box.pack(fill="x")
            box.insert("1.0", text)
            return box
        if target == "entity":
            values = payload.get("attributes") or {key: "" for key in ("lot_id", "physical_cell_id", "design_id", "batch_id", "material", "supplier", "fabrication_method", "current_collector", "composition_basis")}
            attrs = area("식별·문자 정보: 한 줄에 항목 = 값. ID의 선행 0을 유지합니다.", "\n".join(ATTR_LABELS.get(key, key) + " = " + value for key, value in values.items()), 7)
            params = area("공정 변수: 한 줄에 항목 = 값 | 단위. 예: 압착압 = 100 | MPa. 단위·조성 분모·로딩 면 기준을 확인하세요.", "\n".join(PARAM_LABELS.get(key, key) + " = " + str(value["value"]) + " | " + value["unit"] for key, value in payload.get("parameters", {}).items()))
        else:
            attrs = area("시험·전극 조건: 한 줄에 항목 = 값. 이름에 적힌 단위를 확인하세요.", "\n".join(META_LABELS.get(key, key) + " = " + str(value) for key, value in payload.get("metadata", {}).items() if value is not None))
            params = None
        notes = area("메모", payload.get("notes") or "", 3)
        ttk.Label(form, text="이 항목의 근거를 복수 선택하세요. 아래에 원문과 슬라이드 위치를 함께 표시합니다.").pack(anchor="w", pady=(14, 5))
        proofs = ttk.Treeview(form, columns=("location", "text"), show="headings", selectmode="extended", height=8)
        proofs.heading("location", text="장표·슬라이드/표")
        proofs.heading("text", text="원문")
        proofs.column("location", width=230)
        proofs.column("text", width=600)
        proofs.pack(fill="x")
        doc_names = {item["document_id"]: item["name"] for item in self.snap.get("documents", [])}
        for proof in self.snap.get("evidence", []):
            proofs.insert("", "end", iid=proof["evidence_id"], values=(doc_names.get(proof["document_id"], "") + " / " + proof["locator"], proof["text"][:600] or "이미지·도표 직접 확인 필요"))
        proofs.selection_set([key for key in payload.get("evidence_ids", []) if proofs.exists(key)])
        def open_proof():
            selected = proofs.selection()
            if selected:
                evidence = next(row for row in self.snap["evidence"] if row["evidence_id"] == selected[0])
                document = next(row for row in self.snap["documents"] if row["document_id"] == evidence["document_id"])
                if document["proof"]["valid"]:
                    self.app.open_path(document["proof"]["path"])
                else:
                    messagebox.showerror("근거 확인", "장표 사본이 없거나 변경됐습니다.", parent=window)
        ttk.Button(form, text="선택 근거의 보관 장표 열기", command=open_proof).pack(anchor="w", pady=5)
        reason = area("검토 이유 (확정·수정 이력에 남습니다)", "PPT 원문과 값·대응 관계 확인", 2)
        def save():
            try:
                item = {key: variable.get().strip() for key, variable in entries.items() if variable.get().strip()}
                item.update(evidence_ids=list(proofs.selection()), notes=notes.get("1.0", "end-1c"), field_evidence={})
                for key in ("revision", "content_hash"):
                    if key in payload:
                        item[key] = payload[key]
                if target == "entity":
                    item["attributes"] = _pairs(attrs, ATTR_LABELS)
                    item["parameters"] = {}
                    for key, raw in _pairs(params, PARAM_LABELS).items():
                        parts = raw.split("|", 1)
                        value = parts[0].strip()
                        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", value):
                            value = float(value)
                        item["parameters"][key] = {"value": value, "unit": parts[1].strip() if len(parts) > 1 else ""}
                else:
                    item["metadata"] = {key: float(value) if key in NUMERIC_METADATA_FIELDS else value for key, value in _pairs(attrs, META_LABELS).items()}
                    item.pop("sample_aliases", None)
                item["field_evidence"] = retained_evidence(payload, item, target)
                explanation = reason.get("1.0", "end-1c").strip()
                def commit():
                    if proposal:
                        from .slide_candidates import review_proposal
                        return review_proposal(self.project, proposal["proposal_id"], item, reason=explanation)
                    if target == "entity":
                        return trace.save_bundle(self.project, {"entities": [item]}, reason=explanation)
                    with KnowledgeRegistry(self.project / "knowledge/knowledge.sqlite3") as registry:
                        return registry.upsert_records([item], reviewer="local_user", reason=explanation)
                self.perform("검토 항목 저장", commit, after=lambda result: (window.destroy(), self.refresh()))
            except Exception as exc:
                messagebox.showerror("입력 확인", str(exc), parent=window)
        ttk.Button(window, text="값·근거·상태를 확인하고 저장", command=save).pack(pady=12)
        return window

    def edge_editor(self, existing=None):
        if self.app.busy:
            return
        item = existing or {}
        window = tk.Toplevel(self.window)
        window.title("공정·셀·시험의 연결 근거 확인")
        window.geometry("940x620")
        form = ttk.Frame(window, padding=16)
        form.pack(fill="both", expand=True)
        ttk.Label(form, text="장표에서 확인한 투입·산출·셀·시험 대응만 연결합니다.\n공정 이름으로 시험을 자동 분리하지 않습니다. 셀 ID와 날짜는 일반적인 연결 오류를 확인하는 데 사용합니다.", wraplength=880).pack(anchor="w", pady=8)
        sources = {"[" + row["entity_id"] + "] " + row["name"]: row["entity_id"] for row in self.snap["entities"]}
        targets = {key: ("entity", value) for key, value in sources.items()}
        targets.update({"[시험 " + row["record_id"] + "] " + (row.get("experiment_name") or ""): ("record", row["record_id"]) for row in self.snap["records"]})
        variables = {}
        for key, title, values in (("source", "시작 항목", list(sources)), ("target", "다음 항목", list(targets)),
                                   ("relation", "연결 의미", list(trace.RELATIONS.values())), ("status", "검토 상태", ["draft", "confirmed", "retired"])):
            ttk.Label(form, text=title).pack(anchor="w", pady=(7, 2))
            var = tk.StringVar()
            ttk.Combobox(form, textvariable=var, values=values, state="readonly", width=110).pack(fill="x")
            variables[key] = var
        variables["source"].set(next((key for key, value in sources.items() if value == item.get("source_id")), ""))
        variables["target"].set(next((key for key, value in targets.items() if value == (item.get("target_kind", "entity"), item.get("target_id"))), ""))
        variables["relation"].set(trace.RELATIONS.get(item.get("relation"), "투입"))
        variables["status"].set(item.get("status", "draft"))
        role = tk.StringVar(value=item.get("role", ""))
        ttk.Label(form, text="투입·산출 역할 (양극/음극, 전해질막 등)").pack(anchor="w", pady=(8, 2))
        ttk.Entry(form, textvariable=role).pack(fill="x")
        ttk.Label(form, text="연결 근거: Ctrl/Shift로 복수 선택").pack(anchor="w", pady=8)
        proofs = tk.Listbox(form, selectmode="extended", exportselection=False, height=6)
        proofs.pack(fill="both", expand=True)
        evidence = self.snap["evidence"]
        doc_names = {row["document_id"]: row["name"] for row in self.snap["documents"]}
        for index, row in enumerate(evidence):
            proofs.insert("end", doc_names.get(row["document_id"], "") + " / " + row["locator"] + " · " + (row["text"][:160] or "그림·도표 원본 확인"))
            if row["evidence_id"] in item.get("evidence_ids", []):
                proofs.selection_set(index)
        def save():
            try:
                target_kind, target_id = targets[variables["target"].get()]
                edge = {"source_id": sources[variables["source"].get()], "target_kind": target_kind, "target_id": target_id,
                        "relation": next(key for key, value in trace.RELATIONS.items() if value == variables["relation"].get()),
                        "status": variables["status"].get(), "role": role.get(), "evidence_ids": [evidence[index]["evidence_id"] for index in proofs.curselection()]}
                for key in ("edge_id", "revision", "content_hash"):
                    if key in item:
                        edge[key] = item[key]
                self.perform("이력 연결 저장", lambda: trace.save_bundle(self.project, {"edges": [edge]}), after=lambda result: (window.destroy(), self.refresh()))
            except Exception as exc:
                messagebox.showerror("연결 확인", str(exc), parent=window)
        ttk.Button(form, text="연결 의미와 근거를 확인하고 저장", command=save).pack(pady=12)

    def edit_edge(self):
        item = self.selected(self.edges, self.by_edge)
        if item:
            self.edge_editor(item)

    def link_raw(self):
        record = self.selected(self.records, self.by_record)
        dataset_id = self.dataset_labels.get(self.raw_choice.get())
        if not record or not dataset_id:
            messagebox.showinfo("대응 선택", "시험 기록과 raw/Parquet을 모두 선택하세요.", parent=self.window)
            return
        self.dataset.set(dataset_id)
        self.perform("PPT 시험 기록과 raw 대응 저장", lambda: library.link_record(self.project, dataset_id, record["record_id"]), after=lambda result: self.refresh())

    def open_graph(self):
        from .traceability_report import build_report
        dataset_id = self.dataset_labels.get(self.raw_choice.get())
        self.perform("공정 → raw 추적 지도", lambda: build_report(self.project, focus=dataset_id, verify=bool(dataset_id)), after=self.app.open_path)

    def history(self):
        row = self.selected(self.entities, self.by_entity)
        edge = self.selected(self.edges, self.by_edge)
        if row or edge:
            entries = trace.revisions(self.project, "entities" if row else "edges", row["entity_id"] if row else edge["edge_id"])
            self.app.text_dialog("검토·수정 이력", "\n\n".join(item["at"] + " · " + item["reviewer"] + " · " + item["reason"] + "\n" + json.dumps(item["payload"], ensure_ascii=False, indent=2) for item in entries))

    def import_bundle(self):
        path = filedialog.askopenfilename(parent=self.window, filetypes=[("검토 등록 묶음", "*.json")])
        if path:
            self.perform("검토한 이력 묶음 등록", lambda: trace.save_bundle(self.project, json.loads(Path(path).read_text(encoding="utf-8-sig"))), after=lambda result: self.refresh())

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.window, defaultextension=".json", initialfile="공정_시험_추적.json")
        if path:
            self.perform("이력 내보내기", lambda: str(trace.export_json(self.project, path)))

    def demo(self):
        from .traceability_demo import create_demo
        path = filedialog.askdirectory(parent=self.window, title="합성 예제를 만들 비어 있는 새 폴더")
        if path:
            def ready(result):
                self.window.destroy()
                self.app.project.set(result["project"])
                self.app.reload()
                self.app.reload_documents()
                TraceWindow(self.app)
            self.perform("합성 PPT·공정·raw 연결 예제 생성", lambda: create_demo(path), after=ready)
