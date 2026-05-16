# Telegram Urgent Agent Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #36 by sending Telegram only for urgent NAT `AGENT_ANALYSIS` results while preserving current-main scheduler, Redis, DART, uv, and WebSocket behavior.

**Architecture:** Backend keeps the current scheduler as the orchestration point. NAT analysis returns urgency metadata through `AnalysisReport`; scheduler applies a strict Telegram gate and calls a small outbound-only notifier before broadcasting the same WebSocket event. Telegram config is optional and failures are isolated from scheduler state and WebSocket delivery.

**Tech Stack:** FastAPI backend, Pydantic v2, APScheduler, Redis scheduler state, httpx, Telegram Bot API, uv, pytest.

---

## File Structure

- Modify `.env.example`: add Telegram token and chat id.
- Modify `backend/pyproject.toml`: keep dependency source of truth in uv metadata; no new runtime dependency is required if using existing `httpx`.
- Modify `backend/uv.lock`: refresh only if `uv lock` changes metadata.
- Delete `backend/requirements.txt`: stale file reintroduced by the inherited branch.
- Delete `backend/requirements.lock`: stale file reintroduced by the inherited branch.
- Modify `backend/config.py`: add Telegram config values and placeholder detection helper.
- Modify `backend/schemas.py`: add urgency fields to `AnalysisReport`.
- Modify `backend/services.py`: update NAT JSON prompt and keep fallback parsing backward compatible.
- Create `backend/telegram_notifier.py`: outbound-only Telegram formatter, gate, and HTTP sender.
- Modify `backend/scheduler.py`: call notifier only for urgent analysis without changing WebSocket broadcast.
- Modify `backend/main.py`: remove old bot lifecycle imports and calls from the inherited branch.
- Delete `backend/telegram_bot.py`: replace old `python-telegram-bot` style implementation with outbound-only notifier.
- Add `backend/tests/test_telegram_notifier.py`: unit tests for gate, config, formatting, send success, and send failure.
- Modify `backend/tests/test_services.py`: verify urgency defaults and prompt fields.
- Modify `backend/tests/test_scheduler.py`: verify urgent Telegram send, normal skip, send failure isolation, and WebSocket preservation.

## Task 1: Remove Inherited Stale Telegram Wiring

**Files:**
- Delete: `backend/requirements.txt`
- Delete: `backend/requirements.lock`
- Delete: `backend/telegram_bot.py`
- Modify: `backend/main.py`

- [ ] **Step 1: Remove stale dependency files from the branch**

Run:

```bash
git rm backend/requirements.txt backend/requirements.lock
```

Expected: both files are staged for deletion. The backend dependency source remains `backend/pyproject.toml` and `backend/uv.lock`.

- [ ] **Step 2: Remove old bot lifecycle import from `backend/main.py`**

Replace:

```python
from .telegram_bot import start_bot, stop_bot, notifier
```

with no import. There should be no Telegram import in `backend/main.py` after this task.

- [ ] **Step 3: Remove lifecycle calls from `lifespan`**

Change this block:

```python
    init_db()
    start_scheduler()
    await start_bot()
    logger.info("Database initialized and scheduler started.")
    yield
    # 앱 종료 시 실행: 스케줄러 안전 종료
    stop_scheduler()
    await stop_bot()
    logger.info("Scheduler stopped.")
```

to:

```python
    init_db()
    start_scheduler()
    logger.info("Database initialized and scheduler started.")
    yield
    # 앱 종료 시 실행: 스케줄러 안전 종료
    stop_scheduler()
    logger.info("Scheduler stopped.")
```

- [ ] **Step 4: Delete old bot module**

Run:

```bash
git rm backend/telegram_bot.py
```

Expected: old `python-telegram-bot` based file is staged for deletion.

- [ ] **Step 5: Verify old dependency source is gone**

Run:

```bash
git status --short
```

Expected includes:

```text
D  backend/requirements.lock
D  backend/requirements.txt
D  backend/telegram_bot.py
M  backend/main.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/main.py
git commit -m "chore: 텔레그램 초안 의존성 정리" -m "- 구 requirements 파일 제거
- 구 bot lifecycle 제거"
```

## Task 2: Add Telegram Configuration

**Files:**
- Modify: `.env.example`
- Modify: `backend/config.py`

- [ ] **Step 1: Add environment example**

Ensure `.env.example` contains this block after Redis:

