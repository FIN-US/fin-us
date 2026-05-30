# Telegram Interactive Commands Design

## Goal

Issue #41 adds an interactive Telegram surface on top of the existing Telegram alert bot.

The first implementation stays inside the current Telegram command boundary:

- Keep `/alerts urgent|all|off|status` behavior unchanged.
- Add deterministic MCP commands for balance, quote, and investor trend lookup.
- Add free-form NAT chat fallback for regular Telegram text.
- Keep Telegram chat separate from scheduler analysis reports and dashboard WebSocket broadcasts.

This work is implemented as one feature branch and one PR, but with atomic commits:

1. MCP command support and help text.
2. NAT chat fallback with explicit Telegram conversation id.

## Current Context

PR #38 added NAT `conversation-id` support. Backend NAT calls can now pass a `conversation_id` through:

```python
llm_chat("nat", text, conversation_id="...")
```

NAT stores short-term transcript state by `conversation-id`. Telegram chat must use this boundary explicitly and must not fall back to the shared `NAT_CONVERSATION_ID` default.

The current Telegram command implementation only supports `/alerts`. The poller already advances `offset` only after `handle_update()` returns successfully, which must be preserved.

## Architecture

`backend/telegram_commands.py` remains the Telegram interaction boundary.

`TelegramCommandPoller` keeps its current responsibility:

- Poll Telegram `getUpdates`.
- Pass each update to `TelegramCommandHandler`.
- Advance `offset` only after handler success.

`TelegramCommandHandler.handle_update()` becomes the command router. The routing order is:

1. Ignore messages from any chat other than configured `TELEGRAM_CHAT_ID`.
2. `/alerts ...` -> existing alert mode handling.
3. `/help` -> command list.
4. `/balance` -> MCP balance lookup.
5. `/quote <stock>` -> MCP quote lookup.
6. `/trend <stock>` -> MCP investor trend lookup.
7. Unknown slash command -> help text.
8. Any other non-empty text -> NAT chat fallback.

The implementation keeps the current flat module style. New behavior is split into private handler helpers such as `_handle_balance`, `_handle_quote`, `_handle_trend`, and `_handle_chat_fallback`.

To keep tests focused, `TelegramCommandHandler` may accept optional injected runners:

- `mcp_runner`, defaulting to `run_mcp_tool`.
- `llm_runner`, defaulting to `llm_chat`.

No new command abstraction or separate router module is needed for this scope.

## MCP Commands

MCP commands are deterministic lookups and do not use NAT.

Tool mapping:

- `/balance` -> `run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})`
- `/quote <stock>` -> `run_mcp_tool(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock})`
- `/trend <stock>` -> `run_mcp_tool(TRADING_MCP_PARAMS, "get_investor_trading", {"stock_name": stock})`

The backend sends MCP output to Telegram without additional parsing or summarization because `mcp-trading` already returns user-facing Korean text.

Missing arguments return short usage text:

- `사용법: /quote <종목명>`
- `사용법: /trend <종목명>`

## NAT Chat Fallback

Regular Telegram text is sent to NAT through the backend LLM boundary:

```python
llm_chat("nat", text, conversation_id=f"telegram:{chat_id}")
```

This preserves PR #38 compatibility:

- Telegram chat gets NAT SQLite transcript continuity by chat id.
- Telegram users do not share the process-wide `NAT_CONVERSATION_ID` default.
- Telegram chat does not reuse scheduler/API analysis threads such as `api:{stock}:{date}` or `{source}:{stock}:{date}`.
- Telegram chat does not call `perform_stock_analysis()`.
- Telegram chat does not create `AgentReport` rows.

NAT chat responses are sent only back to the Telegram chat. They must not trigger scheduler Telegram alert delivery, and they must not broadcast through `/api/v1/ws`.

## Telegram UX

The initial UX stays small:

- Send Telegram `typing` chat action before slow MCP/NAT work.
- Send the final result with the existing text sending path.
- Truncate outgoing messages at a Telegram-safe length around 4000 characters and append `...(이하 생략)`.

Issue #41 mentions placeholder messages and `editMessageText`. That is intentionally deferred. The current notifier only exposes text sending, and `typing` plus final response gives a smaller first implementation with fewer Telegram API surfaces.

`/help` lists the supported commands:

- `/alerts urgent|all|off|status`
- `/balance`
- `/quote <종목명>`
- `/trend <종목명>`

Free-form NAT chat can be described briefly in the help response.

## Error Handling

The handler converts expected work failures into short Telegram replies and then returns normally:

- Unknown slash command -> help text.
- Missing command argument -> command-specific usage text.
- MCP failure -> `조회 실패: <짧은 사유>`.
- NAT failure -> `응답 생성 실패: <짧은 사유>`.

When a failure was reported to the user, the update is considered handled and the poller may advance `offset`.

Failures that prevent command handling itself from completing should still propagate to the poller. Examples include `/alerts` Redis state failures or Telegram send failures. In those cases, the poller must not advance `offset`.

## Boundaries

In scope:

- Telegram command routing.
- MCP deterministic lookup commands.
- NAT text fallback with explicit `conversation_id`.
- Focused Telegram command tests.
- README update for the new Telegram commands if implementation changes user-facing operation.

Out of scope:

- Frontend GUI NAT chat changes.
- WebSocket chat.
- Persistent MCP stdio sessions.
- Multi-user authorization beyond the existing `TELEGRAM_CHAT_ID` gate.
- Placeholder message editing.
- Saving Telegram chat results as `AgentReport`.

## Testing

Focused tests should cover:

- Existing `/alerts` behavior.
- Poller `offset` advance only after handler success.
- `/help` response.
- Unknown slash command response.
- Missing `/quote` and `/trend` arguments.
- `/balance`, `/quote`, and `/trend` MCP tool names and arguments.
- Regular text routed to `llm_chat("nat", ..., conversation_id="telegram:{chat_id}")`.
- NAT chat fallback does not call `perform_stock_analysis()`.
- MCP and NAT work failures produce short user-facing failure messages.

After Telegram changes, run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```
