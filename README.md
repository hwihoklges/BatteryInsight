# Battery Insight — 전기화학 데이터 정리·분석 코드

## 3.2.1: PPT 근거와 공정·셀·시험·raw 이력

장표·샘플 연결 탭의 **PPT · 공정부터 raw까지 추적**에서 소재/전극 로트, 배합·건조·압착·조립·활성화 공정, 물리 셀, 개별 시험, Parquet을 연결합니다. PPT 표와 명시 항목은 검토 대기로 추출하고 각 값의 슬라이드·표 근거와 수정 이력을 남깁니다. WIP 전·후 전용 분류 없이 장표에 등록한 공정·셀·시험을 연결합니다. 셀 ID와 날짜의 일반적인 정합성은 검사합니다.

실제 PPT는 아직 등록하지 않았습니다. MD의 항목은 입력 명세로 사용했으며 실제 셀의 공정값으로 간주하지 않습니다. 별도 새 프로젝트에 합성 PPT와 CYC를 만들어 연결 동작을 볼 수 있습니다. [사용 절차·필수 항목·현재 범위](TRACEABILITY.md)를 참고하세요.

## 3.1.0: CYC 4101 지원과 수동 시작 대기 목록

4100과 4101의 서로 다른 헤더 예약 영역을 구분하고, 파일에 선언된 열 ID로 시간·용량·전압 위치를 찾습니다. 제공된 4101 파일의 모든 376,758행을 별도 바이너리 판독과 대조합니다. 자세한 구조는 [NATIVE_CYC_FORMAT.md](NATIVE_CYC_FORMAT.md)에 기록했습니다.

데이터 탭에서 `폴더 추가`를 반복하거나 `여러 하위 폴더 선택`으로 복수 선택합니다. 대기 목록에서 순서를 확인한 뒤 **시작**을 눌러야 변환합니다. 폴더 선택과 30초 변경 반영은 CYC 변환을 시작하지 않습니다. 변환 중에는 목록을 잠그며, 한 폴더가 실패해도 다음 폴더를 처리하고 각 결과를 남깁니다.

## 통합 실행판

**`BatteryInsight.exe` 하나에 CYC 직접 변환, Parquet 등록·분류 DB, 실험 등록표, 장표 근거·샘플 연결, 기존 비교 작업실과 분석 CLI를 통합했다.** Windows 배포 ZIP을 풀고 `바로실행.cmd` 또는 EXE를 실행한다. Python/인터넷/API 키가 필요 없으며 반복 실행은 LLM을 호출하지 않는다.

`데이터·실험 분류 → 장표·샘플 연결 → 지표·관계망·분석`의 한 화면에서 작업한다. 이미 변환한 CYC Parquet도 폴더 등록으로 가져올 수 있다. 단위 확인 전에는 원본 수치를 표시하고, 물리 단위 확인 후 프로파일 지표를 계산해 기존 관계망·기능 선택·셀 평균과 연결한다. 파일명 후보와 확인 정보를 구분하고, 원본/장표 기록이 바뀌면 연결을 재확인한다.

자세한 절차·지원 범위는 [통합 사용 안내](battery_insight/resources/통합사용안내.html)를 참조한다. CYC 보존 전용 지표와 전류·CC/CV가 필요한 기존 분석 지표는 입력 정보에 맞게 가용성을 표시한다. 공식 AIM 정의가 없는 항목은 여전히 미설정이며 계산식을 추정하지 않는다. 프로젝트 내부 Parquet은 상대 경로로 등록하여 프로젝트 폴더 이동을 지원하고, 외부 데이터는 참조 경로로 연결한다.

```powershell
python -m battery_insight desktop
python -m battery_insight cyc-import --project D:/BIProject --input D:/Raw
python -m battery_insight library-scan --project D:/BIProject --input D:/Parquet
python -m battery_insight library-open --project D:/BIProject
python -m battery_insight self-test
```