```text
# Telegram Bot
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

- [ ] **Step 2: Add config values and helper**

In `backend/config.py`, after `REDIS_URL`, add:

```python
# Telegram urgent alert settings.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def is_placeholder_secret(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    return not normalized or normalized.startswith("your_") or normalized.endswith("_here")
```

- [ ] **Step 3: Verify no MCP child env leak**

Run:

```bash
rg -n "TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|_MCP_ENV_ALLOWED_KEYS" backend/config.py
```

Expected: Telegram values exist, but they are not added to `_MCP_ENV_ALLOWED_KEYS`.

- [ ] **Step 4: Commit**

```bash
git add .env.example backend/config.py
git commit -m "feat: 텔레그램 알림 설정 추가" -m "- bot token 및 chat id 환경 변수 추가
- placeholder 비활성화 판별 추가"
```

## Task 3: Extend Analysis Report Urgency Contract

**Files:**
- Modify: `backend/schemas.py`
- Modify: `backend/services.py`
- Modify: `backend/tests/test_services.py`

- [ ] **Step 1: Add failing schema compatibility tests**

Add to `backend/tests/test_services.py`:

```python
def test_analysis_from_nat_text_defaults_telegram_urgency_fields():
    data = services.analysis_from_nat_text(
        (
            '{"summary":"요약",'
            '"details":{"decision":"HOLD","confidence_score":0.5,'
            '"reason":"근거","target_stock":"삼성전자"},'
            '"source_news":["기존 뉴스"],'
            '"source_signals":["기존 signal"],'
            '"trading_trend":null}'
        ),
        "삼성전자",
    )

    assert data["urgency"] == "normal"
    assert data["urgency_reason"] is None
    assert data["telegram_alert"] is False


def test_analysis_from_nat_text_parses_telegram_urgency_fields():
    data = services.analysis_from_nat_text(
        (
            '{"summary":"긴급 요약",'
            '"details":{"decision":"SELL","confidence_score":0.8,'
            '"reason":"규제 리스크","target_stock":"삼성전자"},'
            '"source_news":["뉴스"],'
            '"source_signals":["signal"],'
            '"trading_trend":null,'
            '"urgency":"critical",'
            '"urgency_reason":"거래정지 위험",'
            '"telegram_alert":true}'
        ),
        "삼성전자",
    )

    assert data["urgency"] == "critical"
    assert data["urgency_reason"] == "거래정지 위험"
    assert data["telegram_alert"] is True
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --project backend pytest backend/tests/test_services.py::test_analysis_from_nat_text_defaults_telegram_urgency_fields backend/tests/test_services.py::test_analysis_from_nat_text_parses_telegram_urgency_fields -q
```

Expected: first test fails because urgency fields do not exist yet.

- [ ] **Step 3: Update schema**

In `backend/schemas.py`, change imports:

```python
from typing import Any, Literal
```

Then change `AnalysisReport` to:

```python
class AnalysisReport(BaseModel):
    summary: str
    details: TradingSignal
    source_news: list[str]
    source_signals: list[str] | None = None
    trading_trend: str | None = None
    urgency: Literal["low", "normal", "high", "critical"] = "normal"
    urgency_reason: str | None = None
    telegram_alert: bool = False
```

- [ ] **Step 4: Update NAT JSON prompt**

In `backend/services.py`, extend the JSON example inside `perform_stock_analysis` so the final fields are:

```python
        '"trading_trend":"수급 한줄 요약 또는 null",'
        '"urgency":"low"|"normal"|"high"|"critical",'
        '"urgency_reason":"긴급 판단 사유 한 줄 또는 null",'
        '"telegram_alert":true|false}'
```

Immediately before the JSON instruction, add this Korean policy sentence to the prompt:

```python
        "Telegram 알림은 매우 긴급한 경우에만 사용한다. "
        "거래정지·상장폐지 위험, 대규모 공시, 실적 쇼크, 소송·규제 리스크, "
        "보유종목에 대한 급격한 위험 변화처럼 즉시 확인이 필요한 경우에만 "
        'urgency를 "high" 또는 "critical"로 두고 telegram_alert를 true로 둔다. '
        "그 외에는 urgency를 normal 이하로 두고 telegram_alert를 false로 둔다. "
```

- [ ] **Step 5: Run services tests**

Run:

```bash
uv run --project backend pytest backend/tests/test_services.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add backend/schemas.py backend/services.py backend/tests/test_services.py
git commit -m "feat: 분석 리포트 긴급도 필드 추가" -m "- urgency 및 telegram_alert 계약 추가
- NAT 분석 프롬프트 긴급 알림 기준 추가
- 기존 응답 호환성 테스트 추가"
```

## Task 4: Add Outbound Telegram Notifier

**Files:**
- Create: `backend/telegram_notifier.py`
- Add: `backend/tests/test_telegram_notifier.py`

- [ ] **Step 1: Write notifier tests**

Create `backend/tests/test_telegram_notifier.py`:

```python
import httpx
import pytest

from backend.telegram_notifier import TelegramNotifier, should_send_telegram_alert


