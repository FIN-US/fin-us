# Fin-Us: Multi-Agent AI Investment Orchestrator

Fin-Us는 **MCP (Model Context Protocol)** 아키텍처와 **멀티 에이전트 워크플로우**를 결합한 차세대 지능형 투자 시스템입니다. 독립적인 인격을 가진 에이전트들이 각자의 도구(MCP)를 활용해 협업하며, 뉴스 분석부터 실제 매매 집행까지의 복잡한 의사결정을 자율적으로 수행합니다.

> **처음 쓰시나요? → [사용 설명서](docs/user-guide.md)**
> API 키 발급부터 텔레그램 명령, 자주 겪는 문제까지 실제 사용 방법을 다룹니다.
> 이 README는 시스템의 구조와 설치를 설명합니다.

## 목차

| | |
| :--- | :--- |
| [Multi-Agent Ecosystem](#multi-agent-ecosystem) | 6종 에이전트의 역할 |
| [주요 특징](#주요-특징) | 설계 원칙 |
| [시스템 아키텍처](#시스템-아키텍처) | 4개 레이어 구성 |
| [설치 및 시작하기](#설치-및-시작하기) | 환경 변수 · Docker 실행 · 로컬 실행 |
| [에이전트별 보유 스킬](#에이전트별-보유-스킬-mcp-tools) | MCP 도구 목록 |
| [텔레그램 인터페이스](#텔레그램-인터페이스) | 대표 명령과 자동 동작 |
| [로드맵](#로드맵) | 완료·예정 항목 |
| [알려진 한계](#알려진-한계) | 현재 안 되는 것 |

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

## 주요 특징

- **NAT 기반 멀티 에이전트**: `finus_nat` 레이어를 통해 라우터 및 브랜치 구조의 복잡한 에이전트 워크플로우를 제어합니다.
- **YAML 기반 에이전트 설정**: 에이전트의 성격, 배경지식, 작업 지침을 코드가 아닌 YAML 파일로 관리하여 유연한 튜닝이 가능합니다.
- **관심사 분리 (SoC)**: MCP를 통해 도구(Tool)를 분리하고, 에이전트별로 역할을 분산하여 LLM의 할루시네이션(환각)을 최소화했습니다.
- **실시간 지식 확장**: 공식 API 기반 MCP 서버를 통해 뉴스와 정형 금융 데이터에 접근합니다.

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
4.  **Presentation Layer (`frontend-react/`)**: React (TypeScript)
    - 실시간 투자 신호 및 분석 리포트를 시각화하여 사용자에게 제공합니다.

## 설치 및 시작하기

### 사전 준비 사항

- Docker / Docker Compose (권장 실행 경로)
- Python 3.13, Node.js & npm (로컬 실행 시)
- API Keys: OpenAI/Anthropic API, 한국투자증권(KIS) API Key/Secret, Naver Search API, OpenDART API Key, Telegram Bot Token

> 각 키가 어떤 기능에 쓰이고 없으면 무엇이 막히는지, 텔레그램 봇 토큰은 어떻게 발급하는지는 [사용 설명서 2-1](docs/user-guide.md#2-1-준비물)에 있습니다.

### 1. 환경 변수 설정

Fin-Us는 모든 설정을 단일 루트 `.env` 파일에서 관리합니다. 초기 설정 CLI가 기능 묶음별로 사용 여부를 묻고, 쓴다고 한 항목만 입력받습니다.

```bash
bash scripts/setup_env.sh   # .env 생성
bash scripts/check_env.sh   # 설정 점검
```

`.env.example`을 참고해 직접 편집해도 됩니다.

### 2. 실행 (Docker 권장)

```bash
bash scripts/setup_deps.sh   # MCP 의존성 설치 + 이미지 빌드
bash scripts/run_stack.sh    # 전체 스택 기동
```

| 서비스 | 포트 | 역할 |
| :--- | :--- | :--- |
| `frontend` | 5173 | 웹 대시보드 |
| `backend` | 8000 | API·스케줄러·텔레그램 봇 |
| `finus-nat` | 8001 | 멀티 에이전트 엔진 |
| `redis` | 6379 | 신호 중복 방지 캐시 |

`backend`는 `finus-nat`과 `redis`가 healthy가 된 뒤에 기동합니다. `finus-nat`은 최대 90초까지 걸릴 수 있습니다. 전부 지우고 다시 시작하려면 `bash scripts/reset_clean.sh`를 사용합니다.

### 3. 로컬 실행 (선택)

`uvicorn --reload`로 백엔드를 직접 띄우고 싶을 때 사용합니다.

```bash
# 1. MCP Servers (News, Trading, DART) 빌드
(cd mcp-news && npm ci)
(cd mcp-trading && npm ci)
(cd mcp-dart && npm ci)

# 2. Backend Orchestrator 실행 (프로젝트 루트에서)
uv sync --project backend
uv run --project backend uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 3. Frontend 실행
cd frontend-react && npm ci && npm run dev
```

### 4. NAT 에이전트 설정

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

## 텔레그램 인터페이스

일상적인 사용은 대부분 텔레그램 봇에서 이뤄집니다. 웹 대시보드(5173)와 REST API(8000/docs)는 확인·개발용입니다.

백엔드가 떠 있으면 **10분마다** 보유·관심 종목의 뉴스와 공시를 확인하고, 새로운 신호가 있을 때만 AI 분석을 돌려 텔레그램으로 알립니다. 평일 **08:30**에는 모닝 브리핑이 전송됩니다.

| 명령 | 설명 |
| :--- | :--- |
| `/balance` | 예수금·총자산·보유 종목 조회 |
| `/watch add <종목명>` | 관심 종목 등록 (자동 감시 대상에 포함) |
| `/quote <종목명>` · `/trend <종목명>` | 현재가 · 외국인·기관 수급 |
| `/earnings <종목명> [기간]` | DART 실적과 뉴스 기반 구조화 리포트 |
| `/buy <종목명> <수량> [지정가]` | 매수 확인 대기 생성 (60초 내 확정 필요) |
| `/alerts urgent\|all\|off\|status` | 분석 알림 모드 변경·확인 |
| `/help` | 전체 명령 목록 |

슬래시 명령이 아닌 일반 텍스트는 NAT 에이전트에게 전달됩니다. `삼성전자 1주 시장가로 매수해줘`처럼 자연어로 주문해도 동일한 확인 단계를 거칩니다.

실계좌 주문은 `KIS_ORDER_ENV=real`, `KIS_REAL_ORDER_ENABLED=true`, 사용자의 60초 내 확정이 **모두** 충족돼야 실행됩니다. 기본값은 모의투자입니다.

> 전체 명령 목록과 인라인 버튼, 알림 동작, 문제 해결은 [사용 설명서 3장](docs/user-guide.md#3-텔레그램으로-쓰기)과 [7장](docs/user-guide.md#7-자주-겪는-문제)에 있습니다.

## 로드맵

- [x] 멀티 에이전트 협업 구조 설계 (YAML-based NAT Layer)
- [x] MCP 기반 뉴스 수집 및 트레이딩 도구 통합
- [x] **네이버 증권 리서치 리포트 분석 에이전트 구현**
- [x] LLM(ChatGPT/Claude) 기반 전략 수립 파이프라인 완성
- [x] **포트폴리오 모니터링 및 실시간 알림 에이전트 도입**
- [ ] NVIDIA NeMo Guardrails를 이용한 투자 가이드라인 준수 레이어 추가
- [ ] 기술적 분석(차트) 고도화 및 보조지표 분석 도구 추가
- [ ] AWS App Runner & Docker 기반 클라우드 배포

## 알려진 한계

- **영숫자·9자리 종목코드는 주문 불가** — 코스닥 스팩·리츠·ETN·펀드 등 전체의 약 18%. 조회는 정상 ([#138](https://github.com/FIN-US/fin-us/issues/138))
- **웹 대시보드 포트폴리오가 항상 비어 있음** — 계좌 → DB 동기화 미구현 ([#122](https://github.com/FIN-US/fin-us/issues/122))
- **API에 인증이 없음** — 외부에 포트를 열지 마세요
- **대기 주문이 메모리에만 존재** — 백엔드 재시작 시 확인 대기 중이던 주문이 사라짐 ([#63](https://github.com/FIN-US/fin-us/issues/63))

전체 목록은 [사용 설명서 8장](docs/user-guide.md#8-알아둘-한계)에 있습니다.

---

## 참고
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