raw → 단위/부호 표준화 → Parquet 저장 → 전기화학 feature → 이상 지표·근거·원인 후보를 연결하는 로컬 Python 패키지다. 기존 `data_analysis`, `data_analysis_projects`, `pne_studio2` 및 Studio가 연결하는 `cyclediag`의 소스·export 계약을 확인하여 만들었다.

**코드와 로드맵에 더해, 사용자가 제공한 PNE RPT ZIP 4개·Cycle 폴더 1개의 36개 채널을 실제 변환하는 데이터베이스를 구현했다.** 실제 변환 결과는 소스 패키지 밖의 `../raw_database/`에 저장한다. 합성 회귀 검사와 실제 입력 검증을 구분해 기록하며, 머신러닝은 사용하지 않는다. 상세 계획은 [ROADMAP.md](ROADMAP.md), 검증 결과는 `validation_summary.json`을 참조한다.

2026-10-22 코드 효율 점검에서 중복 Parquet 읽기·동일 구간 분석·꺼진 상관 계산을 줄이고, 변경 없는 화면 확인에 ETag/304를 적용했다. 실제 36채널 새로고침은 3회 중앙값 **9.24 → 6.93초**였으며 전체 분석과 원곡선 응답이 전후 동일하다. 측정 조건, 반영 코드와 다음 개선 순서는 [성능 점검 보고서](performance_report.html), 재현 수치는 [performance_verification.json](performance_verification.json)에 있다. 대규모 비교의 전체 쌍 생성과 watch의 주기적 전체 재계산은 후속 개선 대상이다.

## 장표에서 실험·샘플을 먼저 등록

PPTX·PDF를 하나씩 추가해 **문서 → 페이지/슬라이드 근거 → 실험·샘플 → 확인된 raw 연결 → 지표**로 추적한다. 장표 등록 SQLite는 raw Parquet DB와 분리한다. 날짜·세트번호·샘플의 의미는 명시적으로 검토하며 파일명 후보만으로 자동 연결하지 않는다. 필요한 정보와 단계별 계획은 [장표 등록 준비 안내](knowledge_intake_guide.html)에 정리했다.

```powershell
python -m pip install -e ".[documents]"
python -m battery_insight knowledge-init --out ../experiment_knowledge --database ../raw_database
python -m battery_insight knowledge-intake --registry ../experiment_knowledge/knowledge.sqlite3 --documents "실험장표.pptx" --out ../experiment_knowledge/review
```

실제 장표는 아직 등록하지 않았다. `../experiment_knowledge/`에 빈 DB, 등록 템플릿, 기존 36개 raw 시험의 미확정 연결 준비표를 만들었다. 문서 수집만으로 실험 기록이나 연결이 확정되지는 않는다. 등록은 `knowledge-records`, 후보 생성은 `knowledge-match`, 확정·해제는 `knowledge-link`로 수행하며 전체 검토표는 `knowledge-export`로 갱신한다. 각 명령의 입력은 `--help`에서 확인한다. 한 번 발급된 `record_id`는 다음 수정 때 그대로 사용해 버전 이력을 이어간다.

시험 시작일·종료일·제조일·보고일, 샘플 ID·물리 셀 ID를 구분한다. 세트번호는 문자열로 저장해 선행 0을 보존한다. 그림으로 붙인 표·스캔 PDF·그래프 숫자는 자동으로 사실 확정하지 않으며 확인 대기다. 문서 해석이나 raw 원본이 바뀐 연결은 보류 사유를 표시한다. [합성 장표 연결 예시](demo_knowledge/knowledge_review/index.html)는 실제 사용자 자료가 아니다.

## 모든 사이클의 전압 프로파일과 지표를 따로 저장

기본 `voltage_profiles` 저장은 **모든 사이클의 시간·용량·전압을 원래 점 수와 정밀도로 보존**한다. 셀/시험/사이클/step/연속 구간, 충방전 종류, 원본 위치도 함께 저장한다. 숫자를 반올림하거나 곡선을 다운샘플링하지 않고 Parquet + Zstandard로 압축한다. 원본 파일은 수정하거나 삭제하지 않는다.

