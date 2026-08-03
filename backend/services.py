import json
import httpx
import logging
import re
from datetime import date
from typing import Any, Literal, Optional
from urllib.parse import quote as _url_quote
from fastapi import HTTPException
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sqlmodel import Session
from .config import (
    OPENAI_API_KEY, OPENAI_CHAT_MODEL,
    ANTHROPIC_API_KEY, ANTHROPIC_CHAT_MODEL,
    OLLAMA_API_KEY, OLLAMA_MODEL, OLLAMA_BASE_URL,
    NAT_BASE_URL, NAT_CHAT_MODEL, NAT_CONVERSATION_ID,
    NEWS_MCP_PARAMS, TRADING_MCP_PARAMS,
)
from .schemas import TradingSignal, AnalysisReport
from .models import AgentReport

logger = logging.getLogger(__name__)
_NAT_RESPONSE_LOG_PREVIEW_CHARS = 800

# 입력이 이미 종목코드 형태인지 판정하는 범위. mcp-trading/stock-master.js의
# resolveStock() 지름길(6~7자 영숫자를 그대로 코드로 인정)과 상한을 맞춘다.
# KIS 국내 종목코드는 6자리 숫자(005930)만이 아니다. 코스닥 스팩·리츠 등 약 18%가
# 영문이 섞인 형태(0001A0)이고, 펀드는 더 길다(F70100026). 숫자 6자리만 인정하면
# MCP가 정확히 돌려준 코드를 백엔드가 스스로 버리게 된다.
_STOCK_CODE_RE = re.compile(r"^[0-9A-Z]{6,7}$")
# resolve_stock_code 응답 형식: "종목명 (코드, 시장)" — mcp-trading/index.js
# 종목명 자체에 괄호가 들어가는 경우가 있어(예: "...테이블1(A)") 코드+쉼표 조합에 앵커한다.
# 입력 판정(_STOCK_CODE_RE)과 달리 상한을 두지 않는다({6,}) — 이건 MCP *응답*에서 코드를
# 뽑아내는 정규식이라 6자(3,889종목)·7자 ETN(389종목)·9자 펀드(75종목, 전체 4,353종목 중)가
# 모두 나올 수 있다. {6,7}로 좁히면 펀드 코드 75종 전부가 조용히 빠진다.
# 숫자 불변식(_has_code_digit 적용)은 이 파일에만 있다 — telegram_commands.py의
# 같은 정규식은 이 검사가 없다. #140에서 두 곳을 통합할 때 함께 옮겨야 한다.
_STOCK_CODE_EXTRACT_RE = re.compile(r"\(([0-9A-Z]{6,}),")
# 비대칭 주의: 위 두 정규식의 상한이 다르다 보니, 사용자가 9자 펀드 코드를 직접 입력해도
# 그대로 통과되지 않는다. _looks_like_stock_code의 {6,7}이 9자 입력을 코드로 보지 않아
# MCP를 호출하지만, stock-master.js의 지름길도 똑같이 {6,7}이라 이 입력을 코드로 인정하지
# 않고 정확한 종목명 매칭으로 넘어가 결국 "찾을 수 없음" 예외를 던진다. 즉 추출 쪽 상한을
# 넓힌 것은 MCP가 돌려준 9자 코드를 버리지 않기 위함이지, 사용자가 9자 코드를 직접 입력해도
# 왕복되게 만들지는 않는다.

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


def _has_code_digit(value: str) -> bool:
    """실제 종목코드는 항상 숫자를 포함한다(종목마스터 4,353종 전수 확인, 0건 예외).

    입력 판정과 MCP 추출 결과 검증이 같은 불변식을 쓰도록 한 곳에 둔다.
    """
    return any(ch in "0123456789" for ch in value)


