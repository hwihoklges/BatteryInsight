# BatteryInsight 로드맵 (원점 재검토, 2026-10-24)

이전 로드맵(2026-10-22)은 [docs/archive/ROADMAP_2026-10-22.md](docs/archive/ROADMAP_2026-10-22.md)에 보존했다.

## 1. 목표 재정의

> **서로 다른 설계의 전기화학 지표와 데이터를 설계 간에 비교하는 "거미줄(네트워크)"을 만들고, 그 결과를 질의 가능한 데이터베이스로 축적한다.**

- 변환(raw → Parquet)은 별도 패키지 **`cyc2parquet`** 로 분리한다. BatteryInsight는 변환 결과 Parquet만 입력으로 받는다.
- 비교의 단위는 **설계(design)** 다. 설계 ← 물리 셀 ← 시험 ← 데이터셋(Parquet 조각) 계층을 DB에 명시한다.
- 지표는 매번 다시 계산하는 화면용 값이 아니라 **영구 테이블**(사이클 지표 → 셀 요약 → 설계 요약)로 저장한다.
- 원칙(유지): 원본 불변, 머신러닝 없음, 추정값은 `candidate`/`inferred`로 표시하고 사람이 확정, 단위 미확정이면 계산하지 않음, 같은 물리 셀 반복은 1표본.

## 2. 현재 상태 진단 (2026-10-24)

| 영역 | 상태 | 판단 |
|---|---|---|
| CYC → Parquet 변환 (`cyc2parquet` 패키지로 분리 완료) | 4100/4101 검증, 2,932 파일 변환 완료 | 완료. 독립 패키지(자체 CLI·테스트·pyproject), BatteryInsight는 `pip` 의존성으로 사용 |
| Parquet 라이브러리 (`library.py`) | 데이터셋·메타데이터·규칙·문서 연결, 스탬프 검증 | 유지 (DB의 데이터셋 계층) |
| DX 분류 (`projects/*/dx`) | 513 시험, 시험종류 추정 88.8% | 유지. 설계 후보(태그) 공급원 |
| 파생 step/cycle | step 99.96%, cycle F1 0.949 | 유지. 지표 계산의 입력 |
| 지표 (`run_metrics`) | run(시험×채널) 단위 9종: 초기 용량, N 사이클 용량·유지율, 80% 도달(censored), 퇴화 기울기, CE 중앙값, 방전 평균전압·저하. 1,537 run (ok 711 / partial 134 / insufficient 692) | 완료 (P2). 저항·휴지 지표는 미포함 |
| 설계 DB (`research_db`) | `db/research.sqlite3`: 설계 후보 306, provisional 셀 1,537, 코호트 59 (설계 2개 이상 37) | 완료 (P3). 설계·셀 확정은 사용자 입력 대기 |
| 거미줄 비교 (`design_network`) | 설계 노드, 태그 Jaccard·코호트 내 강건 z RMS 간선, 레이더, 불일치 표, 쌍 비교 (`db/network.html`) | 완료 (P4) |
| 구 raw-CSV 파이프라인 (`pipeline/ingest/adapters/features/diagnostics/experiment_updates`, `cli run/update/watch`) | 동작, 테스트 다수 | **동결**(신규 투자 없음), 차기 분리 후보 |
| 장표·추적·Copilot (`knowledge_*`, `traceability*`, `copilot_*`) | 동작 | 유지. 설계 메타데이터의 근거 공급원 |

## 3. 목표 아키텍처

```
cyc2parquet (별도 패키지)          BatteryInsight
CYC/CTS ──► Parquet(+meta) ──► ① library   데이터셋·단위·확정 메타데이터
                                 ② derived   step / cycle 사이드카
                                 ③ metrics   run 지표 (사이클 표·프로파일·관측값) (db/run_*.parquet)
                                 ④ research  설계·셀·시험·데이터셋 + 지표 요약 (db/research.sqlite3)
                                 ⑤ network   설계 거미줄·레이더·상이점        (db/network.json, network.html)
                                 ⑥ UI        데스크톱 / 작업실 / Excel 내보내기
```

### 데이터 계약 (패키지 간)

- Parquet 열: `source_row, step_time_s, voltage_native, capacity_native, cycle?, step?, total_time_s?`; 스키마 메타데이터 `format_id, source_sha256, converter`.
- 전류 열은 없다. 전류는 step의 `Δcapacity/Δt`로 **유도**하며 결과에 `derived` 표시.
- 단위는 library 메타데이터(`voltage_unit`, `capacity_unit`)가 확정해야 지표를 계산한다.

### research DB 스키마 (SQLite, `db/research.sqlite3`)

