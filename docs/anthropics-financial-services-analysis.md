# anthropics/financial-services 분석 및 fin-us 적용 가능 요소

> 원본 레포: https://github.com/anthropics/financial-services  
> 분석일: 2026-05-31

---

## 레포 개요

`anthropics/financial-services`는 금융 서비스 워크플로우(투자은행, 리서치, PE, 자산관리)를 위한 **Claude 에이전트 레퍼런스 구현체**다.  
Claude Cowork 플러그인 또는 Managed Agents API로 배포 가능한 에이전트와 스킬 모음으로 구성되어 있다.

### 핵심 구성

| 구성 요소 | 설명 |
|---|---|
| **Agent Plugins** | Pitch Agent, Market Researcher, Earnings Reviewer, KYC Screener 등 10개 에이전트 |
| **Vertical Plugins** | financial-analysis, equity-research, private-equity, wealth-management 등 7개 버티컬 |
| **Skills** | 버티컬별 도메인 전문 스킬 (마크다운 파일) |
| **Commands** | `/comps`, `/dcf`, `/earnings` 등 슬래시 명령 |
| **MCP Connectors** | Daloopa, Morningstar, FactSet, Moody's 등 12개 외부 데이터 제공자 |
| **Managed Agent Cookbooks** | `agent.yaml` + 서브에이전트 구조의 헤드리스 배포 템플릿 |

---

## fin-us 현황 요약

fin-us는 **한국 주식시장 대상 멀티 에이전트 AI 투자 오케스트레이터**로:

- **UI**: Telegram 봇 (슬래시 명령 + 자연어)
- **에이전트 6종**: News Analyst, Trading Executor, Strategy Planner, Recommend Agent, Monitoring Agent, Diary Agent
- **MCP 3종**: mcp-news (Naver), mcp-trading (KIS API), mcp-dart (OpenDART)
- **YAML 기반 에이전트 설정** (`finus_nat/configs/agents/`)

---

## 적용 가능 요소 분석

### ✅ 즉시 적용 가능 (높은 우선순위)

#### 1. Earnings Reviewer 패턴 → News Analyst 강화

**원본**: 실적 발표 후 콜 녹취 + 재무제표 → 모델 업데이트 → 리서치 노트 초안  
**적용**: 한국 분기 실적 발표 시즌에 News Analyst가 공시(OpenDART) + 뉴스를 조합해 **구조화된 실적 분석 리포트** 자동 생성

- 실적 서프라이즈/미스 판단 로직 추가
- 컨센서스 대비 비교 분석
- Telegram 명령: `/earnings <종목명>` 추가

```
# 적용 방향
finus_nat/configs/agents/news_agent.yml에
earnings_analysis 스킬 추가 및
DART 실적공시 트리거 연동
```

#### 2. Catalyst Calendar 스킬 → Monitoring Agent 강화

**원본**: 커버리지 종목의 upcoming catalyst(실적 발표일, 배당 기준일, AGM 등) 트래킹  
**적용**: 관심종목 watchlist의 **주요 이벤트 캘린더 자동 추적**

- 분기 실적 발표일, 배당락일, 주요 공시 등록 시 사전 알림
- Telegram 명령: `/catalysts <종목명>` 추가

#### 3. Morning Note 패턴 → 일일 브리핑 강화

**원본**: 아침 미팅 노트 + 트레이드 아이디어 요약  
**적용**: 스케줄러가 매일 장 시작 전 **모닝 브리핑 메시지를 Telegram에 자동 전송**

현재 스케줄러(`backend/scheduler.py`)가 주기적으로 신호를 수집하고 있으므로, 아침 브리핑 포맷을 추가하면 된다:

```
📰 오늘의 시장 요약
📊 관심종목 동향
🎯 오늘의 트레이딩 아이디어
⚡ 주요 촉매 이벤트
```

---

### 🔄 중기 적용 가능 (중간 우선순위)

#### 4. Thesis Tracker 스킬 → Recommend Agent + Diary Agent 강화

**원본**: 종목별 투자 thesis 유지·업데이트 (`/thesis`)  
**적용**: Recommend Agent가 종목 추천 시 **투자 근거(thesis)를 DB에 저장**하고, Diary Agent가 사후 복기 시 thesis 대비 실제 결과를 비교

- SQLite에 `investment_thesis` 테이블 추가
- `/thesis <종목명>` 명령으로 현재 투자 thesis 조회

#### 5. Portfolio Monitoring 스킬 → Monitoring Agent 구조화

**원본**: 포트폴리오 KPI 트래킹 및 분산 분석 (`/portfolio`)  
**적용**: Monitoring Agent가 단순 잔고 확인을 넘어 **포트폴리오 건전성 지표를 구조화**해 리포팅

- 섹터별 분산도 (편중 위험 감지)
- 개별 종목 비중 한도 체크
- 수익/손실 기여도 분석

