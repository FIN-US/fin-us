"""텔레그램 명령 진입점: 묶음별 핸들러를 조립하고 업데이트를 각 묶음으로 보낸다."""

from typing import Any

from backend.ai.mcp_tools import short_error as _short_error
from backend.telegram.handlers.base import (
    _nat_answer_message,
    _telegram_command_parts,
    _telegram_int,
    _TextConfirm,
)
from backend.telegram.handlers.lookup import LookupCommands
from backend.telegram.handlers.orders import OrderCommands
from backend.telegram.handlers.settings import SettingsCommands
from backend.telegram.handlers.texts import (
    ALERT_CALLBACK_PREFIX,
    BALANCE_REFRESH_CALLBACK,
    LEVEL_CALLBACK_PREFIX,
    LOOKUP_CALLBACK_PREFIX,
    LOOKUP_COMMAND_HELP,
    MARKET_QUOTE_CALLBACK,
    MARKET_TREND_CALLBACK,
    MARKET_TREND_DETAIL_CALLBACK,
    NAT_PROGRESS_MESSAGE,
    NATURAL_ORDER_HELP,
    RISK_CALLBACK_PREFIX,
    TELEGRAM_INTERACTIVE_HELP,
    TRADE_CALLBACK_PREFIX,
    TRADE_COMMAND_HELP,
    WATCH_LIST_CALLBACK,
)
from backend.trading.orders import ORDER_CANCEL_CALLBACK, ORDER_CONFIRM_CALLBACK


