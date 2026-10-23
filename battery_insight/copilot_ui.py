"""Explicit Copilot CLI actions and offline request/result transfer."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import copilot_cli as cli


class CopilotWindow:
    def __init__(self, owner):
        self.owner, self.project = owner, owner.project
        self.window = tk.Toplevel(owner.window)
        self.window.title("회사 Copilot CLI · 장표 해석")
        self.window.geometry("950x650")
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="회사 Copilot CLI로 장표 텍스트·표 해석", font=("맑은 고딕", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text="선택한 장표의 텍스트·표만 CLI에 보냅니다. raw 시계열은 보내지 않습니다.\n회사 CLI의 로그인·네트워크를 사용합니다. 모델 입력과 출력은 Copilot 이용량에 반영될 수 있습니다.",
                  wraplength=880).pack(anchor="w", pady=8)
        settings = cli.config(self.project)
        form = ttk.Frame(frame)
        form.pack(fill="x")
        self.executable = tk.StringVar(value=settings["executable"])
        self.model = tk.StringVar(value=settings["model"])
        self.timeout = tk.StringVar(value=str(settings["timeout_s"]))
        for row, (title, variable) in enumerate((("Copilot 실행 파일", self.executable), ("모델 ID (빈칸: 회사 CLI 기본값)", self.model), ("요청별 제한 시간(초)", self.timeout))):
            ttk.Label(form, text=title).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=variable, width=65).grid(row=row, column=1, sticky="ew", padx=8)
        form.columnconfigure(1, weight=1)
        ttk.Button(form, text="찾기", command=self.browse).grid(row=0, column=2)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=10)
        for title, action in (("설정 저장", self.save), ("CLI 설치·옵션 확인", self.check),
                              ("요청 폴더 열기", lambda: owner.app.open_path(cli.root(self.project) / "requests"))):
            ttk.Button(buttons, text=title, command=action).pack(side="left", padx=(0, 8))
        ttk.Label(frame, text="해석할 장표 선택 (Ctrl/Shift로 여러 개 선택)").pack(anchor="w")
        self.documents = tk.Listbox(frame, selectmode="extended", exportselection=False, height=9)
        self.documents.pack(fill="both", expand=True, pady=5)
        self.docs = [item for item in owner.snap.get("documents", []) if item.get("format") in {"pptx", "pdf", "xlsx"}]
        for doc in self.docs:
            self.documents.insert("end", doc["name"] + " [" + doc["document_id"][-8:] + "]")
        direct = ttk.Frame(frame)
        direct.pack(fill="x", pady=8)
        ttk.Button(direct, text="선택 장표 해석 시작", command=self.analyze).pack(side="left", padx=(0, 8))
        ttk.Button(direct, text="오프라인 요청 파일만 만들기", command=self.prepare).pack(side="left")
        portable = ttk.Frame(frame)
        portable.pack(fill="x", pady=5)
        ttk.Button(portable, text="다른 PC의 요청 파일 실행", command=self.run_files).pack(side="left", padx=(0, 8))
        ttk.Button(portable, text="Copilot 응답 파일 가져오기", command=self.import_files).pack(side="left")
        self.status = tk.StringVar(value="자동 호출하지 않습니다. 설치·옵션 확인은 로그인 성공 확인과 다릅니다.")
        ttk.Label(frame, textvariable=self.status, wraplength=880).pack(anchor="w", pady=10)
        ttk.Label(frame, text="결과는 PPT 검토 대기에 추가됩니다. 원문·단위·셀 대응을 확인해 등록하세요.\n이미지 표·그래프 인식과 자유 질의는 현재 연결 범위에 포함되지 않습니다.", wraplength=880).pack(anchor="w")

    def values(self):
        try:
            seconds = int(self.timeout.get())
        except ValueError as exc:
            raise ValueError("제한 시간은 정수로 입력하세요.") from exc
        return cli.checked_config({"executable": self.executable.get(), "model": self.model.get(), "timeout_s": seconds})

    def browse(self):
        path = filedialog.askopenfilename(parent=self.window, title="회사에서 설치한 Copilot 실행 파일", filetypes=[("Copilot 실행 파일", "*.exe *.cmd *.ps1 *.bat"), ("모든 파일", "*.*")])
        if path:
            self.executable.set(path)

    def save(self):
        try:
            cli.save_config(self.project, self.values())
            self.status.set("실행 경로·모델·제한 시간을 저장했습니다. 인증 정보는 저장하지 않습니다.")
        except ValueError as exc:
            messagebox.showerror("Copilot 설정", str(exc), parent=self.window)

    def _start(self, title, work, *, refresh=False):
        def done(result):
            if self.window.winfo_exists():
                self.status.set(str(result))
            if refresh:
                self.owner.refresh()
        self.owner.perform(title, work, after=done)

    def selected_documents(self):
        ids = [self.docs[index]["document_id"] for index in self.documents.curselection()]
        if not ids:
            raise ValueError("해석할 장표를 선택하세요.")
        return ids

    def check(self):
        try:
            settings = self.values()
        except ValueError as exc:
            messagebox.showerror("Copilot 설정", str(exc), parent=self.window)
            return
        self._start("회사 Copilot CLI 설치·옵션 확인", lambda: cli.check_cli(settings))

    def prepare(self):
        try:
            ids = self.selected_documents()
        except ValueError as exc:
            messagebox.showerror("장표 선택", str(exc), parent=self.window)
            return
        self._start("오프라인 Copilot 요청 만들기", lambda: cli.prepare(self.project, ids))

    def analyze(self):
        try:
            ids, settings = self.selected_documents(), self.values()
        except ValueError as exc:
            messagebox.showerror("Copilot 해석", str(exc), parent=self.window)
            return
        def work():
            cli.save_config(self.project, settings)
            requests = cli.prepare(self.project, ids)
            added, calls = 0, 0
            for path in requests["requests"]:
                result = cli.run_request(path, settings)
                added += cli.import_result(self.project, result["output"])["added"]
                calls += result["llm_calls"]
            return f"검토 후보 {added}개 추가 · 새 Copilot 요청 {calls}회 · 요청 파일 {requests['count']}개"
        self._start("선택 장표를 회사 Copilot CLI로 해석", work, refresh=True)

    def run_files(self):
        paths = filedialog.askopenfilenames(parent=self.window, title="다른 PC에서 만든 Copilot 요청 JSON", filetypes=[("Copilot 요청", "*.json")])
        if not paths:
            return
        try:
            settings = self.values()
        except ValueError as exc:
            messagebox.showerror("Copilot 설정", str(exc), parent=self.window)
            return
        self._start("회사 Copilot CLI로 요청 파일 실행", lambda: [cli.run_request(path, settings) for path in paths])

    def import_files(self):
        paths = filedialog.askopenfilenames(parent=self.window, title="회사 PC의 Copilot 응답 JSON", filetypes=[("Copilot 응답", "*.result.json *.json")])
        if paths:
            self._start("Copilot 응답을 검토 대기로 가져오기", lambda: [cli.import_result(self.project, path) for path in paths], refresh=True)
