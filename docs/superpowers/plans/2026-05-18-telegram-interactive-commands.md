# Telegram Interactive Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement issue #41 by adding Telegram MCP lookup commands and NAT chat fallback while preserving PR #38 NAT conversation-id isolation and existing `/alerts` behavior.

**Architecture:** Keep `backend/telegram_commands.py` as the Telegram interaction boundary. Extend `TelegramCommandHandler` with small private helpers, inject MCP/NAT runners for tests, and keep `TelegramCommandPoller` offset semantics unchanged. Use `TelegramNotifier` only for Telegram API delivery helpers and do not mix this path into scheduler reports or `/api/v1/ws`.

**Tech Stack:** FastAPI backend modules, `httpx`, Telegram Bot API, MCP stdio client through existing `run_mcp_tool`, NAT chat through existing `llm_chat`, `pytest`, `pytest-asyncio`, `uv`.

---

## File Structure

- Modify `backend/telegram_commands.py`
  - Add command constants and message truncation helper.
  - Add optional `mcp_runner` and `llm_runner` dependencies.
  - Route `/help`, `/balance`, `/quote`, `/trend`, unknown slash commands, and free-form NAT chat.
  - Preserve existing `/alerts` behavior and poller offset rule.

- Modify `backend/telegram_notifier.py`
  - Add `send_chat_action(action: str = "typing")` for slow Telegram work.
  - Keep `send_text()` behavior compatible with existing tests.

- Modify `backend/tests/test_telegram_commands.py`
  - Extend `FakeNotifier` for chat actions.
  - Add handler tests for help, MCP commands, missing args, unknown slash commands, NAT fallback, and failure UX.
  - Keep current `/alerts` and offset tests.

- Modify `backend/tests/test_telegram_notifier.py`
  - Add a small unit test for `send_chat_action()` HTTP payload.

- Modify `README.md`
  - Add the new Telegram interactive commands next to the existing `/alerts` section after implementation is passing.

---

### Task 1: Add MCP Telegram Commands

**Files:**
- Modify: `backend/telegram_commands.py`
- Modify: `backend/tests/test_telegram_commands.py`
- Modify: `backend/tests/test_telegram_notifier.py`
- Modify: `backend/telegram_notifier.py`
- Commit: `feat: 텔레그램 MCP 조회 명령 추가`

- [ ] **Step 1: Add failing tests for help and command routing**

Edit `backend/tests/test_telegram_commands.py`.

Update `FakeNotifier`:

```python
class FakeNotifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.messages = []
        self.actions = []

    async def send_text(self, text):
        self.messages.append(text)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True
```

Add these tests below the existing `/alerts` tests:

```python
@pytest.mark.asyncio
async def test_help_command_replies_with_supported_commands():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/help"}})

    assert "/alerts urgent|all|off|status" in notifier.messages[-1]
    assert "/balance" in notifier.messages[-1]
    assert "/quote <종목명>" in notifier.messages[-1]
    assert "/trend <종목명>" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_slash_command_replies_with_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/unknown"}})

    assert "/help" in notifier.messages[-1]
    assert "/balance" in notifier.messages[-1]
```

- [ ] **Step 2: Run help tests and verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_help_command_replies_with_supported_commands backend/tests/test_telegram_commands.py::test_unknown_slash_command_replies_with_help -q
```

Expected: FAIL because `TelegramCommandHandler` returns for non-`/alerts` text.

- [ ] **Step 3: Add help constants and routing**

Edit `backend/telegram_commands.py`.

Add imports:

```python
from .config import TRADING_MCP_PARAMS
from .services import run_mcp_tool
```

Add constants below `ALERT_COMMAND_HELP`:

```python
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
        "일반 문장은 NAT에게 바로 질문합니다.",
    ]
)
QUOTE_COMMAND_HELP = "사용법: /quote <종목명>"
TREND_COMMAND_HELP = "사용법: /trend <종목명>"
TELEGRAM_MESSAGE_LIMIT = 4000
TELEGRAM_TRUNCATION_SUFFIX = "...(이하 생략)"
```

Add helper functions above `TelegramCommandHandler`:

```python
def _telegram_text(text: str) -> str:
    value = (text or "").strip()
    if len(value) <= TELEGRAM_MESSAGE_LIMIT:
        return value
    keep = TELEGRAM_MESSAGE_LIMIT - len(TELEGRAM_TRUNCATION_SUFFIX)
    return f"{value[:keep]}{TELEGRAM_TRUNCATION_SUFFIX}"


