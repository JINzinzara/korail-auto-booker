# KORAIL Auto Booker

매번 주말/연휴 때 표 다 매진되고, 표 구하겠다고 몇날 몇일 12시까지 대기하다가 결제하는 생활에 빡쳐서 만들어 버린<br>**KORAIL 비공개 모바일 API를 사용한 로컬 CLI 자동 예매 도구**

> [!WARNING]
> 공식 KORAIL API 및 제품 아님 - 비공개 모바일 API 또는 macro error 정책에 따라 미작동 가능성 있음<br>
> KORAIL 계정 필요<br>
> 악용하지 말아주세요ㅠㅠ 정말 필요하신 분만 사용해 주세요

## 주요 기능

- **좌석 조회**: 출발일, 시간대, 열차 종류 지정
- **좌석 자동 재조회**: 지정 시간대에 좌석 없을 시, 설정 간격(예: 2시간)으로 자동 재조회
- **자동 예약**: 좌석 발견 시, 최신 상태 확인 후 예약
- **결제 금액 오류 방지**: 최초 승인한 최대 운임 초과 시, 결제 차단
- **중복 예약 및 결제 방지**: SQLite 트랜잭션을 통해 트레킹
- **결제 오류 방지**: 결제 결과 불명확할 시, 재결재 방지하고 발권 여부부터 확인
- **구매 완료 티켓 확인**: 프로세스 재시작 시, 저장된 예약 및 결제 상태 복구

**현재 제한 사항**:<br>
- 승객 종류 = 성인 (특가 상품 불가) 
- 전 구간 일반 좌석
- 웹 UI와 HTTP API는 제공하지 않음
- 차후 기능 추가, 리셀 방지 규칙; 동일 날짜·방향·출발시간 중복 / 같은 방향 노선 전후 2일 이내 중복 금지

## 기술 스택

| 분야 | 기술 |
|------|------|
| 언어 및 런타임 | Python 3.11 이상 |
| CLI | `argparse` |
| 데이터베이스 | SQLite (`sqlite3`) |
| 보안 입력 | `getpass` |
| KORAIL 연동 | `korail-mobile-api` — 특정 Git 커밋으로 버전 고정 |
| HTTP 및 암호화 | `httpx`, `cryptography` |
| 테스트 | `unittest` |
| 패키징 | `setuptools`, `pyproject.toml` |

## 빠른 시작

### 사전 요구사항

- Python 3.11 이상
- Git
- 인터넷 연결
- 본인 KORAIL 계정
- KORAIL 결제에 사용할 개인카드
- 실제 Android 기기의 DynaPath 식별 정보
- Python 버전 확인:

```bash
python3 --version
```

Windows:

```powershell
py --version
```

### 2. 소스 코드 받기

GitHub:

```bash
git clone https://github.com/JINzinzara/korail-auto-booker.git
cd korail-auto-booker
```

*Hugging Face: repo 전체를 내려받아 압축을 풀고, 터미널에서 해당 폴더로 이동한 뒤 아래 설치 단계 진행*

### 3. 설치

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
```

### 4. 여행(Trip) 조건 생성

*`create`는 여행 조건을 검증하고 SQLite에 `DRAFT` 상태로 저장*

macOS/Linux:

```bash
export KORAIL_DB_PATH="$PWD/korail-booker.sqlite3"
export KORAIL_DEPARTURE_STATION="?"
export KORAIL_ARRIVAL_STATION="?"
export KORAIL_TRAVEL_DATE="?"
export KORAIL_EARLIEST_DEPARTURE="?"
export KORAIL_LATEST_DEPARTURE="?"
export KORAIL_TRAIN_TYPES="?"
export KORAIL_PASSENGER_COUNT="?"

korail-booker create
```

Windows PowerShell:

```powershell
$env:KORAIL_DB_PATH="$PWD\korail-booker.sqlite3"
$env:KORAIL_DEPARTURE_STATION="?"
$env:KORAIL_ARRIVAL_STATION="?"
$env:KORAIL_TRAVEL_DATE="?"
$env:KORAIL_EARLIEST_DEPARTURE="?"
$env:KORAIL_LATEST_DEPARTURE="?"
$env:KORAIL_TRAIN_TYPES="?"
$env:KORAIL_PASSENGER_COUNT="?"

korail-booker create
```

`?`를 실제 여행 조건으로 변경<br>
여러 열차 종류는 쉼표로 구분(아래 코드 참조)

```bash
export KORAIL_TRAIN_TYPES="KTX,KTX-산천"
```

생성 결과:

```text
trip_id=1 status=DRAFT
```

### 5. 실제 조회·예약·결제 실행

**실제 API 호출, 예약, 카드 결제 사용**<br>
*`MAX_FARE_WON`: 승인 최대 결제 금액은 넉넉히 잡아주세요*

```text
KORAIL_MOBILE_API_LIVE=1
KORAIL_RESERVE_APPROVED=1
KORAIL_REAL_CHARGE_APPROVED=1
```

macOS/Linux:

```bash
export KORAIL_MOBILE_API_LIVE="1"
export KORAIL_RESERVE_APPROVED="1"
export KORAIL_REAL_CHARGE_APPROVED="1"

