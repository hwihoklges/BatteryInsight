# 수명 관련 지표 중복 정리

2026-10-21에 확인한 사전 589개에서 **대표 223개**를 기본 목록으로 남겼다. 모델 학습이나 실제 raw 분석 없이 코드의 산식·역할·기존 중복 registry와 export 헤더를 대조했다. 별도 AIM 명세는 확인되지 않아 이 목록을 AIM 전체의 확정 명세라고 부르지는 않는다.

| 처리 | 개수 | 의미 |
|---|---:|---|
| 대표 유지 | 223 | 수명 관련 관측량·지표. 용량 유지율 등 결과 지표는 `target` 역할을 유지 |
| 주 목록에서 제외 | 108 | 수식상 중복 6, baseline 표현 56, 기존 계열 대표 선택 29, legacy 구현 대체 17. 연결 체인의 첫 판정 기준으로 집계 |
| 보조 정보 분리 | 150 | 시험 조건, QC, 메타데이터, 계산된 진단 결과. 지표와 함께 해석할 정보 |
| 정의/출력 확인 대기 | 108 | 출력 확인이 부족한 참조와 legacy 피크 등. 중복이라는 이유로 제거한 것이 아님 |

## 대표를 정한 방식

| 제외한 표현 또는 계열 | 남긴 대표 | 판단 |
|---|---|---|
| `CI` | `CE` | `CI = 100 − CE`; 독립 정보가 아님 |
| `delta_x`, `x_inc` | 대응 원값 | baseline과 단위·필터 설정을 보관할 때 파생 가능한 표현. `inc=(x−x_ref)/abs(x_ref)×100`은 현재 cyclediag 산식 |
| 방전 용량·유지율·방전 에너지·SOC0 말단 Q | `SoHQ` | 건강 상태 목록에서는 유지율을 대표로 선택. 에너지와 용량이 수학적으로 같다는 뜻은 아님 |
| 충전 용량·CC 용량·충전 에너지·dQdV 적분 | `chgCapa` | 기존 registry의 동반 변화 계열을 대표 하나로 축소 |
| CV 용량·CV 시간·CC 비중의 대체 표현 | `chgCapa_CCratio` | CC/CV 계열 대표. CV 시간 고정 피팅인 `_norm`은 동일식으로 분류하지 않음 |
| 같은 SOC 구간의 hysteresis 면적·최대 ΔV | `hyst_area` 및 구간별 `hyst_area_*` | global/low/mid/high는 각각 유지하고 같은 구간 내부에서만 축소 |
| `Q_relax`, RPT/DCIR 및 % 표현 | `Q_relax_pct` | 표의 대표 선택. RPT와 DCIR 측정 쌍은 다르므로 `Q_relax_source`와 significant QC를 반드시 함께 사용 |
| legacy 용량·효율·평균전압 등의 구현 | 해당 cyclediag 대표 | 신규 계산 시 선택할 구현. 과거 값의 단위·필터·baseline 차이를 무시한 rename이나 병합은 금지 |

기존 registry family는 특정 시험군의 중복 판단을 재사용한 **목록 선택 정책**이다. 이번 작업에서 상관계수를 새로 계산하지 않았다. 프로토콜·온도·휴지 길이가 바뀌면 원래 사전에서 다시 분리해 검토한다. 예를 들어 EoC 60초/30분/end의 축소와 EoD 30분/end의 축소는 휴지 조건에 종속된다. EoD 60초는 별도 유지했다.

## 이름이 비슷해도 유지한 구분

- 0.1/10/30/60초 저항, SOC 20/50/80, 충전/방전 방향은 각각 유지한다.
- `CI_per_hour`, 서로 다른 OCV 추정기, 구간별 hysteresis, 서로 다른 peak 번호·위치·크기는 합치지 않는다.
- legacy `chgCapa_CCratio`는 CC 용량 유지율이고 `delta_chgCapa_CCratio`는 CC 비중 변화다. 각각 `CCcapa`, `CCratio`를 거쳐 올바른 대표에 연결했다.
- legacy `chg_dchg_capa/enrg`는 현재 충전과 이전 사이클 방전을 비교하므로 같은 사이클 효율의 역수로 없애지 않았다.
- legacy 피크는 초기 anchor·Butterworth 필터와 극대/극소 번호 체계를 사용한다. 현재 SG·MAD 기반 피크의 같은 번호와 대응시키지 않고 확인 목록으로 옮겼다.
- 대표가 실제 사전에 없으면 alias라는 이유만으로 삭제하지 않는다. 예를 들어 현재 스냅샷의 `R_SOC_diff_20_80`은 대응 대표가 수집되지 않아 유지한다.

## 파일과 코드

- `catalog/metrics_catalog.html`: 대표 목록과 접어서 볼 수 있는 108개 제외표. 대표 행에서 제외한 표현과 적용 조건을 확인한다.
- `catalog/metrics_catalog.csv`, `.parquet`: 대표 223개.
- `catalog/removed_duplicates.csv`, `.parquet`: 제외 항목, 최종 대표, 판정 종류, 이유와 연결 체인.
- `catalog/support_and_review.csv`, `.parquet`: QC·시험 조건·출처 및 확인 대기 항목.
- `catalog/pruning_decisions.csv`, `.parquet`: 전체 589개에 대한 판정. 각 항목은 정확히 한 상태에 속한다.
- `catalog/all_metrics/`: 정리 전 589개 전체 사전.
- `catalog/pruning_policy.json`, `pruning_summary.json`: 규칙 스냅샷, 건수, registry와 정책 코드 SHA-256.
- `battery_insight/dedup.py`: 대표 연결·검증·판정표 생성. 원본 코드 모듈은 실행하지 않고 AST로 규칙만 읽는다.

`catalog` 기본 명령에 자동 적용된다. `catalog --full`은 정리 전 목록만 생성한다. `prune`은 전체 사전에 규칙을 다시 적용하며, 이미 줄인 표를 입력하면 오류로 거부한다. 원본 raw·기존 export·기존 소스는 수정하지 않았고, 새 raw 파이프라인의 계산 결과 컬럼도 삭제하지 않는다. baseline과 절대 용량 같은 원래 값은 재해석을 위해 계속 보관해야 한다.
