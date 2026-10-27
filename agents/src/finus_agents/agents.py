import logging
import os
import re
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import ChatRequestOrMessage
from nat.data_models.api_server import ChatResponse
from nat.data_models.api_server import Message
from nat.data_models.api_server import Usage
from nat.data_models.api_server import UserMessageContentRoleType
from nat.data_models.component_ref import FunctionRef
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig
from nat.utils.type_converter import GlobalTypeConverter
from finus_agents.finus_api import (
    DATA_TOOL_LEDGER,
    REASONING_TRACE,
    DataToolLedger,
    ReasoningTrace,
)
from finus_agents.pii_guard import (
    INPUT_AMOUNTS_HEADER,
    PII_MAPPING,
    REQUEST_INPUT_AMOUNT_BOUNDS,
    REQUEST_INPUT_AMOUNTS,
    input_amount_bounds,
    input_amounts_from_header,
    install_mapping_box,
    unmask_response,
)

logger = logging.getLogger(__name__)

MEMORY_PROMPT_PREFIX = "Relevant context from memory:"
CONVERSATION_ID_HTTP_HEADER = "conversation-id"
_DEFAULT_CONVERSATION_ID = os.environ.get(
    "FINUS_DEFAULT_CONVERSATION_ID",
    "fin-us-default",
).strip()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---- Tool enforcement gate (#152) ----

# Matches financial numbers: "5,000,000원", "500만원", "1억원", "7만 5300원", "5.3%", "100주".
# `주` uses a negative lookahead to exclude time expressions like "2주 동안", "1주일", "2주간".
_FIN_NUM_RE = re.compile(
    r"[\d,]+(?:\.\d+)?"                           # base number with optional decimal
    r"(?:\s*[만억조]\s*(?:[\d,]+(?:\.\d+)?)?)*"   # 만/억/조 chain; trailing digits optional
    r"\s*(?:원|달러|위안|엔|%"
    r"|주(?!\s*(?:일|간|동안|가\s*(?:가|는|를|에|의|상승|하락|올|내)|식|말|중|후|째|만에))"  # 주: exclude time/price(주가+조사) expressions
    r"|좌)",
    re.UNICODE,
)

_KO_UNIT_VALUES: dict[str, int] = {
    "만": 10_000,
    "억": 100_000_000,
    "조": 1_000_000_000_000,
}
# Regex for one numeric term: digits (with optional decimal) + optional Korean unit.
_NUM_TERM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([만억조])?")
# Scale factor: multiply normalised value by 1000 to preserve up to 3 decimal places.
# This prevents int(float(s)) truncation that would equate 5.3% and 5.7%.
_NUM_SCALE = 1000


def _normalize_number_str(s: str) -> int | None:
    """Normalize a comma/Korean-unit number to a scaled integer (×_NUM_SCALE=1000).

    Examples: ``"75,300"`` → 75_300_000; ``"7만5300"`` → 75_300_000;
              ``"500만"`` → 5_000_000_000; ``"5.3"`` → 5_300 (≠ ``"5.7"`` → 5_700).
    Returns None when *s* cannot be fully parsed (e.g. trailing non-numeric residue).
    """
    s = s.replace(",", "").strip()
    if not s:
        return None
    total, pos, seen = 0.0, 0, False
    for m in _NUM_TERM_RE.finditer(s):
        if m.start() != pos:      # gap between matches means unparsed residue
            return None
        pos, seen = m.end(), True
        value = float(m.group(1))
        total += value * _KO_UNIT_VALUES[m.group(2)] if m.group(2) else value
    if not seen or pos != len(s):
        return None
    return int(round(total * _NUM_SCALE))


def _extract_financial_numbers(text: str) -> frozenset[int]:
    """Return a frozenset of normalized integer values of all financial numbers in *text*."""
    result: set[int] = set()
    for m in _FIN_NUM_RE.finditer(text):
        raw = re.sub(r"[원달러위안엔%주좌\s]", "", m.group()).strip()
        n = _normalize_number_str(raw)
        if n is not None:
            result.add(n)
    return frozenset(result)


def _has_numeric_claims(text: str) -> bool:
    """Return True when *text* contains any numeric financial claim."""
    return bool(_FIN_NUM_RE.search(text))


def _check_tool_enforcement(
    answer: str,
    ledger: DataToolLedger,
    chat_request: ChatRequest,
) -> bool:
    """Return True when the gate should trip (answer must be retried or rejected).

    The gate trips only when ALL three conditions hold:
    1. The answer contains financial numeric claims.
    2. No data tool produced a successful result.
    3. The claimed numbers are NOT already present in the input transcript
       (an agent restating previously provided numbers must pass).
    """
    if not _has_numeric_claims(answer):
        return False
    if ledger.any_success():
        return False
    answer_nums = _extract_financial_numbers(answer)
    if not answer_nums:
        return False
    transcript_text = "\n".join(message_content_text(m) for m in chat_request.messages)
    transcript_nums = _extract_financial_numbers(transcript_text)
    return not answer_nums.issubset(transcript_nums)


# Deterministic rejection returned when both retry attempts fail.
# Must NOT be model-generated text.
_TOOL_ENFORCEMENT_REJECTION = (
    "[데이터 미확인] 데이터 조회 도구 없이 수치가 포함된 답변이 반복됐습니다. "
    "잔고·현재가·거래내역 등은 반드시 도구로 조회한 뒤 제시해야 합니다. "
    "다시 질문해 주세요."
)

# Returned when every read tool executed but returned an empty result set.
# Skips the retry — the second attempt cannot produce rows either.
# Must NOT be model-generated text.
_EMPTY_RESULT_REJECTION = (
    "[조회 결과 없음] 요청하신 조건으로 조회했으나 데이터가 없습니다. "
    "조회 기간이나 종목을 바꿔 다시 질문해 주세요."
)

# Prepended to the query on the second attempt to force tool usage.
# Changes the input rather than repeating the same system-prompt instruction
# (which the model already ignored on the first attempt).
_CORRECTIVE_TOOL_PREFIX = (
    "[도구 강제] 반드시 적절한 데이터 조회 도구를 먼저 호출하여 Observation을 받은 후 답변하세요. "
    "도구 없이 수치를 포함한 답변은 거부됩니다.\n\n"
)


# ---- Reasoning trace recording (#260) ----


def _trace_ledger_tools(ledger: DataToolLedger) -> None:
    """*ledger*에 기록된 도구를 현재 요청의 추론 기록에 병합한다.

    기록 원천은 도구 강제 원장뿐이다 — 모델 출력 텍스트를 파싱하지 않는다
    (#129와 같은 원칙: provenance는 코드가 관측한 것에서만 만든다).

    추론 기록 박스가 없으면(박스를 심지 않는 config나 CLI 경로) 조용히 no-op한다.
    각주는 답변에 덧붙는 부가 정보이므로, 없다고 해서 답변 경로를 막지 않는다.
    """
    trace = REASONING_TRACE.get()
    if trace is not None:
        trace.record_ledger_tools(ledger)


def _trace_route(branch_name: str) -> None:
    """supervisor가 고른 브랜치명을 현재 요청의 추론 기록에 남긴다.

    ``_choose_branch``가 ``_normalize_branch``로 허용 목록에 정규화한 뒤의 값만
    들어온다 — LLM이 출력한 원문 문자열이 그대로 흘러가지 않는다.
    """
    trace = REASONING_TRACE.get()
    if trace is not None:
        trace.record_route(branch_name)


def _trace_branch_answer(text: str) -> None:
    """브랜치가 만든 답변 본문을 현재 요청의 추론 기록에 남긴다 (#294).

    최상위(:func:`with_reasoning_trace`)가 각주를 붙일지 판정하는 기준이다 —
    근거는 :meth:`ReasoningTrace.record_branch_answer` 참고.
    """
    trace = REASONING_TRACE.get()
    if trace is not None:
        trace.record_branch_answer(text)


async def _run_with_gate(
    *,
    inner: Any,
    query: str,
    chat_request: ChatRequest,
    inner_name: str,
) -> str:
    """Invoke *inner* agent with the tool enforcement gate applied.

    First attempt:
        Run with *query*; return the answer if the gate passes.
    Second attempt (gate tripped on first):
        Re-run with a *changed* input that prepends an explicit tool-use directive.
        Return the second answer if the gate passes.
    Both attempts fail:
        Return *_TOOL_ENFORCEMENT_REJECTION* — a deterministic, non-model string.
    """
    # --- First attempt ---
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        result = await inner.ainvoke(ChatRequestOrMessage(input_message=query))
    finally:
        DATA_TOOL_LEDGER.reset(token)
        _trace_ledger_tools(ledger)  # #260: 실행된 도구를 각주용 기록에 병합
    answer = chat_response_plain_text(result)

    if not _check_tool_enforcement(answer, ledger, chat_request):
        return answer

    # Gate tripped — log judgment details for post-hoc analysis.
    transcript_text = "\n".join(message_content_text(m) for m in chat_request.messages)
    logger.warning(
        "Tool enforcement gate tripped on first attempt (%s) — retrying. "
        "claimed=%s transcript=%s ledger_records=%d",
        inner_name,
        sorted(_extract_financial_numbers(answer)),
        sorted(_extract_financial_numbers(transcript_text)),
        len(ledger.records),
    )

    # Side-effect guard: if a write tool already ran, skip the retry to avoid
    # duplicate side effects (e.g. duplicate diary entries).
    if ledger.had_side_effect():
        logger.error(
            "Tool enforcement gate tripped on first attempt (%s) but a side-effecting "
            "tool ran — skipping retry to avoid duplicate writes",
            inner_name,
        )
        # 재시도는 어느 쪽이든 건너뛴다. 메시지만 실제 상황에 맞춘다:
        # 쓰기 도구와 함께 빈 조회가 실행됐다면 "도구 없이"는 사실이 아니다.
        return _EMPTY_RESULT_REJECTION if ledger.only_empty_reads() else _TOOL_ENFORCEMENT_REJECTION

    # Empty-result guard: all read tools executed but returned empty data.
    # Retrying is futile — the tools will return the same empty result again.
    # Return an honest "no data" message instead of the misleading _TOOL_ENFORCEMENT_REJECTION.
    if ledger.only_empty_reads():
        logger.info(
            "Gate tripped but every read tool returned an empty result (%s) — "
            "skipping retry; the retry cannot produce rows.",
            inner_name,
        )
        return _EMPTY_RESULT_REJECTION

    # --- Second attempt with changed input ---
    corrective_query = _CORRECTIVE_TOOL_PREFIX + query
    ledger2 = DataToolLedger()
    token2 = DATA_TOOL_LEDGER.set(ledger2)
    try:
        result2 = await inner.ainvoke(ChatRequestOrMessage(input_message=corrective_query))
    finally:
        DATA_TOOL_LEDGER.reset(token2)
        _trace_ledger_tools(ledger2)  # #260: 재시도에서 실행된 도구도 병합(중복 제거)
    answer2 = chat_response_plain_text(result2)

    if not _check_tool_enforcement(answer2, ledger2, chat_request):
        return answer2

    # 1차와 동일: 2차에서 도구는 실행됐고 결과가 비었을 뿐이라면
    # "도구 없이"라고 말하는 _TOOL_ENFORCEMENT_REJECTION은 사실과 다르다.
    # 교정 프리픽스가 2차의 도구 호출을 유도하므로 흔한 경로다.
    if ledger2.only_empty_reads():
        logger.info(
            "Gate tripped twice (%s) but the retry's read tools all returned "
            "empty results — returning the honest no-data message.",
            inner_name,
        )
        return _EMPTY_RESULT_REJECTION

    logger.error(
        "Tool enforcement gate tripped twice (%s) — returning deterministic rejection",
        inner_name,
    )
    return _TOOL_ENFORCEMENT_REJECTION


def message_content_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(str(part) for part in content)


def is_memory_system_message(message: Message) -> bool:
    if message.role != UserMessageContentRoleType.SYSTEM:
        return False
    return message_content_text(message).lstrip().startswith(MEMORY_PROMPT_PREFIX)


def messages_without_memory(messages: list[Message]) -> list[Message]:
    return [m for m in messages if not is_memory_system_message(m)]


def latest_user_plain_text(messages: list[Message]) -> str:
    if not messages:
        return ""
    for message in reversed(messages):
        if message.role == UserMessageContentRoleType.USER:
            return message_content_text(message)
    return message_content_text(messages[-1])


def role_short_label(role: UserMessageContentRoleType) -> str:
    if role == UserMessageContentRoleType.USER:
        return "user"
    if role == UserMessageContentRoleType.ASSISTANT:
        return "assistant"
    if role == UserMessageContentRoleType.SYSTEM:
        return "system"
    return str(role)


def truncate_end(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


# 가운데를 줄였다는 표지. 모델이 줄인 자리를 원문의 일부로 읽지 않게 말로 적는다.
_MIDDLE_ELISION = " …(중략)… "
# 앞부분 몫. 목록형 답(잔고·순위·종목별 뉴스)은 항목이 앞에서 시작하므로 앞을 더 남기고,
# 끝에는 결론과 추가 제안("…비교해 드릴까요?")이 들어갈 만큼만 남긴다.
_MIDDLE_HEAD_SHARE = 0.6


def truncate_middle(text: str, max_chars: int) -> str:
    """앞과 끝을 남기고 가운데를 줄여 *max_chars* 이내로 만든다 (#451).

    직전 답을 가리키는 후속 질문은 두 곳을 본다 — "그중 두 번째 종목은?"은 앞쪽 목록을,
    추가 제안에 대한 "응"은 답의 끝을 가리킨다. :func:`truncate_end`는 끝을 버리므로
    후자가 항상 끊긴다.
    """
    if len(text) <= max_chars:
        return text
    available = max_chars - len(_MIDDLE_ELISION)
    if available < 2:
        return truncate_end(text, max_chars)
    head = int(available * _MIDDLE_HEAD_SHARE)
    tail = available - head
    return text[:head].rstrip() + _MIDDLE_ELISION + text[len(text) - tail:].lstrip()


def _last_assistant_index(messages: list[Message]) -> int | None:
    """가장 뒤의 assistant 메시지 위치. 후속 질문이 가리키는 "직전 답"이다."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == UserMessageContentRoleType.ASSISTANT:
            return index
    return None


def chat_response_plain_text(result: ChatResponse | str) -> str:
    if isinstance(result, str):
        return result
    if result.choices and result.choices[0].message.content is not None:
        return result.choices[0].message.content
    return ""


def chat_response_with_text(response: ChatResponse, text: str) -> ChatResponse:
    """*response*의 본문만 *text*로 갈아 끼운 사본을 돌려준다 (#231의 역치환용).

    :func:`chat_response_plain_text`가 읽는 바로 그 자리(``choices[0].message.content``)를
    쓴다. 중첩 model_copy를 쓰는 이유는 ``with_reasoning_trace``와 같다 — 다른 필드
    (``usage``/``model``/``extra="allow"``로 붙은 ``routed_agent``·``tools_used``)를
    건드리지 않아야 backend의 ``_nat_message_from_payload``가 그대로 동작한다.

    본문을 실을 자리가 없으면(choices가 비었을 때) 원본을 그대로 돌려준다 —
    :func:`chat_response_plain_text`도 그 경우 ``""``를 돌려주므로 갈아 끼울 것이 없다.
    """
    if not response.choices:
        return response
    choice = response.choices[0]
    message = choice.message.model_copy(update={"content": text})
    return response.model_copy(update={"choices": [choice.model_copy(update={"message": message}), *response.choices[1:]]})


class FeBranchConfig(FunctionBaseConfig, name="fe_branch"):
    inner_function_name: str = Field(
        ...,
        min_length=1,
        description="Name of the function entry in the same config (the nested react_agent).",
    )
    tool_description: str | None = Field(
        default=None,
        description="Optional LangChain tool description; defaults to a generic delegation message.",
    )
    max_history_messages: int = Field(
        default=4,
        ge=1,
        le=32,
        description="서브 에이전트에 넣을 최근(비-memory) 메시지 개수 상한. 오래된 메시지부터 잘림.",
    )
    history_message_max_chars: int = Field(
        default=280,
        ge=60,
        le=8000,
        description="히스토리에서 user/system 등 한 메시지당 최대 문자 수(말줄임).",
    )
    history_assistant_message_max_chars: int = Field(
        default=180,
        ge=40,
        le=8000,
        description="히스토리에서 직전 답보다 앞선 assistant 한 메시지당 최대 문자 수. 긴 잔고 답변을 짧게 잘라 ReAct 입력 부담을 줄임.",
    )
    # #451: 직전 답까지 180자로 자르면 "그중 두 번째 종목은?"(앞쪽 목록)과 추가 제안에 대한 "응"(답의 끝)이
    # 둘 다 맥락을 잃는다. 후속 질문은 거의 항상 바로 앞 답을 가리키므로 그 한 건만 넉넉히 넘기고,
    # 더 오래된 답은 위 180자를 그대로 둔다 — ReAct 입력은 루프의 매 LLM 호출마다 다시 실리므로
    # 늘어나는 양을 한 건으로 묶어 비용 증가에 상한을 둔다. 넘치면 가운데를 줄인다(truncate_middle).
    history_last_assistant_message_max_chars: int = Field(
        default=1000,
        ge=40,
        le=8000,
        description="현재 요청 바로 앞 assistant 답의 최대 문자 수. 넘으면 앞·끝을 남기고 가운데를 줄임(#451).",
    )
    history_transcript_max_chars: int = Field(
        # 기본 4건(오래된 답 180 + 사용자 280 + 직전 답 1000 + 사용자 280)과 역할 표지가 들어가는 크기.
        # 1200이면 직전 답이 길 때 앞선 메시지가 통째로 밀려난다.
        default=2000,
        ge=200,
        le=500_000,
        description="[Recent conversation] 블록 전체 문자 상한. 초과 시 가장 오래된 메시지부터 제거.",
    )


def _history_line(
    message: Message,
    *,
    is_last_assistant: bool,
    user_cap: int,
    assistant_cap: int,
    last_assistant_cap: int,
) -> str:
    text = message_content_text(message).strip()
    if is_last_assistant:
        body = truncate_middle(text, last_assistant_cap)
    elif message.role == UserMessageContentRoleType.ASSISTANT:
        body = truncate_end(text, assistant_cap)
    else:
        body = truncate_end(text, user_cap)
    return f"{role_short_label(message.role)}: {body}"


def _contextual_user_query(
    chat_request: ChatRequest,
    max_history_messages: int,
    user_message_max_chars: int,
    assistant_message_max_chars: int,
    transcript_max_chars: int,
    last_assistant_message_max_chars: int,
) -> str:
    """서브 에이전트(ReAct)에 넘길 질문을 최근 대화와 현재 요청으로 만든다.

    히스토리는 transcript(SQLite)가 저장한 본문 그대로다 — 최상위
    ``finus_reasoning_trace_agent``가 역치환하기 **전**의 응답을 저장하므로 계좌 금액·수량은
    이전 턴의 자리표시자로 남아 있고, 이 함수는 그것을 되돌리지 않는다(#451).
    """
    transcript_messages = [message for message in chat_request.messages if not is_memory_system_message(message)]
    recent = transcript_messages[-max_history_messages:]
    if len(recent) <= 1:
        return latest_user_plain_text(chat_request.messages)

    def render(msgs: list[Message]) -> str:
        last_assistant = _last_assistant_index(msgs)
        return "\n".join(
            _history_line(
                message,
                is_last_assistant=index == last_assistant,
                user_cap=user_message_max_chars,
                assistant_cap=assistant_message_max_chars,
                last_assistant_cap=last_assistant_message_max_chars,
            )
            for index, message in enumerate(msgs)
        )

    history = render(recent)
    while len(history) > transcript_max_chars and len(recent) > 1:
        recent = recent[1:]
        history = render(recent)
    if len(history) > transcript_max_chars:
        history = truncate_end(history, transcript_max_chars)
    return (
        "다음 최근 대화 맥락을 참고하되, 반드시 현재 사용자 요청을 중심으로 답하세요.\n\n"
        "[Recent conversation - oldest to newest]\n"
        f"{history}\n\n"
        "[Current user request]\n"
        f"{latest_user_plain_text(chat_request.messages)}"
    )


@register_function(config_type=FeBranchConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def fe_branch(config: FeBranchConfig, builder: Builder):
    inner_name = config.inner_function_name
    desc = config.tool_description or (
        f"Run the nested ReAct agent `{inner_name}` on the user's question (plain text).")

    async def run_subagent(chat_request_or_message: ChatRequestOrMessage) -> str:
        chat_request = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
        inner = await builder.get_function(inner_name)
        query = _contextual_user_query(
            chat_request,
            config.max_history_messages,
            config.history_message_max_chars,
            config.history_assistant_message_max_chars,
            config.history_transcript_max_chars,
            config.history_last_assistant_message_max_chars,
        )
        return await _run_with_gate(
            inner=inner,
            query=query,
            chat_request=chat_request,
            inner_name=inner_name,
        )

    yield FunctionInfo.from_fn(run_subagent, description=desc)


class SupervisorBranch(BaseModel):
    name: str = Field(..., min_length=1)
    function_name: FunctionRef = Field(...)
    description: str = Field(..., min_length=1)


class FinusSupervisorAgentConfig(FunctionBaseConfig, name="finus_supervisor_agent"):
    llm_name: LLMRef = Field(...)
    branches: list[SupervisorBranch] = Field(..., min_length=1)
    max_history_messages: int = Field(default=6, ge=1, le=64)
    history_message_max_chars: int = Field(default=220, ge=60, le=8000)
    fallback_branch: str | None = Field(
        default=None,
        description=(
            "라우터 출력에서 허용된 브랜치 이름을 하나로 읽지 못할 때 보낼 브랜치. 비우면 종전처럼 "
            "ValueError로 실패한다. 조회 전용이고 주문 안내·쓰기 도구가 없는 브랜치여야 한다 — "
            "라우팅이 실패한 턴은 의도를 모르는 턴이다."
        ),
    )
    description: str = Field(default="Fin-Us supervisor agent")

    @model_validator(mode="after")
    def _fallback_branch_is_a_branch(self) -> "FinusSupervisorAgentConfig":
        if self.fallback_branch is not None and self.fallback_branch not in {b.name for b in self.branches}:
            raise ValueError(
                f"fallback_branch {self.fallback_branch!r} is not one of the branches "
                f"({', '.join(b.name for b in self.branches)})",
            )
        return self


def _format_history(messages: list[Message], max_msgs: int, per_msg_max: int) -> str:
    user_cap = min(per_msg_max * 2, 1200)
    recent = messages_without_memory(messages)[-max_msgs:]
    # 직전 답은 같은 한도 안에서 끝을 남긴다(#451) — 추가 제안에 대한 "응"을 라우팅하려면
    # 제안이 놓인 답의 끝이 보여야 한다. 한도를 늘리지 않으므로 라우팅 입력 길이는 그대로다.
    last_assistant = _last_assistant_index(recent)
    lines = [
        _history_line(
            m,
            is_last_assistant=index == last_assistant,
            user_cap=user_cap,
            assistant_cap=per_msg_max,
            last_assistant_cap=per_msg_max,
        )
        for index, m in enumerate(recent)
    ]
    return "\n".join(lines)


def _normalize_branch(raw: str, branches: list[SupervisorBranch]) -> str | None:
    """라우터 출력에서 브랜치 이름 하나를 읽는다. 하나로 정해지지 않으면 ``None``.

    정확히 일치하지 않으면 출력 안에 들어 있는 이름을 찾되, **서로 다른 브랜치가 둘 이상**
    들어 있으면 고르지 않는다. 예전에는 설정 순서상 앞선 브랜치를 골라서 "news_agent 또는
    trading_agent" 같은 출력이 목록 첫 항목인 trading_agent로 조용히 기울었다.
    """
    text = raw.strip().strip("`'\" \n\t").lower()
    for branch in branches:
        if branch.name.lower() == text or str(branch.function_name).lower() == text:
            return branch.name
    found = {
        branch.name
        for branch in branches
        if branch.name.lower() in text or str(branch.function_name).lower() in text
    }
    return found.pop() if len(found) == 1 else None


def _supervisor_system_prompt(branches: list[SupervisorBranch]) -> str:
    """supervisor 라우팅 system prompt. 규칙은 한 줄에 하나다 — 줄이 붙으면 뒤 규칙이 앞 문장의
    꼬리로 읽힌다(PR #388 리뷰: 첫 규칙 끝의 개행이 빠져 trading 라우팅 규칙이 붙어 있었다)."""
    branch_list = "\n".join(f"- {b.name}: {b.description}" for b in branches)
    branch_names = ", ".join(b.name for b in branches)
    return (
        "당신은 Fin-Us supervisor입니다. 당신의 유일한 역할은 현재 사용자 턴에 가장 적합한 브랜치 하나를 고르는 것입니다.\n\n"
        "규칙:\n"
        "- 최신 메시지가 짧거나 섹터/종목명을 생략했다면, 최근 대화와 같은 주제를 이어가는 것으로 추론하세요.\n"
        "- 가격, 거래량, 시가총액 순위, 계좌 잔고, 보유 종목 요청과 주문 요청(주문은 내지 않고 텔레그램 명령을 "
        "안내한다)은 trading_agent로 라우팅하세요.\n"
        "- 브랜치는 요청 대상(보유 종목·특정 종목)이 아니라 원하는 정보의 종류로 고르세요. 뉴스·공시·실적을 "
        "원하면 대상이 보유 종목이어도 news_agent로 라우팅하세요(news_agent가 잔고로 보유 종목을 먼저 확인합니다).\n"
        "- 열린 탐색이나 새로운 스크리닝 요청에는 recommend_agent를 사용하세요. 다만 사용자가 직전 턴의 "
        "섹터/종목 비교를 계속하고 있다면, 그 진행 중인 작업에 맞는 브랜치를 우선하세요.\n"
        "- 허용된 목록의 브랜치 이름 하나만 정확히 출력하세요. 설명과 문장부호는 출력하지 마세요.\n\n"
        f"브랜치:\n{branch_list}\n\n"
        f"허용된 이름: {branch_names}"
    )


@register_function(config_type=FinusSupervisorAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_supervisor_agent(config: FinusSupervisorAgentConfig, builder: Builder):
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    branch_functions = {b.name: await builder.get_function(b.function_name) for b in config.branches}

    system_prompt = _supervisor_system_prompt(config.branches)

    async def _choose_branch(chat_request: ChatRequest) -> str:
        latest = latest_user_plain_text(chat_request.messages)
        history = _format_history(chat_request.messages, config.max_history_messages,
                                  config.history_message_max_chars)
        prompt = (
            f"[Recent conversation]\n{history}\n\n"
            "[Latest user message]\n"
            f"{latest}\n\n"
            "'대표만', '그중에서', '좁혀서', '계속', '바로', '진행', '해줘' 같은 후속 요청은 보통 위 대화의 "
            "같은 종목/섹터/지표를 가리킵니다. 새로운 모호한 질문으로 보지 말고, 위아래 의도를 합쳐서 라우팅하세요.\n\n"
            "브랜치?"
        )
        response = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
        selected = _normalize_branch(str(response.content), config.branches)
        if selected is None:
            if config.fallback_branch is None:
                allowed = ", ".join(b.name for b in config.branches)
                raise ValueError(
                    "Supervisor LLM must output exactly one allowed branch name "
                    f"({allowed}); got {response.content!r}",
                )
            # 라우팅 실패를 사용자 오류로 내보내지 않고 정해 둔 조회 전용 브랜치로 넘긴다.
            # 예전에는 이 ValueError가 NAT 422 → backend "응답 생성 실패: {...}"로 원문 그대로
            # 사용자에게 나갔다. 출력은 로그 길이를 묶어 남긴다(라우터 출력은 보통 한 단어다).
            logger.warning(
                "Supervisor output is not exactly one allowed branch; falling back to %s (output=%r)",
                config.fallback_branch,
                str(response.content)[:80],
            )
            selected = config.fallback_branch
        logger.info(
            "Supervisor routed to %s (history=%d)",
            selected,
            len(messages_without_memory(chat_request.messages)),
        )
        return selected

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse:
        chat_request = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
        selected = await _choose_branch(chat_request)
        _trace_route(selected)  # #260: 라우팅 결과는 코드가 고른 값 — 각주의 담당 에이전트
        forward = chat_request.model_copy(update={"messages": messages_without_memory(chat_request.messages)})
        result = await branch_functions[selected].ainvoke(forward)
        response = result if isinstance(result, ChatResponse) else ChatResponse.from_string(str(result), usage=Usage())
        # #294: 위 두 기록이 "어느 답변에 대한 것인지"를 남긴다. 브랜치가 예외로
        # 죽으면 여기에 도달하지 않으므로, 기록 없이 올라간 텍스트에는 각주가 붙지 않는다.
        _trace_branch_answer(chat_response_plain_text(response))
        return response

    yield FunctionInfo.from_fn(_response_fn, description=config.description)


def with_reasoning_trace(response: ChatResponse, trace: ReasoningTrace) -> ChatResponse:
    """*response*에 ``routed_agent``/``tools_used``를 **추가**한다 (#260).

    기존 필드(``choices``/``usage``/``model`` 등)는 건드리지 않는다 — backend의
    ``_nat_message_from_payload``와 scheduler의 ``analysis_from_nat_text``가
    그대로 동작해야 한다. ``ChatResponse.model_config``가 ``extra="allow"``라
    추가 필드는 그대로 직렬화되고, 두 필드를 모르는 소비자는 무시하면 된다.

    두 값의 출처는 코드 기록뿐이다 — supervisor의 분기 선택 결과와 도구 강제
    원장이며, 모델 출력 텍스트에서 파싱하지 않는다 (#129와 같은 원칙).
    :class:`ReasoningTrace` docstring 참고.

    관측된 것이 아무것도 없으면(라우팅도 도구 실행도 없음) 필드를 붙이지 않는다.
    빈 값을 실어 보내는 것보다 없는 편이 소비자 입장에서 명확하다 — backend는
    필드가 없으면 각주를 조용히 생략한다. 반대로 라우팅은 됐는데 도구가 하나도
    실행되지 않은 경우는 관측 결과 그 자체이므로 ``tools_used: []``로 싣는다.

    ``tools_used``는 ``[{"name": ..., "ok": ..., "empty": ...}]`` 형태다. 결과 상태를
    빼고 이름만 실어 보내면 소비자가 실패한 호출과 빈 결과까지 "확인한 자료"로
    표시하게 된다 — :class:`ToolUse` docstring 참고.

    **본문이 브랜치가 만든 답변일 때만 붙인다 (#294)** — 기록이 있다는 것과 지금
    돌려보내는 본문이 그 기록의 결과라는 것은 다르다. 왜 이 판정이 필요한지는
    :meth:`ReasoningTrace.record_branch_answer` 참고.
    """
    if trace.routed_agent is None and not trace.tools_used:
        return response
    body = chat_response_plain_text(response)
    if trace.branch_answer is None or body != trace.branch_answer:
        # 기록은 있는데 본문이 브랜치 답변이 아니다. 각주를 생략하는 것만으로 사용자
        # 피해는 막히지만, #273에서 각주가 "조용히" 사라진 것이 문제였으므로 남긴다.
        # 두 길이를 함께 남기는 것은 "실패라서 다름"과 "vendor가 본문을 변형함"을 로그만으로
        # 구분하기 위해서다 — 후자면 정상 경로의 각주가 전면 소실되는데 증상이 같다.
        # 본문 자체는 싣지 않는다(운영 로그에 답변 전문이 흘러가지 않게).
        logger.warning(
            "Reasoning trace footnote omitted — response body is not the branch answer "
            "(routed_agent=%s tools=%d body_len=%d recorded_len=%s)",
            trace.routed_agent,
            len(trace.tools_used),
            len(body),
            len(trace.branch_answer) if trace.branch_answer is not None else "none",
        )
        return response
    return response.model_copy(
        update={
            "routed_agent": trace.routed_agent,
            "tools_used": [
                {"name": tool.name, "ok": tool.ok, "empty": tool.empty}
                for tool in trace.tools_used
            ],
        }
    )


class FinusReasoningTraceAgentConfig(FunctionBaseConfig, name="finus_reasoning_trace_agent"):
    inner_agent_name: FunctionRef = Field(..., description="Agent/workflow to invoke with a reasoning-trace box installed.")
    description: str = Field(default="Fin-Us agent that attaches the reasoning trace footnote (#260)")


@register_function(config_type=FinusReasoningTraceAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_reasoning_trace_agent(config: FinusReasoningTraceAgentConfig, builder: Builder):
    """추론 각주(#260)의 **유일한** 소유 지점 — 박스를 심고, 결과를 응답에 붙인다 (#273).

    두 라우터 config가 이 함수를 최상위 ``workflow``로 쓰고, 그 아래 무엇이 오든
    (``auto_memory_agent``가 끼든 안 끼든) 각주는 같은 자리에서 붙는다. 부착 지점이
    config에 따라 갈리지 않는 것이 이 함수가 존재하는 이유의 전부다.

    이전에는 ``finus_sqlite_transcript_agent``가 그 역할을 겸했는데, ``router.yml``에서는
    그 위에 vendor ``auto_memory_agent``가 얹힌다. vendor ``_response_fn``의 시그니처가
    ``(input_message: str) -> str``이라 안쪽이 ``ChatResponse``에 붙인 ``routed_agent``/
    ``tools_used``가 래퍼를 통과하면서 사라졌다. backend는 두 필드가 없으면 각주를
    조용히 생략하므로(#260) 메모리 모드에서는 로그도 예외도 없이 기능만 없어졌다.

    vendor 래퍼를 ``ChatResponse``가 통과하도록 감싸는 대안도 있었으나, vendor
    시그니처에 의존하므로 NAT 업그레이드 때 같은 방식으로 다시 깨진다. 부착 지점을
    vendor 바깥으로 올리면 vendor가 무엇을 버리든 무관해진다.

    **vendor가 삼킨 실패에는 붙지 않는다 (#294).** supervisor가 브랜치의 답변 본문을
    박스에 함께 남기고(:func:`_trace_branch_answer`), :func:`with_reasoning_trace`가
    본문이 그 기록과 같을 때만 붙인다. 그 판정이 왜 "브랜치가 성공했는가"가 아닌지는
    :meth:`ReasoningTrace.record_branch_answer` 참고.
    """
    inner_agent = await builder.get_function(config.inner_agent_name)

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse | str:
        # 박스는 여기서 한 번만 심는다. 안쪽(supervisor, _run_with_gate)은 mutate만 한다 —
        # ContextVar.set()은 LangGraph의 copy_context() 경계를 넘어 바깥으로 전파되지
        # 않기 때문이다(DataToolLedger와 동일). 반대 방향(바깥→안쪽)은 전파되므로
        # vendor auto_memory_agent가 중간에 끼어 있어도 안쪽이 같은 박스를 본다.
        trace = ReasoningTrace()
        trace_token = REASONING_TRACE.set(trace)
        # PII 매핑 박스(#231)도 같은 자리에서 같은 이유로 심는다 — 두 라우터 config가
        # 모두 이 함수를 최상위 workflow로 쓰므로, config가 무엇이든(vendor
        # auto_memory_agent가 끼든 안 끼든) 마스킹의 시작과 끝이 한 쌍으로 맞물린다.
        # 안쪽 도구는 이 dict를 mutate만 한다(finus_agents/pii_guard.py 참고).
        mapping_token = PII_MAPPING.set(install_mapping_box())
        # 사용자 발화 금액 상자(#468)도 같은 자리에서 심는다. 조회 인자 복원만 이 상자를 읽는다.
        amounts = _request_input_amounts(chat_request_or_message)
        amounts_token = REQUEST_INPUT_AMOUNTS.set(amounts)
        # 그 금액마다 사용자가 말한 방향("이하"=상한)도 함께 심는다 — 순위 가격 범위 자리 맞춤용.
        bounds_token = REQUEST_INPUT_AMOUNT_BOUNDS.set(_request_input_amount_bounds(chat_request_or_message, amounts))
        try:
            try:
                result = await inner_agent.ainvoke(chat_request_or_message)
            finally:
                REASONING_TRACE.reset(trace_token)

            # 문자열 입력(`nat run --input`)은 평문으로 돌려준다 — 각주를 실을 곳이 없다.
            # **종전 동작과 다르다.** 옛 transcript agent는 isinstance 검사가 먼저라
            # 문자열 입력에도 각주가 실린 ChatResponse를 돌려줬고, 그 아래 `is_string`
            # 분기는 안쪽(supervisor)이 항상 ChatResponse를 반환하므로 도달하지 않는
            # 죽은 코드였다 — "실을 곳이 없다"는 주석이 실행되지 않는 분기에 붙어 있었다.
            # 여기서는 검사 순서를 뒤집어 그 의도를 실제로 실행한다. HTTP 경로(backend·
            # scheduler)는 messages를 보내므로 영향받지 않는다.
            if chat_request_or_message.is_string:
                return unmask_response(chat_response_plain_text(result))
            if isinstance(result, ChatResponse):
                traced = with_reasoning_trace(result, trace)
            else:
                # vendor auto_memory_agent는 str을 돌려준다 — 여기서 ChatResponse로 만들며
                # 각주를 붙이므로, 안쪽에서 무엇이 버려졌는지와 무관하게 필드가 실린다.
                traced = with_reasoning_trace(ChatResponse.from_string(str(result), usage=Usage()), trace)

            # 역치환은 각주를 붙인 **뒤**에 한다 (#231). with_reasoning_trace는 본문이
            # supervisor가 기록해 둔 브랜치 답변과 같을 때만 각주를 붙이는데(#294),
            # 그 기록은 마스킹된 상태로 남는다. 먼저 역치환하면 본문만 원값으로 바뀌어
            # 비교가 어긋나고, 정상 응답에서 각주가 통째로 사라진다.
            body = chat_response_plain_text(traced)
            restored = unmask_response(body)
            return traced if restored == body else chat_response_with_text(traced, restored)
        finally:
            REQUEST_INPUT_AMOUNT_BOUNDS.reset(bounds_token)
            REQUEST_INPUT_AMOUNTS.reset(amounts_token)
            PII_MAPPING.reset(mapping_token)

    yield FunctionInfo.from_fn(_response_fn, description=config.description)


def _request_input_amounts(chat_request_or_message: ChatRequestOrMessage) -> dict[str, str]:
    """이 HTTP 요청의 :data:`INPUT_AMOUNTS_HEADER`를 최신 사용자 발화에 묶어 읽는다 (#468).

    헤더가 없거나(요청 밖 실행, 구버전 backend) 읽을 수 없으면 빈 상자다 — 그때 조회 인자의
    발화 금액 자리표시자는 종전처럼 거부된다.
    """
    # 어떤 예외든 빈 상자로 떨어진다(fail-closed). 헤더는 다른 프로세스가 보낸 값이라 깊은 중첩 JSON의
    # RecursionError 같은 ValueError 밖의 예외도 올 수 있고, 여기서 터지면 요청 전체가 죽는다.
    try:
        headers = getattr(Context.get().metadata, "headers", None)
        raw = headers.get(INPUT_AMOUNTS_HEADER) if headers else None
    except Exception:  # noqa: BLE001 — 요청 밖(nat run 등)에서는 메타데이터가 없을 수 있다.
        return {}
    if not raw:
        return {}
    try:
        return input_amounts_from_header(raw, _latest_user_text(chat_request_or_message))
    except Exception as exc:  # noqa: BLE001
        # 예외 메시지·값은 남기지 않는다(헤더에 사용자 금액 원문이 있다). 종류만 남긴다.
        logger.warning("%s 헤더를 읽지 못해 발화 금액 상자를 비웠습니다 (%s)", INPUT_AMOUNTS_HEADER, type(exc).__name__)
        return {}


def _latest_user_text(chat_request_or_message: ChatRequestOrMessage) -> str:
    if chat_request_or_message.is_string:
        return chat_request_or_message.input_message or ""
    return latest_user_plain_text(chat_request_or_message.messages or [])


def _request_input_amount_bounds(chat_request_or_message: ChatRequestOrMessage, amounts: dict[str, str]) -> dict[str, str]:
    """발화 금액 상자의 자리표시자마다 사용자가 말한 범위 방향을 읽는다. 실패하면 빈 상자(방향 모름)다."""
    if not amounts:
        return {}
    try:
        return input_amount_bounds(_latest_user_text(chat_request_or_message), amounts)
    except Exception as exc:  # noqa: BLE001 — 방향을 모르면 LLM이 고른 자리를 그대로 쓴다(종전 동작).
        logger.warning("발화 금액 방향을 읽지 못했습니다 (%s)", type(exc).__name__)
        return {}


def nat_chat_extra_headers(conversation_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        CONVERSATION_ID_HTTP_HEADER: conversation_id.strip(),
    }


class FinusSqliteTranscriptAgentConfig(FunctionBaseConfig, name="finus_sqlite_transcript_agent"):
    inner_agent_name: FunctionRef = Field(..., description="Agent/workflow to invoke after adding transcript history.")
    db_path: str = Field(
        default=".state/conversations.sqlite3",
        description="SQLite path for persisted chat transcripts. Relative paths are resolved from agents/.",
    )
    max_history_messages: int = Field(default=30, ge=0)
    description: str = Field(default="Fin-Us agent with persisted SQLite conversation transcript")

    @field_validator("max_history_messages", mode="before")
    @classmethod
    def _coerce_max_history_messages(cls, value: Any) -> int:
        if value is None:
            return 30
        if isinstance(value, bool):
            raise ValueError("max_history_messages must be an integer, not a boolean")
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return int(value)


def _resolve_db_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_id_id
            ON chat_messages(conversation_id, id)
            """
        )


def _stored_role(message: Message) -> str | None:
    if is_memory_system_message(message):
        return None
    if message.role in (UserMessageContentRoleType.USER, UserMessageContentRoleType.ASSISTANT):
        return str(message.role.value)
    return None


def _has_client_supplied_transcript(messages: list[Message]) -> bool:
    turns = [
        message
        for message in messages
        if message.role in (UserMessageContentRoleType.USER, UserMessageContentRoleType.ASSISTANT)
    ]
    return len(turns) > 1


def _latest_user_message(messages: list[Message]) -> Message | None:
    for message in reversed(messages):
        if message.role == UserMessageContentRoleType.USER:
            return message
    return None


def _load_history(db_path: Path, conversation_id: str, max_messages: int) -> list[Message]:
    if max_messages <= 0 or not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM (
                SELECT id, role, content
                FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (conversation_id, max_messages),
        ).fetchall()
    return [
        Message(role=UserMessageContentRoleType(role), content=content)
        for role, content in rows
        if role in {UserMessageContentRoleType.USER.value, UserMessageContentRoleType.ASSISTANT.value}
    ]


def _append_messages(db_path: Path, conversation_id: str, messages: list[tuple[str, str]]) -> None:
    if not messages:
        return
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO chat_messages (conversation_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(conversation_id, role, content, now) for role, content in messages],
        )


def _resolve_conversation_id() -> str:
    """Use request header when present; otherwise a single default thread (non-multi-tenant)."""
    cid = Context.get().conversation_id
    if cid and str(cid).strip():
        return str(cid).strip()
    return _DEFAULT_CONVERSATION_ID


def _chat_request_with_messages(chat_request: ChatRequest, messages: list[Message]) -> ChatRequest:
    dumped: dict[str, Any] = chat_request.model_dump()
    dumped["messages"] = [message.model_dump() for message in messages]
    return ChatRequest.model_validate(dumped)


@register_function(config_type=FinusSqliteTranscriptAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_sqlite_transcript_agent(config: FinusSqliteTranscriptAgentConfig, builder: Builder):
    inner_agent = await builder.get_function(config.inner_agent_name)
    db_path = _resolve_db_path(config.db_path)
    _init_db(db_path)

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse | str:
        chat_request = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
        conversation_id = _resolve_conversation_id()

        if _has_client_supplied_transcript(chat_request.messages):
            history: list[Message] = []
        else:
            history = _load_history(db_path, conversation_id, config.max_history_messages)

        forward_messages = history + chat_request.messages
        forward = _chat_request_with_messages(chat_request, forward_messages)

        # 추론 기록 박스(#260)는 이 에이전트가 심지 않는다 — 최상위
        # finus_reasoning_trace_agent가 심고 붙인다(#273). 여기서 겸하면
        # router.yml처럼 위에 vendor auto_memory_agent가 얹히는 config에서
        # 부착 결과가 그 래퍼에 막혀 조용히 사라진다.
        result = await inner_agent.ainvoke(forward)
        assistant = chat_response_plain_text(result)

        to_store: list[tuple[str, str]] = []
        latest_user = _latest_user_message(chat_request.messages)
        if latest_user is not None:
            role = _stored_role(latest_user)
            if role is not None:
                to_store.append((role, message_content_text(latest_user)))
        if assistant:
            to_store.append((UserMessageContentRoleType.ASSISTANT.value, assistant))

        _append_messages(db_path, conversation_id, to_store)

        if isinstance(result, ChatResponse):
            return result
        if chat_request_or_message.is_string:
            return str(result)
        return ChatResponse.from_string(str(result), usage=Usage())

    yield FunctionInfo.from_fn(_response_fn, description=config.description)


class FeStubConfig(FunctionBaseConfig, name="fe_stub"):
    pass


@register_function(config_type=FeStubConfig)
async def fe_stub(_config: FeStubConfig, _builder: Builder):
    async def placeholder_note(query: str) -> str:
        return (
            "This branch has no external data tools yet. Answer from general knowledge only, "
            f"or say you cannot fetch live data. Context: {query[:400]!r}"
        )

    yield FunctionInfo.from_fn(
        placeholder_note,
        description="Explain if live data is needed and answer briefly from general knowledge when possible.",
    )


class FinusMemoryDisabledConfig(FunctionBaseConfig, name="finus_memory_disabled"):
    description: str = Field(
        default="Memory is disabled in this Fin-Us NAT config.",
        description="Tool description shown to agents when Mem0 is not configured.",
    )


@register_function(config_type=FinusMemoryDisabledConfig)
async def finus_memory_disabled(config: FinusMemoryDisabledConfig, _builder: Builder):
    async def memory_disabled(note: str = "") -> str:
        return (
            "Memory is disabled for this run, so no durable user memory was read or written. "
            "Continue using only the live conversation context."
        )

    yield FunctionInfo.from_fn(memory_disabled, description=config.description)
