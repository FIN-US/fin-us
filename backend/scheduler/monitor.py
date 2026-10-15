"""10분 주기 시장 감시: 신호 수집·유의성 판정·분석 알림·룰 기반 자동 제안."""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Session

from backend.ai.analysis import perform_stock_analysis, ReportSession

from backend.ai.signals import score_signal, SignalScore
from backend.core.config import (
    DART_MCP_PARAMS,
    NEWS_MCP_PARAMS,
    SIGNAL_SCORE_THRESHOLD,
    TRADING_MCP_PARAMS,
)
from backend.core.database import engine
from backend.core.redis_state import (
    create_redis_client,
    PendingOrderStore,
    RedisPendingOrderStore,
    RedisSchedulerState,
    signal_hash,
)
from backend.core.timeutil import KST
from backend.core.ws_manager import manager
from backend.pii.egress import personal, public
from backend.repositories.filtered_signal import FilteredSignalRecorder
from backend.repositories.watchlist import WatchlistReader
from backend.scheduler import deps
from backend.scheduler.catalyst import _telegram_user_level
from backend.scheduler.portfolio import (
    _parse_balance_holdings,
    _refresh_portfolio_prices,
    _sync_portfolio_from_balance,
    is_balance_truncated,
    truncation_notice,
)
from backend.telegram.notifier import (
    should_send_telegram_alert,
    telegram_notifier,
    TelegramTextSender,
)
from backend.trading.order_assist import OrderAssistResult, ProposalTrigger, run_order_assist
from backend.trading.order_rules import (
    build_rule_scope,
    build_trigger_signal,
    format_auto_message,
    load_rule,
    match_rule,
    most_urgent,
    OrderAssistRule,
    RuleMatch,
    should_report_rejection,
    should_run,
)
from backend.trading.orders import is_korean_market_open, send_order_prompt

logger = logging.getLogger(__name__)


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
    # 이 소스의 원문이 뉴스·공시처럼 누구나 같은 값을 조회하는 공개 데이터인가 (#395).
    # True면 채점·분석 프롬프트에서 원문 속 금액을 마스킹하지 않는다. 기본값이 False인 것이
    # fail-safe의 자리다 — 계좌 데이터를 읽는 소스가 표시 없이 추가돼도 마스킹된다.
    # 판정 기준은 backend/pii/egress.py 모듈 docstring.
    public_data: bool = False


SIGNAL_SOURCES = [
    SignalSource(
        name="news",
        mcp_params=NEWS_MCP_PARAMS,
        tool_name="get_market_news",
        public_data=True,
    ),
    SignalSource(
        name="disclosure",
        mcp_params=DART_MCP_PARAMS,
        tool_name="get_disclosure_signal",
        public_data=True,
    ),
]


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


# 자동 제안(#314)이 쓰는 대기 주문 저장소. 텔레그램 폴러가 만드는 것과 같은 클래스이고
# 같은 redis 키(RedisKeys.pending_order)를 보므로, 여기서 저장한 대기 주문을 기존
# _handle_confirm이 그대로 소비한다 — 확정 버튼 경로가 수동 /advise와 완전히 같다.
#
# 프로세스당 하나만 만든다. RedisPendingOrderStore는 클라이언트 하나를 쥐고 그 커넥션
# 풀을 재사용하는데, 트리거마다 새로 만들면 풀이 쌓인다(telegram.handlers.base의
# _create_pending_order_store가 핸들러당 한 번만 불리는 것과 같은 이유다).
_rule_pending_order_store: PendingOrderStore | None = None


def _default_pending_order_store() -> PendingOrderStore:
    global _rule_pending_order_store
    if _rule_pending_order_store is None:
        _rule_pending_order_store = RedisPendingOrderStore(create_redis_client())
    return _rule_pending_order_store


async def _sleep(seconds: float) -> None:
    """테스트가 전역 ``asyncio.sleep`` 대신 이 모듈의 이름만 대체할 수 있게 하는 간접층.

    telegram.handlers.base._sleep과 같은 이유다 — 확정 전송의 재시도 백오프(최대 13초)를
    실제로 기다리는 테스트는 그 시간을 그대로 벽시계로 문다.
    """
    await asyncio.sleep(seconds)


