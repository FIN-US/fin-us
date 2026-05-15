# Plan: 수치형 선행 시그널 4종을 MCP 도구로 추가

## Context

현재 매수 시그널은 Naver 한국 뉴스(3건/종목) + KIS 가격·투자자 매매동향 위에서만 산출된다. 뉴스는 본질적으로 후행 지표 — 기사가 나올 시점이면 가격은 이미 반영되어 있다. 의사결정 에이전트(`trading_agent`, `strategy_agent`)에게 **선행 포지셔닝 시그널**이 필요하다.

사용자가 선정한 4개 시그널(전부 무료·공식 API):
1. KIS 공매도 종목별 추이
2. KIS 프로그램매매 동향
3. KIS 선물·옵션 시세 + 외국인 선물 포지션
4. DART 공시 (5%룰 + 임원·주요주주 거래)

크롤링·`pykrx`·`data.krx.co.kr` 스크래핑은 사용하지 않는다.

## Scope (고정)

- KIS 3종 → 기존 `mcp-trading/index.js`에 도구 3개 추가 (기존 KIS 인증 재사용)
- DART → 신규 MCP 서버 `mcp-dart/` (별도 인증 + 업스트림)
- NAT 연동: `trading_agent.yml` + `strategy_agent.yml` 의 tools 리스트에만 추가
- 신규 에이전트 만들지 않음, 스케줄러 손대지 않음, `recommend_agent`/프론트엔드 변경 없음
- 매수 시그널 융합 로직은 LLM 프롬프트에 맡긴다 (별도 수치 융합기 도입 없음)

## Architecture

```
trading_agent.yml / strategy_agent.yml
  tools: [...기존, finus_short_sell_trend, finus_program_trading,
          finus_futures_kospi200, finus_dart_major_shareholders,
          finus_dart_executive_trades]
                │
                ├─ mcp-trading (stdio)  ── KIS REST  (기존 KIS_APP_KEY/SECRET)
                │     ├─ get_short_sell_trend       TR_ID FHPST04830000*
                │     ├─ get_program_trading_trend  TR_ID FHPPG04650100*
                │     └─ get_futures_kospi200       TR_ID FHMIF10000000* + FHMIF10020000*
                │
                └─ mcp-dart (stdio, NEW) ── opendart.fss.or.kr  (DART_API_KEY)
                      ├─ get_major_shareholders     /api/majorstock.json
                      └─ get_executive_trades       /api/elestock.json
                      (corpCode.xml: 7일 캐시, mcp-dart/data/CORPCODE.xml)

* TR_ID는 구현 시 docs.koreainvestment.com 샘플 코드로 재확인 필요
```

논리상 4개 시그널 = NAT 도구 5개 (DART가 2개로 분리 — 기존 도구 1:1 패턴 유지). 한 번에 다 호출하면 분석당 KIS 5호출 + DART 2호출 정도, 쿼터 안전 범위.

## File-by-File Changes

### Modify

| 파일 | 변경 |
|---|---|
| `mcp-trading/index.js` | `kisGet()`(L158) 재사용해 도구 3개 추가. `ListTools` + dispatch에 등록. 페이퍼/실전 분기는 L29 `KIS_BALANCE_TR_ID` 패턴 모방. |
| `mcp-trading/package.json` | (변경 없음, 의존성 동일) |
| `finus_nat/src/nat_finus_nat/finus_api.py` | L33–51 허용 env에 `DART_API_KEY` 추가. L168 `_mcp_trading_stock` 옆에 `_mcp_dart_stock` 헬퍼. L184 `FinusInvestorTradingConfig` 패턴으로 5개 컨피그+async generator 등록. |
| `finus_nat/configs/common.yml` | L4–11 패턴으로 5개 도구 매핑 추가. |
| `finus_nat/configs/agents/trading_agent.yml` | L7–11 tools 리스트에 5개 이름 append. |
| `finus_nat/configs/agents/strategy_agent.yml` | tools에 5개 append + L16 `additional_instructions`에 공매도/프로그램매매/선물/공시 활용 지침 한 줄 추가. |
| `backend/config.py` | L36 부근 `_DART_MCP_DIR` 추가, L37–55 allow-list에 `DART_API_KEY`, L88 부근 `DART_MCP_PARAMS = _stdio_server_params(_DART_MCP_DIR)`. |
| `backend/Dockerfile` | L8–11에 mcp-dart 빌드 스테이지 (mcp-news 미러). |
| `finus_nat/Dockerfile` | L14–22에 mcp-dart 복사 + `npm ci`. |
| `docker-compose.yml` | backend env (L33–37)에 `DART_MCP_DIR: /opt/mcp-dart` 추가. 신규 서비스 불필요(stdio). |
| `.env.example` | `DART_API_KEY=` 한 줄 추가. |
| `scripts/install_fin_us_mcp.sh` | `cd mcp-dart && npm ci` append. |

### Create

| 파일 | 내용 |
|---|---|
| `mcp-dart/package.json` | deps: `@modelcontextprotocol/sdk ^1.29.0`, `axios ^1.16.0`, `dotenv`, `adm-zip`, `fast-xml-parser`. |
| `mcp-dart/index.js` | `mcp-news/index.js`의 `loadRootEnv` (L18–39) + stdio + 2개 도구. corp_code 해석: `data/CORPCODE.xml` mtime 체크 → 7일 초과/없음이면 `/api/corpCode.xml` 받아 unzip 후 캐시. 6자리 stock_code 필터로 corp_code 조회. |
| `mcp-dart/Dockerfile` | `mcp-news/Dockerfile` 미러. |
| `mcp-dart/data/.gitkeep` | 캐시 디렉토리 보장. |