지표는 열을 버리기 **전** 원본의 전류·상태·시간 등을 사용해 계산하고 `gold/`의 별도 Parquet에 저장한다. 계산 조건이 부족한 지표는 사유와 함께 결측으로 남긴다. 검증된 로컬 전기화학 지표와 기존 AIM 관련 지표 목록은 구분하며, 확인되지 않은 AIM 공식 전체를 계산했다고 표시하지 않는다.

| 저장 선택 | 보존 내용 | 용도 |
|---|---|---|
| `voltage_profiles` (기본) | 모든 시점의 시간·용량·전압 + 구분·출처 정보 | 가벼운 전압 곡선 DB + 별도 지표 |
| `curves` | 전압 프로파일 + 전류·상태 등 핵심 열 | 전류가 필요한 분석의 재계산 |
| `full_fields` | 정규화 열과 검증된 Restore 51개 필드 전체 | 폭넓은 사후 재처리 |
| `features_only` | 지표·품질·출처만 저장 | 시계열 저장을 생략 |

`keep_columns`로 보존 열을 추가하고, `keep_cycles`/`keep_steps`로 보존 대상을 명시할 수 있다. 기본값은 전체 사이클·전체 step이다. 저장 필터는 지표 추출 후 적용하므로 지표 계산 범위를 바꾸지 않는다. 전류를 생략한 DB에서는 전압 곡선을 그대로 조회할 수 있지만 DCIR 등 전류 의존 지표를 새로 계산하려면 원본을 다시 읽어야 한다.

```powershell
python -m battery_insight database-import --config examples/database.template.json --out D:/BatteryDatabase
python -m battery_insight database-query --database D:/BatteryDatabase --table cycles --out cycle_metrics.parquet
python -m battery_insight workbench --config D:/BatteryDatabase/workbench_config.json --out D:/BatteryDatabase/workbench --open
```

`examples/database.template.json`의 경로·시험 종류·RPT 시점을 실제 값으로 바꾼다. 비교 설정은 `examples/database_workbench.template.json`을 복사해 `database_path`와 관측점/구간을 지정한다. 네이티브 입력은 이번 실제 파일로 검증한 `pne_restore_51_smallcell_v1` 스키마를 사용하며, 다른 장비 단위/포맷을 자동 추측하지 않는다. `catalog.sqlite3`는 실험·설계·출처 인덱스이고 큰 시계열과 지표는 Parquet에 저장한다. 같은 원본·설정·코드의 완료 결과는 해시를 검증해 재사용한다. 새 원본을 설정에 추가하고 다시 실행하면 새 결과를 등록한다.

실제 제공 자료용 설정·등록표·실행 파일은 `../raw_database/`에 있다. `experiments.csv`에서 `physical_cell_id`, `design_id`, `batch_id`, 소재·조성·로딩·조건을 채운 뒤 비교 화면을 새로고침한다. 원본 채널 ID는 그대로 두고 `physical_cell_id`로 여러 시험에서 같은 물리 셀을 연결한다. 자료에 없는 설계/배치/온도/SOC/CC·CV를 파일명으로 추정하지 않는다.

평균은 **같은 설계·배치·시험 조건·RPT 시점/관측 구간** 안에서 물리 셀마다 동일 비중으로 계산한다. 같은 셀의 반복 시험은 먼저 중앙값 하나로 정리하고, 여러 셀은 산술평균·표본 표준편차·유효 셀 수로 표시한다. 물리 셀이나 설계/배치가 미확인인 채널은 평균에서 제외하며 개별 시험은 계속 조회할 수 있다. RPT 내부 cycle을 수명 cycle로 오인하지 않도록 축을 나누고, 특정 내부 cycle 하나만 비교하는 `snapshot`도 지원한다.

## 기능을 선택하는 비교 작업실

