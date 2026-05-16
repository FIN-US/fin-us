# Telegram Urgent Agent Alerts Design

## Context

Issue #36 is about Telegram alerts for important NAT agent analysis results. The existing branch started from an older Telegram notification attempt, but current `main` has moved to a newer backend shape: uv-based backend dependencies, Redis-backed scheduler state, multiple `SignalSource` entries, DART disclosure signals, and WebSocket `AGENT_ANALYSIS` broadcasts.

The implementation must align with current `main`. It must not reintroduce old scheduler behavior, old requirements files as dependency source of truth, browser automation, Playwright, Chromium, crawling, or scraping logic.

Telegram is a high-attention channel. It should not mirror every WebSocket event. The first release sends only urgent NAT analysis alerts.

## Goals

- Use one BotFather-created Telegram bot for this service.
- Read Telegram bot token and target chat id from `.env`.
- Send outbound-only Telegram alerts for urgent `AGENT_ANALYSIS` results.
- Extend the existing NAT analysis result contract with urgency metadata.
- Keep the existing WebSocket broadcast behavior unchanged.
- Ensure Telegram send failures do not block scheduler state updates or WebSocket broadcast.
- Keep a clear boundary for later same-bot commands such as `/nat` and `/account`.

## Non-Goals

- Do not implement inbound Telegram polling, webhook handling, or command routing in phase 1.
- Do not implement `/nat`, `/account`, `/help`, or account lookup chat in phase 1.
- Do not create multiple Telegram bots for separate features.
- Do not send `SYSTEM_PING`, scheduler status, skipped signal, cooldown, or raw source update messages to Telegram.
- Do not crawl Telegram channels, web pages, Naver pages, DART pages, or any unofficial scraping surface.
- Do not replace WebSocket broadcasting with Telegram.

## Current Branch Cleanup

The inherited branch contains an older Telegram implementation. It should be treated as prior art only.

The final implementation should preserve only the useful idea of a small outbound notifier. It should replace stale integration points with current-main-compatible wiring:

- Dependencies must live in `backend/pyproject.toml` and `backend/uv.lock`.
- `backend/requirements.txt` and `backend/requirements.lock` must not be reintroduced as backend dependency source of truth.
- Scheduler integration must stay inside the current signal-source flow.
- DART and news sources must continue to run independently.
- Redis hash, lock, cooldown, and fallback behavior must remain intact.

## Data Contract

Extend `AnalysisReport` with urgency metadata:

```python
urgency: Literal["low", "normal", "high", "critical"] = "normal"
urgency_reason: str | None = None
telegram_alert: bool = False
```

Existing NAT responses that do not include these fields must still parse successfully. Missing fields default to `normal`, `None`, and `False`.

The WebSocket `AGENT_ANALYSIS` payload keeps the same outer shape:

```json
{
  "type": "AGENT_ANALYSIS",
  "stock": "삼성전자",
  "source": "news 또는 disclosure",
  "data": {
    "summary": "한 줄 요약",
    "details": {
      "decision": "BUY | SELL | HOLD",
      "confidence_score": 0.82,
      "reason": "근거",
      "target_stock": "삼성전자"
    },
    "source_news": ["헤드라인1"],
    "source_signals": ["분석에 사용한 외부 signal"],
    "trading_trend": "수급 한줄 요약 또는 null",
    "urgency": "critical",
    "urgency_reason": "긴급 판단 사유",
    "telegram_alert": true
  },
  "reason": "significant_change_detected"
}
```

## Urgency Rules

The NAT prompt should ask for urgency metadata in the final JSON. The default is conservative:

- `low`: background or minor information.
- `normal`: meaningful enough for WebSocket analysis, but not urgent enough for Telegram.
- `high`: material event that should be checked soon.
- `critical`: emergency-level event that may require immediate user attention.

`telegram_alert` should be `true` only when urgency is `high` or `critical` and the analysis identifies a concrete material event. Examples include:

