import json
import httpx
import logging
import re
from datetime import date
from statistics import pstdev
from typing import Any, Literal, NamedTuple, Optional, Sequence, get_args, overload
from urllib.parse import quote as _url_quote
from fastapi import HTTPException
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pydantic import ValidationError
from sqlmodel import Session
from .config import (
    OPENAI_API_KEY, OPENAI_CHAT_MODEL,
    ANTHROPIC_API_KEY, ANTHROPIC_CHAT_MODEL,
    OLLAMA_API_KEY, OLLAMA_MODEL, OLLAMA_BASE_URL,
    NAT_BASE_URL, NAT_CHAT_MODEL, NAT_CONVERSATION_ID,
    NEWS_MCP_PARAMS, TRADING_MCP_PARAMS,
    SIGNAL_SCORE_MIN, SIGNAL_SCORE_MAX, SIGNAL_SCORE_THRESHOLD,
)
from .schemas import TradingSignal, AnalysisReport
from .models import AgentReport
from .stock_code import (
    _STOCK_CODE_EXTRACT_RE,
    _has_code_digit,
    _is_known_master_code,
    _is_unresolved_echo,
    _looks_like_stock_code,
)

logger = logging.getLogger(__name__)
_NAT_RESPONSE_LOG_PREVIEW_CHARS = 800

# LLM 출력이 절대 정할 수 없는, 서버가 provider에서만 파생하는 필드.
# analysis_from_nat_text가 모델의 raw JSON을 그대로 AnalysisReport에 넘기면
# 악의적이거나 비정상적인 LLM 출력이 이 값들을 주입할 수 있으므로 파싱 단계에서
# 걷어낸다. 실제 값은 perform_stock_analysis가 provider_supports_tools()로 채운다.
_DERIVED_PROVENANCE_FIELDS = frozenset({"provider", "provider_supports_tools"})

# schemas.AnalysisReport.urgency의 Literal 값에서 파생 — 스키마와 두 곳이 갈라지는 것을 막는다.
# AnalysisReport.urgency: Literal["low", "normal", "high", "critical"]
_URGENCY_LEVELS: frozenset[str] = frozenset(
    get_args(AnalysisReport.model_fields["urgency"].annotation)
)

# _STOCK_CODE_EXTRACT_RE, _has_code_digit, _looks_like_stock_code → backend/stock_code.py (#140)

# 종목명 -> 종목코드 프로세스 메모리 캐시.
# resolve_stock_code MCP 호출은 매번 새 stdio 서브프로세스를 띄우는 비용이 있고
# (run_mcp_tool 참고), 종목명-코드 매핑은 사실상 불변이므로 캐싱해 재조회를 피한다.
_stock_code_cache: dict[str, str] = {}
# 종목마스터는 4,353종(별칭 포함해도 여유롭게 포함)이므로 이 상한을 정상적으로
# 채울 일은 없다. _normalize_stock_input의 정규화가 stock-master.js의 정규화와
# 다시 어긋나는 미래의 변경이 있어도(둘은 서로 다른 언어의 별개 구현이다) 캐시가
# 무한정 자라지 않도록 하는 독립적인 방어선이다.
_STOCK_CODE_CACHE_MAX = 8192


def _normalize_stock_input(stock: str) -> str:
    r"""입력을 JS String.trim()과 동일하게 양끝만 정규화합니다.

    Python의 공백 판정(정규식 \s, str.isspace()와 동일한 범위)은 JS String.trim()이
    제거하는 WhiteSpace/LineTerminator 집합을 포함하고 U+FEFF(BOM) 하나만 빠짐을
    확인했다. 그래서 문자 클래스에 U+FEFF를 더한 [\s\ufeff]로 앞뒤를 한 번에
    훑으면, 공백과 BOM이 뒤섞여 나오는 입력(예: ' \ufeff \ufeffSAMSUNG')도
    JS trim()과 동일하게 끝까지 벗겨낸다 — strip()을 여러 번 나눠 부르면 한
    번 벗기다 멈춘 자리에서 그대로 멈춰 이런 입력을 끝까지 벗기지 못한다.
    Python이 더 지우는 문자도 있지만(U+001C~U+001F, U+0085 — JS 기준으로는
    공백이 아님) 문제가 되지 않는다: 캐시 키가 조금 더 뭉개질 뿐, 이 함수를
    거친 같은 문자열이 입력 판정·MCP 질의·캐시 키에 그대로 쓰이므로 세 곳이
    서로 어긋나지 않는다.
    양끝(trim)만 처리하고 문자열 내부의 U+FEFF는 지우지 않는다 — 내부까지
    지우면 이 함수의 결과가 실제로 MCP에 보내는 질의 문자열보다 더 뭉개져,
    U+FEFF가 중간에 낀 입력이 캐시에 이미 있는 무관한 이름과 우연히 같아질 수
    있다. 그러면 같은 입력인데도 캐시 상태에 따라 결과가 달라진다.

    입력 판정(_looks_like_stock_code), MCP 질의, 캐시 키까지 이 함수를 거친 같은
    문자열을 공유해야 세 곳이 서로 어긋나지 않는다.
    """
    return re.sub(r"^[\s\ufeff]+|[\s\ufeff]+$", "", stock)