async def monitor_market_task(
    watchlist_repo: WatchlistReader | None = None,
    filtered_signal_repo: FilteredSignalRecorder | None = None,
):
    """
    주기적으로 시장 상황을 모니터링합니다.
    """
    fallback_to_memory = True
    try:
        async with deps.redis_state() as state:
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
    watchlist_repo: WatchlistReader | None = None,
    filtered_signal_repo: FilteredSignalRecorder | None = None,
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
            balance_text = await deps.run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
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
            # 잘린 잔고로 동기화하면 아직 못 읽은 보유 종목이 삭제되므로,
            # _sync_portfolio_from_balance가 내부에서 잘림을 감지해 건너뜁니다.
            # 동기화 실패는 감시 루프에 영향을 주지 않아야 하므로 예외를 격리합니다.
            try:
                with Session(engine) as sync_session:
                    sync_result = _sync_portfolio_from_balance(balance_text, sync_session, holdings=holdings)
                # 잘림·마커 부재로 동기화를 건너뛴 경우(None)에는 테이블이 그대로이므로
                # 브로드캐스트하지 않는다. 동기화가 수행된 경우 updated_at을 매번 갱신하는
                # 특성상 내용이 직전과 동일해도 신호가 나간다 — 즉 정상 주기마다 1건이다.
                # payload에는 보유 종목 개수만 싣고 종목명·수량 등 실제 보유 내역은 담지
                # 않는다. WebSocket 인증은 FINUS_API_KEY를 채운 배포에서만 걸리고(#266),
                # 키를 비운 배포에서는 이 채널이 무인증으로 열려 있다(새 설치만 setup_env가
                # 키를 채운다). 신호만 보내고 클라이언트가 /api/v1/db/portfolio를 재조회하게
                # 두면 채널이 열려 있어도 계좌 보유 현황이 그 채널로 나가지 않는다.
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
                # 잘림 사유(max_pages/time_budget/error/...)마다 운영 대응이 다르므로 안내
                # 문구 줄을 그대로 싣는다(truncation_notice — 실현손익 잘림 경고와 공유).
                logger.warning(
                    "잔고 연속조회가 잘려 감시 대상이 불완전할 수 있습니다: 보유 종목 %d건만 확보 — %s",
                    len(owned_stocks),
                    truncation_notice(balance_text),
                )

        # 2. 시세 갱신 (#196). get_balance_rlz_pl(TTTC8494R)의 리포트에서 종목별
        # 현재가를 읽어 Portfolio.current_price와 price_updated_at을 채운다.
        #
        # 잔고 조회 성공(balance_ok) 밖에 두는 이유: 두 조회는 서로 다른 TR이고 서로의
        # 전제가 아니다. 잔고가 실패해도 이미 저장된 행의 시세는 갱신할 수 있고, 그때
        # 갱신을 함께 멈추면 KIS 장애 한 번이 잔고 공백에 더해 시세까지 TTL 밖으로
        # 밀어낸다. 반대로 시세 조회가 실패해도 위 잔고 동기화는 이미 끝나 있다.
        #
        # 위치가 잔고 동기화 뒤인 것은 순서 때문이다 — 이번 주기에 새로 삽입된 행이
        # 같은 주기에 시세를 받는다. 앞에 두면 신규 종목은 한 주기(10분) 동안
        # price_known=False로 남는다.
        #
        # 예외를 격리하는 이유는 Portfolio 동기화와 같다: 시세 갱신 실패가 뉴스·공시
        # 감시를 멈춰서는 안 된다. 도구 호출 실패 자체는 _refresh_portfolio_prices가
        # 이미 삼키므로, 여기 걸리는 것은 DB 오류처럼 그보다 안쪽의 실패다.
        try:
            await _refresh_portfolio_prices()
        except Exception as e:
            logger.error("Portfolio 시세 갱신 중 오류 (감시는 계속): %s", e, exc_info=True)

        if watchlist_repo is None:
            watchlist_repo = deps.SqliteWatchlistRepo(lambda: Session(engine))
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

        # 자동 제안(#314)은 감시가 끝난 뒤 한 번만 돈다. 감시 루프 안에서 바로 부르면
        # 제안 왕복(기본 120초) + 검증 왕복이 나머지 종목의 감시를 그만큼 늦춘다.
        # 룰이 꺼져 있으면 rule이 None이라 match_rule이 항상 None을 돌려주고, 아래
        # 리스트는 빈 채로 남는다.
        rule = load_rule()
        # 룰 대상은 보유 종목 + 관심 종목뿐이다. stocks_to_monitor가
        # DEFAULT_MONITOR_STOCKS로 떨어진 주기에는 이 집합과 겹치는 종목이 없다.
        rule_scope = build_rule_scope(owned_stocks, watchlist)
        rule_matches: list[RuleMatch] = []

        with Session(engine) as session:
            for stock in stocks_to_monitor:
                stock_rule = rule if stock in rule_scope else None
                for source in SIGNAL_SOURCES:
                    match = await _monitor_signal(
                        stock, source, session, state, filtered_signal_repo, rule=stock_rule
                    )
                    if match is not None:
                        rule_matches.append(match)

        await run_rule_triggered_proposal(rule_matches, state)

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
    repo: FilteredSignalRecorder | None,
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
        await (repo or deps._default_filtered_signal_repo()).record(
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
    session: ReportSession,
    state: RedisSchedulerState | None,
    filtered_signal_repo: FilteredSignalRecorder | None = None,
    *,
    rule: OrderAssistRule | None = None,
) -> RuleMatch | None:
    """이 종목·소스를 한 번 감시한다.

    ``rule``이 주어지고 분석 결과가 그 조건을 만족하면 :class:`RuleMatch`를 돌려준다 (#314).
    **여기서 제안을 만들지는 않는다** — 판정만 하고 호출부가 주기 끝에 한 건만 태운다.
    분석까지 가지 못한 모든 경로(동일 신호, cooldown, 유의미하지 않음, 예외)는 ``None``이다.
    """
    analysis_token = None
    try:
        # 1. 최신 signal 수집
        current_signal = await deps.run_mcp_tool(
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
        # 외부 LLM으로 나가는 신호 원문의 공개/개인 표시는 소스 정의가 정한다(#395).
        signal_segment = (
            public(current_signal) if source.public_data else personal(current_signal)
        )
        signal_score = await score_signal(
            stock,
            signal_segment,
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
            trigger_signal=signal_segment,
            signal_score=signal_score,
        )
        await _set_last_signal_state(state, source.name, stock, current_signal, current_digest)

        await _send_telegram_alert_if_needed(stock, source.name, analysis_data, state)

        # 분석이 갱신됐다는 신호만 WebSocket으로 보낸다. 분석 전문(analysis_data)은
        # 싣지 않는다 — PORTFOLIO_UPDATE(#229)와 같은 패턴이고, 클라이언트는 이 신호를
        # 받으면 /api/v1/db/reports를 재조회한다.
        #
        # #266: 이 채널이 인증을 받는지는 배포 설정에 달렸다. Origin 허용목록 검사
        # (main.py is_allowed_ws_origin)가 브라우저발 Cross-Site WebSocket Hijacking을
        # 막고, 비브라우저 클라이언트는 FINUS_API_KEY를 채운 배포에서만 막힌다(#266).
        # 키를 비운 배포가 남아 있으므로(새 설치만 setup_env가 키를 채운다) 여기서는
        # 무인증을 전제로 둔다 —
        # 분석 전문을 싣지 않으면 채널이 뚫려도 유출될 내용 자체가 없다.
        #
        # stock·source는 남긴다. 어떤 종목의 리포트를 다시 읽어야 하는지 알려 주는 값이고,
        # 이걸 빼면 클라이언트가 매 신호마다 전체 리포트를 훑어야 한다. 재조회 대상인
        # /api/v1/db/reports는 같은 인증 설정을 그대로 받으므로(키가 없으면 둘 다 열려
        # 있고, 키가 있으면 둘 다 닫힌다), 종목명만 남기는 것이 새로 여는 경로가 아니다.
        await manager.broadcast({
            "type": "AGENT_ANALYSIS",
            "stock": stock,
            "source": source.name,
            "reason": "significant_change_detected"
        })
        logger.info(f"[{source.name}:{stock}] 분석 결과 브로드캐스트 완료")

        # 룰 판정은 감시가 할 일을 전부 마친 뒤에 한다. 앞의 알림·브로드캐스트는 이
        # 판정 결과와 무관하게 나가야 하고, 판정은 순수 함수라 실패할 자리도 없다.
        match = match_rule(rule, stock=stock, source=source.name, analysis_data=analysis_data)
        if match is not None:
            logger.info(
                "[%s:%s] 자동 제안 룰 충족 (rule=%s, urgency=%s)",
                source.name,
                stock,
                match.rule_id,
                match.urgency,
            )
        return match

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


async def run_rule_triggered_proposal(
    matches: list[RuleMatch],
    state: RedisSchedulerState | None,
    *,
    pending_orders: PendingOrderStore | None = None,
    notifier: TelegramTextSender | None = None,
    now_factory: Callable[[], datetime] | None = None,
    assist: Callable[..., Any] | None = None,
) -> OrderAssistResult | None:
    """룰을 만족한 신호 중 한 건으로 주문 제안을 만든다 (#314).

    이 함수가 하는 일은 문턱 검사, 호출, 결과 전달이 전부다. 제안·한도 판정·검증·대기 주문
    저장은 전부 order_assist.run_order_assist 안에 있다 — telegram.handlers.orders._handle_advise가
    같은 이유로 그렇게 쓴다. 규칙이 두 파일에 나뉘면 "어느 쪽이 최종 판정인가"가 흐려진다.

    **주문은 나가지 않는다.** 승인 결과로 만들어지는 것은 60초 확정 버튼이 달린 대기 주문
    하나이고, 그 버튼을 소비하는 것은 기존 _handle_confirm이다. 자동 트리거라도 예외가 없다.

    돌려주는 값은 실행한 :class:`OrderAssistResult`이고, 문턱에서 막혔으면 ``None``이다.
    """
    if not matches:
        return None

    notifier = notifier if notifier is not None else telegram_notifier
    now_factory = now_factory or (lambda: datetime.now(KST))
    assist = assist or run_order_assist

    # ① 전달 경로 — 승인 결과를 보낼 수 없으면 제안을 만들지 않는다. 보내지 못할 대기
    #    주문을 저장하면 슬롯만 잡히고, 그 슬롯을 공유하는 사용자의 /buy가 영문 모를
    #    충돌로 막힌다.
    chat_id = str(getattr(notifier, "chat_id", "") or "").strip()
    if not getattr(notifier, "enabled", False) or not chat_id:
        logger.info("Telegram 전달 경로가 없어 자동 제안을 건너뜁니다.")
        return None

    # ② 장 운영 시간 — run_order_assist도 같은 검사를 하지만 거기서 걸리면 '거부'가 되고,
    #    감시는 24시간 도므로 사용자가 요청한 적도 없는 거부가 밤새 쌓인다. 자동 트리거는
    #    장 밖에서 아예 돌지 않는다.
    if not is_korean_market_open(now_factory()):
        logger.debug("장 운영 시간이 아니어서 자동 제안을 건너뜁니다.")
        return None

    # ③ 알림 모드 — off면 트리거 자체를 돌리지 않는다(order_rules.should_run 참고).
    #    모드를 읽지 못하면 진행하지 않는다. 확인하지 못한 상태를 '허용'으로 읽지 않는다.
    try:
        alert_mode = await state.get_telegram_alert_mode() if state is not None else "urgent"
    except Exception as e:  # noqa: BLE001 — fail-closed
        logger.error("알림 모드를 확인하지 못해 자동 제안을 건너뜁니다: %s", e)
        return None
    if not should_run(alert_mode):
        logger.info("알림이 꺼져 있어 자동 제안을 건너뜁니다 (mode=%s).", alert_mode)
        return None

    match = most_urgent(matches)
    if match is None:
        return None
    if len(matches) > 1:
        # 나머지를 버렸다는 사실을 남긴다. 대기 주문 슬롯이 하나라 두 번째부터는 어차피
        # conflict로 끝나지만, 로그가 없으면 "왜 저 종목은 제안이 안 왔지"에 답할 수 없다.
        logger.info(
            "이번 주기에 자동 제안 룰을 만족한 신호가 %d건이라 가장 긴급한 한 건만 진행합니다 "
            "(선택=%s/%s, 나머지=%s).",
            len(matches),
            match.stock,
            match.source,
            [f"{m.stock}/{m.source}" for m in matches if m is not match],
        )

    trigger = ProposalTrigger(
        source="scheduler_rule",
        stock=match.stock,
        chat_id=chat_id,
        rule_id=match.rule_id,
        # 수치가 아니라 "무엇을 확인하라"는 지시 문구다 (order_rules 모듈 docstring 3번).
        trigger_signal=build_trigger_signal(match.source),
    )
    store = pending_orders if pending_orders is not None else _default_pending_order_store()
    try:
        result = await assist(trigger, pending_orders=store, now_factory=now_factory)
    except Exception as e:  # noqa: BLE001 — 감시 주기를 여기서 끝내지 않는다.
        logger.error("자동 제안 실행 중 오류 (stock=%s): %s", match.stock, e, exc_info=True)
        return None

    if result.order is not None:
        # 승인은 알림 모드와 무관하게 보낸다. 확정 버튼이 필요할 뿐 아니라, 이 시점에는
        # 이미 대기 주문 슬롯이 잡혀 있어 알리지 않으면 사용자의 다음 /buy가 막힌다.
        #
        # /advise와 같은 재시도를 쓴다 (PR #327 리뷰). 여기까지 온 제안은 제안 왕복
        # (≤120초)·검증 왕복(≤40초)과 재제안 냉각을 이미 소비한 뒤라, 단발 전송으로 두면
        # 일시적인 429 한 번에 그 전부가 버려지고 같은 종목은 냉각이 풀릴 때까지(기본
        # 60분) 다시 시도되지도 않는다.
        #
        # 전송과 message_id 기록은 /buy·/advise와 같은 send_order_prompt 하나다 (#386). 이 주문은
        # 자동 제안(origin=auto_proposal)이라 텍스트 /confirm으로는 아예 확정되지 않고 확정 버튼으로만
        # 확정된다 (#390). 사용자가 다른 주문을 보고 보낸 /confirm이 전송 지연 끝에 이 주문에 닿는
        # 창이 거기서 닫힌다. id는 여전히 남긴다 — 확정 규칙의 한 조건이고 로그의 단서다.
        sent = await send_order_prompt(
            notifier, store, result.order, format_auto_message(result.message), sleep=_sleep
        )
        if not sent:
            # /advise와 같은 처리다 (#247). 프롬프트가 끝내 안 나갔으면 사용자는 대기 주문의
            # 존재를 모르고, 60초 안의 다음 명령이 영문 모를 충돌로 막힌다.
            logger.error("자동 제안 프롬프트를 보내지 못해 대기 주문을 정리합니다 (stock=%s).", match.stock)
            try:
                await store.delete(chat_id)
            except Exception as e:  # noqa: BLE001
                logger.error("자동 제안 프롬프트 미전달 후 대기 주문 정리 실패: %s", e)
        return result

    # 거부·충돌. 사용자가 요청한 적 없는 시도라 all 모드에서만 알린다. 이쪽은 부수효과가
    # 남지 않은 통지라 재시도 예산을 쓰지 않는다 — 못 보내면 로그로 남는 것이 전부다.
    if should_report_rejection(alert_mode):
        await notifier.send_text(format_auto_message(result.message))
    else:
        logger.info(
            "자동 제안이 %s로 끝났습니다 (stock=%s, mode=%s). 사용자에게 보내지 않습니다: %s",
            result.status,
            match.stock,
            alert_mode,
            result.message,
        )
    return result


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