### 도구 시그니처

- `get_short_sell_trend({stock_name, days?=5})` → 최근 N일 공매도 체결량·거래대금·비중·잔고
- `get_program_trading_trend({stock_name, days?=5})` → 프로그램 순매수 수량·대금 (차익/비차익 분리)
- `get_futures_kospi200({symbol?})` → KOSPI200 선물 현재가 + 외국인 5일 순포지션 (내부적으로 KIS 2호출)
- `get_major_shareholders({stock_name})` → 최근 5건 5%룰 보고 (보고자, 보유비율, 변동사유, 보고일)
- `get_executive_trades({stock_name})` → 최근 10건 임원 거래 (성명, 직위, 거래일, 매수/매도, 수량, 단가)

## Verification

### 도구 단위 (호스트, Docker 없음)
```bash
# MCP Inspector로 stdio 직접 호출
npx @modelcontextprotocol/inspector node /Users/soroso/Desktop/fin-us/mcp-trading/index.js
# 각 도구를 stock_name="삼성전자"로 호출
#   - get_short_sell_trend: 5일 행, ssts_cntg_qty > 0
#   - get_program_trading_trend: prgm_ntby_qty 비-0
#   - get_futures_kospi200: 분기 만기 심볼 + 외국인 순포지션 숫자

npx @modelcontextprotocol/inspector node /Users/soroso/Desktop/fin-us/mcp-dart/index.js
# 삼성전자(corp_code 00126380)로:
#   - get_major_shareholders: 최근 5%룰 보고 1건 이상
#   - get_executive_trades: 분기당 수 건
```

### 에이전트 단위
```bash
cd /Users/soroso/Desktop/fin-us
uv run --project finus_nat nat run \
  --config_file finus_nat/configs/agents/strategy_agent.yml \
  --input "삼성전자 공매도·프로그램매매·외국인 선물 포지션·임원거래까지 종합해서 전략 의견 줘"
```
`trading_agent.yml:11`의 `verbose: true`로 ReAct 트레이스에 `Action: get_short_sell_trend`, `Action: get_dart_major_shareholders` 등이 stderr에 찍히는지 확인.

### 백엔드 통합 (E2E)
```bash
bash scripts/run_stack.sh
curl -X POST http://localhost:8000/api/v1/analyze?provider=nat \
  -H "Content-Type: application/json" \
  -d '{"stock":"삼성전자"}'
```
응답 본문에 공매도·프로그램매매·선물 관련 문구가 포함되는지 본다.

## Risks & Open Questions

- **TR_ID 미확정**: `FHPST04830000` / `FHPPG04650100` / `FHMIF10000000` / `FHMIF10020000` — docs.koreainvestment.com 샘플 코드와 KIS `kis_devlp` GitHub 저장소(`examples_user/domestic_futureoption_quote/inquire_price.py`)에서 재확인. 틀리면 `kisGet`(L174)이 `rt_cd != "0"` 메시지를 그대로 뱉으니 디버그 가능.
- **KOSPI200 선물 프런트먼스 심볼**: `101W` + 3자리 월코드 포맷이 샘플마다 살짝 다름. `resolveKospi200FrontMonth()` 헬퍼를 mcp-trading에 작게 추가.
- **페이퍼 트레이딩(`openapivts`)에 선물 없음 가능성**: `KIS_URL?.includes("openapivts")` 체크해서 명시적 에러 반환 권장.
- **DART 쿼터(10k/일)**: corpCode.xml 주 1회 = 무시 가능. majorstock+elestock 분석당 2호출 — 안전.
- **KIS 레이트(~20 req/s)**: ReAct 루프가 도구를 폭주 호출할 경우를 대비해 `kisGet` 후 100ms 슬립 추가를 옵션으로 노트.
- **DART 도구 개수**: 명세는 "4개"지만 majorstock과 elestock은 의미가 다르므로 2개로 분리. 사용자가 단일 `get_dart_disclosures({stock_name, type})`로 합치길 원하면 trivial.
- **`strategy_agent.yml`의 `base: recommend_agent.yml` 상속**: tools 정의는 결국 `common.yml`로 체이닝되므로 신규 도구를 `common.yml`에만 등록하면 양쪽 모두 인식. 별도 처리 불필요.

## Out of Scope (이번 PR에서 안 함)

- 신규 `signals_agent` 도입
- 스케줄러 `check_signal_significance`에 포지셔닝 신호 반영
- `recommend_agent` / `monitoring_agent` 도구 확장
- 매크로·기술적 지표·환율·해외 야간선물
- 프론트엔드 변경 (별도 시그널 패널 등)
- KOSPI200 spot 기반 베이시스 계산 (현물 지수 추가 호출 필요)

## Critical Files

- `mcp-trading/index.js` — KIS 3종 도구 핵심 구현
- `mcp-dart/index.js` — 신규 서버 전체
- `finus_nat/src/nat_finus_nat/finus_api.py` — 도구 5개 NAT 등록
- `finus_nat/configs/common.yml` — 도구 이름 매핑
- `backend/config.py` — `DART_MCP_DIR` plumbing
