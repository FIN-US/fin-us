# backend

FastAPI 서버. 대시보드 API, 10분 주기 감시 스케줄러, 텔레그램 봇을 한 프로세스에서 실행.

## 구성

```
backend/
├── main.py         FastAPI 앱 진입점 (API 라우트, 기동 시 스케줄러·텔레그램 폴러 시작)
├── core/           설정(.env 로드)·DB 엔진·모델·스키마·Redis 상태·WebSocket
├── repositories/   DB 접근 (관심 종목, 예정(촉매) 이벤트, 걸러진 신호, 체결 통지)
├── ai/             외부 지능 호출
│   ├── llm.py        LLM 호출 단일 진입점 llm_chat (OpenAI·Anthropic·Ollama·NAT)
│   ├── mcp_tools.py  MCP 서버 도구 실행 run_mcp_tool
│   ├── signals.py    뉴스·공시 신호 유의성 채점
│   └── analysis.py   종목 분석 리포트·모닝 브리핑
├── scheduler/      주기 작업 (APScheduler)
│   ├── jobs.py       작업 등록
│   ├── monitor.py    10분 주기 감시·알림·자동 제안
│   ├── portfolio.py  잔고·시세 동기화
│   ├── catalyst.py   예정(촉매) 이벤트 사전 알림
│   ├── trade_notification.py  체결 통지 재배달
│   └── deps.py       작업들이 함께 쓰는 외부 의존성
├── telegram/       텔레그램 봇
│   ├── poller.py     getUpdates 폴링 루프, 봇 시작·종료
│   ├── commands.py   명령 디스패치 (핸들러 조립)
│   ├── handlers/     명령 묶음별 처리 (orders·lookup·settings·base·texts·trend)
│   ├── notifier.py   메시지 전송·재시도
│   ├── delivery_alarm.py  전송 실패 집계·정지 알람
│   └── presentation.py  메시지 틀·용어 각주 (terms.json)
├── trading/        주문 경로 (주문 객체·확정 대기, AI 주문 제안과 한도 검사, 자동 제안 룰, 종목코드 판정)
├── pii/            외부 LLM으로 나가는 개인·계좌 정보 마스킹
├── scripts/        보조 스크립트 (초기 설정 CLI, 테스트 알림 전송, 신호 평가셋 수집)
├── tests/          pytest
└── schema_docs.md  DB 스키마 (ER 다이어그램)
```

## 개발

```bash
uv sync --project backend
uv run --project backend pytest backend/tests        # 테스트
uv run --project backend pyright --project backend   # 타입 검사
```

- import는 `backend.…` 절대 경로만 사용.
- 외부 LLM 호출은 반드시 `ai/llm.py`의 `llm_chat`을 거침 (개인정보 마스킹 지점). 다른 경로로 공급자를 부르면 `tests/test_services.py`의 우회 검사가 실패.
- Docker에서는 `docker compose restart backend`로 코드 변경 반영.
