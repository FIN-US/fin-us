"""외부 LLM 전송 경계(backend/pii_egress.py) 회귀 테스트 (#395).

세 층으로 본다.

1. ``prepare_egress`` 단위 — 개인 구간만 마스킹, 공개 구간 보존, 설정 계좌번호, 실패 시 차단,
   구간 사이 scope 충돌, 전후 비교 로그.
2. provider 경계 — ``llm_chat``이 OpenAI·Anthropic 구현에 넘기는 **실제 문자열**에서 계좌번호·
   금액·수량이 사라지고 공개 수치는 남는지. 차단되면 provider가 호출되지 않는지.
3. 호출부 — 모닝 브리핑·``/earnings``·signal 채점·종목 분석·주문 제안이 출처를 올바르게
   표시하는지. 프롬프트 조립 함수만 보면 "표시는 했는데 경계가 무시한다"는 회귀가 통과하므로,
   전부 진짜 ``llm_chat``/``request_proposal``을 태우고 provider 자리에서 받는다.
"""
from __future__ import annotations

import logging
import re

import pytest

from backend import order_assist, pii_egress, pii_mask, services
from backend.pii_egress import (
    EgressBlocked,
    Segment,
    personal,
    prepare_egress,
    public,
    render_unmasked,
)
from backend.pii_mask import unmask_pii
from backend.pii_registry import _SCOPED_PLACEHOLDER_RE
from backend.telegram_commands import TelegramCommandHandler
from backend.tests.test_balance_parser import REAL_BALANCE_TEXT
from backend.tests.test_telegram_commands import FakeNotifier

# 잔고 픽스처(mcp-trading formatBalanceReport 출력)에 실제로 들어 있는 개인 수치.
BALANCE_PRIVATE_VALUES = ("1,210,000원", "1,000,000원", "67,000원", "200,500원")

# 공개 데이터에 흔한 표기 — 전체 마스킹이면 전부 자리표시자가 되던 것들이다(실측).
# "1,234,567주"는 QTY로, 10자리 "1234567890"은 ACCOUNT로 오분류됐다.
PUBLIC_NEWS = "매출 79조987억원, 영업이익 1조2,345억원. 외국인 1,234,567주 순매수, 거래대금 1234567890"
PUBLIC_VALUES = ("79조987억원", "1조2,345억원", "1,234,567주", "1234567890")

CONFIGURED_ACCOUNT = "8765432101"  # CANO 87654321 + 상품코드 01


@pytest.fixture(autouse=True)
def _no_configured_account(monkeypatch):
    """개발자 셸의 실제 KIS_ACCOUNT_NO가 테스트 결과를 바꾸지 않게 기본은 비운다."""
    monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)


def _placeholders(text: str) -> list[str]:
    return [m.group(0) for m in _SCOPED_PLACEHOLDER_RE.finditer(text)]


# ---------------------------------------------------------------------------
# 1. prepare_egress 단위
# ---------------------------------------------------------------------------


class TestPersonalAndPublicSegments:
    def test_plain_string_is_fully_masked_like_before(self):
        """표시 없는 문자열은 개인이다 — #230의 전체 마스킹과 같은 결과여야 한다."""
        text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원, 예수금 1234567"

        outgoing, mapping = prepare_egress(text, label="test")

        for value in ("12345678-01", "3주", "12,345,000원", "1234567"):
            assert value not in outgoing
        assert unmask_pii(outgoing, mapping) == text

    def test_public_segment_numbers_are_sent_verbatim(self):
        outgoing, mapping = prepare_egress(
            ["[뉴스]\n", public(PUBLIC_NEWS), "\n[잔고]\n", personal(REAL_BALANCE_TEXT)],
            label="test",
        )

        for value in PUBLIC_VALUES:
            assert value in outgoing, f"공개 수치 {value!r}가 마스킹됐다"
        for value in BALANCE_PRIVATE_VALUES:
            assert value not in outgoing, f"잔고 수치 {value!r}가 그대로 나갔다"
        # 공개 구간은 자리표시자를 하나도 만들지 않는다 — 매핑의 원값에 공개 수치가 없다.
        assert not set(mapping.values()) & set(PUBLIC_VALUES)
        assert unmask_pii(outgoing, mapping) == render_unmasked(
            ["[뉴스]\n", public(PUBLIC_NEWS), "\n[잔고]\n", personal(REAL_BALANCE_TEXT)]
        )

    def test_bare_string_inside_a_sequence_is_personal(self):
        """정적 문구를 감싸지 않아도 되게 한 편의가 공개 취급으로 새지 않는다."""
        outgoing, _ = prepare_egress(["예수금 1,000,000원", public(" 매출 500억원")], label="test")

        assert "1,000,000원" not in outgoing
        assert "매출 500억원" in outgoing

    def test_every_personal_segment_gets_its_own_restorable_mapping(self):
        prompt = [personal("평가금액 1,210,000원"), " / ", personal("예수금 1,000,000원")]

        outgoing, mapping = prepare_egress(prompt, label="test")

        assert len(_placeholders(outgoing)) == 2
        assert unmask_pii(outgoing, mapping) == render_unmasked(prompt)


