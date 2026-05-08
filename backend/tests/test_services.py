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
