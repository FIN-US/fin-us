"""설명 수준 설정(/level, /start 온보딩)과 출력 계층의 결합 테스트 (#297).

test_telegram_commands.py에 얹지 않은 이유는 그 파일이 이미 3천 줄이 넘고, 여기서 고정하는
것은 명령 라우팅이 아니라 "수준이 나가는 문장에 실제로 반영되는가"이기 때문이다.
"""

from datetime import date
from types import SimpleNamespace
from typing import cast

import pytest

import backend.telegram_commands as telegram_commands
from backend.presentation import (
    LEVEL_BEGINNER,
    LEVEL_INTERMEDIATE,
    REASONING_FOOTNOTE_SEPARATOR,
    TERM_FOOTNOTE_MARK,
)
from backend.telegram_commands import (
    LEVEL_COMMAND_HELP,
    LEVEL_ONBOARDING_QUESTION,
    TelegramCommandHandler,
)
from backend.services import NatAnswer, NatToolUse
from backend.telegram_notifier import TelegramNotifier


@pytest.fixture(autouse=True)
def _clear_level_cache():
    # 수준 캐시는 모듈 수준이다. 비우지 않으면 앞 테스트가 저장한 값이 다음 테스트의
    # 저장소를 덮어써, 통과 여부가 실행 순서에 달리게 된다.
    telegram_commands.reset_level_cache()
    yield
    telegram_commands.reset_level_cache()


