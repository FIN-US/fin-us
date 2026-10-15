"""주문 명령(/buy /sell /advise /confirm /cancel)과 자연어 주문."""

import asyncio
import logging
import re
import secrets
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from backend.ai.services import short_error as _short_error
from backend.core.config import TRADING_MCP_PARAMS
from backend.core.timeutil import KST
from backend.telegram.delivery_alarm import delivery_metrics
from backend.telegram.handlers.base import (
    _TextConfirm,
    HandlerBase,
    OrderReading,
    STOCK_NOT_FOUND_ERROR_MARKER,
    TelegramSendError,
)
from backend.telegram.handlers.texts import (
    ADVISE_COMMAND_HELP,
    BUY_COMMAND_HELP,
    CONFIRM_AUTO_PROPOSAL_BUTTON_ONLY_TEXT,
    CONFIRM_BEFORE_PROMPT_TEXT,
    CONFIRM_PROMPT_UNKNOWN_TEXT,
    CONFIRM_REPLAY_REFUSED_TEXT,
    NAT_PROGRESS_MESSAGE,
    ORDER_STALE_CALLBACK_TEXT,
    PENDING_ORDER_CONFLICT_TEXT,
    SELL_COMMAND_HELP,
    TRADE_CALLBACK_PREFIX,
    TRADE_COMMAND_HELP,
    UNRESOLVED_STOCK_WARNING,
)
from backend.trading.order_assist import parse_current_price, ProposalTrigger, run_order_assist
from backend.trading.orders import (
    is_korean_market_open,
    ORDER_CANCEL_CALLBACK,
    ORDER_CONFIRM_CALLBACK,
    ORDER_EXPIRES_AFTER,
    order_reply_markup,
    OrderSide,
    PendingOrder,
    send_order_prompt,
)
from backend.trading.stock_code import (
    _is_unresolved_echo,
    _looks_like_stock_code,
    _STOCK_CODE_EXTRACT_RE,
    describe_orderable_code_policy,
    extract_stock_name,
    is_orderable_stock_code,
)

logger = logging.getLogger(__name__)


