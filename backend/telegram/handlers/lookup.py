"""조회 명령(/balance /quote /trend /watch /catalysts /earnings)과 시세 버튼."""

import asyncio
import re
import secrets
from typing import Any

from urllib.parse import quote as _url_quote

from backend.ai.mcp_tools import short_error as _short_error
from backend.core.config import DART_MCP_PARAMS, NEWS_MCP_PARAMS, TRADING_MCP_PARAMS
from backend.core.timeutil import KST
from backend.pii.egress import public, Segment
from backend.telegram.handlers.base import HandlerBase
from backend.telegram.handlers.texts import (
    BALANCE_REFRESH_CALLBACK,
    CATALYST_COMMAND_HELP,
    EARNINGS_COMMAND_HELP,
    LOOKUP_CALLBACK_PREFIX,
    LOOKUP_COMMAND_HELP,
    MARKET_CALLBACK_LIMIT,
    MARKET_QUOTE_CALLBACK,
    MARKET_STALE_CALLBACK_TEXT,
    MARKET_TREND_CALLBACK,
    MARKET_TREND_DETAIL_CALLBACK,
    QUOTE_COMMAND_HELP,
    TREND_COMMAND_HELP,
    WATCH_COMMAND_HELP,
    WATCH_LIST_CALLBACK,
    WATCH_LIST_QUOTE_DELAY_SECONDS,
)
from backend.telegram.handlers.trend import (
    _format_trend_detail,
    _format_trend_summary,
    _parse_investor_flows,
    trend_detail_button_text,
)
from backend.telegram.presentation import (
    as_list_items,
    KIND_ANALYSIS,
    KIND_QUOTE,
    render,
    sanitize_markdown,
)


