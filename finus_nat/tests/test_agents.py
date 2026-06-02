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
