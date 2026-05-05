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
- **실시간 지식 확장**: Playwright 기반 MCP 서버를 통해 실시간 시장 데이터 및 **증권사 리서치 리포트**에 접근합니다.

## 🏗️ 시스템 아키텍처

시스템은 '관심사 분리' 원칙에 따라 다음과 같이 구성됩니다.

1.  **Orchestration Layer (`backend/`)**: Python (FastAPI)
    - 외부 요청을 수신하고 전체 시스템의 진입점 역할을 수행합니다.
    - 에이전트의 분석 결과를 통합하여 프론트엔드에 제공합니다.
2.  **NAT Layer (`finus_nat/`)**: 멀티 에이전트 워크플로우 엔진
    - 라우터 및 브랜치 구조를 통해 사용자 요청에 최적화된 에이전트를 매칭합니다.
    - 전문화된 6종의 에이전트 협업 프로세스를 제어하며, MCP 서버와의 인터페이스를 담당합니다.
3.  **Tooling Layer (MCP Servers)**:
    - **News Provider (`mcp-news/`)**: 실시간 뉴스 및 **네이버 증권 리서치 리포트** 공급.
    - **Trading Provider (`mcp-trading/`)**: 증권사 API를 통한 정형 금융 데이터 공급 및 명령 집행.
4.  **Presentation Layer (`frontend-react/`)**: React (TypeScript)
    - 실시간 투자 신호 및 분석 리포트를 시각화하여 사용자에게 제공합니다.

## 🛠️ 설치 및 시작하기

### 사전 준비 사항

- Python 3.12+
- Node.js & npm
- API Keys: OpenAI/Anthropic API, 한국투자증권(KIS) API Key/Secret

### 1. 에이전트 페르소나 설정

`backend/agents/` 폴더 내의 YAML 파일을 수정하여 에이전트의 성격을 정의할 수 있습니다.

```yaml
# 예시: news_analyst.yaml
name: "NewsAnalyst"
role: "시장 심리 및 리서치 분석가"
goal: "뉴스의 행간을 읽고 증권사 리포트를 분석하여 투자 신뢰도를 산출함"
```

### 2. 환경 변수 설정

각 프로젝트 폴더의 `.env.example`을 참고하여 `.env` 파일을 생성합니다.

```bash
# backend/.env
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key

# mcp-trading/.env
KIS_API_KEY=your_kis_key
...
```

### 3. 시스템 가동

```bash
# 1. MCP Servers (News & Trading) 빌드
cd mcp-news && npm install
cd mcp-trading && npm install

# 2. Backend Orchestrator 실행
# 프로젝트 루트 디렉토리에서 실행하는 것을 권장합니다.
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8787

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
| **News Analyst**     | `get_market_news`      | 네이버 뉴스 기반 최신 동향 수집              |
|                      | `get_investor_trading` | 기관/외국인 수급 데이터 분석                 |
|                      | `get_research_reports` | **네이버 증권 리서치 종목 분석 리포트 수집** |
| **Trading Executor** | `get_balance`          | 실시간 계좌 잔고 및 수익률 확인              |
|                      | `execute_trade`        | 매수/매도 주문 실행 (KIS API)                |

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
cp backend/.env.example backend/.env
# 키 입력 후:
bash scripts/setup_deps.sh
```

또는:

```bash
bash scripts/run_stack.sh
```

- 로컬에서 `uvicorn --reload`만 쓰고 싶다면 볼륨 마운트된 소스로 호스트에서 실행하면 됩니다.

---

### 🙌 팀원 목록

남기연, 우용재, 김성현, 김진성, 김현민
