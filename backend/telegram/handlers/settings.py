"""설정 명령(/start /alerts /level /risk)."""

import logging
import time
from typing import Any

from backend.ai.mcp_tools import short_error as _short_error
from backend.ai.user_preferences import (
    normalize_risk_choice,
    RISK_AGGRESSIVE,
    RISK_CLEAR,
    RISK_CONSERVATIVE,
    RISK_PROFILE_LABELS,
    telegram_user_id,
    UserMemoryDisabled,
)
from backend.telegram.handlers.base import _level_cache_put, HandlerBase, LEVEL_CACHE_TTL_SECONDS
from backend.telegram.handlers.texts import (
    ALERT_CALLBACK_PREFIX,
    ALERT_COMMAND_HELP,
    ALERT_MODE_EMOJIS,
    LEVEL_CALLBACK_PREFIX,
    LEVEL_COMMAND_HELP,
    RISK_CALLBACK_PREFIX,
    RISK_COMMAND_HELP,
    RISK_MEMORY_DISABLED_TEXT,
    START_MESSAGE,
    START_RISK_MESSAGE,
)
from backend.telegram.notifier import TELEGRAM_ALERT_MODES
from backend.telegram.presentation import (
    LEVEL_BEGINNER,
    LEVEL_INTERMEDIATE,
    level_label,
    normalize_level,
)

logger = logging.getLogger(__name__)


