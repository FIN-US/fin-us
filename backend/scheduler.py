import os
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select
from .catalyst_repo import CatalystEventInput, SqliteCatalystEventRepo
from .filtered_signal_repo import SqliteFilteredSignalRepo
from .ws_manager import manager
from .database import engine
from .config import (
    NEWS_MCP_PARAMS,
    TRADING_MCP_PARAMS,
    DART_MCP_PARAMS,
    FILTERED_SIGNAL_RETENTION_DAYS,
    SIGNAL_SCORE_THRESHOLD,
)
from .redis_state import RedisSchedulerState, signal_hash, redis_state
from .services import (
    SignalScore,
    perform_stock_analysis,
    run_mcp_tool,
    score_signal,
    generate_morning_briefing,
)
from .models import Portfolio
from .timeutil import KST
from .watchlist_repo import SqliteWatchlistRepo
from .presentation import (
    DEFAULT_TELEGRAM_USER_LEVEL,
    KIND_ALERT,
    KIND_BRIEFING,
    render,
)
from .telegram_notifier import telegram_notifier
from .telegram_notifier import should_send_telegram_alert

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler(timezone=KST)

# 뉴스 필터링에 사용할 모델 제공자 (ollama 또는 openai)
FILTER_PROVIDER = os.environ.get("NEWS_FILTER_PROVIDER", "ollama")

# 종목별 마지막으로 분석된 signal 내용을 저장하는 인메모리 캐시 (LLM 호출 최적화용)
# Redis 장애 시에만 사용하는 프로세스 로컬 fallback입니다.
last_analyzed_signal_cache = {}
last_analyzed_news_cache = last_analyzed_signal_cache

DEFAULT_MONITOR_STOCKS = [
    "삼성전자",
    "SK하이닉스",
    "현대차",
    "NAVER"
]


@dataclass(frozen=True)
class SignalSource:
    name: str
    mcp_params: Any
    tool_name: str
    stock_arg_name: str = "stock_name"


SIGNAL_SOURCES = [
    SignalSource(
        name="news",
        mcp_params=NEWS_MCP_PARAMS,
        tool_name="get_market_news",
    ),
    SignalSource(
        name="disclosure",
        mcp_params=DART_MCP_PARAMS,
        tool_name="get_disclosure_signal",
    ),
]

CATALYST_EVENT_LABELS = {
    "earnings": "실적",
    "dividend": "배당",
    "disclosure": "공시",
    "agm": "주주총회",
}


# get_balance 연속 실패 횟수. 감시 잡이 10분 주기로 상시 도는 탓에, 장애가 지속되면
# 동일한 에러 로그가 무한 반복돼 알림 피로를 낳고 정작 중요한 장애를 묻는다(#185).
# 이 카운터로 "첫 실패·원인 변경·1시간 주기"마다 error를 남기고 복구 시 집계 보고한다.
# 카운터는 프로세스마다 별개이고 정확도가 로그 억제에만 쓰이므로 동기화가 필요 없다.
# (worker가 여럿이면 worker당 첫 실패가 각각 error로 남지만, 그 정도 중복은 허용한다.)
_balance_failure_streak = 0
_last_balance_error: str | None = None

# 걸러진 신호 기록(#304)의 연속 실패 횟수. 위 잔고 카운터와 같은 문제를 같은 방식으로
# 막는다 — 다만 이쪽이 더 시끄럽다. 잔고는 한 주기에 한 번 실패하지만 기록은
# (종목 × 소스)마다 시도하므로, 억제가 없으면 DB 장애 한 번에 한 주기당 수십 건의
# 동일한 error가 쌓인다. 주기(6회)로 세면 여전히 주기마다 여러 번 남으므로 20회로
# 잡는다 — 종목 10개·소스 2개 기준으로 대략 한 주기에 한 번꼴이다.
# 카운터는 프로세스마다 별개이고 정확도가 로그 억제에만 쓰이므로 동기화가 필요 없다.
_filtered_signal_failure_streak = 0
_last_filtered_signal_error: str | None = None
# 첫 실패·원인 변경 이후로는 이 횟수마다 한 번만 error로 올린다.
_FILTERED_SIGNAL_ERROR_LOG_PERIOD = 20


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))


def _default_filtered_signal_repo() -> SqliteFilteredSignalRepo:
    return SqliteFilteredSignalRepo(lambda: Session(engine))


# "- 종목명 (코드) ..." 형식의 잔고 종목 줄을 검증하는 정규식.
# 줄에 "(코드)" 그룹이 없는 경우(예: "- 삼성전자 · 3주")를 계약 위반으로 거부한다.
# greedy .+가 마지막 " (코드)" 직전까지를 이름으로 잡으므로 이전의 rsplit("(", 1)과
# 동일하게 다중 괄호 이름(CJ4우(전환), 룩셈부르크코어오피스(파생형)(A))을 올바르게
# 처리한다. 단, 코드 자리 검증은 아래 문자 클래스로 rsplit보다 엄격해 "(합성)" 같은
# 비코드 괄호를 코드로 인정하지 않는 점에서 rsplit과 의도적으로 다르다(아래 설명 참고).
# ^- 가 "- " 접두사를 소비하므로 종목명 중간의 "- "(한국 - 전력)는 보존된다.
# "(코드)" 뒤의 구분자(· 또는 :)는 매칭 대상에 포함하지 않아 포맷 변형에 유연하게
# 대응한다.
#
# 코드 클래스를 [0-9A-Z]{6,}으로 한정하는 이유:
#   - stock_code.py의 _STOCK_CODE_EXTRACT_RE와 동일한 컨벤션. 다만 이 정규식 자체를
#     stock_code.py로 통합하지는 않았다 — 추출 대상 포맷이 다르다("이름 (코드, 시장)"이
#     아니라 "- 이름 (코드) · N주").
#   - 하한 6: KIS 표준 6자리 코드(005930 등). 상한 없음({6,}): ETN 7자리(389종목),
#     펀드 9자리(75종목 예: F70102B96)까지 포함.
#   - [^()]*를 쓰면 두 가지 문제가 생긴다:
#     1) 빈 코드 통과 — "- 삼성전자 () · 3주"의 빈 괄호가 코드 자리에 매치돼
#        (name="삼성전자", code="") 무효 줄이 살아남는다.
#     2) KODEX 오염 — "- KODEX 200 (합성) · 3주"에서 "(합성)"이 코드 자리에 들어가
#        이름이 "KODEX 200"으로 잘려 이슈 #157이 막으려던 오염이 재현된다.
STOCK_LINE_RE = re.compile(r"^- (?P<name>.+) \((?P<code>[0-9A-Z]{6,})\)")