export KORAIL_MAX_FARE_WON="?"
export KORAIL_MEMBER_NO="본인의_KORAIL_회원번호"

export KORAIL_DYNAPATH_DEVICE_ID="실제_16자리_소문자_hex"
export KORAIL_DYNAPATH_OS_VERSION="15"
export KORAIL_DYNAPATH_DEVICE_MODEL="실제_Android_기기_모델"

korail-booker run 1
```

Windows PowerShell:

```powershell
$env:KORAIL_MOBILE_API_LIVE="1"
$env:KORAIL_RESERVE_APPROVED="1"
$env:KORAIL_REAL_CHARGE_APPROVED="1"

$env:KORAIL_MAX_FARE_WON="?"
$env:KORAIL_MEMBER_NO="본인의_KORAIL_회원번호"

$env:KORAIL_DYNAPATH_DEVICE_ID="실제_16자리_소문자_hex"
$env:KORAIL_DYNAPATH_OS_VERSION="15"
$env:KORAIL_DYNAPATH_DEVICE_MODEL="실제_Android_기기_모델"

korail-booker run 1
```

*DynaPath 값에는 임의의 예시값이 아닌 실제 Android 기기 정보를 입력*

- `KORAIL_DYNAPATH_DEVICE_ID`: Android ID, 소문자 16자리 16진수
- `KORAIL_DYNAPATH_OS_VERSION`: Android 버전 예: `15`
- `KORAIL_DYNAPATH_DEVICE_MODEL`: 실제 기기 모델 예: `SM-S928N`

*아래의 정보는 `read -s` 할 것을 권장*

- KORAIL 비밀번호
- 카드번호
- 카드 비밀번호 앞 2자리
- 카드 유효기간 `YYMM`
- 생년월일 `YYMMDD`

실행 중 좌석이 발견 시 workflow:

1. 최신 좌석 재확인
2. 예약 생성
3. 예약 운임과 `KORAIL_MAX_FARE_WON` 비교
4. 카드 결제 1회 실행
5. 승차권 목록에서 발권 여부 확인

### 6. 조회 횟수와 간격 조정

**조회 간격: 10s ~ 5s**

```bash
export KORAIL_POLL_INTERVAL_SECONDS="10"
```

조회 횟수를 제한하려면 다음 값 조정

```bash
export KORAIL_MAX_POLLS="6"
```

`KORAIL_MAX_POLLS`를 설정하지 않으면 발권되거나 오류로 종료될 때까지 무한 조회 루프

조회 횟수가 끝났지만 여행 상태가 `MONITORING`이라면 같은 `trip_id`로 재실행 가능

```bash
korail-booker run 1
```

## 폴더 구조

```text
korail-auto-booker/
├── pyproject.toml
├── README.md
├── src/
│   └── korail_booker/
│       ├── __init__.py
│       ├── app.py       # CLI, 환경변수 및 비밀정보 입력
│       ├── domain.py    # 여행·후보·예약·구매 상태 모델
│       ├── engine.py    # 조건에 맞는 최우선 열차 선택
│       ├── korail.py    # KORAIL API 연결과 응답 변환
│       ├── storage.py   # SQLite 상태 저장과 상태 전이
│       └── worker.py    # 조회·예약·결제·발권 확인 흐름
└── tests/
    ├── test_app.py
    ├── test_contract.py
    ├── test_engine.py
    ├── test_korail.py
    └── test_worker.py
```

## 아키텍처 개요



주요 상태 흐름:

```text
DRAFT
  → MONITORING
  → CLAIMING
  → RESERVED
  → PAYING
  → RECONCILING
  → TICKETED
```

> [참고 사항]<br>
> SQLite 트랜잭션과 고유번호 인덱스로 하나의 여행에 대해 하나의 구매 시도만 진행<br>
결제 요청 이후 미응답 시 자동 재결재 하지 않음 &rarr; `RECONCILING`로 상태 저장 후 승차권 목록만 재조회<br>
`CLAIMING` 상태에서 프로세스가 중단된 경우, 자동 재실행하지 않고 수동 확인이 필요

공개 Hugging Face Space나 원격 예약 서비스 형태의 운영은 권장하지 않음<br> 
현재 프로그램은 터미널 숨김 입력과 로컬 SQLite를 전제로 하며, 원격 서비스에 계정·카드정보를 전달하도록 설계되지 않음

## 주의사항

- 본인 계정과 본인 카드만 사용할 것
- 운임 상한을 실제 예상 운임보다 지나치게 높게 설정하지 말 것
- 같은 여행을 여러 프로세스에서 동시에 실행하지 말 것
- KORAIL 모바일 API 변경으로 언제든 작동이 중단될 가능성 다분
- 자동화 사용에 따른 계정, 예약, 결제 관련 책임은 사용자에게 있습니다!!!
