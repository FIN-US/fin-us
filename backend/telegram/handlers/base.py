"""명령 핸들러의 공통 기반: 전송·콜백 응답·사용자 수준 조회와 저장소 팩토리."""

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from contextvars import ContextVar
from sqlmodel import Session

from backend.ai.llm import llm_chat
from backend.ai.mcp_tools import run_mcp_tool
from backend.ai.user_preferences import request_risk_profile
from backend.core.config import (
    KIS_ORDER_ENV,
    KIS_REAL_ORDER_ENABLED,
    TRADING_MCP_PARAMS,
    VISUALIZATION_URL,
)
from backend.core.database import engine
from backend.core.redis_state import (
    create_redis_client,
    InMemoryPendingOrderStore,
    PendingOrderStore,
    redis_state,
    RedisPendingOrderStore,
    RedisTelegramPollerStore,
    TelegramPollerStore,
)
from backend.core.timeutil import KST
from backend.repositories.catalyst import SqliteCatalystEventRepo
from backend.repositories.watchlist import SqliteWatchlistRepo
from backend.telegram.handlers.texts import (
    ALERT_CALLBACK_PREFIX,
    BALANCE_REFRESH_CALLBACK,
    LOOKUP_CALLBACK_PREFIX,
    PROGRESS_DONE_MESSAGE,
    TRADE_CALLBACK_PREFIX,
)
from backend.telegram.notifier import send_text_settled, TelegramCommandNotifier
from backend.telegram.presentation import (
    DEFAULT_TELEGRAM_USER_LEVEL,
    kind_for_agent,
    reasoning_footnote,
    render,
)
from backend.trading.orders import McpTradingOrderGateway, OrderType, TradeLedger, TradeRecorder

logger = logging.getLogger(__name__)

# 확정 전송의 재시도 상수(SETTLED_SEND_*)와 루프(send_text_settled)는 telegram_notifier로
# 옮겼다 (PR #327 리뷰). 스케줄러의 자동 제안(#314)이 같은 재시도를 써야 하는데, 핸들러
# 메서드로 두면 그쪽에서 부를 수 없어 한쪽만 단발 전송이 되고 같은 429에 한쪽 메시지만
# 조용히 버려진다.
#
# 핸들러 모듈은 SETTLED_SEND_*를 다시 내보내지 않는다. 값을 읽는 것은 telegram_notifier
# 안이므로 테스트가 상한을 줄이려면 telegram_notifier 쪽을 패치해야 하며, 잘못된 대상을
# 잡으면 test_settled_send_gives_up_at_the_wall_clock_bound의 elapsed 단언이 잡는다.

# 각주 상수(REASONING_FOOTNOTE_*)와 라벨 표(AGENT_LABELS·TOOL_LABELS), 각주 조립 자체는
# #297에서 presentation으로 옮겼다. 동작은 그대로다 — 옮긴 이유는 나가는 문장을 조립하는
# 지점이 하나여야 용어 각주와의 순서·길이 예산을 한 곳에서 정할 수 있기 때문이다.
# 핸들러 모듈은 그 이름들을 다시 내보내지 않는다. 재수출만 남은 임포트는 정의가 두 곳에 있다는
# 착시를 만들고, 정적 검사에는 미사용 임포트로 보인다 (#297 자가리뷰).


class TelegramSendError(RuntimeError):
    """텔레그램 전송 실패. handle_update가 던지는 예외 중 유일하게 update와 무관하다.

    MCP·redis·저장소 오류는 전부 사용자 메시지로 변환되므로, handle_update가 실제로
    던지는 지배적 경로는 전송 실패다. 이걸 update별 poison으로 세면 429 한 번에
    대기 중인 명령이 전부 폐기되므로 폴러가 별도 예산으로 다뤄야 한다 (PR #242 리뷰).
    """

# 설명 수준 캐시 (#297). 값 자체는 거의 바뀌지 않으므로 짧게 들고 있어도 사용자가 체감할
# 지연은 없고, /level로 바꾸면 _apply_level이 그 자리에서 캐시를 갱신하므로 즉시 반영된다.
LEVEL_CACHE_TTL_SECONDS = 60.0
# 읽기에 실패했을 때는 더 길게 기다린다. 실패의 지배적 원인은 redis 부재이고 그건 다음
# 1초 안에 낫지 않는다 — 짧게 잡으면 장애 내내 메시지마다 소켓 타임아웃을 다시 문다.
LEVEL_LOOKUP_FAILURE_COOLDOWN_SECONDS = 300.0
_level_cache: tuple[str, float] | None = None


