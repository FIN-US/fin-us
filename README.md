# 📈 Fin-Us: Multi-Agent AI Investment Orchestrator

Fin-Us는 **MCP (Model Context Protocol)** 아키텍처와 **멀티 에이전트 워크플로우**를 결합한 차세대 지능형 투자 시스템입니다. 독립적인 인격을 가진 에이전트들이 각자의 도구(MCP)를 활용해 협업하며, 뉴스 분석부터 실제 매매 집행까지의 복잡한 의사결정을 자율적으로 수행합니다.

## 🤖 Multi-Agent Ecosystem

Fin-Us는 단일 모델이 모든 일을 처리하지 않고, 역할이 분리된 전문 에이전트들이 협력합니다. 각 에이전트의 페르소나와 지침은 `finus_nat/configs/agents/`의 YAML 설정을 통해 관리됩니다.

| 에이전트 (Agent) | 페르소나 (Persona) | 주요 역할 및 목표 |
| :--- | :--- | :--- |
| **News Analyst** | 시장 심리 분석가 | 뉴스, 수급, 리서치 리포트를 분석하여 시장 심리 지수(0~100) 산출 |
| **Trading Executor** | 자산 운용 관리자 | 계좌 잔고 및 수익률 기반 리스크 관리 및 매매 관점(BUY/SELL/HOLD) 제안 |
| **Strategy Planner** | 전략 수립가 | 뉴스/수급/잔고 데이터를 종합하여 구체적인 매매 시나리오 및 규칙 제안 |
| **Recommend Agent** | 투자 아이디어 뱅크 | 모멘텀, 촉매, 리스크 관점에서 종목별 투자 권고안 및 아이디어 정리 |
| **Monitoring Agent** | 포트폴리오 파수꾼 | 실시간 잔고 및 시장 변화를 관찰하여 포트폴리오 건전성 유지 및 알림 |
| **Diary Agent** | 매매 복기 기록가 | 매매 기록, 감정 정리, 투자 일지 초안 작성 및 성찰 지원 |

## 🚀 주요 특징

- **NAT 기반 멀티 에이전트**: `finus_nat` 레이어를 통해 라우터 및 브랜치 구조의 복잡한 에이전트 워크플로우를 제어합니다.
- **YAML 기반 에이전트 설정**: 에이전트의 성격, 배경지식, 작업 지침을 코드가 아닌 YAML 파일로 관리하여 유연한 튜닝이 가능합니다.
- **관심사 분리 (SoC)**: MCP를 통해 도구(Tool)를 분리하고, 에이전트별로 역할을 분산하여 LLM의 할루시네이션(환각)을 최소화했습니다.
- **실시간 지식 확장**: 공식 API 기반 MCP 서버를 통해 뉴스와 정형 금융 데이터에 접근합니다.

## 🏗️ 시스템 아키텍처

시스템은 '관심사 분리' 원칙에 따라 다음과 같이 구성됩니다.

1.  **Orchestration Layer (`backend/`)**: Python (FastAPI)
    - 외부 요청을 수신하고 전체 시스템의 진입점 역할을 수행합니다.
    - 에이전트의 분석 결과를 통합하여 프론트엔드에 제공합니다.
2.  **NAT Layer (`finus_nat/`)**: 멀티 에이전트 워크플로우 엔진
    - 라우터 및 브랜치 구조를 통해 사용자 요청에 최적화된 에이전트를 매칭합니다.
    - 전문화된 6종의 에이전트 협업 프로세스를 제어하며, MCP 서버와의 인터페이스를 담당합니다.
3.  **Tooling Layer (MCP Servers)**:
    - **News Provider (`mcp-news/`)**: 네이버 뉴스 검색 API 기반 최신 뉴스 공급.
    - **Trading Provider (`mcp-trading/`)**: 한국투자증권 Open API 기반 정형 금융 데이터 공급 및 명령 집행.
    - **Disclosure Provider (`mcp-dart/`)**: OpenDART 공식 API 기반 5% 룰·임원/주요주주 지분공시 signal 및 실적 리포트 공급.
4.  **Presentation Layer (`frontend-react/`)**: React (TypeScript)
    - 실시간 투자 신호 및 분석 리포트를 시각화하여 사용자에게 제공합니다.

## 🛠️ 설치 및 시작하기

### 사전 준비 사항

