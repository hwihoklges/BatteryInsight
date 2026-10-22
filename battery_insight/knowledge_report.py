"""Local review artifacts and an intake checklist for document-backed metadata."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path


REQUIREMENTS = [
    {"field": "document / evidence", "label": "장표와 근거 위치", "priority": "등록 필수", "detail": "원본 파일, 슬라이드/페이지/표의 위치, 해당 문구를 기록합니다. 그림만 있으면 판독 대기로 남깁니다."},
    {"field": "experiment_name", "label": "실험명·목적", "priority": "확정 필수", "detail": "예: 구동압 비교, WIP 전후, RPT BOL. 제목이 넓은 주제이면 각 표·샘플의 실제 시험 목적을 별도로 확인합니다."},
    {"field": "sample_id / sample_name", "label": "샘플 식별자·표기명", "priority": "확정 필수", "detail": "장표에 쓰인 이름과 내부 식별자를 구분합니다. 동일 샘플의 약어·별칭은 명시적으로 등록하며, 비슷한 이름을 자동으로 합치지 않습니다."},
    {"field": "test_date / date_kind", "label": "날짜와 그 의미", "priority": "raw 후보 연결 필수", "detail": "시험 시작일·종료일·제조일·보고일을 구분합니다. 연결 키에는 확인된 시험 시작일(test_start)만 사용합니다. 연도가 불명확한 6자리 날짜는 확인 전 확정하지 않습니다."},
    {"field": "namespace / set_no", "label": "세트번호와 번호 사용 범위", "priority": "raw 후보 연결 필수", "detail": "세트번호는 문자열로 보관해 001과 1을 구분합니다. 프로젝트·장비 등 번호가 유일해지는 범위(namespace)를 함께 확인합니다."},
    {"field": "test_type / rpt_stage", "label": "시험 종류·RPT 시점", "priority": "raw 후보 연결 필수", "detail": "Cycle/RPT/DCIR 등의 종류, BOL/100cyc/WIP 전후 등 시점을 따로 기록합니다. RPT 내부 cycle 번호를 수명 cycle로 바꾸지 않습니다."},
    {"field": "channel_id / raw key", "label": "샘플과 채널의 대응표", "priority": "다중 후보 해소", "detail": "한 세트에 여러 샘플이 있으면 채널·셀 번호·원본 경로 등 대응 근거가 필요합니다. 날짜와 세트가 같아도 일대일 연결을 가정하지 않습니다."},
    {"field": "physical_cell_id", "label": "물리 셀 ID·반복 시험 여부", "priority": "셀 평균·이력 연결", "detail": "같은 샘플명으로 만든 서로 다른 셀과 같은 셀의 후속 RPT를 구분합니다. 실제 동일 셀이라는 근거가 있을 때만 채웁니다."},
    {"field": "design_id / batch_id", "label": "설계·제조 배치", "priority": "설계 비교·셀 평균", "detail": "동일 설계와 제조 배치를 확인합니다. 샘플명이 같다는 이유만으로 같은 설계·배치로 지정하지 않습니다."},
    {"field": "metadata: electrode", "label": "활물질·전극 구성·로딩", "priority": "확장 정보", "detail": "양극/음극 소재, 전해질·도전재·바인더, 조성 기준(전극 전체/활물질만), 로딩의 단면/양면과 mg/cm² 또는 mAh/cm² 단위를 기록합니다."},
    {"field": "metadata: conditions", "label": "시험 조건", "priority": "비교 가능성 판단", "detail": "온도 °C, 압력 MPa, 충방전 C-rate, 전압 범위, formation 조건과 protocol을 구분합니다. 원본 프로토콜 식별자를 장표의 약칭으로 덮어쓰지 않습니다."},
    {"field": "status / reviewer / reason", "label": "확정 여부·검토 이력", "priority": "확정 필수", "detail": "draft/confirmed 상태, 검토자·이유, 근거 ID, 버전을 남깁니다. 수정 이력은 보존하고, 기존 연결에 쓰인 기록이 바뀌면 재검토합니다."},
]

STYLE = """body{font-family:system-ui,'Malgun Gothic',sans-serif;background:#f3f6f8;color:#173044;line-height:1.65;margin:0}main{max-width:1200px;margin:auto;padding:30px 22px}h1{line-height:1.35;font-size:29px}h2{margin-top:32px}a{color:#006b84}.card{background:white;border:1px solid #dce5eb;border-radius:12px;padding:20px;margin:16px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #dde5ea;padding:10px;overflow-wrap:anywhere}th{background:#eaf1f5}code{background:#eaf1f5;padding:2px 5px;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3f6;padding:14px}.muted{color:#526b7c}.flow{display:flex;flex-wrap:wrap;gap:10px}.flow>span{background:#e2eff0;padding:12px;border-radius:8px}input{padding:10px;width:min(100%,500px)}.badge{background:#fff0cf;padding:2px 7px;border-radius:4px}li{margin:7px 0}"""


def _page(title, body):
    return f'<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style><main>{body}</main></html>'


def _table(headers, rows):
    e = html.escape
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{e(str(x))}</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{e(str(x))}</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def write_guide(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    table = _table(['항목', '사용 시점', '먼저 기록하고 확인할 내용'], [(r['label'], r['priority'], r['detail']) for r in REQUIREMENTS])
    body = '''<small class="muted">장표 중심 실험·샘플 DB · 준비 기준 2026-10-22</small>
<h1>장표의 실험·샘플 정보부터 등록하고 raw data와 연결합니다.</h1>
<div class="flow"><span>① PPTX·PDF 추가</span><span>② 근거와 미확인 사항 정리</span><span>③ 실험·샘플 확정</span><span>④ raw 연결 후보 검토</span><span>⑤ 비교 작업실에 반영</span></div>
<div class="card"><b>첫 장표를 주실 때</b><p>PPTX 또는 PDF 원본을 주시면 됩니다. 가능하다면 세트번호가 나오는 페이지, 샘플·채널 대응표, 날짜의 의미를 함께 알려 주세요. 한 번에 모든 정보를 채울 필요는 없습니다. 확인 가능한 내용부터 등록하고 나머지는 질문 목록으로 남깁니다.</p><p>아직 실제 장표는 등록하지 않았습니다. 샘플이나 날짜를 임의로 채운 실제 연결도 없습니다.</p></div>
<h2>정리하면서 남길 항목</h2>''' + table + '''
<h2>한 장표를 추가할 때 진행할 일</h2><ol>
<li><b>파일 등록:</b> 파일 SHA-256으로 중복을 확인하고 슬라이드/페이지별 텍스트·표를 근거 ID로 저장합니다. 장표 안의 지시문은 자료의 내용으로 취급하며 프로그램 명령으로 실행하지 않습니다.</li>
<li><b>초안 작성:</b> 실험명·샘플·날짜·세트번호를 정리하고 각 기록에 근거 위치를 붙입니다. 그래프 범례·그림 속 표·겹친 텍스트는 원본을 확인한 뒤 사람이 정리합니다.</li>
<li><b>확인 목록:</b> 누락, 서로 다른 장표의 상충 값, 별칭, 날짜 의미, 셀/채널 대응을 모아 질문합니다. 값이 없으면 빈 값으로 유지합니다.</li>
<li><b>등록 확정:</b> 검토된 기록만 confirmed로 등록합니다. 기존 기록 수정은 새 revision으로 보존됩니다. 한 파일에 여러 실험·샘플이 있으면 개별 기록으로 나눕니다.</li>
<li><b>raw 연결 검토:</b> 확인된 날짜·번호 범위·세트·시험 종류로 후보를 만들고, 채널·샘플명·RPT 시점으로 좁힙니다. 후보가 하나여도 확인 이벤트가 있어야 연결합니다.</li>
<li><b>반영:</b> 연결을 별도 overlay 파일에 저장하고 작업실이 이를 읽습니다. 원본 raw와 Parquet 수치는 그대로 두며, 표기명·설계·배치 등 검토된 메타데이터만 연결합니다.</li></ol>
<h2>데이터를 나누는 기준</h2><div class="card">
<p><b>문서 → 근거 → 실험/샘플 기록 → 연결 검토 → raw 시험 → 지표</b> 순서로 출처를 추적합니다. 장표의 샘플 ID, 물리 셀 ID, 기존 raw 시험 ID는 서로 다른 키입니다.</p>
<p>실험·샘플 DB는 <code>knowledge.sqlite3</code>에, 대용량 원곡선과 지표는 기존 Parquet DB에 저장합니다. 문서 바이너리는 DB에 복제하지 않고 원본 경로·파일 해시·추출 근거를 보관하므로 장표 원본도 보관해 주세요.</p>
<p>샘플명은 셀 평균 기준이 아닙니다. 기존처럼 같은 설계·배치·조건·관측 시점 안에서 확인된 물리 셀별 동일 비중으로 평균을 계산합니다. 미확인 셀 ID를 이름으로 만들어 채우지 않습니다.</p></div>
<h2>이번 기초 구현과 다음 단계</h2>''' + _table(['단계', '산출물', '범위'], [
        ('현재', 'PPTX/PDF 텍스트·표 추출, SQLite 등록, 근거/수정 이력', '장표 원본을 한 개씩 추가. Excel·이미지는 보조 입력. 그림은 판독 대기.'),
        ('현재', 'raw 키 템플릿·후보 매칭·명시적 확정/해제·읽기 전용 작업실 연결', '날짜/세트번호 추측으로 자동 확정하지 않음. 오래된 연결은 재검토.'),
        ('첫 장표 수령', '용어·번호 체계, 샘플/채널 대응, 확인 질문 목록', '실제 문서로 추출과 의미를 대조하고 필요한 필드 사전을 보완.'),
        ('후속', '스캔 PDF OCR, 복잡한 표·범례 해석, 화면에서 검토·승인', '현재 자동 OCR/그래프 수치 디지털화/의미 자동 확정은 구현 범위 밖.'),
        ('후속', '여러 장표의 설계 변경 이력과 후속 RPT 연결', '명시적인 같은 셀·배치 근거가 있는 경우에만 병합.'),
    ]) + '''<h2>명령줄 사용 예</h2><p class="muted">일반 사용 시에는 장표를 전달하면 이 절차에 따라 추가합니다. 아래 명령은 로컬 실행·재현용입니다. 경로는 실제 파일로 바꿉니다.</p><pre>python -m pip install -e ".[documents]"
python -m battery_insight knowledge-init --out ../experiment_knowledge
python -m battery_insight knowledge-intake --registry ../experiment_knowledge/knowledge.sqlite3 --documents "실험장표.pptx" --out ../experiment_knowledge/review
python -m battery_insight knowledge-records --registry ../experiment_knowledge/knowledge.sqlite3 --records records.json --reviewer "검토자" --reason "슬라이드 표 및 채널 대응 확인"
python -m battery_insight knowledge-match --registry ../experiment_knowledge/knowledge.sqlite3 --database ../raw_database --raw-keys raw_keys.csv --out ../experiment_knowledge/matching
python -m battery_insight knowledge-link --registry ../experiment_knowledge/knowledge.sqlite3 --database ../raw_database --proposals ../experiment_knowledge/matching/proposals.json --decisions decisions.json --reviewer "검토자" --reason "시험 시작일·세트·채널 확인" --out ../experiment_knowledge/links.json
python -m battery_insight knowledge-export --registry ../experiment_knowledge/knowledge.sqlite3 --out ../experiment_knowledge/review</pre>
<p>작업실 설정에 <code>"knowledge_links": "연결 파일의 상대 또는 절대 경로"</code>를 추가하면 확인된 연결을 읽습니다. 연결이 없어도 기존 작업실은 그대로 동작합니다. 장표 해석 수정, raw 내용 변경, 기존 등록표와 충돌하는 정보는 자동으로 덮어쓰지 않습니다.</p>
<h2>알아둘 한계</h2><ul><li>PDF의 텍스트 순서가 표의 행/열 관계를 보장하지 않습니다. PPTX에서 그림으로 붙여 넣은 표, 스캔 PDF, 그래프 수치는 원본 확인이 필요합니다.</li><li>현재 추출기는 문구와 위치를 수집합니다. 문구를 실험 사실로 판단하는 작업은 검토된 등록 기록 단계에서 수행합니다.</li><li>공식 AIM 정의가 새로 확인되는 기능은 아닙니다. 장표의 결과 요약을 raw에서 계산한 지표처럼 저장하지 않습니다.</li></ul>'''
    output.write_text(_page('장표 등록 준비사항과 실행 계획', body), encoding='utf-8')
    return output


def write_templates(output):
    """Create examples once; never replace a user-edited intake template."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    templates = {
        'records.template.json': {'records': [{'record_id': '', 'status': 'draft', 'sample_id': '', 'sample_name': '',
            'experiment_name': '', 'namespace': '', 'test_date': '', 'date_kind': 'unknown', 'set_no': '',
            'manufacture_date': '', 'report_date': '', 'test_end_date': '',
            'test_type': '', 'rpt_stage': '', 'channel_id': '', 'physical_cell_id': '', 'design_id': '', 'batch_id': '',
            'evidence_ids': [], 'metadata': {}}]},
        'decisions.template.json': {'decisions': []},
        'decisions.example.json': {'decisions': [{'experiment_id': '<후보의 experiment_id>', 'record_id': '<선택한 record_id>',
            'decision': 'confirm', 'expected_record_revision': 1, 'source_sha256': '<후보의 원본 SHA-256>',
            'reason': '<날짜·세트·샘플·채널을 확인한 근거>'}]},
        'required_information.json': {'schema_version': 'knowledge_intake_checklist_v1', 'fields': REQUIREMENTS,
                                      'unresolved_policy': 'Keep unknown; do not infer from filename, nearby table rows or sample name.'},
    }
    for name, value in templates.items():
        path = output / name
        if not path.exists():
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path = output / 'raw_keys.template.csv'
    if not path.exists():
        with path.open('w', newline='', encoding='utf-8-sig') as stream:
            csv.writer(stream).writerow(['experiment_id', 'test_date', 'date_kind', 'set_no', 'namespace', 'sample_name'])
    write_guide(output / 'intake_guide.html')


def write_review(snapshot, output, *, proposals=None):
    """Write escaped review HTML, full JSON, and text-preserving CSV exports."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'knowledge_snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    records, documents, evidence = (snapshot.get(k, []) for k in ('records', 'documents', 'evidence'))
    e = html.escape
    cols = ['record_id', 'revision', 'status', 'experiment_name', 'sample_id', 'sample_name', 'test_date', 'date_kind', 'manufacture_date', 'report_date', 'test_end_date', 'namespace', 'set_no', 'test_type', 'rpt_stage', 'channel_id', 'physical_cell_id', 'design_id', 'batch_id', 'evidence_ids', 'metadata']
    with (output / 'records.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=cols)
        writer.writeheader()
        for record in records:
            row = {key: record.get(key, '') for key in cols}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    row[key] = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    row[key] = "'" + value  # Spreadsheet-view export only; JSON remains canonical.
            writer.writerow(row)
    body = '<h1>장표 기반 실험·샘플 등록 검토</h1>'
    body += f'<p>문서 {len(documents)}개 · 근거 {len(evidence)}개 · 실험/샘플 기록 {len(records)}개 · 확정 {sum(r.get("status") == "confirmed" for r in records)}개</p>'
    body += '<p><a href="knowledge_snapshot.json">전체 근거·등록 JSON</a> · <a href="records.csv">등록표 CSV</a> · <a href="intake_guide.html">필요 정보·작업 순서</a></p>'
    if snapshot.get('raw_inventory_summary'):
        body += f'<div class="card"><b>raw 연결 준비 · {int(snapshot["raw_inventory_summary"]["experiments"])}개 시험</b><p><a href="raw_keys.csv">날짜·세트·샘플 연결 준비표 CSV</a> · <a href="raw_inventory.json">원본 시험 목록과 미확인 파일명 후보</a></p><p>시험 ID는 기존 DB와 동일합니다. 날짜의 의미·세트번호·샘플 대응을 확인한 다음 후보 매칭에 사용합니다.</p></div>'
    body += '<div class="card"><b>읽기 전용 검토표</b><p>이 화면은 자동 확정하지 않습니다. 근거를 확인한 기록과 raw 연결 검토 이벤트를 각각 등록해야 작업실에 반영됩니다. CSV는 표시용이며 원문과 ID의 기준은 JSON입니다. Excel에서 세트번호를 텍스트로 열어 선행 0을 보존하세요.</p></div>'
    if snapshot.get('intake_results'):
        body += '<h2>이번 파일 수집 결과</h2>' + _table(['입력', '상태', '검토·오류 사유'], [(r.get('source_path'), r.get('status'), json.dumps(r.get('issues', []), ensure_ascii=False)) for r in snapshot['intake_results']])
    body += '<h2>문서</h2>' + _table(['문서 ID', '파일명', '상태', '문제·검토 필요'], [(d.get('document_id'), d.get('name'), d.get('status'), json.dumps(d.get('issues', []), ensure_ascii=False)) for d in documents])
    body += '<h2>실험·샘플 기록</h2>' + _table(['기록 / 버전', '실험 / 샘플', '날짜 / 의미', '번호 범위 / 세트', '시험 / 시점 / 채널', '셀 / 설계 / 배치', '상태 / 근거'], [
        (f"{r.get('record_id')} / {r.get('revision')}", f"{r.get('experiment_name')} / {r.get('sample_name')}", f"{r.get('test_date')} / {r.get('date_kind')}", f"{r.get('namespace')} / {r.get('set_no')}", f"{r.get('test_type')} / {r.get('rpt_stage')} / {r.get('channel_id')}", f"{r.get('physical_cell_id')} / {r.get('design_id')} / {r.get('batch_id')}", f"{r.get('status')} / {', '.join(r.get('evidence_ids', []))}") for r in records])
    questions = []
    for r in records:
        missing = [key for key in ('experiment_name', 'sample_id', 'sample_name', 'test_date', 'set_no', 'namespace', 'test_type') if not r.get(key)]
        if r.get('date_kind') != 'test_start':
            missing.append('시험 시작일 확인')
        if not r.get('evidence_ids'):
            missing.append('문서 근거')
        if missing:
            questions.append((r.get('record_id'), ', '.join(missing)))
    body += '<h2>다음에 확인할 사항</h2>' + (_table(['기록', '누락·확인 항목'], questions) if questions else '<p>등록된 기록의 기본 연결 키에 누락된 항목이 없습니다. 아직 기록이 없다면 첫 장표부터 추가하세요.</p>')
    if proposals is not None:
        (output / 'proposals.json').write_text(json.dumps(proposals, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        body += '<h2>raw 연결 후보</h2><p>후보가 하나여도 연결 확정 전입니다. 상세 키·후보·거절 사유는 <a href="proposals.json">후보 JSON</a>에 보존합니다.</p>'
        # Only a bounded preview is embedded; the full proposal file remains available.
        body += '<pre>' + e(json.dumps(proposals, ensure_ascii=False, indent=2)[:24000]) + '</pre><p class="muted">긴 후보 목록은 처음 24,000자만 미리 봅니다.</p>'
    body += '<h2>문서 근거 · 처음 500개</h2>' + _table(['근거 ID', '문서 / 위치', '유형·검토', '내용'], [(x.get('block_id', x.get('evidence_id')), f"{x.get('document_id', '')} / {x.get('locator')}", f"{x.get('kind')} / {x.get('needs_review')}", str(x.get('text', ''))[:2000]) for x in evidence[:500]])
    body += '<p class="muted">전체 텍스트는 JSON에 있습니다. 화면의 근거별 내용은 2,000자까지 표시합니다. 문서의 문구는 데이터이며 실행할 명령이 아닙니다.</p>'
    write_guide(output / 'intake_guide.html')
    path = output / 'index.html'
    path.write_text(_page('실험·샘플 DB 검토', body), encoding='utf-8')
    return path