| 테이블 | 키 | 내용 |
|---|---|---|
| `designs` | design_id | 라벨, 상태(candidate/confirmed), 태그·설계 필드, 충돌, 근거 |
| `cells` | cell_id | 상태(confirmed=`physical_cell_id` / provisional=run), design_id |
| `tests` | test_key | 시험종류(+출처: 확정/이름/추정), 시작일, 온도, 압력, 프로토콜, RPT 시점, 태그 |
| `runs` / `run_datasets` | run_id (= test_key:채널) | 셀·설계·코호트 / 조각 순서·sha256 |
| `run_profile` | run_id | 공칭 용량, C-rate 등급, 전압창, 품질·사유 |
| `observations` | run_id, metric_id, window | 값, 단위, censored, 품질, 방법 버전 |
| `cell_metrics` | cell_id, cohort_key, metric_id, window | run 중앙값 |
| `design_metrics` | design_id, cohort_key, metric_id, window | 셀 수, 평균, 표준편차, 중앙값, 최소, 최대 (셀 동일 비중) |
| `cohorts`, `metric_defs`, `build_log` | | 코호트 정의·지표 정의·빌드 이력 |

## 4. 단계별 계획

검토(2026-10-24) 반영 사항:
- **식별 우선:** 지표보다 설계·셀·시험 식별을 먼저 확정 가능한 형태로 만든다. 확인된 `physical_cell_id`가 없으면 셀은 *provisional*(run = 시험×채널)로 두고 표본 수에 그 상태를 표시한다.
- **연결표:** 셀↔run↔데이터셋(조각)은 1:N 연결표로 저장하고 조각 순서를 기록한다.
- **설계 후보:** DX 태그(소재·공정·형상·공급사)를 정규화한 후보 ID를 제안하되 원문과 충돌(예: dry와 wet 동시)을 보존한다. 사용자가 `db/design_map.csv`로 확정하며 재빌드가 덮어쓰지 않는다.
- **코호트 키:** 지표는 `시험종류|온도|C-rate 등급|전압창` 코호트와 관측 창(N 사이클)을 키로 저장한다. 같은 코호트 안에서만 전기화학 유사도를 계산한다.
- **유도 전류 제한:** Δ용량/Δt는 구간 평균 전류다. C-rate 등급 분류에만 쓰고 저항 지표는 MVP에서 제외한다.
- **품질 게이트:** 완전 충·방전 쌍이 없으면 CE는 NULL, 사이클이 모자라면 유지율·기울기 NULL, 80% 미도달은 *censored*로 표시한다.
- **변환기 분리(P1)** 는 사용자 요청이므로 수행하되 다른 단계와 독립적으로 병렬 진행한다.

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| **P1 변환기 분리** (완료) | `native_cyc` → 독립 패키지 `cyc2parquet`(자체 CLI·테스트·pyproject). BatteryInsight는 의존성으로 사용 | 두 패키지 테스트 통과, 데스크톱 변환 동작, 배포 빌드 스크립트 반영 |
| **P2 run 지표** (`run_metrics`) (완료) | run(시험×채널) 단위로 조각을 이어 붙인 사이클 표, 공칭 용량·C-rate 등급·전압창(프로토콜 서명), 초기 용량·N 사이클 용량/유지율·80% 도달·퇴화 기울기·CE 중앙값·방전 평균전압 저하 | 실데이터 전체 계산, 품질 상태·사유 기록 |
| **P3 연구 DB** (`research_db`) (완료) | 설계·셀·시험·run·데이터셋 계층, 관측값, 셀 중앙값 → 설계 통계(셀 동일 비중), 코호트, 식별 공백 보고서, Excel | `db/research.sqlite3`, `research.xlsx`, `identity_gaps.csv`; 사용자 매핑 보존 |
| **P4 설계 거미줄** (`design_network`) (완료) | 설계 노드, 설계 유사도(태그 키별 Jaccard)·전기화학 유사도(코호트 내 강건 표준화 RMS) 간선, 레이더, 상이점 표, 쌍 비교 | 오프라인 단일 `db/network.html` + JSON |
| **P5 통합** (완료) | `db-build`, `network` 명령, 데스크톱 버튼, 문서 | 한 명령으로 사이드카 → 지표 → DB → 거미줄 |
| **P6 정리** | 구 raw-CSV 파이프라인 분리 여부 결정, 저항·휴지 지표(검증 후) 추가 | 사용 여부 확인 후 |

## 5. 의도적으로 하지 않는 것

- 지표·설계 의미의 자동 확정, 파일명만으로 설계 확정
- 머신러닝 모델 학습·예측
- 원본 CYC/Parquet 수정
- 단위가 확인되지 않은 값의 물리 지표화