# 아래 주입 지점의 cast는 이 대역 때문이다 (#292). 주입 지점의 선언 타입이 구체
# 클래스라 대역이 그대로는 타입 검사를 통과하지 못한다. 이 주입 지점을 Protocol로
# 좁히고 아래 cast를 걷어내는 것은 #319가 추적한다(#271이 좁힌 것은 state_store와
# pending_order_store 둘뿐이다). 그때까지는 주입 지점에서만 좁혀 둔다.
class FakeNotifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.bot_username = ""
        self.messages = []
        self.reply_markups = []
        self.actions = []
        self.callback_answers = []
        self.deletes = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return True

    async def send_text_returning_id(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return 1

    async def delete_message(self, message_id):
        self.deletes.append(message_id)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.callback_answers.append((callback_query_id, text))
        return True


class FakeState:
    def __init__(self, level=None):
        self.level = level
        self.writes = []

    async def get_telegram_user_level(self):
        return self.level or LEVEL_BEGINNER

    async def set_telegram_user_level(self, level):
        self.level = level
        self.writes.append(level)
        return True


class BrokenState:
    """redis가 없을 때의 저장소. 열기 자체가 실패한다."""

    def __init__(self):
        raise ConnectionError("redis unavailable")


def _handler(notifier, state=None, **kwargs):
    return TelegramCommandHandler(
        notifier=notifier,
        state_factory=(lambda: state) if state is not None else BrokenState,
        **kwargs,
    )


async def _send(handler, text):
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": text}})


async def _press(handler, data):
    await handler.handle_update(
        {
            "callback_query": {
                "id": "cb",
                "data": data,
                "message": {"chat": {"id": 123}},
            }
        }
    )


# ---- /level ----


@pytest.mark.asyncio
async def test_level_without_argument_shows_current_setting_and_buttons():
    notifier = FakeNotifier()
    state = FakeState(LEVEL_INTERMEDIATE)

    await _send(_handler(notifier, state), "/level")

    assert notifier.messages[-1] == f"현재 설명 수준: 중급\n{LEVEL_COMMAND_HELP}"
    assert state.writes == []
    buttons = notifier.reply_markups[-1]["inline_keyboard"][0]
    assert [button["callback_data"] for button in buttons] == [
        "level:beginner",
        "level:intermediate",
    ]


@pytest.mark.asyncio
async def test_level_command_stores_the_normalized_value():
    """사용자는 한국어로 치지만 저장은 정규값으로 한다 — 표기가 여러 벌 쌓이면 안 된다."""
    notifier = FakeNotifier()
    state = FakeState()

    await _send(_handler(notifier, state), "/level 중급")

    assert state.writes == [LEVEL_INTERMEDIATE]
    assert "중급" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_level_argument_shows_usage_without_storing():
    notifier = FakeNotifier()
    state = FakeState()

    await _send(_handler(notifier, state), "/level 고수")

    assert state.writes == []
    assert notifier.messages[-1] == LEVEL_COMMAND_HELP


# ---- /start 온보딩 ----


@pytest.mark.asyncio
async def test_start_asks_one_question_and_stores_nothing():
    """버튼을 누르지 않고 지나간 사용자도 기본값(초보)으로 동작해야 한다."""
    notifier = FakeNotifier()
    state = FakeState()

    await _send(_handler(notifier, state), "/start")

    assert LEVEL_ONBOARDING_QUESTION in notifier.messages[-1]
    assert state.writes == []
    assert len(notifier.reply_markups[-1]["inline_keyboard"][0]) == 2


@pytest.mark.asyncio
async def test_onboarding_button_stores_the_choice():
    notifier = FakeNotifier()
    state = FakeState()

    await _press(_handler(notifier, state), f"level:{LEVEL_INTERMEDIATE}")

    assert state.writes == [LEVEL_INTERMEDIATE]
    assert notifier.callback_answers == [("cb", None)]


@pytest.mark.asyncio
async def test_unknown_level_button_is_rejected():
    notifier = FakeNotifier()
    state = FakeState()

    await _press(_handler(notifier, state), "level:고수")

    assert state.writes == []
    assert notifier.callback_answers == [("cb", "지원하지 않는 버튼입니다.")]


# ---- 나가는 문장에의 반영 ----


def _nat_runner(answer):
    async def runner(provider, text, *, conversation_id=None):
        return answer

    return runner


@pytest.mark.asyncio
async def test_beginner_answer_carries_both_footnotes():
    """용어 각주와 #260의 추론 각주가 이 순서로 공존한다."""
    notifier = FakeNotifier()
    handler = _handler(
        notifier,
        FakeState(LEVEL_BEGINNER),
        llm_runner=_nat_runner(
            NatAnswer(
                "예수금은 120만원 남아 있고, 어제 주문은 전부 체결됐습니다.",
                routed_agent="trading_agent",
                tools_used=(NatToolUse("finus_account_balance", ok=True),),
            )
        ),
    )

    await _send(handler, "잔고 알려줘")

    message = notifier.messages[-1]
    assert f"{TERM_FOOTNOTE_MARK} 예수금" in message
    assert message.index(TERM_FOOTNOTE_MARK) < message.index(REASONING_FOOTNOTE_SEPARATOR)
    assert message.endswith("📚 확인한 자료: KIS 시세·계좌 조회")


@pytest.mark.asyncio
async def test_intermediate_answer_drops_only_the_term_footnote():
    notifier = FakeNotifier()
    handler = _handler(
        notifier,
        FakeState(LEVEL_INTERMEDIATE),
        llm_runner=_nat_runner(
            NatAnswer(
                "예수금은 120만원 남아 있고, 어제 주문은 전부 체결됐습니다.",
                routed_agent="trading_agent",
                tools_used=(NatToolUse("finus_account_balance", ok=True),),
            )
        ),
    )

    await _send(handler, "잔고 알려줘")

    message = notifier.messages[-1]
    assert TERM_FOOTNOTE_MARK not in message
    assert REASONING_FOOTNOTE_SEPARATOR in message


@pytest.mark.asyncio
async def test_markdown_residue_never_reaches_telegram():
    """이 봇은 parse_mode를 쓰지 않는다. 남으면 별표가 그대로 화면에 뜬다."""
    notifier = FakeNotifier()
    handler = _handler(
        notifier,
        FakeState(LEVEL_INTERMEDIATE),
        llm_runner=_nat_runner("### 요약\n**중요**: 오늘은 관망을 권합니다."),
    )

    await _send(handler, "오늘 어때?")

    assert notifier.messages[-1] == "요약\n중요: 오늘은 관망을 권합니다."


@pytest.mark.asyncio
async def test_diary_answer_uses_the_diary_template():
    notifier = FakeNotifier()
    handler = _handler(
        notifier,
        FakeState(LEVEL_INTERMEDIATE),
        llm_runner=_nat_runner(
            NatAnswer("오늘 일지를 저장했습니다.", routed_agent="diary_agent", tools_used=())
        ),
    )

    await _send(handler, "오늘 일지 써줘")

    assert notifier.messages[-1].startswith("📓 매매일지\n오늘 일지를 저장했습니다.")


def _mcp_handler(notifier, response, state=None):
    async def mcp_runner(server_params, tool_name, arguments):
        return response

    return TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        state_factory=(lambda: state) if state is not None else BrokenState,
    )


