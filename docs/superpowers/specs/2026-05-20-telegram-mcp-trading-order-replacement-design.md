# Telegram mcp-trading 주문 전환 설계

## 배경

Issue #43의 현재 브랜치는 Telegram `/buy`, `/sell`, `/confirm`, `/cancel` 흐름을 구현하면서 주문 실행을 공식 KIS Trading MCP에 맡기는 방향으로 작성되어 있다. 이 방식은 한국투자증권 공식 MCP 저장소를 별도로 클론하고 Docker 컨테이너를 프로젝트 밖에서 실행해야 하므로, 현재 `fin-us`의 로컬 MCP 구성과 운영 경로가 갈라진다.

기존 `mcp-trading`은 이미 한국투자증권 Open API 인증, 토큰 캐시, 잔고 조회, 현재가 조회, 투자자 매매동향 조회를 담당한다. 따라서 주문 실행도 같은 `mcp-trading` 서버로 흡수하면 Telegram 명령, backend API, scheduler가 모두 동일한 거래 MCP 경계를 사용한다.

## 결정

공식 KIS Trading MCP 주문 어댑터를 새 커밋으로 대체한다. 기존 커밋을 rebase/reset으로 되돌리지 않고, replacement commit으로 다음 변경을 추가한다.

- Telegram 명령 UX와 pending-order 안전장치는 유지한다.
- 주문 실행 backend provider만 공식 KIS MCP remote adapter에서 로컬 `mcp-trading` stdio tool 호출로 바꾼다.
- `mcp-trading`에 `place_order` tool을 추가하고, 이 tool이 한국투자증권 Open API `order-cash`를 직접 호출한다.
- 실계좌 주문은 backend의 `KIS_REAL_ORDER_ENABLED=true` guard를 계속 통과해야 한다.

## 목표

- 프로젝트 외부 공식 KIS MCP Docker 실행 요구 제거
- `/balance`, `/quote`, `/trend`, `/buy`, `/sell`, `/confirm`이 모두 `TRADING_MCP_PARAMS` 기반 로컬 `mcp-trading` 경로 사용
- 모의투자와 실계좌 주문을 모두 지원하되, 실계좌 주문은 명시적 enable 없이는 차단
- 주문 성공 시 기존 `TradeHistory` 기록 유지
- 주문 실패 또는 상태 확인이 필요한 애매한 실패는 pending order를 제거해 중복 주문 위험을 줄이는 기존 UX 유지

## 비목표

- 증권사에 이미 접수된 주문의 정정/취소 구현
- 해외주식, 신용주문, 선물옵션 주문 구현
- 공식 KIS MCP 저장소 클론 또는 Docker 실행 자동화
- `mcp-trading/data/stocks.json` 전체 종목 마스터 확장
- 기존 공식 KIS MCP 커밋 히스토리 삭제

## 아키텍처

현재 주문 실행 흐름:

```text
Telegram /confirm
  -> TelegramCommandHandler
  -> OfficialKisMcpOrderGateway
  -> official KIS Trading MCP over SSE/streamable HTTP
  -> KIS Open API
```

대체 후 주문 실행 흐름:

```text
Telegram /confirm
  -> TelegramCommandHandler
  -> McpTradingOrderGateway
  -> run_mcp_tool(TRADING_MCP_PARAMS, "place_order", args)
  -> mcp-trading/index.js
  -> KIS Open API /uapi/domestic-stock/v1/trading/order-cash
```

조회 흐름은 변경하지 않는다.

```text
/balance -> run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
/quote   -> run_mcp_tool(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": ...})
/trend   -> run_mcp_tool(TRADING_MCP_PARAMS, "get_investor_trading", {"stock_name": ...})
```

## 구성 요소

### `mcp-trading`

`mcp-trading`은 `place_order` tool을 추가로 노출한다.

입력:

- `stock_name`: 사용자 입력 종목명 또는 6자리 종목코드
- `stock_code`: backend가 이미 resolve한 6자리 종목코드
- `side`: `BUY` 또는 `SELL`
- `quantity`: 양의 정수
- `price`: 양의 정수
- `order_env`: `demo` 또는 `real`

동작:

- `KIS_API_KEY`, `KIS_API_SECRET`, `KIS_ACCOUNT_NO`, `KIS_URL`을 사용한다.
- `KIS_ACCOUNT_NO`는 앞 8자리를 `CANO`, 뒤 2자리를 `ACNT_PRDT_CD`로 나눈다.
- 국내주식 현금 주문 endpoint `/uapi/domestic-stock/v1/trading/order-cash`를 POST로 호출한다.
- 지정가 주문이므로 `ORD_DVSN`은 `"00"`으로 보낸다.
- 한국투자증권 공식 샘플 기준 TR ID는 다음과 같이 선택한다.
  - 실전 매수: `TTTC0012U`
  - 실전 매도: `TTTC0011U`
  - 모의 매수: `VTTC0012U`
  - 모의 매도: `VTTC0011U`
- 거래소 구분은 현재 Telegram 수동 주문 범위에서 `SOR`로 보낸다.

### Backend

`backend/trading_orders.py`에서 공식 MCP transport 관련 코드를 제거하고, `McpTradingOrderGateway`를 둔다.

`McpTradingOrderGateway`는 다음 책임만 가진다.

- `KIS_ORDER_ENV=real`이고 `KIS_REAL_ORDER_ENABLED`가 false면 403으로 차단
- `run_mcp_tool(TRADING_MCP_PARAMS, "place_order", args)` 호출
- MCP text 응답을 `OrderExecutionResult`로 변환

`TelegramCommandHandler`의 parsing, market-hours guard, pending-order TTL, `/confirm`, `/cancel`, 실패 시 pending clear 정책은 유지한다.

## 환경 변수

유지:

- `KIS_API_KEY`
- `KIS_API_SECRET`
- `KIS_ACCOUNT_NO`
- `KIS_URL`
- `KIS_ORDER_ENV=demo|real`
- `KIS_REAL_ORDER_ENABLED=true|false`
- `TRADING_MCP_DIR`

제거:

- `FINUS_KIS_TRADING_MCP_URL`
- `FINUS_KIS_TRADING_MCP_TRANSPORT`
- `FINUS_KIS_TRADING_TOOL_NAME`

`mcp-trading`은 `order_env`와 `KIS_URL`이 맞지 않으면 실패한다.

- `order_env=demo`는 `KIS_URL`에 `openapivts`가 포함되어야 한다.
- `order_env=real`은 `KIS_URL`에 `openapivts`가 포함되면 안 된다.

이 검사는 모의투자 명령이 실전 URL로 나가거나 실계좌 명령이 모의 URL로 나가는 설정 사고를 막기 위한 fail-closed 정책이다.

## 오류 처리

- KIS API가 `rt_cd !== "0"`을 반환하면 `mcp-trading`은 `isError: true` MCP 응답을 반환한다.
- backend `run_mcp_tool`은 MCP `isError`를 HTTPException으로 변환하는 기존 계약을 사용한다.
- Telegram `/confirm`은 주문 실패 또는 상태 확인 필요 메시지를 보내고 pending order를 제거한다.
- 실계좌 enable guard 실패는 broker에 주문이 나가지 않은 확정 실패이므로 pending order를 유지해 사용자가 설정 후 다시 `/confirm`할 수 있게 한다.

## 테스트 전략

- Node `node:test`로 `mcp-trading` 주문 helper를 검증한다.
- backend pytest로 `McpTradingOrderGateway`가 `TRADING_MCP_PARAMS`와 `place_order`를 호출하는지 검증한다.
- Telegram command tests는 기존 pending-order UX를 유지하면서 gateway factory가 로컬 `mcp-trading` gateway를 만드는지 검증한다.
- 기존 focused Telegram 테스트와 전체 backend 테스트를 모두 실행한다.

## 구현 순서

1. `mcp-trading` 주문 payload helper와 Node tests 추가
2. `mcp-trading` `place_order` tool 추가
3. backend 공식 KIS MCP gateway를 `McpTradingOrderGateway`로 대체
4. 공식 KIS MCP env/docs 제거 및 README 정리
5. focused tests, full backend tests, Node checks, `git diff --check` 실행

## 참고

- 한국투자증권 Open API 공식 GitHub 샘플: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py
- 한국투자증권 API 문서의 국내주식 주문/계좌 카테고리: https://apiportal.koreainvestment.com/apiservice-apiservice%3F/uapi/domestic-stock/v1/trading/order-cash