## 6. 구현 점검 (2026-10-24, 1차 구현 후)

로드맵 대비 구현을 실데이터(PNE_initial, 2,932 파일)로 점검한 결과와 조치.

| # | 발견 | 조치 | 상태 |
|---|---|---|---|
| G1 | `cycles_summary`가 일부(50개) 데이터셋만 담긴 채 남아 있었고, 사이클 단계가 매번 전체 요약을 다시 계산함. 단위 계수가 하드코딩(mV/uAh) | 데이터셋별 sha256+단위 키로 요약 행 재사용, library 메타데이터 단위 사용(미확정이면 요약 제외) | 완료 |
| G2 | 변경 없는 재빌드도 약 100 s (NAS의 사이드카 2,932개 확인) | 단계별 입력 지문(`db/build_state.json`)으로 미변경 단계 건너뜀 → 0.1 s, `--force`로 전체 재계산 | 완료 |
| G3 | 채널 이상·집게 불량 등 `anomaly` 시험(6 run)이 설계 통계에 포함됨 | 관측값은 보존, 셀·설계 요약에서 제외, `identity_gaps.csv`에 `anomaly:*` 표시 | 완료 |
| G4 | 설계 확정이 태그 1개/시험 키 단위라 후보 설계를 통째로 확정·병합하기 어려움 | `design_candidates.csv`(설계 후보별 시험·셀 수·태그·충돌·샘플명) 생성, `design_map.csv`에 `match_type=candidate` 추가 | 완료 |
| G5 | 거미줄 기본 코호트가 설계 2개짜리, 115개 설계 코호트에서 간선 6,555개로 판독 불가 | 설계 수 많은 코호트 우선, 30개 초과 시 노드별 상위 3개 간선만 표시, 라벨은 툴팁·선택 시 | 완료 |
| G6 | 시험 온도 미상 405/513 → 코호트 키 대부분 `T?` | 파일명에 온도 근거가 거의 없음(후보 11건도 25/45 혼재). DX 분류표 `temperature_c` 입력이 필요 | 사용자 입력 |
| G7 | 설계·셀 확정 0건 (후보 306, provisional 셀 1,537) | `design_candidates.csv` → `design_map.csv`, `dx_files.csv`의 `physical_cell_id` 입력 | 사용자 입력 |
| G8 | Parquet 비교 작업실은 여전히 4개 프로파일 지표 사용 | 작업실 지표를 `run_metrics`로 교체 또는 작업실을 거미줄 쌍 비교로 대체할지 결정 | 다음 |
| G9 | 파생 사이클 기반 지표의 검증 미수행 | `metric_validation`: 라벨 있는 652 데이터셋에서 기록 사이클 vs 파생 사이클 지표 비교 (`db/metric_validation.*`, `db-build` 단계). 82.7% 완전 일치, 초기 용량 2% 이내 92.6%, 유지율@100 2% 이내 93.8%, CE 2% 이내 90.4%. 불일치 113건은 전부 펄스 프로토콜의 사이클 계수 차이(PNE TotalCycle = 스케줄 루프) | 완료 |
| G10 | 저항·휴지(rest) 지표 없음 | 펄스 구간 ΔV/ΔI(유도 전류 한계 검토 후) | 다음 (P6) |
| G11 | 변경 확인만으로 steps·cycles 단계가 각 약 25 s (NAS footer 2,932회) | 디렉터리별 `_index.json`(size/mtime 키) 캐시 → 0.5 s | 완료 |
| G12 | run_metrics가 is_full을 위해 사이클 사이드카 2,932개를 다시 읽음, research_db의 iterrows·전체 xlsx | `cycles_summary.is_full`(SUMMARY_VERSION 2), run 병렬화, groupby 집계, xlsx 축소 | 완료 |
| G13 | metric_validation 단위 계수 (V, Q) 순서 뒤바뀜 → 용량이 1000배, ZERO_AH 판정 왜곡 | 순서 수정 + 회귀 테스트 | 완료 |
| G14 | 설계 간 V–Q 곡선 비교에 원본 4 GB 필요 | `cycle_profiles` (N사이클마다 + 특수 사이클, 128점) 21 MB, `db-build` 단계 | 완료 |
| G15 | 장표와 실험 데이터 연결 없음 | `slide-link`: 장표O/데이터X, 데이터O/장표X, 둘다(설계) 목록 | 완료 (후보 검토 필요) |
| G16 | 장표 연결이 슬라이드 단어 겹침에만 의존, 2팀만, 담당자 별칭(임수현=양수현) 오류 | 별칭 제거(이름 완전일치), 2팀+4팀 다중 루트, `slide_facts` Set 구조화(설계·조건·결과수치), 장표 수치 ↔ DB 사이클 대조로 `verified`, `research.sqlite3` 표·`v_test_slides`, 네트워크 노드에 장표 참조 | 완료 |
| G17 | 장표 Set 1개 = 셀 여러 개인데 DB는 채널 단위; 장표에만 있는 설계값(로딩·기공도·비율)이 설계 카탈로그에 반영되지 않음 | 장표 설계값 → `test_slide_facts`/`design_slide_facts`(`slide_*` 별도 열, 속성 합의·불일치, 설계 문구 2종 이상 = 설계 묶음 점검 34건) 완료. 남은 것: Set 속성 → `design_map` 제안(사용자 확정), `no1-5`/`#1~#3` 셀 번호 파싱으로 채널 수준 연결 | 일부 완료 |
| G18 | 자동 매칭 오류(표지·일반 토큰 후보, 다른 Set, 구버전 덱, 우연한 ICE 일치)를 사람이 일일이 확인할 수 없음 | 513 test 전수 검토(에이전트 10개, 검토 키트) → `slides/slide_link_review.csv`(재실행 유지, 사람 수정 우선) → `confirmed`/`probable`·`no_slide`. 덱 버전군·슬라이드 사본 해시, 사번 담당자 후보, 검토 기준 자동매칭 검증(요약), 결과어 없는 페이지·표지·타인 폴더 후보 제거, ICE 허용 ±0.7 → ±0.3 %p | 완료 (사람 확인 권장) |
| G19 | 자동 매칭 정확도 한계: 자동 1순위가 검토 슬라이드와 일치 111/323, 자동 후보에 정답 포함 189/323 | Set 라벨 ↔ 샘플명 대응, 장표 날짜(도급일) 파싱, 검토 데이터로 가중치 학습(외부 의존성 없이) | 다음 |
| G20 | 장표 연결이 없는 test 186건은 "왜 없는지"를 사람이 덱을 다시 찾아봐야 알 수 있음 | `slide_context`: 시험 역할·담당자 장표 기간 대비 시점·같은 실험군의 보고 장표(샘플명 공통어 우선, 투입일·배치는 하위 실험으로 분리)·데이터 신호로 정황 추정 → `test_context`, `v_test_slide_facts.ctx_*`, `데이터만 정황` 시트. 186건 모두 정황 부여(미상 0), 검토 메모 키워드와 대조해 장표 보관 이전 32/32, 미보고 11/11, 분석용 11/11, 장비 7/7 일치. 남은 것: Set 라벨 ↔ 샘플명 대응(G19)으로 형제 장표를 Set 단위까지 좁히기 | 완료 (추정, 사람 확인 권장) |

