"""Append-one-document workflow for reviewed experiment metadata."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import os


def add_knowledge_commands(commands):
    init = commands.add_parser('knowledge-init', help='Initialize an evidence registry and intake checklist')
    init.add_argument('--out', required=True)
    init.add_argument('--database', help='Optional raw DB to prepare an unconfirmed raw-key inventory')
    intake = commands.add_parser('knowledge-intake', help='Extract local slide/page evidence without asserting experiment facts')
    intake.add_argument('--registry', required=True)
    intake.add_argument('--documents', nargs='+', required=True)
    intake.add_argument('--out', required=True)
    records = commands.add_parser('knowledge-records', help='Append draft/reviewed experiment and sample record revisions')
    records.add_argument('--registry', required=True)
    records.add_argument('--records', required=True, help='JSON list or object with records array')
    records.add_argument('--reviewer')
    records.add_argument('--reason')
    records.add_argument('--out', help='Optional refreshed review directory')
    match = commands.add_parser('knowledge-match', help='Propose date/set/sample matches; never auto-confirm')
    match.add_argument('--registry', required=True)
    match.add_argument('--database', required=True)
    match.add_argument('--raw-keys', help='Explicitly reviewed raw keys CSV or JSON; filename hints are not keys')
    match.add_argument('--out', required=True)
    link = commands.add_parser('knowledge-link', help='Confirm, reject or clear individual links in a separate overlay')
    for name in ('registry', 'database', 'proposals', 'decisions', 'reviewer', 'reason', 'out'):
        link.add_argument('--' + name, required=True)
    link.add_argument('--previous', help='Existing overlay, preserving earlier confirmations and review events')
    export = commands.add_parser('knowledge-export', help='Export the evidence registry and outstanding questions')
    export.add_argument('--registry', required=True)
    export.add_argument('--out', required=True)


def _read_rows(path, key):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f'Expected a JSON list or an object containing a {key} array')
    return value


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _require_registry(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Knowledge registry is missing; run knowledge-init first')
    return path


def run_knowledge_command(args):
    from .knowledge_registry import KnowledgeRegistry
    from .knowledge_report import write_templates, write_review
    command = args.command
    if command == 'knowledge-init':
        root = Path(args.out).resolve()
        root.mkdir(parents=True, exist_ok=True)
        write_templates(root)
        with KnowledgeRegistry(root / 'knowledge.sqlite3') as registry:
            snapshot = registry.snapshot()
        if args.database:
            from .raw_linking import write_raw_key_template, raw_inventory
            path = root / 'raw_keys.csv'
            # A filled key sheet belongs to the user. Reinitialization must not erase it.
            inventory = write_raw_key_template(args.database, path) if not path.exists() else raw_inventory(args.database)
            _write_json(root / 'raw_inventory.json', inventory)
            snapshot['raw_inventory_summary'] = {'experiments': len(inventory['rows']), 'database_path': inventory['database_path']}
        print(write_review(snapshot, root))
    elif command == 'knowledge-intake':
        from .document_intake import extract_document
        registry_path = _require_registry(args.registry)
        out = Path(args.out).resolve()
        reserved = {out / name for name in ('index.html', 'knowledge_snapshot.json', 'records.csv', 'intake_guide.html')}
        if any(Path(path).resolve() in reserved | {registry_path} for path in args.documents):
            raise ValueError('Document input must not be a registry or generated review output')
        results = []
        with KnowledgeRegistry(registry_path) as registry:
            for path in args.documents:
                extraction = extract_document(path)
                # A missing/oversized input has no content identity. Keep its
                # diagnostic in this intake report and continue other documents.
                if extraction.get('sha256'):
                    registry.import_document(extraction)
                results.append({'name': extraction.get('name'), 'document_id': extraction.get('document_id'),
                                'source_path': str(Path(path).resolve()),
                                'status': extraction.get('status'), 'issues': extraction.get('issues', [])})
            snapshot = registry.snapshot()
        snapshot['intake_results'] = results
        _write_json(out / 'intake_result.json', results)
        print(json.dumps(results, ensure_ascii=False))
        print(write_review(snapshot, out))
        return 2 if any(row['status'] == 'failed' for row in results) else 0
    elif command == 'knowledge-records':
        with KnowledgeRegistry(_require_registry(args.registry)) as registry:
            records = registry.upsert_records(_read_rows(args.records, 'records'), reviewer=args.reviewer, reason=args.reason)
            snapshot = registry.snapshot()
        print(json.dumps({'records': [{'record_id': r['record_id'], 'revision': r['revision'], 'status': r['status']} for r in records]}, ensure_ascii=False))
        if args.out:
            print(write_review(snapshot, args.out))
    elif command == 'knowledge-match':
        from .raw_linking import propose_links
        with KnowledgeRegistry(_require_registry(args.registry), read_only=True) as registry:
            snapshot = registry.snapshot()
        proposals = propose_links(args.database, snapshot['records'], args.raw_keys)
        print(write_review(snapshot, args.out, proposals=proposals))
    elif command == 'knowledge-link':
        from .raw_linking import review_links
        out = Path(args.out).resolve()
        database = Path(args.database).resolve()
        protected = {_require_registry(args.registry), Path(args.proposals).resolve(), Path(args.decisions).resolve()}
        if out in protected or out.is_relative_to(database):
            raise ValueError('Write links outside the raw database and input/registry files')
        # Updating an overlay preserves its events unless a different previous file is explicit.
        previous = args.previous or (str(out) if out.exists() else None)
        with KnowledgeRegistry(_require_registry(args.registry), read_only=True) as registry:
            overlay = review_links(args.database, registry, args.proposals, _read_rows(args.decisions, 'decisions'),
                                   args.reviewer, args.reason, existing_overlay=previous)
        _write_json(out, overlay)
        print(out)
    elif command == 'knowledge-export':
        with KnowledgeRegistry(_require_registry(args.registry), read_only=True) as registry:
            snapshot = registry.snapshot()
        print(write_review(snapshot, args.out))
    else:
        raise ValueError('Unknown knowledge command')
    return 0
