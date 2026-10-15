"""종목 분석 리포트·모닝 브리핑 생성과 종목코드 해석."""

import logging
import re
from datetime import date
from typing import Any, Protocol, Sequence

from mcp import StdioServerParameters
from pydantic import ValidationError
from urllib.parse import quote as _url_quote

from backend.ai import llm, mcp_tools
from backend.ai.llm import _json_objects_from_text, normalize_llm_provider, provider_supports_tools
from backend.ai.signals import _URGENCY_LEVELS, SignalScore
from backend.core.config import NEWS_MCP_PARAMS, TRADING_MCP_PARAMS
from backend.core.models import AgentReport
from backend.core.schemas import AnalysisReport, TradingSignal
from backend.pii.egress import personal, public, Segment
from backend.trading.stock_code import (
    _has_code_digit,
    _is_known_master_code,
    _is_unresolved_echo,
    _looks_like_stock_code,
    _STOCK_CODE_EXTRACT_RE,
)

logger = logging.getLogger(__name__)


# LLM 출력이 절대 정할 수 없는, 서버가 provider에서만 파생하는 필드.
# analysis_from_nat_text가 모델의 raw JSON을 그대로 AnalysisReport에 넘기면
# 악의적이거나 비정상적인 LLM 출력이 이 값들을 주입할 수 있으므로 파싱 단계에서
# 걷어낸다. 실제 값은 perform_stock_analysis가 provider_supports_tools()로 채운다.
_DERIVED_PROVENANCE_FIELDS = frozenset({"provider", "provider_supports_tools"})

# _STOCK_CODE_EXTRACT_RE, _has_code_digit, _looks_like_stock_code → backend/trading/stock_code.py (#140)

# 종목명 -> 종목코드 프로세스 메모리 캐시.
# resolve_stock_code MCP 호출은 매번 새 stdio 서브프로세스를 띄우는 비용이 있고
# (run_mcp_tool 참고), 종목명-코드 매핑은 사실상 불변이므로 캐싱해 재조회를 피한다.
_stock_code_cache: dict[str, str] = {}
# 종목마스터는 4,401종(별칭 포함해도 여유롭게 포함)이므로 이 상한을 정상적으로
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

    이미 종목코드 형태이면서 종목마스터(mcp/trading/data/stocks.json)에 실재하면
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
        resolved = await mcp_tools.run_mcp_tool(
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
        # 상한 도달은 정상 조건이 아니다 — 종목마스터 4,401종 규모에서는 일어나지
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
    trigger_signal: str | Segment | None,
) -> list[str | Segment]:
    """trigger_signal이 있을 때 프롬프트에 삽입할 컨텍스트 구간을 만든다.

    trigger_signal의 공개/개인 표시는 호출부가 정한 그대로 옮긴다(#395). 스케줄러는 뉴스·공시
    원문을 ``public()``으로 넘긴다. 맨 문자열은 출처를 모르는 값이라 개인 구간으로 마스킹된다.
    """
    if not trigger_signal:
        return []
    signal = trigger_signal if isinstance(trigger_signal, Segment) else personal(trigger_signal)
    source_label = trigger_source or "signal"
    return [
        f"\n분석 트리거 데이터 출처: {source_label}\n--- 트리거 데이터 ---\n",
        Segment(signal.text[:4000], public=signal.public),
        "\n------------------\n"
        "위 트리거 데이터를 투자 판단의 주요 근거로 반영하라. "
        "필요하면 라우터·서브에이전트로 보조 시장 데이터를 추가 확인하라.\n",
    ]


def _build_nat_prompt(
    stock: str, trigger_context: Sequence[str | Segment]
) -> list[str | Segment]:
    """NAT 멀티에이전트용 프롬프트. 도구를 활용해 BUY/SELL/HOLD 판단을 생성한다.

    provider=nat 경로 전용이며, provider_supports_tools=True인 경우에만 호출한다.
    """
    return [
        f"종목: {stock}. 라우터·서브에이전트를 활용해 투자 관점 분석을 하라. ",
        *trigger_context,
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
        '"telegram_alert":true|false}',
    ]


def _build_toolless_prompt(
    stock: str, trigger_context: Sequence[str | Segment]
) -> list[str | Segment]:
    """도구 없는 provider(openai/anthropic/ollama)용 프롬프트.

    실시간 시장 데이터(MCP/KIS/뉴스)에 접근하지 못하므로 BUY/SELL/HOLD 판단과
    신뢰도 점수를 생성하지 않는다 (#162 A). 일반적 배경 설명만 허용한다.
    """
    return [
        f"종목: {stock}에 대한 일반적인 배경 정보와 투자 관련 개요를 설명하라. ",
        *trigger_context,
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
        '"telegram_alert":false}',
    ]


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