class TestConfiguredAccountNumber:
    def test_cano_alone_is_masked_although_the_regex_misses_it(self, monkeypatch):
        """``_ACCOUNT_RE``는 10자리만 본다 — 8자리 CANO 단독 표기는 정규식만으로는 나간다."""
        assert "87654321" in pii_mask.mask_pii("계좌 87654321 잔고 조회")[0]  # 전제: 정규식은 놓친다
        monkeypatch.setenv("KIS_ACCOUNT_NO", CONFIGURED_ACCOUNT)

        outgoing, mapping = prepare_egress("계좌 87654321 잔고 조회", label="test")

        assert "87654321" not in outgoing
        assert unmask_pii(outgoing, mapping) == "계좌 87654321 잔고 조회"

    @pytest.mark.parametrize("written", ["87654321-01", "8765432101", "87654321"])
    def test_account_is_masked_even_inside_a_public_segment(self, monkeypatch, written):
        monkeypatch.setenv("KIS_ACCOUNT_NO", "87654321-01")
        text = f"공시 원문에 섞인 {written} 번호"

        outgoing, mapping = prepare_egress([public(text)], label="test")

        assert "87654321" not in outgoing
        assert unmask_pii(outgoing, mapping) == text

    def test_longer_numbers_that_merely_contain_the_cano_are_left_alone(self, monkeypatch):
        monkeypatch.setenv("KIS_ACCOUNT_NO", CONFIGURED_ACCOUNT)
        text = "거래량 987654321 / 876543210"

        outgoing, _ = prepare_egress([public(text)], label="test")

        assert outgoing == text

    def test_malformed_setting_is_ignored_instead_of_blocking(self, monkeypatch):
        monkeypatch.setenv("KIS_ACCOUNT_NO", "your-account-no")

        outgoing, _ = prepare_egress([public(PUBLIC_NEWS)], label="test")

        assert outgoing == PUBLIC_NEWS


class TestFailSafeBlocksTheCall:
    def test_masking_exception_blocks_instead_of_sending_raw(self, monkeypatch):
        def broken_mask(text):
            raise RuntimeError("정규식 회귀")

        monkeypatch.setattr(pii_egress, "mask_pii", broken_mask)

        with pytest.raises(EgressBlocked) as exc_info:
            prepare_egress("예수금 1,000,000원", label="test")

        # 오류는 텔레그램·API 응답으로 나간다 — 프롬프트 내용을 싣지 않는다.
        assert "1,000,000" not in str(exc_info.value.detail)
        assert exc_info.value.status_code == 503

    def test_unknown_segment_type_blocks(self):
        with pytest.raises(EgressBlocked):
            prepare_egress(["정상 문구", 12345678], label="test")  # type: ignore[list-item]

    def test_scope_collision_between_segments_is_redrawn(self, monkeypatch):
        """두 구간이 같은 scope를 뽑으면 자리표시자 문자열까지 같아져 한쪽 원값이 덮인다."""
        # mask_pii는 자리표시자가 없는 구간(" / ")에서도 scope를 하나 뽑는다. 세 번째 뽑기가
        # 첫 구간과 겹쳐야 충돌이 실제로 일어난다.
        draws = iter(["abc123", "abc123", "abc123", "def456"])
        monkeypatch.setattr(pii_mask.secrets, "token_hex", lambda _n: next(draws))
        prompt = [personal("평가금액 1,210,000원"), " / ", personal("예수금 1,000,000원")]

        outgoing, mapping = prepare_egress(prompt, label="test")

        assert next(draws, None) is None, "충돌이 일어나지 않아 재추첨 경로를 타지 않았다"
        assert unmask_pii(outgoing, mapping) == render_unmasked(prompt)

    def test_account_scrub_redraws_when_its_scope_collides_with_a_segment(self, monkeypatch):
        """설정 계좌번호 가리기도 자기 scope를 뽑는다 — 앞 구간의 ACCOUNT 자리표시자와 겹칠 수 있다."""
        monkeypatch.setenv("KIS_ACCOUNT_NO", CONFIGURED_ACCOUNT)
        draws = iter(["abc123", "abc123", "def456"])
        monkeypatch.setattr(pii_mask.secrets, "token_hex", lambda _n: next(draws))
        prompt = [personal("다른 계좌 12345678-01"), public(" 공시 87654321")]

        outgoing, mapping = prepare_egress(prompt, label="test")

        assert next(draws, None) is None, "충돌이 일어나지 않아 재추첨 경로를 타지 않았다"
        assert "87654321" not in outgoing and "12345678-01" not in outgoing
        assert unmask_pii(outgoing, mapping) == render_unmasked(prompt)

    def test_scope_that_keeps_colliding_blocks(self, monkeypatch):
        monkeypatch.setattr(pii_mask.secrets, "token_hex", lambda _n: "abc123")

        with pytest.raises(EgressBlocked):
            prepare_egress(
                [personal("평가금액 1,210,000원"), personal("예수금 1,000,000원")], label="test"
            )