**설계/전기화학 두 지도, 교차 검색, 원곡선·이상 근거, 조건을 고정한 비교, CSV/XLSX 등록, 수명 구간 변화, 반복 셀 분포, 기준 고정·갱신 이력**을 한 작업실에서 사용할 수 있다. 화면 표시와 실제 계산 항목을 각각 선택하고 이름을 붙여 저장한다. 항목 선택을 바꾸면 실제 유사도를 다시 계산한다. 빈 선택은 전체 선택으로 바뀌지 않는다.

```powershell
python -m battery_insight workbench --config my_relations.json --out D:/BatteryWorkbench --open
```

가상 12셀로 바로 보려면 **`start_demo.cmd`**를 실행한다. [작업실 화면 미리보기](demo_workbench/index.html)도 제공한다. 설정·선택 기능과 데이터 등록 방법은 [WORKBENCH.md](WORKBENCH.md)에 있다. 저장된 HTML은 미리보기이며 재계산·원곡선 새 조회·등록 저장은 로컬 서버에서 동작한다. 실제 원본 파일은 수정하지 않는다. 선택형 CC/CV·SOC별 DCIR·휴지 지표는 기본으로 꺼 두고 필요한 항목만 켤 수 있다.

## Raw 추가 시 실험 관계 지도 갱신

**셀별 시험 하나를 점으로 표시**하고, 두 가지 독립된 기준으로 연결한다. [가상 raw 12셀 예시](demo_relations/index.html)에서 같은 셀을 선택한 채 지도를 바꿀 수 있다.

| 지도 | 비교 항목 | 확인할 차이 |
|---|---|---|
| 전극 설계·재료 | 양·음극 활물질, 고체전해질·바인더·도전재, 전극 조성, 로딩 | 어떤 설계가 비슷하거나 다른가 |
| 전기화학 지표 | 같은 관측 구간의 중복 정리된 대표 지표 | 어떤 반응 패턴이 비슷하거나 다른가 |

이웃 비교표는 두 유사도를 나란히 보여준다. 설계가 비슷한데 전기화학 결과가 다른 셀, 설계가 달라도 지표가 비슷한 셀을 찾는 용도다. 두 점수를 하나로 합산하지 않는다. 소재명은 정확 일치, 숫자는 설정한 차이 기준, 조성은 완전한 질량비 벡터로 비교한다. 로딩 `mAh/cm²`와 `mg/cm²`는 별개다. 실제 설계값은 등록표에 입력하며 raw에서 추정하지 않는다.

```powershell
python -m battery_insight update --config my_relations.json --out D:/BatteryRelations
python -m battery_insight watch --config my_relations.json --out D:/BatteryRelations --interval 30 --port 8765
```

watch 실행 중 새 raw·등록표를 확인하고 `http://127.0.0.1:8765/`에서 두 지도를 갱신한다. 설정 템플릿은 `examples/relations.template.json`, `examples/experiments.template.csv`다. 미등록·기록 중·실패 파일은 상태 표에 표시한다. 전기화학 지도에는 레이더와 셀 단위 Spearman 상관행렬도 있다. 머신러닝은 사용하지 않는다. 입력 기준과 사용법은 [RELATIONS.md](RELATIONS.md)를 참조한다.

## 우선 사용할 기능: 지표 목록 정리 (머신러닝 없음)

현재 목표는 AIM 관련 지표를 포함한 **기존 전기화학 feature를 나열하고 정의·단위·출처별로 정리**하는 것이다. 모델 학습·예측은 필수 범위에서 제외했다. 아래 명령은 raw를 분석하지 않고 코드의 지표 사전을 만든다.

```powershell
python -m battery_insight catalog --cyclediag-root C:/Users/LGES/Cursor/cyclediag --studio-root C:/Users/LGES/Cursor/pne_studio2 --out catalog
```

