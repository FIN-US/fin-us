from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Awaitable, Callable, Literal, Protocol

from fastapi import HTTPException

from .telegram_notifier import TelegramTextSender, send_text_settled_receipt
from .timeutil import KST

logger = logging.getLogger(__name__)

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "MARKET"]

# 대기 주문을 누가 만들었는가 (#390).
# - user_command: 사용자가 방금 낸 명령이 만든 주문(/buy·/sell·자연어 주문·/advise).
# - auto_proposal: 사용자 명령 없이 생긴 주문(룰 트리거 자동 제안, scheduler.run_rule_triggered_proposal).
# 텍스트 /confirm은 user_command만 확정할 수 있다. 근거는 PendingOrder.confirmable_by_text 참조.
OrderOrigin = Literal["user_command", "auto_proposal"]
# 출처의 fail-closed 기본값. 모르는 주문은 자동 제안으로 간주해 텍스트 확정을 막는다.
DEFAULT_ORDER_ORIGIN: OrderOrigin = "auto_proposal"
# 저장값을 읽을 때 쓰는 허용 목록. redis_state._deserialize가 이 집합 밖의 값을
# DEFAULT_ORDER_ORIGIN으로 접는다 — 저장값이 코드보다 오래 살기 때문에 읽는 쪽이 방어한다.
ORDER_ORIGINS: frozenset[str] = frozenset(("user_command", "auto_proposal"))

# 대기 주문을 보지 않고(출처를 모른 채) "그 주문을 먼저 처리하라"고 안내할 때 쓰는 다음 행동 문장
# (PR #391 리뷰). 충돌(/buy·/advise·자동 제안이 슬롯에 막힘)과 /confirm 재배달 거절이 쓴다.
# 확정 버튼을 먼저 권하는 이유: 버튼은 두 출처 모두 확정하지만 텍스트 /confirm은 사용자 주문만
# 확정한다(#390). "/confirm 또는 /cancel"로 안내하면 자동 제안 앞에서는 안내대로 한 /confirm이
# 다시 버튼 안내로 돌아온다. 출처를 읽어 분기하지 않는 것은, 안내 시점과 사용자가 행동하는 시점
# 사이에 대기 주문이 바뀔 수 있어 어느 쪽이든 두 출처를 다 덮는 문장이 필요하기 때문이다.
PENDING_ORDER_NEXT_STEP_TEXT = (
    "주문 메시지의 확정 버튼으로 확정하거나 /cancel로 취소하세요. "
    "직접 낸 주문은 /confirm으로도 확정됩니다."
)