class TestComparisonLog:
    def test_debug_log_shows_before_and_after(self, caplog, monkeypatch):
        monkeypatch.setattr(pii_egress, "PII_EGRESS_DEBUG_LOG", True)
        caplog.set_level(logging.DEBUG, logger="backend.pii_egress")

        outgoing, _ = prepare_egress(
            ["잔고 ", personal("예수금 1,000,000원"), " 뉴스 ", public("매출 500억원")],
            label="llm_chat:openai",
        )

        messages = [record.getMessage() for record in caplog.records]
        before = next(m for m in messages if "마스킹 전" in m)
        after = next(m for m in messages if "마스킹 후" in m)
        assert "llm_chat:openai" in before and "llm_chat:openai" in after
        assert "예수금 1,000,000원" in before
        assert "1,000,000원" not in after and outgoing in after
        summary = next(m for m in messages if "구간 4개(공개 1)" in m)
        assert "AMOUNT" in summary

    def test_debug_level_alone_does_not_log_plaintext_without_the_flag(self, caplog, monkeypatch):
        """디버깅하려고 루트 레벨만 DEBUG로 올려도 평문 잔고가 로그에 남지 않는다 (PR #404 리뷰)."""
        monkeypatch.setattr(pii_egress, "PII_EGRESS_DEBUG_LOG", False)
        caplog.set_level(logging.DEBUG)  # 루트 로거
        caplog.set_level(logging.DEBUG, logger="backend.pii_egress")

        prepare_egress("예수금 1,000,000원", label="test")

        assert "1,000,000원" not in caplog.text
        assert not [r for r in caplog.records if r.name == "backend.pii_egress"]

    def test_nothing_is_logged_above_debug(self, caplog, monkeypatch):
        monkeypatch.setattr(pii_egress, "PII_EGRESS_DEBUG_LOG", True)
        caplog.set_level(logging.INFO, logger="backend.pii_egress")

        prepare_egress("예수금 1,000,000원", label="test")

        assert not [r for r in caplog.records if r.name == "backend.pii_egress"]


# ---------------------------------------------------------------------------
# 2. provider 경계 — llm_chat이 외부 구현에 실제로 넘기는 문자열
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_providers(monkeypatch):
    sent: dict[str, list[str]] = {"openai": [], "anthropic": [], "nat": []}

    async def fake_openai(user_msg):
        sent["openai"].append(user_msg)
        return "ok"

    async def fake_anthropic(user_msg):
        sent["anthropic"].append(user_msg)
        return "ok"

    async def fake_nat(user_msg, *, conversation_id=None):
        sent["nat"].append(user_msg)
        return services.NatAnswer("ok")

    monkeypatch.setattr(services, "_llm_openai_chat", fake_openai)
    monkeypatch.setattr(services, "_llm_anthropic_chat", fake_anthropic)
    monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_external_provider_receives_masked_account_and_amounts(
    captured_providers, monkeypatch, provider
):
    monkeypatch.setenv("KIS_ACCOUNT_NO", CONFIGURED_ACCOUNT)
    prompt = [
        "계좌 87654321-01, 보조 계좌 표기 87654321\n",
        personal(REAL_BALANCE_TEXT),
        "\n[뉴스]\n",
        public(PUBLIC_NEWS),
    ]

    await services.llm_chat(provider, prompt)

    (sent,) = captured_providers[provider]
    assert "87654321" not in sent
    assert re.search(r"(?<!\d)\d{8}-?\d{2}(?!\d)", sent.replace("1234567890", "")) is None
    for value in BALANCE_PRIVATE_VALUES + ("3주", "1주"):
        assert value not in sent, f"{provider}로 {value!r}가 나갔다"
    for value in PUBLIC_VALUES:
        assert value in sent, f"{provider}로 가는 공개 수치 {value!r}가 마스킹됐다"