def _level_cache_get(now: float) -> str | None:
    if _level_cache is None or now >= _level_cache[1]:
        return None
    return _level_cache[0]


def _level_cache_put(level: str, expires_at: float) -> None:
    global _level_cache
    _level_cache = (level, expires_at)


def reset_level_cache() -> None:
    """캐시를 비운다. 테스트가 수준 저장소를 갈아끼울 때 쓴다."""
    global _level_cache
    _level_cache = None



def _nat_answer_message(
    result: Any,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
    question: str = "",
) -> str:
    """NAT 응답을 텔레그램 메시지로 만든다 (#260, #297).

    ``routed_agent``/``tools_used``는 ``ai.llm.NatAnswer``가 실어 오는 속성이다.
    속성이 없는 값(구버전 경로, 문자열만 주는 대역)이면 각주 없이 본문만 보낸다.

    #297에서 조립을 presentation.render에 위임했다. 마크다운 정리와 용어 각주가 함께
    붙지만 추론 각주의 모양은 그대로다. 틀은 라우팅된 에이전트로 정한다(매매일지 답변은
    일지 틀). 본문을 파싱해 추측하지 않는 것이 #260 각주와 같은 원칙이다.

    길이는 여기서 맞추지 않는다 (#313). 상한을 넘으면 전송 계층이 나눠 보내고, 각주는
    마지막 조각에 남는다 — 자리 다툼이 없어졌으므로 예산 규칙도 없어졌다.
    """
    routed_agent = getattr(result, "routed_agent", None)
    return render(
        result,
        kind_for_agent(routed_agent),
        level,
        reasoning=reasoning_footnote(routed_agent, getattr(result, "tools_used", ())),
        question=question,
    )


def _telegram_command_parts(text: str) -> tuple[str, str, str]:
    command, _, argument = text.partition(" ")
    command_name, separator, bot_username = command.partition("@")
    return command_name.lower(), bot_username.lower() if separator else "", argument.strip()


def _create_order_gateway() -> McpTradingOrderGateway:
    order_env = "real" if KIS_ORDER_ENV == "real" else "demo"
    return McpTradingOrderGateway(
        server_params=TRADING_MCP_PARAMS,
        mcp_runner=run_mcp_tool,
        order_env=order_env,
        real_order_enabled=KIS_REAL_ORDER_ENABLED,
    )


def _create_trade_recorder() -> TradeRecorder:
    return TradeRecorder(lambda: Session(engine))


def _create_pending_order_store() -> PendingOrderStore:
    """프로덕션 Redis pending_order 저장소를 생성한다.

    단일 Redis 클라이언트 인스턴스를 생성해 커넥션 풀을 재사용한다.
    핸들러 인스턴스당 한 번만 호출되므로 클라이언트 누적 없음.

    반환 타입은 구체 클래스가 아니라 Protocol이다. 유일한 호출부인 주입 지점이 계약 밖의
    것을 집어들지 못하게 막아, 구현체 교체가 그 지점으로 번지지 않게 한다 (PR #287 리뷰).
    """
    return RedisPendingOrderStore(create_redis_client())


def _create_poller_state_store() -> TelegramPollerStore:
    """프로덕션 폴러 상태 저장소를 생성한다 (#248).

    _create_pending_order_store와 같은 이유로 클라이언트를 하나만 만들어 풀을 재사용하고,
    같은 이유로 반환 타입을 Protocol로 좁힌다.
    """
    return RedisTelegramPollerStore(create_redis_client())


# 핸들러가 "이 update는 다시 실행하면 안 되는 지점을 지났다"를 폴러에 알리는 통로 (#259 3단계).
#
# 폴러는 handle_update가 돌아온 뒤에 offset을 영속화한다. /confirm 체결 성공은 그 사이에 결과
# 전송이 들어앉아 있어, 거기서 프로세스가 죽으면 같은 update가 재배달·재실행되고 claim이 비어
# "확정할 대기 주문이 없습니다"로 체결을 오표시했다(#269). 확정 시점을 아는 것은 핸들러이고
# 영속화를 쥔 것은 폴러라, 핸들러가 그 시점에 폴러를 부르게 한다.
#
# 인자가 아니라 ContextVar로 넘긴다. handle_update는 폴러의 덕 타이핑된 주입 지점이라(테스트
# 대역 수십 개가 handle_update(update) 하나만 구현한다) 시그니처를 넓히면 구현 전부가 함께
# 바뀐다. 핸들러 속성에 심으면 update마다 공유 객체를 고쳐 쓰게 된다. ContextVar는 폴러가
# handle_update를 await하는 동안에만 값이 보이고, 폴러 밖에서 부른 handle_update에서는 None이다.
_update_settled_hook: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "telegram_update_settled_hook", default=None
)