# 대기 주문의 앱 레벨 만료 창. PendingOrder의 성질이므로 여기 둔다 (#299).
# telegram_commands가 같은 이름으로 재수출하며(기존 import 경로 유지), order_assist도
# 여기서 직접 읽는다 — telegram_commands에 두면 order_assist와 순환 import가 된다.
# redis 쪽 PENDING_ORDER_TTL_SEC(10분)은 이 값의 10배 여유로 잡힌 별개 장치다.
ORDER_EXPIRES_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class PendingOrder:
    chat_id: str
    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    # LIMIT이면 주문 조건(지정가)이다. MARKET이면 주문 조건이 아니라 **표시·기록용
    # 참고단가**로, 주문 시점 현재가가 들어온다 (#309). 시장가 주문의 체결가는 KIS
    # 현금주문 응답에 없으므로(order-cash output은 KRX_FWDG_ORD_ORGNO·ODNO·ORD_TMD뿐)
    # 이것이 주문 시점에 얻을 수 있는 최선의 단가다.
    price: int
    created_at: datetime
    order_type: OrderType = "LIMIT"
    callback_token: str = ""
    # 이 주문의 확정 프롬프트(버튼이 달린 마지막 조각)의 Telegram message_id다 (#386).
    # 저장할 때는 비어 있다. 프롬프트는 저장이 슬롯을 따낸 뒤에야 보내므로(#247) id는 전송이
    # 끝나야 생기고, send_order_prompt가 그때 채운다. None은 "모른다"이고, 텍스트
    # /confirm은 모르는 주문을 실행하지 않는다(prompted_before 참조). 버튼 확정은 이 값을
    # 보지 않는다. 버튼에는 주문별 callback_token이 실려 있어 다른 주문의 확정을 이미 막는다.
    prompt_message_id: int | None = None
    # 이 주문을 만든 계기 (#390). 기본값이 "자동 제안"인 것은 fail-closed다 — 출처를 싣지 않은
    # 생성 경로나 이 필드가 없는 기존 저장값은 텍스트 /confirm으로 확정되지 않는 쪽으로 읽힌다.
    # 텍스트 확정이 조용히 열리는 방향으로는 틀리지 않는다. 확정 버튼은 출처를 보지 않는다.
    origin: OrderOrigin = DEFAULT_ORDER_ORIGIN

    def text_confirm_allowed(self) -> bool:
        """텍스트 /confirm으로 확정할 수 있는 출처면 True (#390).

        사용자가 모르는 사이에 대기 주문이 생기는 출처는 자동 제안 하나뿐이고, 텍스트
        /confirm이 "본 적 없는 주문"을 확정하는 창도 거기서만 열린다. 사용자가 낸 주문은
        같은 기기에서 /confirm과 순서대로 전송되므로(오프라인 큐도 보낸 순서를 지킨다)
        /confirm이 뒤에 보낸 주문의 프롬프트를 추월할 수 없다.
        """
        return self.origin == "user_command"

    def confirmable_by_text(self, message_id: int | None) -> bool:
        """``message_id``의 텍스트 /confirm이 이 주문을 확정해도 되면 True (#386, #390).

        claim_if의 판정이자 이 규칙의 정본이다. 두 조건이 함께 서야 한다.

        - 출처가 사용자 명령일 것 (#390). 자동 제안 주문은 확정 버튼으로만 확정한다.
        - 그 /confirm이 이 주문의 확정 프롬프트보다 뒤에 보낸 것일 것 (#386).

        어느 한쪽이라도 모르면 False, 즉 실행하지 않는 쪽이다.
        """
        return self.text_confirm_allowed() and self.prompted_before(message_id)

    def prompted_before(self, message_id: int | None) -> bool:
        """``message_id`` 메시지보다 이 주문의 확정 프롬프트가 먼저 나갔으면 True (#386).

        텍스트 /confirm이 이 주문을 확정할 수 있는지를 가른다. 한 채팅의 message_id는 봇과
        사용자 메시지를 합쳐 단조 증가하는 순번이라, 프롬프트 id보다 큰 /confirm은 프롬프트가
        나간 뒤에 보낸 것이다. 시계를 쓰지 않으므로 텔레그램·호스트 시계 오차가 끼어들 자리가
        없다.

        둘 중 하나라도 모르면 False, 즉 실행하지 않는 쪽이다. 폴러 적체나 다운타임 뒤에 늦게
        처리된 /confirm이 그사이 생긴 새 주문을 확정 없이 실행하던 경로가 이 판정으로 닫힌다.

        이것만으로는 텍스트 확정의 조건이 아니다. message_id의 순서는 서버가 **받은** 순서라
        사용자 기기의 전송 지연에서 뒤집힌다 — 출처 판정(text_confirm_allowed, #390)이 함께
        서야 하고, 둘을 묶은 것이 confirmable_by_text다.
        """
        return (
            self.prompt_message_id is not None
            and message_id is not None
            and self.prompt_message_id < message_id
        )


class PromptMessageIdStore(Protocol):
    """send_order_prompt가 대기 주문 저장소에 요구하는 전부 (#386).

    redis_state.PendingOrderStore의 일부다. 그 Protocol을 여기서 import하지 않는 것은
    redis_state가 이 모듈의 PendingOrder를 타입으로 끌어오기 때문이다(순환을 만들지 않는다).
    """

    async def set_prompt_message_id(
        self, chat_id: str, callback_token: str, message_id: int
    ) -> bool: ...


