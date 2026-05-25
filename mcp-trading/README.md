# mcp-trading

Fin-Us **Trading Provider** MCP 서버입니다. 한국투자증권(KIS) Open API를 호출해 **계좌·시세·수급** 같은 정형 데이터를 에이전트/백엔드에 제공합니다.

> **Kis Trading MCP와 구분하세요.**  
> `finus_nat`의 `trading_agent` 등이 쓰는 **`kis-trading-mcp-tool`**(원격 SSE, 보통 `:3300/sse`)은 [open-trading-api의 Kis Trading MCP](https://github.com/koreainvestment/open-trading-api)로, TR 단위(`api_type` + `params`) 범용 호출을 지원합니다.  
> **이 디렉터리(`mcp-trading`)** 는 Fin-Us 전용 **경량 stdio MCP**입니다.

## 역할 in Fin-Us

| 소비자 | 연결 방식 | 용도 |
|--------|-----------|------|
| `backend/` | stdio (`node index.js`) | `/api/v1/trading/balance`, 스케줄러 보유종목, Telegram 명령 |
| `finus_nat/` Docker 이미지 | 소스만 번들 (vendor) | 주 에이전트는 Kis Trading MCP 사용; 이 서버를 직접 래핑하는 NAT 도구는 없음 |

## 제공 도구 (Tools)

| 도구 | 설명 | KIS TR / API |
|------|------|----------------|
| `get_balance` | 계좌 잔고·보유종목 요약 | `inquire-balance` (`TTTC8434R` / 모의 `VTTC8434R`, v1_국내주식-006) |
| `get_stock_holdings` | **보유 종목 상세** (수량·평가손익·주문가능, 연속조회) | `inquire-balance` (`TTTC8434R` / 모의 `VTTC8434R`, v1_국내주식-006) |
| `get_stock_quote` | 국내 주식 현재가 시세 | `inquire-price` (`FHKST01010100`) |
| `get_investor_trading` | 개인/외국인/기관 순매수 (최근 약 5일) | `inquire-investor` (`FHKST01010900`) |
| `get_today_daily_orders` | **당일 주문·체결 전체** (연속조회 paginate) | `inquire-daily-ccld` (`TTTC0081R` / 모의 `VTTC0081R`, v1_국내주식-005) |
| `get_balance_rlz_pl` | **잔고 + 실현손익·평가손익** (체결기준, 연속조회) | `inquire-balance-rlz-pl` (`TTTC8494R`, v1_국내주식-041, **모의 미지원**) |
| `resolve_stock_code` | 종목명 → 6자리 코드 변환 | 로컬 `data/stocks.json` (API 미호출) |

모든 도구는 **사람이 읽기 쉬운 한국어 텍스트**를 반환합니다(JSON이 아님).

## 환경 변수

`fin-us/.env`(프로젝트 루트)에서 로드합니다. Inspector 등 cwd가 달라도 `index.js` 기준으로 `../.env`를 찾고, 없으면 상위 디렉터리·`FINUS_ENV_PATH`·`FINUS_ROOT` 등을 순서대로 시도합니다.

| 변수 | 필수 | 설명 |
|------|------|------|
| `KIS_URL` | 예 | KIS REST base URL (예: 모의 `https://openapivts.koreainvestment.com:29443`) |
| `KIS_API_KEY` | 예 | App Key |
| `KIS_API_SECRET` | 예 | App Secret |
| `KIS_ACCOUNT_NO` | `get_balance`, `get_stock_holdings` 등 | 10자리 (계좌 8자리 + 상품코드 2자리) |
| `KIS_TOKEN_CACHE_PATH` | 아니오 | OAuth 토큰 파일 캐시 경로 (기본: OS temp) |
| `KIS_TR_ID_DAILY_CCLD` | 아니오 | 일별주문체결 TR override (기본: 실전 `TTTC0081R`, 모의 `VTTC0081R`) |
| `KIS_TR_ID_BALANCE_RLZ_PL` | 아니오 | 잔고실현손익 TR override (기본: `TTTC8494R`) |
| `FINUS_ENV_PATH` | 아니오 | `.env` 파일 절대/상대 경로 (Inspector 등에서 루트 `.env` 위치를 직접 지정) |
| `FINUS_ROOT` / `FINUS_VENDOR_ROOT` | 아니오 | Fin-Us 루트 디렉터리 — `{root}/.env` 를 추가 후보로 탐색 |

### `get_today_daily_orders` 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `trade_date` | 당일(KST) | `YYYYMMDD` |
| `stock_name` | (전체) | 특정 종목만 필터 |
| `ccld_dvsn` | `00` | `00` 전체 / `01` 체결 / `02` 미체결 |
| `sll_buy_dvsn` | `00` | `00` 전체 / `01` 매도 / `02` 매수 |

실전 계좌는 API 1회 최대 100건, 모의는 15건까지 반환하므로 서버가 `tr_cont`·`CTX_AREA_*` 연속조회로 **당일 전체**를 모읍니다.

### `get_stock_holdings` 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `stock_name` | (전체) | 특정 종목만 필터 (종목명 또는 6자리 코드) |

실전 계좌는 API 1회 최대 50건까지 반환하므로 서버가 연속조회로 **전체 보유 종목**을 모읍니다.

## 로컬 실행

```bash
cd fin-us
cp .env.example .env   # KIS 키 설정

cd mcp-trading
npm ci
node index.js
```

정상 기동 시 **stderr**에 `Trading MCP Server is running...`만 출력됩니다. stdout은 JSON-RPC 전용이라 `console.log`는 stderr로 리다이렉트됩니다.

## 종목 코드 매핑

`data/stocks.json`에 등록된 종목명·별칭만 `resolve_stock_code` / 시세·수급 도구에서 인식합니다.  
6자리 코드를 직접 넣으면 매핑 없이 호출할 수 있습니다. 종목 추가는 [to_be_fixed.md](../to_be_fixed.md) 참고.

---

## MCP Inspector로 연결할 수 없는 이유

많은 경우 **URL(SSE) 연결**을 시도해서 실패합니다. 이 서버는 그 방식을 **지원하지 않습니다**.

### 1. stdio 전용 — HTTP/SSE 서버 없음

`index.js`는 `StdioServerTransport`만 사용합니다.

```367:368:fin-us/mcp-trading/index.js
const transport = new StdioServerTransport();
await server.connect(transport);
```

MCP Inspector의 **Transport: SSE** / `http://localhost:3300/sse` 같은 URL 연결은 **다른 서버(Kis Trading MCP)** 용입니다. `mcp-trading`은 포트를 열지 않습니다.

### 2. `:3300`은 Kis Trading MCP (별도 프로젝트)

`finus_nat` 설정의 `FINUS_KIS_TRADING_MCP_URL=http://localhost:3300/sse`는 **upstream Kis Trading MCP**를 가리킵니다.  
Fin-Us `mcp-trading/`과 이름만 비슷할 뿐, 바이너리·도구 스키마·TR 목록이 다릅니다.

### 3. Docker만 띄우면 Inspector URL로 붙을 수 없음

`Dockerfile`의 `CMD ["node", "index.js"]`도 stdio 프로세스입니다. 컨테이너에 **HTTP 포트가 없어** Inspector가 URL로 접속할 수 없습니다.  
백엔드는 컨테이너 **안에서** `node` 자식 프로세스로 stdio 세션을 엽니다.

### 4. Inspector에서 stdio로 붙이려면

**먼저 의존성 설치** (필수):

```bash
cd fin-us/mcp-trading
npm ci
```

`node_modules`가 없으면 Node가 `@modelcontextprotocol/sdk` 등에서 `ERR_MODULE_NOT_FOUND`를 stderr로 출력하고, Inspector proxy는 이를 **`Command not found, transports removed`** 로만 보여줍니다. (`node` 실행 파일이 없다는 뜻이 아닙니다.)

**Transport: stdio (Command)** 를 선택하고, Command / Args를 **분리**합니다 (`/path/to/` placeholder 그대로 쓰지 마세요).

| 항목 | 값 |
|------|-----|
| Command | `/opt/homebrew/bin/node` (또는 `which node` 결과) |
| Args | `/Users/you/nemo-agent-toolkit/fin-us/mcp-trading/index.js` |
| CWD | `fin-us/mcp-trading` |
| Env | `KIS_URL`, `KIS_API_KEY`, `KIS_API_SECRET`, `KIS_ACCOUNT_NO` |

Inspector CLI (의존성 설치 후):

```bash
cd /path/to/fin-us/mcp-trading
npx @modelcontextprotocol/inspector node "$(pwd)/index.js"
```

또는 config 파일:

```json
{
  "mcpServers": {
    "mcp-trading": {
      "command": "node",
      "args": ["/absolute/path/to/fin-us/mcp-trading/index.js"],
      "env": {
        "KIS_URL": "https://openapivts.koreainvestment.com:29443",
        "KIS_API_KEY": "...",
        "KIS_API_SECRET": "...",
        "KIS_ACCOUNT_NO": "1234567801"
      }
    }
  }
}
```

```bash
npx @modelcontextprotocol/inspector --config ./mcp-trading-inspector.json --server mcp-trading
```

연결 전 로컬에서 프로세스가 뜨는지 확인:

```bash
cd fin-us/mcp-trading && node index.js
# stderr: Trading MCP Server is running...  → OK
# ERR_MODULE_NOT_FOUND → npm ci 필요
```

KIS 자격 증명은 Inspector 실행 환경(또는 UI Env)에 **반드시** 넣어야 `get_balance` 등이 동작합니다.

---

## 관련 문서

- [Fin-Us README](../README.md) — 전체 아키텍처
- [architecture.md](../architecture.md) — backend ↔ mcp-trading 호출
- [finus_nat README](../finus_nat/README.md) — Kis Trading MCP 포트(3300) 및 NAT 에이전트