## 7. 진행 기록

- 2026-10-24: 3.4.0 정리(원시 DB·카탈로그 생성기 제거, 스탬프 검증, 파생 step/cycle). 본 로드맵 수립.
- 2026-10-24: P1~P5 구현 (3.5.0). `cyc2parquet` 분리, `run_metrics`/`research_db`/`design_network`/`db_build`/`metric_validation`, 데스크톱·CLI 연결. 구현 점검 G1~G5, G9 반영.
- 2026-10-25: G11~G15. 빌드 효율화(전체 재계산 약 160 s → 57 s), 사이클 V–Q 프로파일 저장소, 장표↔실험 연결.
- 2026-10-25: G16. 장표 내부 Set 정보 구조화와 수치 대조, 2팀+4팀(541 덱, 7,168 슬라이드, Set 언급 7,752), 별칭 제거, DB·네트워크 연결.
- 2026-10-25: G17(일부)·G18. 513 test 전수 검토(link 323 / no_slide 156 / unsure 34) → DB `test_slide_facts`·`design_slide_facts`·`owner_candidates`·`slide_link_review`, 뷰 `v_test_slide_facts`. 사번 5개 담당자 후보(00219393 안지현, 07000664 임창윤, 07005399 양수현, 07005823 이준호(약), 07100044 박재호).
- 2026-10-25: G20. 데이터만 있는 test 186건 정황 추정(`test_context`): 장표 보관 이전 32, 선행·점검 단계(본시험 장표 있음) 30 / (본시험도 미보고) 18, 미보고(최신 장표 이후) 21, 사이클 데이터 없음 19, 분석용 셀 17, 같은 투입분 다른 실험만 보고됨 15, 같은 실험군은 보고됨 11, 장비·채널 점검 9, 담당자 장표 없음 7, 실험군 전체 미보고 7. 같은 실험군 장표 참조 55건(53건은 샘플명 공통어 있음).
