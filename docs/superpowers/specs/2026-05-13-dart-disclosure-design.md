# DART Disclosure MCP Integration Design

## Context

The current backend scheduler periodically monitors market news through MCP sources and triggers NAT analysis when a source signal changes. The scheduler already supports multiple `SignalSource` entries, Redis-backed per-source hashes, locks, cooldowns, and fallback memory state. This design adds DART disclosure data as a first-class source while preserving the existing scheduler flow.

The implementation must not introduce crawling. DART data access must use official OpenDART APIs only.

## Goals

- Add a separate `mcp-dart` server for DART disclosure data.
- Use OpenDART official APIs only: `corpCode.xml`, `list.json`, `majorstock.json`, and `elestock.json`.
- Resolve stock name or stock code to DART `corp_code` through the official corp-code file.
- Periodically collect disclosure signals through the existing backend scheduler.
- Trigger NAT analysis when disclosure signal content changes.
- Expose a backend route for manual disclosure checks.
- Register a NAT function so selected agents can directly query disclosure signals.

## Non-Goals

- Do not implement KIS short-selling, program trading, futures/options price, or foreign futures position in this spec.
- Do not add order execution or trading-decision automation based on disclosure data.
- Do not scrape DART pages, Naver pages, HTML pages, or unofficial endpoints.
- Do not redesign scheduler state, Redis keys, or NAT routing architecture beyond the minimal additions needed for this source.

## Architecture

`mcp-dart` is the only DART data-access boundary. It reads `DART_API_KEY` from the root `.env`, downloads and caches the official OpenDART corp-code file, and formats disclosure data into a text signal that backend and NAT can both consume.

Backend adds `DART_MCP_PARAMS` and includes `DART_API_KEY` in the existing MCP child-process environment allowlist. The scheduler adds one source:

```python
SignalSource(
    name="disclosure",
    mcp_params=DART_MCP_PARAMS,
    tool_name="get_disclosure_signal",
)
```

The monitored universe stays the same as the current scheduler: account holdings from `mcp-trading.get_balance`, or `DEFAULT_MONITOR_STOCKS` when there are no holdings.

NAT registers `finus_disclosure_signal` as a function that calls the same `mcp-dart` tool. The initial agent tool lists should include it for `news_agent`, `strategy_agent`, and `monitoring_agent`. Trading execution agents remain out of scope for this first integration.

## MCP Tool

`mcp-dart` exposes one required tool:

```text
get_disclosure_signal(stock_name: string) -> text
```

Input accepts a Korean stock name or six-digit stock code. The tool resolves input to `corp_code`, then fetches and formats:

- Latest disclosure list entries for shareholding-related filings from `list.json`, limited to DART detail types `D001`, `D002`, and `D005`.
- 5% rule / major shareholding data from `majorstock.json`.
- Executive and major shareholder ownership/transaction data from `elestock.json`.

The output is a stable, human-readable text signal:

```text
[삼성전자] DART 지분공시 signal
- 종목코드: 005930
- 고유번호: 00126380
- 조회기간: YYYYMMDD~YYYYMMDD

[최신 공시]
- 2026-05-13 | 대량보유상황보고서 | 접수번호 ... | 보고자 ...

[5% 룰 대량보유 요약]
- 보고자: ...
- 보유비율: ...%
- 변동비율: ...%
- 보유목적: ...

[임원 주요주주 거래 요약]
- 보고자: ...
- 직위/관계: ...
- 소유주식수: ...
- 증감: ...
```

The text signal combines filing-list context and structured ownership summaries. Backend Redis hashes this entire signal, so any new relevant filing or structured disclosure change can trigger analysis.

## Official API Usage

Allowed endpoints:

- `https://opendart.fss.or.kr/api/corpCode.xml`
- `https://opendart.fss.or.kr/api/list.json`
- `https://opendart.fss.or.kr/api/majorstock.json`
- `https://opendart.fss.or.kr/api/elestock.json`

`corpCode.xml` returns a ZIP file containing `CORPCODE.xml`. `mcp-dart` parses this official file and stores a JSON cache at `mcp-dart/data/corp-codes.json`.

Cache shape:

```json
{
  "fetched_at": "2026-05-13T00:00:00.000Z",
  "source": "https://opendart.fss.or.kr/api/corpCode.xml",
  "items": [
    {
      "corp_code": "00126380",
      "corp_name": "삼성전자",
      "stock_code": "005930",
      "modify_date": "20240530"
    }
  ]
}
```

The cache TTL is 24 hours. If the cache exists and is fresh, `mcp-dart` reuses it. If the cache is missing or stale, it downloads the official corp-code ZIP again. Only entries with non-empty `stock_code` are required for this first implementation.

## Backend API

Add:

```text
GET /api/v1/disclosures?stock=삼성전자
```

The route calls:

```python
run_mcp_tool(DART_MCP_PARAMS, "get_disclosure_signal", {"stock_name": stock})
```

Response shape follows existing `CommonResponse` style:

```json
{
  "status": "success",
  "message": null,
  "data": {
    "stock": "삼성전자",
    "disclosure": "...text signal..."
  }
}
```

## Scheduler Behavior

The existing scheduler source loop remains the orchestration point. With `disclosure` added to `SIGNAL_SOURCES`, each monitored stock is checked for news and disclosure independently.

Expected behavior:

- Disclosure fetch failure affects only `disclosure:<stock>`.
- Redis hash comparison remains per source and stock.
- Cooldown remains per source and stock.
- News source continues even if disclosure source fails.
- NAT analysis receives `trigger_source="disclosure"` and `trigger_signal=<DART signal>`.
- WebSocket broadcast includes `"source": "disclosure"` for disclosure-triggered analysis.

## NAT Integration

Add a NAT function config type:

```text
finus_disclosure_signal
```

It should call `mcp-dart` through the same vendor-root helper pattern used for `mcp-news` and `mcp-trading`.

The function is added to `finus_nat/configs/common.yml`, then included in:

- `news_agent`
- `strategy_agent`
- `monitoring_agent`

Agent instructions should state that disclosure data is official OpenDART data and that 5% rule plus executive/major shareholder ownership changes should be considered as investment signals, not as direct trading instructions.

## Environment And Runtime

Add `DART_API_KEY` to `.env.example`.

Add `DART_MCP_DIR`, defaulting to `<repo>/mcp-dart`, to backend config.

Docker and install scripts must install `mcp-dart` dependencies alongside `mcp-news` and `mcp-trading`:

- `backend/Dockerfile`
- `finus_nat/Dockerfile`
- `scripts/install_fin_us_mcp.sh`
- `scripts/setup_deps.sh` when it enumerates MCP packages directly; no change is needed if it delegates to `scripts/install_fin_us_mcp.sh`

The runtime must not include Playwright, browser automation, HTML scraping, or DOM parsing for DART.

## Error Handling

`mcp-dart` should return MCP errors for:

- Missing or placeholder `DART_API_KEY`.
- Failed official API response.
- Invalid ZIP or XML from `corpCode.xml`.
- No matching `corp_code` for the stock input.
- Ambiguous stock-name match.

Backend keeps using existing `run_mcp_tool` and scheduler exception handling. Scheduler failures set cooldown for the failed source and stock where applicable.

## Verification

Required checks:

- `node --check mcp-dart/index.js`
- `npm ci --omit=dev` in `mcp-dart`
- Backend config tests for `DART_MCP_PARAMS` and `DART_API_KEY` allowlist.
- Backend route test for `/api/v1/disclosures`.
- Scheduler test proving `news` and `disclosure` sources are processed independently.
- NAT unit tests for `finus_disclosure_signal` helper routing to `mcp-dart`.
- Dockerfile/script inspection or build-oriented test confirming `mcp-dart` dependencies are installed.
- `backend/venv/bin/python -m pytest backend/tests` when the local backend venv is available.

## Implementation Order

1. Scaffold `mcp-dart` using the existing Node MCP style from `mcp-news` and `mcp-trading`.
2. Implement official OpenDART client, corp-code cache, and `get_disclosure_signal`.
3. Add backend config, env allowlist, route, and scheduler source.
4. Add NAT helper function and selected agent tool config.
5. Update Dockerfiles, dependency scripts, `.env.example`, and README snippets.
6. Add focused tests and run the verification commands.
