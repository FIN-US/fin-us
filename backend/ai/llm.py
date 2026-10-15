"""LLM 공급자 호출(OpenAI·Anthropic·Ollama·NAT)과 단일 진입점 ``llm_chat``.

``llm_chat``과 공급자 함수는 다른 모듈에서도 이 모듈을 거쳐(``llm.llm_chat``) 찾는다.
테스트가 바꿔 끼우는 지점을 이 모듈 하나로 두기 위해서다.
"""

import httpx
import json
import logging
from typing import Any, Literal, NamedTuple, overload

from anthropic import AsyncAnthropic
from fastapi import HTTPException
from openai import AsyncOpenAI

from backend.ai.user_preferences import nat_user_id_for_conversation
from backend.core.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_CHAT_MODEL,
    NAT_BASE_URL,
    NAT_CHAT_MODEL,
    NAT_CONVERSATION_ID,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OPENAI_API_KEY,
    OPENAI_CHAT_MODEL,
)
from backend.pii.egress import EgressPrompt, prepare_egress
from backend.pii.mask import unmask_pii
from backend.pii.registry import active_mapping

logger = logging.getLogger(__name__)


_NAT_RESPONSE_LOG_PREVIEW_CHARS = 800

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

    agents이 도구 강제 원장에서 만들어 보내는
    ``{"name": ..., "ok": ..., "empty": ...}``에 대응한다.

    ``ok=False``는 도구를 호출했지만 오류 응답을 받았다는 뜻이고,
    ``ok=True, empty=True``는 호출은 성공했지만 결과 집합이 비었다는 뜻이다(#209).
    셋 다 각주에서 구분해 표시해야 한다 — 실패나 빈 결과를 그냥 "확인한 자료"로
    보여주면 사용자는 답변이 그 데이터에 근거했다고 읽지만 실제로는 아니다.

    ``empty``에 기본값이 있는 이유: 이 필드를 싣지 않는 구버전 agents과 섞일 수
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

    두 필드는 agents이 supervisor의 라우팅 결과와 도구 강제 원장에서 만들어
    실어 보내는 값이다. 여기서는 **읽기만** 한다 — 답변 텍스트를 뒤져서 도구명이나
    에이전트명을 추측하지 않는다.

    ``tools_used``는 ``[{"name": str, "ok": bool, "empty": bool}]`` 형태다
    (``empty``는 구버전 agents에는 없다). 필드가 없거나 타입이
    어긋나면 조용히 비운다. 두 필드를 싣지 않는 구버전 agents과 혼용될 수 있고,
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
            # 호출 순서 유지 + 중복 제거. agents이 이미 그렇게 보내지만,
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
    # 텔레그램 채팅 스레드일 때만 사용자 식별자를 싣는다 — NAT 추천 브랜치가 이 값으로 저장된
    # 투자 성향을 읽는다(#397). 다른 스레드(스케줄러·실적·API)는 헤더가 없어 성향 없이 동작한다.
    user_id = nat_user_id_for_conversation(cid)
    if user_id is not None:
        headers["x-user-id"] = user_id
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
    user_msg: EgressPrompt,
    *,
    conversation_id: str | None = None,
) -> NatAnswer: ...


@overload
async def llm_chat(
    provider_key: Literal["openai", "anthropic", "ollama"],
    user_msg: EgressPrompt,
    *,
    conversation_id: str | None = None,
) -> str: ...


async def llm_chat(
    provider_key: Literal["openai", "anthropic", "nat", "ollama"],
    user_msg: EgressPrompt,
    *,
    conversation_id: str | None = None,
) -> str:
    """4개 provider 경로(OpenAI/Anthropic/Ollama/NAT)의 단일 진입점.

    #230(F-17/NFR-05): 외부로 나가는 프롬프트는 반드시 이 함수를 거쳐야 마스킹된다.
    _llm_openai_chat 등 provider별 구현을 직접 호출하는 새 경로가 생기면 마스킹
    계층을 우회한다 — backend/tests/test_services.py의 회귀 테스트가 이를 잡는다.

    #395: 마스킹은 `pii_egress.prepare_egress`가 한다. *user_msg*가 문자열이면 종전대로
    전체를 마스킹하고, 구간 목록이면 ``public()``으로 표시한 공개 데이터 구간은 건너뛴다.
    비식별화가 실패하면 `EgressBlocked`가 provider 호출 **전에** 올라온다 — 원문 전송
    fallback은 없다(근거는 pii_egress 모듈 docstring).

    mask_pii/unmask_pii는 이 함수 지역 변수(mapping)로만 존재한다. 요청마다 새로
    만들어지므로, llm_chat이 asyncio.gather 등으로 동시에 여러 번 호출돼도 서로의
    매핑을 덮어쓰지 않는다.

    다만 지역 변수인 것만으로는 **저장 경로가 막힌다**(#339). LLM이 사용자 발화를 보고
    쓴 매매일지는 backend가 만든 자리표시자를 싣고 `POST /api/v1/db/diary`로 오는데,
    그것은 이 왕복의 바깥이라 되돌릴 매핑이 없었다. 그래서 provider 호출 구간 동안만
    매핑을 scope 키로 등록해(`pii_registry.active_mapping`) 그 요청이 조회할 수 있게
    한다 — 수명은 여전히 이 호출이고, 등록소는 scope별로 갈려 있어 동시 호출이 서로를
    덮지 않는다(#354). 등록소 밖으로 나가는 순간 등록은 지워지므로 `unmask_pii`는
    블록 바깥에서 지역 변수로 돈다.
    """
    masked_msg, mapping = prepare_egress(user_msg, label=f"llm_chat:{provider_key}")

    with active_mapping(mapping):
        if provider_key == "openai":
            raw = await _llm_openai_chat(masked_msg)
        elif provider_key == "anthropic":
            raw = await _llm_anthropic_chat(masked_msg)
        elif provider_key == "ollama":
            raw = await _llm_ollama_chat(masked_msg)
        else:
            raw = await _llm_nat_chat(masked_msg, conversation_id=conversation_id)

    return unmask_pii(raw, mapping)


# llm_chat의 분기와 함께 유지한다: NAT처럼 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로
# 라우팅되는 provider만 여기 나열한다. _llm_openai_chat/_llm_anthropic_chat/
# _llm_ollama_chat은 tools 파라미터 없이 모델을 그대로 호출하므로, 이 셋에 대해
# False라는 것은 코드로 증명 가능하다 (#162). 반대로 "nat"은 NAT 멀티에이전트로
# 라우팅된다는 provider 차원의 능력만 의미할 뿐, 그 호출에서 실제로 도구가 실행됐다는
# 관측이 아니다 — NAT ReAct 에이전트가 도구 없이도 답을 낼 수 있는 문제(#152, 여섯
# agents/configs/agents/*.yml 모두 raise_on_parsing_failure: false)가 열려 있는
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