@pytest.mark.asyncio
async def test_blocked_masking_never_reaches_the_provider(captured_providers, monkeypatch):
    def broken_mask(text):
        raise RuntimeError("정규식 회귀")

    monkeypatch.setattr(pii_egress, "mask_pii", broken_mask)

    with pytest.raises(EgressBlocked):
        await services.llm_chat("openai", "예수금 1,000,000원")

    assert captured_providers["openai"] == []


@pytest.mark.asyncio
async def test_response_placeholders_from_personal_segments_are_restored(monkeypatch):
    async def echo_openai(user_msg):
        return f"받은 값: {_placeholders(user_msg)[0]}"

    monkeypatch.setattr(services, "_llm_openai_chat", echo_openai)

    answer = await services.llm_chat("openai", [personal("예수금 1,000,000원"), public(" 매출 500억원")])

    assert answer == "받은 값: 1,000,000원"


# ---------------------------------------------------------------------------
# 3. 호출부 — 출처 표시가 실제 전송까지 이어지는지
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_morning_briefing_masks_balance_but_keeps_news_and_flows(
    captured_providers, monkeypatch
):
    async def fake_run_mcp_tool(params, tool_name, arguments):
        if tool_name == "get_balance":
            return REAL_BALANCE_TEXT
        if tool_name == "get_market_news":
            return f"{arguments['stock_name']} {PUBLIC_NEWS}"
        if tool_name == "get_investor_trading":
            return "기관 2,500주 순매도, 외국인 12,000주 순매수"
        raise AssertionError(tool_name)

    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.generate_morning_briefing(["삼성전자"])

    (sent,) = captured_providers["nat"]
    for value in BALANCE_PRIVATE_VALUES:
        assert value not in sent, f"모닝 브리핑 잔고 {value!r}가 그대로 나갔다"
    for value in PUBLIC_VALUES + ("2,500주", "12,000주"):
        assert value in sent, f"모닝 브리핑 공개 수치 {value!r}가 마스킹됐다"


@pytest.mark.asyncio
async def test_morning_briefing_failure_text_is_not_trusted_as_public(
    captured_providers, monkeypatch
):
    """조회 실패 문구는 예외 메시지를 싣는다 — 출처가 공개 도구여도 마스킹한다."""

    async def fake_run_mcp_tool(params, tool_name, arguments):
        if tool_name == "get_market_news":
            raise RuntimeError("예수금 1,000,000원 조회 중 오류")
        return "없음"

    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.generate_morning_briefing([])

    (sent,) = captured_providers["nat"]
    assert "get_market_news 조회 실패" in sent
    assert "1,000,000원" not in sent


@pytest.mark.asyncio
async def test_earnings_command_sends_dart_figures_unmasked(captured_providers):
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "get_earnings_report":
            return "2분기 매출 79조987억원, 영업이익 10조4,439억원"
        return "뉴스: 반도체 수요 회복, 설비투자 53조원"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=services.llm_chat,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자"}})

    (sent,) = captured_providers["nat"]
    for value in ("79조987억원", "10조4,439억원", "53조원"):
        assert value in sent, f"/earnings 실적 수치 {value!r}가 마스킹됐다"


@pytest.mark.asyncio
async def test_score_signal_keeps_public_figures_and_masks_unmarked_text(
    captured_providers,
):
    await services.score_signal("삼성전자", public(PUBLIC_NEWS), source="news", provider="openai")
    await services.score_signal("삼성전자", PUBLIC_NEWS, source="news", provider="openai")

    marked, unmarked = captured_providers["openai"]
    for value in PUBLIC_VALUES:
        assert value in marked
        assert value not in unmarked