def extract_stocks_from_balance(balance_text: str) -> list[str]:
    """mcp-trading의 get_balance 결과에서 종목명 리스트를 추출합니다.

    _parse_balance_holdings에 파싱을 위임하고 이름만 모아 반환합니다.
    형식 위반 줄 경고와 파싱 실패 경고는 _parse_balance_holdings가 담당합니다.

    [보유 종목 리스트] 섹션이 없으면 [] 반환(마커 부재 === 파싱 불가).
    잘림 안내 문구는 STOCK_LINE_RE에 매치되지 않아 파싱 단계에서 걸러집니다.

    현재는 테스트에서만 사용하며 프로덕션 경로는 _parse_balance_holdings를 직접 호출한다.
    """
    return [h.name for h in _parse_balance_holdings(balance_text)]


# mcp-trading/balance.js의 formatTruncationNote()가 잘림 시 항상 덧붙이는 문구 중,
# truncated 사유(max_pages/time_budget/no_cursor/repeated_cursor/error, 그리고
# 목록에 없는 사유의 fallback "연속조회가 완료되지 않아")에 관계없이 고정으로 남는
# 부분만 매칭한다:
#   `\n\n[안내] ${reason} 조회가 중단되어 일부 보유 종목이 위 목록에서 ...`
# ${reason}만 사유별로 바뀌고 앞뒤 리터럴은 고정이므로, 사유 문구 자체(예: "페이지
# 상한")를 매칭 대상에 넣지 않아도 모든 현재·미래 사유를 잡는다. 사유별 문구까지
# 매칭하면 새 사유가 추가될 때마다 이 목록도 함께 고쳐야 해서 더 잘 깨진다.
#
# 판별은 "조회가 중단되어" 하나로만 한다. "[안내]"는 mcp-trading/index.js:570의
# 모의투자 실현손익 안내와도 공유하는 표지라 get_balance 밖에서도 등장할 수 있어
# 판별력이 없고, 실제 discriminator는 잘림 문구에만 존재하는 이 접미사다. 고정할
# 리터럴이 하나로 줄어 아래 결합 테스트도 이 한 줄만 지키면 된다.
#
# get_balance는 MCP 텍스트 응답만 backend로 넘어오고 truncated 값 자체(구조화된
# 필드)는 MCP 경계를 넘어오지 않으므로, 이 함수는 문자열 매칭에 의존할 수밖에
# 없다. mcp-trading/balance.js의 리터럴이 바뀌면 이 매칭도 함께 깨진다 — 그
# 결합을 mcp-trading/tests/order.test.js의 잘림 노트 테스트가 `조회가 중단되어`를
# 직접 단언해 고정하고, backend/tests/test_balance_parser.py가 픽스처로 재현한다.
_BALANCE_TRUNCATION_MARKER = "조회가 중단되어"

# balance.js formatBalanceReport()가 보유 종목 섹션을 구분하는 헤더 리터럴.
# balance.js:273-274를 보면, 보유 종목이 0건이어도 이 마커와 "보유 종목이 없습니다."를
# 항상 출력한다. 따라서 이 마커가 없다는 것은 "보유 0건"이 아니라 "응답을 읽지 못함"이다.
# run_mcp_tool은 MCP content가 비면 예외 대신 빈 문자열을 반환하므로(services.py),
# 이 경로는 실재한다. _sync_portfolio_from_balance가 이 마커 부재를 계약 위반으로
# 처리해 전량 교체를 막는다.
_BALANCE_HOLDINGS_MARKER = "[보유 종목 리스트]"


def is_balance_truncated(balance_text: str) -> bool:
    """get_balance 응답에 mcp-trading/balance.js의 잘림 안내 문구가 포함되어 있는지 확인합니다.

    extract_stocks_from_balance()는 이 문구를 의도적으로 무시하므로(안내 문구가
    종목명으로 오인식되지 않도록), 잘림 여부는 이 함수로 별도 확인해야 한다.
    """
    return _BALANCE_TRUNCATION_MARKER in balance_text


# balance.js formatBalanceReport()의 종목 줄 형식에서 수량과 평단가를 추출하는 정규식.
#
# 수량(hldg_qty): "- 삼성전자 (005930) · 3주" 줄 끝의 "N주" 부분.
# 코드 닫는 괄호 직후 공백+중점+공백("· ")을 앵커로 사용한다. formatQuantity가
# toLocaleString("ko-KR")로 쉼표를 삽입하므로 "1,234주" 형태도 허용한다.
# · 는 U+00B7(MIDDLE DOT)로, balance.js 템플릿 리터럴에서 그대로 쓰는 문자다.
_QTY_RE = re.compile(r"\)\s*·\s*([\d,]+)주")

# 평단가(pchs_avg_pric): "  평단가 67,000원 → ..." 줄.
# formatAmount가 toLocaleString("ko-KR")로 쉼표를 삽입하고 "원"을 붙인다.
# 평단가는 매수 단가의 가중평균이라 정수가 아닌 것이 정상이다(예: "66,666.67원").
# mcp-trading/tests/balance.test.js가 "평단가 66,666.67원"을 리터럴로 단언하므로
# 소수점 허용은 가정이 아니라 계약이다. (?:\.\d+)?로 정수·소수 양쪽을 잡는다.
# "-원" 형태(parseNumber가 null을 반환하는 경우)는 [\d,]+에 매치되지 않아
# 기본값 0.0이 사용된다.
# _QTY_RE(수량)는 hldg_qty가 항상 정수이므로 소수점 허용이 필요 없다.
_AVG_PRICE_RE = re.compile(r"평단가\s+([\d,]+(?:\.\d+)?)원")


