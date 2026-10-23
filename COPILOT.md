# 회사 Copilot CLI 연결

BatteryInsight 3.3.0의 선택 기능이다. 별도 LLM API나 로컬 모델을 사용하지 않는다. 회사에서 설치·허용한 **GitHub Copilot CLI**를 호출하며 로그인, 회사망·프록시, 모델 허용 정책은 해당 CLI가 담당한다. 인증 토큰을 BatteryInsight에 입력하거나 저장하지 않는다. 모델 ID를 비우면 회사 CLI의 기본 모델을 사용한다.

현재 개발 PC의 PATH에서는 실제 Copilot CLI를 찾지 못했다. 어댑터·입출력·검토 등록은 합성 장표와 모의 CLI로 검증하며, 실제 회사 계정 로그인과 모델 응답 품질은 검증하지 않았다.

## 같은 PC에서 사용

1. 기존 장표·샘플 연결에서 PPTX/PDF/Excel을 등록한다.
2. PPT · 공정부터 raw까지 추적 → **회사 Copilot CLI**를 연다.
3. 설치한 copilot.exe, copilot.cmd 또는 copilot.ps1을 선택한다. 회사 CLI에 이미 로그인한 상태여야 한다. 앱은 로그인·설치·업데이트를 자동 실행하지 않는다.
4. **CLI 설치·옵션 확인**으로 실행 파일과 지원 옵션을 확인한다. 이는 계정 로그인 성공이나 모델 호출 성공을 뜻하지 않는다.
5. 장표를 선택하고 **선택 장표 해석 시작**을 누른다. 요청별 제한 시간은 기본 180초이다.
6. PPT 검토 대기에서 Copilot 후보의 원문·단위·셀 대응을 확인한 뒤 등록한다.

Copilot은 선택한 장표의 추출된 텍스트·표만 읽는다. 원본 곡선·Parquet 행·다른 폴더 전체를 보내지 않는다. 현재 이미지 표·그래프 인식과 DB 자유 질의는 구현 범위에 포함되지 않는다.

## 데이터 PC에 인터넷이 없는 경우

1. 데이터 PC에서 장표를 선택하고 **오프라인 요청 파일만 만들기**를 누른다. Copilot 설치나 통신이 필요하지 않다.
2. 프로젝트의 knowledge/copilot/requests 폴더에서 요청 JSON을 회사 Copilot이 가능한 PC로 옮긴다. 요청 파일에는 선택 장표의 텍스트·표가 들어 있으므로 회사의 자료 이동 절차를 따른다.
3. 회사 PC에서 BatteryInsight를 실행하고 같은 Copilot 창의 **다른 PC의 요청 파일 실행**을 사용한다. 빈 프로젝트여도 실행할 수 있다. CLI가 응답을 요청 파일 옆의 .result.json으로 저장한다.
4. 응답을 데이터 PC로 가져와 **Copilot 응답 파일 가져오기**를 누른다. 원래 장표가 등록된 프로젝트에서 가져와야 한다.
5. 장표 SHA, 근거 ID, 위치·원문이 현재 프로젝트와 맞는지 검사한 뒤 검토 대기에 추가한다.

한 장표는 작은 요청 여러 개로 나뉠 수 있다. 모든 요청을 순차 실행한다. 중간에 오류가 나면 이미 만든 응답은 보존하며 다시 실행할 때 재사용한다. 동일 요청·모델 설정의 응답은 재사용해 중복 호출을 줄인다. CLI 기본 모델이 바뀌더라도 기존 응답은 이전 해석 결과로 재사용될 수 있으므로 모델을 비교하려면 허용된 모델 ID를 명시한다.

## 처리 범위

- 폴더 감시나 앱 시작만으로 Copilot을 호출하지 않는다. 선택한 실행 버튼이나 명시적 CLI 명령이 있어야 한다.
- Copilot 이용량은 회사 구독 정책에 따른다. 변환·DB·기존 비교는 계속 오프라인으로 동작하며 Copilot을 호출하지 않는다.
- 장표 내용은 명령이 아닌 분석할 원문으로 취급한다. 도구 없는 임시 추출 에이전트와 CLI 도구 제한 옵션을 사용하며 프로젝트·raw 폴더를 작업 폴더로 주지 않는다. 회사 CLI 자체의 관리 설정과 로그 정책은 그대로 적용된다.
- 응답은 JSON 구조, 허용 필드, 제공한 근거 ID, 원문에 있는 값인지 검사한다. 이 검사는 값의 의미가 올바르게 연결됐다는 보장이 아니므로 최종 확인이 필요하다.
- 후보만 추가한다. 소재·셀·시험 확정, 공정 관계 생성, raw 연결을 자동 실행하지 않는다. 원문에 없는 ID, 추정 날짜·단위 환산값은 반영하지 않는다.
- 명령 실행 실패·시간 초과·형식 오류 때 다른 API나 로컬 모델로 우회하지 않는다. CLI의 로그인이나 정책 오류는 회사 CLI에서 확인한다.
- 전송 요청과 응답은 프로젝트에 보관한다. 모델 가중치 재학습은 수행하지 않는다.

## 명령 실행

    BatteryInsight.exe copilot-check --copilot-exe C:/Tools/copilot.exe
    BatteryInsight.exe copilot-prepare --project D:/BIProject --documents DOCUMENT_ID
    BatteryInsight.exe copilot-run --input REQUEST.json --copilot-exe C:/Tools/copilot.exe
    BatteryInsight.exe copilot-import --project D:/BIProject --input RESPONSE.result.json

자동화 규격은 GitHub 공식 [프로그램 실행 문서](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)와 [CLI 명령 문서](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)를 기준으로 한다. 필요한 옵션이 없는 구버전은 명시적으로 오류 처리한다.
