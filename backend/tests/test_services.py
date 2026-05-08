import pytest


class FakeSession:
    def add(self, report):
        self.report = report

    def commit(self):
        pass

    def refresh(self, report):
        pass


@pytest.mark.asyncio
async def test_check_signal_significance_uses_generic_source_prompt(monkeypatch):
    from .. import services

    prompts = []

    async def fake_llm_chat(provider, prompt):
        prompts.append((provider, prompt))
        return "YES"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)

    result = await services.check_signal_significance(
        "삼성전자",
        "긍정 SNS 언급 급증",
        source="sns",
        provider="ollama",
    )

    assert result is True
    assert prompts[0][0] == "ollama"
    assert "signal 출처: sns" in prompts[0][1]
    assert "뉴스 내용" not in prompts[0][1]


@pytest.mark.asyncio
async def test_perform_stock_analysis_includes_trigger_signal(monkeypatch):
    from .. import services

    prompts = []

    async def fake_llm_chat(provider, prompt):
        prompts.append((provider, prompt))
        return "plain analysis"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)

    await services.perform_stock_analysis(
        "삼성전자",
        "nat",
        FakeSession(),
        trigger_source="sns",
        trigger_signal="SNS mentions spiked after earnings guidance",
    )

    assert prompts[0][0] == "nat"
    assert "분석 트리거 데이터 출처: sns" in prompts[0][1]
    assert "SNS mentions spiked after earnings guidance" in prompts[0][1]
    assert '"source_signals"' in prompts[0][1]


def test_analysis_from_nat_text_backfills_source_signals():
    from ..services import analysis_from_nat_text

    data = analysis_from_nat_text(
        (
            '{"summary":"요약",'
            '"details":{"decision":"HOLD","confidence_score":0.5,'
            '"reason":"근거","target_stock":"삼성전자"},'
            '"source_news":["기존 뉴스"],'
            '"trading_trend":null}'
        ),
        "삼성전자",
    )

    assert data["source_news"] == ["기존 뉴스"]
    assert data["source_signals"] == ["기존 뉴스"]