@dataclass(frozen=True)
class _BalanceHolding:
    """_parse_balance_holdings의 파싱 결과 단위."""
    code: str
    name: str
    quantity: int
    avg_price: float


def _parse_balance_holdings(balance_text: str) -> list[_BalanceHolding]:
    """get_balance 텍스트에서 보유 종목의 코드·이름·수량·평단가를 파싱합니다.

    mcp-trading/balance.js의 formatBalanceReport()가 생성하는 3줄 블록 형식에
    의존합니다:
      줄1: "- {prdt_name} ({pdno}) · {hldg_qty}주"
      줄2: "  평단가 {pchs_avg_pric}원 → 평가금액 {evlu_amt}원"
      줄3: "  손익 ... · 수익률 ..."

    current_price는 이 포맷에 포함되지 않아 파싱하지 않습니다.
    inquire-balance(TTTC8434R) output1에 현재가 필드가 없기 때문입니다
    (이슈 #122 후속 이슈 참고).

    잘림 가드와 마커 부재 가드는 호출처(_sync_portfolio_from_balance)가 담당하므로
    이 함수는 있는 그대로 파싱합니다. _BALANCE_HOLDINGS_MARKER가 없으면 [] 반환.

    형식 위반 줄(코드 괄호 누락 등)은 모아서 호출당 1회 경고 로그를 남깁니다.
    수량·평단가 파싱이 실패한 줄도 경고를 남기고 기본값(0)을 사용합니다.
    extract_stocks_from_balance는 이 함수에 파싱을 위임합니다.
    """
    holdings: list[_BalanceHolding] = []
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        return holdings

    section = balance_text.split(_BALANCE_HOLDINGS_MARKER)[1]
    lines = section.split("\n")
    malformed: list[str] = []

    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue

        match = STOCK_LINE_RE.match(line)
        if match is None:
            malformed.append(line)
            continue

        name = match.group("name").strip()
        code = match.group("code")
        if not name:
            continue

        # 수량: "- 삼성전자 (005930) · 3주" → 3
        qty_match = _QTY_RE.search(line)
        quantity = 0
        if qty_match:
            try:
                quantity = int(qty_match.group(1).replace(",", ""))
            except ValueError:
                logger.warning("[%s] 수량 파싱 실패(ValueError). quantity=0으로 저장합니다: %r", name, line)
        else:
            logger.warning("[%s] 수량 정규식이 매치되지 않아 quantity=0으로 저장합니다: %r", name, line)

        # 평단가: 다음 줄 "  평단가 67,000원 → ..." → 67000.0 (소수 포함)
        avg_price = 0.0
        if i + 1 < len(lines):
            avg_match = _AVG_PRICE_RE.search(lines[i + 1])
            if avg_match:
                try:
                    avg_price = float(avg_match.group(1).replace(",", ""))
                except ValueError:
                    logger.warning(
                        "[%s] 평단가 파싱 실패(ValueError). avg_price=0으로 저장합니다: %r",
                        name, lines[i + 1],
                    )
            else:
                logger.warning(
                    "[%s] 평단가 정규식이 매치되지 않아 avg_price=0으로 저장합니다: %r",
                    name, lines[i + 1] if i + 1 < len(lines) else "(다음 줄 없음)",
                )

        holdings.append(_BalanceHolding(code=code, name=name, quantity=quantity, avg_price=avg_price))

    if malformed:
        logger.warning(
            "예상치 못한 잔고 종목 줄 형식 %d건을 건너뛰었습니다(예시 최대 3건): %r",
            len(malformed), malformed[:3],
        )
    return holdings


def _sync_portfolio_from_balance(
    balance_text: str,
    session: Session,
    *,
    holdings: list[_BalanceHolding] | None = None,
) -> int | None:
    """get_balance 응답을 바탕으로 Portfolio 테이블을 동기화합니다.

    전략: 전량 교체 (기존 행 전체 삭제 후 신규 삽입). 매 잔고 조회마다 실행되어
    보유하지 않게 된 종목을 자동으로 제거합니다.

    잔고가 잘린 경우(is_balance_truncated) 전량 교체를 수행하면 아직 파악되지 않은
    보유 종목이 삭제될 수 있으므로, 동기화를 건너뛰고 기존 데이터를 보존합니다.

    current_price는 get_balance(inquire-balance, TTTC8434R) output1에 현재가 필드가
    없어 항상 null로 둡니다. null 수익률이 "실제 0%"와 혼동되지 않도록
    /api/v1/portfolio 응답에서 return_rate: null로 구분됩니다(이슈 #122).
    current_price 확보 방안은 후속 이슈를 참고하세요.

    holdings: 호출처에서 이미 _parse_balance_holdings를 실행한 경우 그 결과를 전달하면
    재파싱을 건너뜁니다. None이면 balance_text에서 직접 파싱합니다.
    잘림·마커 가드는 holdings 인자 유무에 관계없이 항상 balance_text를 검사합니다.

    반환값(이슈 #229): 동기화를 실제로 수행했으면 삽입한 종목 수(int, 0 이상)를,
    잘림·마커 부재로 건너뛰었으면 None을 반환합니다. bool 대신 개수를 반환하는 이유는
    호출처(monitor_market_task)가 WebSocket으로 PORTFOLIO_UPDATE를 브로드캐스트할 때
    holdings_count를 함께 실어야 하는데, bool만으로는 호출처가 그 값을 얻기 위해
    holdings 리스트를 별도로 들고 있거나 다시 세야 하기 때문입니다. int 반환값 자체가
    "동기화 성공 여부"와 "성공 시 종목 수"를 한 번에 전달합니다.
    """
    if is_balance_truncated(balance_text):
        logger.warning(
            "잔고 연속조회가 잘려 Portfolio 동기화를 건너뜁니다. 기존 데이터를 유지합니다."
        )
        return None

    # 마커가 없으면 "보유 0건"이 아니라 "응답을 읽지 못함"이다.
    # balance.js는 보유 종목이 없어도 마커와 "보유 종목이 없습니다."를 항상 출력한다
    # (balance.js:273-274). run_mcp_tool은 MCP content가 비면 예외 대신 ""를
    # 반환하므로(services.py) 이 경로는 실재한다. 전량 교체는 되돌릴 수 없으므로
    # 기존 데이터를 보존하고 error 로그로 운영자에게 알린다.
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        logger.error(
            "잔고 응답에서 %r 섹션을 찾지 못해 Portfolio 동기화를 건너뜁니다 "
            "(응답 길이 %d). 기존 데이터를 유지합니다.",
            _BALANCE_HOLDINGS_MARKER,
            len(balance_text),
        )
        return None


    # 호출처에서 이미 파싱한 결과가 있으면 재파싱을 건너뛴다(경고 중복 방지).
    if holdings is None:
        holdings = _parse_balance_holdings(balance_text)

    # 전량 교체: 기존 행 전체 삭제
    existing = session.exec(select(Portfolio)).all()
    for row in existing:
        session.delete(row)
    session.flush()

    # 신규 삽입
    now = datetime.now(timezone.utc)
    for h in holdings:
        session.add(Portfolio(
            stock_code=h.code,
            stock_name=h.name,
            quantity=h.quantity,
            avg_price=h.avg_price,
            current_price=None,
            updated_at=now,
        ))

    session.commit()
    logger.info("Portfolio 동기화 완료: %d개 종목", len(holdings))
    return len(holdings)


