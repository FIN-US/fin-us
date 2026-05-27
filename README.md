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
    - **Disclosure Provider (`mcp-dart/`)**: OpenDART 공식 API 기반 5% 룰 및 임원·주요주주 지분공시 signal 공급.
4.  **Presentation Layer (`frontend-react/`)**: React (TypeScript)
    - 실시간 투자 신호 및 분석 리포트를 시각화하여 사용자에게 제공합니다.

## 🛠️ 설치 및 시작하기

### 사전 준비 사항

- Python 3.12+
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

프로젝트 루트 디렉토리의 `.env.example`을 참고하여 `.env` 파일을 생성합니다. Fin-Us는 이제 모든 설정을 단일 루트 `.env` 파일에서 관리합니다.

```bash
# 프로젝트 루트의 .env 파일 생성 및 편집
cp .env.example .env
# OpenAI, Anthropic, KIS, Naver, DART API 키 및 Ollama 설정을 입력하세요.
```

### 3. 시스템 가동

```bash
# 1. MCP Servers (News, Trading, DART) 빌드
(cd mcp-news && npm ci)
(cd mcp-trading && npm ci)
(cd mcp-dart && npm ci)

# 2. Backend Orchestrator 실행
# 프로젝트 루트 디렉토리에서 실행하는 것을 권장합니다.
uv sync --project backend
uv run --project backend uvicorn backend.main:app --host 0.0.0.0 --port 8787

# 3. Frontend 실행 (Unity WebGL)
cd frontend/Build
python -m http.server 8080
```

브라우저에서 `http://localhost:8080`을 엽니다.

Unity WebGL 빌드는 `index.html`을 더블클릭해서 `file://` URL로 실행할 수 없습니다. 브라우저가 `Build/Build.data`, `Build/Build.wasm` 같은 Unity 산출물 다운로드를 차단하므로, 로컬 웹 서버로 `frontend/Build` 폴더를 서빙해야 합니다.

프론트엔드 Unity 프로젝트를 수정하거나 다시 빌드해야 하는 경우에만 Unity Editor가 필요합니다. 단순 실행만 하는 팀원은 위 명령으로 커밋된 WebGL 빌드 결과물을 실행하면 됩니다.

## 📝 에이전트별 보유 스킬 (MCP Tools)

| 에이전트             | 스킬 이름              | 설명                                         |
| :------------------- | :--------------------- | :------------------------------------------- |
| **News Analyst**     | `get_market_news`      | 네이버 뉴스 검색 API 기반 최신 동향 수집      |
|                      | `get_investor_trading` | KIS API 기반 기관/외국인 수급 데이터 분석     |
|                      | `get_disclosure_signal` | OpenDART 공식 API 기반 지분공시 signal 조회   |
|                      | `get_research_reports` | deprecated: 공식 대체 API 미선정으로 비활성화 |
| **Trading Executor** | `get_balance`          | 실시간 계좌 잔고 및 수익률 확인              |
|                      | `execute_trade`        | 매수/매도 주문 실행 (KIS API)                |

Backend는 `GET /api/v1/disclosures?stock=삼성전자`로 DART 지분공시 signal을 제공하며, 스케줄러는 뉴스 signal과 함께 공시 signal도 주기적으로 수집합니다.

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
cp .env.example .env
# 키 입력 후:
bash scripts/setup_deps.sh
```

또는:

```bash
bash scripts/run_stack.sh
```

- 로컬에서 `uvicorn --reload`만 쓰고 싶다면 볼륨 마운트된 소스로 호스트에서 실행하면 됩니다.

### Telegram 긴급 알림 수동 테스트

Docker Compose가 실행 중이고 `.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`가 설정되어 있으면 backend 컨테이너에서 테스트 알림을 보낼 수 있습니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py
```