def _short_error(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    text = str(detail if detail is not None else exc).strip()
    return _telegram_text(text)[:300] or exc.__class__.__name__
```

Update `handle_update()` routing:

```python
        text = (message.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/alerts"):
            await self._handle_alerts(text)
            return
        if text == "/help" or text.startswith("/help "):
            await self.notifier.send_text(TELEGRAM_INTERACTIVE_HELP)
            return
        if text == "/balance" or text.startswith("/balance "):
            await self._handle_balance()
            return
        if text == "/quote" or text.startswith("/quote "):
            await self._handle_quote(text)
            return
        if text == "/trend" or text.startswith("/trend "):
            await self._handle_trend(text)
            return
        if text.startswith("/"):
            await self.notifier.send_text(TELEGRAM_INTERACTIVE_HELP)
            return
```

Move the existing `/alerts` body into a new method:

```python
    async def _handle_alerts(self, text: str) -> None:
        parts = text.split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        async with self._state() as state:
            if action == "status":
                mode = await state.get_telegram_alert_mode()
                await self.notifier.send_text(f"현재 Telegram 알림 모드: {mode}")
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self.notifier.send_text(ALERT_COMMAND_HELP)
                return

            await state.set_telegram_alert_mode(action)
            await self.notifier.send_text(f"Telegram 알림 모드가 {action}(으)로 변경되었습니다.")
```

- [ ] **Step 4: Run help tests and verify pass**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_help_command_replies_with_supported_commands backend/tests/test_telegram_commands.py::test_unknown_slash_command_replies_with_help -q
```

Expected: PASS.

- [ ] **Step 5: Add failing tests for MCP commands**

Edit `backend/tests/test_telegram_commands.py`.

Add:

```python
@pytest.mark.asyncio
async def test_balance_command_calls_mcp_runner():
    calls = []

    async def fake_mcp_runner(params, tool_name, arguments):
        calls.append((params, tool_name, arguments))
        return "잔고 결과"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=fake_mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    assert calls[0][1:] == ("get_balance", {})
    assert notifier.actions == ["typing"]
    assert notifier.messages[-1] == "잔고 결과"


@pytest.mark.asyncio
async def test_quote_command_calls_mcp_runner_with_stock_name():
    calls = []

    async def fake_mcp_runner(params, tool_name, arguments):
        calls.append((params, tool_name, arguments))
        return "현재가 결과"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=fake_mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/quote 삼성전자"}})

    assert calls[0][1:] == ("get_stock_quote", {"stock_name": "삼성전자"})
    assert notifier.actions == ["typing"]
    assert notifier.messages[-1] == "현재가 결과"


@pytest.mark.asyncio
async def test_trend_command_calls_mcp_runner_with_stock_name():
    calls = []

    async def fake_mcp_runner(params, tool_name, arguments):
        calls.append((params, tool_name, arguments))
        return "수급 결과"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=fake_mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}})

    assert calls[0][1:] == ("get_investor_trading", {"stock_name": "삼성전자"})
    assert notifier.actions == ["typing"]
    assert notifier.messages[-1] == "수급 결과"


@pytest.mark.asyncio
async def test_quote_and_trend_missing_args_reply_with_usage():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/quote"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trend"}})

    assert notifier.messages[-2] == "사용법: /quote <종목명>"
    assert notifier.messages[-1] == "사용법: /trend <종목명>"
```

- [ ] **Step 6: Run MCP tests and verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_balance_command_calls_mcp_runner backend/tests/test_telegram_commands.py::test_quote_command_calls_mcp_runner_with_stock_name backend/tests/test_telegram_commands.py::test_trend_command_calls_mcp_runner_with_stock_name backend/tests/test_telegram_commands.py::test_quote_and_trend_missing_args_reply_with_usage -q
```

Expected: FAIL because `TelegramCommandHandler.__init__()` does not accept `mcp_runner` and command helpers do not exist.

- [ ] **Step 7: Implement MCP runner injection and helpers**

Edit `backend/telegram_commands.py`.

Update `TelegramCommandHandler.__init__()`:

```python
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.mcp_runner = mcp_runner
```

Add helper methods inside `TelegramCommandHandler`:

```python
    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))

    async def _handle_quote(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self.notifier.send_text(QUOTE_COMMAND_HELP)
            return
        stock = parts[1].strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_stock_quote",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))

    async def _handle_trend(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self.notifier.send_text(TREND_COMMAND_HELP)
            return
        stock = parts[1].strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_investor_trading",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))
```

- [ ] **Step 8: Add failing notifier chat action test**

Edit `backend/tests/test_telegram_notifier.py`.

Add:

```python
@pytest.mark.asyncio
async def test_send_chat_action_posts_typing_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeClient)

    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_chat_action() is True
    assert captured["url"] == "https://api.telegram.org/bottoken/sendChatAction"
    assert captured["json"] == {"chat_id": "123", "action": "typing"}
```

- [ ] **Step 9: Run notifier chat action test and verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_notifier.py::test_send_chat_action_posts_typing_payload -q
```

Expected: FAIL because `send_chat_action()` does not exist.

- [ ] **Step 10: Implement notifier chat action**

Edit `backend/telegram_notifier.py`.

Add method below `send_text()`:

```python
    async def send_chat_action(self, action: str = "typing") -> bool:
        if not self.enabled:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendChatAction"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "action": action,
                    },
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram chat action send failed: %s", exc)
            return False
```

- [ ] **Step 11: Add MCP failure and truncation tests**

Edit `backend/tests/test_telegram_commands.py`.

Add:

```python
@pytest.mark.asyncio
async def test_mcp_failure_replies_with_short_failure_message():
    async def failing_mcp_runner(params, tool_name, arguments):
        raise RuntimeError("kis unavailable")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=failing_mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    assert notifier.messages[-1] == "조회 실패: kis unavailable"


@pytest.mark.asyncio
async def test_mcp_result_is_truncated_for_telegram_limit():
    async def fake_mcp_runner(params, tool_name, arguments):
        return "가" * 4100

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=fake_mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    assert len(notifier.messages[-1]) == 4000
    assert notifier.messages[-1].endswith("...(이하 생략)")
```

- [ ] **Step 12: Run Task 1 focused tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py -q
```

Expected: PASS.

- [ ] **Step 13: Commit Task 1**

Run:

```bash
git status --short
git diff -- backend/telegram_commands.py backend/telegram_notifier.py backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py
git add backend/telegram_commands.py backend/telegram_notifier.py backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py
git commit -m "feat: 텔레그램 MCP 조회 명령 추가" -m "- /help, /balance, /quote, /trend 라우팅 추가" -m "- MCP 조회 실패와 긴 메시지 응답 처리 추가"
```

Expected: one atomic commit containing only MCP command support and notifier chat action support.

---

### Task 2: Add NAT Chat Fallback

**Files:**
- Modify: `backend/telegram_commands.py`
- Modify: `backend/tests/test_telegram_commands.py`
- Commit: `feat: 텔레그램 NAT 채팅 fallback 추가`

- [ ] **Step 1: Add failing tests for NAT fallback routing**

Edit `backend/tests/test_telegram_commands.py`.

Add:

```python
@pytest.mark.asyncio
async def test_regular_text_calls_nat_with_telegram_conversation_id():
    calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append((provider, text, conversation_id))
        return "NAT 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "삼성전자 오늘 어때?"}})

    assert calls == [("nat", "삼성전자 오늘 어때?", "telegram:123")]
    assert notifier.actions == ["typing"]
    assert notifier.messages[-1] == "NAT 응답"


@pytest.mark.asyncio
async def test_regular_text_uses_actual_chat_id_in_conversation_id():
    calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append(conversation_id)
        return "ok"

    notifier = FakeNotifier(chat_id="456")
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 456}, "text": "계속 봐줘"}})

    assert calls == ["telegram:456"]
