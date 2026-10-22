"""Local raw database: source catalog + selected, compressed Parquet + features.

The source files stay in their original locations. Native Restore fragments are
streamed from ZIPs/directories once per channel; binary files, backup files and
already converted CSVs are not ingested as extra experiments.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import copy
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

import numpy as np
import pandas as pd

from .compact_storage import CompactWriter, save_compact_table, storage_policy
from .ingest import complete_cycle_chunks, sha256_file
from .pipeline import digest_json, save_json, _dictionary


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(_json(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _code_hash():
    names = ('database.py', 'pne.py', 'compact_storage.py', 'features.py', 'diagnostics.py', 'ingest.py')
    return digest_json({name: sha256_file(Path(__file__).with_name(name)) for name in names})


def scan_database_sources(config):
    from .pne import discover_pne_sources, RESTORE_SCHEMA
    if config.get('schema_version') != 1 or not isinstance(config.get('sources'), list) or not config['sources']:
        raise ValueError('Database config requires schema_version=1 and a nonempty sources list')
    seen, seen_members, descriptors = set(), set(), []
    for position, entry in enumerate(config['sources']):
        if entry.get('format', RESTORE_SCHEMA) != RESTORE_SCHEMA:
            raise ValueError('Choose the verified pne_restore_51_smallcell_v1 profile explicitly for this native database importer')
        if entry.get('test_type') not in {'Cycle', 'Insitu', 'RPT', 'DCIR', 'HPPC', 'Formation', 'GITT', 'Storage'}:
            raise ValueError('Each source needs an explicit supported test_type; filenames do not assign it')
        if entry['test_type'] == 'RPT' and not str(entry.get('rpt_stage', '')).strip():
            raise ValueError('RPT sources require an explicit rpt_stage label')
        dataset_id = entry.get('dataset_id', f'dataset_{position + 1}')
        for native in discover_pne_sources(entry['path']):
            if entry.get('channels') is not None and native['channel_id'] not in entry['channels']:
                continue
            if native['source_id'] in seen:
                raise ValueError('Duplicate channel source in database config')
            seen.add(native['source_id'])
            for member in native['members']:
                locator = ((str(Path(native['container']).resolve()).casefold(), member['member']) if native['is_archive']
                           else (str((Path(native['container']) / member['member']).resolve()).casefold(),))
                if locator in seen_members:
                    raise ValueError('The same Restore member is selected more than once; check overlapping input folders/channels')
                seen_members.add(locator)
            descriptors.append({'native': native, 'entry': dict(entry, dataset_id=dataset_id)})
    if not descriptors:
        raise ValueError('No supported Restore channels were selected')
    return descriptors


def _metadata(native, entry):
    explicit = dict(entry.get('metadata', {}))
    overrides = entry.get('channel_overrides', {})
    explicit.update(overrides.get(native['channel_id'], {}))
    explicit.update(overrides.get(native['source_id'], {}))
    test_id = 'test_' + native['source_id'].removeprefix('pne_')
    # A channel is not evidence that the same physical cell was reused later.
    cell_id = str(explicit.get('cell_id') or native['source_id'])
    experiment = {'experiment_id': test_id, 'test_id': test_id, 'cell_id': cell_id,
        'cell_identity_status': 'explicit' if explicit.get('cell_id') else 'provisional_channel_identity',
        'test_type': entry['test_type'], 'rpt_stage': entry.get('rpt_stage', ''),
        'label': f"{entry['dataset_id']} · {native['channel_id']}", 'dataset_id': entry['dataset_id'],
        'cohort': f"{entry['dataset_id']}:{entry['test_type']}",
        'protocol': native.get('protocol_key') or 'unverified', 'chemistry': '', 'temperature_c': None,
        'design_id': '', 'batch_id': '', 'include': True,
        'source_id': native['source_id'], 'source_container': native['container'],
        'source_channel': native['channel_id'], 'source_channel_path': native['channel_path'],
        'source_member_count': len(native['members']), 'status': 'pending'}
    allowed = {'cell_id', 'label', 'cohort', 'protocol', 'chemistry', 'temperature_c', 'design_id', 'batch_id',
               'rpt_stage', 'rpt_life_cycle', 'pressure_mpa', 'charge_rate_c', 'discharge_rate_c',
               'voltage_min_v', 'voltage_max_v', 'formation_id', 'include'}
    # Design fields are deliberately user-entered rather than inferred from paths.
    allowed |= {k for k in explicit if k.startswith(('cathode_', 'anode_', 'electrolyte_', 'separator_'))}
    if set(explicit) - allowed:
        raise ValueError(f'Unknown or protected channel metadata: {sorted(set(explicit) - allowed)}')
    experiment.update(explicit)
    if type(experiment['include']) is not bool:
        raise ValueError('Channel include must be a JSON boolean')
    return experiment


def _source_identity(native, experiment):
    return {'kind': 'pne_restore', 'path': native['container'],
            'sha256': native.get('container_sha256') or native.get('channel_sha256'),
            'channel_sha256': native.get('channel_sha256'), 'source_id': native['source_id'],
            'channel_path': native['channel_path'], 'members': copy.deepcopy(native['members']),
            'cell_id': experiment['cell_id'], 'test_id': experiment['test_id']}


def _verify_run(run):
    manifest = json.loads((run / 'run_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('status') != 'complete':
        raise ValueError('Incomplete raw database run')
    for relative, expected in manifest['artifact_sha256'].items():
        path = (run / relative).resolve()
        if not path.is_relative_to(run.resolve()) or not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f'Changed raw database artifact: {relative}')
    return manifest


def _joined(parts):
    items = [p for p in parts if not p.empty]
    return pd.concat(items, ignore_index=True) if items else pd.DataFrame()


def _native_chunks(native, experiment, batch_rows):
    from .pne import iter_pne_batches
    for batch in iter_pne_batches(native, batch_rows):
        frame = batch.copy()
        frame['source_member_row'] = frame['source_row']
        frame['source_row'] = frame['record_index']
        frame['cell_id'], frame['test_id'], frame['test_type'] = experiment['cell_id'], experiment['test_id'], experiment['test_type']
        frame['step_id'] = frame['step_id'].astype(str)
        frame['step_kind'] = frame['step_type']
        frame['temperature_c'], frame['soc_pct'] = np.nan, np.nan
        yield frame


def _channel_run(native, experiment, output, policy, settings, *, batch_rows, max_cycle_rows, diagnostics_settings):
    """All feature calculations see original retained-in-memory samples, before selection."""
    from .features import extract_features, finalize_cycles
    from .diagnostics import diagnose, life_summary
    # Stat/source member signatures support incremental lookup. The parser verifies
    # member hashes before publishing a new run, and reuse verifies all artifacts.
    if not native['is_archive']:
        for member in native['members']:
            member['sha256'] = sha256_file(Path(native['container']) / member['member'])
    signature = {'container': native['container'], 'container_sha256': native.get('container_sha256'),
                 'channel_path': native['channel_path'], 'members': [{k: m.get(k) for k in ('member', 'bytes', 'crc32', 'mtime_ns', 'sha256')}
                     for m in native['members']], 'schedules': native.get('schedules', [])}
    fingerprint = {'source': signature, 'cell_id': experiment['cell_id'], 'test_id': experiment['test_id'],
                   'test_type': experiment['test_type'], 'include': experiment['include'], 'storage': policy, 'features': settings,
                   'diagnostics': diagnostics_settings, 'code_hash': _code_hash()}
    run_id = digest_json(fingerprint)[:24]
    destination = output / 'runs' / run_id
    if (destination / 'run_manifest.json').is_file():
        manifest = _verify_run(destination)
        # Folder reuse also verifies the source bytes; unchanged timestamps alone
        # are not sufficient when users retain/restore file modification times.
        if not native['is_archive']:
            identity = manifest['identity']['inputs'][0]
            for member in identity['members']:
                if sha256_file(Path(native['container']) / member['member']) != member['sha256']:
                    raise ValueError('Source member bytes changed without a matching database revision')
        return destination, manifest, True
    staging = output / 'runs' / ('.' + run_id + '.' + uuid.uuid4().hex[:8] + '.staging')
    staging.mkdir(parents=True, exist_ok=False)
    signal_path = staging / 'silver' / digest_json(experiment['test_id'])[:16] / 'part-000000.parquet'
    writer = CompactWriter(signal_path, policy)
    cycle_parts, pulse_parts, quality_parts = [], [], []
    try:
        def chunks():
            for frame in _native_chunks(native, experiment, batch_rows):
                writer.write(frame)
                yield frame
        for cycle in complete_cycle_chunks(chunks(), max_cycle_rows):
            features, pulses, issues = extract_features(cycle, settings)
            for table in (features, pulses):
                if not table.empty:
                    table['include'] = bool(experiment.get('include', True))
                    table['mapping_method'] = 'verified_pne_restore_51_smallcell_v1'
            cycle_parts.append(features)
            pulse_parts.append(pulses)
            quality_parts.append(issues)
        retention = writer.close()
        if not retention['source_rows']:
            raise ValueError('No raw records in channel')
        cycles = _joined(cycle_parts)
        if not cycles.empty:
            cycles = finalize_cycles(cycles, settings)
        tables = {'cycles': cycles, 'pulses': _joined(pulse_parts), 'quality': _joined(quality_parts),
                  'diagnostics': diagnose(cycles, diagnostics_settings), 'life_summary': life_summary(cycles, {}),
                  'external_features': pd.DataFrame()}
        for name, frame in tables.items():
            save_compact_table(frame, staging / 'gold' / (name + '.parquet'), policy)
        source = _source_identity(native, experiment)
        save_json(staging / 'source_descriptor.json', _json(native))
        save_json(staging / 'config_snapshot.json', {'schema_version': 1, 'features': settings, 'storage': policy,
                  'sources': [{'path': native['container'], 'cell_id': experiment['cell_id'], 'test_id': experiment['test_id'],
                               'test_type': experiment['test_type'], 'format': native['schema']}]})
        save_json(staging / 'feature_dictionary.json', _dictionary(cycles, tables['pulses']))
        manifest = {'run_id': run_id, 'status': 'complete', 'created_at': _now(),
                    'identity': {'inputs': [source], 'config_hash': digest_json(fingerprint), 'code_hash': fingerprint['code_hash']},
                    'rows': {k: len(v) for k, v in tables.items()}, 'retention': retention,
                    'source_row_basis': 'channel_record_index', 'source_location_columns': ['source_member', 'source_member_row'],
                    'input_readonly': True, 'feature_settings': settings, 'aim_status': 'not_configured',
                    'limitations': ['Physical cell/design identity requires user metadata',
                        'Native CC/CV mode, SOC and sensor temperature are not inferred',
                        'Step termination does not prove a complete SOC window',
                        'RPT internal cycle is not lifetime cycle', 'Unverified measurements remain missing']}
        manifest['artifact_sha256'] = {p.relative_to(staging).as_posix(): sha256_file(p) for p in sorted(staging.rglob('*')) if p.is_file()}
        save_json(staging / 'run_manifest.json', manifest)
        staging.rename(destination)
        return destination, manifest, False
    except Exception as exc:
        writer.close()
        save_json(staging / 'failure.json', {'status': 'failed', 'message': str(exc), 'source_id': native['source_id']})
        raise


def _catalog(output, experiments, manifest):
    """SQLite indexes identity/design/provenance; dense samples live only in Parquet."""
    # sqlite3's context manager commits/rolls back but does not close the handle.
    # Exit the transaction before closing so Windows can move/remove the DB.
    with closing(sqlite3.connect(output / 'catalog.sqlite3')) as db, db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE IF NOT EXISTS datasets (dataset_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS cells (cell_id TEXT PRIMARY KEY, identity_status TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS designs (design_id TEXT PRIMARY KEY, properties_json TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS experiments (experiment_id TEXT PRIMARY KEY, cell_id TEXT NOT NULL, dataset_id TEXT NOT NULL, design_id TEXT, test_type TEXT, rpt_stage TEXT, run_path TEXT, status TEXT, metadata_json TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS experiments_cell ON experiments(cell_id)')
        db.execute('CREATE INDEX IF NOT EXISTS experiments_design ON experiments(design_id, test_type, rpt_stage)')
        db.execute('CREATE TABLE IF NOT EXISTS imports (import_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, summary_json TEXT NOT NULL)')
        design_properties = {}
        for row in experiments:
            db.execute('INSERT OR REPLACE INTO datasets VALUES (?,?)', (row['dataset_id'], json.dumps({'source_container': row['source_container']}, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO cells VALUES (?,?)', (row['cell_id'], row['cell_identity_status']))
            if row.get('design_id'):
                properties = {k: v for k, v in row.items() if k.startswith(('cathode_', 'anode_', 'electrolyte_', 'separator_'))}
                design_properties.setdefault(row['design_id'], {}).update(properties)
            db.execute('INSERT OR REPLACE INTO experiments VALUES (?,?,?,?,?,?,?,?,?)',
                (row['experiment_id'], row['cell_id'], row['dataset_id'], row.get('design_id'), row['test_type'], row.get('rpt_stage'),
                 row.get('run_path'), row['status'], json.dumps(_json(row), ensure_ascii=False)))
        for identifier, properties in design_properties.items():
            db.execute('INSERT OR REPLACE INTO designs VALUES (?,?)', (identifier, json.dumps(properties, ensure_ascii=False)))
        # Remove current catalog entries no longer selected, never their source files.
        ids = [r['experiment_id'] for r in experiments]
        db.execute('DELETE FROM experiments WHERE experiment_id NOT IN (' + ','.join('?' for _ in ids) + ')', ids)
        db.execute('INSERT OR REPLACE INTO imports VALUES (?,?,?)',
                   (manifest['import_id'], manifest['generated_at'], json.dumps(manifest['summary'], ensure_ascii=False)))


def import_database(config_path, output, *, progress=None):
    config_path, output = Path(config_path).resolve(), Path(output).resolve()
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    config = copy.deepcopy(config)
    for source in config.get('sources', []):
        source['path'] = str((config_path.parent / source['path']).resolve())
    policy = storage_policy(config.get('storage'))
    batch_rows = config.get('batch_rows', 65536)
    if type(batch_rows) is not int or not 1 <= batch_rows <= 262144:
        raise ValueError('batch_rows must be an integer from 1 to 262144')
    descriptors = scan_database_sources(config)
    designs = {}
    for item in descriptors:
        row = _metadata(item['native'], item['entry'])
        if row.get('design_id'):
            known = designs.setdefault(row['design_id'], {})
            for key, value in row.items():
                if not key.startswith(('cathode_', 'anode_', 'electrolyte_', 'separator_')) or value is None or value == '':
                    continue
                if key in known and known[key] != value:
                    raise ValueError(f"Conflicting {key} for design_id {row['design_id']}; split the design identity or correct metadata")
                known[key] = value
    if any(output == Path(x['native']['container']) or output.is_relative_to(Path(x['native']['container']))
           for x in descriptors if not x['native']['is_archive']):
        raise ValueError('Database output must not be inside an original raw source directory')
    output.mkdir(parents=True, exist_ok=True)
    settings = config.get('features', {})
    experiments, reused = [], 0
    for number, item in enumerate(descriptors, 1):
        native, entry = item['native'], item['entry']
        row = _metadata(native, entry)
        began = time.perf_counter()
        try:
            run, metadata, cached = _channel_run(native, row, output, policy, settings, batch_rows=batch_rows,
                max_cycle_rows=config.get('max_cycle_rows', 2_000_000), diagnostics_settings=config.get('diagnostics', {}))
            reused += cached
            signals = sorted((run / 'silver').rglob('*.parquet')) if (run / 'silver').exists() else []
            source_identity = metadata['identity']['inputs'][0]
            row.update(status='complete', run_path=str(run), raw_path=str(signals[0]) if signals else '',
                       source_sha256=source_identity['sha256'], retention=metadata['retention'], rows=metadata['rows'],
                       source_restore_bytes=sum(m['bytes'] for m in source_identity['members']),
                       reused=bool(cached), elapsed_s=round(time.perf_counter() - began, 3))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row.update(status='failed', error=str(exc), elapsed_s=round(time.perf_counter() - began, 3))
        experiments.append(row)
        if progress:
            progress({'index': number, 'total': len(descriptors), 'label': row['label'], 'status': row['status'],
                      'reused': row.get('reused', False), 'rows': row.get('retention', {}).get('source_rows'),
                      'elapsed_s': row['elapsed_s'], 'error': row.get('error')})
    complete = [r for r in experiments if r['status'] == 'complete']
    unique_archives = {x['native']['container']: Path(x['native']['container']).stat().st_size
                       for x in descriptors if x['native']['is_archive']}
    summary = {'experiments': len(experiments), 'complete': len(complete), 'failed': len(experiments) - len(complete),
               'reused': reused, 'source_rows': sum(r['retention']['source_rows'] for r in complete),
               'saved_rows': sum(r['retention']['saved_rows'] for r in complete),
               'selected_restore_bytes': sum(r.get('source_restore_bytes', 0) for r in complete),
               'signal_parquet_bytes': sum(r['retention']['parquet_bytes'] for r in complete),
               'provided_zip_bytes': sum(unique_archives.values()), 'provided_zip_count': len(unique_archives),
               'original_files_deleted': False, 'machine_learning_used': False}
    summary['signal_fraction_of_restore'] = (summary['signal_parquet_bytes'] / summary['selected_restore_bytes']) if summary['selected_restore_bytes'] else None
    manifest = {'schema_version': 'raw_database_v1', 'generated_at': _now(), 'experiments': experiments,
                'storage_policy': policy, 'summary': summary, 'source_config': str(config_path),
                'import_id': digest_json({'experiments': experiments, 'storage': policy})[:24]}
    _catalog(output, experiments, manifest)
    # User-edited registration is preserved on subsequent imports. New default
    # identities are separately exported so an explicit merge remains reviewable.
    registry_rows = [{k: v for k, v in row.items() if not isinstance(v, (dict, list))} for row in experiments]
    registry = pd.DataFrame(registry_rows)
    temporary = output / 'experiments.generated.csv.tmp'
    registry.to_csv(temporary, index=False, encoding='utf-8-sig')
    temporary.replace(output / 'experiments.generated.csv')
    if not (output / 'experiments.csv').exists():
        registry.to_csv(output / 'experiments.csv', index=False, encoding='utf-8-sig')
    _atomic_json(output / 'database_manifest.json', manifest)
    _atomic_json(output / 'storage_report.json', summary)
    from .database_report import write_database_report
    write_database_report(output, manifest)
    return manifest


def query_feature_table(database, table='cycles', *, experiment_ids=None, columns=None):
    """Read only current, verified feature partitions for the selected experiments."""
    if table not in {'cycles', 'pulses', 'quality', 'diagnostics', 'life_summary'}:
        raise ValueError('Unknown database feature table')
    root = Path(database).resolve()
    manifest = json.loads((root / 'database_manifest.json').read_text(encoding='utf-8'))
    rows = []
    selected = set(experiment_ids) if experiment_ids is not None else None
    for experiment in manifest['experiments']:
        if experiment['status'] != 'complete' or selected is not None and experiment['experiment_id'] not in selected:
            continue
        run = Path(experiment['run_path']).resolve()
        if not run.is_relative_to(root / 'runs'):
            raise ValueError('Database run must remain under its runs directory')
        metadata = json.loads((run / 'run_manifest.json').read_text(encoding='utf-8'))
        relative = f'gold/{table}.parquet'
        if sha256_file(run / relative) != metadata['artifact_sha256'][relative]:
            raise ValueError('Feature artifact was modified')
        frame = pd.read_parquet(run / relative, columns=columns)
        if not frame.empty:
            frame.insert(0, 'experiment_id', experiment['experiment_id'])
            frame.insert(1, 'dataset_id', experiment['dataset_id'])
            frame.insert(2, 'rpt_stage', experiment.get('rpt_stage', ''))
            rows.append(frame)
    return _joined(rows)