- Python 3.13
- Node.js & npm
- API Keys: OpenAI/Anthropic API, 한국투자증권(KIS) API Key/Secret, Naver Search API, OpenDART API Key

### 1. NAT 에이전트 설정

에이전트의 페르소나, 도구 구성, 라우팅 지침은 NAT 레이어에서 정의합니다. `finus_nat/configs/agents/` 폴더 내의 YAML 파일을 수정하여 각 에이전트의 성격과 작업 지침을 관리할 수 있습니다.

```yaml
# 예시: finus_nat/configs/agents/news_agent.yml
functions:
  news_agent:
    _type: react_agent
    tool_names:
      - finus_market_news
      - finus_investor_trading
    additional_instructions: |
      역할: 시장 심리 분석가
      뉴스와 외국인/기관 매매 동향을 종합 분석합니다.
```

### 2. 환경 변수 설정

Fin-Us는 모든 설정을 단일 루트 `.env` 파일에서 관리합니다. 처음 실행하는 경우 초기 설정 CLI로 `.env`를 생성합니다.

```bash
bash scripts/setup_env.sh
```

기존처럼 `.env.example`을 참고해 `.env`를 직접 편집할 수도 있습니다.

### 3. 시스템 가동

```bash
# 1. MCP Servers (News, Trading, DART) 빌드
(cd mcp-news && npm ci)
(cd mcp-trading && npm ci)
(cd mcp-dart && npm ci)

# 2. Backend Orchestrator 실행
# 프로젝트 루트 디렉토리에서 실행하는 것을 권장합니다.
uv sync --project backend
uv run --project backend uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 3. Frontend 실행
cd frontend-react && npm ci && npm run dev
```

## 📝 에이전트별 보유 스킬 (MCP Tools)

| 에이전트             | 스킬 이름              | 설명                                         |
| :------------------- | :--------------------- | :------------------------------------------- |
| **News Analyst**     | `get_market_news`      | 네이버 뉴스 검색 API 기반 최신 동향 수집      |
|                      | `get_investor_trading` | KIS API 기반 기관/외국인 수급 데이터 분석     |
|                      | `get_disclosure_signal` | OpenDART 공식 API 기반 지분공시 signal 조회   |
|                      | `get_earnings_report`  | OpenDART 공식 API 기반 매출·영업이익·순이익 YoY 실적 리포트 |
|                      | `get_research_reports` | deprecated: 공식 대체 API 미선정으로 비활성화 |
| **Trading Executor** | `get_balance`          | 실시간 계좌 잔고 및 수익률 확인              |
|                      | `execute_trade`        | 매수/매도 주문 실행 (KIS API)                |

Backend는 `GET /api/v1/disclosures?stock=삼성전자`로 DART 지분공시 signal을 제공하며, 스케줄러는 뉴스 signal과 함께 공시 signal도 주기적으로 수집합니다.
관심 종목의 DART signal은 촉매 이벤트 캘린더에도 저장되며, 매일 오전 D-1/D-0 이벤트를 Telegram으로 사전 알림합니다.

## 📅 로드맵

- [x] 멀티 에이전트 협업 구조 설계 (YAML-based NAT Layer)
- [x] MCP 기반 뉴스 수집 및 트레이딩 도구 통합
- [x] **네이버 증권 리서치 리포트 분석 에이전트 구현**
- [x] LLM(ChatGPT/Claude) 기반 전략 수립 파이프라인 완성
- [x] **포트폴리오 모니터링 및 실시간 알림 에이전트 도입**
- [ ] NVIDIA NeMo Guardrails를 이용한 투자 가이드라인 준수 레이어 추가
- [ ] 기술적 분석(차트) 고도화 및 보조지표 분석 도구 추가
- [ ] AWS App Runner & Docker 기반 클라우드 배포

---

## Docker로 한번에 설치하기

```bash
bash scripts/setup_env.sh
bash scripts/setup_deps.sh
```

또는:

```bash
bash scripts/run_stack.sh
```

- 로컬에서 `uvicorn --reload`만 쓰고 싶다면 볼륨 마운트된 소스로 호스트에서 실행하면 됩니다.

### 주문 멱등 원장 영속화

`/buy`·`/sell` 확정 주문이 중복 제출되는 것을 막는 마지막 방어선은 `mcp-trading/order-dedup.js`의 파일 기반 원장입니다. 백엔드 측 방어(`backend/telegram_commands.py`의 `pending_orders`)는 프로세스 메모리에만 존재하므로 재시작하면 사라지지만, 이 원장은 파일에 남아 컨테이너가 재생성되어도 최근 주문 이력을 유지합니다.