def _infer_catalyst_event_type(description: str) -> str:
    text = description.lower()
    if any(keyword in description for keyword in ("실적", "분기보고서", "반기보고서", "사업보고서")):
        return "earnings"
    if "배당" in description:
        return "dividend"
    if any(keyword in description for keyword in ("주주총회", "주총")):
        return "agm"
    if any(keyword in text for keyword in ("major stock", "executive")):
        return "disclosure"
    return "disclosure"


def _parse_catalyst_date(raw: str) -> date | None:
    compact_match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", raw)
    if compact_match is not None:
        year, month, day = compact_match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    dashed_match = re.search(r"\b(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\b", raw)
    if dashed_match is None:
        return None
    year, month, day = dashed_match.groups()
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _parse_disclosure_signal_events(stock_name: str, raw_signal: str) -> list[CatalystEventInput]:
    events: list[CatalystEventInput] = []
    for line in str(raw_signal).splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue

        event_date = _parse_catalyst_date(stripped)
        if event_date is None:
            continue

        parts = [part.strip(" -") for part in stripped.split("|")]
        description = next(
            (part for part in parts if part and _parse_catalyst_date(part) is None),
            stripped.lstrip("-").strip(),
        )
        events.append(
            CatalystEventInput(
                stock_name=stock_name,
                event_type=_infer_catalyst_event_type(description),
                event_date=event_date,
                description=description,
                source="dart",
            )
        )
    return events


def _format_catalyst_alert(event: Any) -> str:
    d_day = "D-Day" if event.days_until_event == 0 else f"D-{event.days_until_event}"
    label = CATALYST_EVENT_LABELS.get(event.event_type, event.event_type or "기타")
    return "\n".join(
        [
            f"📅 촉매 이벤트 알림 ({d_day})",
            f"- 종목: {event.stock_name}",
            f"- 날짜: {event.event_date.isoformat()}",
            f"- 유형: {label}",
            f"- 내용: {event.description}",
        ]
    )


async def _collect_catalyst_events(
    watchlist: list[str],
    catalyst_repo: SqliteCatalystEventRepo,
) -> None:
    for stock in watchlist:
        try:
            signal = await run_mcp_tool(
                DART_MCP_PARAMS,
                "get_disclosure_signal",
                {"stock_name": stock},
            )
            events = _parse_disclosure_signal_events(stock, str(signal))
            if events:
                await catalyst_repo.upsert_events(events)
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 수집 중 오류: %s", stock, e)


async def _telegram_user_level(state: Any = None) -> str:
    """설명 수준을 읽는다. 못 읽으면 기본값(초보) (#297 자가리뷰).

    브리핑·촉매 알림이 이 함수를 쓰지 않고 기본값을 그대로 넘기고 있었다. 그래서 /level
    중급으로 바꾼 사용자도 매 브리핑마다 용어 각주를 받았다 — 분석 알림만 수준을 읽고
    나머지는 안 읽는, 사용자 눈에는 설명할 수 없는 차이였다.

    ``state``가 있으면 그걸 쓰고(이미 열린 연결을 한 번 더 열 이유가 없다) 없으면 직접
    연다. 실패는 삼킨다 — 수준은 다른 메시지의 곁다리 정보이고, 못 읽었다고 브리핑 자체를
    거르면 부가 기능이 본 기능을 잡아먹는다 (telegram_commands._current_level과 같은 판단).
    """
    try:
        if state is not None:
            return await state.get_telegram_user_level()
        async with redis_state() as opened:
            return await opened.get_telegram_user_level()
    except Exception as exc:
        logger.warning("설명 수준을 읽지 못해 기본값을 씁니다: %s", exc)
        return DEFAULT_TELEGRAM_USER_LEVEL


async def _send_due_catalyst_alerts(
    watchlist: list[str],
    catalyst_repo: SqliteCatalystEventRepo,
    *,
    notifier: Any,
    today: date,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
) -> None:
    try:
        due_events = await catalyst_repo.list_due_for_notification(watchlist, today=today)
    except Exception as e:
        logger.error("촉매 이벤트 알림 대상 조회 중 오류: %s", e)
        return

    for event in due_events:
        try:
            sent = await notifier.send_text(
                render(_format_catalyst_alert(event), KIND_ALERT, level)
            )
            if sent is True:
                await catalyst_repo.mark_notification_sent(
                    event.id,
                    days_until_event=event.days_until_event,
                )
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 Telegram 알림 중 오류: %s", event.stock_name, e)


