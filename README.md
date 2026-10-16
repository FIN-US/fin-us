# 금융 취약계층의 금융 이해력 향상을 돕는 에이전트 자산 관리 파트너

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

2026 한이음 드림업 공모전 제출물

금융 취약계층 및 개인 투자자의 금융 이해력 향상을 돕는 대화형 AI 자산관리 파트너. AI가 뉴스·공시·시세를 24시간 감시·분석하며 유의미한 변화만 선별해 쉬운 말로 알리고, 사용자는 대화로 이해 및 판단해 안전하게 참여한다.

### 개발 배경 및 필요성 

1. **개인 투자자의 구조적 손실과 실시간 대응 한계**: 본업이 있는 개인 투자자는 장중 실시간 대응이 어려워 손실을 반복한다(2024년 개인 국내주식 평균 -16.77%, 전 연령대 손실, NH증권 359만명 분석). 따라서 개인을 대신해 시장을 24시간 감시·분석해 주는 도구의 필요성이 있음.
2. **금융 취약계층의 정보 소외와 진입장벽**: 전문 용어와 복잡한 MTS/HTS 인터페이스가 진입장벽. 전 국민 금융이해력 65.7점으로 2년 전보다 하락, 특히 저소득(59.7)·저학력(59)·20대(62.6) 등 취약층이 낮음(한국은행, 2024). 1,000만 원 미만 소액 투자자의 2025년 수익률은 1.5%로 자산가(37.4%)에 크게 못 미쳐(서울신문, 2026) 지식·정보 부족이 실제 손실로 이어짐.
3. **기존 AI 투자 도구의 기술 장벽과 안전성 한계**: 시중 도구는 기술 지식을 전제해 취약계층의 독립 활용이 어렵고, LLM 환각·오주문·개인정보 유출 위험(무방어 에이전트 공격 성공률 23.6%(VentureBeat, 2025)). 따라서 실자산을 다루는 만큼 안심하고 쓰도록 안전 제어를 갖출 필요성이 있음.

### 프로젝트 특징

1. **타깃 차별화(사회적 가치)**: 저소득·청년 등 금융 지식·정보 약자를 정면 타깃해, 로보어드바이저와 증권사 AI가 고액자산가·적극투자자·수익률 중심인 것과 대비.
2. **이해력 향상(교육) 목적**: 정보 요약에 머무는 경쟁 서비스와 달리 대화로 이해를 돕고 매매일지 복기로 학습을 유도. 금융 용어는 검수된 사전으로만 각주 설명하며(AI 즉석 생성 금지), 사용자 수준(초보/중급)에 따라 조절.
3. **환각·오주문 안전장치**: ①답변의 수치를 도구 호출 기록과 대조해 근거 없는 수치를 차단하는 검증 게이트 ②60초 확인·실계좌 이중 플래그·주문 멱등 ③AI 매매 제안은 검증 에이전트가 재심사하되 거부 방향으로만 개입 가능.
4. **실시간 3단계 필터 자동 감시**: 해시 중복차단→경량 모델 유의성→심층 분석으로 소음·비용을 계단식 최소화. 로보어드바이저의 자산배분과 달리 개별 종목의 뉴스·공시 변화에 실시간 대응.
5. **대화형 접근성 + 3D 시각화**: 게임 엔진(Unity)으로 숫자 나열(MTS/HTS)을 3D 그래픽으로 바꿔 인지 부담을 낮추고, Telegram 대화와 결합해 복잡한 조작·전문 용어 없이 이용.
6. **멀티에이전트 통합(NVIDIA NAT + MCP 자체 구현)**: 뉴스·공시·시세를 하나의 대화 흐름에 통합해, 증권사 단편 기능 대비 통합성 우위.

## 시스템 아키텍처

(이미지 삽입 예정)

## Multi-Agent Ecosystem

역할이 나뉜 전문 에이전트들이 협력하는 구조.

| 에이전트 | 페르소나 | 주요 역할 | 사용 데이터 |
| :--- | :--- | :--- | :--- |
| **News Analyst** | 시장 심리 분석가 | 뉴스·수급·공시 분석·요약 | 뉴스, 시세·수급, 공시, 실적 |
| **Trading Agent** | 계좌·시세 안내자 | 계좌·시세 조회 응답 (주문은 텔레그램 명령으로 안내) | 계좌·시세, 뉴스, 공시 |
| **Strategy Planner** | 전략 수립가 | 뉴스·수급·잔고를 종합해 매매 시나리오 제안 | 계좌·시세, 뉴스, 공시 |
| **Recommend Agent** | 투자 아이디어 뱅크 | 투자 성향에 맞춘 종목 추천 | 계좌·시세, 뉴스 |
| **Monitoring Agent** | 포트폴리오 파수꾼 | 잔고·시장 변화 점검 | 계좌·시세, 뉴스, 공시 |
| **Diary Agent** | 매매 복기 기록가 | 매매 기록과 투자 일지 작성 지원 | 매매 기록, 투자 일지 |
| **Verifier** | 주문 검증자 | AI가 제안한 주문을 재심사해 위험하면 거부 | 제안된 주문 |

