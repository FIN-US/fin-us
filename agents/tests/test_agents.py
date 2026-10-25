import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from nat.data_models.api_server import ChatRequestOrMessage, ChatResponse, Usage

from finus_agents import agents
from finus_agents.finus_api import REASONING_TRACE, ReasoningTrace

_BRANCHES = [
    agents.SupervisorBranch(name="trading_agent", function_name="trading_branch_agent", description="조회"),
    agents.SupervisorBranch(name="news_agent", function_name="news_branch_agent", description="뉴스"),
    agents.SupervisorBranch(name="diary_agent", function_name="diary_branch_agent", description="일지"),
]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("news_agent", "news_agent"),
        ("  News_Agent \n", "news_agent"),
        ("`TRADING_AGENT`", "trading_agent"),
        ("news_branch_agent", "news_agent"),
        ("브랜치: diary_agent.", "diary_agent"),
    ],
    ids=["exact", "case_and_whitespace", "backticks_upper", "function_name", "embedded_one"],
)
def test_normalize_branch_reads_one_allowed_name(raw, expected):
    assert agents._normalize_branch(raw, _BRANCHES) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "portfolio_agent", "trading", "뉴스", "news_agent 또는 trading_agent"],
    ids=["empty", "blank", "outside_list", "partial_name", "korean", "two_names"],
)
def test_normalize_branch_returns_none_unless_exactly_one(raw):
    """두 이름이 함께 나오면 고르지 않는다 — 예전에는 설정 순서상 앞선 trading_agent로 기울었다.

    뮤테이션: 포함 검사를 종전의 "첫 일치 반환"으로 되돌리면 two_names가 red.
    """
    assert agents._normalize_branch(raw, _BRANCHES) is None


def test_fallback_branch_must_be_one_of_the_branches():
    with pytest.raises(ValueError, match="fallback_branch"):
        agents.FinusSupervisorAgentConfig(llm_name="router_llm", branches=_BRANCHES, fallback_branch="strategy_agent")


async def _run_supervisor(router_output: str, *, fallback_branch: str | None):
    """가짜 라우터 LLM이 *router_output*을 낼 때 어느 브랜치가 불렸는지 돌려준다."""
    config = agents.FinusSupervisorAgentConfig(
        llm_name="router_llm", branches=_BRANCHES, fallback_branch=fallback_branch,
    )
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content=router_output))
    called: list[str] = []

    def branch(name: str) -> MagicMock:
        async def ainvoke(_request):
            called.append(name)
            return ChatResponse.from_string(f"{name} 답변", usage=Usage())

        fn = MagicMock()
        fn.ainvoke = AsyncMock(side_effect=ainvoke)
        return fn

    builder = MagicMock()
    builder.get_llm = AsyncMock(return_value=llm)
    builder.get_function = AsyncMock(side_effect=lambda fn_name: branch(str(fn_name).replace("_branch", "")))

    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        async with agents.finus_supervisor_agent(config, builder) as info:
            await info.single_fn(ChatRequestOrMessage(messages=[{"role": "user", "content": "요즘 어때?"}]))
    finally:
        REASONING_TRACE.reset(token)
    return called, trace


@pytest.mark.parametrize(
    "router_output",
    ["portfolio_agent", "", "news_agent 또는 trading_agent", "잘 모르겠습니다"],
    ids=["outside_list", "empty", "two_names", "sentence"],
)
async def test_supervisor_falls_back_when_branch_unreadable(router_output, caplog):
    """라우터 출력에서 이름을 못 읽으면 설정한 fallback 브랜치가 답하고 경고를 남긴다.

    예전에는 ValueError가 NAT 422 → backend "응답 생성 실패: {...}"로 사용자에게 나갔다.
    각주의 담당 에이전트도 실제로 답한 fallback 브랜치여야 한다.

    뮤테이션: fallback 분기를 지우고 raise만 남기면 red(ValueError).
    """
    with caplog.at_level(logging.WARNING, logger=agents.logger.name):
        called, trace = await _run_supervisor(router_output, fallback_branch="news_agent")

    assert called == ["news_agent"]
    assert trace.routed_agent == "news_agent"
    assert any("falling back to news_agent" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "router_output,expected",
    [("trading_agent", "trading_agent"), (" Diary_Agent\n", "diary_agent")],
    ids=["exact", "case_and_whitespace"],
)
async def test_supervisor_routes_readable_name_without_fallback(router_output, expected, caplog):
    """정상 이름은 fallback과 무관하게 그 브랜치로 간다 — 경고도 없다."""
    with caplog.at_level(logging.WARNING, logger=agents.logger.name):
        called, trace = await _run_supervisor(router_output, fallback_branch="news_agent")

    assert called == [expected]
    assert trace.routed_agent == expected
    assert not any("falling back" in r.getMessage() for r in caplog.records)


async def test_supervisor_without_fallback_still_raises():
    """fallback을 설정하지 않은 supervisor는 종전처럼 실패한다(아무 브랜치도 부르지 않는다)."""
    with pytest.raises(ValueError, match="exactly one allowed branch"):
        await _run_supervisor("portfolio_agent", fallback_branch=None)


def test_supervisor_rules_are_one_per_line():
    """supervisor 규칙은 한 줄에 하나다 — 줄이 붙으면 뒤 규칙이 앞 문장의 꼬리로 읽힌다.

    PR #388 리뷰: 첫 규칙 끝에 개행이 없어 "…추론하세요 - 가격, 거래량, …"으로 trading 라우팅
    규칙(#380에서 주문 요청 안내를 더한 규칙)이 앞 규칙에 붙어 있었다.

    뮤테이션: 첫 규칙 끝의 ``.\\n``을 공백으로 되돌리면 red.
    """
    branches = [
        agents.SupervisorBranch(name="trading_agent", function_name="trading_branch_agent", description="조회"),
    ]
    prompt = agents._supervisor_system_prompt(branches)

    rules = prompt.split("규칙:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert rules and all(rule.startswith("- ") for rule in rules)
    assert [rule for rule in rules if " - " in rule] == []
    assert any(rule.startswith("- 가격, 거래량") and "trading_agent" in rule for rule in rules)
