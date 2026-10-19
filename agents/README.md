# agents

멀티 에이전트 엔진. [NVIDIA NeMo Agent Toolkit(NAT)](https://github.com/NVIDIA/NeMo-Agent-Toolkit) 위에서 라우터가 질문을 전문 에이전트로 보내고, 에이전트는 MCP 도구로 데이터를 조회해 답함. backend는 주로 OpenAI 호환 `/v1/chat/completions`로 호출.

## 구성

```
agents/
├── configs/
│   ├── router.yml            기본 설정 (사용자 메모리 켜짐)
│   ├── router_nomemory.yml   메모리 끈 설정 (FINUS_MEM0_ENABLED=0)
│   ├── common.yml            LLM·공용 도구 등록
│   ├── agents/*.yml          에이전트별 역할·도구 (news, trading, strategy, recommend, monitoring, diary, verifier)
│   └── prompts/*.md          에이전트별 ReAct 시스템 프롬프트
├── src/finus_agents/         NAT 플러그인 패키지
│   ├── agents.py             라우터, 환각 게이트(답변 수치를 도구 결과와 대조), 대화 기록
│   ├── tool_first_react.py   첫 턴에 데이터 도구 호출을 강제하는 ReAct 에이전트
│   ├── finus_api.py          MCP 도구 래퍼, KIS 조회 전용 래퍼, backend API 호출
│   ├── verifier.py           AI 주문 제안 검증 에이전트
│   ├── order_price_guard.py  지정가 괴리 검사
│   ├── pii_guard.py, pii_mask.py  도구 결과의 개인·계좌 정보 마스킹
│   ├── user_memory.py, mem0_local.py  투자 성향 메모리 (로컬 저장)
│   ├── register.py           NAT 플러그인 등록
│   └── chat_cli.py           터미널 대화 클라이언트
├── scripts/
│   ├── run.sh                로컬 실행
│   ├── patch_vendor.py       NAT 라이브러리 패치 (Docker 빌드·로컬 실행 공용)
│   └── kis_tr_verdicts.py    KIS 조회 허용 목록 판정표 재생성
└── tests/
```

## 실행

- Docker: 루트의 `bash scripts/run_stack.sh`가 `finus-nat` 서비스로 함께 실행 (포트 8001).
- 로컬: `bash agents/scripts/run.sh` → 터미널에서 대화.
  - `--nomemory`: 메모리 끈 설정 사용
  - `--once`: 질문 한 번만 실행하고 종료
- 테스트: `uv run --project agents pytest agents/tests`

## 외부 의존

- **KIS Trading MCP 서버**: 에이전트의 계좌·시세 조회 도구(`kis-trading-mcp-tool-readonly`)가 사용. [open-trading-api](https://github.com/koreainvestment/open-trading-api)의 `MCP/Kis Trading MCP`를 포트 3300으로 실행 (기본 3000에서 변경 필요). 서버에 준 `MCP_ACCESS_TOKEN`을 `.env`의 `FINUS_KIS_TRADING_MCP_TOKEN`에 같은 값으로 넣음. 주소는 `FINUS_KIS_TRADING_MCP_URL`로 변경 가능.
- **mcp/**: 뉴스·공시·잔고 도구는 저장소의 `mcp/` 서버를 stdio로 직접 실행.

## 알아둘 규칙

- 에이전트는 **주문을 낼 수 없음**. KIS 도구는 조회 전용 래퍼로만 연결되고, 주문은 backend 텔레그램 주문 경로(`/buy`·`/sell`·자연어 주문·`/advise`)에서 사용자가 확정해야만 나감. 설정이 주문 가능한 도구에 닿으면 `tests/test_agent_configs.py`가 실패.
- 사용자 메모리는 투자 성향(안정형·공격형) 하나만 로컬(`agents/.state/mem0`)에 저장. 대화 내용·계좌·금액은 저장하지 않음.
- 새 에이전트는 `finus_tool_first_react_agent` 타입으로 추가 (첫 턴 데이터 조회 강제).