## 폴더 구조

```
fin-us/
├── frontend/   3D 대시보드 (Unity WebGL) + nginx 설정
├── backend/    API 서버·스케줄러·텔레그램 봇 (FastAPI)
├── agents/     멀티 에이전트 엔진 (NVIDIA NAT)
├── mcp/        데이터 공급 MCP 서버
│   ├── trading/  한국투자증권: 잔고·시세·수급·주문
│   ├── news/     네이버 뉴스
│   └── dart/     OpenDART 공시·실적
├── scripts/    설치·실행 스크립트
└── docker-compose.yml
```

폴더별 역할과 구성은 각 폴더의 README 참고: [frontend](frontend/README.md) · [backend](backend/README.md) · [agents](agents/README.md) · [mcp](mcp/README.md) · [scripts](scripts/README.md)

## 실행 방법

### 사전 준비 사항

- Docker / Docker Compose
- Node.js & npm
- [uv](https://docs.astral.sh/uv/)
- [KIS Trading MCP](https://github.com/koreainvestment/open-trading-api) 서버 (포트 3300): 에이전트의 계좌·시세 조회용

| API 키 | 용도 |
| :--- | :--- |
| `OPENAI_API_KEY` | AI 분석·멀티 에이전트 (필수) |
| `KIS_API_KEY` / `KIS_API_SECRET` / `KIS_ACCOUNT_NO` | 계좌 조회, 매매 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 뉴스 수집 |
| `DART_API_KEY` | 공시·실적 조회 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 텔레그램 알림·명령 |

> `TELEGRAM_CHAT_ID`는 백엔드를 끈 상태에서 `getUpdates`로 확인(켜져 있으면 빈 결과).

### 설치 및 실행

```bash
bash scripts/setup_env.sh    # .env 생성 (API 키 입력)
bash scripts/setup_deps.sh   # 의존성 설치 + 이미지 빌드
bash scripts/run_stack.sh    # 실행 (첫 실행은 최대 4분)
```

- 3D 대시보드: http://localhost:8080
- 기본은 모의투자. 실계좌는 `.env`에 `KIS_ORDER_ENV=real`, `KIS_REAL_ORDER_ENABLED=true`, 실전 `KIS_URL`(`https://openapi.koreainvestment.com:9443`)과 실계좌 키가 모두 필요.

## 사용 방법

텔레그램 봇 1:1 대화로 사용. 명령이 아닌 메시지는 AI 대화로 처리(자연어 주문 포함). 백엔드 실행 중 아래 작업은 자동 수행.

| 언제 | 무엇을 |
| :--- | :--- |
| 10분마다 | 관심·보유 종목 뉴스·공시 확인, 중요한 변화 시 알림 |
| 매일 08:30 | 실적·배당·공시 등 예정 이벤트 사전 알림 |
| 평일 08:30 | 모닝 브리핑 (시장 요약, 관심종목 동향, 트레이딩 아이디어) |

### 명령어 목록

**조회**

| 명령 | 설명 |
| :--- | :--- |
| `/balance` | 예수금·총자산·보유 종목 조회 |
| `/watch list` / `add <종목명>` / `remove <종목명>` | 관심 종목 관리 |
| `/quote <종목명>` | 현재가 조회 |
| `/trend <종목명>` | 외국인·기관·개인 수급 조회 |
| `/catalysts <종목명>` | 예정 이벤트 조회 |
| `/earnings <종목명> [기간]` | 실적 리포트 (기간 예: `2025Q1`, `2025FY`) |
| `/visualize` | 3D 대시보드 링크 |

**매매**

| 명령 | 설명 |
| :--- | :--- |
| `/advise <종목명>` | AI가 주문을 제안하고 한도·검증을 거쳐 확인 대기 |
| `/buy <종목명> <수량> [지정가]` | 매수 (지정가 생략 시 시장가) |
| `/sell <종목명> <수량> [지정가]` | 매도 (지정가 생략 시 시장가) |
| `/confirm` | 대기 중인 주문 실행 (60초 내, **확정** 버튼과 동일) |
| `/cancel` | 대기 중인 주문 취소 |

**설정·안내**

| 명령 | 설명 |
| :--- | :--- |
| `/alerts urgent\|all\|off\|status` | 알림 범위 설정 (기본 `urgent`) |
| `/level 초보\|중급` | 용어 설명 수준 설정 |
| `/risk 안정형\|공격형\|해제` | 종목 추천 투자 성향 설정 |
| `/help` · `/trade` · `/lookup` | 사용법 안내 |

---

> 투자 조언이 아니며, 실계좌 사용에 따른 책임은 사용자에게 있음.