async def _resolve_stock_code(stock: str) -> str:
    """종목명(또는 이미 종목코드인 값)을 종목코드로 변환합니다.

    이미 종목코드 형태이면서 종목마스터(mcp-trading/data/stocks.json)에 실재하면
    MCP 호출 없이 그대로 사용합니다(#151). 코드 형태이지만 마스터에 없으면
    지름길을 포기하고 아래 MCP 경로로 흘려보내 이름·별칭 매칭을 시도합니다.
    조회 실패/예외 시 리포트 저장을 막지 않도록 빈 문자열로 폴백합니다.
    """
    stock = _normalize_stock_input(stock)
    if _looks_like_stock_code(stock):
        shortcut_code = stock.upper()
        if _is_known_master_code(shortcut_code):
            return shortcut_code
        # 마스터에 없는 코드 형태 입력(#151): 지름길을 포기하고 아래 MCP 경로로
        # 흘려보낸다. 백엔드와 MCP가 같은 stocks.json을 보므로(모듈 상단 주석
        # 참고) MCP의 Step 1(코드 일치)도 결국 실패하지만, Step 2(이름·별칭
        # 매칭)는 이 코드 형태 입력이 우연히 실제 종목명과 같은 경우를 잡아낼
        # 수 있어 그대로 흘려보낸다. Step 2도 실패하면 stock-master.js Step 3가
        # market="UNKNOWN"으로 존재 검증 없이 에코하므로, 아래에서 그 에코를
        # 감지해 실패로 취급하고 ''로 폴백한다.

    cached = _stock_code_cache.get(stock)
    if cached is not None:
        return cached

    try:
        resolved = await run_mcp_tool(
            TRADING_MCP_PARAMS,
            "resolve_stock_code",
            {"stock_name": stock},
        )
        resolved_text = str(resolved)
        match = _STOCK_CODE_EXTRACT_RE.search(resolved_text)
        code = match.group(1) if match else None
        # stock-master.js Step 3의 미해석 에코("999999 (999999, UNKNOWN)")를 성공으로
        # 오인하면 안 된다 — 판정 근거는 stock_code._is_unresolved_echo에 있다.
        # #151 이전에는 _looks_like_stock_code가 숫자를 포함한 코드 형태 입력을 MCP로
        # 보내지 않아 이 에코가 도달할 일이 없었지만, #151에서 마스터에 없는 코드 형태
        # 입력을 이 MCP 경로로 흘려보내게 되면서 숫자가 있는 에코("999999")도 여기
        # 도달할 수 있다 — _has_code_digit만으로는 걸러지지 않으므로 따로 감지한다.
        if code is None or not _has_code_digit(code) or _is_unresolved_echo(resolved_text):
            logger.warning(
                "종목코드 추출 실패, 빈 문자열로 폴백: stock=%s, response=%s",
                stock,
                resolved,
            )
            return ""
        # 상한 도달은 정상 조건이 아니다 — 종목마스터 4,353종 규모에서는 일어나지
        # 않아야 하며, 발생했다면 입력 정규화가 stock-master.js와 다시 어긋났다는
        # 신호다. dict는 자동으로 비우지 않으므로 침묵한 채 자라기만 하면 상한을
        # 채운 항목들이 영구히 자리를 차지해, 그 이후 정상적인 신규 종목명마다
        # 매번 MCP 서브프로세스를 새로 띄우는 지연이 조용히 고정된다.
        if len(_stock_code_cache) >= _STOCK_CODE_CACHE_MAX:
            logger.warning(
                "종목코드 캐시 상한 도달(%d), 최초 항목 축출: stock=%s",
                _STOCK_CODE_CACHE_MAX, stock,
            )
            _stock_code_cache.pop(next(iter(_stock_code_cache)))
        _stock_code_cache[stock] = code
        return code
    except Exception as exc:
        logger.warning(
            "종목코드 조회 실패, 빈 문자열로 폴백: stock=%s, error=%s",
            stock,
            exc,
        )
        return ""


def _find_http_exception(exc: BaseException) -> HTTPException | None:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _find_http_exception(nested)
            if found is not None:
                return found
    return None


def _nat_conversation_id(
    stock: str,
    *,
    trigger_source: str | None = None,
) -> str:
    """Per-stock NAT thread so scheduler/API runs do not share ReAct transcript."""
    today = date.today().isoformat()
    prefix = trigger_source or "api"
    # HTTP 헤더는 ASCII만 허용되므로 한글 종목명을 퍼센트 인코딩
    safe_stock = _url_quote(stock, safe="")
    return f"{prefix}:{safe_stock}:{today}"


def _build_trigger_context(
    trigger_source: str | None,
    trigger_signal: str | None,
) -> str:
    """trigger_signal이 있을 때 프롬프트에 삽입할 컨텍스트 블록을 만든다."""
    if not trigger_signal:
        return ""
    source_label = trigger_source or "signal"
    return (
        f"\n분석 트리거 데이터 출처: {source_label}\n"
        f"--- 트리거 데이터 ---\n{trigger_signal[:4000]}\n"
        "------------------\n"
        "위 트리거 데이터를 투자 판단의 주요 근거로 반영하라. "
        "필요하면 라우터·서브에이전트로 보조 시장 데이터를 추가 확인하라.\n"
    )


def _build_nat_prompt(stock: str, trigger_context: str) -> str:
    """NAT 멀티에이전트용 프롬프트. 도구를 활용해 BUY/SELL/HOLD 판단을 생성한다.

    provider=nat 경로 전용이며, provider_supports_tools=True인 경우에만 호출한다.
    """
    return (
        f"종목: {stock}. 라우터·서브에이전트를 활용해 투자 관점 분석을 하라. "
        f"{trigger_context}"
        "Telegram 알림은 매우 긴급한 경우에만 사용한다. "
        "거래정지·상장폐지 위험, 대규모 공시, 실적 쇼크, 소송·규제 리스크, "
        "보유종목에 대한 급격한 위험 변화처럼 즉시 확인이 필요한 경우에만 "
        'urgency를 "high" 또는 "critical"로 두고 telegram_alert를 true로 둔다. '
        "그 외에는 urgency를 normal 이하로 두고 telegram_alert를 false로 둔다. "
        "답변 마지막에 **다음 JSON 한 개만** 출력하라 (다른 텍스트 없이도 됨):\n"
        '{"summary":"한 줄 요약",'
        '"details":{"decision":"BUY"|"SELL"|"HOLD","confidence_score":0.0-1.0,'
                '"reason":"근거","target_stock":"'
        f'{stock}'
        '"},'
        '"source_news":["헤드라인1","헤드라인2"],'
        '"source_signals":["분석에 사용한 외부 signal 1","분석에 사용한 외부 signal 2"],'
        '"trading_trend":"수급 한줄 요약 또는 null",'
        '"urgency":"low"|"normal"|"high"|"critical",'
        '"urgency_reason":"긴급 판단 사유 한 줄 또는 null",'
        '"telegram_alert":true|false}'
    )


def _build_toolless_prompt(stock: str, trigger_context: str) -> str:
    """도구 없는 provider(openai/anthropic/ollama)용 프롬프트.

    실시간 시장 데이터(MCP/KIS/뉴스)에 접근하지 못하므로 BUY/SELL/HOLD 판단과
    신뢰도 점수를 생성하지 않는다 (#162 A). 일반적 배경 설명만 허용한다.
    """
    return (
        f"종목: {stock}에 대한 일반적인 배경 정보와 투자 관련 개요를 설명하라. "
        f"{trigger_context}"
        "이 요청은 실시간 시장 데이터(공시·수급·뉴스 도구)에 접근하지 않는다. "
        "따라서 BUY/SELL/HOLD 판단이나 신뢰도 점수를 생성하지 않는다 — "
        "데이터 없이 만들어진 매매 신호는 소비자를 오도할 수 있다. "
        "답변 마지막에 **다음 JSON 한 개만** 출력하라 (다른 텍스트 없이도 됨):\n"
        '{"summary":"한 줄 설명",'
        '"source_news":[],'
        '"source_signals":[],'
        '"trading_trend":null,'
        '"urgency":"normal",'
        '"urgency_reason":null,'
        '"telegram_alert":false}'
    )