class OrderCommands(HandlerBase):
    """주문 명령(/buy /sell /advise /confirm /cancel)과 자연어 주문."""

    async def _send_order_prompt(self, order: PendingOrder, text: str) -> bool:
        """/buy·/sell·자연어 주문과 /advise의 확정 프롬프트 전송 (#386).

        전송과 message_id 기록은 trading_orders.send_order_prompt 하나가 하고, 스케줄러의 자동
        제안도 같은 함수를 부른다. 여기서는 테스트가 대체할 수 있는 _sleep을 넘겨 주는 것 말고
        하는 일이 없다 — _send_text_settled와 같은 모양이다.
        """
        return await send_order_prompt(
            self.notifier, self.pending_orders, order, text, sleep=self._sleep
        )

    async def _handle_order_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        try:
            order = await self.pending_orders.get(chat_id)
        except Exception as exc:
            logger.error("pending_order 조회 실패 (callback): %s", exc)
            await self._answer_callback_query(
                callback_query_id,
                text="주문 저장소 오류로 처리할 수 없습니다. 잠시 후 다시 시도하세요.",
            )
            return
        token = self._extract_order_callback_token(data)
        if order is None or not token or token != order.callback_token:
            await self._answer_callback_query(
                callback_query_id,
                text=ORDER_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "confirm":
            await self._handle_confirm(chat_id)
            return
        await self._handle_cancel(chat_id)

    async def _handle_trade_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(TRADE_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = TRADE_COMMAND_HELP
            reply_markup = self._trade_reply_markup()
        elif action == "buy":
            text = BUY_COMMAND_HELP
            reply_markup = None
        elif action == "sell":
            text = SELL_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_advise(self, argument: str, chat_id: str) -> None:
        """``/advise <종목명>`` — 주문 보조 (#299).

        이 메서드가 하는 일은 파싱, 호출, 결과 전달이 전부다. 제안·한도 판정·검증·
        대기 주문 저장은 모두 order_assist.run_order_assist 안에 있다. 규칙이 두 파일에
        나뉘면 "어느 쪽이 최종 판정인가"가 흐려지고, 그 순간 한쪽만 고치는 변경이 생긴다.

        승인 결과의 버튼은 기존 _order_reply_markup을 그대로 쓴다 — 확정/취소 콜백,
        60초 만료, 체결 흐름이 /buy와 완전히 같은 경로를 탄다.
        """
        stock_name = argument.strip()
        if not stock_name:
            await self._send_text_or_raise(ADVISE_COMMAND_HELP)
            return

        await self.notifier.send_chat_action("typing")
        # 제안 에이전트 왕복은 수십 초가 걸린다. /earnings·NAT 폴백과 같은 방식으로
        # 접수 즉시 진행 메시지를 남기고, 결과가 오면 치운다 (#260).
        progress_message_id = await self._send_progress_message(NAT_PROGRESS_MESSAGE)
        trigger = ProposalTrigger(source="telegram", stock=stock_name, chat_id=chat_id)
        try:
            result = await run_order_assist(
                trigger,
                pending_orders=self.pending_orders,
                mcp_runner=self.mcp_runner,
                now_factory=self.now_factory,
            )
        except Exception as exc:
            await self._clear_progress_message(progress_message_id)
            # 자연어 폴백의 LLM 실패 통지와 같은 이유로 settled다 (#259 4단계, PR #377 리뷰).
            # 재시도 가능한 전송이면 전송 실패가 update 재시도로 번져 진행 메시지가 다시 나가고,
            # 예외가 제안 왕복 뒤(냉각 마킹 등)에서 났다면 제안 에이전트까지 다시 호출된다.
            await self._send_text_settled(f"주문 보조 실패: {_short_error(exc)}")
            return
        await self._clear_progress_message(progress_message_id)

        if result.order is None:
            # 거부·충돌 둘 다 여기로 온다. 충돌을 조용히 버리면 사용자는 명령이 씹힌
            # 것으로 읽는다 — 대기 주문이 있다는 사실을 반드시 알려야 한다.
            await self._send_text_settled(result.message)
            return

        notified = await self._send_order_prompt(result.order, result.message)
        if not notified:
            # /buy와 같은 처리다. 프롬프트가 끝내 안 나갔으면 사용자는 대기 주문의
            # 존재를 모르고, 60초 안의 다음 명령이 영문 모를 충돌로 막힌다 (#247).
            try:
                await self.pending_orders.delete(chat_id)
            except Exception as exc:
                logger.error("제안 프롬프트 미전달 후 대기 주문 정리 실패: %s", exc)

    async def _handle_order_command(
        self, side: OrderSide, readings: list[OrderReading], chat_id: str
    ) -> None:
        """주문 확인 대기를 만든다. ``readings``는 인자를 읽을 수 있는 해석 전부다 (#387).

        /buy·/sell은 _order_argument_readings가 위치로 가능한 해석을 모두 내고, 자연어 주문은
        ``주``·``원`` 표지로 이미 확정한 해석 하나를 넘긴다. 어느 해석으로 주문을 만들지는
        _resolve_order_reading이 종목 마스터로 정한다 — 두 경로가 같은 판정을 지난다.
        """
        usage = BUY_COMMAND_HELP if side == "BUY" else SELL_COMMAND_HELP
        if not readings:
            await self._send_text_or_raise(usage)
            return

        now = self.now_factory()
        if not is_korean_market_open(now):
            await self._send_text_or_raise(
                "주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"
            )
            return

        try:
            await self._drop_expired_pending_order(chat_id, now)
            has_pending = await self.pending_orders.has(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if has_pending:
            await self._send_text_or_raise(PENDING_ORDER_CONFLICT_TEXT)
            return

        await self.notifier.send_chat_action("typing")
        try:
            chosen = await self._resolve_order_reading(side, readings)
            if chosen is None:
                # 두 해석이 모두 실재 종목이다. 안내는 이미 나갔다.
                return
            reading, resolved = chosen
            stock_name = reading.stock_name
            quantity = reading.quantity
            price = reading.price
            order_type = reading.order_type
            stock_code = self._extract_stock_code(str(resolved))
            # 사용자가 코드를 직접 입력해도 해석된 종목명을 쓴다. 원문(stock_name)을 그대로
            # 넣으면 name == code가 되어 정상 종목에도 미해석 경고가 뜬다(#139 리뷰).
            resolved_name = self._extract_stock_name(str(resolved)) or stock_name
            if stock_code is None:
                await self._send_text_or_raise("주문 준비 실패: 종목코드를 확인할 수 없습니다.")
                return
            # 추출 성공 != 존재 확인. stock-master.js Step 3는 마스터에 없는 코드 형태
            # 입력을 market="UNKNOWN"으로 그대로 에코하므로("999999 (999999, UNKNOWN)")
            # 코드 추출은 성공하고 아래 숫자 6~7자 검사도 통과한다. 백엔드가 여기서
            # 끊지 않으면 실재 여부 확인이 KIS 왕복이나 /confirm 시점 브로커 거절로
            # 미뤄진다 — 리포트 저장 경로(services._resolve_stock_code)는 이미 같은
            # 판정으로 막고 있으므로, 위험도가 더 높은 주문 경로도 같은 선에서 막는다.
            if _is_unresolved_echo(str(resolved)):
                await self._send_text_or_raise(
                    f"주문 불가: {stock_name}({stock_code}) — 종목마스터에 없는 종목입니다. "
                    "종목코드를 확인하거나 mcp/trading/data/stocks.json을 갱신하세요."
                )
                return
            # 추출 성공 != 주문 가능. mcp/trading/order.js의 buildCashOrderBody()가
            # 기본값으로는 숫자 코드만 주문을 받으므로(#73에서 확정된 정책, #138에서
            # KIS_ALNUM_STOCK_ORDER_ENABLED 플래그로 완화 가능해짐), 시세·잔고 조회와
            # 60초 대기 슬롯을 쓰기 전에 여기서 끊는다. 이 검사가 없으면 /confirm
            # 이후에야 같은 사유로 실패한다.
            if not is_orderable_stock_code(stock_code):
                # 사용자가 입력한 건 종목명인데 거절 사유는 종목코드 형태다.
                # 코드를 함께 보여주지 않으면 인과가 보이지 않는다.
                # 조사(은/는)는 코드 끝자리 받침에 따라 갈리므로 아예 쓰지 않는다.
                # 상품군을 단정하지 않는다(#138). 이전 문구는 특정 상품군(ETN, 펀드)을
                # 예로 들었는데 두 번 부정확했다 — 차단되는 776종목의 상당수는 그 상품군이
                # 아니라 보통주·우선주이고, 하필 ETN은 KIS order-cash 스펙이 PDNO 대상으로
                # 명시한 상품이다.
                # 막는 근거는 상품 종류가 아니라 코드 형태이므로 그것만 말한다.
                # 허용 형태 문장은 판정 함수와 같은 모듈에서 받아 온다 — 여기 리터럴로
                # 두면 위 가드는 플래그를 보는데 안내만 보지 않아, 플래그를 켠 프로세스가
                # 8자 코드 사용자에게 "숫자 6~7자리만"이라고 잘못 답한다(#138 리뷰).
                await self._send_text_or_raise(
                    f"주문 불가: {stock_name}({stock_code}) — 현재 주문을 지원하는 "
                    f"종목코드 형태가 아닙니다. {describe_orderable_code_policy()}"
                )
                return
            quote_result, balance_result = await asyncio.gather(
                self.mcp_runner(
                    TRADING_MCP_PARAMS,
                    "get_stock_quote",
                    {"stock_name": stock_name},
                ),
                self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {}),
            )
        except TelegramSendError:
            # 위 검증 실패 메시지(두 해석 모호(#387)·종목코드 미확인·미등록 종목·주문 불가 코드)의
            # 전송이 실패한 경우다. 전송 실패는 "이 명령을 처리하다 생긴 오류"가 아니라 "사용자에게
            # 말을 걸 수 없는 상태"라 사용자 메시지로 변환하는 것 자체가 무의미하다.
            #
            # 이 분기가 막는 것은 폴러가 실패를 못 보는 것이 아니다 — 변환한 메시지가
            # 실패하면 그것도 TelegramSendError라 어차피 폴러에 도달한다. 실제로 막는 것은
            # 변환한 메시지가 성공했을 때 "주문 준비 실패: telegram send failed"라는
            # 무의미한 중복 메시지가 사용자에게 한 번 더 가는 것이다 (PR #253 2차 리뷰).
            #
            # 이 지점은 아직 부수효과가 없어 재시도가 안전하다 (#249).
            raise
        except Exception as exc:
            await self._send_text_or_raise(f"주문 준비 실패: {_short_error(exc)}")
            return

        if order_type == "MARKET":
            # 시장가에는 지정가가 없지만, 단가를 모르는 채로 두면 그 0이 체결 기록
            # (TradeHistory.price)까지 그대로 내려가 일 거래대금 한도를 무력화한다(#309).
            # KIS 현금주문 응답에는 체결가가 없으므로(order-cash output은 ODNO·ORD_TMD뿐)
            # 방금 프롬프트용으로 받아 둔 현재가를 기록용 참고단가로 쓴다. /advise는 이미
            # 같은 값을 같은 자리에 넣는다(order_assist.run_order_assist).
            #
            # 읽지 못해도 주문은 막지 않는다 — 사용자가 명시적으로 낸 주문을 기록 사정으로
            # 되돌리는 것이기 때문이다. 대신 0이 그대로 남고, load_daily_usage의 가드가
            # 그날 /advise를 usage_failed로 막는다. 한도가 조용히 넓어지는 것보다 그쪽이
            # 낫다는 것이 #309의 결론이다.
            reference_price = parse_current_price(str(quote_result))
            if reference_price is None:
                logger.warning(
                    "시장가 참고단가를 읽지 못했다 (stock=%s) — 이 주문이 확정되면 단가 "
                    "없이 기록되고, 그때부터 오늘 /advise가 일 거래대금 집계 실패로 "
                    "막힌다 (#309)",
                    stock_code,
                )
            else:
                price = reference_price

        order = PendingOrder(
            chat_id=chat_id,
            stock_name=resolved_name,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            # MARKET이면 표시·기록용 참고단가(주문 시점 현재가)다. 주문 자체에는 쓰이지
            # 않는다 — McpTradingOrderGateway가 시장가에는 price=0을 보낸다.
            price=price,
            # now(명령 수신 시각)가 아니라 저장 직전 시각으로 스탬프한다. 위 MCP 조회는
            # run_mcp_tool의 wait_for(30초)를 두 구간(resolve, gather) 쓰므로 최대 60초가
            # 걸리고, now를 쓰면 프롬프트가 도착하기도 전에 만료 시각이 지나 있다 —
            # 절대 시각 표기가 "과거 시각에 만료됩니다"가 된다 (PR #253 2차 리뷰).
            # now는 장 운영 판정과 만료 스윕에 그대로 쓴다.
            created_at=self.now_factory(),
            order_type=order_type,
            callback_token=secrets.token_urlsafe(8),
            # 사용자가 방금 낸 명령이다 (/buy·/sell·자연어 주문). 텍스트 /confirm으로 확정할 수
            # 있는 출처다 — 명령과 /confirm이 같은 기기에서 순서대로 전송되기 때문이다 (#390).
            origin="user_command",
        )
        try:
            stored = await self.pending_orders.set_if_absent(chat_id, order)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장 실패: {_short_error(exc)}")
            return
        if not stored:
            # MCP 호출 사이에 같은 chat에서 /buy가 먼저 체결된 경우(또는 자동 제안이 슬롯을 잡은 경우)
            await self._send_text_or_raise(PENDING_ORDER_CONFLICT_TEXT)
            return
        # 대기 주문이 저장된 뒤다 — 재실행은 has_pending에 걸려 "이미 대기 중인 주문이
        # 있습니다"로 끝나고, 사용자는 확인 버튼을 영영 받지 못한다 (#247).
        # 프롬프트의 message_id는 여기서 주문에 남는다. 그 전까지 텍스트 /confirm은 이 주문을
        # 실행하지 않는다(#386).
        notified = await self._send_order_prompt(
            order, self._format_order_prompt(order, str(quote_result), str(balance_result))
        )
        if not notified:
            # 프롬프트가 끝내 안 나갔으면 사용자는 대기 주문의 존재를 모른다. 그대로 두면
            # 60초 안의 다음 /buy가 영문 모를 "이미 대기 중인 주문이 있습니다"로 막힌다.
            # 아직 아무것도 체결되지 않았으므로 지우는 쪽이 안전하다 (PR #253 2차 리뷰).
            try:
                await self.pending_orders.delete(chat_id)
            except Exception as exc:
                logger.error("프롬프트 미전달 후 대기 주문 정리 실패: %s", exc)

    async def _handle_cancel(self, chat_id: str) -> None:
        try:
            await self._drop_expired_pending_order(chat_id, self.now_factory())
            has_pending = await self.pending_orders.has(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if not has_pending:
            await self._send_text_or_raise("취소할 대기 주문이 없습니다.")
            return
        try:
            await self.pending_orders.delete(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        # 대기 주문이 삭제된 뒤다 — 재실행은 "취소할 대기 주문이 없습니다"로 끝난다 (#247).
        await self._send_text_settled("대기 주문을 취소했습니다.")

    async def _handle_confirm(self, chat_id: str, *, text_confirm: _TextConfirm | None = None) -> None:
        """대기 주문을 claim해 실행한다.

        ``text_confirm``은 텍스트 /confirm에서만 넘어온다. 막으려는 규칙은 "메시지를 보낸 뒤 생긴 주문은
        그 메시지로 확정할 수 없다"이다. claim(GETDEL)은 **같은** 주문을 두 번 주지 않을 뿐,
        /confirm이 그사이 생긴 **다른** 대기 주문(자동 제안·새 /buy)을 받는 것은 막지 않는다.
        그러면 사용자가 본 적도 없는 주문이 확정 없이 실행된다. 이 규칙은 두 창에서 깨졌다.

        - 재시작 뒤의 재실행(#383): 주문 A를 claim하고 결과 전송 도중 죽으면 같은 update가
          재배달되고, 그사이 생긴 B를 claim한다.
        - 늦게 처리된 처음 실행(#386): 폴러는 update를 순서대로 처리한다. 그래서 LLM 대기·재시도
          대기 뒤에 줄 선 /confirm이나 봇이 내려가 있던 동안 쌓인 /confirm은 늦게 처리된다.
          그사이 A가 60초 앱 만료를 넘기면 자동 제안이 A를 치우고 B를 넣는다.
        - 사용자 쪽 전송 지연(#390): 텔레그램 클라이언트는 연결이 끊긴 동안 보낸 메시지를 로컬
          큐에 두었다가 재연결 때 올린다. A를 보고 누른 /confirm이 큐에 묶인 사이 A가 만료되고
          자동 제안 B의 프롬프트가 먼저 message_id를 받으면, 늦게 올라간 /confirm의 id가 더 크다.

        판정은 PendingOrder.confirmable_by_text 하나이고 조건이 둘이다.

        1. **출처가 사용자 명령일 것** (#390). 사용자가 모르는 사이에 대기 주문이 생기는 출처는
           자동 제안뿐이라 위 세 창이 모두 거기서 열린다. 자동 제안 주문은 텍스트로 확정하지
           않고 확정 버튼으로만 확정한다 — 버튼은 주문마다 다른 토큰에 묶여 있어 애초에 다른
           주문을 확정할 수 없다. 사용자가 낸 /buy·/sell·자연어 주문·/advise는 같은 기기에서
           /confirm과 순서대로 전송되므로(오프라인 큐도 보낸 순서를 지킨다) 이 역전이 성립하지
           않는다. 기각한 대안은 "프롬프트에 단 답장만 확정으로 인정"이다 — 사용자가 매번
           답장을 써야 하고, 원인이 자동 제안 쪽에만 있는데 사용자 주문의 텍스트 확정까지
           없앤다(#390 결정 코멘트).
        2. **프롬프트 message_id 대조** (#386). 대기 주문은 자기 확정 프롬프트의 message_id를
           들고 있고(PendingOrder.prompt_message_id), 텍스트 /confirm은 자기 message_id가 그보다
           클 때만 claim한다. 한 채팅의 message_id는 봇과 사용자 메시지를 합쳐 단조 증가하므로
           "프롬프트가 나간 뒤에 보낸 /confirm"이 곧 이 대조다. 시계를 쓰지 않는다. message.date와
           created_at을 비교하면 텔레그램·호스트 시계 오차가 오판 방향을 정하고, 폴러의 수신
           시각과 비교하면 적체 중에는 수신이 항상 주문 생성보다 늦어 그 경우를 가려내지 못한다.
           이 대조는 사용자 주문 안에서도 순서를 지킨다 — 재실행된 /confirm의 id는 그사이 생긴
           주문의 프롬프트 id보다 작고, /buy 처리 중에 미리 보낸 /confirm도 여기 걸린다.

        판정과 claim은 claim_if 한 번에 원자적으로 끝난다. 판정에 걸린 주문은 꺼내지 않으므로 B는
        소비되지 않고 남는다. 사용자는 B의 프롬프트를 보고 버튼으로 확정·취소한다.

        fail-closed: 출처를 모르면(필드가 없는 기존 저장값, 목록 밖의 값) 자동 제안으로 읽어 막는다.
        프롬프트 id를 모르면(전송은 됐지만 id를 못 읽음, 기록 실패, 저장부터 기록까지의 창) 또는
        /confirm의 message_id를 모르면 실행하지 않는다. 저장부터 기록까지의 창은 폴러 밖에서 만든
        주문(자동 제안)에만 열리고, 그 주문은 이제 출처 판정에서 먼저 막힌다. 폴러 안의
        /buy·/advise는 id를 기록한 뒤에야 다음 update로 넘어간다.

        #383의 update 표지는 그대로 둔다. 재실행은 이제 id 대조에도 걸리지만, 표지는 주문을 보지
        않고 update만으로 판정하는 별도 층이다. 표지는 owner(프로세스)를 담아 프로세스 안의
        재시도(전송 실패 뒤 폴러 재시도)는 그대로 통과시킨다.

        버튼 콜백은 ``text_confirm``을 넘기지 않아 세 판정 모두 걸리지 않는다. 버튼에는 주문마다 다른
        토큰이 실려 있어 _handle_order_callback이 다른 주문의 확정을 이미 거절한다.

        남는 경우: 사용자 주문 쪽 근거는 "명령과 /confirm이 한 기기에서 순서대로 나간다"이다. 같은
        계정을 두 기기에서 동시에 쓰면서 한 기기의 /confirm이 오프라인 큐에 묶인 사이 다른 기기에서
        새 /buy를 내면, 늦게 올라간 /confirm이 그 /buy를 확정할 수 있다. 그 주문은 사용자가 방금
        직접 낸 것이고 프롬프트도 그 기기에 떠 있어, 자동 제안처럼 "존재를 모르는 주문"이 나가는
        경우와는 피해의 종류가 다르다. 이 창은 수용했다(#390). 사용법 변화도 남는다 — 자동 제안
        주문은 텍스트 /confirm으로 확정할 수 없고 확정 버튼(또는 /cancel)을 쓴다.
        """
        # order_gateway 부재 체크를 claim 전에 수행해 주문이 소비되지 않게 한다.
        if self.order_gateway is None:
            await self._send_text_or_raise("주문 실행 설정이 준비되지 않았습니다.")
            return
        update_id = text_confirm.update_id if text_confirm is not None else None
        if update_id is not None:
            try:
                first_run = await self.pending_orders.mark_confirm_update(
                    chat_id, update_id, self._confirm_owner
                )
            except Exception as exc:
                # 판정하지 못하면 실행하지 않는다. claim과 같은 저장소라 이 오류는 곧 claim의
                # 오류이기도 하다.
                await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
                return
            if not first_run:
                logger.warning(
                    "재시작 전에 처리를 시작한 /confirm update %s가 재배달돼 실행하지 않는다",
                    update_id,
                )
                await self._send_text_or_raise(CONFIRM_REPLAY_REFUSED_TEXT)
                return
        refused: PendingOrder | None = None
        try:
            await self._drop_expired_pending_order(chat_id, self.now_factory())
            # 원자적 읽기+삭제. 멀티워커 경합이나 프로세스 안의 재시도에서 정확히 하나의 호출만
            # order를 받고 나머지는 None을 받는다. 재시작 뒤 재배달은 위 표지가 먼저 걸러낸다(#383).
            if text_confirm is None:
                order = await self.pending_orders.claim(chat_id)
            else:
                # 텍스트 /confirm은 사용자가 낸 주문이고(#390) 그 주문의 프롬프트를 본 뒤에 보낸
                # 것일 때만(#386) 꺼낸다. 두 조건은 confirmable_by_text 하나에 있다.
                confirm_message_id = text_confirm.message_id
                outcome = await self.pending_orders.claim_if(
                    chat_id, lambda pending: pending.confirmable_by_text(confirm_message_id)
                )
                order = outcome.order if outcome.claimed else None
                refused = None if outcome.claimed else outcome.order
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if refused is not None:
            # 주문은 저장소에 그대로 있다. 재시도해도 같은 판정에 도달하므로 재시도 가능한 전송이다.
            if not refused.text_confirm_allowed():
                # 자동 제안(또는 출처를 모르는 저장값)이다. 프롬프트 id를 알든 모르든 텍스트로는
                # 확정되지 않으므로 id 대조 안내가 아니라 버튼 안내를 보낸다 (#390).
                logger.warning(
                    "텍스트 /confirm(message_id=%s)이 자동 제안 대기 주문(origin=%s)에 닿아 "
                    "실행하지 않는다 — 확정 버튼으로만 확정한다 (#390)",
                    text_confirm.message_id if text_confirm is not None else None,
                    refused.origin,
                )
                await self._send_text_or_raise(CONFIRM_AUTO_PROPOSAL_BUTTON_ONLY_TEXT)
                return
            logger.warning(
                "텍스트 /confirm(message_id=%s)이 대기 주문의 프롬프트(message_id=%s)보다 앞서거나 "
                "순서를 알 수 없어 실행하지 않는다 (#386)",
                text_confirm.message_id if text_confirm is not None else None,
                refused.prompt_message_id,
            )
            unknown = (
                refused.prompt_message_id is None or text_confirm is None or text_confirm.message_id is None
            )
            await self._send_text_or_raise(
                CONFIRM_PROMPT_UNKNOWN_TEXT if unknown else CONFIRM_BEFORE_PROMPT_TEXT
            )
            return
        if order is None:
            await self._send_text_or_raise("확정할 대기 주문이 없습니다.")
            return

        await self.notifier.send_chat_action("typing")
        try:
            result = await self.order_gateway.place_order(order)
        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 403:
                # 403 = 실계좌 가드 미충족: 주문 미실행이 확실하므로 대기 주문 복원.
                # created_at을 유지하므로 앱 레벨 60초 만료는 그대로 적용된다.
                # set_if_absent: 복원 도중 새 /buy가 들어온 경우 새 주문을 보호한다.
                restored = False
                try:
                    restored = await self.pending_orders.set_if_absent(chat_id, order)
                except Exception as put_exc:
                    logger.error("pending_order 복원 실패 (403 후): %s", put_exc)
                else:
                    if not restored:
                        logger.warning(
                            "403 복원 생략 — 그 사이 새 대기 주문이 생성됨: %s", chat_id
                        )
                message = f"주문 실패: {_short_error(exc)}"
                if restored:
                    # 대기 주문이 claim 이전 상태로 돌아갔다. 재실행하면 같은 403으로
                    # 같은 메시지에 도달하므로 재시도가 안전하다 — 단 만료 창이 남아 있는
                    # 동안만이다. 전송 실패 창(300초)이 주문 만료(60초)의 5배라, 60초를
                    # 넘겨 재시도하면 _drop_expired_pending_order가 먼저 지워 "확정할 대기
                    # 주문이 없습니다"가 나가고 원래 거절 사유는 유실된다. 403은 미체결이
                    # 확실해 위험하지는 않다 (PR #253 2차 리뷰).
                    #
                    # 이 안전은 프로세스 안의 재시도에만 해당한다. 재시작을 넘긴 재시도(예산은
                    # #350으로 살아남는다)는 owner가 달라 #383 표지에 걸려, 사유 대신 재실행
                    # 안내로 끝난다. 실행하지 않는 쪽이라 안전하고 복원된 주문은 그대로 남는다.
                    await self._send_text_or_raise(message)
                else:
                    # 복원에 실패했거나 새 주문이 선점했다 — 재실행은 "확정할 대기 주문이
                    # 없습니다"로 끝나 원래 사유를 전하지 못한다 (#247).
                    await self._send_text_settled(message)
                return

            # 주문 실행 결과 불명확: claim으로 이미 삭제됨 — 추가 delete 불필요.
            # 재실행은 claim이 비어 "확정할 대기 주문이 없습니다"로 끝나므로 확인 요청이
            # 사라진다 (#247).
            await self._send_text_settled(
                "주문 실패 또는 상태 확인 필요: "
                f"{_short_error(exc)}\n"
                "중복 주문 방지를 위해 대기 주문을 제거했습니다."
            )
            return

        # 주문 성공: claim으로 이미 삭제됨 — 추가 delete 불필요.
        #
        # 여기부터가 체결 통지 outbox다 (#259 2단계). 재실행은 "확정할 대기 주문이
        # 없습니다"로 끝나므로(#247) 이 통지는 전송 실패로 사라지면 안 되는데, 그 보장을
        # 이 자리의 재시도로 만들지 않는다. _send_text_settled는 최대
        # SETTLED_SEND_TIMEOUT_SECONDS(20초) 동안 폴러 루프를 붙잡고, 그동안 같은 배치에서
        # 재시도를 기다리는 update가 예산(UPDATE_RETRY_WINDOW_SECONDS, 60초)을 통째로 잃는다.
        # 상한을 60초 아래로 내려도 닫히지 않는다 — 그러면 429를 흡수한다는 재시도의 목적이
        # 사라진다.
        #
        # 대신 순서를 기록 → 전송 → 마킹으로 둔다. 체결이 먼저 원장에 남고(notified_at =
        # null) 통지 책임이 outbox로 넘어가므로 전송은 한 번만 시도하면 된다. 실패하면
        # 미통지 행이 남고 scheduler.trade_notification.trade_notification_task가 다음 주기에 다시 알린다.
        # 순서를 뒤집어 전송을 먼저 하면 그 사이의 죽음이 곧 무응답이다.
        try:
            trade_id = self.trade_recorder.record(result)
        except Exception as exc:
            # 원장이 없으면 outbox도 없다 — 재배달할 근거가 남지 않는다. 그래서 이 경우에만
            # 예전처럼 그 자리에서 재시도한다. 폴러를 붙잡는 대가를 다시 무는 대신, 이
            # 메시지가 사용자가 받을 유일한 통지가 되기 때문이다.
            #
            # 기록 실패를 경고 문자열로만 덧붙이던 예전 동작은 outbox가 서는 순간 위험해진다.
            # 그때는 "기록 실패"가 곧 "통지가 조용히 사라지는 경로"인데, 그 사실이 성공
            # 메시지 뒤에 붙는 한 줄로만 드러났다.
            logger.error("체결 이력 기록 실패 — outbox 대상에서 빠진다: %s", exc)
            await self._send_text_settled(
                f"주문 완료: {result.message}"
                f"\n거래 이력 기록 실패: {_short_error(exc)}"
                "\n이 메시지가 유일한 통지입니다 — 받지 못했다면 증권사 앱에서 확인하세요."
            )
            return

        # 여기서 update를 확정한다 (#259 3단계). 이 아래에서 죽어도 체결은 원장에 미통지로
        # 남아 outbox가 배달하므로 update를 다시 실행할 이유가 없다 — 재실행은 claim이 비어
        # "확정할 대기 주문이 없습니다"로 체결을 오표시할 뿐이다. 그래서 전송보다 먼저 폴러가
        # offset을 영속화하게 한다.
        #
        # 대가: 영속화 대기가 전송 뒤에서 앞으로 옮겨 왔다. redis가 멈추면 "주문 완료"가
        # 최대 STATE_STORE_TIMEOUT_SECONDS(3초)만큼 늦게 나간다(PR #376 리뷰). 상한이 있고
        # 저장 실패는 삼켜지므로 통지 자체를 막지는 않는다.
        #
        # 기록 뒤여야 한다. 위 기록 실패 경로는 원장이 없어 먼저 확정하면 그 경로에서 죽을 때
        # 무응답이 되므로 확정하지 않는다. 나머지 settled 경로(/buy 프롬프트·/cancel·403·
        # 불명확)도 같은 이유로 여기에 넣지 않았다 — 되살릴 원장이 없는 메시지에서 먼저
        # 영속화하면 오표시 한 줄을 무응답과 바꾸는 거래가 된다.
        #
        # 그 경로들의 재실행 창은 수용했다(#383). 재시작이 settled 전송(최대 20초)과 겹칠 때만
        # 열리고 피해는 오표시 한 줄로 유계다. 수용하지 않은 것은 재실행된 텍스트 /confirm이
        # 그사이 생긴 다른 대기 주문을 실행하는 경우 하나이고, 이 메서드 첫머리의 표지와 프롬프트
        # id 대조(#386)가 막는다.
        await self._mark_update_settled()

        try:
            sent = await self.notifier.send_text(f"주문 완료: {result.message}")
        except Exception as exc:
            # 예외를 여기서 받는 이유는 재실행 방지가 아니다 — 위에서 이미 확정했으므로 예외가
            # 올라가도 폴러는 COMMITTED로 받아 재실행하지 않는다(#259 3단계). 받는 이유는
            # 실패의 흔적이다: 올려 보내면 폴러의 일반 로그 한 줄로 끝나 trade_id와
            # delivery_metrics 집계가 남지 않는다. 실제 notifier는 실패를 False로 접어 오므로
            # 아래 sent is False 분기와 같은 기록을 남긴다.
            #
            # CancelledError는 BaseException이라 여기 걸리지 않는다. 폴러의 graceful
            # shutdown이 막히지 않고, 그 경우 행은 미통지로 남아 재시작 뒤 배달된다.
            logger.error(
                "체결 통지 전송 중 예외 — outbox 재배달 대기 (trade_id=%s): %s", trade_id, exc
            )
            delivery_metrics.record_failure("fill_notify", trade_id=trade_id)
            return
        if sent is False:
            # 여기서 끝내도 통지가 사라지지는 않는다. notified_at이 비어 있는 행이 곧
            # 재배달 대기열이다. 그래서 이 자리에는 알람을 걸지 않고 횟수만 센다 (#259
            # 5단계). 알람은 재배달마저 계속 실패할 때 scheduler.trade_notification.trade_notification_task가
            # 울린다 — 사용자가 실제로 아무것도 못 받고 있는 것은 그때다.
            logger.error("체결 통지 전송 실패 — outbox 재배달 대기 (trade_id=%s)", trade_id)
            delivery_metrics.record_failure("fill_notify", trade_id=trade_id)
            return

        try:
            self.trade_recorder.mark_notified(trade_id, notified_at=self.now_factory())
        except Exception as exc:
            # 전송은 이미 나갔다. 마킹만 실패하면 다음 주기가 같은 체결을 한 번 더 알린다 —
            # 중복이지만 무응답보다 낫고, 재배달 문구가 재전송임을 밝힌다. asyncio.timeout
            # 만료처럼 "보냈는지 모르는" 경우도 같은 결말이라 여기만의 문제가 아니다.
            logger.error("체결 통지 마킹 실패 — 중복 배달 가능 (trade_id=%s): %s", trade_id, exc)
            delivery_metrics.record_failure("fill_mark", trade_id=trade_id)

    def _order_argument_readings(self, argument: str) -> list[OrderReading]:
        """/buy·/sell 인자를 위치로 읽을 수 있는 해석을 **전부** 돌려준다 (#387).

        형식은 ``<종목명> <수량> [지정가]``이다. 끝 토큰이 양의 정수가 아니면 해석이 없다(빈 목록).

        끝 두 토큰이 모두 양의 정수면 해석이 둘이다(이름 자리가 종목코드 하나면 지정가 하나뿐 —
        아래 분기 참조). 예전에는 무조건 지정가로 읽어, 종목명이
        숫자로 끝나는 ``/buy KODEX 200 10``이 ``KODEX`` 200주 지정가 10원이 됐다.

        1. 지정가: 앞부분이 종목명, 끝에서 둘째가 수량, 끝이 지정가.
        2. 시장가: 끝에서 둘째까지가 종목명, 끝이 수량.

        여기서는 고르지 않는다. 종목명이 실제로 해석되는지는 파서가 알 수 없으므로
        _resolve_order_reading이 종목 마스터로 정한다. 순서는 예전 해석(지정가)이 먼저이고,
        둘 다 해석되지 않을 때 실패 사유를 이 첫 해석으로 알린다(예전과 같은 안내).
        """
        parts = argument.split()
        if len(parts) < 2:
            return []

        last_value = self._parse_positive_int(parts[-1])
        if last_value is None:
            return []

        readings: list[OrderReading] = []
        previous_value = self._parse_positive_int(parts[-2]) if len(parts) >= 3 else None
        if previous_value is not None:
            readings.append(
                OrderReading(
                    stock_name=" ".join(parts[:-2]),
                    quantity=previous_value,
                    price=last_value,
                    order_type="LIMIT",
                )
            )
            if len(parts) == 3 and _looks_like_stock_code(parts[0]):
                # 이름 자리가 종목코드 하나면(`/buy 005930 10 75000`) 시장가 해석을 만들지 않는다
                # (PR #392 리뷰). "종목코드 + 숫자"는 종목명이 될 수 없어 조회가 헛돌고, 미등록 코드면
                # 두 이름 미발견 안내가 "'999999 10' 시장가로 읽은 경우"·"종목코드로 입력하세요"를
                # 이미 코드를 친 사용자에게 보낸다. 하나로 두면 예전 안내("종목마스터에 없는 종목")다.
                # _looks_like_stock_code는 숫자가 든 6·7·9자만 코드로 보므로 KIWOOM·HANARO 같은
                # 영문 이름은 여기 걸리지 않는다. 마스터에 "코드 형태 + 숫자" 이름이 없다는 전제는
                # 커밋된 stocks.json을 test_no_master_name_is_a_stock_code_followed_by_a_number가,
                # 운영자의 직접 갱신을 mcp/trading/scripts/update_stock_master.py의 쓰기 전 검사가
                # 막는다. 이 분기를 바꾸면 공유 판정표 unreadable_numeric_name_policy.json을 함께 고친다.
                return readings
        readings.append(
            OrderReading(
                stock_name=" ".join(parts[:-1]),
                quantity=last_value,
                price=0,
                order_type="MARKET",
            )
        )
        return readings

    async def _resolve_order_reading(
        self, side: OrderSide, readings: list[OrderReading]
    ) -> tuple[OrderReading, str] | None:
        """해석 중 종목 마스터가 실제로 해석하는 것 하나와 그 resolve_stock_code 응답을 고른다 (#387).

        해석마다 종목명을 resolve_stock_code로 확인한다(동시 호출). 판정은 fail-closed다.

        1. 둘 이상이 실재 종목으로 해석되면 추측하지 않는다. 두 해석을 종목코드와 함께 보여 주고
           종목코드로 다시 입력하라고 안내한 뒤 None — 주문을 만들지 않는다.
        2. 어느 해석이 "종목 없음"이 아닌 이유(연결 실패·타임아웃·마스터 안의 이름 중복 등)로
           확인되지 않으면 그 예외를 올린다. 모호함이 없다는 것을 증명하지 못했기 때문이다.
           호출부는 이것을 "주문 준비 실패"로 알린다.
        3. 하나만 해석되면(나머지는 "종목 없음"이 확실) 그 해석이다.
        4. 아무것도 해석되지 않았을 때: 해석이 둘이면 두 종목명을 함께 적어 "찾지 못했다"고 알리고
           None이다(PR #392 리뷰). 해석이 하나면 그 결과를 돌려준다 — 호출부가 예전과 같은 사유
           (종목코드 미확인·미등록 종목)로 알리고, 예외였으면("종목 없음") 여기서 올린다.

        "해석됨"은 응답에서 종목코드를 뽑을 수 있고 UNKNOWN 에코가 아닌 것이다. "종목 없음"은
        코드를 못 뽑았거나, UNKNOWN 에코이거나, resolveStock의 미발견 오류(STOCK_NOT_FOUND_ERROR_MARKER)다.

        해석이 하나뿐이면(끝 토큰 하나만 정수, 자연어 주문) 1·3이 성립할 수 없고 결과는 예전과 같다.
        """
        results = await asyncio.gather(
            *(
                self.mcp_runner(
                    TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": reading.stock_name}
                )
                for reading in readings
            ),
            return_exceptions=True,
        )
        for result in results:
            # CancelledError 같은 BaseException은 판정 대상이 아니다. 삼키면 종료가 막힌다.
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result

        found: list[tuple[OrderReading, str]] = []
        for reading, result in zip(readings, results):
            if isinstance(result, BaseException):
                continue
            raw = str(result)
            if self._extract_stock_code(raw) is not None and not _is_unresolved_echo(raw):
                found.append((reading, raw))

        if len(found) >= 2:
            logger.warning(
                "주문 인자가 두 실재 종목으로 읽혀 주문을 만들지 않는다 (#387): %s",
                [(reading.stock_name, raw) for reading, raw in found],
            )
            await self._send_text_or_raise(self._format_ambiguous_order_readings(side, found))
            return None

        for result in results:
            if isinstance(result, Exception) and not self._is_stock_not_found_error(result):
                raise result

        if found:
            return found[0]

        if len(readings) >= 2:
            # 해석이 둘인데 어느 이름도 종목이 아니다. 지정가 해석의 이름("Kodex")만 알리면 사용자가
            # 입력한 종목명("Kodex 200")이 아닌 것을 찾지 못했다고 읽힌다(PR #392 리뷰). 둘 다 적는다.
            await self._send_text_or_raise(self._format_unresolved_order_readings(readings))
            return None

        first = results[0]
        if isinstance(first, Exception):
            raise first
        return readings[0], str(first)

    def _is_stock_not_found_error(self, exc: Exception) -> bool:
        """resolve_stock_code가 "마스터에 그런 종목이 없다"로 실패했는가 (#387)."""
        return STOCK_NOT_FOUND_ERROR_MARKER in str(getattr(exc, "detail", exc))

    def _format_ambiguous_order_readings(
        self, side: OrderSide, found: list[tuple[OrderReading, str]]
    ) -> str:
        """두 해석이 모두 실재 종목일 때의 안내 (#387).

        해석마다 종목명·종목코드·수량·가격을 보여 주고, 그 해석을 종목코드로 쓴 명령을 붙인다.
        종목코드 명령은 이름 끝 숫자가 없어 다시 모호해지지 않는다.
        """
        command = "/buy" if side == "BUY" else "/sell"
        lines = ["주문 불가: 입력이 두 가지로 읽혀 주문을 만들지 않았습니다."]
        for reading, raw in found:
            code = self._extract_stock_code(raw) or ""
            name = self._extract_stock_name(raw) or reading.stock_name
            if reading.order_type == "LIMIT":
                detail = f"{reading.quantity:,}주, 지정가 {reading.price:,}원"
                example = f"{command} {code} {reading.quantity} {reading.price}"
            else:
                detail = f"{reading.quantity:,}주, 시장가"
                example = f"{command} {code} {reading.quantity}"
            lines.append(f"- {name}({code}) {detail} → {example}")
        lines.append("원하는 주문을 종목코드로 다시 입력하세요.")
        return "\n".join(lines)

    def _format_unresolved_order_readings(self, readings: list[OrderReading]) -> str:
        """해석이 둘인데 어느 종목명도 종목 마스터에 없을 때의 안내 (PR #392 리뷰).

        ``/buy Kodex 200 10``(대소문자 오타)은 "Kodex"와 "Kodex 200" 모두 미발견이다. 한 이름만 적으면
        사용자가 입력하지 않은 이름을 찾지 못했다고 읽히므로, 해석마다 종목명과 읽은 뜻을 함께 적는다.
        """
        lines = ["주문 준비 실패: 입력한 종목을 종목 마스터에서 찾지 못했습니다."]
        for reading in readings:
            if reading.order_type == "LIMIT":
                detail = f"{reading.quantity:,}주, 지정가 {reading.price:,}원으로 읽은 경우"
            else:
                detail = f"{reading.quantity:,}주, 시장가로 읽은 경우"
            lines.append(f"- '{reading.stock_name}' ({detail})")
        lines.append("종목명(대소문자·띄어쓰기)을 확인하거나 6자리 종목코드로 입력하세요.")
        return "\n".join(lines)

    def _looks_like_natural_order(self, text: str) -> bool:
        return (
            ("매수" in text or "매도" in text)
            and re.search(r"\d[\d,]*\s*주", text) is not None
        )

    def _parse_natural_order_text(self, text: str) -> tuple[OrderSide, OrderReading] | None:
        """자연어 주문에서 (방향, 해석)을 뽑는다.

        수량은 ``주``, 지정가는 ``원`` 표지로 갈리므로 해석이 하나로 정해진다. 뽑은 값을 문자열로
        다시 이어 붙여 /buy 파서에 넘기면 안 된다 — ``KODEX 200 10주 매수``가 ``KODEX 200 10``이
        되어 위치 해석의 모호함을 새로 만든다(#387). 해석 그대로 넘긴다.
        """
        buy_count = text.count("매수")
        sell_count = text.count("매도")
        if buy_count + sell_count != 1:
            return None
        side: OrderSide = "BUY" if buy_count == 1 else "SELL"

        quantity_match = re.search(r"(?P<quantity>\d[\d,]*)\s*주", text)
        if quantity_match is None:
            return None
        quantity = self._parse_positive_int(quantity_match.group("quantity"))
        if quantity is None:
            return None

        has_market = "시장가" in text
        price_matches = list(re.finditer(r"(?P<price>\d[\d,]*)\s*원", text))
        if has_market and price_matches:
            return None
        price = 0
        if price_matches:
            price = self._parse_positive_int(price_matches[-1].group("price")) or 0
            if price <= 0:
                return None

        stock_name = text[: quantity_match.start()].strip()
        if not stock_name:
            side_word = "매수" if side == "BUY" else "매도"
            side_index = text.find(side_word)
            if side_index < quantity_match.start():
                stock_name = text[
                    side_index + len(side_word) : quantity_match.start()
                ].strip()
        if not stock_name:
            return None

        return side, OrderReading(
            stock_name=stock_name,
            quantity=quantity,
            price=price,
            order_type="LIMIT" if price > 0 else "MARKET",
        )

    def _parse_positive_int(self, raw_value: str) -> int | None:
        try:
            value = int(raw_value.replace(",", ""))
        except ValueError:
            return None
        return value if value > 0 else None

    async def _drop_expired_pending_order(self, chat_id: str, now: datetime) -> None:
        order = await self.pending_orders.get(chat_id)
        if order is None:
            return
        created_at = order.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=KST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=KST)
        if now.astimezone(KST) - created_at.astimezone(KST) > ORDER_EXPIRES_AFTER:
            await self.pending_orders.delete(chat_id)

    def _extract_stock_code(self, text: str) -> str | None:
        match = _STOCK_CODE_EXTRACT_RE.search(text)
        return match.group(1) if match else None

    def _extract_stock_name(self, text: str) -> str | None:
        # 규칙은 stock_code.extract_stock_name 하나가 소유한다 — order_assist도 같은
        # 것을 쓴다. 두 곳이 각자 자르면 같은 종목이 화면마다 다른 이름으로 남는다.
        return extract_stock_name(text)

    def _extract_order_callback_token(self, data: str) -> str | None:
        if data in {ORDER_CONFIRM_CALLBACK, ORDER_CANCEL_CALLBACK}:
            return None
        return self._extract_callback_token(data)

    def _trade_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🛒 매수 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}buy"},
                    {"text": "💸 매도 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}sell"},
                ]
            ]
        }

    def _order_reply_markup(self, order: PendingOrder) -> dict[str, Any]:
        # 실제 조립은 trading_orders.order_reply_markup 하나뿐이다 (#314). 스케줄러의
        # 자동 제안이 같은 버튼을 써야 해서 옮겼고, 여기서는 호출부 이름만 유지한다.
        return order_reply_markup(order)

    def _format_order_prompt(
        self,
        order: PendingOrder,
        quote_result: str,
        balance_result: str,
    ) -> str:
        side_text = "매수" if order.side == "BUY" else "매도"
        lines = [
            f"{order.stock_name} {side_text} 주문 확인",
            f"종목코드: {order.stock_code}",
            f"수량: {order.quantity:,}주",
            f"주문유형: {'시장가' if order.order_type == 'MARKET' else '지정가'}",
        ]
        if order.order_type == "LIMIT":
            amount = order.quantity * order.price
            lines.extend(
                [
                    f"지정가: {order.price:,}원",
                    f"주문금액: {amount:,}원",
                ]
            )
        elif order.price > 0:
            # 시장가에도 참고단가가 잡히면서 금액을 셀 수 있게 됐다(#309). 돈이 나가는
            # 확인 단계에서 금액만 빠져 있을 이유가 없다 — 일 거래대금 한도에 가산될
            # 값도 이것이다. 체결가가 아니라 주문 시점 현재가 기준이므로 "예상"이고,
            # 제안 경로의 승인 메시지도 같은 라벨을 쓴다 (PR #323 리뷰).
            lines.append(f"예상 주문금액: {order.quantity * order.price:,}원")

        # 기록용 참고단가와 **같은 파서**로 읽는다. 원문 줄을 그대로 집어 오면 표시와
        # 기록이 서로 다른 라벨 집합을 보게 되고("price:"는 여기서만 매치된다), 라벨이
        # 바뀌는 날 사용자는 확인 화면에서 현재가를 보는데 기록은 0("금액 모름")이 되는
        # 어긋남이 생긴다. 하나로 묶어 두면 둘이 함께 사라진다 (PR #323 리뷰).
        current_price = parse_current_price(str(quote_result))
        if current_price is not None:
            lines.append(f"현재가: {current_price:,}원")

        balance = self._first_line_containing(
            str(balance_result),
            # get_balance가 실제로 내는 라벨은 "예수금"이다(mcp/trading/balance.js).
            # 나머지는 다른 잔고 응답이 섞여 들어올 때를 위한 방어선이다 — 이 프롬프트가
            # 보여주는 것은 주문가능금액이 아니라 예수금이라는 점을 유의한다(#310).
            ("주문가능", "예수금", "총자산", "balance"),
        )
        if balance:
            # mcp-trading은 목록 항목을 "- 라벨: 값"으로 낸다. 그 줄을 그대로 붙이면
            # 직접 조립한 위 줄들(종목코드:, 수량:, 현재가: …)과 표기가 어긋난다.
            # 접두사만 떼어 제안 경로의 승인 메시지와 같은 표기로 맞춘다 (PR #323 리뷰).
            lines.append(balance.removeprefix("- "))

        # 미해석 에코(name == code)는 이제 주문 준비 단계에서 _is_unresolved_echo가
        # 끊으므로 여기까지 오지 않는다(#151). 마스터에 name == code인 항목이 생기는
        # 경우에 대비한 방어선으로만 남긴다 — 현재 마스터 4,401종에는 0건이다.
        if order.stock_name == order.stock_code:
            lines.append(UNRESOLVED_STOCK_WARNING)

        # 만료를 "60초 후"로 쓰면 메시지가 언제 도착하든 60초를 약속하게 된다. 실제로는
        # created_at이 MCP 조회 전(_handle_order_command의 now = self.now_factory())에 찍히고,
        # 전송이 429로 밀리면 _send_text_settled가 SETTLED_SEND_TIMEOUT_SECONDS까지 더 쓴다
        # — 사용자가 확인 버튼을 받는 시점엔 이미 상당히 지나 있다.
        # 절대 시각은 도착이 늦어도 어긋나지 않는다.
        expires_at = order.created_at + ORDER_EXPIRES_AFTER
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=KST)
        lines.extend(
            [
                "",
                "/confirm 입력 시 대기 주문을 확정합니다.",
                "/cancel 입력 시 대기 주문을 취소합니다.",
                f"이 주문은 {expires_at.astimezone(KST):%H:%M:%S}에 만료됩니다.",
            ]
        )
        return "\n".join(lines)
