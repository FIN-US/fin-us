from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import Message
from nat.data_models.api_server import UserMessageContentRoleType

from nat_finus_nat import agents


def _user_request(text: str) -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(
                role=UserMessageContentRoleType.USER,
                content=text,
            )
        ]
    )


def test_earnings_analysis_request_does_not_use_holdings_news_shortcut():
    request = _user_request(
        "News Analyst 실적 분석 모드로 다음 종목의 구조화된 실적 리포트를 작성하라.\n"
        "종목: 삼성전자\n\n"
        "[최신 뉴스]\n"
        "삼성전자 최신 뉴스"
    )

    assert agents._is_holdings_news_request(request) is False


def test_holdings_news_request_still_uses_holdings_news_shortcut():
    request = _user_request("내 보유종목 삼성전자 최신 뉴스 알려줘")

    assert agents._is_holdings_news_request(request) is True


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
