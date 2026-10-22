# Raw 추가 시 갱신되는 두 가지 실험 관계 지도

점 하나는 **셀별 시험 하나**다. 활물질·전극 구성·로딩의 유사성과 전기화학 지표의 유사성을 각각 계산한다. 머신러닝은 사용하지 않는다. `demo_relations/index.html`은 가상 셀 12개로 만든 예시이며 소재명·조성·로딩도 모두 합성값이다.

설계·전기화학 항목을 직접 켜고 끄면서 계산하려면 확장 [실험 비교 작업실](WORKBENCH.md)을 사용한다. 아래 `update/watch`는 기본 두 지도 보고서의 사용법이다.

## 두 지도와 비교표

| 보기 | 입력과 의미 | 조작 |
|---|---|---|
| 전극 설계·재료 | 등록표의 소재, 양·음극 조성, 로딩이 비슷한 실험 연결 | 기본 화면. 점·선을 선택하면 실제 값과 차이 표 표시 |
| 전기화학 지표 | 공통 관측창의 대표 지표가 비슷한 실험 연결 | 같은 셀 선택을 유지하며 전환. 레이더로 값 패턴 비교 |
| 이웃 비교 | 선택 셀과 다른 셀의 설계/전기화학 유사도를 나란히 표시 | 설계·반응의 일치/불일치 후보 확인; 두 점수 합산 없음 |
| 지표 상관행렬 | 전기화학 지표 간 Spearman 순위 상관 | 같은 물리 셀의 반복 시험은 셀 중앙값으로 요약 |
| 데이터 상태 | 새 파일·미등록·기록 중·실패·부족 자료 | 상태 탭 확인 |

설계가 비슷한데 반응이 다른 셀, 설계가 달라도 반응이 비슷한 셀을 찾는 용도다. 네트워크 선은 **실험 유사도**이며 상관계수가 아니다. 노드 위치는 시각 배치다. 유사도·상관만으로 원인이나 성능 순위를 확정하지 않는다.

## 설계 입력과 비교 기준

`experiments.template.csv`의 설계 열에 확인된 값을 입력한다. `design_group`은 서로 비교할 설계 집합이다. 빈 group은 `전체 등록 설계`로 묶는다. `cohort`는 전기화학 측정 조건의 비교 집합이며 두 집합은 독립이다. 설계 지도는 include=true인 등록 전체를 사용하므로 raw가 아직 없어도 표시할 수 있다. 실제 소재명이나 조성은 raw·화학계 preset·파일명으로 추정하지 않는다.

기본 `design.fields` 12개:

- 양·음극 각각 활물질, 고체전해질, 바인더, 도전재의 **명시한 소재명** 8개
- 양·음극 각각 활물질/고체전해질/도전재/바인더/기타의 **건조 전극 총 고형분 질량 조성** 2개
- 양·음극 각각 **단면 면적 용량 로딩 mAh/cm²** 2개

`examples/design_fields.optional.json`에서 질량 로딩 mg/cm², 압착 후 밀도, 전해질막 질량·두께·소재, 활물질 내부 blend를 선택할 수 있다. 기본 mAh/cm²는 로컬 Studio 계산기의 단위에 맞춘 것이다. mg/cm²를 대신 쓰려면 해당 field를 **교체**한다. 동일 group의 파생 표현을 동시에 선택하면 오류로 거부한다. 밀도·두께·로딩·N/P처럼 서로 유도되는 항목을 무조건 추가하지 않는다.

조성의 `composition_basis`는 `electrode_total`(건조 전극 총 고형분) 또는 `active_material_total`(활물질 내부)로 명시한다. 모두 wt%다. 전체 구성성분 값을 제공하고 합계는 기본 100±1 percentage point여야 한다. 0은 확인된 부재를 의미한다. 빈 성분은 unknown이며 0으로 채우거나 자동 재정규화하지 않는다. 소재명이 여러 가지로 쓰였다면 등록 단계에서 동일한 이름으로 정리한다. 문자열이 다른 물질의 화학적 유사도는 추정하지 않는다.

설계 거리는 항목별 차이의 가중 평균이고, 유사도는 `1 - 거리`다.

| 종류 | 차이 계산 |
|---|---|
| 소재명 | 정확 일치 0, 불일치 1 |
| 숫자 | `min(abs(차이) / scale, 1)`; scale은 명시한 단위의 비교 간격 |
| 조성 | `sum(abs(성분별 wt% 차이)) / 200`; 전체 조성 벡터를 한 항목으로 가중 |