#### 6. Competitive Analysis 스킬 → Recommend Agent 강화

**원본**: 경쟁사 비교 및 시장 포지셔닝 분석 (`/competitive-analysis`)  
**적용**: Recommend Agent가 종목 추천 시 **동종업계 대비 상대 밸류에이션 비교** 추가

- PER/PBR/ROE 업종 평균 대비 비교
- 동종 피어 그룹 자동 매핑 (섹터 정보 활용)

#### 7. Idea Generation / Screening 스킬 → Recommend Agent 강화

**원본**: 종목 스크리닝 및 아이디어 소싱 (`/screen`)  
**적용**: Recommend Agent에 **조건 기반 종목 스크리닝** 기능 추가

- KIS API의 시세 데이터 + DART 재무 데이터를 조합
- "52주 신고가 근접 + 기관 순매수 + PER 업종 하위" 같은 멀티팩터 스크리닝

---

### 🏗️ 아키텍처/인프라 개선 참고

#### 8. Managed Agent Cookbook 패턴 → NAT Layer 구조 개선

**원본**: `agent.yaml` + 서브에이전트 + `steering-examples.json` 구조  
**현재 fin-us**: `finus_nat/configs/agents/` YAML 기반이지만 서브에이전트 위임 구조 없음

적용 방향:
- 오케스트레이터 에이전트가 전문 서브에이전트에 태스크를 **명시적으로 핸드오프**
- `steering-examples.json` 방식으로 라우팅 예시를 문서화해 에이전트 경계를 명확화

```yaml
# 참고: earnings-reviewer/agent.yaml 구조
name: earnings-reviewer
orchestrator:
  model: claude-opus-4-5
  system_prompt: <path>
  callable_agents:
    - model-updater
    - note-drafter
```

#### 9. Skills-as-Markdown 패턴 → 에이전트 프롬프트 관리 개선

**원본**: 스킬을 `.md` 파일로 관리, `sync-agent-skills.py`로 에이전트에 자동 배포  
**적용**: 현재 `additional_instructions`에 인라인으로 작성된 에이전트 지침을 **별도 마크다운 파일로 분리**하면 버전 관리 및 튜닝이 용이

#### 10. MCP 커넥터 중앙화 패턴

**원본**: `financial-analysis` 플러그인에 모든 MCP 커넥터를 집중, 나머지 에이전트는 참조  
**적용**: 현재 각 에이전트가 MCP 도구를 직접 참조하는 구조를 **중앙 MCP 레지스트리 패턴**으로 정리

---

## 적용 제외 항목 (관련성 낮음)

| 항목 | 제외 이유 |
|---|---|
| Pitch Agent, CIM Builder | 투자은행 딜 자료 제작 — B2B 기관 업무 |
| GL Reconciler, Month-End Closer | 펀드 회계 운용 — 해당 없음 |
| KYC Screener | 기관 고객 온보딩 — 개인 투자자 서비스와 무관 |
| LBO / DCF / 3-Statement Model | 기업 밸류에이션 모델링 — 리테일 트레이딩과 거리 있음 |
| Valuation Reviewer, Statement Auditor | PE/LP 보고 특화 — 해당 없음 |
| Microsoft 365 통합 | 기업 내부 어드민 도구 |
| LSEG / S&P Global 파트너 플러그인 | 유료 데이터 구독 필요, 한국 시장 데이터 없음 |

---

## 우선순위 요약

| 우선순위 | 적용 요소 | 대상 에이전트/컴포넌트 | 난이도 |
|---|---|---|---|
| 🔴 높음 | Earnings Reviewer 패턴 | News Analyst + DART MCP | 중 |
| 🔴 높음 | Morning Note 브리핑 | 스케줄러 + Telegram Notifier | 낮음 |
| 🟡 중간 | Catalyst Calendar | Monitoring Agent + watchlist | 중 |
| 🟡 중간 | Thesis Tracker | Recommend Agent + Diary Agent + DB | 중 |
| 🟡 중간 | Portfolio Monitoring 구조화 | Monitoring Agent | 낮음 |
| 🟢 낮음 | Competitive Analysis | Recommend Agent | 중 |
| 🟢 낮음 | Idea Generation/Screening | Recommend Agent + KIS API | 높음 |
| 🟢 낮음 | Managed Agent 아키텍처 패턴 | NAT Layer 전반 | 높음 |

---

## 참고 링크

- [equity-research 스킬 목록](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/equity-research/skills)
- [wealth-management 스킬 목록](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/wealth-management/skills)
- [Earnings Reviewer Cookbook](https://github.com/anthropics/financial-services/tree/main/managed-agent-cookbooks/earnings-reviewer)
- [Market Researcher Cookbook](https://github.com/anthropics/financial-services/tree/main/managed-agent-cookbooks/market-researcher)
- [Private Equity 스킬 목록](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/private-equity/skills)