```

- [ ] **Step 2: Run NAT routing tests and verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_regular_text_calls_nat_with_telegram_conversation_id backend/tests/test_telegram_commands.py::test_regular_text_uses_actual_chat_id_in_conversation_id -q
```

Expected: FAIL because `TelegramCommandHandler.__init__()` does not accept `llm_runner` and regular text is not routed.

- [ ] **Step 3: Implement NAT runner injection and fallback**

Edit `backend/telegram_commands.py`.

Add import:

```python
from .services import llm_chat, run_mcp_tool
```

Update `TelegramCommandHandler.__init__()`:

```python
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
        llm_runner: Callable[..., Any] = llm_chat,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.mcp_runner = mcp_runner
        self.llm_runner = llm_runner
```

At the end of `handle_update()`, after unknown slash command handling, add:

```python
        await self._handle_chat_fallback(text, str(chat.get("id", "")).strip())
```

Add helper method:

```python
    async def _handle_chat_fallback(self, text: str, chat_id: str) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.llm_runner(
                "nat",
                text,
                conversation_id=f"telegram:{chat_id}",
            )
        except Exception as exc:
            await self.notifier.send_text(f"응답 생성 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))
```

- [ ] **Step 4: Add NAT failure and truncation tests**

Edit `backend/tests/test_telegram_commands.py`.

