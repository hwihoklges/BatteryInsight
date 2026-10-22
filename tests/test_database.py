import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pandas as pd
import pytest

from battery_insight.database import import_database, query_feature_table, scan_database_sources
from battery_insight.ingest import sha256_file
from test_pne import row


def setup_source(tmp_path, *, archive=False):
    raw_dir = tmp_path / 'raw'
    restore = raw_dir / 'M01Ch001[001]' / 'Restore'
    restore.mkdir(parents=True)
    text = ''.join(row(i, step=1 if i <= 3 else 2, kind=1 if i <= 3 else 2,
                       end=i in {3, 6}, **{'18': 1, '13': ((i - 1) % 3 + 1) * 100}) for i in range(1, 7))
    raw = restore / 'ch01_SaveData0001.csv'
    raw.write_text(text, encoding='utf-8')
    container = raw_dir
    if archive:
        container = tmp_path / 'raw.zip'
        with zipfile.ZipFile(container, 'w') as z:
            z.write(raw, raw.relative_to(raw_dir))
    config = {'schema_version': 1, 'batch_rows': 2, 'sources': [
        {'path': str(container), 'test_type': 'RPT', 'rpt_stage': 'BOL', 'dataset_id': 'sample'}]}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    return path, config, raw


def test_database_catalog_selected_parquet_provenance_and_readonly_reuse(tmp_path):
    path, config, raw = setup_source(tmp_path)
    before = sha256_file(raw)
    output = tmp_path / 'database'
    result = import_database(path, output)
    assert result['summary']['complete'] == 1 and result['summary']['failed'] == 0
    experiment = result['experiments'][0]
    signal = pd.read_parquet(experiment['raw_path'])
    assert len(signal) == 6 and signal.source_row.tolist() == list(range(1, 7))
    assert signal.source_member_row.tolist() == list(range(1, 7))
    assert result['storage_policy']['profile'] == 'voltage_profiles'
    assert 'current_a' not in signal
    assert signal.voltage_v.iloc[0] == 3743518 / 1e6
    assert signal.capacity_ah.iloc[0] == 1229 / 1e9
    assert experiment['retention']['future_recalculation']['all_source_rows_retained'] is True
    assert experiment['retention']['future_recalculation']['current_column_retained'] is False
    assert not any(c.startswith('pne_raw_') for c in signal)
    assert not list(Path(experiment['run_path']).rglob('*.csv'))
    assert not (Path(experiment['run_path']) / 'bronze').exists()
    assert sha256_file(raw) == before
    with sqlite3.connect(output / 'catalog.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM experiments').fetchone()[0] == 1
        assert db.execute('SELECT identity_status FROM cells').fetchone()[0] == 'provisional_channel_identity'
    features = query_feature_table(output, 'cycles')
    assert len(features) == 1 and features.rpt_stage.tolist() == ['BOL']
    second = import_database(path, output)
    assert second['summary']['reused'] == 1
    assert second['experiments'][0]['run_path'] == experiment['run_path']


def test_features_only_does_not_change_gold_values_or_keep_signal(tmp_path):
    path, config, _ = setup_source(tmp_path)
    first = import_database(path, tmp_path / 'curves')
    config['storage'] = {'profile': 'features_only'}
    path.write_text(json.dumps(config), encoding='utf-8')
    second = import_database(path, tmp_path / 'features')
    assert second['summary']['saved_rows'] == 0 and second['summary']['source_rows'] == 6
    assert not second['experiments'][0]['raw_path']
    pd.testing.assert_frame_equal(pd.read_parquet(Path(first['experiments'][0]['run_path']) / 'gold/cycles.parquet'),
                                  pd.read_parquet(Path(second['experiments'][0]['run_path']) / 'gold/cycles.parquet'))


def test_curves_profile_retains_current_and_same_gold_values_as_default(tmp_path):
    path, config, _ = setup_source(tmp_path)
    voltage = import_database(path, tmp_path / 'voltage')
    config['storage'] = {'profile': 'curves'}
    path.write_text(json.dumps(config), encoding='utf-8')
    curves = import_database(path, tmp_path / 'curves')
    signal = pd.read_parquet(curves['experiments'][0]['raw_path'])
    assert signal.current_a.iloc[0] == 4913830 / 1e9
    assert len(signal) == 6
    for name in ('cycles', 'pulses', 'quality', 'diagnostics', 'life_summary'):
        pd.testing.assert_frame_equal(
            pd.read_parquet(Path(voltage['experiments'][0]['run_path']) / 'gold' / (name + '.parquet')),
            pd.read_parquet(Path(curves['experiments'][0]['run_path']) / 'gold' / (name + '.parquet')),
        )


