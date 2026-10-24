from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import sqlite3
import pandas as pd
from .pipeline import run_pipeline, save_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local electrochemical evidence pipeline; source files are read-only")
    commands = parser.add_subparsers(dest="command", required=True)
    from .knowledge_cli import add_knowledge_commands
    add_knowledge_commands(commands)
    run = commands.add_parser("run", help="CSV/Parquet -> Bronze/Silver/Gold and a local review report")
    run.add_argument("--config", required=True)
    run.add_argument("--out", required=True)
    inspect = commands.add_parser("inspect", help="Read-only source-code/feature inventory; no raw-data conversion")
    inspect.add_argument("--roots", nargs="+", required=True)
    inspect.add_argument("--out", required=True)
    compare = commands.add_parser("compare", help="Check a controlled comparison against an explicit variable list")
    compare.add_argument("--registry", required=True)
    compare.add_argument("--cell-a", required=True)
    compare.add_argument("--cell-b", required=True)
    compare.add_argument("--target", required=True)
    compare.add_argument("--variables", nargs="+", required=True)
    compare.add_argument("--out", required=True)
    review = commands.add_parser("review", help="Append a human review event; does not rewrite features or remove cells")
    review.add_argument("--run", required=True)
    review.add_argument("--item", required=True, help="table name and stable row/feature identity")
    review.add_argument("--decision", choices=["accepted", "rejected", "needs_evidence"], required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--reviewer", required=True)
    for name in ("update", "watch"):
        updating = commands.add_parser(name, help="Registered raw inbox -> experiment network and indicator correlations")
        updating.add_argument("--config", required=True)
        updating.add_argument("--out", required=True)
        updating.add_argument("--verify-all", action="store_true", help="Hash raw content even when file size/mtime are unchanged")
        if name == "watch":
            updating.add_argument("--interval", type=float, default=30)
            updating.add_argument("--port", type=int, default=8765, help="Loopback dashboard port; 0 disables the local server")
    workbench = commands.add_parser("workbench", help="Local comparison workspace with selectable modules, evidence and registration")
    workbench.add_argument("--config", required=True)
    workbench.add_argument("--out", required=True)
    workbench.add_argument("--port", type=int, default=8765)
    workbench.add_argument("--interval", type=float, default=30)
    workbench.add_argument("--verify-all", action="store_true")
    workbench.add_argument("--open", action="store_true", help="Open the local workspace in the browser")
    workbench.add_argument("--build-only", action="store_true", help="Build a saved preview without running a server")
    args = parser.parse_args(argv)
    try:
        if args.command.startswith("knowledge-"):
            from .knowledge_cli import run_knowledge_command
            return run_knowledge_command(args)
        elif args.command == "workbench":
            from .workbench_server import Workbench, serve_workbench
            if args.build_only:
                controller = Workbench(args.config, args.out, verify_all=args.verify_all)
                print(controller.output / "dashboard" / "index.html")
            else:
                serve_workbench(args.config, args.out, port=args.port, interval=args.interval,
                                verify_all=args.verify_all, open_browser=args.open)
        elif args.command in {"update", "watch"}:
            from .experiment_updates import update_experiments, watch_experiments
            if args.command == "update":
                result = update_experiments(args.config, args.out, verify_all=args.verify_all)
                print(json.dumps(result["ingestion"], ensure_ascii=False))
                print(Path(args.out).resolve() / "dashboard" / "index.html")
            else:
                import functools
                from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
                import threading
                server = None
                if not 0 <= args.port <= 65535:
                    raise ValueError("port must be 0..65535")
                try:
                    for result in watch_experiments(args.config, args.out, interval=args.interval, verify_all=args.verify_all):
                        if args.port and server is None:
                            class QuietHandler(SimpleHTTPRequestHandler):
                                def log_message(self, format, *args):
                                    pass
                            handler = functools.partial(QuietHandler, directory=str(Path(args.out).resolve() / "dashboard"))
                            server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
                            threading.Thread(target=server.serve_forever, daemon=True).start()
                            print(f"Local dashboard: http://127.0.0.1:{args.port}/", flush=True)
                        print(json.dumps({"generated_at": result["generated_at"], "processed": result["ingestion"]["processed"], "reused": result["ingestion"]["reused"], "pending": len(result["ingestion"]["pending"]), "failed": len(result["ingestion"]["failed"])}, ensure_ascii=False), flush=True)
                except KeyboardInterrupt:
                    print("Stopped watching; saved report remains available.")
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
        elif args.command == "run":
            print(run_pipeline(args.config, args.out))
        elif args.command == "inspect":
            from .adapters import inspect_assets
            save_json(Path(args.out), inspect_assets(args.roots))
            print(args.out)
        elif args.command == "compare":
            from .diagnostics import compare_cells
            p = Path(args.registry)
            registry = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p, encoding="utf-8-sig")
            save_json(Path(args.out), compare_cells(registry, args.cell_a, args.cell_b, args.target, args.variables))
            print(args.out)
        elif args.command == "review":
            run_dir = Path(args.run)
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            if not args.reason.strip() or not args.reviewer.strip() or not args.item.strip():
                raise ValueError("item, reason and reviewer cannot be blank")
            event = {"run_id": manifest["run_id"], "item": args.item, "decision": args.decision,
                     "reason": args.reason, "reviewer": args.reviewer, "at": datetime.now(timezone.utc).isoformat()}
            with (run_dir / "review_events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            print(run_dir / "review_events.jsonl")
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0
