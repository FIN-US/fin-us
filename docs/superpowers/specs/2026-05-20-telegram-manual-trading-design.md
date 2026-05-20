# Telegram Manual Trading Design

## Context

Issue #43 adds manual stock order commands to the Telegram bot:

- `/buy <종목명> <수량> <지정가>`
- `/sell <종목명> <수량> <지정가>`
- `/confirm`
- `/cancel`

The current Telegram command surface is read-oriented: `/alerts`, `/balance`, `/quote`, `/trend`, `/help`, plus free-form NAT chat fallback. Existing read paths use the local `mcp-trading` stdio MCP. This feature introduces a write-capable trading path, so order execution must have a clearer safety boundary than the existing read commands.

## Decision

Use the official KIS Trading MCP for order execution only. Keep the local `mcp-trading` server for existing read operations in this issue.

This means:

- `/balance`, `/quote`, `/trend`, backend trading routes, and scheduler balance extraction keep using local `mcp-trading`.
- `/confirm` executes the pending order through an official KIS Trading MCP adapter.
- Telegram command logic stays provider-agnostic and calls a backend order gateway rather than knowing the MCP transport or KIS parameter schema.
- A whole-project migration from local `mcp-trading` to official KIS MCP is out of scope for issue #43.

## Safety Boundary

The feature supports both mock and real KIS account environments.

- Mock trading can execute when the official KIS MCP is configured for mock/demo trading.
- Real-account order execution is blocked unless an explicit opt-in flag is enabled.
- Use `KIS_REAL_ORDER_ENABLED=true` as the opt-in flag because the boundary is KIS order execution, not Telegram delivery.

If the configured order target is real trading and the flag is absent, `/confirm` must refuse the order, send a clear Telegram message, skip the MCP order call, and skip `TradeHistory` persistence.

## Command Flow

1. `/buy` or `/sell` parses `stock_name`, `quantity`, and `price`.
2. Invalid format, non-positive quantity, or non-positive price returns a usage message.
3. Commands outside weekday 09:00-15:30 KST are rejected before a pending order is created.
4. If the chat already has a pending order, the new order command is rejected.
5. The handler fetches current quote and balance through existing read tools for the confirmation prompt.
6. The handler stores one pending order in memory by `chat_id`.
7. Pending orders expire after 60 seconds.
8. `/cancel` removes the pending order and confirms cancellation.
9. `/confirm` validates that a pending order exists and has not expired.
10. `/confirm` calls the backend order gateway.
11. On successful order execution, the backend records `TradeHistory`.
12. On gateway or KIS failure, Telegram receives a short failure message and no trade history is written.

The confirmation prompt should show:

- stock name
- side
- quantity
- limit price
- estimated order amount
- current quote when available
- balance/deposit context when available
- `/confirm` and `/cancel` instructions
- 60-second expiration notice

Balance parsing is best-effort. KIS remains the final source of truth for whether an order is accepted.

## Components

### TelegramCommandHandler

Responsibilities:

- route `/buy`, `/sell`, `/confirm`, and `/cancel`
- parse and validate command arguments
- check market hours
- manage in-memory pending orders
- build confirmation and error messages
- call read tools for prompt context
- call the order gateway only on `/confirm`
- record trade history only after gateway success

The handler should receive injectable dependencies for tests:

- current time provider
- order gateway
- trade recorder or session factory

### Pending Order Store

Use an in-memory dictionary keyed by `chat_id`.

Stored fields:

- chat id
- stock name
- stock code when available
- side: `BUY` or `SELL`
- quantity
- limit price
- created timestamp

This matches the issue scope. Redis-backed pending orders can be a later improvement if process restarts become a practical problem.

### Order Gateway

Add a narrow backend adapter for official KIS Trading MCP order execution.

Responsibilities:

- hide official MCP transport and payload shape from Telegram command code
- select real/demo execution parameters from configuration
- enforce `KIS_REAL_ORDER_ENABLED` before real-account order calls
- return normalized success/failure details for Telegram and DB recording

The gateway should be small and order-specific for this issue. It should not become a full trading abstraction for every KIS endpoint yet.

### Trade History

Reuse existing `TradeHistory`.

Save after successful order execution:

- stock code
- stock name
- trade type: `BUY` or `SELL`
- quantity
- price
- trade date from the model default

If the official MCP returns an order number or execution id, include it in the Telegram success message. Do not extend the database schema in this issue unless implementation shows the existing model cannot support the acceptance criteria.

## Error Handling

- Invalid `/buy` or `/sell`: return usage text.
- Market closed: `주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)`
- Duplicate pending order: tell the user to `/confirm` or `/cancel` the existing order first.
- Expired pending order: remove it and ask the user to enter the order again.
- `/confirm` without pending order: explain that there is no pending order.
- `/cancel` without pending order: explain that there is no pending order to cancel.
- Official MCP/KIS rejection: forward a short readable error message.
- Real account without opt-in: refuse explicitly and do not call the order gateway.
- Telegram send failure: preserve the existing poller behavior where update offsets advance only after handling succeeds.

## Testing

Focused backend tests should cover:

- `/buy` and `/sell` valid parsing
- invalid format
- non-positive quantity and price
- market-hours rejection
- pending order creation
- duplicate pending order rejection
- `/cancel` success
- `/confirm` without pending order
- expiry before confirmation
- successful gateway call on `/confirm`
- trade recorder called only after gateway success
- gateway/KIS failure leaves no trade history
- real-account guard blocks execution without `KIS_REAL_ORDER_ENABLED`
- `/help` includes the new commands

Focused command:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```

Also run syntax/static checks that match the final implementation surface, including `node --check` only if JavaScript MCP code changes are made.

## Non-Goals

- No automatic AI trading.
- No full migration from local `mcp-trading` to official KIS MCP.
- No Redis pending-order persistence.
- No new dashboard trading UI.
- No order correction/cancel endpoint beyond cancelling a pending Telegram confirmation.
- No holiday calendar management; KIS/API rejection handles holidays beyond the weekday/time gate.

## Open Implementation Notes

- Confirm the exact official KIS Trading MCP `api_type` and required params for domestic cash limit orders during implementation with `find_api_detail`.
- Keep official MCP-specific field names inside the gateway.
- Keep Telegram copy concise and Korean-first.