def _analysis_from_toolless_text(raw: str) -> dict[str, Any]:
    """도구 없는 provider 응답 파싱. details(decision/confidence_score)는 생성하지 않는다.

    JSON 파싱에 성공하면 summary/source_news 등을 추출하고 AnalysisReport로 스키마 검증한다.
    실패하면 원문을 요약으로 사용한다. 어느 경우에도 details=None을 유지한다.

    stock 파라미터는 이 함수 안에서 쓸 곳이 없다(details=None이므로 TradingSignal.target_stock
    미사용) — Improvement #2 (#162 리뷰).
    """
    text = (raw or "").strip()
    # #222: 중첩 JSON 객체 문제는 _json_objects_from_text의 span 기반 필터로
    # 근본 해결되었다(최상위 객체만 후보에 오른다).
    # sort는 추가 방어선으로 남긴다 — 텍스트에 복수의 독립적인 최상위 JSON이
    # 있고 summary 없는 객체가 뒤에 위치해 reversed 후 선두에 올 경우를 대비한다.
    # 안정 정렬이므로 summary를 모두 가진 후보들 사이에서는 기존 reversed 순서
    # (텍스트 내 마지막 우선)가 유지된다.
    _candidates = _json_objects_from_text(text)
    _candidates.sort(key=lambda d: "summary" not in d)
    for data in _candidates:
        # ValidationError를 유발하는 AnalysisReport() 생성만 try 안에 둔다.
        # 정규화는 TypeError를 낼 수 있어 except ValidationError에 잡히지 않으므로
        # try 밖에서 먼저 처리한다 (Nitpick #2).
        raw_urgency = data.get("urgency")
        # JSON 후보의 값은 임의 타입이다 — list/dict를 frozenset에 넣으면
        # TypeError로 죽으므로 str로 좁힌 뒤 비교한다. 문자열이 아니면 알 수 없는 값이다.
        # 도구 없는 경로는 판단을 만들지 않으므로, 알 수 없는 urgency는 후보 전체를
        # 버리는 대신 안전한 방향(normal)으로 낮춘다. 필드 하나 때문에
        # summary·source_news까지 잃고 원본 JSON이 화면에 노출되는 것을 막는다.
        urgency = (
            raw_urgency
            if isinstance(raw_urgency, str) and raw_urgency in _URGENCY_LEVELS
            else "normal"
        )
        # 알 수 없는 urgency를 버릴 때 로그를 남겨 프롬프트 드리프트를 감지할 수 있게 한다.
        if raw_urgency is not None and urgency != raw_urgency:
            logger.warning(
                "toolless 응답의 urgency=%r가 스키마 값이 아니라 %r로 정규화했습니다. "
                "프롬프트 드리프트를 의심하세요.",
                raw_urgency,
                urgency,
            )
        # urgency를 신뢰할 수 없어 낮춘 경우, 그 값을 설명하던 reason도 함께 버린다.
        # 남기면 "Urgency: normal - 즉시 대응 필요" 같은 모순된 문구가 소비자에게 나간다.
        # raw_urgency is None 은 "키가 없거나 null" — 하향이 아니므로 normalized=False.
        normalized = raw_urgency is not None and urgency != raw_urgency
        try:
            report = AnalysisReport(
                summary=str(data.get("summary") or text[:8000] or "빈 응답"),
                details=None,  # 도구 없는 provider는 매매 판단을 생성하지 않는다
                source_news=_string_list(data.get("source_news")),
                source_signals=_string_list(
                    data.get("source_signals") or data.get("source_news")
                ),
                trading_trend=data.get("trading_trend"),
                urgency=urgency,
                urgency_reason=None if normalized else data.get("urgency_reason"),
                telegram_alert=bool(data.get("telegram_alert", False)) and not normalized,
            )
        except ValidationError:
            continue  # 관련 없는 JSON 객체 — 다음 후보로
        return report.model_dump()
    # JSON 파싱 실패 또는 유효한 후보 없음: 원문을 요약으로 사용
    return AnalysisReport(
        summary=text[:8000] if text else "빈 응답",
        details=None,
        source_news=[],
        source_signals=[],
    ).model_dump()