Add:

```python
@pytest.mark.asyncio
async def test_nat_failure_replies_with_short_failure_message():
    async def failing_llm_runner(provider, text, *, conversation_id=None):
        raise RuntimeError("nat unavailable")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=failing_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "질문"}})

    assert notifier.messages[-1] == "응답 생성 실패: nat unavailable"


@pytest.mark.asyncio
async def test_nat_response_is_truncated_for_telegram_limit():
    async def fake_llm_runner(provider, text, *, conversation_id=None):
        return "나" * 4100

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "긴 답변 줘"}})

    assert len(notifier.messages[-1]) == 4000
    assert notifier.messages[-1].endswith("...(이하 생략)")
```

- [ ] **Step 5: Run Task 2 focused tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git status --short
git diff -- backend/telegram_commands.py backend/tests/test_telegram_commands.py
git add backend/telegram_commands.py backend/tests/test_telegram_commands.py
git commit -m "feat: 텔레그램 NAT 채팅 fallback 추가" -m "- 일반 텍스트 NAT 라우팅 추가" -m "- telegram:{chat_id} conversation-id 전달 검증 추가"
```

Expected: one atomic commit containing only NAT fallback behavior and tests.

---

### Task 3: Document Telegram Interactive Commands

**Files:**
- Modify: `README.md`
- Commit: `docs: 텔레그램 인터랙티브 명령 문서화`

- [ ] **Step 1: Update README after implementation passes**

Edit `README.md` in the existing Telegram section. Replace the command list block with:

```text
/alerts urgent  # high/critical 긴급 분석만 전송
/alerts all     # NAT 분석이 실행될 때마다 전송
/alerts off     # Telegram 분석 알림 중지
/alerts status  # 현재 알림 모드 확인
/help           # 사용 가능한 Telegram 명령 확인
/balance        # 예수금·총자산·보유 종목 조회
/quote <종목명> # 현재가 조회
/trend <종목명> # 외국인·기관·개인 수급 조회
```

Add this short paragraph after the block:

```markdown
슬래시 명령이 아닌 일반 텍스트는 NAT 채팅으로 전달됩니다. Telegram 채팅은 `telegram:{chat_id}` conversation id를 사용하므로 스케줄러 분석 리포트와 대화 이력이 섞이지 않습니다.
```

- [ ] **Step 2: Verify README diff**

Run:

```bash
git diff -- README.md
```

Expected: diff only updates the Telegram command documentation.

- [ ] **Step 3: Commit Task 3**

Run:

```bash
git add README.md
git commit -m "docs: 텔레그램 인터랙티브 명령 문서화" -m "- /help, MCP 조회 명령 사용법 추가" -m "- Telegram NAT chat conversation-id 경계 설명"
```

Expected: one docs-only commit.

---

### Task 4: Final Verification

**Files:**
- Verify only; no planned file changes.

- [ ] **Step 1: Run focused Telegram regression suite**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```

Expected: PASS.

- [ ] **Step 2: Run service regression tests for PR #38 compatibility**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_services.py -q
```

Expected: PASS, including NAT `conversation_id` tests.

- [ ] **Step 3: Inspect commit split**

Run:

```bash
git log --oneline --decorate -6
git status --short --branch
```

Expected:

- Branch is `feat/issue-41-telegram-interactive-commands`.
- Working tree is clean.
- Commits are separated into spec, plan, MCP implementation, NAT fallback implementation, and README docs.

- [ ] **Step 4: Prepare PR draft only after user asks**

When the user asks to open a PR, read `.github/PULL_REQUEST_TEMPLATE.md`, inspect the final diff against `main`, print the full PR draft first, and create the PR only after approval.

Run for diff context:

```bash
git diff --stat main...HEAD
git diff main...HEAD -- backend/telegram_commands.py backend/telegram_notifier.py backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py README.md
```

Expected: diff matches issue #41 scope only.
