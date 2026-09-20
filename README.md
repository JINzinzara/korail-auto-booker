# korail-auto-booker

KORAIL 비공개 모바일 API를 사용하는 로컬 CLI 자동 예매 도구입니다. 공식 KORAIL 제품이 아니며 KORAIL과 제휴·승인 관계가 없습니다.

## 배포 원칙

- Hugging Face에는 소스 코드만 공개합니다.
- 사용자는 코드를 내려받아 자신의 PC에서 실행합니다.
- 공개 Hugging Face Space나 원격 예약 서비스로 운영하지 않습니다.
- 비공개 API와 자동화 차단 정책 변경에 따라 언제든 동작이 중단되거나 계정 사용이 제한될 수 있습니다.

## 요구 사항

- Python 3.11 이상
- Windows, Linux 또는 macOS
- 본인 KORAIL 계정과 개인카드

```bash
git clone <HUGGING_FACE_REPOSITORY_URL>
cd korail-auto-booker
```

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

## 실행

여행 생성 전 다음 환경변수를 설정합니다.

```text
KORAIL_DB_PATH
KORAIL_DEPARTURE_STATION
KORAIL_ARRIVAL_STATION
KORAIL_TRAVEL_DATE              YYYY-MM-DD
KORAIL_EARLIEST_DEPARTURE       HH:MM
KORAIL_LATEST_DEPARTURE         HH:MM
KORAIL_TRAIN_TYPES              예: KTX
KORAIL_PASSENGER_COUNT
```

```bash
korail-booker create
```

출력된 `trip_id`를 확인한 뒤 라이브 실행값을 설정합니다.

```text
KORAIL_MOBILE_API_LIVE=1
KORAIL_RESERVE_APPROVED=1
KORAIL_REAL_CHARGE_APPROVED=1
KORAIL_MAX_FARE_WON
KORAIL_MEMBER_NO
KORAIL_DYNAPATH_DEVICE_ID        16자리 hex
KORAIL_DYNAPATH_OS_VERSION       예: 15
KORAIL_DYNAPATH_DEVICE_MODEL     예: Android
```

```bash
korail-booker run <trip_id>
```

`KORAIL_PASSWORD`와 카드 관련 환경변수를 설정하지 않으면 다음 값을 터미널에서 숨김 입력합니다.

- KORAIL 비밀번호
- 카드번호
- 카드 비밀번호 앞 2자리
- 카드 유효기간 `YYMM`
- 생년월일 `YYMMDD`

좌석이 발견되면 승인 운임 상한 안에서 실제 예약과 카드 결제가 실행됩니다.

## 데이터와 보안

- 계정 비밀번호와 카드정보는 파일·SQLite·Keychain에 저장하지 않습니다.
- 숨김 입력값은 현재 프로세스 메모리에서만 사용합니다.
- SQLite에는 여행 상태와 중복 결제 방지에 필요한 예약 식별값만 저장합니다.
- 결제 결과가 불명확하면 자동 재결제하지 않고 승차권 조회로 복구합니다.

## 구조

```text
CLI → Application Service → Booking Worker → KORAIL Gateway
                         ↘ SQLite 상태 저장
```