def _looks_like_stock_code(stock: str) -> bool:
    """이미 종목코드 형태여서 MCP 조회를 생략해도 되는지 판정합니다.

    주의(#150 이후): stock-master.js의 resolveStock은 코드 형태 입력이라도 이름·별칭
    매칭을 먼저 시도하고 실패했을 때만 코드로 인정한다. 이 함수는 마스터를 볼 수 없어
    그 확인 없이 코드로 확정하므로, JS보다 더 공격적이다.
    지금은 _has_code_digit이 안전망 역할을 한다 — 코드 형태 이름 3종(SIMPAC/INVENI/
    WISCOM)이 모두 숫자를 포함하지 않아 여기서 걸러지고 MCP로 넘어간다.
    숫자를 포함한 6~7자 이름이 신규 상장되면 이 안전망이 뚫린다. mcp-trading/tests/
    stock-master.test.js의 "exactly 3 master stock names..." 테스트가 그 신호이며,
    그 테스트가 깨지면 이 함수도 함께 재검토해야 한다.
    """
    value = stock.strip().upper()
    return bool(_STOCK_CODE_RE.match(value)) and _has_code_digit(value)


async def _resolve_stock_code(stock: str) -> str:
    """종목명(또는 이미 종목코드인 값)을 종목코드로 변환합니다.

    이미 종목코드 형태이면 MCP 호출 없이 그대로 사용합니다.
    조회 실패/예외 시 리포트 저장을 막지 않도록 빈 문자열로 폴백합니다.
    """
    stock = _normalize_stock_input(stock)
    if _looks_like_stock_code(stock):
        return stock.upper()

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
        if code is None or not _has_code_digit(code):
            logger.warning(
                "종목코드 추출 실패, 빈 문자열로 폴백: stock=%s, response=%s",
                stock,
                resolved,
            )
            return ""
        # 지름길 에코("SIMPAC (SIMPAC, UNKNOWN)")는 숫자가 없어 위 _has_code_digit
        # 가드에서 이미 걸러진다.
        # #150 이후 resolveStock이 이름·별칭 매칭을 코드 지름길보다 먼저 시도하므로,
        # 실제 마스터 데이터로는 이 에코가 더 이상 발생하지 않는다. 지금은 사용자가
        # 코드 형태 입력을 직접 준 경우에 대한 방어로만 남아 있다.
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