Docker Compose에서는 `KIS_ORDER_DEDUP_PATH` 기본값에 따라 원장이 호스트의 `./.state/kis-order-dedup.json`에 저장됩니다(`.:/app` 바인드 마운트 덕분). 이 경로는 `.gitignore`·`.dockerignore`에 등록되어 있어 커밋되지도, 이미지에 구워지지도 않습니다.

`.env`나 셸 환경변수로 `KIS_ORDER_DEDUP_PATH`를 직접 지정하면 위 기본값을 덮어씁니다. 이때 지정하는 값은 **호스트 경로가 아니라 컨테이너 내부 경로**입니다. 바인드 마운트된 `/app` 아래를 벗어난 경로를 넣으면 오류 없이 컨테이너 쓰기 계층에 원장이 만들어지고, 재생성과 함께 사라집니다.

> **주의**: `scripts/reset_clean.sh`나 `docker compose down -v`는 이 파일을 지우지 않습니다(바인드 마운트라 호스트에 그대로 남습니다). 다만 사용자가 `./.state/`를 직접 삭제하면 TTL이 만료될 때까지 중복 주문 방어선이 사라집니다.

기본 TTL은 120초(`mcp-trading/order-dedup.js:6`)로 사용자의 연타 클릭을 막기 위한 값입니다. 재빌드를 동반한 재배포는 보통 2분을 넘기므로 TTL이 이미 만료된 뒤라, 원장을 영속화해도 재배포 사이의 중복 주문까지는 막지 못합니다. 배포 창 전체를 덮으려면 `KIS_ORDER_DEDUP_TTL_MS`를 상향 조정하세요.

원장 파일에는 주문 요청 body와 KIS 응답이 그대로 저장되며, 여기에는 계좌번호(`CANO`, `mcp-trading/order.js:81-82`)가 평문으로 포함됩니다. 생성 시 파일 권한이 `0600`(`mcp-trading/order-dedup.js:121`)으로 지정되지만 이는 Linux 호스트에서만 의미가 있고, 어느 경우든 호스트 파일시스템에 그대로 남는다는 점을 유의하세요.

### Telegram 긴급 알림 수동 테스트

Docker Compose가 실행 중이고 `.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`가 설정되어 있으면 backend 컨테이너에서 테스트 알림을 보낼 수 있습니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py
```

메시지 포맷만 확인하려면 실제 전송 없이 dry-run으로 실행합니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py --dry-run
```

### Telegram 모닝 브리핑

Backend 스케줄러는 매 거래일 오전 8시 30분에 Telegram 모닝 브리핑을 자동 전송합니다.

브리핑은 `mcp-news`, `mcp-trading`, NAT Strategy Planner 흐름을 사용해 아래 항목을 요약합니다.

- 오늘의 시장 요약: 전일 미국/선물 시장 동향과 주요 이슈
- 관심종목 동향: `/watch` 관심 종목별 최신 뉴스와 외국인·기관 수급
- 오늘의 트레이딩 아이디어: 장 시작 전 참고할 간략 시나리오
- 주요 촉매 이벤트: 당일/금주 실적, 배당락, 공시 등 확인 필요 이벤트

모닝 브리핑은 정기 브리핑 메시지이므로 긴급 분석 알림 게이트와 분리되어 있습니다. `/alerts urgent|all|off|status`는 스케줄러 분석 알림의 전송 범위를 제어하며, 모닝 브리핑 자체의 발송 스케줄은 APScheduler의 `morning_briefing` 작업으로 관리합니다.

같은 Telegram 봇에서 명령을 사용할 수 있습니다. 기본 알림 모드는 긴급 분석만 전송하는 `urgent`입니다.

### 명령어 목록