def test_should_send_telegram_alert_requires_high_or_critical_with_flag():
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "high"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "critical"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "normal"}) is False
    assert should_send_telegram_alert({"telegram_alert": False, "urgency": "critical"}) is False
    assert should_send_telegram_alert({}) is False


def test_notifier_disabled_for_missing_or_placeholder_config():
    assert TelegramNotifier("", "123").enabled is False
    assert TelegramNotifier("your_telegram_bot_token_here", "123").enabled is False
    assert TelegramNotifier("token", "your_telegram_chat_id_here").enabled is False


def test_format_analysis_alert_uses_plain_text():
    notifier = TelegramNotifier("token", "123")
    message = notifier.format_analysis_alert(
        stock="삼성전자",
        source="disclosure",
        analysis_data={
            "summary": "대량보유 변동",
            "details": {
                "decision": "HOLD",
                "confidence_score": 0.82,
                "reason": "단기 변동성 확대 가능성",
            },
            "urgency": "critical",
            "urgency_reason": "대량보유 변동 공시",
            "telegram_alert": True,
        },
    )

    assert "[긴급] 삼성전자 / disclosure" in message
    assert "Decision: HOLD (0.82)" in message
    assert "Reason: 단기 변동성 확대 가능성" in message
    assert "Urgency: critical - 대량보유 변동 공시" in message
    assert "Summary: 대량보유 변동" in message


@pytest.mark.asyncio
async def test_send_analysis_alert_skips_when_gate_is_false(monkeypatch):
    notifier = TelegramNotifier("token", "123")
    called = False

    async def fake_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(notifier, "_post_message", fake_post)

    result = await notifier.send_analysis_alert(
        "삼성전자",
        "news",
        {"telegram_alert": False, "urgency": "critical"},
    )

    assert result is False
    assert called is False


@pytest.mark.asyncio
async def test_send_analysis_alert_returns_false_on_http_error(monkeypatch):
    notifier = TelegramNotifier("token", "123")

    async def fake_post(*args, **kwargs):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(notifier, "_post_message", fake_post)

    result = await notifier.send_analysis_alert(
        "삼성전자",
        "news",
        {"telegram_alert": True, "urgency": "high"},
    )

    assert result is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --project backend pytest backend/tests/test_telegram_notifier.py -q
```

Expected: fail because `backend.telegram_notifier` does not exist.

- [ ] **Step 3: Implement notifier**

Create `backend/telegram_notifier.py`:

```python
import logging
from typing import Any

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, is_placeholder_secret

logger = logging.getLogger(__name__)

URGENT_TELEGRAM_LEVELS = {"high", "critical"}