class LookupCommands(HandlerBase):
    """조회 명령(/balance /quote /trend /watch /catalysts /earnings)과 시세 버튼."""

    async def _handle_lookup_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(LOOKUP_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = LOOKUP_COMMAND_HELP
            reply_markup = self._lookup_reply_markup()
        elif action == "quote":
            text = QUOTE_COMMAND_HELP
            reply_markup = None
        elif action == "trend":
            text = TREND_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_market_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        token = self._extract_callback_token(data)
        # pop이 아니라 get이다. pop하면 조회 결과 전송이 실패했을 때 폴러 재시도가
        # MARKET_STALE_CALLBACK_TEXT로 끝나 방금 누른 버튼에 "이전 조회 버튼입니다"가 뜬다.
        # 토큰 소비는 되돌릴 수 있는 부수효과라 확정시킬 이유가 없다 — 전송이 성공한 뒤에
        # 소비해 재시도 경계를 전송 뒤로 옮긴다 (PR #253 2차 리뷰).
        context = self.market_callbacks.get(token) if token else None
        # token이 falsy면 context는 반드시 None이므로 판정 결과는 그대로다.
        # token을 함께 보면 아래 pop(token)에서 str | None이 str로 좁혀진다.
        if token is None or context is None:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        callback_chat_id, stock = context
        if callback_chat_id != chat_id:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "quote":
            await self._handle_quote(stock, chat_id)
        else:
            await self._handle_trend(stock, chat_id, detail=action == "trend_detail")
        # 전송이 성공한 뒤에만 소비한다. 위에서 TelegramSendError가 나면 여기 도달하지
        # 않으므로 재시도가 같은 토큰으로 같은 조회를 다시 수행한다.
        self.market_callbacks.pop(token, None)

    async def _handle_watch(self, argument: str) -> None:
        parts = argument.split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        stock = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "list":
            watchlist = await self.watchlist_repo.get_watchlist()
            if not watchlist:
                text = "관심 종목이 없습니다.\n/watch add <종목명> 으로 추가하세요."
            else:
                lines = []
                for index, stock in enumerate(watchlist):
                    if index > 0:
                        await asyncio.sleep(WATCH_LIST_QUOTE_DELAY_SECONDS)
                    try:
                        raw = await self.mcp_runner(
                            TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock}
                        )
                        summary = self._parse_quote_summary(str(raw))
                        if summary:
                            price, rate = summary
                            try:
                                rate_float = float(rate.rstrip("%"))
                            except ValueError:
                                line = f"• {stock}  (조회 실패)"
                                lines.append(line)
                                continue
                            if rate_float < 0:
                                line = f"🔵 {stock}  {price}  ▼ {rate}"
                            elif rate_float == 0.0:
                                line = f"⬜ {stock}  {price}  {rate}"
                            else:
                                sign = "" if rate.startswith("+") else "+"
                                line = f"🔴 {stock}  {price}  ▲ {sign}{rate}"
                        else:
                            line = f"• {stock}  (조회 실패)"
                    except Exception:
                        line = f"• {stock}  (조회 실패)"
                    lines.append(line)
                text = "관심 종목 목록:\n" + "\n".join(lines)
            await self._send_text_or_raise(text, reply_markup=self._watch_reply_markup())
            return

        if subcommand == "add":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.add_to_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에 {stock}을(를) 추가했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        if subcommand == "remove":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.remove_from_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에서 {stock}을(를) 삭제했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        await self._send_text_or_raise(WATCH_COMMAND_HELP)

    async def _handle_catalysts(self, argument: str) -> None:
        stock = argument.strip()
        if not stock:
            await self._send_text_or_raise(CATALYST_COMMAND_HELP)
            return

        today = self.now_factory().astimezone(KST).date()
        events = await self.catalyst_repo.list_upcoming(stock, today=today, limit=20)
        if not events:
            await self._send_text_or_raise(
                f"{stock} 예정 이벤트가 없습니다.\n"
                "이벤트는 관심 종목 대상 스케줄러가 수집한 뒤 표시됩니다."
            )
            return

        items = [
            f"{event.event_date.isoformat()}  {getattr(event, 'description', '')}"
            f" ({self._format_catalyst_type(getattr(event, 'event_type', ''))})"
            for event in events
        ]
        # 제목은 목록 밖이다. 글머리표는 "•"가 아니라 이 봇의 나열 표시를 쓴다 (#297 자가리뷰).
        lines = [f"📅 {stock} 예정 이벤트", *as_list_items(items)]
        await self._send_text_or_raise(
            render(
                "\n".join(lines),
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            )
        )

    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(
            render(result, KIND_QUOTE, await self._current_level()),
            reply_markup=self._balance_reply_markup(),
        )

    async def _handle_quote(self, argument: str, chat_id: str) -> None:
        if not argument:
            await self._send_text_or_raise(QUOTE_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_stock_quote",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(
            render(
                result,
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            ),
            reply_markup=self._market_reply_markup("trend", chat_id, stock),
        )

    def _format_catalyst_type(self, event_type: str) -> str:
        return {
            "earnings": "실적",
            "dividend": "배당",
            "disclosure": "공시",
            "agm": "주주총회",
        }.get(event_type, event_type or "기타")

    def _balance_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🔄 새로고침", "callback_data": BALANCE_REFRESH_CALLBACK}]
            ]
        }

    def _watch_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📋 목록 새로고침", "callback_data": WATCH_LIST_CALLBACK}]
            ]
        }

    def _lookup_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "💵 현재가 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}quote",
                    },
                    {
                        "text": "📊 수급 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}trend",
                    },
                ]
            ]
        }

    def _market_reply_markup(
        self,
        action: str,
        chat_id: str,
        stock: str,
        *,
        detail_days: int = 0,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(8)
        if len(self.market_callbacks) >= MARKET_CALLBACK_LIMIT:
            self.market_callbacks.pop(next(iter(self.market_callbacks)), None)
        self.market_callbacks[token] = (chat_id, stock)
        quote_button = {
            "text": "💵 현재가 보기",
            "callback_data": f"{MARKET_QUOTE_CALLBACK}:{token}",
        }
        if action == "quote":
            return {"inline_keyboard": [[quote_button]]}
        if action == "trend_detail":
            # 수급 요약 아래에는 상세가 먼저다. 방금 받은 메시지에 이어지는 동작이라
            # 다른 종류의 조회(현재가)보다 앞에 온다 (#297 검수 4차).
            return {
                "inline_keyboard": [
                    [
                        {
                            "text": trend_detail_button_text(detail_days),
                            "callback_data": f"{MARKET_TREND_DETAIL_CALLBACK}:{token}",
                        }
                    ],
                    [quote_button],
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 수급 보기",
                        "callback_data": f"{MARKET_TREND_CALLBACK}:{token}",
                    }
                ]
            ]
        }

    def _parse_quote_summary(self, raw: str) -> tuple[str, str] | None:
        price_line = self._first_line_containing(raw, ("현재가:",))
        rate_line = self._first_line_containing(raw, ("전일 대비:",))
        if not price_line or not rate_line:
            return None
        price_match = re.search(r"현재가:\s*(.+)", price_line)
        rate_match = re.search(r"\(([^)]+%)\)", rate_line)
        if not price_match or not rate_match:
            return None
        return price_match.group(1).strip(), rate_match.group(1).strip()

    async def _handle_trend(self, argument: str, chat_id: str, *, detail: bool = False) -> None:
        """수급 조회. 기본은 요약, ``detail``이면 전체 일수를 세로로 (#297 검수 4차).

        두 경로가 같은 MCP 호출을 각자 한 번씩 한다. 요약할 때 상세까지 만들어 들고 있지
        않는 이유는, 그러려면 버튼을 누를 때까지 원문을 어딘가 보관해야 하고 그 저장소가
        market_callbacks처럼 또 하나의 만료·용량 관리 대상이 되기 때문이다. 조회 버튼이
        이미 재조회로 동작하고 있어(현재가·수급 버튼) 규칙도 그쪽과 같아진다.
        """
        if not argument:
            await self._send_text_or_raise(TREND_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_investor_trading",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return

        flows = _parse_investor_flows(str(result))
        if not flows:
            # 형식이 바뀌어 파싱이 깨졌다. 원문을 그대로 보내고 상세 버튼은 달지 않는다 —
            # 눌러 봐야 같은 원문이 한 번 더 갈 뿐이다 (#297 검수 2차).
            body = str(result)
            markup = self._market_reply_markup("quote", chat_id, stock)
        elif detail:
            body = _format_trend_detail(stock, flows)
            markup = self._market_reply_markup("quote", chat_id, stock)
        else:
            body = _format_trend_summary(stock, flows)
            markup = self._market_reply_markup(
                "trend_detail", chat_id, stock, detail_days=len(flows)
            )
        await self._send_text_or_raise(
            render(
                body,
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            ),
            reply_markup=markup,
        )

    async def _handle_earnings(self, argument: str, chat_id: str) -> None:
        parsed = self._parse_earnings_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(EARNINGS_COMMAND_HELP)
            return

        stock, period = parsed
        # LLM 호출 전에 읽는다 — 뒤로 미루면 수십 초짜리 왕복이 끝난 다음 redis 왕복이
        # 한 번 더 붙는다 (_handle_chat_fallback과 같은 이유).
        level = await self._current_level()
        dart_arguments: dict[str, Any] = {"stock_name": stock}
        if period:
            dart_arguments["period"] = period

        await self.notifier.send_chat_action("typing")
        try:
            dart_result, news_result = await asyncio.gather(
                self.mcp_runner(DART_MCP_PARAMS, "get_earnings_report", dart_arguments),
                self.mcp_runner(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock}),
            )
            result = await self.llm_runner(
                "nat",
                self._earnings_analysis_prompt(stock, period, str(dart_result), str(news_result)),
                conversation_id=f"telegram:{chat_id}:earnings:{_url_quote(stock, safe='')}",
            )
        except Exception as exc:
            # 답변과 같은 이유로 settled다 (#259 4단계, PR #377 리뷰). 재시도 가능한 전송이면
            # 전송 실패가 update 재시도로 번져 DART·뉴스 조회와(실패가 LLM 쪽이었다면) LLM
            # 호출까지 되풀이한다.
            await self._send_text_settled(f"조회 실패: {_short_error(exc)}")
            return

        # LLM 호출이 끝난 뒤다 — 재실행은 DART·뉴스 조회와 LLM 호출을 그대로 반복해
        # 예산만큼 재과금된다 (#247).
        await self._send_text_settled(
            render(
                self._format_earnings_response(str(result)),
                KIND_ANALYSIS,
                level,
                question=argument,
            )
        )

    def _parse_earnings_argument(self, argument: str) -> tuple[str, str | None] | None:
        parts = argument.split()
        if not parts:
            return None

        period = None
        if re.match(r"^\d{4}(?:[-_\s]?Q[1-4]|[-_\s]?FY)$", parts[-1], flags=re.IGNORECASE):
            period = parts[-1].upper().replace("-", "").replace("_", "")
            parts = parts[:-1]

        stock = " ".join(parts).strip()
        if not stock:
            return None
        return stock, period

    def _earnings_analysis_prompt(
        self,
        stock: str,
        period: str | None,
        dart_result: str,
        news_result: str,
    ) -> list[str | Segment]:
        # 공개/개인 구분(#395): DART 실적과 뉴스는 공개 데이터라 수치를 그대로 보낸다 — 이
        # 명령은 매출·영업이익 증감을 읽는 것이 목적이라, 마스킹하면 분석이 성립하지 않는다.
        # 종목·기간은 사용자가 친 인자라 개인 구간(맨 문자열)으로 둔다. 기준은
        # backend/pii/egress.py 모듈 docstring.
        period_line = f"조회 기간: {period}" if period else "조회 기간: DART MCP 기본값"
        return [
            "News Analyst 실적 분석 모드로 다음 종목의 구조화된 실적 리포트를 작성하라.\n"
            f"종목: {stock}\n"
            f"{period_line}\n\n"
            "[DART 실적 데이터]\n",
            public(dart_result),
            "\n\n[최신 뉴스]\n",
            public(news_result),
            "\n\n반드시 다음 항목을 포함하라:\n"
            "- 매출/영업이익/순이익 전년 동기 대비 증감\n"
            "- 컨센서스 대비 서프라이즈/미스 판단. 컨센서스 데이터가 없으면 추정하지 말고 데이터 없음으로 표시\n"
            "- 주요 뉴스 기반 정성적 코멘트\n"
            "- 다음 분기 전망\n"
            "첫 줄은 반드시 `호재`, `악재`, `중립` 중 하나의 판정과 한 줄 근거로 시작하라.\n"
            "Markdown 문법(`#`, `**`, 표, 코드블록)을 쓰지 말고 Telegram에서 읽기 쉬운 일반 텍스트로 답하라.\n"
            "투자 조언은 단정하지 말고, 확인된 DART·뉴스 근거와 한계를 구분해 한국어로 답하라.",
        ]

    def _format_earnings_response(self, text: str) -> str:
        # 출력 계층의 정리기를 쓴다. 예전에는 이 클래스가 자체 정리기(_telegram_plain_text)를
        # 갖고 있었는데, 그쪽은 머리글·굵게·글머리표만 알아서 링크·기울임·취소선·이스케이프를
        # 그대로 흘렸고 글머리표도 "•"로 바꿔 나머지 메시지와 어긋났다 (#297 자가리뷰).
        # 판정 접두어를 붙이려면 정리된 첫 줄을 봐야 해서 render보다 먼저 한 번 부른다 —
        # sanitize_markdown은 이미 정리된 문장에 다시 걸어도 같은 결과다.
        plain = sanitize_markdown(text)
        verdict = self._earnings_verdict(plain)
        if plain.startswith(("🟢 호재", "🔴 악재", "⚪ 중립")):
            return plain
        lines = plain.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            first = lines[0].strip()
            for label in ("호재", "악재", "중립"):
                if first == label:
                    lines = lines[1:]
                    while lines and not lines[0].strip():
                        lines.pop(0)
                    body = "\n".join(lines).strip()
                    return f"{verdict}\n{body}".strip()
                if first.startswith(f"{label}:") or first.startswith(f"{label} -"):
                    lines[0] = first.replace(label, verdict, 1)
                    return "\n".join(lines).strip()
        return f"{verdict}\n{plain}".strip()

    def _earnings_verdict(self, text: str) -> str:
        if "악재" in text:
            return "🔴 악재"
        if "호재" in text:
            return "🟢 호재"
        return "⚪ 중립"