**알림 설정**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/alerts urgent\|all\|off\|status` | 알림 모드 변경 또는 현재 상태 확인 | 🚨 긴급만 · 📣 전체 · 🔕 끄기 · 🔎 현재 상태 |

**조회**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/balance` | 예수금·총자산·보유 종목 조회 | 🔄 새로고침 |
| `/watch list` | 관심 종목 목록 조회 | 📋 목록 새로고침 |
| `/watch add <종목명>` | 관심 종목 추가 (스케줄러 자동 모니터링 대상에 포함) | 📋 목록 새로고침 |
| `/watch remove <종목명>` | 관심 종목 삭제 | 📋 목록 새로고침 |
| `/catalysts <종목명>` | 관심 종목의 예정 촉매 이벤트 조회 (실적·배당·공시·주주총회) | - |
| `/quote <종목명>` | 현재가 조회 | 📊 수급 보기 |
| `/trend <종목명>` | 외국인·기관·개인 수급 조회 | 💵 현재가 보기 |
| `/earnings <종목명> [기간]` | DART 실적과 최신 뉴스 기반 구조화 리포트 생성. 기간 예: `2025Q1`, `2025FY` | - |
| `/visualize` | `VISUALIZATION_URL`에 설정된 Unity 포트폴리오 시각화 링크 제공 | - |

`/earnings`는 OpenDART 실적 데이터와 Naver 뉴스를 NAT News Analyst에 전달합니다. OpenDART가 제공하지 않는 컨센서스/시장 기대치 데이터는 추정하지 않고 데이터 없음으로 표시합니다. Telegram 응답은 Markdown이 아닌 일반 텍스트이며 첫 줄에 `🟢 호재`, `🔴 악재`, `⚪ 중립` 판정을 표시합니다.

`/visualize`는 홈서버나 Tailscale 시연 환경에서 접근 가능한 Unity WebGL URL을 그대로 안내합니다. 예: `VISUALIZATION_URL=http://100.x.y.z:8080/`

**매매**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/buy <종목명> <수량> [지정가]` | 60초 매수 확인 대기 생성. 지정가 생략 시 시장가 | ✅ 확정 · ❌ 취소 |
| `/sell <종목명> <수량> [지정가]` | 60초 매도 확인 대기 생성. 지정가 생략 시 시장가 | ✅ 확정 · ❌ 취소 |
| `/confirm` | 대기 중인 주문 실행 | - |
| `/cancel` | 대기 중인 주문 취소 (증권사 제출 주문은 취소 불가) | - |

**안내**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/help` | 사용 가능한 명령 확인 | 💰 잔고 · 🔔 알림 · 🧾 매매 · 🔎 조회 |
| `/trade` | 매수·매도 입력 안내 | 매수 입력법 · 매도 입력법 |
| `/lookup` | 현재가·수급 조회 입력 안내 | 현재가 입력법 · 수급 입력법 |

> 슬래시 명령 대신 `삼성전자 1주 시장가로 매수해줘`, `NAVER 2주 200,000원에 매도해줘`처럼 자연어로 입력해도 동일한 주문 확인 대기가 생성됩니다. 자연어 주문도 실제 제출 전 `확정` 버튼 또는 `/confirm`이 필요합니다.
>
> 실계좌 주문은 `KIS_ORDER_ENV=real`과 `KIS_REAL_ORDER_ENABLED=true`가 모두 설정되어야 실행됩니다.

`mcp-trading/data/stocks.json`은 KIS 공개 코스피/코스닥 종목 마스터 기반의 종목명 해석 캐시입니다. 신규 상장 등으로 종목명이 잡히지 않으면 6자리 종목코드를 직접 입력하거나 아래 명령으로 캐시를 갱신합니다.

```bash
python3 mcp-trading/scripts/update_stock_master.py
```

슬래시 명령이 아닌 일반 텍스트는 NAT 채팅으로 전달됩니다. Telegram 채팅은 `telegram:{chat_id}` conversation id를 사용하므로 스케줄러 분석 리포트와 대화 이력이 섞이지 않습니다.

---

## ⚖️ 참고
- 본 프로젝트는 **학술적 목적**의 캡스톤 디자인 결과물이며, 일체의 상업적 목적이 없습니다.


--- 

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=20232A)
![TypeScript](https://img.shields.io/badge/TypeScript-6-3178C6?logo=typescript&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-8-646CFF?logo=vite&logoColor=white)
![Node.js](https://img.shields.io/badge/Node.js-24-5FA04E?logo=nodedotjs&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-1.29-000000)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-API-412991?logo=openai&logoColor=white)
![Anthropic](https://img.shields.io/badge/Anthropic-API-191919)
![OpenDART](https://img.shields.io/badge/OpenDART-API-005BAC)
![KIS](https://img.shields.io/badge/KIS-Open%20API-003B71)
![Naver](https://img.shields.io/badge/Naver-Search%20API-03C75A?logo=naver&logoColor=white)
