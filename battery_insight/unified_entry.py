"""Single GUI/CLI entry; legacy BatteryInsight commands stay available."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__

COMMANDS = {"desktop", "cyc-import", "library-scan", "library-export", "library-assign", "library-rules",
            "library-open", "library-documents", "library-candidates", "library-link", "library-features", "self-test", "check-gui", "version",
            "trace-extract", "trace-import", "trace-review", "trace-report", "trace-export", "trace-demo",
            "copilot-check", "copilot-prepare", "copilot-run", "copilot-import", "db-build", "network"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def emit_json(result, *, error=False):
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
        stream.flush()
    except (OSError, UnicodeError):
        # Diagnostic output must never turn an already committed conversion
        # into a reported failure. --result-json remains UTF-8 and authoritative.
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass
    result_path = None
    try:
        values = []
        cleaned = []
        index = 0
        while index < len(argv):
            if argv[index] == "--result-json":
                if index + 1 >= len(argv):
                    raise ValueError("--result-json 경로가 필요합니다.")
                values.append(argv[index + 1])
                index += 2
            elif argv[index].startswith("--result-json="):
                values.append(argv[index].split("=", 1)[1])
                index += 1
            else:
                cleaned.append(argv[index])
                index += 1
        if len(values) > 1:
            raise ValueError("--result-json은 한 번만 지정하세요.")
        if values:
            proposed = Path(values[0]).resolve()
            if proposed.suffix.lower() != ".json":
                raise ValueError("실행 결과는 별도 .json 파일로 저장하세요.")
            protected = []
            for position, token in enumerate(cleaned):
                if token in {"--project", "--input", "--out", "--config", "--metadata", "--rules", "--spec"} and position + 1 < len(cleaned):
                    protected.append(Path(cleaned[position + 1]).resolve())
                elif token.startswith(tuple(name + "=" for name in ("--project", "--input", "--out", "--config", "--metadata", "--rules", "--spec"))):
                    protected.append(Path(token.split("=", 1)[1]).resolve())
            if any(proposed == path or proposed.is_relative_to(path) for path in protected):
                raise ValueError("실행 결과 JSON은 입력·프로젝트·출력 경로 바깥에 지정하세요.")
            if proposed.exists():
                old = json.loads(proposed.read_text(encoding="utf-8"))
                if not isinstance(old, dict) or old.get("application") != "BatteryInsight":
                    raise ValueError("이 경로에는 다른 파일이 있습니다. 새 결과 파일을 지정하세요.")
            result_path = proposed
        if not cleaned:
            from .desktop import launch
            return launch()
        cleaned[0] = {"--self-test": "self-test", "--check-gui": "check-gui", "--version": "version"}.get(cleaned[0], cleaned[0])
        if cleaned[0] not in COMMANDS:
            from .cli import main as legacy_main
            with contextlib.ExitStack() as stack:
                if sys.stdout is None:
                    stack.enter_context(contextlib.redirect_stdout(stack.enter_context(open(os.devnull, "w"))))
                if sys.stderr is None:
                    stack.enter_context(contextlib.redirect_stderr(stack.enter_context(open(os.devnull, "w"))))
                code = legacy_main(cleaned)
            result = {"status": "success" if code == 0 else "failed", "command": cleaned[0], "exit_code": code}
        else:
            parser = Parser(description="BatteryInsight integrated offline application", allow_abbrev=False)
            parser.add_argument("command", choices=sorted(COMMANDS))
            parser.add_argument("--project", type=Path)
            parser.add_argument("--input", type=Path)
            parser.add_argument("--out", type=Path)
            parser.add_argument("--dataset")
            parser.add_argument("--record")
            parser.add_argument("--proposal")
            parser.add_argument("--action", choices=["accepted", "dismissed"], default="accepted")
            parser.add_argument("--verify-raw", action="store_true")
            parser.add_argument("--copilot-exe")
            parser.add_argument("--copilot-model")
            parser.add_argument("--metadata", type=Path)
            parser.add_argument("--rules", type=Path)
            parser.add_argument("--spec", type=Path)
            parser.add_argument("--documents", nargs="+")
            parser.add_argument("--build-only", action="store_true")
            parser.add_argument("--port", type=int, default=0)
            parser.add_argument("--workers", type=int, default=8)
            parser.add_argument("--no-network", action="store_true")
            parser.add_argument("--force", action="store_true")
            args = parser.parse_args(cleaned)
            from . import library
            if args.command in {"self-test", "check-gui", "version", "desktop"}:
                if args.command == "version":
                    result = {"status": "success"}
                elif args.command == "desktop":
                    from .desktop import launch
                    return launch(args.project)
                elif args.command == "self-test":
                    from .integrated_selftest import run_self_test
                    result = run_self_test()
                else:
                    from .desktop import check_gui
                    with tempfile.TemporaryDirectory(prefix="batteryinsight_gui_") as temporary:
                        result = check_gui(args.project or Path(temporary) / "project")
            elif args.command.startswith("copilot-"):
                from . import copilot_cli
                settings = copilot_cli.config(args.project) if args.project else copilot_cli.checked_config({})
                if args.copilot_exe:
                    settings["executable"] = args.copilot_exe
                if args.copilot_model is not None:
                    settings["model"] = args.copilot_model
                settings = copilot_cli.checked_config(settings)
                if args.command == "copilot-check":
                    result = copilot_cli.check_cli(settings)
                elif args.command == "copilot-run":
                    if not args.input:
                        raise ValueError("--input Copilot 요청 JSON이 필요합니다.")
                    result = copilot_cli.run_request(args.input, settings, output=args.out)
                else:
                    if not args.project:
                        raise ValueError("--project로 프로젝트 폴더를 지정하세요.")
                    if args.command == "copilot-prepare":
                        result = copilot_cli.prepare(args.project, args.documents)
                    else:
                        if not args.input:
                            raise ValueError("--input Copilot 응답 JSON이 필요합니다.")
                        result = copilot_cli.import_result(args.project, args.input)
                result = {"status": "success", **result}
            else:
                if args.project is None:
                    raise ValueError("--project로 프로젝트 폴더를 지정하세요.")
                if args.command.startswith("trace-"):
                    from . import traceability
                    if args.command == "trace-extract":
                        from .slide_candidates import extract_proposals
                        result = extract_proposals(args.project)
                    elif args.command == "trace-demo":
                        from .traceability_demo import create_demo
                        result = create_demo(args.project)
                    elif args.command == "trace-import":
                        if not args.spec:
                            raise ValueError("--spec 검토한 공정 등록 JSON이 필요합니다.")
                        result = traceability.save_bundle(args.project, json.loads(args.spec.read_text(encoding="utf-8-sig")))
                    elif args.command == "trace-review":
                        from .slide_candidates import review_proposal
                        if not args.proposal:
                            raise ValueError("--proposal 검토할 후보 ID가 필요합니다.")
                        result = {"saved": review_proposal(args.project, args.proposal,
                            json.loads(args.spec.read_text(encoding="utf-8-sig")) if args.spec else None, action=args.action)}
                    elif args.command == "trace-export":
                        if not args.out:
                            raise ValueError("--out 별도 JSON 내보내기 경로가 필요합니다.")
                        result = {"output": str(traceability.export_json(args.project, args.out))}
                    else:
                        from .traceability_report import build_report
                        result = {"output": str(build_report(args.project, focus=args.dataset, verify=args.verify_raw))}
                elif args.command == "cyc-import":
                    if not args.input:
                        raise ValueError("--input CYC 폴더가 필요합니다.")
                    result = library.convert_and_register(args.project, args.input)
                elif args.command == "library-scan":
                    if not args.input:
                        raise ValueError("--input Parquet 폴더가 필요합니다.")
                    result = library.scan_folder(args.project, args.input)
                elif args.command == "library-export":
                    if not args.out:
                        raise ValueError("--out 등록표 CSV 경로가 필요합니다.")
                    result = {"output": str(library.export_registration(args.project, args.out))}
                elif args.command == "library-assign":
                    if not args.dataset or not args.metadata:
                        raise ValueError("--dataset 및 --metadata JSON이 필요합니다.")
                    result = library.assign_metadata(args.project, args.dataset, json.loads(args.metadata.read_text(encoding="utf-8-sig")))
                elif args.command == "library-rules":
                    if not args.rules:
                        raise ValueError("--rules JSON이 필요합니다.")
                    result = {"rules": library.set_rules(args.project, json.loads(args.rules.read_text(encoding="utf-8-sig"))), "classification": library.apply_rules(args.project)}
                elif args.command == "library-documents":
                    if not args.documents:
                        raise ValueError("--documents 경로가 필요합니다.")
                    result = {"documents": library.import_documents(args.project, args.documents)}
                elif args.command == "library-candidates":
                    result = {"candidates": library.knowledge_candidates(args.project, args.dataset)}
                elif args.command == "library-link":
                    result = library.link_record(args.project, args.dataset, args.record)
                elif args.command == "library-features":
                    result = library.import_feature_export(args.project, args.dataset, args.spec)
                elif args.command == "db-build":
                    from .db_build import build_database
                    result = build_database(args.project, workers=args.workers, network=not args.no_network, force=args.force,
                                            progress=lambda message: print(message, file=sys.stderr) if sys.stderr else None)
                elif args.command == "network":
                    from .design_network import build_network
                    output = Path(build_network(args.project))
                    result = {"output": str(output)}
                    if not args.build_only:
                        import webbrowser
                        webbrowser.open(output.resolve().as_uri())
                elif args.command == "library-open":
                    from .library_workbench import workbench_config
                    from .workbench_server import Workbench, serve_workbench
                    config = workbench_config(args.project)
                    output = args.project / "workbench"
                    if args.build_only:
                        controller = Workbench(config, output)
                        result = {"experiments": len(controller.bundle["experiments"]), "output": str(output / "dashboard/index.html")}
                    else:
                        serve_workbench(config, output, port=args.port, open_browser=True)
                        result = {}
                result = {"status": "success", **result}
        result = {"application": "BatteryInsight", "version": __version__, **result}
        if result_path:
            from .library import atomic_json
            atomic_json(result_path, result)
        emit_json(result)
        return 0 if result.get("status") == "success" else 1
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        result = {"application": "BatteryInsight", "version": __version__, "status": "success" if code == 0 else "failed", "exit_code": code}
        if result_path:
            from .library import atomic_json
            atomic_json(result_path, result)
        return code
    except Exception as exc:
        result = {"application": "BatteryInsight", "version": __version__, "status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        if result_path:
            from .library import atomic_json
            try:
                atomic_json(result_path, result)
            except OSError:
                pass
        emit_json(result, error=True)
        if not argv:
            try:
                from tkinter import messagebox
                messagebox.showerror("BatteryInsight", str(exc))
            except Exception:
                pass
        return 1