@pytest.mark.asyncio
async def test_balance_goes_through_the_output_layer():
    """문서가 대표 예시로 든 용어(예수금·평가손익)가 사는 자리다 (#297 자가리뷰).

    자연어로 물으면 설명되는데 /balance로 물으면 안 되는 차이는 설명할 수 없다.
    """
    notifier = FakeNotifier()
    handler = _mcp_handler(
        notifier,
        "예수금: 1,204,300원\n**평가손익**: +48,000원",
        FakeState(LEVEL_BEGINNER),
    )

    await _send(handler, "/balance")

    message = notifier.messages[-1]
    assert "**" not in message  # 마크다운 잔재도 함께 정리된다
    assert f"{TERM_FOOTNOTE_MARK} 예수금: " in message


@pytest.mark.asyncio
async def test_catalysts_use_the_shared_list_marker():
    """이 명령만 "•"를 쓰고 있었다. 한 화면에 두 종류가 섞이면 안 된다."""
    notifier = FakeNotifier()

    class FakeCatalystRepo:
        async def list_upcoming(self, stock_name, *, today, limit=20):
            return [
                SimpleNamespace(
                    event_date=date(2026, 8, 20),
                    description="실적 발표",
                    event_type="earnings",
                )
            ]

    handler = TelegramCommandHandler(
        notifier=cast(TelegramNotifier, notifier),
        catalyst_repo=FakeCatalystRepo(),
        state_factory=lambda: FakeState(LEVEL_INTERMEDIATE),
    )

    await _send(handler, "/catalysts 삼성전자")

    lines = notifier.messages[-1].splitlines()
    assert lines[0].startswith("📅 삼성전자")
    assert lines[1].startswith("- 2026-08-20")
    assert "•" not in notifier.messages[-1]


@pytest.mark.asyncio
async def test_level_lookup_failure_falls_back_to_beginner_without_failing_the_message():
    """수준을 못 읽었다고 답변 자체를 실패시키면 부가 기능이 본 기능을 잡아먹는다."""
    notifier = FakeNotifier()
    handler = _handler(
        notifier,
        None,  # BrokenState — 열기 자체가 실패한다
        llm_runner=_nat_runner("예수금은 120만원 남아 있고 추가 매수 여력이 있습니다."),
    )

    await _send(handler, "잔고 알려줘")

    assert f"{TERM_FOOTNOTE_MARK} 예수금" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_level_lookup_is_not_repeated_for_every_message():
    """redis가 죽어 있는 동안 메시지마다 소켓 타임아웃을 다시 물면 안 된다 (#268)."""
    opened = 0

    class CountingState(FakeState):
        def __init__(self):
            nonlocal opened
            opened += 1
            super().__init__(LEVEL_INTERMEDIATE)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=cast(TelegramNotifier, notifier),
        state_factory=CountingState,
        llm_runner=_nat_runner("답변입니다."),
    )

    await _send(handler, "질문 1")
    await _send(handler, "질문 2")

    assert opened == 1