`catalog/metrics_catalog.html`은 **중복 정리 후 대표 지표 223개**를 검색하는 기본 목록이며, `metrics_catalog.csv`는 같은 목록의 Excel용 표다. 전체 589개 중 108개를 중복 표현·계열 대표 선택·구현 대체로 제외했다. 품질·조건·진단 등 보조 150개와 정의/출력 확인이 필요한 108개는 따로 보존한다. 전체 사전은 `catalog/all_metrics/`, 제외 항목과 대표·근거는 `catalog/removed_duplicates.csv`, 전체 판정은 `catalog/pruning_decisions.csv`에 있다. 자세한 선정 기준은 [PRUNING.md](PRUNING.md)를 참조한다. 기존 실제 export의 생성된 컬럼까지 대조하려면 `--exports path/to/cycle_indicators.csv`를 추가한다. export의 **헤더만** 읽으며 셀 데이터 분석은 하지 않는다.

`catalog` 명령은 앞으로도 기본으로 대표 지표를 선택한다. 정리 전 전체 사전만 만들려면 `--full --out catalog_full`을 쓴다. 저장한 **전체 사전**에 정리 규칙만 다시 적용하려면:

```powershell
python -m battery_insight prune --catalog catalog/all_metrics/metrics_catalog.parquet --registry C:/Users/LGES/Cursor/cyclediag/features/indicator_registry.py --out catalog
```

이미 줄인 `catalog/metrics_catalog.*`를 `prune`에 넣으면 원래 항목이 사라지므로 오류로 거부한다. 같은 전체 사전으로 재실행하면 같은 대표를 선택한다. 정리는 **기본 목록의 선택**이며 원본 extractor, raw, 기존 export 값은 그대로다. 원값·baseline을 보관하고, 실제 값 병합·단위 변환에는 이 선택표를 사용하지 않는다.

별도 AIM 명세는 아직 확인되지 않아 AIM에 속한 모든 지표라고 단정하지 않는다. 같은 이름의 다른 정의, alias의 수치 변환, 확인되지 않은 단위를 드러내는 구조다. 아래 raw 파이프라인은 최초 요청에 맞춰 함께 구현한 선택 기능이며 목록 작성에 필수는 아니다.

## 구현 범위

| 단계 | 구현 |
|---|---|
| 데이터 입력 | 헤더가 있는 CSV / Parquet, 명시 컬럼·단위·전류 부호, 청크 처리, 한 cycle의 청크 경계 연결 |
| 정리·저장 | 읽기 전용 raw, Bronze/Silver/Gold Parquet(zstd), 파일 SHA-256, 원본 데이터 레코드 번호, 설정·코드·환경 해시 |
| 기본 지표 | 충방전 용량/에너지, CE·retention, CC/CV 용량, 평균 전압, 온도, 공급된 SOC 기준 10→80% 시간 |
| 전기화학 지표 | 단조 CC 구간 dQ/dV 피크, 실제 저SOC dV/dQ 및 별도 throughput proxy, 유효 pulse의 0.1/1/10/30/60 s DCIR, 휴지 전압 변화, 조건이 확인된 EoC 60 s 저항 |
| 수명 관측 | Cycle/Insitu 공통 life_index, 초기 cutoff 경향, 연속 threshold EOL 확인, 미도달/관측 중단 표시 |
| 이상 검토 | 초기 기준 대비 robust deviation, 선택 threshold, 지표별 원인 후보·근거·출처·검토상태; 가중 합산 위험 점수 없음 |
| 기존 코드 연결 | cyclediag wide CSV, data_analysis L2 JSONL, 검증된 DB export를 명시 매핑하여 long Parquet로 보존 |
| 외부 지표 분석 | 선택한 외부 metric을 raw cycle에 명확히 연결한 뒤 진단. 단위·버전·cutoff 충돌 검사 |
| 변수 통제 | 셀 레지스트리의 목표 변수와 통제 변수 비교, 교란·누락 정보·n=1 경고 |
| 결과 확인 | 자체 포함 HTML, CSV, Parquet, 사람 검토 append 로그, source-code inventory |

**AIM은 실제 의미·모델·지표 명세를 확인하지 못해 기본 `not_configured`다.** AIM 계산식이나 성능을 추정해서 넣지 않았다. 동일 adapter에 실제 export를 매핑하면 연결할 수 있다. 학습된 수명 예측 모델·EIS/GITT 직접 계산·자동 셀 로그 Excel 정규화·자동 프로토콜 분류는 후속 범위다.

