"""Small local inventory report; never embeds raw measurements or writes sources."""
from __future__ import annotations

from html import escape
from pathlib import Path


def write_database_report(output, manifest):
    root = Path(output)
    summary, policy = manifest['summary'], manifest['storage_policy']
    ratio = summary.get('signal_fraction_of_restore')
    reduction = f'{100 * (1 - ratio):.1f}%' if ratio is not None else '—'
    rows = []
    for experiment in manifest['experiments']:
        retention = experiment.get('retention', {})
        status = experiment['status']
        path = Path(experiment['run_path']) if status == 'complete' else None
        link = (f'<a href="{escape(path.relative_to(root).as_posix())}/run_manifest.json">완료</a>'
                if path else escape(experiment.get('error', status)))
        rows.append('<tr>' + ''.join(f'<td>{value}</td>' for value in (
            escape(experiment['dataset_id']), escape(experiment['source_channel']),
            escape(experiment['test_type']), escape(experiment.get('rpt_stage', '')),
            f"{retention.get('source_rows', 0):,}", f"{retention.get('saved_rows', 0):,}",
            f"{retention.get('parquet_bytes', 0) / 1_000_000:.3f}", link)) + '</tr>')
    saved = sorted(set(column for row in manifest['experiments'] for column in row.get('retention', {}).get('saved_columns', [])))
    omitted_current = bool(summary['saved_rows']) and 'current_a' not in saved
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>전압 프로파일 데이터베이스</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 20px;color:#163b41;background:#f4f8f8}}
h1{{font-size:28px}}a{{color:#126e74}}.cards{{display:flex;flex-wrap:wrap;gap:12px}}.card{{padding:18px 24px;background:white;border:1px solid #d6e4e4;border-radius:12px}}
.card b{{display:block;font-size:25px}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #dae5e5}}.scroll{{overflow:auto}}code{{background:#e4eded;padding:2px 5px}}.note{{padding:16px;background:#e6f0ee;border-radius:8px}}</style>
<h1>전압 프로파일과 지표 데이터베이스</h1>
<p>모든 원본은 그대로 두고, 선택한 시계열과 계산 지표를 별도 Parquet에 저장합니다. 머신러닝은 사용하지 않습니다.</p>
<div class="cards"><div class="card">완료 채널<b>{summary['complete']} / {summary['experiments']}</b></div>
<div class="card">원본 기록 → 저장 기록<b>{summary['source_rows']:,} → {summary['saved_rows']:,}</b></div>
<div class="card">시계열 Parquet<b>{summary['signal_parquet_bytes'] / 1_000_000:.2f} MB</b></div>
<div class="card">선택 Restore 대비 감소<b>{reduction}</b></div></div>
<p>비교 기준은 ZIP을 푼 뒤 실제로 읽은 Restore {summary['selected_restore_bytes'] / 1_000_000:.2f} MB입니다.
이미 압축된 ZIP 크기나 Gold 지표·인덱스·이전 버전을 포함한 전체 DB 크기와의 비율은 아닙니다.</p>
<p>저장 프로필 <code>{escape(policy['profile'])}</code> · Zstandard {policy['compression_level']} · 수치 반올림 없음 · 저장용 다운샘플링 없음</p>
<p class="note">지표는 저장 열을 줄이기 전의 원본으로 계산합니다.
{'이 프로필은 전류를 저장하지 않습니다. 전류 의존 지표를 새로 계산하려면 원본을 다시 읽어야 합니다.' if omitted_current else '저장한 열 범위에서 재분석할 수 있습니다.'}
CC/CV·SOC·온도가 확인되지 않으면 필요한 지표는 결측 사유와 함께 남깁니다. 확인되지 않은 AIM 공식 전체를 계산했다고 해석하지 않습니다.</p>
<p><a href="workbench/dashboard/index.html">비교 작업실 미리보기</a> · <a href="experiments.csv">설계·배치·물리 셀 등록표</a> ·
<a href="storage_report.json">저장 집계</a> · <a href="database_manifest.json">실험 목록과 출처</a> · <a href="import_config.json">변환 설정</a></p>
<p><code>start_workbench.cmd</code>를 실행하면 지표 선택·곡선 조회·비교 재계산을 사용할 수 있습니다.
등록표의 <code>physical_cell_id</code>, 설계·배치·조건이 확인된 셀끼리 평균·표준편차·n을 표시합니다. 미확인 채널을 임의로 같은 설계나 독립 셀로 묶지 않습니다.</p>
<h2>저장한 시계열 열</h2><p>{escape(', '.join(saved)) or '시계열 저장 안 함'}</p>
<h2>입력별 변환 결과</h2><div class="scroll"><table><thead><tr><th>자료</th><th>채널</th><th>종류</th><th>RPT 시점</th><th>원본 행</th><th>저장 행</th><th>Parquet MB</th><th>상태</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p>생성 시각: {escape(manifest['generated_at'])}. 기록·지표의 원본 연결은 각 run manifest에서 확인할 수 있습니다.</p></html>'''
    temporary = root / 'index.html.tmp'
    temporary.write_text(body, encoding='utf-8')
    temporary.replace(root / 'index.html')
