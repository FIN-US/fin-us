"""신호 점수화(#298) 평가셋을 CSV로 뽑는다.

뉴스 MCP로 기사를 모아 기사 하나씩 모델에 채점시키고, 사람 채점 열은 비워 둔
CSV를 만든다. 사람 채점은 이 스크립트가 하지 않는다 — 나중에 별도로 손으로 채운 뒤
모델점수와 대조해 임계값(SIGNAL_SCORE_THRESHOLD)과 프롬프트를 조정하는 것이 목적이다.

기사 하나씩 채점하는 이유: 운영 파이프라인은 기사 여러 건을 한 번에 넘겨 종합
점수를 받지만, 그 점수는 사람이 채점하기 어렵다("이 3건을 합치면 몇 점인가?"에는
정답이 없다). 기사 단위라면 사람과 모델이 같은 질문에 답하므로 비교가 성립한다.
운영 프롬프트와 같은 함수(services.score_signal)를 그대로 쓰므로 기준은 공유된다.

사용:
    python -m backend.scripts.build_signal_eval_set --count 30 --output eval.csv
    python -m backend.scripts.build_signal_eval_set --stocks 삼성전자 SK하이닉스

뉴스 MCP(mcp-news)가 네이버 API 키를 필요로 하므로 .env 설정이 되어 있어야 한다.
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlmodel import Session  # noqa: E402

from backend.config import NEWS_MCP_PARAMS  # noqa: E402
from backend.database import engine  # noqa: E402
from backend.scheduler import DEFAULT_MONITOR_STOCKS  # noqa: E402
from backend.services import run_mcp_tool, score_signal  # noqa: E402
from backend.watchlist_repo import SqliteWatchlistRepo  # noqa: E402

DEFAULT_COUNT = 30
DEFAULT_OUTPUT = "signal_eval_set.csv"
# 사람이 엑셀로 열어 채점할 파일이다. utf-8-sig가 아니면 한글이 깨진 채 열린다.
CSV_ENCODING = "utf-8-sig"
CSV_HEADER = ("종목", "제목", "본문요약", "모델점수", "모델근거", "사람점수")


class Article(NamedTuple):
    stock: str
    title: str
    summary: str


def parse_article_line(stock: str, line: str) -> Article | None:
    """mcp-news의 한 줄(``제목 - 설명 | 날짜 | 링크``)을 제목/본문요약으로 나눈다.

    형식이 어긋나면 줄 전체를 제목으로 두고 본문요약을 비운다 — 버리지 않는 이유는
    평가셋에 "형식이 이상한 기사"도 들어가야 모델의 실제 입력 분포를 닮기 때문이다.
    """
    text = line.strip()
    if not text:
        return None

    title, separator, rest = text.partition(" - ")
    if not separator:
        return Article(stock=stock, title=text, summary="")

    # 설명 뒤에 " | 날짜 | 링크"가 붙는다. 사람이 읽을 부분은 설명까지다.
    summary = rest.split(" | ")[0].strip()
    return Article(stock=stock, title=title.strip(), summary=summary)


async def collect_articles(stocks: list[str], count: int) -> list[Article]:
    """종목을 돌며 기사를 count건 모은다. 제목이 같은 기사는 한 번만 담는다."""
    articles: list[Article] = []
    seen_titles: set[str] = set()

    for stock in stocks:
        if len(articles) >= count:
            break
        try:
            raw = await run_mcp_tool(
                NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock}
            )
        except Exception as exc:  # noqa: BLE001 - 한 종목 실패로 수집 전체를 중단하지 않는다
            print(f"[skip] {stock}: 뉴스 수집 실패 — {exc}", file=sys.stderr)
            continue

        for line in str(raw).splitlines():
            article = parse_article_line(stock, line)
            if article is None or article.title in seen_titles:
                continue
            seen_titles.add(article.title)
            articles.append(article)
            if len(articles) >= count:
                break

    return articles


async def score_articles(articles: list[Article], provider: str) -> list[dict[str, Any]]:
    """기사별로 모델 점수를 매긴다. 채점에 실패한 기사는 점수 칸을 비운다.

    실패를 0점으로 적으면 평가셋 자체가 오염된다 — 사람 채점과 대조할 때 "모델이
    중립이라고 봤다"와 "모델이 답을 못 냈다"가 같은 행으로 섞여 정확도가 왜곡된다.
    """
    rows: list[dict[str, Any]] = []
    for index, article in enumerate(articles, start=1):
        signal_text = article.title
        if article.summary:
            signal_text = f"{article.title} - {article.summary}"

        scored = await score_signal(
            article.stock, signal_text, source="news", provider=provider
        )
        # 채점 실패(fail-open)는 is_significant=True에 score=None으로 온다.
        model_score = "" if scored.score is None else scored.score
        rows.append(
            {
                "종목": article.stock,
                "제목": article.title,
                "본문요약": article.summary,
                "모델점수": model_score,
                "모델근거": scored.reason or "",
                "사람점수": "",  # 나중에 손으로 채운다
            }
        )
        print(f"[{index}/{len(articles)}] {article.stock}: {model_score or '채점 실패'}")
    return rows


def write_csv(rows: list[dict[str, Any]], output: Path) -> None:
    with output.open("w", encoding=CSV_ENCODING, newline="") as handle:
        # CSV_HEADER가 리터럴 튜플이라 명시하지 않으면 DictWriter의 키 타입이
        # 그 리터럴 여섯 개로 추론돼, rows(dict[str, Any])를 받지 못한다.
        writer: csv.DictWriter[str] = csv.DictWriter(handle, fieldnames=list(CSV_HEADER))
        writer.writeheader()
        writer.writerows(rows)


async def resolve_stocks(explicit: list[str] | None) -> list[str]:
    """--stocks가 없으면 관심종목 + 기본 감시 종목을 쓴다 (순서 유지, 중복 제거)."""
    if explicit:
        return list(dict.fromkeys(explicit))

    try:
        watchlist = await SqliteWatchlistRepo(lambda: Session(engine)).get_watchlist()
    except Exception as exc:  # noqa: BLE001 - DB가 없어도 기본 종목으로는 돌아가야 한다
        print(f"[warn] 관심종목 조회 실패 — 기본 종목만 사용합니다: {exc}", file=sys.stderr)
        watchlist = []

    return list(dict.fromkeys([*watchlist, *DEFAULT_MONITOR_STOCKS]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="신호 점수화 평가셋(사람 채점용 CSV)을 만든다. (#298)",
    )
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="수집할 기사 수")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="CSV 출력 경로")
    parser.add_argument("--provider", default="ollama", help="채점에 쓸 경량 LLM provider")
    parser.add_argument("--stocks", nargs="*", help="수집 대상 종목 (미지정 시 관심종목+기본 종목)")
    return parser


async def main() -> int:
    args = build_parser().parse_args()

    stocks = await resolve_stocks(args.stocks)
    print(f"수집 대상 종목: {', '.join(stocks)}")

    articles = await collect_articles(stocks, args.count)
    if not articles:
        print("수집된 기사가 없습니다. 뉴스 MCP 설정(.env)을 확인하세요.", file=sys.stderr)
        return 1
    if len(articles) < args.count:
        # 조용히 적게 뽑고 끝내면 평가셋이 왜 30건이 아닌지 나중에 아무도 모른다.
        print(
            f"[warn] {args.count}건을 요청했지만 {len(articles)}건만 모았습니다 "
            "(종목당 기사 수 한도). --stocks로 종목을 늘리세요.",
            file=sys.stderr,
        )

    rows = await score_articles(articles, args.provider)
    output = Path(args.output)
    write_csv(rows, output)

    scored_count = sum(1 for row in rows if row["모델점수"] != "")
    print(f"{output} 에 {len(rows)}행을 썼습니다 (채점 성공 {scored_count}행).")
    print("'사람점수' 열은 비어 있습니다 — 손으로 채운 뒤 모델점수와 대조하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