async def _record_prompt_message_id(
    store: PromptMessageIdStore, order: PendingOrder, message_id: int | None
) -> None:
    """확정 프롬프트를 보낸 뒤 그 message_id를 대기 주문에 남긴다 (#386). send_order_prompt의 몫이다.

    실패해도 예외를 올리지 않는다. 프롬프트는 이미 나갔고 대기 주문도 살아 있으므로, 여기서
    할 수 있는 것은 기록뿐이다. 기록하지 못한 주문은 id가 없는 채로 남고, 텍스트 /confirm은
    그것을 실행하지 않는다(fail-closed). 확정 버튼은 그대로 쓸 수 있다.
    """
    if message_id is None:
        logger.warning(
            "확정 프롬프트의 message_id를 알 수 없다 — 이 대기 주문은 텍스트 /confirm으로 "
            "실행되지 않고 버튼으로만 확정된다 (#386)"
        )
        return
    try:
        recorded = await store.set_prompt_message_id(
            order.chat_id, order.callback_token, message_id
        )
    except Exception as exc:
        logger.error(
            "확정 프롬프트 message_id 기록 실패 — 이 대기 주문은 텍스트 /confirm으로 실행되지 "
            "않는다 (#386): %s",
            exc,
        )
        return
    if not recorded:
        # 프롬프트를 보내는 사이 주문이 확정·취소·만료됐다. 남길 대상이 없다.
        logger.info("확정 프롬프트 message_id를 남길 대기 주문이 이미 없다 (#386)")


# 확정·취소 콜백 데이터 접두사와 그 버튼을 만드는 함수. ORDER_EXPIRES_AFTER와 같은 이유로
# 여기 있다 — telegram_commands가 재수출하고(기존 import 경로 유지), 스케줄러의 자동
# 제안(#314)도 여기서 직접 읽는다. 이 함수가 telegram_commands의 메서드로만 있으면 자동
# 제안이 버튼을 직접 조립하게 되고, 그 순간 "확정 버튼은 한 곳에서만 만든다"가 깨진다.
# 콜백 문자열이 갈리면 _handle_callback_query가 못 알아보는 버튼이 사용자에게 나간다.
ORDER_CONFIRM_CALLBACK = "order:confirm"
ORDER_CANCEL_CALLBACK = "order:cancel"