async def perform_stock_analysis(
    stock: str,
    provider: str,
    session: Session,
    *,
    trigger_source: str | None = None,
    trigger_signal: str | None = None,
    signal_score: "SignalScore | None" = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    종목 분석을 수행하고 결과를 DB에 저장한 뒤 반환합니다.
    API 엔드포인트와 백그라운드 스케줄러에서 공용으로 사용됩니다.

    provider=nat: NAT 멀티에이전트를 통해 도구(MCP/KIS/뉴스)를 호출하고
    BUY/SELL/HOLD 판단·신뢰도 점수를 생성한다 (#162 A 불변).

    provider=openai/anthropic/ollama: 도구 없이 일반 배경 설명만 생성한다.
    decision·confidence_score는 저장하지 않는다 (DB와 API 응답 모두 null).

    signal_score: 감시 파이프라인의 2차 필터가 매긴 채점 결과 (#298). 필터를 거치지
    않은 경로(수동 분석, API 직접 호출)는 None이며, 이때 신호 점수 세 필드는 모두
    null로 남는다 — 0으로 채우지 않는다.
    """
    key = normalize_llm_provider(provider)
    supports_tools = provider_supports_tools(key)

    trigger_context = _build_trigger_context(trigger_source, trigger_signal)

    nat_cid = conversation_id
    if key == "nat" and nat_cid is None:
        nat_cid = _nat_conversation_id(stock, trigger_source=trigger_source)

    if supports_tools:
        # NAT 멀티에이전트: 도구를 활용해 BUY/SELL/HOLD 판단 생성
        user_msg = _build_nat_prompt(stock, trigger_context)
        raw = await llm_chat(key, user_msg, conversation_id=nat_cid)
        data = analysis_from_nat_text(str(raw), stock)
        details = data.get("details") or {}
        decision: str | None = details.get("decision") if isinstance(details, dict) else None
        confidence_score: float | None = (
            details.get("confidence_score") if isinstance(details, dict) else None
        )
        reason: str = details.get("reason", "") if isinstance(details, dict) else ""
    else:
        # 도구 없는 provider: 일반 설명만 생성, 매매 판단 없음
        user_msg = _build_toolless_prompt(stock, trigger_context)
        raw = await llm_chat(key, user_msg, conversation_id=nat_cid)
        data = _analysis_from_toolless_text(str(raw))
        decision = None
        confidence_score = None
        reason = ""

    # #162: 이 값을 API 응답(data)과 DB 저장(report) 양쪽에 동일하게 붙인다 —
    # 소비자가 provider 문자열만 보고 도구 사용 여부를 스스로 추론하지 않게 한다.
    data["provider"] = key
    data["provider_supports_tools"] = supports_tools

    # #298: 2차 필터의 채점 결과도 같은 자리에서 응답·DB 양쪽에 붙인다. 알림
    # 포매터(telegram_notifier.format_analysis_alert)는 이 세 키만 보고 영향도 줄을
    # 만들므로, 여기서 빠지면 알림에서 조용히 사라진다.
    data["signal_score"] = signal_score.score if signal_score is not None else None
    data["signal_reason"] = signal_score.reason if signal_score is not None else None
    data["signal_uncertainty"] = (
        signal_score.uncertainty if signal_score is not None else None
    )

    # 분석 리포트를 데이터베이스에 자동으로 저장
    try:
        stock_code = await _resolve_stock_code(stock)
        report = AgentReport(
            stock_code=stock_code,
            stock_name=stock,
            provider=key,
            summary=data.get("summary", ""),
            decision=decision,
            confidence_score=confidence_score,
            reason=reason,
            provider_supports_tools=supports_tools,
            signal_score=data["signal_score"],
            signal_reason=data["signal_reason"],
            signal_uncertainty=data["signal_uncertainty"],
        )
        session.add(report)
        session.commit()
        session.refresh(report)
        logger.info(f"AgentReport saved for {stock} via {provider}")
    except Exception as e:
        logger.error(f"Failed to save AgentReport for {stock}: {e}")
        # DB 저장 실패해도 데이터는 반환함

    return data


async def generate_morning_briefing(watchlist: list[str] | None = None) -> dict[str, Any]:
    stocks = list(dict.fromkeys(watchlist or []))
    market_summary_source = await _collect_morning_context(
        NEWS_MCP_PARAMS,
        "get_market_news",
        {"stock_name": "미국 증시"},
    )
    balance_text = await _collect_morning_context(TRADING_MCP_PARAMS, "get_balance", {})

    stock_blocks = []
    for stock in stocks:
        news = await _collect_morning_context(
            NEWS_MCP_PARAMS,
            "get_market_news",
            {"stock_name": stock},
        )
        trading = await _collect_morning_context(
            TRADING_MCP_PARAMS,
            "get_investor_trading",
            {"stock_name": stock},
        )
        stock_blocks.append(f"[{stock}]\n뉴스: {news}\n수급: {trading}")

    watchlist_context = "\n\n".join(stock_blocks) if stock_blocks else "관심종목 없음"
    prompt = (
        "Strategy Planner 관점으로 오늘 장 시작 전 Telegram 모닝 브리핑을 작성하라.\n"
        "반드시 다음 JSON 객체 한 개만 출력하라:\n"
        '{"market_summary":"전일 미국/선물 시장 동향과 주요 이슈",'
        '"watchlist":["종목별 뉴스 및 수급 요약"],'
        '"trading_ideas":["오늘의 간략 시나리오"],'
        '"catalysts":["당일/금주 주요 촉매 이벤트"]}\n\n'
        f"시장 뉴스:\n{market_summary_source}\n\n"
        f"잔고:\n{balance_text}\n\n"
        f"관심종목 컨텍스트:\n{watchlist_context}"
    )
    raw = await llm_chat("nat", prompt, conversation_id=f"morning-briefing:{date.today().isoformat()}")
    return _morning_briefing_from_text(str(raw))


async def _collect_morning_context(
    mcp_params: StdioServerParameters,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    try:
        return await run_mcp_tool(mcp_params, tool_name, arguments)
    except Exception as exc:
        logger.error("Morning briefing source failed for %s: %s", tool_name, exc)
        return f"{tool_name} 조회 실패: {exc}"


_BRIEFING_KEYS = ("market_summary", "watchlist", "trading_ideas", "catalysts")


def _morning_briefing_from_text(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    for data in _json_objects_from_text(text):
        # 브리핑 키를 하나도 갖지 않은 후보(래퍼 객체, 무관한 JSON)는 건너뛴다.
        # 그대로 반환하면 브리핑 전체가 빈 값이 되고 아래 원문 폴백도 도달하지 못한다.
        if not any(key in data for key in _BRIEFING_KEYS):
            continue
        return {
            "market_summary": str(data.get("market_summary") or ""),
            "watchlist": _string_list(data.get("watchlist")),
            "trading_ideas": _string_list(data.get("trading_ideas")),
            "catalysts": _string_list(data.get("catalysts")),
        }
    return {
        "market_summary": text or "모닝 브리핑 생성 결과가 비어 있습니다.",
        "watchlist": [],
        "trading_ideas": [],
        "catalysts": [],
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value:
        return [str(value)]
    return []


class SignalScore(NamedTuple):
    """2차 필터가 signal 하나에 대해 내린 채점 결과 (#298).

    ``score``/``reason``/``uncertainty``가 None인 것과 0/""인 것은 다른 상태다.
    None은 "채점하지 못했다", 0은 "무관/중립이라고 채점했다"이다 (#122·#162).
    """

    score: Optional[int]
    reason: Optional[str]
    uncertainty: Optional[float]
    headline_scores: tuple[int, ...]
    is_significant: bool


# 채점에 실패했을 때의 결과. is_significant=True가 핵심이다 (REQ-04 fail-open):
# 경량 LLM이 죽었다고 상세 분석까지 건너뛰면 진짜 대형 악재를 통째로 놓친다.
# 점수는 null로 남겨 "통과시켰지만 몇 점인지는 모른다"를 사후에 구분할 수 있게 한다.
_FAIL_OPEN_SIGNAL_SCORE = SignalScore(None, None, None, (), True)
# 채점 이전에 걸러진 signal(빈 내용, 직전과 동일 내용). LLM을 부르지도 않았으므로 점수가 없다.
_SKIPPED_SIGNAL_SCORE = SignalScore(None, None, None, (), False)

# reason은 알림 한 줄에 들어간다. 모델이 장문을 뱉으면 잘라 쓴다.
_SIGNAL_REASON_MAX_CHARS = 120
# 채점 프롬프트에 넣는 signal 본문 상한 (기존 YES/NO 필터와 동일하게 유지).
_SIGNAL_SNIPPET_CHARS = 1000

def _coerce_signal_score(value: Any) -> Optional[int]:
    """임의의 JSON 값을 -3~+3 정수로 좁힌다. 숫자로 볼 수 없으면 None.

    소수(2.5)는 반올림한다 — 프롬프트는 정수를 요구하지만 경량 모델은 자주 어긴다.
    레인지를 벗어난 값(7, -9)은 버리지 않고 끝값으로 자른다: "아주 큰 호재"라는
    방향 정보 자체는 유효하고, 이걸 파싱 실패로 처리하면 하필 대형 신호에서만
    fail-open이 잦아진다. bool은 int의 하위형이라 True가 1점으로 새는 것을 막는다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/±inf
        return None
    return max(SIGNAL_SCORE_MIN, min(SIGNAL_SCORE_MAX, int(round(value))))


def signal_score_uncertainty(headline_scores: Sequence[int]) -> Optional[float]:
    """기사별 점수의 표준편차. 기사가 2건 미만이면 None.

    LLM에게 확신도를 직접 묻지 않고 코드로 계산한다. headline_scores도 결국 같은
    모델의 자기보고이므로 이것이 "객관적 사실"이 되는 것은 아니다 — 다만 항목별로
    따로 받은 점수의 흩어짐은 한 번에 물어본 확신도보다 다루기 쉽다. 계산 규칙이
    코드에 고정돼 있어 프롬프트가 바뀌어도 일관되고, 사람이 같은 기사에 매긴 점수로
    똑같이 계산해 대조할 수 있다.

    모집단 표준편차(pstdev)를 쓴다: 이 기사 묶음은 더 큰 모집단에서 뽑은 표본이
    아니라 판단에 실제로 쓴 전부다. 1건이면 0.0이 아니라 None이다 — 0.0은
    "기사들이 완전히 일치했다"는 뜻이고, 1건은 흩어짐을 정의할 수 없는 상태다.
    """
    if len(headline_scores) < 2:
        return None
    return pstdev(headline_scores)


def parse_signal_score(raw: str) -> Optional[SignalScore]:
    """채점 응답 텍스트에서 SignalScore를 뽑는다. 실패하면 None.

    None은 호출부에서 fail-open(유의미 취급)으로 이어진다. 그래서 "적당히 넘겨
    짚기"보다 명확히 실패로 떨어뜨리는 편이 안전하다 — 점수를 지어내면 그 값이
    DB와 평가셋에 그대로 남지만, 실패는 null로 남아 구분된다.
    """
    for data in _json_objects_from_text(raw or ""):
        score = _coerce_signal_score(data.get("score"))
        if score is None:
            continue

        raw_reason = data.get("reason")
        reason: Optional[str] = None
        if isinstance(raw_reason, str):
            # 알림 한 줄에 들어가므로 줄바꿈을 접는다.
            collapsed = " ".join(raw_reason.split())
            reason = collapsed[:_SIGNAL_REASON_MAX_CHARS] or None

        raw_headlines = data.get("headline_scores")
        headline_scores: tuple[int, ...] = ()
        if isinstance(raw_headlines, list):
            # 숫자로 볼 수 없는 원소는 버린다. 개수가 기사 수와 어긋날 수 있지만,
            # 표준편차는 "실제로 채점된 기사들"의 흩어짐이면 충분하다.
            headline_scores = tuple(
                coerced
                for coerced in (_coerce_signal_score(item) for item in raw_headlines)
                if coerced is not None
            )

        return SignalScore(
            score=score,
            reason=reason,
            uncertainty=signal_score_uncertainty(headline_scores),
            headline_scores=headline_scores,
            is_significant=abs(score) >= SIGNAL_SCORE_THRESHOLD,
        )
    return None


def _signal_snippet(signal_content: str) -> str:
    """채점에 넣을 본문을 상한까지 자르되, 잘려 나간 마지막 줄은 버린다.

    signal은 기사 한 건이 한 줄이다. 상한에서 반토막 난 줄을 남기면 모델은 그것을
    온전한 기사 1건으로 세어 점수를 매기고, 그 값이 headline_scores를 거쳐
    signal_uncertainty(표준편차)까지 오염시킨다.

    상한 안에 줄바꿈이 하나도 없으면 통째로 한 기사이므로 자른 그대로 쓴다 —
    이때는 버릴 온전한 줄이 없어서, 버리면 채점할 내용 자체가 사라진다.
    """
    if len(signal_content) <= _SIGNAL_SNIPPET_CHARS:
        return signal_content

    truncated = signal_content[:_SIGNAL_SNIPPET_CHARS]
    head, separator, _partial = truncated.rpartition("\n")
    return head if separator else truncated


def _build_signal_score_prompt(stock: str, source: str, snippet: str) -> str:
    """채점 프롬프트. 단계별 기준을 모두 적어 모델이 축을 스스로 상상하지 않게 한다.

    레인지를 -3~+3으로 좁게 잡은 것은 의도다. 0~100 같은 넓은 축에서 경량 모델은
    같은 기사에 매번 다른 점수를 준다 — 좁은 축은 재현성을 사고, 잃는 해상도는
    이 필터가 애초에 필요로 하지 않는다.
    """
    headline_count = len([line for line in snippet.splitlines() if line.strip()])
    return (
        f"당신은 전문 주식 분석가입니다. 다음은 '{stock}' 종목에 대한 최신 외부 signal입니다.\n"
        f"signal 출처: {source}\n\n"
        f"--- signal 내용 ---\n{snippet}\n"
        "------------------\n\n"
        f"이 signal이 '{stock}'의 투자 판단에 미치는 영향을 아래 기준으로 채점하십시오.\n"
        "+3: 실적 서프라이즈, 대형 수주·계약, M&A 등 명확한 대형 호재\n"
        "+2: 방향이 분명한 호재 (신규 대형 고객사, 의미 있는 가이던스 상향 등)\n"
        "+1: 약한 호재 또는 우호적인 분위기\n"
        " 0: 투자 판단과 무관하거나 중립 (홍보성 기사, 단순 시황 나열, 반복되는 내용)\n"
        "-1: 약한 악재\n"
        "-2: 방향이 분명한 악재 (가이던스 하향, 주요 고객 이탈 등)\n"
        "-3: 실적 쇼크, 대형 소송·제재, 회계 이슈 등 명확한 대형 악재\n\n"
        f"signal 내용은 기사 {headline_count}건이 줄 단위로 이어져 있습니다. "
        "headline_scores에는 각 기사를 같은 기준으로 따로 채점한 정수를 원문 순서대로 담으십시오.\n\n"
        "반드시 아래 JSON 객체 하나만 출력하고 다른 설명은 붙이지 마십시오.\n"
        '{"score": <-3~3 정수>, "reason": "<점수 근거 한 줄, 40자 이내>", '
        '"headline_scores": [<-3~3 정수>, ...]}'
    )


async def score_signal(
    stock: str,
    signal_content: str,
    last_signal_content: Optional[str] = None,
    *,
    source: str = "signal",
    provider: str = "ollama",
) -> SignalScore:
    """외부 signal을 -3~+3으로 채점한다 (#298).

    로컬 초경량 모델(예: gemma4) 또는 경량 API 모델(예: gpt-5.4-mini)을 1차
    필터로 사용해 비용과 정확도의 균형을 맞춘다.

    호출·파싱에 실패하면 예외를 올리지 않고 fail-open한다 — 유의미로 통과시키되
    점수는 null로 남긴다 (REQ-04 놓침 방지).
    """
    if not signal_content:
        return _SKIPPED_SIGNAL_SCORE

    if signal_content == last_signal_content:
        return _SKIPPED_SIGNAL_SCORE

    prompt = _build_signal_score_prompt(stock, source, _signal_snippet(signal_content))

    try:
        # 설정된 provider(ollama, openai 등)에 따라 경량 모델 호출
        raw = await llm_chat(provider, prompt)
    except Exception as e:
        logger.error(
            "signal 채점 호출 실패 (%s, %s, %s): %s — 유의미로 통과시킵니다(fail-open)",
            source,
            stock,
            provider,
            e,
        )
        return _FAIL_OPEN_SIGNAL_SCORE

    parsed = parse_signal_score(str(raw))
    if parsed is None:
        logger.warning(
            "signal 채점 응답을 파싱하지 못했습니다 [%s:%s] (Provider: %s) — "
            "유의미로 통과시킵니다(fail-open)",
            source,
            stock,
            provider,
        )
        return _FAIL_OPEN_SIGNAL_SCORE

    logger.info(
        "signal 채점 [%s:%s] (Provider: %s): score=%s, 유의미=%s, 기사별=%s, 흩어짐=%s, 근거=%s",
        source,
        stock,
        provider,
        parsed.score,
        parsed.is_significant,
        list(parsed.headline_scores),
        parsed.uncertainty,
        parsed.reason,
    )
    return parsed


async def check_signal_significance(
    stock: str,
    signal_content: str,
    last_signal_content: Optional[str] = None,
    *,
    source: str = "signal",
    provider: str = "ollama"
) -> bool:
    """외부 signal이 투자 관점에서 유의미한 변화인지 판단합니다.

    판정은 :func:`score_signal`의 점수를 ``|score| >= SIGNAL_SCORE_THRESHOLD``로
    환산한 것이다 (#298 이전에는 경량 LLM에 YES/NO를 직접 물었다).

    점수·근거·불확실성까지 필요한 호출부는 :func:`score_signal`을 직접 쓴다 —
    감시 파이프라인(scheduler)이 그렇게 한다. 이 함수는 "유의미한가"만 알면 되는
    호출부를 위한 얇은 래퍼다.
    """
    scored = await score_signal(
        stock,
        signal_content,
        last_signal_content,
        source=source,
        provider=provider,
    )
    return scored.is_significant


async def check_news_significance(
    stock: str,
    news_content: str,
    last_news_content: Optional[str] = None,
    provider: str = "ollama",
) -> bool:
    return await check_signal_significance(
        stock,
        news_content,
        last_news_content,
        source="news",
        provider=provider,
    )

async def _llm_openai_chat(user_msg: str) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY가 설정되지 않았습니다. backend/.env에 키를 설정하세요.",
        )
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        messages=[{"role": "user", "content": user_msg}],
        temperature=0.2,
    )
    choice = resp.choices[0].message.content
    return (choice or "").strip()


async def _llm_anthropic_chat(user_msg: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY가 설정되지 않았습니다. backend/.env에 키를 설정하세요.",
        )
    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=ANTHROPIC_CHAT_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": user_msg}],
    )
    parts: list[str] = []
    for block in msg.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts).strip()


async def _llm_ollama_chat(user_msg: str) -> str:
    client = AsyncOpenAI(
        api_key=OLLAMA_API_KEY or "ollama",
        base_url=OLLAMA_BASE_URL,
    )
    try:
        resp = await client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": user_msg}],
            temperature=0.2,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama 호출 실패 ({OLLAMA_BASE_URL}, model={OLLAMA_MODEL}): {exc}. "
                "호스트에서 `ollama serve` 실행 여부를 확인하세요. "
                "Docker 백엔드는 기본으로 host.docker.internal:11434를 씁니다; "
                "직접 지정하려면 backend/.env에 OLLAMA_BASE_URL을 넣으세요."
            ),
        ) from exc
    choice = resp.choices[0].message.content
    return (choice or "").strip()


