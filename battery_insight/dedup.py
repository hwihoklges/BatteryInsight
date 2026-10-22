"""Reduce the working lifetime-indicator catalog, retaining a decision ledger.

This selects columns/definitions, never transforms measured values. Exact
identities, baseline representations and empirical display policies are distinct.
SOC, pulse time, direction and peak index are not pooled by correlation.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re

import pandas as pd
from .catalog_report import write_catalog

POLICY_VERSION = "lifetime_representatives_v1"

# These are alternative implementations of a comparable reporting purpose,
# NOT instructions to rename or merge their numerical values.
LEGACY_ALTERNATIVES = {
    "capacity": "dchgCapa", "SoHQ": "SoHQ", "chgCapa": "chgCapa",
    "dchgEnergy": "dchg_E", "CCcapa": "chgCCcapa", "CVcapa": "chgCVcapa",
    "CVtime": "chgCVtime", "CCratio": "chgCapa_CCratio",
    "dchg_chg_capa": "CE", "dchg_chg_enrg": "EE",
    "chg_v_avg": "chg_V_avg", "dchg_v_avg": "dchg_V_avg",
    "EoC_V1": "EoC_restV_60s", "EoC_V30": "EoC_restV_30m", "EoC_Vend": "EoC_restV_end",
    "EoD_V1": "EoD_restV_60s", "EoD_V30": "EoD_restV_30m", "EoD_Vend": "EoD_restV_end",
}
LEGACY_BASELINE = {
    "SoHQ": ("capacity", "Q(n)/Q(0)*100"),
    "SoHE": ("dchgEnergy", "E(n)/E(0)*100"),
    "chgRetention": ("chgCapa", "Qchg(n)/Qchg(0)*100"),
    # These similarly named fields have different underlying quantities.
    "chgCapa_CCratio": ("CCcapa", "Qcc(n)/Qcc(0)*100"),
    "delta_chgCapa_CCratio": ("CCratio", "(CCratio(n)-CCratio(0))*100"),
    "capa_loss_ret": ("capa_loss", "capacity-loss relative to first-cycle value"),
    "enrg_loss_ret": ("enrg_loss", "energy-loss relative to first-cycle value"),
}


@dataclass
class PruneResult:
    primary: pd.DataFrame
    ledger: pd.DataFrame
    summary: dict


def read_registry_policy(path: str | Path) -> dict:
    path = Path(path).resolve()
    blob = path.read_bytes()
    tree = ast.parse(blob.decode("utf-8-sig"))
    wanted = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in {"FAMILIES", "EXACT_ALIASES"}:
            wanted[name] = ast.literal_eval(value)
    if set(wanted) != {"FAMILIES", "EXACT_ALIASES"}:
        raise ValueError("Registry must declare literal FAMILIES and EXACT_ALIASES")
    return {**wanted, "source_path": str(path), "source_sha256": hashlib.sha256(blob).hexdigest(),
            "policy_version": POLICY_VERSION,
            "policy_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "representative_overrides": {"capacity_discharge": "SoHQ"},
            "legacy_implementation_choices": LEGACY_ALTERNATIVES,
            "legacy_baseline_choices": LEGACY_BASELINE,
            "legacy_peak_policy": "review_required: numbering and filters differ; no direct mapping"}


def prune_catalog(frame: pd.DataFrame, policy: dict) -> PruneResult:
    required = {"namespace", "metric_id", "feature_name", "role", "origin"}
    if not required <= set(frame):
        raise ValueError(f"Missing catalog columns: {sorted(required - set(frame))}")
    if frame.empty:
        raise ValueError("Empty catalog")
    if "decision" in frame.columns:
        raise ValueError("Use the full source catalog under all_metrics, not an already pruned table")
    if frame.metric_id.duplicated().any():
        raise ValueError("Duplicate metric_id")
    rows = frame.copy().reset_index(drop=True)
    by_id = {r.metric_id: r for r in rows.itertuples()}
    ids = set(by_id)
    edges: dict[str, dict] = {}
    roles: dict[str, tuple[str, str]] = {}

    def edge(source, target, basis, reason, formula="", conditions=""):
        if source in ids and target in ids and source != target and source not in edges:
            edges[source] = dict(target=target, basis=basis, reason=reason, formula=formula, conditions=conditions)

    for row in rows.itertuples():
        name, ns = row.feature_name, row.namespace
        if row.role in {"meta", "qc", "covariate", "diagnosis"}:
            roles[row.metric_id] = ("support_only", "메타데이터·품질·시험 조건·진단 결과는 독립 수명 지표 목록에서 분리")
        if ns == "cyclediag" and (name in {"n_points", "n_t_le_1s", "pulse_current_A", "f_Q_max", "f_Q_spec"}):
            roles[row.metric_id] = ("support_only", "품질/조건 기록 또는 다른 extractor의 용량 필드; 주 목록의 독립 지표로 사용하지 않음")
        if ns == "studio_legacy" and set(row.origin.split("|")) == {"legacy_reference"}:
            roles[row.metric_id] = ("review_required", "코드 참조만 있고 초기화/실제 export 출력 근거가 없어 확인 목록으로 이동; 중복으로 판정하지 않음")
        if ns == "studio_legacy" and re.fullmatch(r"(?:chg|dchg)_peak\d+_(?:V|Q|dQdV|dVdQ|dEdV)", name):
            roles[row.metric_id] = ("review_required", "legacy peak는 초기 anchor/Butterworth 및 극대·극소 번호 체계가 달라 현재 peak 번호와 직접 대응 불가; 삭제 중복으로 세지 않음")
        if ns == "cyclediag" and set(row.origin.split("|")) == {"source_column_loop"}:
            roles[row.metric_id] = ("review_required", "입력 lookup 후보일 수 있는 문자열로, 실제 산출 컬럼 확인 필요")

    # Explicit out.at assignments exist in enrich_assb.py for these context fields,
    # which the general static column-loop collector does not recognize.
    for name in ("Q_relax_source", "Q_relax_significant", "Q_relax_rpt_significant", "Q_relax_dcir_significant"):
        metric = "cyclediag." + name
        if metric in ids:
            roles[metric] = ("support_only", "Q_relax의 RPT/DCIR 출처와 유의성 QC; enrich_assb의 실제 할당 근거가 있는 필수 보조 정보")

    # 1. Registry identities, including non-identity numeric transformations.
    for old, (canonical, formula) in policy["EXACT_ALIASES"].items():
        edge("cyclediag." + old, "cyclediag." + canonical, "declared_identity",
             "기존 registry의 중복 이름/수치 변환 관계; 대표 정의만 유지", formula,
             "동일 시험·SOC·cycle 정의. 값 자체의 rename/변환은 수행하지 않음")

    # 2. Redundant baseline representations; always keep the baseline separately.
    for row in rows.itertuples():
        name, ns = row.feature_name, row.namespace
        if ns == "cyclediag":
            base = name[6:] if name.startswith("delta_") else name[:-4] if name.endswith("_inc") else ""
            base = policy["EXACT_ALIASES"].get(base, (base, ""))[0]
            if base:
                edge(row.metric_id, ns + "." + base, "baseline_representation",
                     "원값과 baseline 차이/증가율을 독립 지표로 중복 나열하지 않음",
                     "x-x_ref" if name.startswith("delta_") else "(x-x_ref)/abs(x_ref)*100",
                     "baseline 값·cycle·단위·필터 설정 보존; 셀 간 초기 수준 차이는 별도 보관")
        elif ns == "studio_legacy":
            if name in LEGACY_BASELINE:
                target, formula = LEGACY_BASELINE[name]
                edge(row.metric_id, ns + "." + target, "baseline_representation",
                     "고정 기준 표현 중복; 실제 filter 경로가 달라 결과값 동일성을 주장하지 않음", formula)
            elif name.startswith("delta_") or name.endswith("_inc"):
                target = name[6:] if name.startswith("delta_") else name[:-4]
                edge(row.metric_id, ns + "." + target, "baseline_representation",
                     "원값·baseline 차이/증가율 중 원값 정의를 대표로 유지",
                     "전압 delta는 (x-x_bol)*1000; 저항 inc는 기준 대비 상대증가율",
                     "bol_c·단위·개별 필터 설정 보존")

    # 3. Explicit existing families: an aggressive display selection, not a
    # claim that empirical relationships hold outside reviewed conditions.
    for family, members in policy["FAMILIES"].items():
        order = list(members)
        if family == "capacity_discharge" and "SoHQ" in order:
            order = ["SoHQ"] + [x for x in order if x != "SoHQ"]
        present = ["cyclediag." + name for name in order if "cyclediag." + name in ids]
        if not present:
            continue
        representative = present[0]
        conditions = "기존 registry가 정의한 family 기준; 이번 작업에서 상관 재검증은 하지 않음"
        if family.startswith("rest_v_"):
            conditions += "; 휴지 길이/프로토콜 변경 시 60s·30m·end를 다시 분리"
        if family == "cv_amount":
            conditions += "; CCratio_norm은 고정 T_ref 피팅의 다른 방법이며 완전 동일하지 않음"
        if family == "q_relax":
            conditions += "; Q_relax_source 보존 필수: RPT 우선/DCIR fallback, 동일 시험창으로 간주하지 않음"
        for old in present[1:]:
            edge(old, representative, "family_representative",
                 f"{family} 묶음의 대표 정의 1개를 선택", conditions=conditions)

    # 4. Legacy implementations with reviewed reporting counterparts.
    cross = dict(LEGACY_ALTERNATIVES)
    for old, canonical in cross.items():
        edge("studio_legacy." + old, "cyclediag." + canonical, "implementation_replaced",
             "동일 종류의 legacy 보고 지표는 현재 cyclediag 구현을 대표로 선택",
             conditions="단위·필터·baseline·peak 배정은 구현마다 다름. 기존 수치를 자동 병합/변환하지 않음")

    # Resolve chains and reject cycles, so every removal has an unambiguous end.
    def resolve(metric):
        chain, seen = [], {metric}
        while metric in edges:
            record = edges[metric]
            chain.append(dict(metric_id=metric, **record))
            metric = record["target"]
            if metric in seen:
                raise ValueError("Cyclic representative policy")
            seen.add(metric)
        return metric, chain

    decisions = []
    for row in rows.itertuples():
        metric = row.metric_id
        target, chain = resolve(metric)
        action, reason = roles.get(metric, ("keep", "독립 측정 위치·시점·방향 또는 대표 정의 유지"))
        if chain:
            action = "remove_duplicate"
            reason = " → ".join(dict.fromkeys(x["reason"] for x in chain))
        target_action = roles.get(target, ("keep", ""))[0]
        decisions.append(dict(metric_id=metric, decision=action, representative_metric_id=target if chain else metric,
                              representative_status=target_action,
                              removal_basis="|".join(dict.fromkeys(x["basis"] for x in chain)),
                              decision_reason=reason, decision_chain_json=json.dumps(chain, ensure_ascii=False),
                              policy_version=POLICY_VERSION,
                              policy_source_path=policy.get("source_path", ""),
                              policy_source_sha256=policy.get("source_sha256", ""),
                              numerical_values_modified=False,
                              independent_causal_evidence=False))
    ledger = rows.merge(pd.DataFrame(decisions), on="metric_id", validate="one_to_one")
    removed = ledger.loc[ledger.decision.eq("remove_duplicate")]
    replacements = removed.groupby("representative_metric_id").metric_id.agg(lambda x: " | ".join(sorted(x))).to_dict()
    conditions = {}
    for representative, group in removed.groupby("representative_metric_id"):
        notes = {step["conditions"] for value in group.decision_chain_json
                 for step in json.loads(value) if step["conditions"]}
        conditions[representative] = " | ".join(sorted(notes))
    ledger["replaced_metric_ids"] = ledger.metric_id.map(replacements).fillna("")
    ledger["selection_conditions"] = ledger.metric_id.map(conditions).fillna("")
    primary = ledger.loc[ledger.decision.eq("keep")].copy().reset_index(drop=True)
    counts = ledger.decision.value_counts().to_dict()
    summary = dict(policy_version=POLICY_VERSION, before=len(rows), primary=len(primary),
                   removed_duplicates=counts.get("remove_duplicate", 0), support_only=counts.get("support_only", 0),
                   review_required=counts.get("review_required", 0),
                   decision_counts=counts,
                   first_removal_basis_counts=ledger.loc[ledger.decision.eq("remove_duplicate"), "removal_basis"].str.split("|", regex=False).str[0].value_counts().to_dict(),
                   original_values_modified=False, source_code_modified=False,
                   assumptions="Existing registry families are a reporting policy, not a newly measured proof of equivalence.",
                   protected_axes=["SOC", "pulse_time", "charge_discharge", "peak_index", "voltage_band"],
                   registry_sha256=policy.get("source_sha256", ""),
                   policy_code_sha256=policy.get("policy_code_sha256", ""))
    assert sum(counts.values()) == len(rows)
    return PruneResult(primary, ledger, summary)


def write_pruned_catalog(frame: pd.DataFrame, directory: str | Path, registry_path: str | Path) -> dict:
    destination = Path(directory)
    policy = read_registry_policy(registry_path)
    result = prune_catalog(frame, policy)
    # Preserve the full source before writing the reduced primary catalog.
    write_catalog(frame, destination / "all_metrics")
    summary = write_catalog(result.primary, destination)
    for name, table in (("pruning_decisions", result.ledger),
                        ("removed_duplicates", result.ledger.loc[result.ledger.decision.eq("remove_duplicate")]),
                        ("support_and_review", result.ledger.loc[result.ledger.decision.isin(["support_only", "review_required"])])):
        table.to_csv(destination / f"{name}.csv", index=False, encoding="utf-8-sig")
        table.to_parquet(destination / f"{name}.parquet", index=False, compression="zstd")
    (destination / "pruning_summary.json").write_text(json.dumps(result.summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "pruning_policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["pruning"] = result.summary
    (destination / "catalog_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    page = destination / "metrics_catalog.html"
    content = page.read_text(encoding="utf-8")
    notice = (f'<section class="card"><h2>중복 정리 적용: {result.summary["before"]} → {result.summary["primary"]}개</h2>'
              f'<p>중복·파생·계열 대표 선택·구현 대체 {result.summary["removed_duplicates"]}개 제외 · 조건/품질/진단 보조 {result.summary["support_only"]}개 분리 · 출력/정의 확인 대기 {result.summary["review_required"]}개 분리</p>'
              '<p><a href="removed_duplicates.csv">제거 항목과 대표 지표</a> · <a href="pruning_decisions.csv">전체 판정표</a> · '
              '<a href="support_and_review.csv">보조·확인 대기</a> · <a href="all_metrics/metrics_catalog.html">정리 전 전체 사전</a></p>'
              '<p>완전 중복·baseline 표현·기존 family의 대표 선택·legacy 구현 대체를 판정표에서 구분했습니다. 측정값은 변환하지 않습니다.</p></section>')
    excluded_rows = []
    for row in result.ledger.loc[result.ledger.decision.eq("remove_duplicate")].itertuples():
        cells = (row.metric_id, row.representative_metric_id, row.removal_basis, row.decision_reason)
        excluded_rows.append("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in cells) + "</tr>")
    audit = (f'<details class="card" style="max-width:none;margin:16px 0"><summary>제외한 {result.summary["removed_duplicates"]}개 항목과 대표 지표 보기</summary>'
             '<div class="tablewrap"><table><thead><tr><th>제외 항목</th><th>대표 지표</th><th>분류</th><th>근거</th></tr></thead><tbody>'
             + "".join(excluded_rows) + '</tbody></table></div></details>')
    content = content.replace("<main>", "<main>" + notice + audit, 1)
    page.write_text(content, encoding="utf-8")
    return summary