class ReportSession(Protocol):
    """perform_stock_analysis가 세션에 요구하는 전부 (#319).

    이 함수가 세션으로 하는 일은 리포트 한 행을 넣고 커밋한 뒤 새로 읽는 것뿐이다.
    선언 타입이 sqlmodel ``Session``이면 질의·트랜잭션 API 전체가 계약이 되어, 세 메서드만
    갖춘 대역이 주입 지점을 통과하지 못한다 — 그 대역을 통과시키려고 호출부가 cast를 쓰면
    저장 경로 자체가 검사 밖으로 나간다.

    인자를 위치 전용(``/``)으로 선언한다. 호출부가 전부 위치 인자로 부르므로 이름은
    계약이 아니고, 이름까지 고정하면 대역이 ``report`` 같은 자기 도메인 이름을 쓰지
    못한다.
    """

    def add(self, instance: object, /) -> None: ...

    def commit(self) -> None: ...

    def refresh(self, instance: object, /) -> None: ...


async def perform_stock_analysis(
    stock: str,
    provider: str,
    session: ReportSession,
    *,
    trigger_source: str | None = None,
    trigger_signal: str | Segment | None = None,
    signal_score: "SignalScore | None" = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    종목 분석을 수행하고 결과를 DB에 저장한 뒤 반환합니다.
    API 엔드포인트와 백그라운드 스케줄러에서 공용으로 사용됩니다.

    trigger_signal: 뉴스·공시 같은 공개 데이터면 호출부가 ``pii_egress.public()``으로 감싸
    넘긴다. 맨 문자열은 출처를 모르는 값으로 보고 외부 전송 전에 마스킹한다(#395).

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
        raw = await llm.llm_chat(key, user_msg, conversation_id=nat_cid)
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
        raw = await llm.llm_chat(key, user_msg, conversation_id=nat_cid)
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
    # 공개/개인 구분(#395): 뉴스·종목 수급은 공개, 잔고 리포트는 개인이다. 기준은
    # backend/pii/egress.py 모듈 docstring. 잔고만 마스킹되고 뉴스 속 금액은 그대로 나간다.
    market_summary_source = await _collect_morning_context(
        NEWS_MCP_PARAMS,
        "get_market_news",
        {"stock_name": "미국 증시"},
        public_source=True,
    )
    balance_text = await _collect_morning_context(
        TRADING_MCP_PARAMS, "get_balance", {}, public_source=False
    )

    watchlist_context: list[str | Segment] = []
    for stock in stocks:
        news = await _collect_morning_context(
            NEWS_MCP_PARAMS,
            "get_market_news",
            {"stock_name": stock},
            public_source=True,
        )
        trading = await _collect_morning_context(
            TRADING_MCP_PARAMS,
            "get_investor_trading",
            {"stock_name": stock},
            public_source=True,
        )
        if watchlist_context:
            watchlist_context.append("\n\n")
        watchlist_context.extend([f"[{stock}]\n뉴스: ", news, "\n수급: ", trading])

    prompt: list[str | Segment] = [
        "Strategy Planner 관점으로 오늘 장 시작 전 Telegram 모닝 브리핑을 작성하라.\n"
        "반드시 다음 JSON 객체 한 개만 출력하라:\n"
        '{"market_summary":"전일 미국/선물 시장 동향과 주요 이슈",'
        '"watchlist":["종목별 뉴스 및 수급 요약"],'
        '"trading_ideas":["오늘의 간략 시나리오"],'
        '"catalysts":["당일/금주 주요 촉매 이벤트"]}\n\n'
        "시장 뉴스:\n",
        market_summary_source,
        "\n\n잔고:\n",
        balance_text,
        "\n\n관심종목 컨텍스트:\n",
        *(watchlist_context or ["관심종목 없음"]),
    ]
    raw = await llm.llm_chat("nat", prompt, conversation_id=f"morning-briefing:{date.today().isoformat()}")
    return _morning_briefing_from_text(str(raw))


async def _collect_morning_context(
    mcp_params: StdioServerParameters,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    public_source: bool,
) -> Segment:
    """MCP 결과를 프롬프트 구간으로 돌려준다.

    *public_source*는 조회 결과에만 붙인다. 실패 문구는 예외 메시지를 싣고, 예외 메시지에
    무엇이 들어 있을지는 도구가 정하므로 출처와 무관하게 개인 구간(마스킹 대상)으로 둔다.
    """
    try:
        result = await mcp_tools.run_mcp_tool(mcp_params, tool_name, arguments)
    except Exception as exc:
        logger.error("Morning briefing source failed for %s: %s", tool_name, exc)
        return personal(f"{tool_name} 조회 실패: {exc}")
    return public(result) if public_source else personal(result)


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