class NatToolUse(NamedTuple):
    """각주에 실을 도구 한 건 — 도구명과 결과 상태 (#260).

    finus_nat이 도구 강제 원장에서 만들어 보내는
    ``{"name": ..., "ok": ..., "empty": ...}``에 대응한다.

    ``ok=False``는 도구를 호출했지만 오류 응답을 받았다는 뜻이고,
    ``ok=True, empty=True``는 호출은 성공했지만 결과 집합이 비었다는 뜻이다(#209).
    셋 다 각주에서 구분해 표시해야 한다 — 실패나 빈 결과를 그냥 "확인한 자료"로
    보여주면 사용자는 답변이 그 데이터에 근거했다고 읽지만 실제로는 아니다.

    ``empty``에 기본값이 있는 이유: 이 필드를 싣지 않는 구버전 finus_nat과 섞일 수
    있다. 기본값 ``False``는 "빈 결과라는 관측이 없음"이지 "데이터가 있었음"이 아니다.
    """

    name: str
    ok: bool
    empty: bool = False


class NatAnswer(str):
    """NAT 답변 텍스트 + 추론 메타데이터 (#260).

    ``str`` 서브클래스인 이유: ``llm_chat("nat", ...)``의 반환값은 scheduler의
    ``analysis_from_nat_text``, Telegram ``/earnings`` 핸들러 등 여러 곳에서 이미
    문자열로 소비된다. 별도 타입을 새로 반환하면 그 호출부를 전부 고쳐야 하고,
    "기존 응답 파서를 건드리지 않는다"는 이 작업의 전제가 깨진다. ``str``을
    상속하면 기존 호출부는 그대로 동작하고, 각주가 필요한 호출부만 두 속성을 읽는다.

    두 속성은 NAT 응답 페이로드의 **최상위 필드**에서 그대로 온다 — 답변 텍스트
    (``choices[0].message.content``)를 파싱해 만들지 않는다 (#129와 같은 원칙).

    **주의 — 이 타입은 문자열 연산 한 번이면 소실된다.** ``str`` 서브클래스라
    ``strip()``·``re.sub()``·f-string 등의 결과는 전부 plain ``str``이다. 답변
    텍스트를 가공하는 계층이 ``llm_chat`` 경계 안에 새로 생기면
    :meth:`with_text`로 갈아끼워야 한다. 그냥 반환하면 backend는 각주를 **조용히**
    생략하도록 설계돼 있어(로그도 예외도 없다) 기능만 사라진다.
    """

    routed_agent: str | None
    tools_used: tuple[NatToolUse, ...]

    def __new__(
        cls,
        text: str,
        *,
        routed_agent: str | None = None,
        tools_used: tuple[NatToolUse, ...] = (),
    ) -> "NatAnswer":
        answer = super().__new__(cls, text)
        answer.routed_agent = routed_agent
        answer.tools_used = tuple(tools_used)
        return answer

    def with_text(self, text: str) -> "NatAnswer":
        """텍스트만 교체하고 추론 메타데이터를 유지한다 (#260).

        ``llm_chat`` 안에서 응답 텍스트를 가공하는 계층(#230 PII 역치환 등)이 추가될 때
        ``return transform(raw)``로 끝내면 서브클래스가 소실된다. 그 자리에
        ``raw.with_text(transform(raw))``를 쓰면 각주가 살아남는다.
        """
        return NatAnswer(text, routed_agent=self.routed_agent, tools_used=self.tools_used)