class SettingsCommands(HandlerBase):
    """설정 명령(/start /alerts /level /risk)."""

    async def _handle_alerts_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(ALERT_CALLBACK_PREFIX).strip()
        if action != "status" and action not in TELEGRAM_ALERT_MODES:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._handle_alerts(action)

    async def _handle_alerts(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0].lower() if parts else "status"
        async with self._state() as state:
            if action == "status":
                mode = await state.get_telegram_alert_mode()
                await self._send_text_or_raise(
                    f"현재 Telegram 알림 모드: {self._format_alert_mode(mode)}",
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self._send_text_or_raise(
                    ALERT_COMMAND_HELP,
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            await state.set_telegram_alert_mode(action)
            await self._send_text_or_raise(
                f"Telegram 알림 모드가 {self._format_alert_mode(action)}(으)로 변경되었습니다.",
                reply_markup=self._alerts_reply_markup(),
            )

    async def _handle_start(self) -> None:
        """봇 소개 + 수준 1문항 + 투자 성향 1문항. /start는 텔레그램이 첫 대화에서 자동으로 보내는
        명령이다 (#297, #397).

        여기서 아무것도 저장하지 않는다 — 버튼을 누르지 않고 넘어간 사용자도 기본값(초보, 성향
        미설정)으로 동작해야 한다. 저장은 버튼 콜백이나 /level·/risk에서만 일어난다. 그래서 성향
        문항은 NAT을 부르지 않는다: NAT이 내려가 있어도 /start 자체는 실패하지 않는다.
        """
        await self._send_text_or_raise(
            START_MESSAGE,
            reply_markup=self._level_reply_markup(),
        )
        await self._send_text_or_raise(
            START_RISK_MESSAGE,
            reply_markup=self._risk_reply_markup(),
        )

    async def _handle_risk(self, argument: str) -> None:
        """/risk 안정형|공격형|해제. 인자가 없으면 현재 설정을 버튼과 함께 보여준다 (#397).

        /level과 같은 모양이다. 다른 점은 저장소가 redis가 아니라 NAT 사용자 선호 메모리라는 것
        하나다(backend/ai/user_preferences.py). 그래서 캐시를 두지 않는다 — 이 값을 매 메시지마다
        읽는 쪽은 backend가 아니라 NAT이다.
        """
        parts = argument.split()
        if not parts:
            await self._run_risk_request(None)
            return
        choice = normalize_risk_choice(parts[0])
        if choice is None:
            await self._send_text_or_raise(RISK_COMMAND_HELP, reply_markup=self._risk_reply_markup())
            return
        await self._run_risk_request(choice)

    async def _handle_risk_callback(self, callback_query_id: str, data: str) -> None:
        choice = normalize_risk_choice(data.removeprefix(RISK_CALLBACK_PREFIX))
        if choice is None:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return
        await self._answer_callback_query(callback_query_id)
        await self._run_risk_request(choice)

    async def _run_risk_request(self, choice: str | None) -> None:
        """조회(None)·저장·해제를 NAT에 보내고 결과를 알린다.

        NAT 실패는 사용자 메시지로 바꾸고 삼킨다. 재시도 가능한 예외로 올리면 update 재시도가 같은
        쓰기를 반복한다 — 멱등이긴 하지만 사용자는 같은 안내를 여러 번 받는다. 저장에 실패했는데
        성공 문구가 나가는 경우는 없다: 성공 문구는 NAT이 돌려준 **적용 뒤의 값**으로만 만든다.
        """
        user_id = telegram_user_id(self.notifier.chat_id)
        try:
            profile = await self.risk_profile_client(user_id, choice)
        except UserMemoryDisabled:
            await self._send_text_or_raise(RISK_MEMORY_DISABLED_TEXT)
            return
        except Exception as exc:  # noqa: BLE001 — NAT 연결·응답 오류 전부 같은 안내
            logger.warning("투자 성향 요청 실패 (choice=%s): %s", choice, exc)
            await self._send_text_or_raise(
                f"투자 성향을 처리하지 못했습니다: {_short_error(exc)}",
                reply_markup=self._risk_reply_markup(),
            )
            return
        await self._send_text_or_raise(
            self._risk_result_message(choice, profile),
            reply_markup=self._risk_reply_markup(),
        )

    def _risk_result_message(self, choice: str | None, profile: str | None) -> str:
        if profile is None:
            if choice == RISK_CLEAR:
                return "투자 성향을 해제했습니다. 종목 추천을 성향 구분 없이 합니다."
            return f"현재 투자 성향: 설정 안 함 (종목 추천을 성향 구분 없이 합니다)\n{RISK_COMMAND_HELP}"
        label = RISK_PROFILE_LABELS[profile]
        detail = (
            "종목 추천에서 변동성 위험을 먼저 짚어 드립니다."
            if profile == RISK_CONSERVATIVE
            else "종목 추천에서 기회 요인을 먼저 짚어 드립니다."
        )
        if choice is None:
            return f"현재 투자 성향: {label}. {detail}\n{RISK_COMMAND_HELP}"
        return f"투자 성향을 {label}(으)로 저장했습니다. {detail}"

    async def _handle_level(self, argument: str) -> None:
        """/level 초보|중급. 인자가 없으면 현재 설정을 버튼과 함께 보여준다 (#297).

        /alerts와 같은 모양을 일부러 지켰다 — 두 명령 다 "이 채팅의 표시 설정"이고,
        인자 없이 치면 현재 상태가 나오는 규칙이 명령마다 다르면 사용자가 외울 것이 늘어난다.
        """
        parts = argument.split()
        if not parts:
            level = await self._current_level()
            await self._send_text_or_raise(
                f"현재 설명 수준: {level_label(level)}\n{LEVEL_COMMAND_HELP}",
                reply_markup=self._level_reply_markup(),
            )
            return

        requested = normalize_level(parts[0])
        if requested is None:
            await self._send_text_or_raise(
                LEVEL_COMMAND_HELP,
                reply_markup=self._level_reply_markup(),
            )
            return

        await self._apply_level(requested)

    async def _apply_level(self, level: str) -> None:
        async with self._state() as state:
            await state.set_telegram_user_level(level)
        # 저장이 성공한 뒤에만 캐시를 갱신한다. 먼저 갱신하면 저장에 실패한 값이 이 프로세스
        # 안에서만 적용된 것처럼 보이고, 재시작 후 조용히 되돌아간다.
        _level_cache_put(level, time.monotonic() + LEVEL_CACHE_TTL_SECONDS)
        await self._send_text_or_raise(
            self._level_changed_message(level),
            reply_markup=self._level_reply_markup(),
        )

    def _level_changed_message(self, level: str) -> str:
        if level == LEVEL_BEGINNER:
            detail = "낯선 용어가 나오면 한 줄 설명을 붙여 드립니다."
        else:
            detail = "용어 설명 없이 내용만 보냅니다."
        return f"설명 수준을 {level_label(level)}(으)로 바꿨습니다. {detail}"

    async def _handle_level_callback(self, callback_query_id: str, data: str) -> None:
        requested = normalize_level(data.removeprefix(LEVEL_CALLBACK_PREFIX).strip())
        if requested is None:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return
        await self._answer_callback_query(callback_query_id)
        await self._apply_level(requested)

    def _level_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "🌱 처음이에요",
                        "callback_data": f"{LEVEL_CALLBACK_PREFIX}{LEVEL_BEGINNER}",
                    },
                    {
                        "text": "📈 좀 해봤어요",
                        "callback_data": f"{LEVEL_CALLBACK_PREFIX}{LEVEL_INTERMEDIATE}",
                    },
                ]
            ]
        }

    def _risk_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🛡️ 안정형", "callback_data": f"{RISK_CALLBACK_PREFIX}{RISK_CONSERVATIVE}"},
                    {"text": "🚀 공격형", "callback_data": f"{RISK_CALLBACK_PREFIX}{RISK_AGGRESSIVE}"},
                    {"text": "↩️ 설정 안 함", "callback_data": f"{RISK_CALLBACK_PREFIX}{RISK_CLEAR}"},
                ]
            ]
        }

    def _alerts_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🚨 긴급만", "callback_data": f"{ALERT_CALLBACK_PREFIX}urgent"},
                    {"text": "📣 전체", "callback_data": f"{ALERT_CALLBACK_PREFIX}all"},
                    {"text": "🔕 끄기", "callback_data": f"{ALERT_CALLBACK_PREFIX}off"},
                ],
                [
                    {"text": "🔎 현재 상태", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"}
                ],
            ]
        }

    def _format_alert_mode(self, mode: str) -> str:
        emoji = ALERT_MODE_EMOJIS.get(mode)
        if emoji is None:
            return mode
        return f"{emoji} {mode}"