@dataclass(frozen=True)
class _TextConfirm:
    """텍스트 /confirm이 _handle_confirm에 넘기는 식별자 (#383, #386).

    버튼 확정은 이것을 넘기지 않는다. 버튼에는 주문별 토큰이 실려 있어 두 판정(재실행 표지,
    프롬프트 id 대조) 모두 필요 없다. 값이 None이면 update에서 읽지 못한 것이다 — update_id는
    표지를 건너뛰고(#383), message_id는 실행하지 않는 쪽으로 읽힌다(#386).
    """

    update_id: int | None
    message_id: int | None


@dataclass(frozen=True)
class OrderReading:
    """주문 명령 인자를 읽는 한 가지 방법 (#387).

    ``/buy KODEX 200 10``은 위치만으로는 두 가지로 읽힌다 — ``KODEX`` 200주 지정가 10원과
    ``KODEX 200`` 10주 시장가. 파서는 가능한 해석을 전부 이것으로 내고, 어느 쪽인지는
    _resolve_order_reading이 종목 마스터로 정한다. ``price``는 MARKET이면 0이다.
    """

    stock_name: str
    quantity: int
    price: int
    order_type: OrderType


# mcp/trading/stock-master.js resolveStock이 "마스터에 그런 이름이 없다"로 끝날 때 오류 문구의
# 고정 부분 (#387). run_mcp_tool은 도구 오류와 연결 실패를 같은 HTTPException으로 올리므로
# "종목 없음"을 가를 근거가 이 문구뿐이다. 문구가 바뀌어 여기서 못 알아보면 그 해석은 "확인
# 실패"로 읽혀 주문을 진행하지 않는 쪽으로 틀린다. test_telegram_commands가 JS 원문과 대조한다.
STOCK_NOT_FOUND_ERROR_MARKER = "의 종목 코드를 찾을 수 없습니다"


