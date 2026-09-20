"""macOS Keychain과 기존 application service를 연결하는 최소 데스크톱 화면"""

import ctypes
import os
import secrets
import sys
import threading
import tkinter as tk
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from tkinter import messagebox, ttk

from .app import create_trip, run_trip
from .domain import Trip
from .korail import create_card_payment
from .storage import TripStore

KEYCHAIN_FIELDS = (
    "KORAIL_MEMBER_NO",
    "KORAIL_PASSWORD",
    "KORAIL_CARD_NUMBER",
    "KORAIL_CARD_PASSWORD",
    "KORAIL_CARD_EXPIRE",
    "KORAIL_CARD_BIRTHDAY",
)
FIELD_SPECS = (
    ("KORAIL_DEPARTURE_STATION", "출발역", "서울", False),
    ("KORAIL_ARRIVAL_STATION", "도착역", "부산", False),
    ("KORAIL_TRAVEL_DATE", "여행일 (YYYY-MM-DD)", "", False),
    ("KORAIL_EARLIEST_DEPARTURE", "출발 시작 (HH:MM)", "", False),
    ("KORAIL_LATEST_DEPARTURE", "출발 종료 (HH:MM)", "", False),
    ("KORAIL_TRAIN_TYPES", "열차 종류", "KTX", False),
    ("KORAIL_PASSENGER_COUNT", "성인 인원", "1", False),
    ("KORAIL_MAX_FARE_WON", "최대 결제금액 (원)", "", False),
    ("KORAIL_MEMBER_NO", "코레일 회원번호", "", False),
    ("KORAIL_PASSWORD", "코레일 비밀번호", "", True),
    ("KORAIL_CARD_NUMBER", "카드번호", "", True),
    ("KORAIL_CARD_PASSWORD", "카드 비밀번호 앞 2자리", "", True),
    ("KORAIL_CARD_EXPIRE", "유효기간 (YYMM)", "", True),
    ("KORAIL_CARD_BIRTHDAY", "생년월일 (YYMMDD)", "", True),
)
_ITEM_NOT_FOUND = -25300