def should_send_telegram_alert(analysis_data: dict[str, Any]) -> bool:
    return (
        analysis_data.get("telegram_alert") is True
        and analysis_data.get("urgency") in URGENT_TELEGRAM_LEVELS
    )


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None = TELEGRAM_BOT_TOKEN,
        chat_id: str | None = TELEGRAM_CHAT_ID,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = not (
            is_placeholder_secret(self.bot_token)
            or is_placeholder_secret(self.chat_id)
        )

    def format_analysis_alert(
        self,
        *,
        stock: str,
        source: str,
        analysis_data: dict[str, Any],
    ) -> str:
        details = analysis_data.get("details") or {}
        decision = details.get("decision", "HOLD")
        confidence = details.get("confidence_score", "")
        reason = details.get("reason") or analysis_data.get("summary", "")
        urgency = analysis_data.get("urgency", "normal")
        urgency_reason = analysis_data.get("urgency_reason") or "긴급 판단 사유 없음"
        summary = analysis_data.get("summary", "")

        confidence_text = f" ({confidence:.2f})" if isinstance(confidence, int | float) else ""
        lines = [
            f"[긴급] {stock} / {source}",
            f"Decision: {decision}{confidence_text}",
            f"Reason: {reason}",
            f"Urgency: {urgency} - {urgency_reason}",
        ]
        if summary:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)[:4000]

    async def send_analysis_alert(
        self,
        stock: str,
        source: str,
        analysis_data: dict[str, Any],
    ) -> bool:
        if not self.enabled:
            return False
        if not should_send_telegram_alert(analysis_data):
            return False

        try:
            await self._post_message(
                self.format_analysis_alert(
                    stock=stock,
                    source=source,
                    analysis_data=analysis_data,
                )
            )
            return True
        except Exception as exc:
            logger.error("Telegram alert send failed for %s/%s: %s", source, stock, exc)
            return False

    async def _post_message(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()


telegram_notifier = TelegramNotifier()
```

- [ ] **Step 4: Run notifier tests**

Run:

```bash
uv run --project backend pytest backend/tests/test_telegram_notifier.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backend/telegram_notifier.py backend/tests/test_telegram_notifier.py
git commit -m "feat: 텔레그램 긴급 알림 전송기 추가" -m "- outbound-only Bot API notifier 추가
- 긴급도 gate 및 실패 격리 테스트 추가"
```

## Task 5: Wire Notifier Into Scheduler Without Blocking WebSocket

**Files:**
- Modify: `backend/scheduler.py`
- Modify: `backend/tests/test_scheduler.py`

- [ ] **Step 1: Add scheduler tests**

Add these tests to `backend/tests/test_scheduler.py`:

```python
@pytest.mark.asyncio
async def test_monitor_signal_sends_telegram_for_urgent_analysis(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        return "urgent signal"

    async def mock_check_significance(stock, current, last, *, source, provider):
        return True

    async def mock_perform_analysis(*args, **kwargs):
        return {"summary": "긴급", "details": {"decision": "HOLD"}, "telegram_alert": True, "urgency": "high"}

    mock_telegram = MagicMock(return_value=asyncio.Future())
    mock_telegram.return_value.set_result(True)
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

    mock_telegram.assert_called_once()
    mock_broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_signal_keeps_websocket_when_telegram_fails(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        return "urgent signal"

    async def mock_check_significance(stock, current, last, *, source, provider):
        return True

    async def mock_perform_analysis(*args, **kwargs):
        return {"summary": "긴급", "details": {"decision": "HOLD"}, "telegram_alert": True, "urgency": "critical"}

    async def failing_telegram(*args, **kwargs):
        raise RuntimeError("telegram down")

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", failing_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

    mock_broadcast.assert_called_once()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --project backend pytest backend/tests/test_scheduler.py::test_monitor_signal_sends_telegram_for_urgent_analysis backend/tests/test_scheduler.py::test_monitor_signal_keeps_websocket_when_telegram_fails -q
```

Expected: fail because scheduler does not import or call `telegram_notifier`.

- [ ] **Step 3: Add notifier import**

In `backend/scheduler.py`, replace any old import:

```python
from .telegram_bot import notifier as telegram_notifier
```

with:

```python
from .telegram_notifier import telegram_notifier
```

- [ ] **Step 4: Add isolated send helper**

Add above `_monitor_signal`:

```python
async def _send_telegram_alert_if_needed(
    stock: str,
    source: str,
    analysis_data: dict[str, Any],
) -> None:
    try:
        await telegram_notifier.send_analysis_alert(stock, source, analysis_data)
    except Exception as e:
        logger.error("[%s:%s] Telegram 알림 처리 중 오류: %s", source, stock, e)
```

- [ ] **Step 5: Call helper before WebSocket broadcast**

In `_monitor_signal`, after `_set_last_signal_state(...)` and before `manager.broadcast(...)`, add:

```python
        await _send_telegram_alert_if_needed(stock, source.name, analysis_data)
```

- [ ] **Step 6: Run scheduler tests**

Run:

```bash
uv run --project backend pytest backend/tests/test_scheduler.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add backend/scheduler.py backend/tests/test_scheduler.py
git commit -m "feat: 스케줄러 텔레그램 긴급 알림 연결" -m "- AGENT_ANALYSIS 긴급도 gate 기반 알림 연결
- Telegram 실패 시 WebSocket 유지 테스트 추가"
```

## Task 6: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Check lockfile consistency**

Run:

```bash
uv lock --check --project backend
```

Expected: lockfile is current. If `uv` reports lockfile drift, run `uv lock --project backend`, inspect `backend/uv.lock`, and commit only expected lockfile changes.

- [ ] **Step 2: Run focused backend tests**

Run:

```bash
uv run --project backend pytest backend/tests/test_telegram_notifier.py backend/tests/test_services.py backend/tests/test_scheduler.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run full backend tests**

Run:

```bash
uv run --project backend pytest backend/tests -q
```

Expected: all backend tests pass or Redis integration tests skip when `REDIS_INTEGRATION_URL` is unset.

- [ ] **Step 4: Check for accidental crawling or browser automation logic**

Run:

```bash
rg -n "playwright|chromium|puppeteer|cheerio|querySelector|document\\.|innerHTML|scrap|crawl|크롤|selenium|BeautifulSoup|bs4" backend mcp-news mcp-trading mcp-dart finus_nat
```

Expected: no runtime code matches.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git diff --stat origin/main...HEAD
git diff --name-status origin/main...HEAD
```

Expected: diff includes issue #36 Telegram urgent alert files only, plus removal of inherited stale requirements files.

- [ ] **Step 6: Commit any remaining verification-only fixes**

If previous tasks left uncommitted changes, commit them with:

```bash
git add .
git commit -m "test: 텔레그램 긴급 알림 검증 정리" -m "- backend 테스트 및 lockfile 정리"
```

Do not push until the user confirms the branch publishing strategy.