async def perform_stock_analysis(
    stock: str,
    provider: str,
    session: Session,
    *,
    trigger_source: str | None = None,
    trigger_signal: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    종목 분석을 수행하고 결과를 DB에 저장한 뒤 반환합니다.
    API 엔드포인트와 백그라운드 스케줄러에서 공용으로 사용됩니다.
    """
    trigger_context = ""
    if trigger_signal:
        source_label = trigger_source or "signal"
        trigger_context = (
            f"\n분석 트리거 데이터 출처: {source_label}\n"
            f"--- 트리거 데이터 ---\n{trigger_signal[:4000]}\n"
            "------------------\n"
            "위 트리거 데이터를 투자 판단의 주요 근거로 반영하라. "
            "필요하면 라우터·서브에이전트로 보조 시장 데이터를 추가 확인하라.\n"
        )

    user_msg = (
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
    
    key = normalize_llm_provider(provider)
    nat_cid = conversation_id
    if key == "nat" and nat_cid is None:
        nat_cid = _nat_conversation_id(stock, trigger_source=trigger_source)
    raw = await llm_chat(key, user_msg, conversation_id=nat_cid)
    data = analysis_from_nat_text(str(raw), stock)

    # 분석 리포트를 데이터베이스에 자동으로 저장
    try:
        details = data.get("details", {})
        stock_code = await _resolve_stock_code(stock)
        report = AgentReport(
            stock_code=stock_code,
            stock_name=stock,
            provider=key,
            summary=data.get("summary", ""),
            decision=details.get("decision", "HOLD"),
            confidence_score=details.get("confidence_score", 0.0),
            reason=details.get("reason", ""),
            tool_verified=provider_is_tool_backed(key),
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


def _morning_briefing_from_text(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    for data in _json_objects_from_text(text):
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


async def check_signal_significance(
    stock: str,
    signal_content: str,
    last_signal_content: Optional[str] = None,
    *,
    source: str = "signal",
    provider: str = "ollama"
) -> bool:
    """
    외부 signal을 분석하여 투자 관점에서 유의미한 변화가 있는지 판단합니다.
    로컬 초경량 모델(예: gemma4) 또는 경량 API 모델(예: gpt-5.4-mini)을 
    1차 필터로 사용하여 비용과 정확도의 균형을 맞출 수 있습니다.
    """
    if not signal_content:
        return False
    
    if signal_content == last_signal_content:
        return False

    # signal 텍스트 상단 1000자 사용
    snip_content = signal_content[:1000]

    prompt = (
        f"당신은 전문 주식 분석가입니다. 다음은 '{stock}' 종목에 대한 최신 외부 signal입니다.\n"
        f"signal 출처: {source}\n\n"
        f"--- signal 내용 ---\n{snip_content}\n"
        "------------------\n\n"
        "이 signal이 기존 상황을 바꿀 만큼 유의미한 새로운 투자 정보"
        "(실적 발표, 계약, M&A, 수급 변화, 평판 리스크, 급격한 여론 변화 등)를 포함하고 있습니까?\n"
        "중요한 변화가 있다면 'YES', 사소하거나 반복되는 signal이라면 'NO'라고만 답하세요.\n"
        "답변은 반드시 다른 설명 없이 'YES' 또는 'NO' 중 하나로만 대답하십시오."
    )

    try:
        # 설정된 provider(ollama, openai 등)에 따라 경량 모델 호출
        result = await llm_chat(provider, prompt)
        is_significant = "YES" in result.upper()
        logger.info(
            "signal 유의미성 확인 [%s:%s] (Provider: %s): %s",
            source,
            stock,
            provider,
            is_significant,
        )
        return is_significant
    except Exception as e:
        logger.error(f"signal 유의미성 확인 중 오류 발생 ({source}, {stock}, {provider}): {e}")
        return True


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


async def _llm_nat_chat(user_msg: str, *, conversation_id: str | None = None) -> str:
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
        return _nat_message_from_payload(payload)
    except (KeyError, IndexError, TypeError) as exc:
        body_snip = resp.text[:1200] if resp.text else ""
        raise HTTPException(
            status_code=502,
            detail=(
                f"NAT 응답 형식 오류 ({exc}). NAT_CHAT_MODEL이 NAT 서비스에서 쓰는 모델과 일치하는지 확인하세요. "
                f"body[:1200]={body_snip!r}"
            ),
        ) from exc


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


def analysis_from_nat_text(raw: str, stock: str) -> dict[str, Any]:
    """Map NAT assistant output to AnalysisReport JSON for the reference React UI."""
    text = (raw or "").strip()
    for data in _json_objects_from_text(text):
        try:
            report = AnalysisReport(**data)
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
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return list(reversed(objects))


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


# llm_chat의 분기와 함께 유지한다: 실제로 MCP/KIS/뉴스 도구를 호출하는 provider만 여기
# 나열한다. _llm_openai_chat/_llm_anthropic_chat/_llm_ollama_chat은 tools 파라미터 없이
# 모델을 그대로 호출하므로 도구 근거가 없다 (#162). nat만 NAT 멀티에이전트를 거쳐
# 실제 시장 데이터를 조회한다. 새 provider가 도구를 갖추게 되면 이 집합 한 곳만 갱신하면
# provider_is_tool_backed()를 쓰는 모든 호출부(AgentReport.tool_verified 등)에 반영된다.
_TOOL_BACKED_PROVIDERS: frozenset[str] = frozenset({"nat"})


def provider_is_tool_backed(
    provider_key: Literal["openai", "anthropic", "nat", "ollama"],
) -> bool:
    """provider_key가 실제 도구(MCP/KIS/뉴스)를 호출해 응답을 생성하는지 여부.

    AgentReport.tool_verified는 항상 이 함수로 파생시킨다 — 호출부가 True/False를
    손으로 넘기면 언젠가 어긋난다.
    """
    return provider_key in _TOOL_BACKED_PROVIDERS