async def catalyst_calendar_task(
    *,
    watchlist_repo: SqliteWatchlistRepo | None = None,
    catalyst_repo: SqliteCatalystEventRepo | None = None,
    notifier: Any = telegram_notifier,
    today_factory: Callable[[], date] | None = None,
    use_redis_lock: bool = True,
    level: str | None = None,
) -> None:
    """예정 촉매를 모으고 임박한 것을 알린다.

    ``level``은 설명 수준이다. redis 잠금을 쓰는 바깥 호출이 이미 열린 연결로 읽어 안쪽
    호출에 넘긴다 — "redis를 쓰지 않는다"고 한 경로(use_redis_lock=False)가 수준을 읽으려고
    redis를 여는 것은 앞뒤가 맞지 않는다. 그 경로는 기본값으로 간다 (#297 자가리뷰).
    """
    if use_redis_lock:
        try:
            async with redis_state() as state:
                scheduler_token = await state.acquire_scheduler_lock("catalyst_calendar")
                if scheduler_token is None:
                    logger.info("다른 worker가 촉매 이벤트 캘린더를 실행 중입니다. 이번 실행을 건너뜁니다.")
                    return
                try:
                    await catalyst_calendar_task(
                        watchlist_repo=watchlist_repo,
                        catalyst_repo=catalyst_repo,
                        notifier=notifier,
                        today_factory=today_factory,
                        use_redis_lock=False,
                        level=await _telegram_user_level(state),
                    )
                finally:
                    await state.release_lock(
                        state.keys.scheduler_lock("catalyst_calendar"),
                        scheduler_token,
                    )
        except Exception as e:
            logger.error("Redis 기반 촉매 이벤트 캘린더 실행 중 오류: %s", e)
        return

    if watchlist_repo is None:
        watchlist_repo = _default_watchlist_repo()
    if catalyst_repo is None:
        catalyst_repo = _default_catalyst_repo()
    today = today_factory() if today_factory is not None else datetime.now(scheduler.timezone).date()

    try:
        watchlist = await watchlist_repo.get_watchlist()
    except Exception as e:
        logger.error("촉매 이벤트 관심 종목 조회 중 오류: %s", e)
        return

    if not watchlist:
        logger.info("관심 종목이 없어 촉매 이벤트 캘린더를 건너뜁니다.")
        return

    await _collect_catalyst_events(watchlist, catalyst_repo)
    await _send_due_catalyst_alerts(
        watchlist,
        catalyst_repo,
        notifier=notifier,
        today=today,
        level=level or DEFAULT_TELEGRAM_USER_LEVEL,
    )

async def monitor_market_task(
    watchlist_repo: SqliteWatchlistRepo | None = None,
    filtered_signal_repo: SqliteFilteredSignalRepo | None = None,
):
    """
    주기적으로 시장 상황을 모니터링합니다.
    """
    fallback_to_memory = True
    try:
        async with redis_state() as state:
            scheduler_token = await state.acquire_scheduler_lock()
            if scheduler_token is None:
                logger.info("다른 worker가 시장 모니터링을 실행 중입니다. 이번 실행을 건너뜁니다.")
                return

            fallback_to_memory = False
            try:
                await _monitor_market_task(state, watchlist_repo, filtered_signal_repo)
            finally:
                try:
                    await state.release_lock(state.keys.scheduler_lock("market_monitoring"), scheduler_token)
                except Exception as e:
                    logger.error("시장 모니터링 Redis lock 해제 중 오류: %s", e)
    except Exception as e:
        if not fallback_to_memory:
            logger.error("Redis 기반 시장 모니터링 실행 중 오류: %s", e)
            return

        logger.warning(
            "Redis 스케줄러 상태를 사용할 수 없어 인메모리 fallback으로 시장 모니터링을 실행합니다: %s",
            e,
        )
        await _monitor_market_task(None, watchlist_repo, filtered_signal_repo)