## 실행

Python 3.12 이상. 압축을 푼 프로젝트 디렉터리에서:

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

이미 필요한 패키지가 있다면 설치 없이 프로젝트 디렉터리에서 `python -m battery_insight`로 실행할 수 있다. 검증 당시 버전은 `requirements-tested.txt`와 `validation_summary.json`에 기록했다.

합성 데이터 예제:

```powershell
python examples/make_demo.py --out ../../work/demo_input
python -m battery_insight run --config ../../work/demo_input/demo.json --out ../../work/demo_lake
```

출력된 `runs/<run_id>/report.html`을 열면 검토 표를 볼 수 있다. 이 예제는 **가상 셀 1개, 8사이클**이며 실제 성능이나 실제 이상 판정이 아니다.

실제 데이터 설정은 `examples/run.template.json`을 복사해 경로·셀/시험 ID·단위·시험 종류를 확인한 뒤 사용한다.

```powershell
python -m battery_insight run --config my_run.json --out D:/DataLake/insight
```

`D:/DataLake/insight`는 운영 출력 위치 예시다. 현재 경로 존재·여유 용량은 보장하지 않는다. 이번 구현물을 저장한 작업 공간과 운영 데이터 레이크는 별도다.

## 입력을 설정할 때

- `sources[]` 하나는 **하나의 셀·하나의 시험을 담은 파일**이다. 여러 시험이 섞인 `.cyc`는 기존 변환기에서 시험별로 분리한다. cycle 번호가 되돌아가면 통합하지 않고 실패한다.
- `cell_id`, `test_id`는 명시한다. filename/channel 번호로 셀을 추측하지 않는다. 같은 물리 파일을 여러 셀로 이중 등록할 수 없다.
- 숫자의 크기로 A/mA, Ah/mAh, V/mV를 판단하지 않는다. `columns.<target>.unit`에 입력 단위를 쓴다. 전류는 `charge_positive`, `discharge_positive`, `unsigned_by_step` 중 명시한다.
- `test_type`을 source 상수로 지정하거나 per-record 열에 `value_map`을 둔다. 시험 종류가 혼합된 파일에 전체 `Cycle`을 부여하지 않는다. 미확인 값은 `Unknown`이다.
- `mode`는 명시된 CC/CV 정보를 사용한다. `end_flag`는 실제 종료 조건을 확인한 뒤 `complete_end_flags`와 맞춘다. 숫자 `1`을 무조건 정상 완료라고 가정하지 않는다.
- 용량 적분은 **측정된 segment 내부**에서만 한다. measured capacity는 별도 `*_capacity_measured_ah`로 보존한다. 시작 시각이 0이 아닌 구간·누락된 step clock·불완전 종료에서는 완료 근거가 부족하므로 CE/retention이 비어 있을 수 있다.
- `assume_complete_bidirectional_cycles=true`는 사용자가 완결성을 별도 검증했을 때만 켠다. `completion_assumed`로 기록되며 기본값은 false다.
- `source_row`는 헤더를 제외한 **1부터 시작하는 데이터 레코드 번호**다. quoted multiline CSV의 물리 줄 번호와 다를 수 있다. raw SHA와 함께 사용한다.
- 자동 outlier 삭제·전류 오프셋 보정·단위 추정은 없다. `include=false`도 계산 결과에 남고 진단·수명 요약에서 제외된다. 변경 이유는 review 또는 manifest 관리 이력에 기록한다.

한 cycle의 데이터가 기본 2,000,000행을 초과하면 명시적으로 중단한다. 청크 크기는 `chunk_rows`, cycle buffer 상한은 `max_cycle_rows`로 지정한다. 전체 raw를 RAM에 읽지 않지만 Gold 요약과 외부 export는 메모리에 보유한다.

## 기존 지표·AIM 연결