def test_new_source_bytes_with_preserved_stat_make_a_new_revision(tmp_path):
    path, _, raw = setup_source(tmp_path)
    first = import_database(path, tmp_path / 'db')
    old_stat = raw.stat()
    raw.write_text(raw.read_text().replace('3743518', '3743519'), encoding='utf-8')
    os.utime(raw, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert raw.stat().st_size == old_stat.st_size
    second = import_database(path, tmp_path / 'db')
    assert second['summary']['complete'] == 1 and second['summary']['reused'] == 0
    assert second['experiments'][0]['run_path'] != first['experiments'][0]['run_path']


def test_include_is_part_of_derived_identity_and_requires_boolean(tmp_path):
    path, config, _ = setup_source(tmp_path)
    first = import_database(path, tmp_path / 'db')
    config['sources'][0]['metadata'] = {'include': False}
    path.write_text(json.dumps(config), encoding='utf-8')
    second = import_database(path, tmp_path / 'db')
    assert second['experiments'][0]['run_path'] != first['experiments'][0]['run_path']
    assert not query_feature_table(tmp_path / 'db').include.any()
    config['sources'][0]['metadata']['include'] = 'false'
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='boolean'):
        import_database(path, tmp_path / 'bad')


def test_overlapping_input_roots_cannot_duplicate_a_channel(tmp_path):
    _, config, raw = setup_source(tmp_path)
    config['sources'].append(dict(config['sources'][0], path=str(raw.parent.parent)))
    with pytest.raises(ValueError, match='same Restore'):
        scan_database_sources(config)


def test_two_channel_selections_from_same_zip_are_allowed(tmp_path):
    _, config, raw = setup_source(tmp_path, archive=True)
    with zipfile.ZipFile(config['sources'][0]['path'], 'a') as z:
        z.writestr('M01Ch002[002]/Restore/ch02_SaveData0001.csv', raw.read_text())
    config['sources'][0]['channels'] = ['M01Ch001[001]']
    config['sources'].append(dict(config['sources'][0], channels=['M01Ch002[002]']))
    assert len(scan_database_sources(config)) == 2


def test_conflicting_design_identity_is_rejected_before_output(tmp_path):
    path, config, raw = setup_source(tmp_path, archive=True)
    with zipfile.ZipFile(config['sources'][0]['path'], 'a') as z:
        z.writestr('M01Ch002[002]/Restore/ch02_SaveData0001.csv', raw.read_text())
    config['sources'][0]['channel_overrides'] = {
        'M01Ch001[001]': {'design_id': 'same', 'cathode_loading_mg_cm2': 4.5},
        'M01Ch002[002]': {'design_id': 'same', 'cathode_loading_mg_cm2': 5.5}}
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='Conflicting'):
        import_database(path, tmp_path / 'db')
    assert not (tmp_path / 'db').exists()


def test_bad_channel_keeps_original_and_publishes_no_completed_run(tmp_path):
    path, _, raw = setup_source(tmp_path)
    raw.write_text('1,2,3\n')
    result = import_database(path, tmp_path / 'db')
    assert result['summary']['failed'] == 1 and result['summary']['complete'] == 0
    assert result['experiments'][0]['status'] == 'failed'
    assert raw.read_text() == '1,2,3\n'
    assert not list((tmp_path / 'db/runs').glob('*/run_manifest.json'))


def test_registry_user_edits_are_not_overwritten_by_import(tmp_path):
    path, _, _ = setup_source(tmp_path)
    import_database(path, tmp_path / 'db')
    registry = tmp_path / 'db/experiments.csv'
    frame = pd.read_csv(registry, keep_default_na=False)
    frame['design_id'] = 'USER_DESIGN'
    frame.to_csv(registry, index=False, encoding='utf-8-sig')
    before = registry.read_bytes()
    import_database(path, tmp_path / 'db')
    assert registry.read_bytes() == before


def test_tampered_feature_file_is_not_queried(tmp_path):
    path, _, _ = setup_source(tmp_path)
    manifest = import_database(path, tmp_path / 'db')
    cycles = Path(manifest['experiments'][0]['run_path']) / 'gold/cycles.parquet'
    cycles.write_bytes(b'invalid')
    with pytest.raises(ValueError, match='modified'):
        query_feature_table(tmp_path / 'db')
