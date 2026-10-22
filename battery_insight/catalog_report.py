"""Searchable local metric dictionary; no data analysis or model execution."""
from __future__ import annotations
import html
import json
from pathlib import Path
import pandas as pd


def write_catalog(frame: pd.DataFrame, directory: str | Path) -> dict:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    if frame.duplicated(["namespace", "metric_id"]).any():
        raise ValueError("Catalog contains duplicate namespace/metric identities")
    frame.to_csv(destination / "metrics_catalog.csv", encoding="utf-8-sig", index=False)
    frame.to_parquet(destination / "metrics_catalog.parquet", compression="zstd", index=False)
    records = json.loads(frame.to_json(orient="records", force_ascii=False))
    payload = json.dumps(records, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    meta = {"total_rows": len(frame),
            "metadata_rows": int(frame["is_metadata"].sum()) if "is_metadata" in frame else 0,
            "unit_status_counts": frame["unit_status"].value_counts().to_dict(),
            "by_namespace": frame.groupby("namespace").size().to_dict(),
            "by_group": frame.groupby("group").size().to_dict(),
            "aim_scope": "unverified: actual AIM specification not identified",
            "scope": "declared core, static literal references, legacy declarations, and optional actual export headers; dynamic runtime completeness not guaranteed",
            "machine_learning": "not_required_not_executed",
            "raw_data_analysis_executed": False}
    (destination / "catalog_manifest.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    page = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>전기화학 지표 사전</title>
<style>
:root{font:15px/1.55 system-ui,"Malgun Gothic",sans-serif;color:#203447;background:#f4f7fa}body{margin:0}header,main{max-width:1700px;margin:auto;padding:28px}header{background:#173c48;color:white;max-width:none}header>div{max-width:1700px;margin:auto}h1{font-size:30px;margin:0 0 8px}h2{font-size:18px}p{margin:8px 0}.label{letter-spacing:.12em;color:#8ddbd2;font-size:12px}.notice{border-left:4px solid #d49321;background:#fff5df;padding:12px 16px;border-radius:5px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;position:sticky;top:0;background:#f4f7fa;padding:14px 0;z-index:2}input,select{font:inherit;border:1px solid #bacbd5;padding:9px 12px;border-radius:6px}input[type=search]{min-width:280px;flex:1}button{font:inherit;cursor:pointer;background:#dceee9;color:#174d47;border:0;padding:10px;border-radius:6px}.card{background:white;border-radius:10px;padding:16px;box-shadow:0 2px 7px #2342}.tablewrap{overflow:auto;max-height:72vh}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:10px;border-bottom:1px solid #dbe5ea;vertical-align:top}th{background:#eaf3f4;position:sticky;top:0;white-space:nowrap}td.code{font:12px/1.5 ui-monospace,monospace;min-width:220px;overflow-wrap:anywhere}td.desc{min-width:250px;max-width:460px}td.provenance{min-width:260px;max-width:380px;overflow-wrap:anywhere;font-size:12px}.muted{color:#657b87}.badge{display:inline-block;background:#e8eff4;border-radius:4px;padding:2px 6px;font-size:11px}.unknown{background:#fff1d3}a{color:#057066}.count{font-weight:bold;margin:8px 0}details{max-width:440px}summary{cursor:pointer}.instructions{display:flex;gap:16px;flex-wrap:wrap}.instructions>p{flex:1;min-width:250px}</style></head><body>
<header><div><div class="label">LOCAL ELECTROCHEMICAL METRIC DICTIONARY</div><h1>전기화학 지표 사전</h1><p>기존 코드의 지표명 · 정의 · 단위 · 분류 · 중복 관계 · 계산 위치</p><p>머신러닝 학습·예측 없이, 지표 목록과 출처를 정리했습니다.</p></div></header>
<main><div class="notice"><b>AIM 범위 확인 대기</b> — 이 목록은 확인한 기존 코드에서 추출했습니다. 별도의 AIM 명세가 없어 모든 항목이 AIM 지표라고 확정하지 않았습니다. 미확인 단위와 정적 참조 항목은 그대로 표시합니다.</div>
<div class="instructions"><p><b>출처별로 구분</b><br>같은 이름이라도 Studio와 cyclediag의 산식·단위가 다를 수 있습니다.</p><p><b>기준과 변환 확인</b><br>delta, inc, ratio, alias는 원 지표와 독립 증거로 중복 계산하지 않습니다.</p><p><b>계산 여부와 분리</b><br>이 표에는 raw 수치 계산이나 실제 셀 이상 판정 결과가 포함되지 않습니다.</p></div>
<p><a href="metrics_catalog.csv">전체 CSV · Excel에서 열기</a> &nbsp; <a href="metrics_catalog.parquet">Parquet</a> &nbsp; <a href="catalog_manifest.json">목록 범위</a></p>
<div class="toolbar"><input id="search" type="search" placeholder="지표명·정의·원인·소스 검색" aria-label="지표 검색"><select id="namespace" aria-label="코드 출처"><option value="">모든 출처</option></select><select id="group" aria-label="지표 분류"><option value="">모든 분류</option></select><select id="unit" aria-label="단위 확인"><option value="">모든 단위 상태</option></select><label><input id="meta" type="checkbox">메타데이터 포함</label><button id="reset">필터 초기화</button></div>
<div id="count" class="count"></div><div class="card tablewrap"><table><thead><tr><th>지표 · 출처</th><th>분류 · 역할</th><th>단위 · 확인상태</th><th>정의·해석</th><th>기준·중복·변환</th><th>코드 근거</th></tr></thead><tbody id="body"></tbody></table></div><p class="muted">정적 코드에서 확인 가능한 이름과 선택된 export 헤더를 모은 사전입니다. 실제 export 전체와 대조하기 전에는 동적 생성 지표의 완전성을 보증하지 않습니다.</p>
</main><script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent);const ids=['search','namespace','group','unit','meta'];const el=Object.fromEntries(ids.map(k=>[k,document.getElementById(k)]));
for(const [id,key] of [['namespace','namespace'],['group','group'],['unit','unit_status']]){for(const value of [...new Set(data.map(r=>r[key]).filter(Boolean))].sort()){const o=document.createElement('option');o.value=value;o.textContent=value;el[id].append(o)}}
function textCell(row, cls, ...pieces){const td=document.createElement('td');td.className=cls;for(const [i,p] of pieces.entries()){if(i)td.append(document.createElement('br'));td.append(document.createTextNode(String(p??'')))}row.append(td);return td}
function render(){const terms=el.search.value.toLowerCase().trim().split(/\s+/).filter(Boolean);const filtered=data.filter(r=>(!el.namespace.value||r.namespace===el.namespace.value)&&(!el.group.value||r.group===el.group.value)&&(!el.unit.value||r.unit_status===el.unit.value)&&(el.meta.checked||!r.is_metadata)&&terms.every(t=>Object.values(r).join(' ').toLowerCase().includes(t)));document.getElementById('count').textContent=`표시 ${filtered.length}개 / 전체 ${data.length}개`;const body=document.getElementById('body');body.replaceChildren();for(const r of filtered){const tr=document.createElement('tr');textCell(tr,'code',r.metric_id,r.namespace);textCell(tr,'',r.group,r.role);textCell(tr,'',r.unit||'확인 필요',r.unit_status);textCell(tr,'desc',r.description_ko,r.required_data?'필요 데이터: '+r.required_data:'',r.validity_notes?'조건: '+r.validity_notes:'');textCell(tr,'desc',r.family?'family: '+r.family:'',r.retired_alias_of?'→ '+r.retired_alias_of:'',r.alias_formula||'',r.canonical_base&&r.canonical_base!==r.metric_id?'base: '+r.canonical_base:'',r.replaced_metric_ids?'목록에서 제외한 표현: '+r.replaced_metric_ids:'',r.selection_conditions?'선택 조건: '+r.selection_conditions:'');const td=textCell(tr,'provenance',r.source_path,r.source_line?'line '+r.source_line:'',r.origin);const detail=document.createElement('details');const summary=document.createElement('summary');summary.textContent='해시·전체 근거';const pre=document.createElement('p');pre.textContent=r.source_sha256+' '+r.evidence_json;detail.append(summary,pre);td.append(detail);body.append(tr)}}
ids.forEach(id=>el[id].addEventListener('input',render));document.getElementById('reset').onclick=()=>{ids.forEach(id=>id==='meta'?el[id].checked=false:el[id].value='');render()};render();
</script></body></html>'''
    (destination / "metrics_catalog.html").write_text(page.replace("PAYLOAD", payload), encoding="utf-8")
    return meta