# 각주에 실을 도구 개수 상한. tools_used는 다른 서비스(NAT)가 보내는 값이라
# 개수·길이가 backend에서 보장되지 않는다 — 비정상적으로 긴 목록이 텔레그램 메시지의
# 본문 자리를 밀어내지 않도록 파싱 단계에서 잘라 둔다.
NAT_MAX_TOOLS_USED = 16


def _nat_reasoning_trace_from_payload(
    payload: dict[str, Any],
) -> tuple[str | None, tuple[NatToolUse, ...]]:
    """NAT 응답 최상위의 ``routed_agent``/``tools_used``를 읽는다 (#260).

    두 필드는 finus_nat이 supervisor의 라우팅 결과와 도구 강제 원장에서 만들어
    실어 보내는 값이다. 여기서는 **읽기만** 한다 — 답변 텍스트를 뒤져서 도구명이나
    에이전트명을 추측하지 않는다.

    ``tools_used``는 ``[{"name": str, "ok": bool, "empty": bool}]`` 형태다
    (``empty``는 구버전 finus_nat에는 없다). 필드가 없거나 타입이
    어긋나면 조용히 비운다. 두 필드를 싣지 않는 구버전 finus_nat과 혼용될 수 있고,
    각주는 답변에 덧붙는 부가 정보이므로 없다고 해서 응답 자체를 실패로 만들 이유가
    없다. 개수는 :data:`NAT_MAX_TOOLS_USED`로 자른다.
    """
    routed_raw = payload.get("routed_agent")
    routed_agent = routed_raw.strip() if isinstance(routed_raw, str) else ""

    tools_raw = payload.get("tools_used")
    tools_used: list[NatToolUse] = []
    seen: set[str] = set()
    if isinstance(tools_raw, list):
        for item in tools_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            # 호출 순서 유지 + 중복 제거. finus_nat이 이미 그렇게 보내지만,
            # 신뢰 경계 밖의 값이므로 backend에서도 같은 불변식을 세운다.
            if name in seen:
                continue
            seen.add(name)
            # ok/empty가 빠졌거나 bool이 아니면 각각 실패·비어있지 않음으로 본다.
            # ok는 근거를 과장하지 않는 쪽으로 기울고, empty는 관측이 없다는 뜻이므로
            # ok=False와 겹쳐 "실패인데 빈 결과"가 되지 않도록 ok일 때만 읽는다.
            ok = item.get("ok") is True
            tools_used.append(NatToolUse(name, ok, ok and item.get("empty") is True))
            if len(tools_used) >= NAT_MAX_TOOLS_USED:
                logger.warning(
                    "NAT tools_used가 %d개를 넘어 잘라냅니다 (수신 %d개)",
                    NAT_MAX_TOOLS_USED,
                    len(tools_raw),
                )
                break

    return (routed_agent or None), tuple(tools_used)