기본 조건은 공통 항목 3개 이상, 설정한 전체 가중치 중 공통 항목 가중치 60% 이상이다. 결측은 점수 계산에서 제외하되 coverage 분모에는 남아 부족한 쌍을 걸러낸다. 숫자가 모두 같으면 알려진 일치로 처리한다. 템플릿의 scale·가중치는 **조정 가능한 비교 정책**이며 검증된 이상 임계값이 아니다. 모든 로딩은 단면/양면, 활물질/총도포량, 집전체 제외 여부가 동일한 기준이어야 한다. 기준이 다르면 통일 후 입력하거나 design_group을 분리한다.

## 처음 한 번 설정

패키지 폴더에서 기존 설치를 사용하거나 `python -m pip install -e ".[test]"`로 설치한다. 다음 세 파일을 복사해 실제 입력에 맞춘다.

1. `examples/run.template.json`: raw 헤더·단위·전류 부호·완료 조건 등 기존 파이프라인 입력 계약. 하나의 raw source를 둔다.
2. `examples/experiments.template.csv`: experiment_id, cell_id, test_id, raw_path, cohort, protocol, chemistry, test_type을 명시한다. raw_path는 inbox 기준 경로다. 같은 파일을 다른 실험으로 이중 등록할 수 없다.
3. `examples/relations.template.json`: inbox, registrations, pipeline_template, 비교할 life_min/life_max와 대표 지표를 지정한다. 설정 파일 기준 상대경로도 사용할 수 있다.

전기화학 비교군 `cohort`는 같은 프로토콜·test_type으로 구성한다. 기본 엔진은 화학계 혼합도 거부한다. 두 지도 예제 템플릿에는 `comparison.allow_mixed_chemistry=true`를 명시해 서로 다른 소재 셀도 비교할 수 있게 했다. 측정 SOC·전압창·C-rate·온도·압력과 관측축 의미가 비교 가능한 경우에만 이 옵션을 사용한다. 온도·압력 등 다른 시험 조건도 비교 목적에 맞춰 비교군을 나누고, 온도는 등록표에 기록한다. 시험 간 life_index가 같은 노출량을 뜻하는지도 확인한다. 결과 출력 폴더는 inbox와 서로 포함 관계가 없는 별도 폴더로 둔다.

## 추가된 raw를 계속 반영

```powershell
# 한 번 갱신
python -m battery_insight update --config my_relations.json --out D:/BatteryRelations

# 실행 중 30초마다 확인하고 로컬 대시보드 제공
python -m battery_insight watch --config my_relations.json --out D:/BatteryRelations --interval 30 --port 8765
```

watch가 출력하는 `http://127.0.0.1:8765/`를 브라우저로 연다. 첫 처리 후 열리며 화면은 10초마다 새 결과를 확인한다. 프로세스를 끄면 감시도 종료된다. Windows 예약 작업이나 상시 서비스는 설치하지 않는다. 서버 없이 감시하려면 `--port 0`을 사용한다.

새 raw 파일을 inbox에 넣고 **등록표에도 셀·시험·조건을 추가**한다. 파일명의 숫자로 셀을 추측하지 않는다. 등록 없는 파일은 ‘미등록’, 최근 수정 파일은 settle_seconds 동안 ‘기록 안정화 대기’로 표시한다. 등록표를 바꾼 경우도 다음 확인에서 반영된다. 파일별 실패는 다른 파일 처리를 막지 않으며, 수정 중 사라지거나 내용이 바뀐 파일의 관측값은 게시하지 않는다.

이미 처리한 파일은 크기·수정시각·등록 정보·설정·코드 버전을 확인해 재사용한다. 크기와 수정시각을 보존한 채 내용만 교체할 가능성이 있으면 `--verify-all`을 추가한다. 이는 변경 없는 파일도 전체 SHA-256을 읽으므로 큰 raw가 많으면 느릴 수 있다. 실제 재계산 때는 기존 pipeline이 raw를 읽기 전후 해시로 검증한다. 동일 출력 폴더에는 하나의 update/watch 프로세스를 사용한다.