@lru_cache(maxsize=1)
def _frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """macOS Security와 CoreFoundation 함수 시그니처를 한 번만 준비"""
    if sys.platform != "darwin":
        raise RuntimeError("macOS Keychain is only available on macOS")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    uint32 = ctypes.c_uint32
    void_p = ctypes.c_void_p
    char_p = ctypes.c_char_p
    security.SecKeychainFindGenericPassword.argtypes = [
        void_p,
        uint32,
        char_p,
        uint32,
        char_p,
        ctypes.POINTER(uint32),
        ctypes.POINTER(void_p),
        ctypes.POINTER(void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(void_p)]
    security.SecKeychainCopyDefault.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        void_p,
        uint32,
        char_p,
        uint32,
        char_p,
        uint32,
        void_p,
        ctypes.POINTER(void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        void_p,
        void_p,
        uint32,
        void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [void_p, void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core.CFRelease.argtypes = [void_p]
    core.CFRelease.restype = None
    return security, core


def _keychain_names(name: str) -> tuple[bytes, bytes]:
    """환경변수 이름을 Keychain service와 account 바이트로 변환"""
    return f"korail-auto-booker.{name}".encode(), b"local"


def _default_keychain(security: ctypes.CDLL) -> ctypes.c_void_p:
    """현재 사용자의 기본 Keychain 참조를 가져오기"""
    keychain = ctypes.c_void_p()
    status = security.SecKeychainCopyDefault(ctypes.byref(keychain))
    if status != 0:
        raise RuntimeError(f"Keychain access failed ({status})")
    return keychain


def keychain_get(name: str) -> str | None:
    """macOS Keychain에서 비밀값을 읽고 없으면 None 반환"""
    security, core = _frameworks()
    service, account = _keychain_names(name)
    keychain = _default_keychain(security)
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        status = security.SecKeychainFindGenericPassword(
            keychain,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
    finally:
        core.CFRelease(keychain)
    if status == _ITEM_NOT_FOUND:
        return None
    if status != 0:
        raise RuntimeError(f"Keychain read failed ({status})")
    try:
        return ctypes.string_at(data, length.value).decode()
    finally:
        security.SecKeychainItemFreeContent(None, data)
        core.CFRelease(item)


def keychain_set(name: str, value: str) -> None:
    """비밀값을 프로세스 인자에 노출하지 않고 macOS Keychain에 추가 또는 갱신"""
    security, core = _frameworks()
    service, account = _keychain_names(name)
    keychain = _default_keychain(security)
    encoded = value.encode()
    data = ctypes.cast(ctypes.c_char_p(encoded), ctypes.c_void_p)
    existing_length = ctypes.c_uint32()
    existing_data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        status = security.SecKeychainFindGenericPassword(
            keychain,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(existing_length),
            ctypes.byref(existing_data),
            ctypes.byref(item),
        )
        if status == _ITEM_NOT_FOUND:
            status = security.SecKeychainAddGenericPassword(
                keychain,
                len(service),
                service,
                len(account),
                account,
                len(encoded),
                data,
                None,
            )
        elif status == 0:
            try:
                security.SecKeychainItemFreeContent(None, existing_data)
                status = security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(encoded), data
                )
            finally:
                core.CFRelease(item)
    finally:
        core.CFRelease(keychain)
    if status != 0:
        raise RuntimeError(f"Keychain write failed ({status})")


def save_keychain_secrets(values: Mapping[str, str]) -> None:
    """필수 계정·개인카드 값을 검증한 뒤 Keychain에 저장"""
    missing = [name for name in KEYCHAIN_FIELDS if not values.get(name, "").strip()]
    if missing:
        raise ValueError(f"{missing[0]} is required")
    create_card_payment(
        values["KORAIL_CARD_NUMBER"],
        values["KORAIL_CARD_PASSWORD"],
        values["KORAIL_CARD_EXPIRE"],
        values["KORAIL_CARD_BIRTHDAY"],
    )
    for name in KEYCHAIN_FIELDS:
        keychain_set(name, values[name])


def booking_environment(values: Mapping[str, str], database: Path) -> dict[str, str]:
    """화면 입력을 기존 create·run application service 환경으로 변환"""
    return dict(values) | {
        "KORAIL_DB_PATH": str(database),
        "KORAIL_MOBILE_API_LIVE": "1",
        "KORAIL_RESERVE_APPROVED": "1",
        "KORAIL_REAL_CHARGE_APPROVED": "1",
        "KORAIL_POLL_INTERVAL_SECONDS": "10",
    }


def application_database() -> Path:
    """macOS Application Support에 SQLite 디렉터리를 만들고 경로 반환"""
    directory = Path.home() / "Library/Application Support/korail-auto-booker"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "korail-booker.sqlite3"


def _prepare_live_environment() -> None:
    """한 GUI 세션에서 사용할 합성 Android DynaPath 신원을 준비"""
    os.environ.setdefault("KORAIL_MOBILE_API_LIVE", "1")
    os.environ.setdefault("KORAIL_DYNAPATH_DEVICE_ID", secrets.token_hex(8))
    os.environ.setdefault("KORAIL_DYNAPATH_OS_VERSION", "15")
    os.environ.setdefault("KORAIL_DYNAPATH_DEVICE_MODEL", "Android")


class DesktopApp:
    """여행 저장, Keychain 보관, 실예약 실행을 제공하는 macOS 화면"""

    def __init__(self, root: tk.Tk) -> None:
        """화면 상태와 SQLite 저장소를 초기화"""
        self.root = root
        self.database = application_database()
        self.store = TripStore(self.database)
        self.values = {
            name: tk.StringVar(value=default) for name, _, default, _ in FIELD_SPECS
        }
        self.trip_id = tk.StringVar()
        self.approved = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="여행 조건을 입력하세요.")
        self.stop_event = threading.Event()
        self.running = False
        self._build()
        self._load_keychain()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        """표준 Tk 위젯으로 입력·승인·상태 화면을 구성"""
        self.root.title("KORAIL Auto Booker")
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=18)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="KORAIL 자동 예매", font=("Helvetica", 18, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 12), sticky="w"
        )
        for row, (name, label, _, hidden) in enumerate(FIELD_SPECS, start=1):
            ttk.Label(frame, text=label).grid(
                row=row, column=0, padx=(0, 12), pady=3, sticky="w"
            )
            ttk.Entry(
                frame,
                textvariable=self.values[name],
                width=34,
                show="●" if hidden else "",
            ).grid(row=row, column=1, pady=3, sticky="ew")
        row = len(FIELD_SPECS) + 1
        ttk.Label(frame, text="여행 ID").grid(row=row, column=0, pady=3, sticky="w")
        ttk.Entry(frame, textvariable=self.trip_id, width=34).grid(
            row=row, column=1, pady=3, sticky="ew"
        )
        row += 1
        ttk.Checkbutton(
            frame,
            text="좌석 발견 시 실제 예약과 카드 결제를 승인합니다.",
            variable=self.approved,
        ).grid(row=row, column=0, columnspan=2, pady=(10, 4), sticky="w")
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, pady=8, sticky="ew")
        ttk.Button(buttons, text="Keychain 저장", command=self._save_keychain).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(buttons, text="여행 저장", command=self._save_trip).pack(
            side="left", padx=6
        )
        self.run_button = ttk.Button(buttons, text="예약 시작", command=self._start)
        self.run_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(
            buttons, text="중지 요청", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(6, 0))
        row += 1
        ttk.Label(frame, textvariable=self.status, wraplength=480).grid(
            row=row, column=0, columnspan=2, pady=(6, 0), sticky="w"
        )

    def _input_values(self) -> dict[str, str]:
        """현재 화면의 모든 입력값을 문자열 사전으로 복사"""
        return {name: variable.get().strip() for name, variable in self.values.items()}

    def _load_keychain(self) -> None:
        """저장된 계정·카드 값을 화면의 마스킹 필드로 불러오기"""
        try:
            for name in KEYCHAIN_FIELDS:
                value = keychain_get(name)
                if value is not None:
                    self.values[name].set(value)
        except Exception as error:
            self.status.set(str(error))

    def _save_keychain(self) -> None:
        """검증된 계정·카드 입력을 macOS Keychain에 저장"""
        try:
            save_keychain_secrets(self._input_values())
        except Exception as error:
            messagebox.showerror("Keychain 저장 실패", str(error))
            return
        self.status.set("계정과 카드 정보를 macOS Keychain에 저장했습니다.")

    def _save_trip(self) -> None:
        """현재 여행 조건을 결제 없는 DRAFT로 SQLite에 저장"""
        try:
            trip = create_trip(
                self.store,
                booking_environment(self._input_values(), self.database),
            )
        except Exception as error:
            messagebox.showerror("여행 저장 실패", str(error))
            return
        self.trip_id.set(str(trip.id))
        self.status.set(f"여행 {trip.id} 저장 완료: DRAFT")

    def _start(self) -> None:
        """최종 승인 후 저장된 여행의 자동 예약을 백그라운드에서 시작"""
        if self.running:
            return
        if not self.approved.get():
            messagebox.showwarning("승인 필요", "실제 예약·결제 승인을 체크하세요.")
            return
        try:
            trip_id = int(self.trip_id.get())
            values = self._input_values()
            environment = booking_environment(values, self.database)
            max_fare = int(values["KORAIL_MAX_FARE_WON"])
        except ValueError:
            messagebox.showerror("입력 오류", "여행 ID와 최대 결제금액을 확인하세요.")
            return
        if not messagebox.askyesno(
            "실제 결제 확인",
            f"좌석 발견 시 최대 {max_fare:,}원까지 실제 카드 결제합니다. 계속할까요?",
        ):
            return
        self.running = True
        self.stop_event.clear()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set(f"여행 {trip_id} 좌석을 감시하고 있습니다.")
        threading.Thread(
            target=self._run,
            args=(trip_id, environment),
            daemon=True,
        ).start()

    def _run(self, trip_id: int, environment: Mapping[str, str]) -> None:
        """GUI를 멈추지 않고 기존 application service를 실행"""
        try:
            trip = run_trip(
                self.store,
                trip_id,
                environment,
                stop_requested=self.stop_event.is_set,
            )
        except Exception as error:
            self.root.after(0, self._finish, None, str(error))
            return
        self.root.after(0, self._finish, trip, None)

    def _stop(self) -> None:
        """새 구매를 시작하지 않도록 worker에 안전한 중지를 요청"""
        self.stop_event.set()
        self.status.set("안전한 시점에 중지하고 있습니다.")

    def _finish(self, trip: Trip | None, error: str | None) -> None:
        """백그라운드 실행 결과를 화면에 반영하고 버튼을 복구"""
        self.running = False
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if error is not None:
            self.status.set(f"실행 실패: {error}")
            messagebox.showerror("실행 실패", error)
        elif trip is not None:
            self.status.set(f"여행 {trip.id} 상태: {trip.status.value}")

    def _close(self) -> None:
        """실행 중 창 종료를 막아 결제 결과 불명확 상태를 예방"""
        if self.running:
            messagebox.showwarning(
                "실행 중", "먼저 중지 요청 후 완료될 때까지 기다리세요."
            )
            return
        self.root.destroy()


def main() -> None:
    """합성 기기 환경을 준비하고 macOS 데스크톱 화면 실행"""
    _prepare_live_environment()
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
