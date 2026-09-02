# Fin-Us: Multi-Agent AI Investment Orchestrator

Fin-Us는 **MCP (Model Context Protocol)** 아키텍처와 **멀티 에이전트 워크플로우**를 결합한 차세대 지능형 투자 시스템입니다. 독립적인 인격을 가진 에이전트들이 각자의 도구(MCP)를 활용해 협업하며, 뉴스 분석부터 실제 매매 집행까지의 복잡한 의사결정을 자율적으로 수행합니다.

## 목차

- [Multi-Agent Ecosystem](#agents) — 6종 에이전트의 역할
- [주요 특징](#features) — 설계 원칙
- [시스템 아키텍처](#architecture) — 4개 레이어 구성
- [설치 및 시작하기](#install) — 처음 실행까지
  - [사전 준비 사항](#prerequisites)
    - [API 키별 용도](#api-keys)
    - [텔레그램 봇 토큰 발급](#telegram-token)
  - [1. NAT 에이전트 설정](#nat-config)
  - [2. 환경 변수 설정](#env)
  - [3. 시스템 가동](#run)
- [Docker로 한번에 설치하기](#docker) — 권장 실행 경로
  - [주문 멱등 원장 영속화](#order-dedup)
  - [KIS 토큰 캐시와 발급 직렬화](#token-cache)
- [에이전트별 보유 스킬 (MCP Tools)](#skills) — MCP 도구 목록
- [Telegram 봇](#telegram) — 알림과 명령
  - [명령어 목록](#commands)
- [자주 겪는 문제](#troubleshooting) — 증상별 해결
- [알려진 한계](#limitations) — 현재 안 되는 것
- [로드맵](#roadmap) — 완료·예정 항목
- [참고](#notes) — 프로젝트 성격과 면책

<a id="agents" name="agents"></a>

## Multi-Agent Ecosystem

Fin-Us는 단일 모델이 모든 일을 처리하지 않고, 역할이 분리된 전문 에이전트들이 협력합니다. 각 에이전트의 페르소나와 지침은 `finus_nat/configs/agents/`의 YAML 설정을 통해 관리됩니다.

| 에이전트 (Agent) | 페르소나 (Persona) | 주요 역할 및 목표 |
| :--- | :--- | :--- |
| **News Analyst** | 시장 심리 분석가 | 뉴스, 수급, 리서치 리포트를 분석하여 시장 심리 지수(0~100) 산출 |
| **Trading Executor** | 자산 운용 관리자 | 계좌 잔고 및 수익률 기반 리스크 관리 및 매매 관점(BUY/SELL/HOLD) 제안 |
| **Strategy Planner** | 전략 수립가 | 뉴스/수급/잔고 데이터를 종합하여 구체적인 매매 시나리오 및 규칙 제안 |
| **Recommend Agent** | 투자 아이디어 뱅크 | 모멘텀, 촉매, 리스크 관점에서 종목별 투자 권고안 및 아이디어 정리 |
| **Monitoring Agent** | 포트폴리오 파수꾼 | 실시간 잔고 및 시장 변화를 관찰하여 포트폴리오 건전성 유지 및 알림 |
| **Diary Agent** | 매매 복기 기록가 | 매매 기록, 감정 정리, 투자 일지 초안 작성 및 성찰 지원 |

<a id="features" name="features"></a>

## 주요 특징

- **NAT 기반 멀티 에이전트**: `finus_nat` 레이어를 통해 라우터 및 브랜치 구조의 복잡한 에이전트 워크플로우를 제어합니다.
- **YAML 기반 에이전트 설정**: 에이전트의 성격, 배경지식, 작업 지침을 코드가 아닌 YAML 파일로 관리하여 유연한 튜닝이 가능합니다.
- **관심사 분리 (SoC)**: MCP를 통해 도구(Tool)를 분리하고, 에이전트별로 역할을 분산하여 LLM의 할루시네이션(환각)을 최소화했습니다.
- **실시간 지식 확장**: 공식 API 기반 MCP 서버를 통해 뉴스와 정형 금융 데이터에 접근합니다.

<a id="architecture" name="architecture"></a>

## 시스템 아키텍처

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
4.  **Presentation Layer (`frontend/`)**: Unity WebGL + Telegram Bot
    - Unity WebGL 대시보드가 포트폴리오 구성과 분석 결과를 시각화합니다. 빌드 산출물(`frontend/Build/`)은
      Docker Compose의 `frontend`(nginx) 서비스가 정적 서빙합니다.
    - 매매 확인·신호 알림 등 대화형 인터페이스는 Telegram Bot이 담당합니다.

<a id="install" name="install"></a>

## 설치 및 시작하기

<a id="prerequisites" name="prerequisites"></a>

### 사전 준비 사항

- Python 3.13
- Node.js & npm
- Docker / Docker Compose ([Docker로 한번에 설치하기](#docker) 사용 시)
- API Keys: OpenAI/Anthropic API, 한국투자증권(KIS) API Key/Secret, Naver Search API, OpenDART API Key, Telegram Bot Token

<a id="api-keys" name="api-keys"></a>

#### API 키별 용도

전부 있어야 하는 것은 아니고, 쓰려는 기능에 해당하는 것만 발급받으면 됩니다.

| 키 | 무엇에 쓰이나 | 없으면 |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY` | AI 분석 전반 | 분석 기능 전체 불가 |
| `KIS_API_KEY` / `KIS_API_SECRET` / `KIS_ACCOUNT_NO` | 잔고·현재가·수급 조회, 매매 | 계좌 관련 기능 전부 불가 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 뉴스 수집 | 뉴스 기반 감시 불가 |
| `DART_API_KEY` | 공시·실적 조회 | 공시 signal, `/earnings` 불가 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 텔레그램 알림·명령 | 텔레그램 경로 전체 불가 |

<a id="telegram-token" name="telegram-token"></a>

#### 텔레그램 봇 토큰 발급

> 이미 스택을 띄워 둔 상태라면 아래 4번 전에 `docker compose stop backend`로 백엔드를 먼저 멈추세요. 백엔드가 떠 있으면 `getUpdates`가 빈 결과를 돌려줍니다(아래 참고). 값을 채운 뒤 `docker compose start backend`로 되살립니다.

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)를 찾아 `/newbot` 을 보냅니다.
2. 봇 이름과 사용자명(`_bot`으로 끝나야 함)을 정하면 토큰을 줍니다 → `TELEGRAM_BOT_TOKEN`
3. 방금 만든 봇에게 아무 메시지나 한 번 보냅니다.
4. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 를 엽니다.
5. 응답의 `"chat":{"id":...}` 값이 `TELEGRAM_CHAT_ID` 입니다.

> `TELEGRAM_CHAT_ID`가 틀리면 봇이 오류 없이 조용히 명령을 무시합니다. 반응이 없다면 [텔레그램 봇이 반응이 없어요](#troubleshooting)를 참고하세요.

<a id="nat-config" name="nat-config"></a>

### 1. NAT 에이전트 설정

에이전트의 페르소나, 도구 구성, 라우팅 지침은 NAT 레이어에서 정의합니다. `finus_nat/configs/agents/` 폴더 내의 YAML 파일을 수정하여 각 에이전트의 성격과 작업 지침을 관리할 수 있습니다.

에이전트별 역할·지침은 각 YAML의 `additional_instructions`에 둡니다. 반면 모든 에이전트가 공유하는 ReAct 출력 골격(`Thought:` / `Action:` / `Action Input:` 형식 규칙)은 `finus_nat/configs/prompts/*.md` 5개로 분리되어 있고, YAML의 `system_prompt`가 `file://../prompts/<파일>.md`로 이를 참조합니다. `react_kis_full.md`는 trading·monitoring 두 에이전트가 공유하므로 고치면 양쪽에 함께 반영됩니다.

```yaml
# 예시: finus_nat/configs/agents/news_agent.yml
functions:
  news_agent:
    _type: react_agent
    system_prompt: file://../prompts/react_news.md
    tool_names:
      - mcp-news-get-market-news
      - mcp-dart-get-disclosure-signal
    additional_instructions: |
      역할: 시장 심리 분석가
      뉴스와 외국인/기관 매매 동향을 종합 분석합니다.
```

<a id="env" name="env"></a>

### 2. 환경 변수 설정

Fin-Us는 모든 설정을 단일 루트 `.env` 파일에서 관리합니다. 처음 실행하는 경우 초기 설정 CLI로 `.env`를 생성합니다.

```bash
bash scripts/setup_env.sh
```

CLI는 기능 묶음별로 "쓸 건가요?"를 먼저 묻고, 쓴다고 한 것만 값을 입력받습니다.

- 뉴스/공시 데이터 (Naver, OpenDART)
- 계좌 조회와 매매 (KIS)
- Telegram 알림과 시각화
- 로컬 Ollama 모델

기존처럼 `.env.example`을 참고해 `.env`를 직접 편집할 수도 있습니다. 설정이 제대로 됐는지는 아래로 확인합니다.

```bash
bash scripts/check_env.sh
```

<a id="run" name="run"></a>

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

# 3. Frontend(Unity WebGL 번들) 실행 — 화면 확인용 정적 서빙
# file://로 직접 열면 로더가 동작하지 않으므로 반드시 HTTP로 서빙합니다.
python -m http.server 8080 --directory frontend/Build
```

> ⚠️ **이 정적 서빙만으로는 백엔드 연동이 되지 않습니다.** 번들은 백엔드 주소를 모른 채 상대
> 경로(`/api/v1/...`)로만 호출하므로(#246), 위 2번 백엔드가 떠 있어도 `/api` 프록시가 없는
> `http.server`에서는 404가 나고 대시보드에 실데이터 연결 실패 배너와 샘플 데이터가 표시됩니다.
> 화면·레이아웃만 확인할 때 쓰세요.
>
> 백엔드 연동까지 확인하려면 아래 [Docker로 한번에 설치하기](#docker)의 compose 경로를 쓰세요 —
> `frontend`(nginx)가 `/api/`를 같은 네트워크의 `backend`로 중계합니다. compose의 `frontend`만
> 띄우고 백엔드는 호스트에서 돌리는 조합은 nginx가 `backend:8000`을 찾지 못해 502가 납니다.
> 자세한 내용은 [`frontend/README.md`](frontend/README.md)를 참고하세요.

<a id="docker" name="docker"></a>

## Docker로 한번에 설치하기

```bash
bash scripts/setup_env.sh
bash scripts/setup_deps.sh
```

또는:

```bash
bash scripts/run_stack.sh
```

기동되는 서비스와 포트는 다음과 같습니다.

| 서비스 | 포트 | 바인딩 | 역할 |
| :--- | :--- | :--- | :--- |
| `frontend` | 8080 | 전 인터페이스 | Unity WebGL 대시보드 (nginx 정적 서빙 + `/api` 프록시) |
| `backend` | 8000 | `127.0.0.1` | API·스케줄러·텔레그램 봇 |
| `finus-nat` | 8001 | `127.0.0.1` | 멀티 에이전트 엔진 |
| `redis` | 6379 | `127.0.0.1` | 신호 중복 방지 캐시 |

- `frontend`(nginx)는 `/api/`와 `/health`를 `backend:8000`으로 프록시합니다(#245). Unity 번들 소스는 상대 경로(`/api/v1/...`)만 쓰므로(#246) 대시보드와 API가 브라우저에서 항상 같은 오리진(8080)이고, 호스트·스킴·포트가 무엇이든(로컬, Tailscale, 443의 리버스 프록시 뒤) 그대로 동작합니다. 단 root-relative 경로라 서브패스 마운트(`https://example.com/finus/`)까지 따라가지는 않으므로, 그 구성에서는 프록시가 `/api`를 루트에서 함께 중계해야 합니다. **번들도 재빌드되어 더 이상 8000번을 직접 호출하지 않습니다**(#262) — 이로써 가능해진 `CORSMiddleware` 제거와 8000 포트 노출 축소를 #246에서 마무리했습니다(`ALLOW_ORIGINS` 자체는 아래 WebSocket 항목대로 계속 남습니다). nginx는 같은 자리에서 `/api/`에 레이트리밋도 겁니다(#266 1단계, 아래 항목). 자세한 내용은 [`frontend/README.md`](frontend/README.md)를 참고하세요.
- `ALLOW_ORIGINS`는 이름과 달리 이제 **WebSocket(`/api/v1/ws`) 핸드셰이크의 Origin 검사 전용**입니다(#256). HTTP가 same-origin이 되면서 `CORSMiddleware`는 #246에서 제거했고, 그 뒤로 이 목록을 읽는 곳은 WS 검사 하나뿐입니다. `CORSMiddleware`는 애초에 WebSocket 핸드셰이크에 적용되지 않아, 이 검사가 없으면 임의 사이트가 브로드캐스트를 수신할 수 있습니다(Cross-Site WebSocket Hijacking). 따라서 **대시보드를 여는 오리진은 계속 `ALLOW_ORIGINS`에 등록해야 합니다.** 등록하지 않으면 화면은 뜨지만 실시간 알림 연결만 403으로 끊깁니다. Origin 헤더를 보내지 않는 비브라우저 클라이언트(`curl`, `wscat` 등)는 검사 대상이 아닙니다.
- `finus-nat`(8001)과 `redis`(6379)는 **호스트의 루프백에만** 게시됩니다(#285). **인증이 없으면서, 컴포즈 내부 통신만으로 충분한** 두 서비스여서입니다 — `finus-nat`은 `/v1/chat/completions`를 그대로 받고, `redis`에는 `requirepass`가 없는데 대기 주문·폴러 offset·스케줄러 락 같은 금전 경로의 상태가 들어 있습니다. `backend`(8000)도 #246에서 같은 자리로 합류했습니다 — 번들 교체(#246·#262) 전까지는 원격 시연이 8000 직접 호출에 기대고 있어 좁힐 수 없었지만, 그 의존이 사라졌고 열어 두면 아래 레이트리밋을 8000 직접 호출로 그대로 우회할 수 있기 때문입니다. 다른 기기에서 이 세 포트로 직접 붙던 흐름이 있었다면 SSH 포트포워딩(`ssh -L 6379:127.0.0.1:6379 <호스트>`)을 쓰세요. 컴포즈 내부 통신은 `finus-nat:8000`·`redis:6379` 네트워크 별칭을 쓰므로 영향이 없고, 호스트에서 `uvicorn --reload`로 백엔드만 띄우는 흐름은 `.env.example`의 `REDIS_URL=redis://127.0.0.1:6379/0`·`NAT_BASE_URL=http://127.0.0.1:8001`이 받습니다. 기존 `.env`를 그대로 쓰느라 그 두 줄이 없는 환경을 위해 `backend/config.py`의 기본값도 같은 주소를 가리킵니다(#305). 루프백 게시는 IPv4 전용이라 `localhost` 대신 `127.0.0.1`로 적습니다 — `localhost`가 `::1`로 먼저 풀리는 호스트에서 클라이언트의 주소 폴백에 기대지 않기 위해서입니다.
- `/api/`에는 nginx가 레이트리밋을 겁니다(#266 1단계). `/api/v1/analyze`는 **인증이 없으면서** 호출 한 번이 LLM 또는 NAT 멀티 에이전트를 태워 직접 과금으로 이어지므로 6r/m(burst 2) + **동시 2건**으로 따로 낮게 잡고, 그 밖의 `/api/`·`/health`는 2r/s(burst 20)입니다. 정적 자산과 `/nginx-health`는 제한 대상이 아닙니다. 초과 응답은 429이며 본문이 `{"detail": ...}` JSON이라 Unity 배너에 그대로 읽힙니다 — 레이트 초과는 `Retry-After: 10`과 함께, 동시 실행 초과는 그 헤더 없이(언제 풀릴지 서버가 알 수 없습니다) 서로 다른 문구로 나갑니다. **스케줄러의 자동 분석은 여기 걸리지 않습니다** — `perform_stock_analysis`를 같은 프로세스에서 직접 부르고 HTTP를 타지 않습니다. 이 제한은 비용의 뚜껑이지 접근 제어가 아니며, API 인증은 #266 2단계로 남아 있습니다.
- `backend`는 `finus-nat`과 `redis`가 정상(healthy)이 된 뒤에 뜹니다. `finus-nat`은 준비되는 대로 healthy가 되며, 처음 90초 동안은 헬스체크가 실패해도 재시도로 세지 않습니다(`docker-compose.yml`의 `start_period`). 90초가 지난 뒤에도 응답이 없으면 15초 간격으로 10번 더 확인한 뒤 unhealthy로 판정하므로, 최악의 경우 약 4분 뒤에 `backend` 기동이 중단됩니다.
- 로컬에서 `uvicorn --reload`만 쓰고 싶다면 볼륨 마운트된 소스로 호스트에서 실행하면 됩니다.

전부 지우고 처음부터 다시 하려면 아래를 실행합니다. 컨테이너·볼륨·로컬 이미지와 `node_modules`/`venv`/`__pycache__`를 지우며 되돌릴 수 없습니다. 실행 시 확인을 한 번 묻고, `-y`를 붙이면 묻지 않고 진행합니다.

```bash
bash scripts/reset_clean.sh
```

<a id="order-dedup" name="order-dedup"></a>

### 주문 멱등 원장 영속화

`/buy`·`/sell` 확정 주문이 중복 제출되는 것을 막는 마지막 방어선은 `mcp-trading/order-dedup.js`의 파일 기반 원장입니다. 백엔드 측 방어(`backend/telegram_commands.py`의 `pending_orders`)는 프로세스 메모리에만 존재하므로 재시작하면 사라지지만, 이 원장은 파일에 남아 컨테이너가 재생성되어도 최근 주문 이력을 유지합니다.

Docker Compose에서는 `KIS_ORDER_DEDUP_PATH` 기본값에 따라 원장이 호스트의 `./.state/kis-order-dedup.json`에 저장됩니다(`.:/app` 바인드 마운트 덕분). 이 경로는 `.gitignore`·`.dockerignore`에 등록되어 있어 커밋되지도, 이미지에 구워지지도 않습니다.

`.env`나 셸 환경변수로 `KIS_ORDER_DEDUP_PATH`를 직접 지정하면 위 기본값을 덮어씁니다. 이때 지정하는 값은 **호스트 경로가 아니라 컨테이너 내부 경로**입니다. 바인드 마운트된 `/app` 아래를 벗어난 경로를 넣으면 오류 없이 컨테이너 쓰기 계층에 원장이 만들어지고, 재생성과 함께 사라집니다.

> **주의**: `scripts/reset_clean.sh`나 `docker compose down -v`는 이 파일을 지우지 않습니다(바인드 마운트라 호스트에 그대로 남습니다). 다만 사용자가 `./.state/`를 직접 삭제하면 TTL이 만료될 때까지 중복 주문 방어선이 사라집니다. `backend/Dockerfile`에 `USER` 지정이 없어 Linux 호스트에서는 컨테이너가 root로 실행되므로, `./.state/`도 root 소유로 생성됩니다. 이 경우 일반 사용자 권한의 `rm -rf`가 실패할 수 있어 `sudo rm -rf ./.state/`가 필요할 수 있습니다.

기본 TTL은 120초(`DEFAULT_ORDER_DEDUP_TTL_MS`, `mcp-trading/order-dedup.js`)로 사용자의 연타 클릭을 막기 위한 값입니다. 재빌드를 동반한 재배포는 보통 2분을 넘기므로 TTL이 이미 만료된 뒤라, 원장을 영속화해도 재배포 사이의 중복 주문까지는 막지 못합니다. 배포 창 전체를 덮으려면 `KIS_ORDER_DEDUP_TTL_MS`를 상향 조정하세요.

원장 파일에는 주문 요청 body와 KIS 응답이 그대로 저장되며, 여기에는 계좌번호(`CANO`, `buildCashOrderBody`, `mcp-trading/order.js`)가 평문으로 포함됩니다. 생성 시 파일 권한이 `0600`(`#writeLedger`의 `0600` 지정, `mcp-trading/order-dedup.js`)으로 지정되지만 이는 Linux 호스트에서만 의미가 있고, 어느 경우든 호스트 파일시스템에 그대로 남는다는 점을 유의하세요.

> **문제 해결**: `/buy`·`/sell` 실행 시 Telegram에 에러가 그대로 노출되며 모든 주문이 차단된다면, 원인은 둘 중 하나입니다.
>
> 1. **원장 손상** — 원장 경로(`KIS_ORDER_DEDUP_PATH`, 코드 기본값은 `os.tmpdir()`의 `finus-kis-order-dedup.json`, Docker Compose 기본값은 컨테이너 내부 경로 `/app/.state/kis-order-dedup.json`)의 파일이 0바이트·잘린 JSON·형태 불일치 등으로 손상된 경우입니다. `order-dedup.js`의 `#readLedger`는 ENOENT(원장 없음, 신규 설치 정상 상태)만 조용히 통과시키고 그 외 모든 읽기·파싱 실패는 던지므로(#128), **해당 파일을 삭제하면 즉시 재개됩니다.**
> 2. **쓰기 불가** — 그 파일에 쓸 수 없는 경우입니다. `#writeLedger`는 원장과 같은 디렉터리에 임시 파일을 쓰고 `fsync`한 뒤 `rename`으로 경로를 갈아치우는 원자적 쓰기입니다(#128) — `rename`은 대상 **파일**이 아니라 그 파일이 있는 **디렉터리**의 쓰기 권한을 요구하므로, 예전에는 통과하던 "디렉터리는 순회만 되고 쓰기는 안 되지만 그 안의 원장 파일 자체는 쓰기 가능한" 조합도 이제는 모든 주문을 막습니다(Linux에서 실측 확인). 컨테이너가 root로 도는 오늘은 도달하지 않지만 `backend/Dockerfile`에 `USER` 지시자가 추가되는 순간 실전이 될 수 있으니 유의하세요. `rename`이 `EPERM`·`EBUSY`(Windows는 `EACCES`도 포함)로 실패하면 외부 보유자로 인한 일시적 실패로 보고 몇 차례 자동 재시도합니다 — POSIX의 `EACCES`는 대상 디렉터리 권한 문제로 결정론적이라 재시도하지 않고 즉시 차단합니다. **`KIS_ORDER_DEDUP_PATH`를 바인드 마운트된 `/app` 아래의 쓰기 가능한 경로로 지정하면 임시로 우회할 수 있습니다.**
>
> `mcp-trading/index.js`는 `orderDedupStore.reserve(...)`를 KIS 호출용 `try` 블록 바깥에서 실행하므로 위 두 경우 모두 즉시 주문을 차단합니다. 이는 버그가 아니라 방어선이 깨진 상태에서 주문을 조용히 허용하지 않기 위한 fail-closed 설계입니다. `ls -la ./.state/`와 `docker compose logs backend`로 원인을 확인하세요. 이 원장은 단일 writer를 전제로 설계되었습니다 — `docker compose up --scale backend=2`처럼 같은 바인드 마운트 파일에 두 프로세스가 동시에 쓰는 배포는 지원 대상이 아니며, 이 불변식은 코드가 아니라 배포 구성이 지켜야 합니다.

<a id="token-cache"></a>

### KIS 토큰 캐시와 발급 직렬화

KIS OAuth 토큰은 `mcp-trading/token-cache.js`가 파일에 캐시합니다. MCP 호출 하나마다 `mcp-trading` 프로세스가 새로 뜨기 때문에(`backend/services.py`의 `run_mcp_tool`), 이 캐시는 프로세스 안이 아니라 프로세스 **사이**에서 동작해야 합니다.

경로는 `KIS_TOKEN_CACHE_PATH`이고, 미설정 시 `os.tmpdir()`에 `KIS_URL`·API 키 해시별 파일(`finus-kis-token-<해시>.json`)을 만듭니다. 같은 디렉터리에 `<캐시 경로>.lock` 락 파일이 함께 생겼다 사라집니다.

캐시가 비었거나 만료 직후에 여러 프로세스가 동시에 뜨면, 락을 잡은 하나만 `/oauth2/tokenP`를 치고 나머지는 대기하다 캐시가 채워지는 즉시 통과합니다(#324). 락이 없던 예전에는 동시에 뜬 프로세스가 각자 발급을 쳐서 KIS의 발급 유량 제한에 걸렸고, 걸린 쪽은 `Access Token 발급 실패`로 끝났습니다 — `/advise`에서는 그것이 `snapshot_failed` 거부와 함께 해당 종목 재제안 냉각(기본 60분)까지 불렀습니다.

주문 멱등 원장과 달리 이 캐시는 **fail-open**입니다. 캐시 파일이 손상됐거나 락을 만들 수 없는 환경이면 조용히 발급을 한 번 더 할 뿐, 조회나 주문을 막지 않습니다. 최악의 결과가 "발급 한 번 더"뿐이라 막을 이유가 없기 때문입니다. 같은 이유로 캐시 파일은 **언제든 지워도 안전합니다**(다음 호출이 새로 발급합니다).

- 락 보유자가 SIGKILL·OOM으로 죽어 락 파일이 남으면, 9초(`DEFAULT_LOCK_STALE_MS`)가 지난 뒤 기다리던 프로세스가 걷어내고 진행합니다.
- 락을 10초(`DEFAULT_LOCK_WAIT_MS`) 안에 잡지 못하면 락 없이 발급합니다. 락 때문에 토큰 발급 자체가 막히는 쪽이 더 나쁘기 때문입니다. 락 파일을 아예 만들 수 없는 환경(쓰기 불가 경로 등)이면 기다리지 않고 즉시 락 없이 발급합니다.
- 주문(`place_order`)은 대기 상한이 3초(`ORDER_TOKEN_LOCK_WAIT_MS`, `mcp-trading/index.js`)입니다. 주문 경로는 토큰 확보 뒤에도 hashkey·주문 두 번의 요청이 남아 있어, MCP 호출 하나의 상한(30초) 안에 들어오려면 조회 경로만큼 기다릴 수 없습니다.
- 캐시 쓰기는 임시 파일에 쓰고 `rename`으로 갈아치우므로, 다른 프로세스가 잘린 JSON을 읽는 경로가 없습니다.

캐시 파일에는 KIS 액세스 토큰이 평문으로 들어 있습니다. 생성 시 파일 권한을 `0600`으로 지정하지만 이는 Linux 호스트에서만 의미가 있습니다.

<a id="skills" name="skills"></a>

## 에이전트별 보유 스킬 (MCP Tools)

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

<a id="telegram" name="telegram"></a>

## Telegram 봇

일상적인 사용은 대부분 텔레그램 봇에서 이뤄집니다. 봇과의 1:1 대화창에서 `/help`를 보내면 명령 목록과 버튼이 나옵니다.

백엔드가 떠 있으면 아래가 자동으로 실행됩니다.

| 언제 | 무엇을 |
| :--- | :--- |
| **10분마다** | 감시 종목의 뉴스·공시 확인 → 새 신호가 있을 때만 AI 분석 → 조건 충족 시 알림 |
| **매일 08:30** | 촉매 이벤트 캘린더 갱신. D-1/D-0 이벤트 사전 알림 |
| **평일 08:30** | 모닝 브리핑 전송 |
| 60초마다 | 헬스 체크(ping) |

감시 대상은 `/watch`로 등록한 관심 종목과 현재 보유 중인 종목입니다. 같은 뉴스로 반복 분석하지 않도록 이전 신호의 해시를 Redis에 저장(14일 보관)하며, 내용이 바뀌지 않으면 AI 호출 자체를 건너뜁니다.

<a id="telegram-test" name="telegram-test"></a>

### Telegram 긴급 알림 수동 테스트

Docker Compose가 실행 중이고 `.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`가 설정되어 있으면 backend 컨테이너에서 테스트 알림을 보낼 수 있습니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py
```

메시지 포맷만 확인하려면 실제 전송 없이 dry-run으로 실행합니다.

```bash
docker compose exec backend uv run --project /app/backend python /app/backend/scripts/send_test_telegram_alert.py --dry-run
```

<a id="morning-briefing" name="morning-briefing"></a>

### Telegram 모닝 브리핑

Backend 스케줄러는 매 거래일 오전 8시 30분에 Telegram 모닝 브리핑을 자동 전송합니다.

브리핑은 `mcp-news`, `mcp-trading`, NAT Strategy Planner 흐름을 사용해 아래 항목을 요약합니다.

- 오늘의 시장 요약: 전일 미국/선물 시장 동향과 주요 이슈
- 관심종목 동향: `/watch` 관심 종목별 최신 뉴스와 외국인·기관 수급
- 오늘의 트레이딩 아이디어: 장 시작 전 참고할 간략 시나리오
- 주요 촉매 이벤트: 당일/금주 실적, 배당락, 공시 등 확인 필요 이벤트

모닝 브리핑은 정기 브리핑 메시지이므로 긴급 분석 알림 게이트와 분리되어 있습니다. `/alerts urgent|all|off|status`는 스케줄러 분석 알림의 전송 범위를 제어하며, 모닝 브리핑 자체의 발송 스케줄은 APScheduler의 `morning_briefing` 작업으로 관리합니다.

같은 Telegram 봇에서 명령을 사용할 수 있습니다. 기본 알림 모드는 긴급 분석만 전송하는 `urgent`입니다.

<a id="commands" name="commands"></a>

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
이 주소는 Docker Compose의 `frontend`(nginx) 서비스가 서빙하는 8080 포트를 가리킵니다. 로컬에서 확인할 때는 `http://localhost:8080/`입니다.

**매매**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/advise <종목명>` | 주문 제안 → 한도 검사 → 검증 → 60초 확인 대기 생성 | ✅ 확정 · ❌ 취소 |
| `/buy <종목명> <수량> [지정가]` | 60초 매수 확인 대기 생성. 지정가 생략 시 시장가 | ✅ 확정 · ❌ 취소 |
| `/sell <종목명> <수량> [지정가]` | 60초 매도 확인 대기 생성. 지정가 생략 시 시장가 | ✅ 확정 · ❌ 취소 |
| `/confirm` | 대기 중인 주문 실행 | - |
| `/cancel` | 대기 중인 주문 취소 (증권사 제출 주문은 취소 불가) | - |

`/advise`는 종목명만 받아 **주문 내용까지 제안**합니다. 사용자가 정한 주문을 확인만 받는 `/buy`·`/sell`과 다른 점입니다. 결과물은 같은 60초 대기 주문이며, 확정 버튼 없이는 아무것도 체결되지 않습니다.

순서는 ① 종목코드 확정 → ② 재제안 냉각 확인 → ③ 제안 에이전트 호출 → ④ 계좌·시세 조회 → ⑤ **하드 한도를 코드가 판정** → ⑥ 통과분만 검증 에이전트로 → ⑦ 승인이면 대기 주문 생성입니다.

⑤가 이 명령의 핵심입니다. 1회 주문액·종목 비중·일 주문 횟수·일 거래대금·현금 하한·지정가 괴리·보유량 내 매도·주문 금지 종목을 순수 함수가 판정하며, 하나라도 걸리면 검증 에이전트를 호출조차 하지 않습니다. 검증 에이전트는 통과한 제안을 한 번 더 거부할 수만 있고, 코드의 거부를 승인으로 뒤집는 경로는 존재하지 않습니다. 한도 값은 `.env`의 `ORDER_*` 변수로 조정합니다(미설정 시 보수적인 기본값).

판정할 수 없는 상황은 전부 거부입니다 — 응답 파싱 실패, 조회 실패, 타임아웃, 검증 대상 불일치, 잔고 조회가 중간에 끊긴 경우까지. 사용자에게 보이는 수치는 전부 코드가 조회·계산한 값이고, 검증 의견에 섞인 숫자는 `[수치]`로 가려 내보냅니다.

④의 현금은 예수금이 아니라 **주문가능현금**입니다(`get_orderable_cash`, 매수가능조회 `inquire-psbl-order`의 `ord_psbl_cash`). `/balance`가 보여주는 예수금(`inquire-balance`의 `dnca_tot_amt`)은 미수·증거금·미결제 정산이 반영되지 않아 실제로 낼 수 있는 금액과 다를 수 있고, 현금 관련 두 한도(주문금액 초과·현금 하한)가 그 차이만큼 어긋납니다(#310). 두 값이 얼마나 벌어지는지는 계좌 상태에 따라 다르므로, 실계좌에서 `/balance`의 예수금과 `get_orderable_cash`의 주문가능금액을 나란히 비교해 확인하세요.

> 확인 프롬프트에 뜨는 현금은 `/advise`와 `/buy`·`/sell`이 다릅니다. `/advise`는 한도 판정에 쓴 **주문가능금액**을, `/buy`·`/sell`은 잔고 조회의 **예수금**을 보여줍니다. 사용자가 주문 내용을 직접 정하는 `/buy`·`/sell`에는 코드가 판정하는 현금 한도가 없어 조회를 하나 더 태울 이유가 없기 때문입니다.

> 같은 종목은 `ORDER_REPROPOSAL_COOLDOWN_MINUTES`(기본 60분) 동안 다시 제안되지 않습니다. 제안을 받고 `/cancel`로 취소해도 마찬가지입니다 — 냉각이 막는 것은 거부된 제안의 재시도가 아니라 같은 종목에 대한 반복 제안입니다.
>
> 오늘 시장가 주문 이력이 있으면 `/advise`가 그날 막힐 수 있습니다. 시장가 체결가가 기록되지 않아 일 거래대금을 집계할 수 없기 때문이며, 한도를 모른 채 진행하지 않습니다(#309에서 해소 예정).
>
> `/advise`는 NAT 제안 에이전트를 호출하므로 `NAT_BASE_URL`이 실제 NAT 주소를 가리켜야 합니다.

**자동 제안 (기본 꺼짐)**

`.env`에 `ORDER_RULE_TRIGGER_ENABLED=true`를 두면, 감시 주기가 잡은 신호가 조건을 만족할 때 `/advise`를 치지 않아도 같은 제안이 만들어집니다. 조건은 신호 소스(`ORDER_RULE_SOURCES`, 기본 `news,disclosure`)와 긴급도(`ORDER_RULE_URGENCY_LEVELS`, 기본 `critical`)입니다.

**켜도 주문이 자동으로 나가지는 않습니다.** 위 ①~⑦이 그대로 돌고, 결과물도 그대로 60초 확인 대기 주문입니다. 확정 버튼을 누르지 않으면 만료됩니다.

대상은 관심 종목과 보유 종목뿐입니다. 둘 다 비어 감시가 기본 종목으로 떨어진 주기에는 자동 제안이 돌지 않습니다 — 사용자가 고른 적 없는 종목이기 때문입니다. 장 운영 시간 밖에서도 돌지 않고, 한 감시 주기(10분)에 최대 한 건만 제안합니다(대기 주문 자리가 하나뿐이라 두 번째는 어차피 저장되지 않습니다).

결과 통지는 `/alerts` 모드를 따릅니다. `off`면 자동 제안 자체를 돌리지 않고, `urgent`면 승인만 보내고 거부·충돌은 로그로만 남기며, `all`이면 거부·충돌도 보냅니다. **승인은 모드와 무관하게 항상 보냅니다** — 확정 버튼이 필요하고, 알리지 않으면 그 대기 주문 자리 때문에 사용자의 `/buy`가 막히기 때문입니다.

냉각은 `/advise`와 별개로 셉니다(종목코드 + 룰 단위). 수동으로 제안을 받은 종목이 자동 제안의 냉각을 잡아먹지 않고, 그 반대도 마찬가지입니다.

**안내**

| 명령 | 설명 | 인라인 버튼 |
| :--- | :--- | :--- |
| `/help` | 사용 가능한 명령 확인 | 💰 잔고 · 🔔 알림 · 🧾 매매 · 🔎 조회 |
| `/trade` | 매수·매도 입력 안내 | 매수 입력법 · 매도 입력법 |
| `/lookup` | 현재가·수급 조회 입력 안내 | 현재가 입력법 · 수급 입력법 |

> 슬래시 명령 대신 `삼성전자 1주 시장가로 매수해줘`, `NAVER 2주 200,000원에 매도해줘`처럼 자연어로 입력해도 동일한 주문 확인 대기가 생성됩니다. 자연어 주문도 실제 제출 전 `확정` 버튼 또는 `/confirm`이 필요합니다.
>
> 대기 중인 주문이 있으면 새 주문을 낼 수 없습니다. 먼저 확정하거나 취소해야 합니다.
>
> 실계좌 주문은 `KIS_ORDER_ENV=real`과 `KIS_REAL_ORDER_ENABLED=true`가 모두 설정되어야 실행됩니다. 기본값은 모의투자(`demo`)입니다. `KIS_REAL_ORDER_ENABLED`는 **정확히 `true`** 여야 하며 `TRUE`·`1`·`yes`는 인정되지 않습니다.
>
> **업그레이드 시 확인하세요.** 이전 버전은 Docker 배포에서 이 값을 `mcp-trading` 자식 프로세스에 전달하지 않아, `.env`에 `true`를 넣어 두었어도 실계좌 주문이 항상 차단됐습니다(#129). 이제 전달되므로 같은 `.env`로 실제 자금이 움직입니다. Docker를 쓰지 않는 로컬 실행에서는 `mcp-trading`이 `.env`를 직접 읽어 이전에도 적용됐습니다.

`mcp-trading/data/stocks.json`은 KIS 공개 코스피/코스닥 종목 마스터 기반의 종목명 해석 캐시입니다. 신규 상장 등으로 종목명이 잡히지 않으면 6자리 종목코드를 직접 입력하거나 아래 명령으로 캐시를 갱신합니다.

```bash
python3 mcp-trading/scripts/update_stock_master.py
```

슬래시 명령이 아닌 일반 텍스트는 NAT 채팅으로 전달됩니다. Telegram 채팅은 `telegram:{chat_id}` conversation id를 사용하므로 스케줄러 분석 리포트와 대화 이력이 섞이지 않습니다.

<a id="troubleshooting" name="troubleshooting"></a>

## 자주 겪는 문제

**종목명을 못 알아들어요**

`mcp-trading/data/stocks.json` 캐시에 없는 종목입니다. 6자리 종목코드를 직접 입력하거나 `python3 mcp-trading/scripts/update_stock_master.py`로 캐시를 갱신하세요.

**"주문 준비 실패: 종목코드를 확인할 수 없습니다"**

`resolve_stock_code`가 종목을 찾지 못한 경우입니다. 종목명 철자를 확인하거나 종목코드로 입력해 보세요.

**"실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다"**

의도한 것이면 `.env`에 해당 값을 넣고 스택을 재기동하세요. 의도하지 않았다면 실계좌 모드로 잘못 설정된 것입니다.

값을 이미 넣었는데도 이 메시지가 나온다면 철자를 확인하세요. `true` 외의 표기(`TRUE`·`True`·`1`·`yes`·`y`)는 인정되지 않으며, 백엔드와 `mcp-trading` 양쪽이 같은 기준으로 판정합니다.

**텔레그램 봇이 반응이 없어요**

`TELEGRAM_CHAT_ID`가 본인 대화 ID와 일치하는지 확인하세요. 불일치하면 봇이 메시지를 조용히 무시합니다. 아래 순서로 바로잡습니다.

1. `docker compose stop backend`로 백엔드를 멈춥니다. 떠 있는 채로 진행하면 3번의 `getUpdates`가 계속 빈 결과를 돌려줍니다.
2. 봇과의 대화창에 새 메시지를 하나 보냅니다.
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `"chat":{"id":...}` 값을 확인합니다.
4. `.env`의 `TELEGRAM_CHAT_ID`를 그 값으로 고칩니다.
5. `docker compose start backend`로 백엔드를 되살립니다.

이 봇은 `TELEGRAM_CHAT_ID`와 일치하는 대화에서만 명령을 받습니다. 다른 사람이 봇을 찾아 명령해도 무시됩니다.

`TELEGRAM_CHAT_ID`는 형식 검증을 받지 않습니다. 초기 설정 CLI(`backend/scripts/setup_env.py`)의 `URL_KEYS`·`BOOLEAN_KEYS`에도 포함되어 있지 않고, `validate_settings`도 이 값을 들여다보지 않습니다. `.env`를 직접 손으로 고쳐도 마찬가지입니다.

`is_placeholder_secret`(`backend/config.py`)은 값이 비어 있거나 `your_`로 시작하거나 `_here`로 끝날 때만 "설정 안 됨"으로 취급합니다. 즉 값을 아예 채우지 않은 상태는 안전합니다. 하지만 그 외의 문자열이라면 형식이 맞든 안 맞든 봇은 스스로를 "켜졌다"고 판단해(`TelegramNotifier.enabled`) 폴링을 시작하며, 초기 설정 CLI도 "Telegram 알림"이 설정된 것으로 안내합니다.

이 상태에서 실제 채팅 ID와 다른 값이 들어 있으면, 봇은 들어오는 메시지를 조용히 무시하면서도 읽음 오프셋은 그대로 전진시킵니다(`TelegramCommandPoller.run`). 그래서 브라우저로 `getUpdates`를 다시 열어 봐도 이미 읽은 것으로 처리되어 빈 결과만 돌아옵니다.

**분석 알림이 안 와요**

`/alerts status`로 모드를 확인하세요. `off`이거나, `urgent`인데 긴급 조건에 걸리지 않았을 수 있습니다. `/alerts all`로 잠시 바꿔 확인해 보세요.

**알림이 아예 하나도 안 와요**

모니터링 태스크(`backend/scheduler.py`의 `_monitor_market_task`)는 `try` 블록의 첫 문장에서 `get_balance`를 호출합니다. KIS 조회가 실패하면 이 지점에서 예외가 발생해 태스크 전체가 중단되고, 바깥의 `except`는 로그만 남긴 채 넘어가므로 어떤 종목에도 알림이 가지 않습니다. 사용자에게 보이는 신호가 전혀 없는 것은 버그가 아니라 이 구조 때문입니다.

```bash
docker compose logs backend | grep "모니터링 태스크 시작 중 오류"
```

로그에 남는 메시지로 원인을 구분합니다(`backend/services.py`의 `run_mcp_tool`).

| 로그 메시지 패턴 | 원인 | 대응 |
| :--- | :--- | :--- |
| `500: 잔고 조회 중 에러 발생: ...` | KIS API 호출 자체가 실패해 `mcp-trading/index.js`가 `isError`로 응답 | 메시지에 담긴 KIS 에러로 API 키·계좌번호·KIS 서버 상태를 확인 |
| `504: 데이터 공급원(get_balance) 응답 타임아웃 (30초)` | MCP 호출이 30초 안에 끝나지 않음 | KIS 서버 지연이나 네트워크 상태를 확인 후 재시도 |
| `500: 데이터 공급원(get_balance) 연결 실패: ...` | `mcp-trading` MCP 서버 프로세스를 띄울 수 없음 | `mcp-trading`은 별도 컨테이너가 아니라 backend 컨테이너 안에서 stdio로 실행됩니다. `docker compose exec backend ls /opt/mcp-trading/node_modules`로 의존성 설치 여부를, `docker compose logs backend`로 실제 spawn 실패 원인을 확인하세요 |

`/balance`도 동일한 경로(`run_mcp_tool`, `get_balance`)를 사용하므로, `/balance`가 정상 응답한다면 이 문제는 아닙니다.

**엉뚱한 종목 알림이 와요**

`backend/scheduler.py`의 모니터링 태스크는 보유 종목과 `/watch`로 등록한 관심 종목을 합쳐(중복 제거) 감시 대상(`stocks_to_monitor`)을 정합니다. 이 둘을 합친 목록이 완전히 비어 있을 때만(`if not stocks_to_monitor:`) 기본 종목(`삼성전자`, `SK하이닉스`, `현대차`, `NAVER`)으로 대체됩니다. `/balance`로 보유 종목이 있는지, `/watch list`로 관심 종목이 등록되어 있는지 확인하고, 없다면 `/watch add <종목명>`으로 등록하세요.

**컨테이너가 안 떠요**

`backend`는 `finus-nat`이 healthy가 돼야 시작합니다. `finus-nat`은 처음 90초 동안 헬스체크 실패를 눈감아 주고, 그 뒤 15초 간격 10회까지 기다린 다음 unhealthy로 판정합니다. 즉 최대 4분가량 걸릴 수 있으니 그 전에는 기다려도 됩니다.

```bash
docker compose ps                    # STATUS 열에서 health 상태 확인
docker compose logs -f finus-nat     # 실제 실패 원인 확인
```

4분이 지나도 healthy가 되지 않으면 로그가 원인을 말해 줍니다.

<a id="limitations" name="limitations"></a>

## 알려진 한계

- **영숫자·9자리 종목코드는 주문 불가** — 코스닥 스팩·리츠·ETN·펀드 등 전체 종목의 약 18%. 조회는 정상입니다 ([#138](https://github.com/FIN-US/fin-us/issues/138))
- **웹 대시보드 포트폴리오가 항상 비어 있음** — 계좌 데이터를 DB에 동기화하는 기능이 미구현입니다. 실제 잔고는 `/balance`로 확인하세요 ([#122](https://github.com/FIN-US/fin-us/issues/122))
- **API에 인증이 없음** — 외부에 포트를 열지 마세요
- **대기 주문이 메모리에만 존재** — 백엔드를 재시작하면 확인 대기 중이던 주문이 사라집니다 ([#63](https://github.com/FIN-US/fin-us/issues/63))
- **증권사에 제출된 주문은 `/cancel`로 취소되지 않음** — 취소는 확정 전 단계에서만 가능합니다

<a id="roadmap" name="roadmap"></a>

## 로드맵

- [x] 멀티 에이전트 협업 구조 설계 (YAML-based NAT Layer)
- [x] MCP 기반 뉴스 수집 및 트레이딩 도구 통합
- [x] **네이버 증권 리서치 리포트 분석 에이전트 구현**
- [x] LLM(ChatGPT/Claude) 기반 전략 수립 파이프라인 완성
- [x] **포트폴리오 모니터링 및 실시간 알림 에이전트 도입**
- [ ] NVIDIA NeMo Guardrails를 이용한 투자 가이드라인 준수 레이어 추가
- [ ] 기술적 분석(차트) 고도화 및 보조지표 분석 도구 추가
- [ ] AWS App Runner & Docker 기반 클라우드 배포

---

<a id="notes" name="notes"></a>

## 참고

- 본 프로젝트는 **학술적 목적**의 캡스톤 디자인 결과물이며, 일체의 상업적 목적이 없습니다.
- 투자 조언이 아니며, 실계좌 사용의 책임은 사용자에게 있습니다.

---

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688?logo=fastapi&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-WebGL-000000?logo=unity&logoColor=white)
![Node.js](https://img.shields.io/badge/Node.js-24-5FA04E?logo=nodedotjs&logoColor=white)
![nginx](https://img.shields.io/badge/nginx-alpine-009639?logo=nginx&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-1.29-000000)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-API-412991?logo=openai&logoColor=white)
![Anthropic](https://img.shields.io/badge/Anthropic-API-191919)
![OpenDART](https://img.shields.io/badge/OpenDART-API-005BAC)
![KIS](https://img.shields.io/badge/KIS-Open%20API-003B71)
![Naver](https://img.shields.io/badge/Naver-Search%20API-03C75A?logo=naver&logoColor=white)
