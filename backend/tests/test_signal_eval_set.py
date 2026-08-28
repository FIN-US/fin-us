"""#298 평가셋 스크립트(backend/scripts/build_signal_eval_set.py) 테스트.

사람 채점과 대조하는 것이 이 CSV의 유일한 목적이므로, "실패를 0점으로 적지 않는다"와
"헤더·인코딩이 사람이 열 수 있는 형태다"가 핵심 회귀 지점이다.
"""
import csv

import pytest

from backend.scripts import build_signal_eval_set as eval_set
from backend.services import SignalScore


def test_parse_article_line_splits_title_and_summary():
    article = eval_set.parse_article_line(
        "삼성전자",
        "삼성전자, 3분기 영업이익 10조 - 시장 기대치를 웃돌았다 | Mon, 18 Aug 2026 | https://n.example/1",
    )

    assert article == eval_set.Article(
        stock="삼성전자",
        title="삼성전자, 3분기 영업이익 10조",
        summary="시장 기대치를 웃돌았다",
    )


def test_parse_article_line_keeps_lines_without_separator():
    """형식이 어긋난 줄도 버리지 않는다 — 실제 입력 분포에 그런 줄이 섞여 있다."""
    article = eval_set.parse_article_line("삼성전자", "구분자 없는 제목")

    assert article == eval_set.Article(stock="삼성전자", title="구분자 없는 제목", summary="")


def test_parse_article_line_drops_blank_lines():
    assert eval_set.parse_article_line("삼성전자", "   ") is None


@pytest.mark.asyncio
async def test_collect_articles_dedupes_and_respects_count(monkeypatch):
    async def fake_run_mcp_tool(params, tool_name, arguments):
        stock = arguments["stock_name"]
        return f"{stock} 기사A - 설명\n공통 기사 - 설명\n{stock} 기사B - 설명"

    monkeypatch.setattr(eval_set, "run_mcp_tool", fake_run_mcp_tool)

    articles = await eval_set.collect_articles(["삼성전자", "SK하이닉스"], count=10)

    titles = [article.title for article in articles]
    assert titles.count("공통 기사") == 1  # 종목이 달라도 같은 제목은 한 번만
    assert len(articles) == 5

    limited = await eval_set.collect_articles(["삼성전자", "SK하이닉스"], count=2)
    assert len(limited) == 2


@pytest.mark.asyncio
async def test_collect_articles_skips_failing_stock(monkeypatch):
    """한 종목의 뉴스 수집이 실패해도 나머지 종목으로 계속 모아야 한다."""
    async def fake_run_mcp_tool(params, tool_name, arguments):
        if arguments["stock_name"] == "삼성전자":
            raise RuntimeError("뉴스 API 실패")
        return "정상 기사 - 설명"

    monkeypatch.setattr(eval_set, "run_mcp_tool", fake_run_mcp_tool)

    articles = await eval_set.collect_articles(["삼성전자", "SK하이닉스"], count=10)

    assert [article.title for article in articles] == ["정상 기사"]


@pytest.mark.asyncio
async def test_score_articles_leaves_model_score_blank_on_failure(monkeypatch):
    """채점 실패는 빈 칸이어야 한다. 0으로 적으면 '중립 판단'과 섞여 평가가 왜곡된다."""
    scores = iter(
        [
            SignalScore(2, "수주 공시", None, (2,), True),
            SignalScore(None, None, None, (), True),  # fail-open
            SignalScore(0, None, None, (0,), False),
        ]
    )

    async def fake_score_signal(stock, signal_text, *args, **kwargs):
        return next(scores)

    monkeypatch.setattr(eval_set, "score_signal", fake_score_signal)

    rows = await eval_set.score_articles(
        [
            eval_set.Article("삼성전자", "수주", "설명"),
            eval_set.Article("삼성전자", "실패", "설명"),
            eval_set.Article("삼성전자", "중립", "설명"),
        ],
        provider="ollama",
    )

    assert [row["모델점수"] for row in rows] == [2, "", 0]
    assert [row["모델근거"] for row in rows] == ["수주 공시", "", ""]
    # 사람 채점 열은 언제나 비어 있어야 한다 — 모델 점수를 미리 넣으면 채점자가 끌려간다.
    assert all(row["사람점수"] == "" for row in rows)


@pytest.mark.asyncio
async def test_score_articles_sends_title_and_summary_to_the_model(monkeypatch):
    seen = []

    async def fake_score_signal(stock, signal_text, *args, **kwargs):
        seen.append(signal_text)
        return SignalScore(1, "근거", None, (1,), False)

    monkeypatch.setattr(eval_set, "score_signal", fake_score_signal)

    await eval_set.score_articles(
        [
            eval_set.Article("삼성전자", "제목", "요약"),
            eval_set.Article("삼성전자", "요약 없는 제목", ""),
        ],
        provider="ollama",
    )

    assert seen == ["제목 - 요약", "요약 없는 제목"]


def test_write_csv_is_readable_with_the_expected_header(tmp_path):
    output = tmp_path / "eval.csv"
    rows = [
        {
            "종목": "삼성전자",
            "제목": "대형 수주",
            "본문요약": "설명",
            "모델점수": 3,
            "모델근거": "수주 공시",
            "사람점수": "",
        }
    ]

    eval_set.write_csv(rows, output)

    with output.open(encoding=eval_set.CSV_ENCODING, newline="") as handle:
        loaded = list(csv.DictReader(handle))

    assert list(loaded[0].keys()) == list(eval_set.CSV_HEADER)
    assert loaded[0]["모델점수"] == "3"
    assert loaded[0]["사람점수"] == ""


@pytest.mark.asyncio
async def test_resolve_stocks_prefers_explicit_list():
    assert await eval_set.resolve_stocks(["A", "B", "A"]) == ["A", "B"]