def order_reply_markup(order: PendingOrder) -> dict[str, Any]:
    """대기 주문의 확정/취소 인라인 키보드. 수동·자동 제안이 같은 것을 쓴다."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 확정",
                    "callback_data": f"{ORDER_CONFIRM_CALLBACK}:{order.callback_token}",
                },
                {
                    "text": "❌ 취소",
                    "callback_data": f"{ORDER_CANCEL_CALLBACK}:{order.callback_token}",
                },
            ]
        ]
    }


async def send_order_prompt(
    notifier: TelegramTextSender,
    store: PromptMessageIdStore,
    order: PendingOrder,
    text: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """저장된 대기 주문의 확정 프롬프트를 보내고, 그 message_id를 주문에 남긴다 (#386).

    대기 주문을 만드는 세 경로(/buy·/sell·자연어 주문, /advise, 룰 트리거 자동 제안)가 모두
    이 함수 하나로 프롬프트를 보낸다. 전송과 id 기록을 호출부마다 따로 두면 한 경로만 기록을
    빠뜨릴 자리가 생기고, 그 경로의 주문은 텍스트 /confirm으로 영영 확정되지 않는다(PR #389
    리뷰). 버튼도 여기서 붙인다 — order_reply_markup과 같은 "한 곳에서만" 규칙이다.

    전송은 settled 재시도를 쓴다(#247). 반환값은 전송 성공 여부이고, 실패하면 호출부가 대기
    주문을 지운다. id를 남기지 못한 것은 실패로 치지 않는다. 프롬프트는 나갔고 버튼으로 확정할
    수 있으며, 텍스트 /confirm만 fail-closed로 막힌다.
    """
    receipt = await send_text_settled_receipt(
        notifier, text, reply_markup=order_reply_markup(order), sleep=sleep
    )
    if receipt.sent:
        await _record_prompt_message_id(store, order, receipt.message_id)
    return receipt.sent


@dataclass(frozen=True)
class OrderExecutionResult:
    stock_code: str
    stock_name: str
    side: OrderSide
    quantity: int
    # PendingOrder.price와 같은 뜻이다 — MARKET이면 체결가가 아니라 주문 시점 현재가다.
    # TradeHistory.price로 그대로 내려가므로 0이면 "0원 거래"가 아니라 "금액 모름"이고,
    # order_assist.load_daily_usage가 그 상태에서 일 거래대금 집계를 포기한다 (#309).
    price: int
    message: str
    raw_result: str
    order_type: OrderType = "LIMIT"


class TradeLedger(Protocol):
    """주문 경로가 체결 원장에 요구하는 전부 (#259 2단계, #319의 방식).

    이 계약이 생기기 전에는 주입 지점이 ``Any | None``이었고 호출부가
    ``if self.trade_recorder is not None``로 감싸져 있었다. 통지 outbox가 이 원장 위에
    서는 순간 그 선택성은 곧 **통지가 조용히 사라지는 경로**가 된다 — 원장이 없으면
    미통지 행도 없고, 미통지 행이 없으면 재배달도 없다. 그래서 전제를 타입으로 고정한다.

    재배달 쪽(``scheduler.trade_notification_task``)이 쓰는 조회는 여기 없다. 그건
    ``trade_notification_repo.TradeNotificationRepo``의 몫이고, 주문 경로는 쓰지 않는다.
    """

    def record(self, result: OrderExecutionResult, /) -> int: ...

    def mark_notified(self, trade_id: int, /, *, notified_at: datetime) -> None: ...


class TradeRecorder:
    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    def record(self, result: OrderExecutionResult) -> int:
        """체결을 원장에 남기고 그 행의 id를 돌려준다.

        id가 곧 체결 통지의 멱등 키다 (#259 2단계). "한 체결 = 한 통지"가 이 PK 위에서
        자연히 성립하므로 KIS 주문번호 같은 별도 키를 들이지 않는다 — 현재
        ``TradeHistory``에 주문번호 필드가 없어 도입하려면 컬럼 둘과 기록 경로가 함께
        늘어나는데, 지금 필요한 것은 "이 체결에 대한 통지를 이미 보냈는가" 하나다.

        ``notified_at``은 비운 채로 남긴다. 그 상태가 outbox의 "미통지"이며, 통지가
        나가야 ``mark_notified``가 채운다.
        """
        from .models import TradeHistory

        if result.price <= 0:
            # 여기서 막지는 않는다. 주문은 이미 나갔고, 행을 통째로 빠뜨리면 일 주문
            # **횟수** 한도까지 함께 헐거워진다 — 단가만 모르는 행이 낫다. 대신 남긴다:
            # 이 경고가 찍힌 날은 load_daily_usage가 집계를 포기해 /advise가 막힌다.
            logger.warning(
                "단가 없이 거래 이력을 기록한다 (%s %s %d주) — 오늘 /advise는 일 거래대금 "
                "집계 실패로 막힌다 (#309)",
                result.stock_code,
                result.side,
                result.quantity,
            )

        session = self.session_factory()
        try:
            trade = TradeHistory(
                stock_code=result.stock_code,
                stock_name=result.stock_name,
                trade_type=result.side,
                quantity=result.quantity,
                price=float(result.price),
            )
            session.add(trade)
            session.commit()
            trade_id = trade.id
            if trade_id is None:
                # 실제 Session이면 commit 뒤 id가 채워진다(만료된 속성을 읽는 순간
                # 다시 SELECT한다). 여기 걸리는 것은 duck-typed 세션뿐이다. 그래도
                # 조용히 넘기지 않는다 — id 없이는 outbox가 이 행을 마킹할 수 없어
                # 통지가 한 번 더 나간다.
                #
                # 문구가 "기록 실패"가 아닌 이유: commit은 이미 끝났고 아래 rollback도
                # no-op이다. 호출부는 이 예외를 기록 실패로 다루지만 행은 미통지로 남아
                # outbox가 다시 보낸다 — 그 어긋남을 메시지에 적어 다음 사람이 헛짚지
                # 않게 한다.
                raise RuntimeError(
                    "체결 이력 id를 받지 못했습니다 (행은 이미 커밋돼 남아 있을 수 있고, "
                    "그렇다면 통지 재배달 대상으로 남습니다)"
                )
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        return trade_id

    def mark_notified(self, trade_id: int, *, notified_at: datetime) -> None:
        """체결 통지가 나갔음을 원장에 남긴다 (#259 2단계).

        쓰기 자체는 ``trade_notification_repo.mark_trade_notified``가 한다. 주문 경로와
        재배달 경로가 같은 컬럼을 각자 UPDATE하면 한쪽만 조건이 바뀌는 날 절반의 통지가
        다시 미통지로 보인다.
        """
        from .trade_notification_repo import mark_trade_notified

        mark_trade_notified(self.session_factory, trade_id, notified_at=notified_at)


McpRunner = Callable[[Any, str, dict[str, Any]], Awaitable[str]]


def is_korean_market_open(now: datetime | None = None) -> bool:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)

    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(15, 30)


class McpTradingOrderGateway:
    def __init__(
        self,
        *,
        server_params: Any,
        mcp_runner: McpRunner,
        order_env: Literal["real", "demo"],
        real_order_enabled: bool,
    ):
        self.server_params = server_params
        self.mcp_runner = mcp_runner
        self.order_env = order_env
        self.real_order_enabled = real_order_enabled

    async def place_order(self, order: PendingOrder) -> OrderExecutionResult:
        if self.order_env == "real" and not self.real_order_enabled:
            raise HTTPException(
                status_code=403,
                detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
            )

        arguments: dict[str, Any] = {
            "stock_name": order.stock_name,
            "stock_code": order.stock_code,
            "side": order.side,
            "quantity": order.quantity,
            # 시장가에는 참고단가를 보내지 않는다. mcp-trading은 어차피 시장가면
            # ORD_UNPR을 0으로 고정하고(order.js buildCashOrderBody) 중복 방지 키에서도
            # 가격을 "0"으로 정규화하므로(order-dedup.js) 보내도 주문 결과는 같지만,
            # 기록용 값이 주문 조건처럼 읽히는 자리를 만들지 않는다 (#309).
            "price": 0 if order.order_type == "MARKET" else order.price,
            "order_env": self.order_env,
        }
        if order.order_type == "MARKET":
            arguments["order_type"] = "MARKET"

        raw_result = await self.mcp_runner(
            self.server_params,
            "place_order",
            arguments,
        )

        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            # 주문에 보낸 값(시장가면 0)이 아니라 대기 주문이 들고 있던 참고단가를 싣는다.
            # 여기서 0으로 덮으면 체결 기록이 다시 "금액 모름"이 된다 (#309).
            price=order.price,
            message=_extract_order_message(raw_result),
            raw_result=raw_result,
            order_type=order.order_type,
        )


def _extract_order_message(raw_result: str) -> str:
    text = str(raw_result or "").strip()
    if not text:
        return "주문 요청이 접수되었습니다."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]

    if isinstance(data, dict):
        for key in ("msg1", "message", "rt_msg", "output"):
            value = data.get(key)
            if value:
                return str(value)[:500]
    return text[:500]