`dashboard/index.html`은 파일로도 열 수 있다. 이 경우 브라우저가 로컬 파일을 자동 감시할 수 없으므로 새로 연 HTML을 사용하거나 **데이터 열기**로 갱신된 `dashboard/data.json`을 선택한다. 원본 raw는 수정·삭제하지 않는다.

## 비교 계산의 범위

- 모든 실험에 미리 정한 동일 `life_min..life_max` 구간을 사용한다. 관측된 최종 수명으로 정규화하지 않는다. 양끝까지 데이터가 없거나 min_points 미달이면 해당 요약을 비워 둔다. 보간·외삽하지 않는다.
- `summary: median`은 그 구간의 중앙값, `change`는 구간 내 마지막 유효값−첫 유효값이다. 한 번의 비교에서는 같은 방식을 사용한다.
- 같은 family에서 대표 하나만 선택한다. 223개 전체 사전을 무조건 넣지 않고, 단위·정의가 확인된 indicator만 관계 계산에 넣는다. target·시험 조건·QC·진단 결과를 전기화학 지표처럼 가중하지 않는다. 소재·조성·로딩은 별도 설계 지도에만 사용한다.
- 비교군 내부에서 중앙값/MAD로 척도를 맞춘다. MAD가 0이면 IQR, 표준편차 순으로 검토하며 상수 지표는 제외한다.
- 공통 지표의 표준화 거리 RMS로 `유사도 = 1/(1+RMS)`를 계산한다. 기본은 공통 지표 3개 이상, 비교군의 비상수 대표 지표 중 60% 이상이다. 결측을 0으로 채우지 않는다.
- 상관행렬에서는 같은 물리 셀의 반복 시험 요약을 다시 중앙값으로 모아 셀 하나당 한 값으로 계산한다. 사이클 수를 독립 표본 수로 세지 않는다. 배치 효과 같은 추가 교란은 남을 수 있다.
- 새 셀이 들어오면 비교군의 척도와 연결이 달라질 수 있다. 점의 위치는 화면 배치이며 독립적인 물리 거리가 아니다.

## 기존 AIM·cyclediag export 연결

기존 `external_features` 매핑을 pipeline template에 두거나, 등록표의 `config_path`에 셀별 pipeline 설정 경로를 지정한다. 각 설정은 raw source 하나를 가져야 한다. 외부 매핑이 하나일 때 등록표의 `external_path`로 export 경로를 바꿀 수 있다. 상대 외부 경로는 해당 pipeline template 기준이다. 외부 매핑의 셀/시험 ID·단위·버전·revision은 실제 값에 맞게 설정한다.

관계 지표 설정 예시는 다음과 같다. placeholder를 확인한 실제 값으로 바꾼다.

```json
{
  "metric_id": "cyclediag.EoC_dchgR_60s",
  "label": "충전 말단 60초 저항",
  "table": "external_features",
  "column": "cyclediag.EoC_dchgR_60s",
  "unit": "mohm",
  "family": "eoc_resistance_60s",
  "role": "indicator",
  "definition_version": "검증한 실제 정의 버전",
  "source_revision": "검증한 전체 Git commit 또는 SHA256"
}
```

외부 cycle은 동일 셀·시험의 raw cycle에서 life_index에 연결한다. 단위·정의·revision이 다르면 비교하지 않으며, 비교 창 이후를 참조한 지표는 제외 사유로 남긴다. 별도 AIM 명세가 확인되면 이 계약으로 연결한다.

## 가상 raw로 전체 흐름 재현

```powershell
python examples/make_relations_demo.py --out ../../work/relations_demo
python -m battery_insight update --config ../../work/relations_demo/relations.json --out ../../work/relations_result
```

합성 raw 12개, 셀별 8사이클, 대표 지표 5개를 처리한다. 실제 데이터로 학습하거나 실제 이상 원인을 판정한 결과가 아니다.

출력에는 `dashboard/index.html`, `dashboard/data.json`, `observations.parquet`, `experiments.csv`, `update_state.json`, `generated_configs/`, `design_registry_snapshot.json`, 기존 `lake/runs/`가 포함된다. 화면에 표시한 값은 원본 파일 해시와 계산 실행 경로까지 추적할 수 있다.

설계 등록표의 SHA-256, 실제 사용 필드·단위·조성 분모·가중치·scale과 등록값은 `design_registry_snapshot.json`에 보존한다. 기존 design 설정이 없는 config는 계속 동작하며 설계 화면에는 등록 안내를 표시한다.