- Trading halt or delisting risk.
- Major disclosure affecting ownership, control, financing, or governance.
- Earnings shock or material guidance change.
- Lawsuit, regulatory sanction, investigation, or severe reputation risk.
- Sudden portfolio risk that could materially affect a held or monitored stock.

The backend must still enforce the final gate. NAT output alone is not enough.

## Telegram Gate

Send Telegram only when all conditions are true:

```text
event type == AGENT_ANALYSIS
data.telegram_alert == true
data.urgency in {"high", "critical"}
Telegram config is enabled
```

The scheduler already decides when a signal is significant enough for NAT analysis. Telegram adds a second, stricter gate after the analysis result is available.

The WebSocket broadcast should still happen for all existing `AGENT_ANALYSIS` events even when Telegram is disabled, skipped, or fails.

## Telegram Service

Add a small backend notification component, for example `backend/telegram_notifier.py`.

Responsibilities:

- Read token and target chat id from `backend.config`.
- Expose an `enabled` flag.
- Format urgent analysis messages.
- Send messages through Telegram Bot API.
- Catch and log send failures.
- Return a boolean success/failure result instead of raising into scheduler flow.

The first implementation should use direct Bot API HTTP calls through existing `httpx`, not `python-telegram-bot`, because phase 1 does not need polling, webhook, command handlers, or a long-running bot application.

## Message Format

Send a short human-readable message, not raw JSON:

```text
[긴급] 삼성전자 / disclosure
Decision: HOLD (0.82)
Reason: 주요주주 지분 변동과 단기 변동성 확대 가능성
Urgency: critical - 대량보유 변동 공시가 감지됨
Summary: ...
```

Formatting should avoid Markdown features that can break on user-generated stock names or model text. Plain text is acceptable for phase 1.

## Scheduler Integration

The current scheduler path remains the source of truth:

```text
source signal fetch
-> Redis duplicate/cooldown/lock handling
-> significance check
-> NAT analysis
-> Redis signal state update
-> optional Telegram urgent alert
-> WebSocket AGENT_ANALYSIS broadcast
```

Telegram failure must not prevent Redis state update, lock release, cooldown handling, or WebSocket broadcast.

The implementation should place Telegram after `perform_stock_analysis` returns and after signal state is stored. WebSocket broadcast should not depend on Telegram success.

## Environment

Add to `.env.example`:

```text
# Telegram Bot
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

If either value is missing or left as a placeholder, Telegram is disabled and backend startup remains normal.

## Future Same-Bot Commands

Phase 1 should leave room for one bot to handle future inbound commands, but must not implement them yet.

Future same-bot features:

- `/nat <message>`: user sends a direct question to NAT.
- `/account`: user retrieves account summary such as holdings and cash.
- `/help`: user sees supported commands.

Future command handling should reuse the same token, authorization policy, and config. It may introduce a separate command router later. The phase 1 notifier should stay small enough to be reused or wrapped without becoming a polling service now.

## Error Handling

- Missing Telegram config disables alerts and logs a startup/config-level message.
- Telegram HTTP failures are logged and return `False`.
- Message formatting failures are logged and return `False`.
- Scheduler continues after Telegram errors.
- Backward-compatible NAT parsing remains in `analysis_from_nat_text`.

## Verification

Required checks:

- `uv run --project backend pytest backend/tests/test_services.py -q`
- `uv run --project backend pytest backend/tests/test_scheduler.py -q`
- New notifier tests for disabled config, urgent gate, skipped normal urgency, and send failure.
- `uv lock --check --project backend`
- `rg -n "playwright|chromium|puppeteer|cheerio|querySelector|document\\.|innerHTML|scrap|crawl|크롤|selenium|BeautifulSoup|bs4" backend mcp-news mcp-trading mcp-dart finus_nat`

The final search must not find crawling or browser automation logic in service paths. Documentation-only historical notes may mention removed crawling, but runtime code must not.