class TelegramCommandHandler(OrderCommands, LookupCommands, SettingsCommands):

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query") or {}
        if callback_query:
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")).strip() != self.notifier.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        command, bot_username, argument = _telegram_command_parts(text)
        if self._matches_command(command, bot_username, "/alerts"):
            await self._handle_alerts(argument)
            return
        if self._matches_command(command, bot_username, "/level"):
            await self._handle_level(argument)
            return
        if self._matches_command(command, bot_username, "/risk"):
            await self._handle_risk(argument)
            return
        if self._matches_command(command, bot_username, "/start"):
            await self._handle_start()
            return
        if self._matches_command(command, bot_username, "/help"):
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/balance"):
            await self._handle_balance()
            return
        if self._matches_command(command, bot_username, "/watch"):
            await self._handle_watch(argument)
            return
        if self._matches_command(command, bot_username, "/catalysts"):
            await self._handle_catalysts(argument)
            return
        if self._matches_command(command, bot_username, "/trade"):
            await self._send_text_or_raise(
                TRADE_COMMAND_HELP,
                reply_markup=self._trade_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/lookup"):
            await self._send_text_or_raise(
                LOOKUP_COMMAND_HELP,
                reply_markup=self._lookup_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/visualize"):
            await self._handle_visualize()
            return
        if self._matches_command(command, bot_username, "/quote"):
            await self._handle_quote(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/trend"):
            await self._handle_trend(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/earnings"):
            await self._handle_earnings(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/advise"):
            await self._handle_advise(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/buy"):
            await self._handle_order_command(
                "BUY", self._order_argument_readings(argument), str(chat.get("id", "")).strip()
            )
            return
        if self._matches_command(command, bot_username, "/sell"):
            await self._handle_order_command(
                "SELL", self._order_argument_readings(argument), str(chat.get("id", "")).strip()
            )
            return
        if self._matches_command(command, bot_username, "/confirm"):
            await self._handle_confirm(
                str(chat.get("id", "")).strip(),
                text_confirm=_TextConfirm(
                    update_id=_telegram_int(update.get("update_id")),
                    message_id=_telegram_int(message.get("message_id")),
                ),
            )
            return
        if self._matches_command(command, bot_username, "/cancel"):
            await self._handle_cancel(str(chat.get("id", "")).strip())
            return
        if text.startswith("/"):
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return

        if self._looks_like_natural_order(text):
            natural_order = self._parse_natural_order_text(text)
            if natural_order is None:
                await self._send_text_or_raise(NATURAL_ORDER_HELP)
                return

            side, reading = natural_order
            await self._handle_order_command(
                side,
                [reading],
                str(chat.get("id", "")).strip(),
            )
            return

        await self._handle_chat_fallback(text, str(chat.get("id", "")).strip())

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id != self.notifier.chat_id:
            return

        callback_query_id = str(callback_query.get("id", "")).strip()
        data = str(callback_query.get("data") or "").strip()
        if data == ORDER_CONFIRM_CALLBACK or data.startswith(f"{ORDER_CONFIRM_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "confirm",
                data,
            )
            return
        if data == ORDER_CANCEL_CALLBACK or data.startswith(f"{ORDER_CANCEL_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "cancel",
                data,
            )
            return

        if data.startswith(LEVEL_CALLBACK_PREFIX):
            await self._handle_level_callback(callback_query_id, data)
            return
        if data.startswith(RISK_CALLBACK_PREFIX):
            await self._handle_risk_callback(callback_query_id, data)
            return
        if data.startswith(ALERT_CALLBACK_PREFIX):
            await self._handle_alerts_callback(callback_query_id, data)
            return

        if data == BALANCE_REFRESH_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_balance()
            return

        if data == WATCH_LIST_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_watch("list")
            return

        if data.startswith(TRADE_CALLBACK_PREFIX):
            await self._handle_trade_callback(callback_query_id, data)
            return

        if data.startswith(LOOKUP_CALLBACK_PREFIX):
            await self._handle_lookup_callback(callback_query_id, data)
            return

        if data.startswith(f"{MARKET_QUOTE_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "quote", data)
            return

        # 접두어가 겹치므로 긴 쪽(market:trend_detail)을 먼저 본다. 순서가 바뀌면 상세
        # 버튼이 요약 경로로 떨어져 같은 메시지가 두 번 나간다.
        if data.startswith(f"{MARKET_TREND_DETAIL_CALLBACK}:"):
            await self._handle_market_callback(
                callback_query_id, chat_id, "trend_detail", data
            )
            return

        if data.startswith(f"{MARKET_TREND_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "trend", data)
            return

        await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")

    async def _handle_chat_fallback(self, text: str, chat_id: str) -> None:
        await self.notifier.send_chat_action("typing")
        # #260: NAT 응답까지 수십 초가 걸린다. typing 액션은 5초 뒤 사라지므로
        # 접수 즉시 진행 메시지를 남기고, 응답이 오면 그 메시지를 치운 뒤 답변을
        # 새 메시지로 보낸다(편집은 푸시 알림을 발생시키지 않는다).
        progress_message_id = await self._send_progress_message(NAT_PROGRESS_MESSAGE)
        # NAT 호출 전에 읽는다. 호출 뒤로 미루면 수십 초짜리 LLM 왕복이 끝난 다음에 redis
        # 왕복이 한 번 더 붙어 진행 메시지가 그만큼 더 오래 남는다.
        level = await self._current_level()
        try:
            result = await self.llm_runner(
                "nat",
                text,
                conversation_id=f"telegram:{chat_id}",
            )
        except Exception as exc:
            # 통지보다 먼저 치운다 — 아래 전송이 끝내 실패해도 "분석 중"이 채팅에 남지
            # 않는다 (#260).
            await self._clear_progress_message(progress_message_id)
            # 실패 통지도 답변과 같은 settled 전송이다 (#259 4단계, #275). 재시도 가능한
            # 전송으로 두면 전송 실패가 update 재시도로 번져 이 메서드가 처음부터 다시 돈다 —
            # 진행 메시지가 새로 나가고 LLM이 다시 호출된다(실패가 타임아웃이면 이미 과금됐을
            # 수 있다). 대가는 LLM 실패와 통지 전송 최종 실패가 겹칠 때 사용자가 실패 사실을
            # 못 받는 것인데, 주문 상태를 오인할 메시지가 아니고 재질문으로 복구된다. 그 한
            # 건은 settled_send 메트릭에 남는다.
            await self._send_text_settled(f"응답 생성 실패: {_short_error(exc)}")
            return
        # LLM 호출이 끝난 뒤다. 재실행은 같은 conversation_id로 NAT를 다시 호출해 대화
        # 이력을 오염시키고 예산만큼 재과금된다 — 전송 실패 예산으로는 최대 10회다 (#247).
        await self._clear_progress_message(progress_message_id)
        # _nat_answer_message(→ presentation.render)는 길이를 보지 않고 전체 문장을
        # 조립하고, _send_text_settled가 상한에 맞춰 나눠 보낸다 (#260, #297, #313).
        # 각주는 마지막 조각에 통째로 실린다 — render 독스트링 참조.
        await self._send_text_settled(_nat_answer_message(result, level, text))
