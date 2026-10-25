"""#451: 후속 턴이 직전 답의 맥락을 잇는다.

fe_branch는 서브 에이전트(ReAct)에 최근 대화를 붙여 넘기는데, assistant 답을 앞 180자로 잘랐다
(도입 커밋 46239426: "긴 잔고 답변을 짧게 잘라 ReAct 입력 부담을 줄임"). 그래서 "그중 두 번째
종목은?"(앞쪽 목록의 둘째 항목)과 추가 제안에 대한 "응"(답의 끝)이 맥락을 잃었다.

수정은 직전 답 한 건만 넉넉히(기본 1000자) 넘기고, 넘치면 가운데를 줄인다. 더 오래된 답은 180자
그대로다 — 입력 부담이라는 도입 이유를 지키려고 늘어나는 양을 한 건으로 묶는다.
실 KIS·OpenAI 연결 없이 동작한다.
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from nat.data_models.api_server import (
    ChatRequest,
    ChatRequestOrMessage,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType as Role,
)
from nat.utils.type_converter import GlobalTypeConverter

from finus_agents.agents import (
    FeBranchConfig,
    FinusReasoningTraceAgentConfig,
    FinusSqliteTranscriptAgentConfig,
    _contextual_user_query,
    _format_history,
    finus_reasoning_trace_agent,
    finus_sqlite_transcript_agent,
    truncate_middle,
)
from finus_agents.pii_guard import mask_tool_result

_DEFAULTS = FeBranchConfig(inner_function_name="trading_agent_react")

_SECOND_ITEM = "2. 한화에어로스페이스(012450): 수익률 +18.7%"
_OFFER = "같은 메모리 업종인 SK하이닉스와 오늘 등락률·수급을 비교해 드릴까요?"


def _long_answer(*, filler_lines: int = 30) -> str:
    """목록이 앞에서 시작하고 끝에 추가 제안이 붙은 긴 답. 둘째 항목은 앞 180자 밖에 있다."""
    head = (
        "현재 계좌 잔고를 조회했습니다. 보유 종목은 5개이고 평가금액이 큰 순서로 정리하면 다음과 같습니다. "
        "아래 수익률은 평균 매입가 기준이며 수수료와 세금은 반영하지 않았고, 평가금액은 오늘 종가로 계산했습니다.\n\n"
        "1. 삼성전자(005930): 수익률 +4.2%. 반도체 업황 회복 기대가 반영되며 최근 한 달 상승 흐름입니다.\n"
        f"{_SECOND_ITEM}. 방산 수출 수주가 이어집니다.\n"
    )
    middle = "".join(f"- 참고 {i}: 업종 흐름과 수급을 함께 보면 좋습니다.\n" for i in range(filler_lines))
    return f"{head}{middle}\n{_OFFER}"


def _request(*turns: tuple[Role, str]) -> ChatRequest:
    return ChatRequest(messages=[Message(role=role, content=content) for role, content in turns])


def _branch_query(request: ChatRequest, config: FeBranchConfig = _DEFAULTS) -> str:
    return _contextual_user_query(
        request,
        config.max_history_messages,
        config.history_message_max_chars,
        config.history_assistant_message_max_chars,
        config.history_transcript_max_chars,
        config.history_last_assistant_message_max_chars,
    )


def _history_block(query: str) -> str:
    return query.split("[Recent conversation - oldest to newest]\n", 1)[1].split("\n\n[Current user request]", 1)[0]


# ---------------------------------------------------------------------------
# truncate_middle
# ---------------------------------------------------------------------------

def test_truncate_middle_keeps_head_and_tail_within_budget():
    """뮤테이션: 앞부분만 남기면(truncate_end) 끝 문장이 사라져 red."""
    text = "가" * 300 + "끝문장"
    out = truncate_middle(text, 100)

    assert len(out) <= 100
    assert out.startswith("가")
    assert out.endswith("끝문장")
    assert "(중략)" in out


def test_truncate_middle_leaves_short_text_alone():
    assert truncate_middle("짧은 답", 100) == "짧은 답"


# ---------------------------------------------------------------------------
# fe_branch가 서브 에이전트에 넘기는 질문
# ---------------------------------------------------------------------------

def test_branch_query_keeps_list_item_and_trailing_offer_of_previous_answer():
    """직전 답의 앞쪽 목록(둘째 항목)과 끝의 추가 제안이 둘 다 서브 에이전트에 넘어간다.

    뮤테이션: 직전 답에도 180자 한도(truncate_end)를 쓰면 둘째 항목과 제안이 모두 빠져 red.
    직전 답에 truncate_end(1000)를 쓰면 제안이 빠져 red.
    """
    answer = _long_answer()
    assert len(answer) > _DEFAULTS.history_last_assistant_message_max_chars  # 가운데를 줄이는 경우다
    assert answer.index(_SECOND_ITEM) > _DEFAULTS.history_assistant_message_max_chars

    query = _branch_query(_request((Role.USER, "내 잔고 알려줘"), (Role.ASSISTANT, answer),
                                   (Role.USER, "그중 두 번째 종목은?")))

    assert _SECOND_ITEM in query
    assert _OFFER in query
    assert query.endswith("[Current user request]\n그중 두 번째 종목은?")
    previous_answer = _history_block(query).split("\nassistant: ", 1)[1].rsplit("\nuser: ", 1)[0]
    assert len(previous_answer) <= _DEFAULTS.history_last_assistant_message_max_chars


def test_older_answers_keep_the_short_cap():
    """직전 답보다 앞선 답은 종전대로 180자에서 자른다 — 늘어나는 입력은 직전 답 한 건뿐이다.

    뮤테이션: 모든 assistant 답에 직전 답 한도를 쓰면 오래된 답의 뒷부분 표지가 실려 red.
    """
    older = "예전 답입니다. " * 20 + "오래된답의뒷부분"
    query = _branch_query(_request(
        (Role.ASSISTANT, older),
        (Role.USER, "그럼 다른 종목은?"),
        (Role.ASSISTANT, _long_answer()),
        (Role.USER, "응"),
    ))

    assert "오래된답의뒷부분" not in query
    older_line = _history_block(query).split("\n", 1)[0]
    assert older_line.startswith("assistant: 예전 답입니다.")
    assert len(older_line) <= len("assistant: ") + _DEFAULTS.history_assistant_message_max_chars
    assert _OFFER in query


def test_transcript_cap_fits_default_window_with_a_full_previous_answer():
    """기본 4건이 각자 한도까지 차도 전체 상한에 밀려나지 않는다.

    뮤테이션: history_transcript_max_chars 기본값을 1200으로 되돌리면 가장 오래된 답이 밀려나 red.
    """
    query = _branch_query(_request(
        (Role.ASSISTANT, "오래된답머리" + "가" * 400),
        (Role.USER, "질문" * 200),
        (Role.ASSISTANT, _long_answer()),
        (Role.USER, "응"),
    ))

    assert "오래된답머리" in query


# ---------------------------------------------------------------------------
# supervisor 라우팅 입력
# ---------------------------------------------------------------------------

def test_supervisor_history_keeps_the_end_of_the_previous_answer_within_the_same_cap():
    """라우팅 입력은 한도를 늘리지 않고 직전 답의 끝(추가 제안)을 남긴다 — "응"의 대상이 보여야 한다.

    뮤테이션: 직전 답을 truncate_end로 자르면 제안이 빠져 red.
    """
    answer = _long_answer(filler_lines=60)
    assert len(answer) > 800
    history = _format_history(_request((Role.USER, "삼성전자 어때"), (Role.ASSISTANT, answer),
                                       (Role.USER, "응")).messages, 6, 800)

    assistant_line = history.split("\n")[1:-1]
    assert _OFFER in history
    assert "\n".join(assistant_line).startswith("assistant: 현재 계좌 잔고를 조회했습니다.")
    assert len("\n".join(assistant_line)) <= len("assistant: ") + 800


# ---------------------------------------------------------------------------
# 개인정보: 넓어진 창에도 이전 턴의 마스킹 본문만 실린다
# ---------------------------------------------------------------------------

_RAW_AMOUNT = "12,345,000원"


def _function(fn) -> MagicMock:
    mock = MagicMock()
    mock.ainvoke = AsyncMock(side_effect=fn)
    return mock


def _builder(function_mock: MagicMock) -> MagicMock:
    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=function_mock)
    return builder


@asynccontextmanager
async def _nomemory_chain(tmp_path, branch):
    """router_nomemory.yml 체인: trace_agent(PII 박스·역치환) → transcript_agent(SQLite) → 브랜치."""
    config = FinusSqliteTranscriptAgentConfig(inner_agent_name="router_supervisor_agent",
                                              db_path=str(tmp_path / "conversations.sqlite3"))
    async with finus_sqlite_transcript_agent(config, _builder(_function(branch))) as transcript:
        async with finus_reasoning_trace_agent(
            FinusReasoningTraceAgentConfig(inner_agent_name="transcript_router_agent"),
            _builder(_function(transcript.single_fn)),
        ) as trace:
            yield trace.single_fn


@pytest.mark.asyncio
async def test_previous_answer_reaches_the_branch_masked_not_restored(tmp_path):
    """잔고 답의 금액은 사용자에게는 원값으로 나가지만, 다음 턴 서브 에이전트 입력에는 자리표시자로 실린다.

    금액은 앞 180자 밖에 둔다 — 종전에는 잘려서 애초에 실리지 않던 구간이 이번 수정으로 실리게 되므로,
    그 구간이 마스킹된 본문인지를 본다. transcript는 최상위가 역치환하기 전의 응답을 저장한다.

    뮤테이션: transcript가 역치환한 본문(unmask_response)을 저장하게 바꾸면 원값이 실려 red.
    """
    seen: list[str] = []

    async def branch(request):
        chat_request = GlobalTypeConverter.get().convert(request, to_type=ChatRequest)
        if len(chat_request.messages) == 1:
            # 계좌 도구 결과처럼 마스킹하고 매핑을 요청 박스에 쌓는다(finus_api의 계좌 도구와 같은 경로).
            observation = mask_tool_result("finus_mcp_trading_get_balance", f"총 평가금액 {_RAW_AMOUNT}")
            answer = "잔고를 조회했습니다. " + "보유 종목 요약입니다. " * 15 + observation + "\n\n" + _OFFER
            return ChatResponse.from_string(answer, usage=Usage())
        seen.append(_branch_query(chat_request))
        return ChatResponse.from_string("두 번째 종목 설명입니다.", usage=Usage())

    async with _nomemory_chain(tmp_path, branch) as response_fn:
        first = await response_fn(ChatRequestOrMessage(messages=[{"role": "user", "content": "내 잔고 알려줘"}]))
        await response_fn(ChatRequestOrMessage(messages=[{"role": "user", "content": "응"}]))

    first_body = first.choices[0].message.content
    assert _RAW_AMOUNT in first_body  # 사용자 화면에는 원값
    assert first_body.index(_RAW_AMOUNT) > _DEFAULTS.history_assistant_message_max_chars
    (query,) = seen
    assert _RAW_AMOUNT not in query
    assert "12345000" not in query.replace(",", "")
    assert "<AMOUNT_" in query
    assert _OFFER in query
