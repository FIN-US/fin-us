import json
import httpx
import logging
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
        report = AgentReport(
            stock_code="",  # 향후 개선: MCP를 통해 코드를 가져오거나 분석 결과에서 추출
            stock_name=stock,
            provider=key,
            summary=data.get("summary", ""),
            decision=details.get("decision", "HOLD"),
            confidence_score=details.get("confidence_score", 0.0),
            reason=details.get("reason", ""),
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
    raw = await llm_chat("nat", prompt, conversation_id="morning-briefing")
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