async def _monitor_market_task(
    state: RedisSchedulerState | None,
    watchlist_repo: SqliteWatchlistRepo | None = None,
    filtered_signal_repo: SqliteFilteredSignalRepo | None = None,
):
    global _balance_failure_streak, _last_balance_error

    try:
        # 1. 실시간 잔고 조회 및 모니터링 대상 확정
        #
        # get_balance만 자체 try/except로 격리하는 이유(#185): 아래에서 도는
        # _monitor_signal의 신호는 SIGNAL_SOURCES(mcp-news/mcp-dart)에서 오고 KIS와
        # 아무 관련이 없는데, 이 호출이 태스크 전체 try의 첫 문장이라 KIS 장애 한 번이
        # 뉴스·공시 감시까지 통째로 정지시켰다. 관심 종목 조회(아래)가 이미 쓰는
        # fail-open 관용구를 그대로 적용해, 잔고를 못 읽으면 owned_stocks만 비우고
        # 관심 종목·기본 종목 감시는 계속 진행한다. 장애 중 보유 종목 감시 공백은
        # 어차피 조회 자체가 불가능하므로 불가피한 대가다.
        owned_stocks: list[str] = []
        balance_ok = False
        try:
            balance_text = await run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as e:
            signature = f"{type(e).__name__}:{e}"
            _balance_failure_streak += 1
            # 6회 = 약 1시간(10분 주기). 첫 실패는 즉시, 원인이 바뀌면 즉시, 이후는
            # 1시간에 한 번만 error로 올려 알림 피로를 피하면서도 "아직 장애 중"이라는
            # 신호가 끊기지 않게 한다.
            if (
                _balance_failure_streak == 1
                or signature != _last_balance_error
                or _balance_failure_streak % 6 == 0
            ):
                logger.error(
                    "잔고 조회에 실패해 이번 주기의 보유 종목 감시를 건너뜁니다 "
                    "(관심 종목·기본 종목 감시는 계속, %d회 연속): %s",
                    _balance_failure_streak,
                    e,
                    exc_info=True,
                )
            else:
                # 동일 원인의 반복 실패는 debug로 내려 알림 피로를 방지한다.
                logger.debug(
                    "잔고 조회 실패가 %d회 연속됩니다: %s", _balance_failure_streak, e
                )
            _last_balance_error = signature
        else:
            balance_ok = True
            if _balance_failure_streak:
                logger.warning(
                    "잔고 조회가 복구되었습니다. 직전까지 %d회 연속 실패해 보유 종목 감시를 건너뛰었습니다.",
                    _balance_failure_streak,
                )
                _balance_failure_streak = 0
                _last_balance_error = None

            # 같은 텍스트를 두 번 파싱하지 않도록 먼저 한 번만 파싱합니다.
            # extract_stocks_from_balance도 내부에서 _parse_balance_holdings를 호출하는데,
            # _sync_portfolio_from_balance가 같은 텍스트를 또 파싱하면 경고가 두 번씩
            # 찍히고 "저장합니다"라는 문구가 실제로 저장하지 않는 호출에서도 나옵니다.
            holdings = _parse_balance_holdings(balance_text)
            owned_stocks = [h.name for h in holdings]

            # Portfolio 테이블 동기화 — 잔고 조회 성공 시에만 실행.
            # 잘린 잔고에서 전량 교체하면 보유 종목이 사라지므로,
            # _sync_portfolio_from_balance가 내부에서 잘림을 감지해 건너뜁니다.
            # 동기화 실패는 감시 루프에 영향을 주지 않아야 하므로 예외를 격리합니다.
            try:
                with Session(engine) as sync_session:
                    sync_result = _sync_portfolio_from_balance(balance_text, sync_session, holdings=holdings)
                # 잘림·마커 부재로 동기화를 건너뛴 경우(None)에는 테이블이 그대로이므로
                # 브로드캐스트하지 않는다. 동기화가 수행된 경우 전량 교체 특성상 내용이
                # 직전과 동일해도 신호가 나간다 — 즉 정상 주기마다 1건이다.
                # payload에는 보유 종목 개수만 싣고 종목명·수량 등 실제 보유 내역은 담지
                # 않는다. WebSocket에는 인증이 없어(#266) 계좌 보유 현황이 인증 없는
                # 채널로 유출될 수 있으므로, 신호만 보내고 클라이언트가
                # /api/v1/db/portfolio를 재조회하도록 한다.
                if sync_result is not None:
                    try:
                        await manager.broadcast({
                            "type": "PORTFOLIO_UPDATE",
                            "holdings_count": sync_result,
                            "broadcast_at": datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception as e:
                        logger.error(
                            "Portfolio 업데이트 브로드캐스트 중 오류 (감시는 계속): %s",
                            e,
                            exc_info=True,
                        )
            except Exception as e:
                logger.error("Portfolio 동기화 중 오류 (감시는 계속): %s", e, exc_info=True)

            if is_balance_truncated(balance_text):
                # 잘림 사유(max_pages/time_budget/error/...)마다 운영 대응이 다르므로,
                # 안내 문구 줄을 그대로 실어 사유가 로그에 남게 한다. 사유 문자열을 따로
                # 파싱하지 않으므로 balance.js에 새 사유가 추가돼도 자동으로 따라간다.
                notice = next(
                    (
                        line
                        for line in balance_text.splitlines()
                        if _BALANCE_TRUNCATION_MARKER in line
                    ),
                    "",
                )
                logger.warning(
                    "잔고 연속조회가 잘려 감시 대상이 불완전할 수 있습니다: 보유 종목 %d건만 확보 — %s",
                    len(owned_stocks),
                    notice.strip(),
                )

        if watchlist_repo is None:
            watchlist_repo = SqliteWatchlistRepo(lambda: Session(engine))
        try:
            watchlist = await watchlist_repo.get_watchlist()
        except Exception as e:
            logger.error("관심 종목 조회 중 오류: %s", e)
            watchlist = []

        # 보유 종목 + 관심 종목 합산 (순서 유지, 중복 제거)
        seen: set[str] = set()
        stocks_to_monitor: list[str] = []
        for stock in owned_stocks + watchlist:
            if stock not in seen:
                seen.add(stock)
                stocks_to_monitor.append(stock)

        if not stocks_to_monitor:
            if not balance_ok:
                # 보유 종목이 "없는" 게 아니라 "모르는" 상태다. 기본 종목을 감시하면
                # 사용자와 무관한 종목의 알림이 나가므로 이번 주기는 조용히 건너뛴다.
                logger.info("잔고 조회 실패 + 관심 종목 없음 — 이번 주기 감시 대상이 없습니다.")
                return
            logger.info("보유 종목 및 관심 종목이 없습니다. 기본 종목을 감시합니다.")
            stocks_to_monitor = DEFAULT_MONITOR_STOCKS

        with Session(engine) as session:
            for stock in stocks_to_monitor:
                for source in SIGNAL_SOURCES:
                    await _monitor_signal(
                        stock, source, session, state, filtered_signal_repo
                    )
                    
    except Exception as e:
        logger.error(f"모니터링 태스크 시작 중 오류: {e}")


async def _send_telegram_alert_if_needed(
    stock: str,
    source: str,
    analysis_data: dict[str, Any],
    state: RedisSchedulerState | None,
) -> None:
    try:
        alert_mode = await state.get_telegram_alert_mode() if state is not None else "urgent"
        if not should_send_telegram_alert(analysis_data, alert_mode=alert_mode):
            return
        # 알림 모드와 같은 저장소에서 같은 타이밍에 읽는다 (#297).
        level = await _telegram_user_level(state)
        await telegram_notifier.send_analysis_alert(
            stock,
            source,
            analysis_data,
            alert_mode=alert_mode,
            level=level,
        )
    except Exception as e:
        logger.error("[%s:%s] Telegram 알림 처리 중 오류: %s", source, stock, e)


async def _record_filtered_signal(
    repo: SqliteFilteredSignalRepo | None,
    stock: str,
    source: str,
    signal_score: SignalScore,
) -> None:
    """걸러진 신호의 채점 결과를 남긴다 (#304). 실패해도 감시는 계속한다.

    로그 한 줄로는 grep만 되고 집계가 안 돼서 임계값을 조정할 근거가 없었다. 기록이
    실패했다고 감시 루프를 멈추지는 않는다 — 이 값은 사후 분석용이고, 여기서 예외를
    올리면 DB 문제 하나가 다음 종목·소스의 감시까지 통째로 건너뛰게 만든다.
    """
    global _filtered_signal_failure_streak, _last_filtered_signal_error

    if signal_score.score is None:
        # 채점 자체가 없었던 경로(빈 본문·직전과 동일). repo.record도 같은 조건에서
        # 걸러 내지만, 여기서 먼저 끊어 세션을 열지도 않는다.
        return
    try:
        await (repo or _default_filtered_signal_repo()).record(
            stock_name=stock,
            source=source,
            score=signal_score.score,
            threshold=SIGNAL_SCORE_THRESHOLD,
            reason=signal_score.reason,
            uncertainty=signal_score.uncertainty,
        )
    except Exception as e:
        signature = f"{type(e).__name__}:{e}"
        _filtered_signal_failure_streak += 1
        if (
            _filtered_signal_failure_streak == 1
            or signature != _last_filtered_signal_error
            or _filtered_signal_failure_streak % _FILTERED_SIGNAL_ERROR_LOG_PERIOD == 0
        ):
            logger.error(
                "[%s:%s] 걸러진 신호 점수 기록 실패(score=%s, %d건 연속): %s",
                source,
                stock,
                signal_score.score,
                _filtered_signal_failure_streak,
                e,
            )
        else:
            # 같은 원인의 반복 실패는 debug로 내린다. 기록이 비는 구간 자체는 위의
            # error와 아래 복구 로그의 집계로 드러난다.
            logger.debug(
                "[%s:%s] 걸러진 신호 점수 기록 실패가 %d건 연속됩니다: %s",
                source,
                stock,
                _filtered_signal_failure_streak,
                e,
            )
        _last_filtered_signal_error = signature
        return

    if _filtered_signal_failure_streak:
        # 복구 보고. 놓친 건수를 남기지 않으면 분포에 구멍이 뚫린 구간을 나중에
        # 알아볼 방법이 없다 — 기록되지 않은 신호는 어디에도 흔적이 없기 때문이다.
        logger.warning(
            "걸러진 신호 점수 기록이 복구되었습니다. 직전까지 %d건을 기록하지 못했습니다.",
            _filtered_signal_failure_streak,
        )
        _filtered_signal_failure_streak = 0
        _last_filtered_signal_error = None


async def _monitor_signal(
    stock: str,
    source: SignalSource,
    session: Session,
    state: RedisSchedulerState | None,
    filtered_signal_repo: SqliteFilteredSignalRepo | None = None,
):
    analysis_token = None
    try:
        # 1. 최신 signal 수집
        current_signal = await run_mcp_tool(
            source.mcp_params,
            source.tool_name,
            {source.stock_arg_name: stock},
        )

        current_digest = signal_hash(current_signal)
        if state is None:
            cache_key = (source.name, stock)
            last_signal = last_analyzed_signal_cache.get(cache_key)
            if last_signal is not None and signal_hash(last_signal) == current_digest:
                logger.info(f"[{source.name}:{stock}] 동일 signal입니다. 분석을 건너뜁니다.")
                return
        else:
            if await state.in_cooldown(source.name, stock):
                logger.info(f"[{source.name}:{stock}] 분석 cooldown 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_digest = await state.get_last_signal_hash(source.name, stock)
            if last_digest == current_digest:
                logger.info(f"[{source.name}:{stock}] 동일 signal입니다. 분석을 건너뜁니다.")
                return

            analysis_token = await state.acquire_analysis_lock(source.name, stock)
            if analysis_token is None:
                logger.info(f"[{source.name}:{stock}] 다른 worker가 분석 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_signal = await state.get_last_signal_text(source.name, stock)

        # 2. 유의미성 판단 (Local Model or Mini LLM API)
        #    #298: 판정은 |score| >= 임계값이다. 점수·근거·불확실성은 판정과 같은
        #    호출에서 함께 받아 분석 리포트와 알림까지 실어 나른다. 채점하지 못했으면
        #    (fail-open) score가 None이며 그대로 null로 남는다.
        signal_score = await score_signal(
            stock,
            current_signal,
            last_signal,
            source=source.name,
            provider=FILTER_PROVIDER,
        )

        if not signal_score.is_significant:
            await _set_last_signal_state(state, source.name, stock, current_signal, current_digest)
            # 걸러진 신호의 점수도 남긴다. 임계값 미만은 AgentReport에 저장되지 않으므로
            # (분석 자체를 건너뛴다) 임계값을 조정할 때 참고할 분포가 이 로그뿐이다.
            logger.info(
                "[%s:%s] 유의미한 변화 없음(score=%s, 임계값=%s). 분석을 건너뜁니다.",
                source.name,
                stock,
                signal_score.score,
                SIGNAL_SCORE_THRESHOLD,
            )
            await _record_filtered_signal(
                filtered_signal_repo, stock, source.name, signal_score
            )
            return

        # 3. 유의미한 변화가 있을 때만 고성능 에이전트 분석 실행
        logger.info(f"[{source.name}:{stock}] 유의미한 변화 감지! 상세 분석을 시작합니다.")
        analysis_data = await perform_stock_analysis(
            stock,
            "nat",
            session,
            trigger_source=source.name,
            trigger_signal=current_signal,
            signal_score=signal_score,
        )
        await _set_last_signal_state(state, source.name, stock, current_signal, current_digest)

        await _send_telegram_alert_if_needed(stock, source.name, analysis_data, state)

        # 분석이 갱신됐다는 신호만 WebSocket으로 보낸다. 분석 전문(analysis_data)은
        # 싣지 않는다 — PORTFOLIO_UPDATE(#229)와 같은 패턴이고, 클라이언트는 이 신호를
        # 받으면 /api/v1/db/reports를 재조회한다.
        #
        # #266: WebSocket에는 인증이 없다. Origin 허용목록 검사(main.py
        # is_allowed_ws_origin)로 브라우저발 Cross-Site WebSocket Hijacking은 막지만,
        # Origin 헤더를 보내지 않는 비브라우저 클라이언트는 여전히 붙을 수 있다. 분석
        # 전문을 싣지 않으면 채널이 뚫려도 유출될 내용 자체가 없다.
        #
        # stock·source는 남긴다. 어떤 종목의 리포트를 다시 읽어야 하는지 알려 주는 값이고,
        # 이걸 빼면 클라이언트가 매 신호마다 전체 리포트를 훑어야 한다. 재조회 대상인
        # /api/v1/db/reports 자체가 같은 내용을 인증 없이 내주므로, 종목명만 남기는 것이
        # 새로 여는 경로도 아니다.
        await manager.broadcast({
            "type": "AGENT_ANALYSIS",
            "stock": stock,
            "source": source.name,
            "reason": "significant_change_detected"
        })
        logger.info(f"[{source.name}:{stock}] 분석 결과 브로드캐스트 완료")

    except Exception as e:
        if state is not None and analysis_token is not None:
            await state.set_cooldown(source.name, stock, type(e).__name__)
        logger.error(f"[{source.name}:{stock}] 개별 종목 모니터링 중 오류: {e}")
    finally:
        if state is not None and analysis_token is not None:
            try:
                await state.release_lock(state.keys.analysis_lock(source.name, stock), analysis_token)
            except Exception as e:
                logger.error(f"[{source.name}:{stock}] Redis analysis lock 해제 중 오류: {e}")


async def _set_last_signal_state(
    state: RedisSchedulerState | None,
    source: str,
    stock: str,
    signal_text: str,
    digest: str,
) -> None:
    if state is None:
        last_analyzed_signal_cache[(source, stock)] = signal_text
    else:
        await state.set_last_signal(source, stock, signal_text, digest)

async def ping_task():
    """
    스케줄러와 WebSocket이 정상 작동하는지 확인하기 위한 테스트 작업입니다.
    60초마다 모든 클라이언트에게 핑을 보냅니다.
    """
    logger.info("Background task: sending periodic ping via WebSocket")
    await manager.broadcast({
        "type": "SYSTEM_PING",
        "message": "Scheduler is alive and monitoring...",
        "status": "active"
    })


async def morning_briefing_task(
    watchlist_repo: SqliteWatchlistRepo | None = None,
    *,
    level: str | None = None,
):
    """모닝 브리핑. ``level``을 주지 않으면 저장된 설명 수준을 직접 읽는다.

    cron으로 등록될 때는 인자가 없으므로 여기서 읽어야 한다 — 기본값을 그대로 쓰면 /level
    설정이 브리핑에만 적용되지 않는다 (#297 자가리뷰).
    """
    try:
        if level is None:
            level = await _telegram_user_level()
        if watchlist_repo is None:
            watchlist_repo = SqliteWatchlistRepo(lambda: Session(engine))
        try:
            watchlist = await watchlist_repo.get_watchlist()
        except Exception as e:
            logger.error("모닝 브리핑 관심 종목 조회 중 오류: %s", e)
            watchlist = []

        briefing = await generate_morning_briefing(watchlist)
        message = telegram_notifier.format_morning_briefing(briefing)
        await telegram_notifier.send_text(render(message, KIND_BRIEFING, level))
    except Exception as e:
        logger.error("모닝 브리핑 작업 중 오류: %s", e)


async def purge_filtered_signals_task(
    filtered_signal_repo: SqliteFilteredSignalRepo | None = None,
) -> None:
    """보존 기간이 지난 걸러진 신호 기록을 정리한다 (#304).

    감시 루프가 종목·소스마다 10분 주기로 돌아 행이 빠르게 쌓이므로, 보존 정책을
    두지 않으면 이 테이블이 SQLite 파일을 조용히 불린다. 하루 한 번 잘라 낸다.

    monitor_market_task와 달리 Redis 스케줄러 락을 잡지 않는다. 워커가 둘 다 같은
    조건으로 지우면 늦게 도착한 쪽이 0건을 지울 뿐이라 락으로 지킬 상태가 없다.
    """
    repo = filtered_signal_repo or _default_filtered_signal_repo()
    try:
        deleted = await repo.purge_expired(FILTERED_SIGNAL_RETENTION_DAYS)
    except Exception as e:
        # 정리 실패는 다음 주기에 다시 시도된다. 재시도하면 같은 조건으로 지우므로
        # 이번에 못 지운 행도 함께 정리된다.
        logger.error(
            "걸러진 신호 기록 정리 실패 (보존 %d일): %s",
            FILTERED_SIGNAL_RETENTION_DAYS,
            e,
        )
        return
    logger.info(
        "걸러진 신호 기록 정리 완료: %d건 삭제 (보존 %d일)",
        deleted,
        FILTERED_SIGNAL_RETENTION_DAYS,
    )


def start_scheduler():
    """스케줄러를 시작하고 작업을 등록합니다."""
    if not scheduler.running:
        # 60초마다 실행되는 테스트 작업 추가
        scheduler.add_job(ping_task, "interval", seconds=60, id="periodic_ping")
        
        # 10분마다 시장 모니터링 수행 (뉴스 기반 필터링이 있으므로 주기를 짧게 조정 가능)
        scheduler.add_job(
            monitor_market_task,
            "interval",
            minutes=10,
            id="market_monitoring",
            next_run_time=datetime.now(scheduler.timezone),
        )

        scheduler.add_job(
            catalyst_calendar_task,
            "cron",
            hour=8,
            minute=30,
            id="catalyst_calendar",
        )
        scheduler.add_job(
            morning_briefing_task,
            "cron",
            day_of_week="mon-fri",
            hour=8,
            minute=30,
            id="morning_briefing",
        )
        # 걸러진 신호 기록 정리 (#304). 장 시작 전 한산한 시각에 돌려 감시 주기와
        # DB 접근이 겹치지 않게 한다.
        scheduler.add_job(
            purge_filtered_signals_task,
            "cron",
            hour=4,
            minute=10,
            id="purge_filtered_signals",
        )
        
        scheduler.start()
        logger.info("APScheduler started with optimized monitoring tasks.")

def stop_scheduler():
    """스케줄러를 종료합니다."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