메시지 포맷만 확인하려면 실제 전송 없이 dry-run으로 실행합니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py --dry-run
```

같은 Telegram 봇에서 알림 범위를 바꿀 수 있습니다. 기본값은 긴급 알림만 전송하는 `urgent`입니다.

```text
/alerts urgent  # high/critical 긴급 분석만 전송
/alerts all     # NAT 분석이 실행될 때마다 전송
/alerts off     # Telegram 분석 알림 중지
/alerts status  # 현재 알림 모드 확인
/help           # 사용 가능한 Telegram 명령 확인
/balance        # 예수금·총자산·보유 종목 조회
/quote <종목명> # 현재가 조회
/trend <종목명> # 외국인·기관·개인 수급 조회
/buy <종목명|6자리코드> <수량> [지정가]  # 60초 매수 확인 대기 생성
/sell <종목명|6자리코드> <수량> [지정가] # 60초 매도 확인 대기 생성
/confirm       # 대기 중인 수동 주문 실행
/cancel        # 대기 중인 수동 주문 취소
```

`/buy`와 `/sell`은 60초 동안 유효한 주문 확인 대기를 만들고, 지정가를 생략하면 시장가 주문으로 준비합니다. 주문 확인 메시지의 `확정`/`취소` 버튼 또는 `/confirm`/`/cancel` 명령으로 처리할 수 있습니다. `/confirm`은 로컬 `mcp-trading`의 `place_order` 도구를 통해 한국투자증권 Open API 현금 주문을 제출합니다. `/cancel`은 Telegram 확인 대기만 취소하며 이미 증권사에 제출된 주문은 취소하지 않습니다. `/balance`, `/quote`, `/trend` 조회 명령도 같은 로컬 `mcp-trading`을 사용합니다. 실계좌 주문은 `KIS_ORDER_ENV=real`과 `KIS_REAL_ORDER_ENABLED=true`가 모두 설정되어야 실행됩니다.

Telegram 명령은 슬래시 명령을 계속 지원하면서 일부 응답에 버튼을 함께 제공합니다. `/help`는 잔고 조회와 알림 상태 버튼을 제공하고, `/alerts` 응답은 `🚨 긴급만`·`📣 전체`·`🔕 끄기`·`🔎 현재 상태` 버튼으로 모드를 바꿀 수 있습니다. `/balance` 결과는 새로고침 버튼을 제공하며, `/quote`와 `/trend` 결과는 같은 종목의 현재가·수급 조회를 오가는 버튼을 제공합니다.

봇 시작 시 Telegram Bot Command Menu도 등록합니다. Telegram 채팅방의 명령 메뉴에서 `/help`, `/balance`, `/alerts`, `/quote`, `/trend`, `/buy`, `/sell`, `/confirm`, `/cancel`을 선택할 수 있으며, 기존 슬래시 명령과 인라인 버튼은 그대로 사용할 수 있습니다.

슬래시 명령 대신 `삼성전자 1주 시장가로 매수해줘`, `NAVER 2주 200,000원에 매도해줘`처럼 입력해도 같은 주문 확인 대기가 생성됩니다. 자연어 주문도 실제 제출 전에는 반드시 `확정` 버튼 또는 `/confirm`이 필요합니다.

`mcp-trading/data/stocks.json`은 KIS 공개 코스피/코스닥 종목 마스터 기반의 종목명 해석 캐시입니다. 신규 상장 등으로 종목명이 잡히지 않으면 6자리 종목코드를 직접 입력하거나 아래 명령으로 캐시를 갱신합니다.

```bash
python3 mcp-trading/scripts/update_stock_master.py
```

슬래시 명령이 아닌 일반 텍스트는 NAT 채팅으로 전달됩니다. Telegram 채팅은 `telegram:{chat_id}` conversation id를 사용하므로 스케줄러 분석 리포트와 대화 이력이 섞이지 않습니다.

---

## 🙌 팀원 목록

남기연, 우용재, 김성현, 김진성, 김현민

## ⚖️ 참고
- 본 프로젝트는 **학술적 목적**의 캡스톤 디자인 결과물이며, 일체의 상업적 목적이 없습니다. 
