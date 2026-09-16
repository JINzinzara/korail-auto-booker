# korail-auto-booker

## architecture
Desktop UI
    │
    ▼
Application Service
    ├── Trip 관리
    ├── 입력 검증
    └── 사용자 승인
    │
    ▼
Single Booking Worker
    ├── Polling
    ├── Hard Filter
    ├── Soft Ranking
    ├── Reserve
    ├── Fare Guard
    ├── Payment
    └── Ticket Verification
    │
    ▼
KORAIL Gateway
    └── 비공개 KORAIL API 격리

로컬 저장소
├── SQLite
├── OS Secure Store
└── 구조화 Event Log

비동기 보조 경로
Event Log → Recovery Agent → 허용된 복구 명령