@pytest.mark.asyncio
async def test_stock_analysis_trigger_follows_the_callers_marking(captured_providers, monkeypatch):
    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    class Session:
        def add(self, _instance): ...
        def commit(self): ...
        def refresh(self, _instance): ...

    await services.perform_stock_analysis(
        "삼성전자", "anthropic", Session(), trigger_source="disclosure", trigger_signal=public(PUBLIC_NEWS)
    )
    await services.perform_stock_analysis(
        "삼성전자", "anthropic", Session(), trigger_source="disclosure", trigger_signal=PUBLIC_NEWS
    )

    marked, unmarked = captured_providers["anthropic"]
    for value in PUBLIC_VALUES:
        assert value in marked
        assert value not in unmarked


@pytest.mark.asyncio
async def test_monitored_public_source_reaches_scoring_and_analysis_as_public(monkeypatch):
    """스케줄러는 SignalSource.public_data에 따라 원문을 표시한다 — 기본값은 개인이다."""
    from backend import scheduler

    seen: list[tuple[str, object]] = []

    async def fake_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return f"{name}: 매출 500억원"

    async def fake_score_signal(stock, current, last, *, source, provider):
        seen.append(("score:" + source, current))
        return services.SignalScore(3, "근거", None, (3,), True)

    async def fake_analysis(stock, provider, session, **kwargs):
        seen.append(("analysis:" + kwargs["trigger_source"], kwargs["trigger_signal"]))
        return {"summary": "요약"}

    async def fake_alert(*_args, **_kwargs):
        return None

    async def fake_broadcast(_payload):
        return None

    sources = [
        scheduler.SignalSource(name="news", mcp_params=object(), tool_name="get_market_news", public_data=True),
        scheduler.SignalSource(name="custom", mcp_params=object(), tool_name="get_custom"),
    ]
    monkeypatch.setattr(scheduler, "run_mcp_tool", fake_run_mcp_tool)
    monkeypatch.setattr(scheduler, "score_signal", fake_score_signal)
    monkeypatch.setattr(scheduler, "perform_stock_analysis", fake_analysis)
    monkeypatch.setattr(scheduler, "_send_telegram_alert_if_needed", fake_alert)
    monkeypatch.setattr(scheduler.manager, "broadcast", fake_broadcast)

    for source in sources:
        await scheduler._monitor_signal("삼성전자", source, object(), None)  # type: ignore[arg-type]

    assert seen == [
        ("score:news", public("get_market_news: 매출 500억원")),
        ("analysis:news", public("get_market_news: 매출 500억원")),
        ("score:custom", personal("get_custom: 매출 500억원")),
        ("analysis:custom", personal("get_custom: 매출 500억원")),
    ]


def test_production_signal_sources_are_the_public_feeds():
    """운영 감시 소스가 공개 표시를 잃으면 채점·분석 프롬프트의 뉴스 수치가 다시 마스킹된다."""
    from backend import scheduler

    assert {(s.tool_name, s.public_data) for s in scheduler.SIGNAL_SOURCES} == {
        ("get_market_news", True),
        ("get_disclosure_signal", True),
    }


@pytest.mark.asyncio
async def test_order_proposal_request_goes_through_the_egress_boundary(monkeypatch):
    """``/v1/propose-order``는 llm_chat을 지나지 않는 유일한 backend→LLM 전송이다."""
    monkeypatch.setenv("KIS_ACCOUNT_NO", CONFIGURED_ACCOUNT)
    posted: list[dict] = []

    async def fake_post(path, payload, timeout):
        posted.append(payload)
        token = _placeholders(payload["input_message"])[0]
        return {"value": f"확인한 값 {token}"}

    monkeypatch.setattr(order_assist, "_post_json", fake_post)

    answer = await order_assist.request_proposal("종목: 삼성전자 87654321-01 예수금 1,000,000원")

    (payload,) = posted
    assert "87654321" not in payload["input_message"]
    assert "1,000,000원" not in payload["input_message"]
    assert answer == "확인한 값 87654321-01"


@pytest.mark.asyncio
async def test_order_proposal_is_not_posted_when_masking_fails(monkeypatch):
    posted: list[dict] = []

    async def fake_post(path, payload, timeout):
        posted.append(payload)
        return {"value": "{}"}

    def broken_mask(text):
        raise RuntimeError("정규식 회귀")

    monkeypatch.setattr(order_assist, "_post_json", fake_post)
    monkeypatch.setattr(pii_egress, "mask_pii", broken_mask)

    with pytest.raises(EgressBlocked):
        await order_assist.request_proposal("종목: 삼성전자")

    assert posted == []


def test_segment_is_a_plain_value_object():
    assert public("a") == Segment("a", public=True)
    assert personal("a") == Segment("a")