def _nat_message_from_payload(payload: dict[str, Any]) -> str:
    if "error" in payload:
        err = payload["error"]
        msg = err if isinstance(err, str) else err.get("message", repr(err))
        raise HTTPException(status_code=502, detail=f"NAT 오류 응답: {msg}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise KeyError("empty or invalid choices")

    first = choices[0]
    if not isinstance(first, dict):
        raise TypeError("choice entry must be a dict")

    message = first.get("message")
    if not isinstance(message, dict):
        raise KeyError("message")

    content = message.get("content")
    return (content if content is not None else "").strip()


def _log_nat_response(resp: httpx.Response) -> None:
    logger.debug(
        "NAT response received: status_code=%s body_length=%s",
        resp.status_code,
        len(resp.text),
    )
    if resp.text:
        logger.debug(
            "NAT response body preview: %s",
            resp.text[:_NAT_RESPONSE_LOG_PREVIEW_CHARS],
        )


async def _llm_nat_chat(user_msg: str, *, conversation_id: str | None = None) -> NatAnswer:
    url = f"{NAT_BASE_URL}/v1/chat/completions"
    cid = (conversation_id or NAT_CONVERSATION_ID).strip()
    headers = {
        "Content-Type": "application/json",
        "conversation-id": cid,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={
                    "model": NAT_CHAT_MODEL,
                    "messages": [{"role": "user", "content": user_msg}],
                    "temperature": 0.2,
                    "stream": False,
                },
            )
            _log_nat_response(resp)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"NAT에 연결할 수 없습니다 ({url}): {exc}",
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.exception(
            "Failed to parse NAT response JSON: status_code=%s",
            resp.status_code,
        )
        logger.debug(
            "NAT response body preview: %s",
            resp.text[:_NAT_RESPONSE_LOG_PREVIEW_CHARS],
        )
        raise HTTPException(
            status_code=502,
            detail=f"NAT JSON 파싱 실패: {exc}; body[:800]={resp.text[:800]!r}",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="NAT 응답이 JSON 객체가 아닙니다.")

    try:
        message = _nat_message_from_payload(payload)
    except (KeyError, IndexError, TypeError) as exc:
        body_snip = resp.text[:1200] if resp.text else ""
        raise HTTPException(
            status_code=502,
            detail=(
                f"NAT 응답 형식 오류 ({exc}). NAT_CHAT_MODEL이 NAT 서비스에서 쓰는 모델과 일치하는지 확인하세요. "
                f"body[:1200]={body_snip!r}"
            ),
        ) from exc

    # #260: 답변 텍스트는 위에서 기존 파서가 그대로 뽑았고, 여기서는 추론 메타데이터만
    # 덧붙인다. NatAnswer는 str이므로 이 반환값을 문자열로 쓰는 기존 호출부는 불변이다.
    routed_agent, tools_used = _nat_reasoning_trace_from_payload(payload)
    return NatAnswer(message, routed_agent=routed_agent, tools_used=tools_used)


async def run_mcp_tool(
    server_params: StdioServerParameters,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    import asyncio

    async def _call() -> str:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if getattr(result, "isError", False):
                    block = result.content[0] if result.content else None
                    detail = getattr(block, "text", str(block)) if block else "MCP 도구 오류"
                    raise HTTPException(status_code=500, detail=detail)
                if not result.content:
                    return ""
                block = result.content[0]
                return getattr(block, "text", str(block))

    try:
        # MCP 서브프로세스 호출에 30초 타임아웃 적용 — 무기한 블로킹 방지
        return await asyncio.wait_for(_call(), timeout=30.0)
    except asyncio.TimeoutError as exc:
        logger.error("MCP call_tool timed out for %s", tool_name)
        raise HTTPException(
            status_code=504,
            detail=f"데이터 공급원({tool_name}) 응답 타임아웃 (30초)",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        http_exc = _find_http_exception(exc)
        if http_exc is not None:
            raise http_exc from exc
        logger.exception("MCP call_tool failed for %s", tool_name)
        raise HTTPException(
            status_code=500,
            detail=f"데이터 공급원({tool_name}) 연결 실패: {exc}",
        ) from exc


def short_error(exc: BaseException) -> str:
    """예외를 사용자 메시지에 넣을 수 있는 짧은 한 줄로 만든다.

    ``run_mcp_tool``이 올리는 HTTPException은 사유가 ``detail``에 들어 있고, 그 값은
    MCP 서브프로세스의 stderr까지 실려 수천 자가 되기도 한다. 그대로 사용자 메시지에
    박으면 텔레그램 4096자 한도에 걸려 **거부 메시지 자체가 전송되지 않는다** — 가장
    알려야 할 순간에 아무 말도 못 하게 되는 것이라 300자에서 자른다.

    telegram_commands(주문·조회 명령)와 order_assist(주문 보조)가 함께 쓴다. 두 곳이
    각자 자르면 한도가 갈라지므로 예외를 실제로 만들어 올리는 이 모듈에 둔다.
    """
    raw = getattr(exc, "detail", str(exc))
    text = str(raw or "").strip()
    if not text:
        text = exc.__class__.__name__
    return text[:300]


def analysis_from_nat_text(raw: str, stock: str) -> dict[str, Any]:
    """Map NAT assistant output to AnalysisReport JSON for the reference React UI."""
    text = (raw or "").strip()
    for data in _json_objects_from_text(text):
        try:
            report = AnalysisReport(
                **{k: v for k, v in data.items() if k not in _DERIVED_PROVENANCE_FIELDS}
            )
            dumped = report.model_dump()
            if dumped.get("source_signals") is None:
                dumped["source_signals"] = dumped.get("source_news", [])
            return dumped
        except ValueError:
            pass
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    news_snip = lines[:8] if lines else []
    return AnalysisReport(
        summary=text[:8000] if text else "빈 응답",
        details=TradingSignal(
            decision="HOLD",
            confidence_score=0.5,
            reason="NAT 응답을 JSON으로 파싱하지 못해 요약만 표시합니다.",
            target_stock=stock,
        ),
        source_news=news_snip,
        source_signals=news_snip,
        trading_trend=None,
    ).model_dump()


def _json_objects_from_text(text: str) -> list[dict[str, Any]]:
    """텍스트에서 JSON 객체를 최상위 우선 순서로 추출한다.

    raw_decode의 종료 인덱스로 최상위 객체의 span을 추적해, 그 안에서 발견된
    중첩 객체는 후보에서 버리지 않고 '후순위'로 내린다 (#222).
    최상위 객체가 실제 페이로드를 감싸기만 한 래퍼일 때 안쪽으로 폴백하기 위함이다.
    각 그룹 안에서는 텍스트 내 마지막 객체가 먼저 오는 reversed 순서를 유지한다.
    """
    decoder = json.JSONDecoder()
    top: list[dict[str, Any]] = []
    nested: list[dict[str, Any]] = []
    consumed_until = 0  # 직전에 디코드한 최상위 객체의 끝(exclusive)
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, index)  # 슬라이스 없이 idx 지정
        except json.JSONDecodeError:
            continue
        if index < consumed_until:
            nested.append(value)  # 이미 디코드한 최상위 객체 내부 → 후순위
            continue
        top.append(value)
        consumed_until = end
    return list(reversed(top)) + list(reversed(nested))


def normalize_llm_provider(
    provider: str,
) -> Literal["openai", "anthropic", "nat", "ollama"]:
    p = (provider or "openai").strip().lower()
    if p in ("openai", "gpt", "open_ai"):
        return "openai"
    if p in ("anthropic", "claude"):
        return "anthropic"
    if p in ("nat", "nemo", "nvidia"):
        return "nat"
    if p == "ollama":
        return "ollama"
    raise HTTPException(
        status_code=400,
        detail=(
            f"지원하지 않는 provider={provider!r}. "
            "openai | anthropic | ollama | nat(API 전용) 중 하나를 사용하세요."
        ),
    )


# provider별 반환 타입 오버로드 (#260): "nat"만 추론 메타데이터를 실은 NatAnswer를
# 돌려준다. NatAnswer는 str 서브클래스라 단순 유니온(`str | NatAnswer`)으로는 타입
# 체커에게 아무 정보도 못 준다 — str로 접히기 때문이다. Literal 오버로드로 갈라야
# routed_agent/tools_used를 읽는 호출부가 체커의 도움을 받는다.
@overload
async def llm_chat(
    provider_key: Literal["nat"],
    user_msg: str,
    *,
    conversation_id: str | None = None,
) -> NatAnswer: ...


@overload
async def llm_chat(
    provider_key: Literal["openai", "anthropic", "ollama"],
    user_msg: str,
    *,
    conversation_id: str | None = None,
) -> str: ...


async def llm_chat(
    provider_key: Literal["openai", "anthropic", "nat", "ollama"],
    user_msg: str,
    *,
    conversation_id: str | None = None,
) -> str:
    if provider_key == "openai":
        return await _llm_openai_chat(user_msg)
    if provider_key == "anthropic":
        return await _llm_anthropic_chat(user_msg)
    if provider_key == "ollama":
        return await _llm_ollama_chat(user_msg)
    return await _llm_nat_chat(user_msg, conversation_id=conversation_id)


# llm_chat의 분기와 함께 유지한다: NAT처럼 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로
# 라우팅되는 provider만 여기 나열한다. _llm_openai_chat/_llm_anthropic_chat/
# _llm_ollama_chat은 tools 파라미터 없이 모델을 그대로 호출하므로, 이 셋에 대해
# False라는 것은 코드로 증명 가능하다 (#162). 반대로 "nat"은 NAT 멀티에이전트로
# 라우팅된다는 provider 차원의 능력만 의미할 뿐, 그 호출에서 실제로 도구가 실행됐다는
# 관측이 아니다 — NAT ReAct 에이전트가 도구 없이도 답을 낼 수 있는 문제(#152, 여섯
# finus_nat/configs/agents/*.yml 모두 raise_on_parsing_failure: false)가 열려 있는
# 한, 이 필드는 false negative는 구조적으로 불가능해도 false positive는 가능한
# 비대칭적인 신호다. 실제 도구 호출 이력(ledger)은 #152의 몫이며 여기서 만들지 않는다.
# 새 provider가 도구를 갖추게 되면 이 집합 한 곳만 갱신하면 provider_supports_tools()를
# 쓰는 모든 호출부(AgentReport.provider_supports_tools 등)에 반영된다.
_TOOL_CAPABLE_PROVIDERS: frozenset[str] = frozenset({"nat"})


def provider_supports_tools(
    provider_key: Literal["openai", "anthropic", "nat", "ollama"],
) -> bool:
    """provider_key가 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로 라우팅되는지 여부.

    이것은 provider 자체에서 파생한 "능력" 신호다 — 이번 호출에서 실제로 도구가
    호출됐다는 관측이 아니다 (#152 참고). AgentReport.provider_supports_tools와
    /api/v1/analyze 응답의 provider_supports_tools는 항상 이 함수로 파생시킨다 —
    호출부가 True/False를 손으로 넘기면 언젠가 어긋난다.
    """
    return provider_key in _TOOL_CAPABLE_PROVIDERS