`external_features`에는 검증된 export와 명시 매핑을 넣는다. 아래는 기존 코드에서 확인된 **cyclediag의 EoC 60 s 저항(mohm)** 예시다. placeholder는 실제 정보로 바꿔야 한다.

```json
{
  "path": "cycle_indicators.csv",
  "mapping": {
    "namespace": "cyclediag",
    "layout": "wide",
    "columns": {"cell_id": "cell_id", "cycle": "cycle"},
    "constants": {
      "test_id": "실제 raw와 동일한 test_id",
      "definition_version": "검증한 export 정의 버전",
      "source_revision": "실제 전체 40자리 Git commit 또는 64자리 코드 SHA256",
      "observed_until_cycle": 100
    },
    "metrics": {
      "EoC_dchgR_60s": {
        "metric_id": "EoC_dchgR_60s",
        "unit": "mohm",
        "role": "indicator",
        "family": "eoc_discharge_r60"
      }
    }
  }
}
```

외부 export 자체의 SHA-256은 reader가 계산한다. `source_revision`은 export를 **생성한 코드**의 버전이다. `observed_until_cycle`은 지표 계산이 실제로 참조한 마지막 원본 cycle이다. smoothing이나 baseline이 전체 시험을 사용했다면 행의 cycle을 무조건 cutoff로 넣으면 안 된다.

입력은 CSV/Parquet/JSONL, layout은 `wide` 또는 `long`이다. `known_export_mapping()`은 확인한 `cyclediag` 및 `data_analysis_l2_cycles`의 기초 capacity/CE/retention 매핑을 만든다. L2의 CE는 0–1, cyclediag CE는 %이므로 namespace·단위를 유지한다. pickle은 읽지 않는다.

외부 지표의 실제 이상 검토를 켜려면 top-level 설정에 추가한다:

```json
{
  "external_diagnostics": {
    "baseline_window": 5,
    "min_baseline_cycles": 3,
    "allow_retrospective": false,
    "metrics": {
      "cyclediag.EoC_dchgR_60s": {
        "unit": "mohm",
        "absolute_floor": 1.0,
        "direction": "high"
      }
    }
  }
}
```

1.0 mohm 수준의 floor는 예제 수치 허용폭이며 검증된 이상 임계값이 아니다(`mohm` 단위). 현재 cutoff가 100이면 cycle 1–99 지표는 기본 분석에서 보류된다. `allow_retrospective=true`는 명시적인 회고 분석에만 사용하고 초기 수명 예측 자료로 사용하지 않는다. 원본 cycle과 일치하지 않는 외부 행은 경고로 남으며, 서로 다른 정의/단위/버전을 임의로 병합하지 않는다.

AIM은 실제 schema를 받아 `namespace="aim"`으로 동일하게 연결한다. AIM이 모델이라면 모델 버전·학습 범위·타깃·prediction cutoff·불확실성 정의도 별도 명세로 보존해야 한다. 범용 초기 slope를 AIM이라고 표시하지 않는다.

## 분석 결과 읽기

| 파일 | 의미 |
|---|---|
| `gold/cycles.*` | source cycle·life_index, 용량·효율·곡선 지표, complete/unknown/incomplete, source row 범위 |
| `gold/pulses.*` | pulse/rest의 실제 시점·ΔV/ΔI·기준값·보간 양끝 원본 행 |
| `gold/quality.*` | 시간 리셋·누락·구간 부족·미완료·단위/외부 연결 문제 |
| `gold/diagnostics.*` | 지표별 기준 대비 변화, 근거, 후보 원인, 신뢰 수준, pending review |
| `gold/life_summary.*` | 관측 EOL·확인 cycle·censored 상태 및 cutoff 내 초기 경향 |
| `gold/external_features.*` | 기존 코드/AIM export의 단위·버전·cutoff를 유지한 long table |
| `input_snapshot.json`, `run_manifest.json` | 입력·설정·코드·런타임·산출물 해시 |
| `feature_dictionary.json` | 이번 실행의 컬럼·단위·정의 코드 위치 |

