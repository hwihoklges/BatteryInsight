"""Enumerate actual electrochemical column names without executing legacy code.

Core naming loops are explicitly reproduced from reviewed source constants.
Observed exports are read through their header only. Source references and
unreviewed units remain visibly distinct from verified definitions.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import json
from pathlib import Path
import re

import pandas as pd


_AUDITED = {
    "lges_extract.py": "73e1feb3701d80d5ce1d2fa51a355af10a402082d4435f564a780e517015d03a",
    "lges_extra_indicators.py": "1849a80b03de2f25d831cc821bbf1c3c78dd6752de7cddc80c1a8fec9f70088f",
    "dcir_decompose.py": "fd840233b3ee8fc891c9da65ddd0764e4365dc98a37df67651120c74e8a53491",
    "signal_cv.py": "e785d5a6e074199a97d58d6f9b1feb607d28ad33de92c7703836b75cf6b5f7fc",
    "func_feature_extract.py": "3880671aea7488f9d671ae1d919de501ec2bb8173f3e803ce390bacaf16d2e69",
}


def _literal(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset" and len(node.args) == 1:
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def _load(path: Path) -> dict:
    blob = path.read_bytes()
    source = blob.decode("utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    values, lines, nodes = {}, {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        else:
            continue
        nodes[name], lines[name] = value, node.lineno
        try:
            values[name] = _literal(value)
        except (ValueError, TypeError):
            pass
    return dict(path=str(path.resolve()), source=source, tree=tree, values=values, lines=lines,
                nodes=nodes, sha256=hashlib.sha256(blob).hexdigest())


def _patterns(info, name):
    node = info["nodes"].get(name)
    if not isinstance(node, (ast.Tuple, ast.List)):
        return []
    return [re.compile(item.args[0].value) for item in node.elts
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
            and isinstance(item.func.value, ast.Name) and item.func.value.id == "re"
            and item.func.attr == "compile" and item.args and isinstance(item.args[0], ast.Constant)
            and isinstance(item.args[0].value, str)]


def _registry(name, info):
    values = info["values"]
    aliases, families = values.get("EXACT_ALIASES", {}), values.get("FAMILIES", {})
    canonical = aliases.get(name, (name, ""))[0]
    base = canonical
    for _ in range(20):
        changed = aliases.get(base, (base, ""))[0]
        if changed.startswith("delta_"):
            changed = changed[6:]
        elif changed.endswith("_inc"):
            changed = changed[:-4]
        if changed == base:
            break
        base = changed
    role, basis = "indicator", "registry_default_needs_review"
    for candidate in (canonical, base):
        if candidate in values.get("_META_EXACT", set()):
            role, basis = "meta", "registry_exact"
            break
    if role != "meta":
        for candidate in (canonical, base):
            for key, label in (("_TARGET_EXACT", "target"), ("_COVARIATE_EXACT", "covariate"), ("_QC_EXACT", "qc")):
                if candidate in values.get(key, set()):
                    role, basis = label, "registry_exact"
                    break
            if basis != "registry_default_needs_review":
                break
            if any(pattern.search(candidate) for pattern in _patterns(info, "_QC_PATTERNS")):
                role, basis = "qc", "registry_pattern"
                break
            if candidate not in values.get("_DIAGNOSIS_EXEMPT", set()) and any(pattern.search(candidate) for pattern in _patterns(info, "_DIAGNOSIS_PATTERNS")):
                role, basis = "diagnosis", "registry_pattern"
                break
    family = next((group for group, members in families.items() if base in members), base)
    return dict(role=role, role_basis=basis, family=family, canonical_base=base,
                retired_alias_of=aliases.get(name, ("", ""))[0], alias_formula=aliases.get(name, ("", ""))[1])


def _group(name, role):
    if role == "meta":
        return "메타데이터"
    if role == "qc":
        return "품질·유효성"
    if role == "diagnosis":
        return "퇴화 패턴·진단"
    lower = name.lower()
    for tokens, group in [(("dqdv", "dvdq", "dedv", "dvde", "peak", "cliff"), "미분·피크·cliff"),
                          (("rest", "relax", "ocv", "self_discharge", "v_inf"), "휴지·OCV·자기방전"),
                          (("dchgr", "chgr", "r_", "dcir", "tau_ct", "a_diff"), "저항·DCIR"),
                          (("capacity", "capa", "sohq", "retention", "ccratio", "cctime", "cvtime"), "용량·CC/CV·유지율"),
                          (("energy", "enrg", "sohe", "_e"), "에너지"),
                          (("hyst", "plateau", "shape", "fit_", "_v", "voltage", "ir_drop"), "전압·곡선 형태"),
                          (("temp", "cutoff", "duration"), "시험 조건")]:
        if any(token in lower for token in tokens):
            return group
    if name in {"CE", "CE_rev", "CE_local_20", "CI", "CI_per_hour", "EE", "VE"}:
        return "효율"
    return "기타·정의 검토"


def _unit(name, namespace, sources):
    """Only the reviewed source snapshot enables a verified unit rule."""
    base = name[6:] if name.startswith("delta_") else name
    unit, description, source, line = "needs_review", "원본 정의와 시험 조건을 확인할 지표", "", 0
    if namespace == "studio_legacy":
        source = "func_feature_extract.py"
        if name in {"SoHQ", "SoHE", "chgRetention"}:
            unit, description, line = "%", "초기 용량/에너지 대비 유지율", 306
        elif name == "chgCapa_CCratio":
            unit, description, line = "%", "현재 CC 용량 / 초기 CC 용량 ×100; cyclediag의 동명 CC share와 다름", 387
        elif name == "delta_chgCapa_CCratio":
            unit, description, line = "percentage_point", "현재 CCratio와 초기 CCratio 차이 ×100", 388
        elif name in {"CCratio", "CCratio_enrg", "dchg_chg_capa", "chg_dchg_capa", "dchg_chg_enrg", "chg_dchg_enrg"}:
            unit, description, line = "1", "용량 또는 에너지 비율; 분모 cycle 정의 확인", 378
        elif re.fullmatch(r"(?:delta_)?Eo[CD]_V(?:1|10|30|end)", name):
            stamp = {"1": "60초", "10": "600초", "30": "1800초", "end": "휴지 끝"}
            suffix = base.split("_V", 1)[1]
            unit = "mV" if name.startswith("delta_") else "V"
            description = f"휴지 {stamp[suffix]} 전압" + ("의 baseline 대비 차이 ×1000" if name.startswith("delta_") else "")
            line = 579 if name.startswith("delta_") else 552
        elif "dchgR" in name or "chgR" in name:
            description, line = "전류 크기에 따른 단위 추정 분기가 존재하여 저항 단위 재검토 필요", 649
        elif any(x in name.lower() for x in ("capa", "energy", "enrg", "dqdv", "dvdq", "dedv", "dvde")):
            description = "DB2의 용량·에너지 단위 확인 전 단위 확정 불가"
    else:
        source = "lges_extract.py"
        if name.endswith("_inc") and name[:-4] in sources["lges_catalog.py"]["values"].get("DELTA_PCT_COLS", []):
            unit, description, line = "%", "baseline 대비 상대 변화율; 해당 baseline 정의 확인", 524
        elif base in {"chgCapa", "dchgCapa"}:
            unit, description, line = "Ah", "충전/방전 leg 용량; capacity_to_ah 적용", 368
        elif base in {"CE", "SoHQ", "CE_rev", "CE_local_20"}:
            unit, description, line = "%", "충방전 효율 또는 초기 방전 용량 대비 유지율", 374
            if base == "CE_rev":
                description, line = "다음 cycle 충전 용량 / 현재 방전 용량 ×100; 미래 cycle 정보 사용", 546
            if base == "SoHQ":
                line = 512
        elif re.fullmatch(r"Eo[CD]_restV_(?:init|60s|30m|end|relax|relax_60s)", base):
            unit, description, line = "V", "충전/방전 후 휴지 전압 또는 휴지 전압 변화", 334
        elif re.fullmatch(r"Eo[CD]_restV_tau", base):
            unit, description, line = "s", "휴지 전압 완화 시정수; time 축 단위 검증 필요", 454
        elif re.fullmatch(r"Eo[CD]_(?:dchgR|chgR)_(?:0p1s|10s|30s|60s|R10_minus_R0p1|R30_minus_R0p1|10s_T25)", base):
            unit, description, line = "mohm", "명시한 pulse 시간의 ΔV/ΔI ×1000 또는 그 차분/온도 보정", 341
        elif base.endswith("_10_60_ratio"):
            unit, description, line = "1", "10초 저항 / 60초 저항", 363
        elif base in {"EE", "VE"}:
            unit, description, line = "1", "에너지 효율 또는 에너지 효율/CE 비율", 428
        elif base == "CI_per_hour":
            unit, description, line = "percentage_point/hour", "(100−CE) / cycle 소요시간", 449
        elif base == "cycle_duration_h":
            unit, description, line = "h", "cycle 소요시간", 444
        elif base in {"dSoHQ_dN", "d2SoHQ", "EoC_dchgR_10s_growth_50"}:
            unit = {"dSoHQ_dN": "percentage_point/cycle", "d2SoHQ": "percentage_point/cycle^2", "EoC_dchgR_10s_growth_50": "mohm/cycle"}[base]
            description, line = "cycle 축에서 계산한 상태 변화 경향", 549
        elif re.fullmatch(r"(?:chg|dchg)_dQdV_peak\d+_V", base):
            unit, description, line = "V", "dQ/dV peak의 전압 위치", 396
        elif base.startswith("hyst_area") or base.startswith("hyst_max_dV"):
            source, unit, description, line = "lges_extra_indicators.py", "V", "정규화 Q축의 hysteresis 면적 또는 최대 전압 차이; Wh가 아님", 151
        elif base.startswith("hyst_frac"):
            source, unit, description, line = "lges_extra_indicators.py", "1", "전체 hysteresis 면적 대비 구간 비율", 175
        elif "ir_drop_proxy" in base:
            source, unit, description, line = "lges_extra_indicators.py", "V", "초기 전압 변화 proxy; 저항 Ω가 아님", 105
        elif base in {"chgCCcapa", "chgCVcapa"}:
            source, description, line = "signal_cv.py", "원시 Q 차분을 그대로 사용하므로 Ah/mAh는 upstream 용량축 검증 필요", 174
        elif base in {"Q_CV_at_Tref", "chgCVtime", "tau_CV", "chgCapa_CCratio", "chgCapa_CCratio_norm", "cv_detect_mismatch_pct"}:
            source = "signal_cv.py"
            unit = "Ah" if base == "Q_CV_at_Tref" else ("s" if base in {"chgCVtime", "tau_CV"} else "%")
            description, line = "signal 기반 CC/CV 분리 결과; 검출·기준시간 설정에 의존", 1
        elif any(token in base for token in ("dQdV", "dVdQ", "_Q_", "_E", "cliff")):
            description = "원시 Q축 단위가 그대로 사용되는 경로가 있어 Ah/mAh 확인 필요"
        if name.startswith("delta_") and unit != "needs_review":
            description = "baseline 대비 차이: " + description
            if unit == "%":
                unit = "percentage_point"
    info = sources.get(source)
    verified = bool(info and info["sha256"] == _AUDITED.get(source) and unit != "needs_review")
    if not verified:
        unit = "needs_review"
    return dict(unit=unit, unit_status="verified_source" if verified else "needs_review",
                description_ko=description, unit_source_path=info["path"] if info else "",
                unit_source_line=line if info else 0,
                unit_source_sha256=info["sha256"] if info else "")


def _requirements(group, name):
    if group == "메타데이터":
        return "셀·시험 registry와 원본 식별 정보", "셀·채널·시험 매칭을 확인; 수치 열화 지표로 사용하지 않음"
    if group == "저항·DCIR":
        return "time, voltage, current, step, rest, SOC, temperature", "전류 전환·관측 시간·sampling·SOC·온도·입력 A/V/s 단위 정합 필요"
    if group == "미분·피크·cliff":
        return "capacity, voltage, leg, CC 구간, cycle", "Q축 단위·단조성·점밀도·필터·grid·peak matching 검토; 해당 반응의 확정 증거 아님"
    if group == "휴지·OCV·자기방전":
        return "time, voltage, current, step, rest start/end, temperature", "휴지 길이·종료 상태·잔류 전류·시간 단위 검토; 완화 fit을 물리 성분으로 자동 해석하지 않음"
    if group in {"용량·CC/CV·유지율", "효율", "에너지"}:
        note = "완결 cycle·충방전 방향·baseline·적분 단위·CC/CV 판정 검토"
        if name == "CE_rev":
            note += "; 다음 cycle 정보가 필요하므로 현재 cycle 시점 지표가 아님"
        return "current/time 또는 측정 capacity, voltage, leg, completion, baseline", note
    if group == "전압·곡선 형태":
        return "voltage, capacity, leg, cycle, baseline", "정규화 축·정렬·SOC·C-rate·온도·필터를 동일하게 비교"
    if group == "품질·유효성":
        return "해당 계산의 raw 구간·sampling·fit 상태", "품질 보조 정보; 열화 크기와 독립된 해석 필요"
    if group == "퇴화 패턴·진단":
        return "구성 지표, 규칙 설정, 단위, 적용 chemistry/조건", "다중 지표 패턴 점수; 원인 확진이나 학습 예측 성능을 의미하지 않음"
    return "원본 함수와 입력 스키마 검토 필요", "정의·필수 입력·유효 범위 미검증; 자동 계산 가능으로 간주하지 않음"

def build_catalog(cyclediag_root, studio_root, export_paths: list[str] | None = None) -> pd.DataFrame:
    """Build namespaced metric rows from source declarations and observed headers.

    ``export_paths`` must be explicit cyclediag CSV exports; only their first
    CSV record is read. The hash scope is ``header`` for these sources and
    ``file`` for Python definitions. No raw records or legacy code are run.
    The result is a catalog, not a statement that every listed feature has a
    valid measured value in every test.
    """
    feature_root = Path(cyclediag_root) / "features"
    core = _load(feature_root / "lges_catalog.py")
    registry = _load(feature_root / "indicator_registry.py")
    legacy = _load(Path(studio_root) / "db_pipeline" / "lgensol" / "func_feature_extract.py")
    sources = {"lges_catalog.py": core, "indicator_registry.py": registry,
               "func_feature_extract.py": legacy}
    for filename in ("lges_extract.py", "lges_extra_indicators.py", "signal_cv.py", "dcir_decompose.py", "enrich_assb.py", "ocv_drift.py", "rpt_metrics.py", "quality.py"):
        path = feature_root / filename
        if path.is_file():
            sources[filename] = _load(path)
    records: dict[tuple[str, str], dict] = {}

    def add(namespace, name, info, line, origin, declared_group="", hash_scope="file"):
        if not isinstance(name, str) or not name or name.startswith("_"):
            return
        key = namespace, name
        evidence = dict(path=info["path"], line=int(line), sha256=info["sha256"], hash_scope=hash_scope, origin=origin)
        if key not in records:
            records[key] = dict(namespace=namespace, metric_id=f"{namespace}.{name}", feature_name=name,
                                source_path=info["path"], source_line=int(line), source_sha256=info["sha256"],
                                source_hash_scope=hash_scope, _origins=[], _groups=[], _evidence=[])
        row = records[key]
        if evidence not in row["_evidence"]:
            row["_evidence"].append(evidence)
        if origin not in row["_origins"]:
            row["_origins"].append(origin)
        if declared_group and declared_group not in row["_groups"]:
            row["_groups"].append(declared_group)

    values = core["values"]
    groups = ("PATTERN_CHECK_COLS", "REST_EOC_COLS", "REST_EOD_COLS", "REST_DERIVED_COLS",
              "RESISTANCE_DERIVED_COLS", "CAPACITY_COLS", "BAND_CAPACITY_COLS", "SHAPE_COLS",
              "DVDQ_SOC_COLS", "CLIFF_COLS", "CURVE_FIT_COLS", "TRAJECTORY_COLS", "DIAGNOSIS_COLS")
    for group in groups:
        for name in values.get(group, []):
            add("cyclediag", name, core, core["lines"][group], "core_declared", group)
    peak_count = values.get("MAX_DQDV_PEAKS")
    offsets = values.get("RESISTANCE_OFFSETS_S")
    if not isinstance(peak_count, int) or not 1 <= peak_count <= 100 or not isinstance(offsets, tuple):
        raise ValueError("unsupported catalog constants; review naming schema before expansion")
    for index in range(1, peak_count + 1):
        for direction in ("chg", "dchg"):
            for suffix in (f"dQdV_peak{index}_V", f"dQdV_peak{index}", f"dVdQ_peak{index}_Q", f"dVdQ_peak{index}"):
                add("cyclediag", f"{direction}_{suffix}", core, core["lines"]["MAX_DQDV_PEAKS"], "core_generated", "PEAK_COLS")
    for prefix in ("EoC_dchgR", "EoD_chgR"):
        for offset in offsets:
            label = "0p1s" if abs(offset - 0.1) < 1e-9 else f"{int(round(offset))}s"
            add("cyclediag", f"{prefix}_{label}", core, core["lines"]["RESISTANCE_OFFSETS_S"], "core_generated", "RESISTANCE_COLS")
    for group, prefix, suffix in (("DELTA_ABS_COLS", "delta_", ""), ("DELTA_PCT_COLS", "", "_inc")):
        for name in values.get(group, []):
            add("cyclediag", prefix + name + suffix, core, core["lines"][group], "core_generated", group)

    # Explicit aliases/families are catalog evidence, not automatic numeric conversion.
    for name in registry["values"].get("EXACT_ALIASES", {}):
        add("cyclediag", name, registry, registry["lines"]["EXACT_ALIASES"], "registry_retired_alias")
    for group, members in registry["values"].get("FAMILIES", {}).items():
        for name in members:
            add("cyclediag", name, registry, registry["lines"]["FAMILIES"], "registry_family", group)

    # Actual direct output assignments; intermediate private arrays are excluded.
    for filename in ("lges_extract.py", "enrich_assb.py", "rpt_metrics.py", "quality.py"):
        info = sources.get(filename)
        if not info:
            continue
        for node in ast.walk(info["tree"]):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and node.value.id in {"row", "out"} and isinstance(node.slice, ast.Constant):
                add("cyclediag", node.slice.value, info, node.lineno, "source_output_assignment")
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "col" and isinstance(node.iter, (ast.Tuple, ast.List)):
                for item in node.iter.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        add("cyclediag", item.value, info, node.lineno, "source_column_loop")
    signal = sources.get("signal_cv.py")
    if signal:
        for node in signal["tree"].body:
            if isinstance(node, ast.FunctionDef) and node.name == "signal_cv_to_row":
                for item in ast.walk(node):
                    if isinstance(item, ast.Dict):
                        for key in item.keys:
                            if isinstance(key, ast.Constant):
                                add("cyclediag", key.value, signal, key.lineno, "source_output_dict")
    dcir, enrich = sources.get("dcir_decompose.py"), sources.get("enrich_assb.py")
    if dcir and enrich:
        socs = enrich["values"].get("_SOC_ORDER", ())
        for node in dcir["tree"].body:
            if isinstance(node, ast.ClassDef) and node.name == "DcirFitResult":
                for field in node.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        for suffix in ("", *(f"_soc{soc}" for soc in socs)):
                            add("cyclediag", field.target.id + suffix, dcir, field.lineno, "enrich_dataclass_generated", "DCIR_DECOMPOSITION")
        for soc in socs:
            for name in ("self_discharge_rate", "sd_fit_valid", "V_inf_rest", "relax_completeness"):
                add("cyclediag", f"{name}_soc{soc}", enrich, enrich["lines"].get("_SOC_ORDER", 1), "enrich_soc_generated")
    ocv = sources.get("ocv_drift.py")
    if ocv:
        for name in ocv["values"].get("_BROADCAST_COLS", ()):
            add("cyclediag", name, ocv, ocv["lines"]["_BROADCAST_COLS"], "source_broadcast_column")

    for node in ast.walk(legacy["tree"]):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and any(isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self" and target.attr == "val" for target in node.targets):
            for key in node.value.keys:
                if isinstance(key, ast.Constant):
                    add("studio_legacy", key.value, legacy, key.lineno, "legacy_initialized")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) and node.value.value.id == "self" and node.value.attr == "val" and isinstance(node.slice, ast.Constant):
            add("studio_legacy", node.slice.value, legacy, node.lineno, "legacy_reference")

    for raw_path in export_paths or []:
        path = Path(raw_path)
        if path.suffix.lower() != ".csv":
            raise ValueError("catalog observed exports must be CSV; only headers are inspected")
        consumed = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            def header_lines():
                for line in stream:
                    consumed.append(line)
                    yield line
            header = next(csv.reader(header_lines(), strict=True), None)
        if not header or len(header) != len(set(header)) or any(not name.strip() for name in header):
            raise ValueError(f"invalid export header: {path}")
        info = dict(path=str(path.resolve()), sha256=hashlib.sha256("".join(consumed).encode("utf-8")).hexdigest())
        for name in header:
            add("cyclediag", name, info, 1, "observed_export_header", hash_scope="decoded_header_utf8")

    legacy_meta = {"info_str", "file_name", "start_time", "end_time", "cell_id", "cell_type", "rpt_status", "r_cycle"}
    output = []
    for (namespace, name), row in records.items():
        if namespace == "cyclediag":
            annotations = _registry(name, registry)
        else:
            role = "meta" if name in legacy_meta else ("covariate" if name in {"temp_chg", "temp_dis", "t_rest_chg", "t_rest_dis"} else "unclassified")
            annotations = dict(role=role, role_basis="legacy_metadata" if role != "unclassified" else "needs_review",
                               family=name, canonical_base=name, retired_alias_of="", alias_formula="")
        row.update(annotations)
        row.update(_unit(name, namespace, sources))
        row["group"] = _group(name, row["role"])
        row["required_data"], row["validity_notes"] = _requirements(row["group"], name)
        row["is_metadata"] = row["role"] == "meta"
        row["definition_version"] = str(values.get("FEATURE_SET_LGES", "needs_review")) if namespace == "cyclediag" else "legacy_source_snapshot"
        row["origin"] = "|".join(row.pop("_origins"))
        row["declared_groups"] = "|".join(row.pop("_groups"))
        row["evidence_json"] = json.dumps(row.pop("_evidence"), ensure_ascii=False)
        row["definition"] = row["description_ko"]
        output.append(row)
    return pd.DataFrame(output).sort_values(["namespace", "is_metadata", "group", "feature_name"], kind="stable").reset_index(drop=True)