def _telegram_int(value: Any) -> int | None:
    """update의 정수 식별자만 통과시킨다. bool은 int의 하위 타입이라 따로 막는다.

    True가 update_id로 새면 표지 키가 1로 뭉치고, message_id로 새면 1로 대조된다.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))


class HandlerBase:
    """명령 핸들러의 공통 기반: 전송·콜백 응답·사용자 수준 조회와 저장소 팩토리."""
    def __init__(
        self,
        *,
        notifier: TelegramCommandNotifier,
        state_factory: Callable[[], Any] = redis_state,
        watchlist_repo: Any | None = None,
        catalyst_repo: Any | None = None,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
        llm_runner: Callable[..., Any] = llm_chat,
        risk_profile_client: Callable[..., Awaitable[str | None]] = request_risk_profile,
        order_gateway: Any | None = None,
        trade_recorder: TradeLedger | None = None,
        now_factory: Callable[[], datetime] | None = None,
        visualization_url: str = VISUALIZATION_URL,
        pending_order_store: PendingOrderStore | None = None,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.watchlist_repo = watchlist_repo if watchlist_repo is not None else _default_watchlist_repo()
        self.catalyst_repo = catalyst_repo if catalyst_repo is not None else _default_catalyst_repo()
        self.mcp_runner = mcp_runner
        self.llm_runner = llm_runner
        # NAT 사용자 선호 메모리 클라이언트(#397). 테스트가 NAT 없이 /risk를 검증하도록 주입한다.
        self.risk_profile_client = risk_profile_client
        self.order_gateway = order_gateway
        # None을 그대로 두지 않는다 (#259 2단계). 체결 통지 outbox가 이 원장 위에 서므로,
        # 원장이 없는 핸들러는 "체결됐는데 통지가 조용히 사라지는" 배포가 된다. 프로덕션은
        # 어차피 항상 _create_trade_recorder()를 주입해 왔으니 실동작 변화는 없고, 바뀐 것은
        # 미주입 시의 뜻이다 — "원장 없음"이 아니라 "기본 원장"이다. watchlist_repo·
        # catalyst_repo가 같은 규칙을 쓴다.
        self.trade_recorder: TradeLedger = (
            trade_recorder if trade_recorder is not None else _create_trade_recorder()
        )
        self.now_factory = now_factory or (lambda: datetime.now(KST))
        self.visualization_url = visualization_url.strip()
        # 프로덕션에서는 TelegramCommandPoller가 RedisPendingOrderStore를 주입한다.
        # 미주입 시의 InMemoryPendingOrderStore는 동기 dict 인터페이스도 함께 제공하지만
        # 그건 PendingOrderStore 계약 밖이라, 여기서는 프로토콜이 보장하는 것만 쓴다.
        if pending_order_store is None:
            logger.warning(
                "pending_order_store 미주입 — InMemoryPendingOrderStore 사용. "
                "멀티워커 환경에서는 주문이 프로세스 간 격리된다(#63)."
            )
            pending_order_store = InMemoryPendingOrderStore()
        self.pending_orders: PendingOrderStore = pending_order_store
        # /confirm 재실행 표지의 owner (#383). 폴러는 프로세스마다 핸들러를 한 번 만들므로
        # (TelegramCommandPoller.run) 이 값이 곧 "이 프로세스"다 — 같은 값이면 프로세스 안의
        # 재시도, 다르면 재시작 뒤의 재배달이다.
        self._confirm_owner = secrets.token_hex(8)
        self.market_callbacks: dict[str, tuple[str, str]] = {}

    async def _send_text_or_raise(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """재시도 가능한 지점의 전송. 실패하면 폴러가 update 전체를 다시 실행한다.

        호출부가 "여기서 실패해도 다시 실행하면 같은 결과가 나온다"를 보장할 때만 쓴다.
        부수효과가 확정된 뒤라면 _send_text_settled를 쓴다 (#247).

        이 호출이 든 try 블록은 TelegramSendError를 반드시 재던져야 한다. 전송 실패를
        사용자 메시지로 변환하면 무의미한 중복 메시지가 한 번 더 나간다.
        test_every_try_containing_a_retryable_send_reraises_it이 이를 강제한다 (#249).

        메시지가 나뉘어(#313) 앞 조각만 나간 뒤 실패하면, update 재실행은 첫 조각부터
        다시 보낸다. 이미 도착한 조각이 한 벌 더 보이는 대신 빠진 뒷부분이 채워진다 —
        이 경로의 계약이 "다시 실행해도 같은 결과"이므로 재실행 지점을 조각 단위로
        기억할 자리가 없고, 있더라도 중복보다 누락이 나쁘다. 부수효과가 확정돼 재실행할
        수 없는 경로는 _send_text_settled가 조각 단위로 이어 보낸다.
        """
        sent = await self.notifier.send_text(text, reply_markup=reply_markup)
        if sent is False:
            raise TelegramSendError("telegram send failed")

    async def _send_text_settled(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """확정된 부수효과의 결과 전송. 재시도 규칙은 telegram_notifier에 하나뿐이다.

        스케줄러의 자동 제안(#314)이 같은 함수를 부른다 — 한쪽만 단발 전송이면 같은 429에
        한쪽 메시지만 조용히 버려진다 (PR #327 리뷰). 조각 나누기와 조각 단위 재시도(#313)도
        그쪽으로 함께 옮겨 갔다. 자동 제안의 승인 프롬프트는 제안 근거와 검증 의견을 함께
        실어 상한을 넘길 수 있으므로, 나누기가 이 경로에만 있으면 안 된다.

        여기서는 테스트가 대체할 수 있는 _sleep을 넘겨 주는 것 말고 하는 일이 없다.
        """
        return await send_text_settled(
            self.notifier, text, reply_markup=reply_markup, sleep=self._sleep
        )

    async def _mark_update_settled(self) -> None:
        """지금 처리 중인 update를 폴러가 곧바로 확정하게 한다 (#259 3단계).

        폴러는 이 호출 안에서 offset을 영속화한다. 돌아온 뒤에 프로세스가 죽어도 그 update는
        재배달되지 않으므로, **이 뒤에서 잃은 메시지를 되살릴 근거가 따로 있는 자리에서만**
        부른다 — 지금은 체결이 원장에 미통지로 기록된 직후 하나다(_handle_confirm 참조).

        폴러 밖에서 불렸으면(handle_update를 직접 부르는 테스트 등) 아무 일도 하지 않는다.
        """
        hook = _update_settled_hook.get()
        if hook is not None:
            await hook()

    async def _sleep(self, seconds: float) -> None:
        """테스트가 전역 asyncio.sleep 대신 이 인스턴스만 대체할 수 있게 하는 간접층.

        _handle_watch의 조회 간격은 의도적으로 전역 asyncio.sleep을 그대로 쓴다 —
        기존 테스트가 그 sleep을 전역 패치로 가로채는 데 의존한다.
        """
        await asyncio.sleep(seconds)

    async def _answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        """콜백 답. ``text``가 있는 답이 실패하면 같은 문구를 메시지로 대체 전송한다 (#382).

        ``text``를 싣는 호출은 모두 **그 답이 사용자에게 가는 유일한 통지인 분기**다 — 주문
        버튼의 저장소 오류·만료 버튼(ORDER_STALE_CALLBACK_TEXT), 조회 버튼의 만료
        (MARKET_STALE_CALLBACK_TEXT, 토큰 없음·채팅 불일치), 그리고 "지원하지 않는 버튼입니다"
        (알 수 없는 콜백·/alerts·/level·거래·조회 메뉴의 알 수 없는 동작). 모두 답을 한 뒤
        곧바로 끝나고 주문을 실행하거나 대기 주문을 건드리지 않는다. 실제 notifier는 답 실패를
        False로 접어 오므로, 그대로 버리면 사용자는 버튼을 눌렀는데 아무 반응도 받지 못한다.
        뒤에 메시지를 이어 보내는 분기가 ``text``를 싣게 되면 이 규칙이 중복 통지를 만든다 —
        그런 분기는 텍스트 없는 답을 쓴다. 이 규칙("``text``를 실은 호출 바로 뒤는 return이거나
        함수 끝")은 test_every_callback_answer_with_text_ends_its_branch가 구조로 고정한다.

        텍스트 없는 답(스피너 해제)은 대체 전송하지 않는다. 뒤따르는 sendMessage가 결과를 전한다.

        대체 전송은 _send_text_settled다. 이 분기들은 재실행해도 같은 문구에 도달하므로
        _send_text_or_raise로 update를 재시도해도 되지만, 그러면 답 자체도 다시 나간다.
        settled는 인플레이스 재시도로 429를 흡수하고 끝내 실패하면 settled_send로 센다
        (delivery_alarm).

        대가 (주문 상태를 바꾸지 않는 안내 문구라 둘 다 받아들인다):
        - 답이 타임아웃으로 False였지만 실제로는 배달된 경우 토스트와 메시지가 둘 다 보인다.
        - 답이 오래돼서 실패하는 경우. offset이 redis에 영속화되므로 폴러가 오래 내려가 있다
          올라오면 그동안 쌓인 콜백이 다시 배달되고, 그 답은 "query is too old"(400)로 반드시
          실패한다. 그러면 문구를 싣는 분기마다 대체 메시지가 뒤늦게 한 건씩 나간다.
        """
        if not callback_query_id:
            return
        answered = await self.notifier.answer_callback_query(callback_query_id, text=text)
        if answered is False and text:
            await self._send_text_settled(text)

    async def _handle_visualize(self) -> None:
        if not self.visualization_url:
            await self._send_text_or_raise(
                "시각화 URL이 설정되지 않았습니다. VISUALIZATION_URL 환경변수를 설정하세요."
            )
            return

        await self._send_text_or_raise(f"Unity 포트폴리오 시각화:\n{self.visualization_url}")

    def _matches_command(self, command: str, bot_username: str, expected: str) -> bool:
        if command != expected:
            return False
        if not bot_username:
            return True
        notifier_username = str(getattr(self.notifier, "bot_username", "") or "").lower()
        return bot_username == notifier_username

    async def _current_level(self) -> str:
        """저장된 설명 수준. 읽지 못하면 기본값(초보) (#297).

        redis를 만지는 다른 명령(/alerts·/buy)과 달리 여기서 예외를 삼킨다. 그 명령들은
        redis 장애가 곧 명령의 실패지만, 수준은 **다른 메시지의 곁다리 정보**다 —
        수준을 못 읽었다고 시세나 알림 자체를 실패시키면, 부가 기능이 본 기능의 가용성을
        떨어뜨리는 꼴이 된다. 못 읽으면 설명을 붙이는 쪽(초보)으로 떨어진다: 아는 사람에게
        설명이 한 줄 붙는 쪽이, 모르는 사람이 설명을 못 받는 쪽보다 덜 나쁘다.

        캐시는 성능이 아니라 그 fail-open을 싸게 만들기 위한 것이다. 캐시가 없으면 redis가
        죽어 있는 동안 나가는 **모든** 메시지가 소켓 타임아웃(3초, #268)을 한 번씩 문다 —
        조회도 답변도 3초씩 늦어지는데 그 대가로 얻는 것은 어차피 기본값이다. 그래서 실패는
        더 길게 기억한다.

        캐시가 인스턴스가 아니라 모듈 수준인 이유: 이 설정은 채팅 하나에 하나뿐이고
        (알림 모드와 같은 단일 키), 폴러가 핸들러를 다시 세워도 같은 값이어야 한다.
        """
        now = time.monotonic()
        cached = _level_cache_get(now)
        if cached is not None:
            return cached
        try:
            async with self._state() as state:
                level = await state.get_telegram_user_level()
            ttl = LEVEL_CACHE_TTL_SECONDS
        except Exception as exc:
            logger.warning("설명 수준을 읽지 못해 기본값을 씁니다: %s", exc)
            level = DEFAULT_TELEGRAM_USER_LEVEL
            ttl = LEVEL_LOOKUP_FAILURE_COOLDOWN_SECONDS
        _level_cache_put(level, now + ttl)
        return level

    def _extract_callback_token(self, data: str) -> str | None:
        _, separator, token = data.rpartition(":")
        if not separator:
            return None
        return token.strip() or None

    def _help_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "💰 잔고", "callback_data": BALANCE_REFRESH_CALLBACK},
                    {"text": "🔔 알림", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"},
                ],
                [
                    {"text": "🧾 매매", "callback_data": f"{TRADE_CALLBACK_PREFIX}menu"},
                    {"text": "🔎 조회", "callback_data": f"{LOOKUP_CALLBACK_PREFIX}menu"},
                ],
            ]
        }

    def _first_line_containing(self, text: str, needles: tuple[str, ...]) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(needle in stripped for needle in needles):
                return stripped
        return None

    async def _send_progress_message(self, text: str) -> int | None:
        """진행 메시지를 보내고 나중에 치울 message_id를 반환한다 (#260).

        message_id를 돌려주지 못하는 notifier에서는 일반 전송만 하고 ``None``을
        반환한다 — 이 경우 진행 메시지는 대화에 그대로 남는다.

        전송 실패는 여기서 삼킨다. ``TelegramNotifier``의 ``send_text_returning_id``/
        ``send_text``는 이미 내부에서 예외를 잡아 각각 ``None``·무시로 떨어뜨리지만,
        notifier는 생성자로 주입 가능하므로 대역이 예외를 던질 수 있다. 진행 표시는
        답변에 덧붙는 편의 기능이라 이걸로 질의 처리 자체를 실패시키면 사용자는 답도
        못 받는다 — 그래서 주입된 구현이 무엇이든 여기서 한 겹 감싼다.
        """
        try:
            # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
            # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
            # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
            send_returning_id: Callable[..., Any] | None = getattr(
                self.notifier, "send_text_returning_id", None
            )
            if callable(send_returning_id):
                return await send_returning_id(text)
            await self.notifier.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("진행 메시지를 보내지 못했습니다: %s", exc)
        return None

    async def _clear_progress_message(self, message_id: int | None) -> None:
        """진행 메시지를 치운다 — 삭제하고, 삭제가 거부되면 종료 표시로 편집한다 (#260).

        최종 답변을 이 메시지의 **편집으로 내보내지 않는 이유**: 텔레그램은 메시지
        편집에 대해 푸시 알림을 보내지 않고 읽지 않음 표시도 갱신하지 않는다. 편집으로
        답을 내보내면 수십 초를 기다리다 앱을 닫은 사용자가 정작 답변 도착 알림을 받지
        못한다 — 체감 응답성을 높이려는 기능이 알림 가치를 뒤집는 셈이다.
        답변은 항상 새 메시지로 보낸다.

        실패는 삼킨다. 진행 메시지 정리는 답변 전달보다 덜 중요하다.
        """
        if message_id is None:
            return

        # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
        # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
        # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
        delete_message: Callable[..., Any] | None = getattr(
            self.notifier, "delete_message", None
        )
        if callable(delete_message) and await delete_message(message_id):
            return

        edit_message_text: Callable[..., Any] | None = getattr(
            self.notifier, "edit_message_text", None
        )
        if callable(edit_message_text) and await edit_message_text(message_id, PROGRESS_DONE_MESSAGE):
            return

        logger.info("진행 메시지를 정리하지 못했습니다 (message_id=%s)", message_id)

    @asynccontextmanager
    async def _state(self):
        state = self.state_factory()
        if hasattr(state, "__aenter__"):
            async with state as opened:
                yield opened
        else:
            yield state