NaN은 0이 아니라 **계산 불가/자료 부족**이다. dQ/dV 피크는 가장 긴 단조 CC 구간에서 찾은 지배적 수치 피크이며 전극 반응에 자동 배정하지 않는다. derivative moving average는 5점, prominence는 `max(미분 범위×5%, 1e-8)`로 현재 코드 버전에 고정된다. 실제 peak tracking/다중 피크·chemistry 배정은 기존 cyclediag export를 연결한다.

진단 baseline은 같은 셀·시험·시험 종류의 초기 완료 사이클이다. threshold/floor는 조건·장비 해상도에 맞춰 검증해야 하며 `calibration_required` 근거가 표시된다. CE 감소 한 항목만으로 미세단락·LLI·plating을 확정하지 않는다. 저항·dV/dQ·온도·휴지·프로토콜/채널 기록을 함께 검토한다.

초기 slope는 `life.cutoff_cycle` **수명 index** 이내를 사용한다(입력에 life_index가 없으면 source cycle fallback). EOL은 용량 유지율 threshold를 연속 만족해야 확인된다. 관측 종료 cycle을 실제 수명으로 대체하지 않는다. 학습/검증 split·모델 학습·예측 성능 평가 코드는 이번 버전에 포함하지 않았다.

## 변수 비교·검토 기록·inventory

```powershell
python -m battery_insight compare --registry cells.csv --cell-a CELL_A --cell-b CELL_B --target pressure_mpa --variables pressure_mpa temperature_c cathode anode loading_mg_cm2 np_ratio protocol_hash --out comparison.json
python -m battery_insight review --run D:/DataLake/insight/runs/RUN_ID --item "diagnostics:CELL_A:cycle30:EoC_dchgR_60s_mohm" --decision needs_evidence --reason "동일 SOC·온도 비교 필요" --reviewer "담당자"
python -m battery_insight inspect --roots C:/Users/LGES/Cursor/data_analysis C:/Users/LGES/Cursor/data_analysis_projects C:/Users/LGES/Cursor/pne_studio2 C:/Users/LGES/Cursor/cyclediag --out inventory.json
```

`compare`는 명시된 변수 목록 안에서만 통제를 검사한다. 목록 밖 교란은 검출하지 않으며, n=1 쌍으로 집단 통계나 인과성을 주장하지 않는다. review는 append 로그이며 원시값이나 feature를 수정하지 않는다. `reference_inventory.json`에는 이번에 확인한 2,947파일의 코드 해시·정적 후보 위치를 넣었다. 크기 제한 15건과 동적 컬럼 생성 때문에 전체 의미 검토를 완료한 목록은 아니다.

## 저장·재실행·지원 경계

- run ID는 입력 내용·설정·코드·라이브러리 버전에 의해 결정된다. 성공한 동일 run은 산출물 해시를 확인한 뒤 재사용한다.
- 실패 시 `.RUN_ID.staging/failure.json`을 남기고 완료 catalog에 등록하지 않는다. 실패 원인을 수정한 뒤 같은 identity로 다시 실행해야 하면 해당 staging 디렉터리를 **출력 경로 안에서만** 별도로 보관/정리한다. 패키지가 자동 삭제하지 않는다.
- SQLite는 소형 실행 목록이다. Parquet는 DuckDB로 직접 query 가능하며 기존 datalake가 준비되면 catalog를 연결한다. 선택 설치: `pip install -e ".[sql]"`.
- `.cyc`, headerless Restore, ragged StepEnd, `.mpr` 직접 파서는 없다. 먼저 검증된 기존 변환기에서 표준 CSV/export로 변환한다.
- 펄스 시작 시각·기준 휴지·안정 전류·target time coverage가 부족하면 DCIR을 외삽하지 않는다. 1 s rest를 자동으로 물리적 Rct라고 부르지 않는다.
- 원본 삭제/이동, 스케줄 등록, 클라우드 업로드, 기존 소스 코드 수정은 수행하지 않았다.

설계·검증 단계와 회사 도구 대조 계획은 [ROADMAP.md](ROADMAP.md)를 따른다.
