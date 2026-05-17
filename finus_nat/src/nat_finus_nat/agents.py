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

logger = logging.getLogger(__name__)

MEMORY_PROMPT_PREFIX = "Relevant context from memory:"
CONVERSATION_ID_HTTP_HEADER = "conversation-id"
_DEFAULT_CONVERSATION_ID = os.environ.get("FINUS_DEFAULT_CONVERSATION_ID", "fin-us-default")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_KNOWN_STOCK_NAMES = ("삼성전자", "NAVER", "네이버")


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


def chat_response_plain_text(result: ChatResponse | str) -> str:
    if isinstance(result, str):
        return result
    if result.choices and result.choices[0].message.content is not None:
        return result.choices[0].message.content
    return ""


def _is_holdings_news_request(chat_request: ChatRequest) -> bool:
    latest = latest_user_plain_text(chat_request.messages)
    wants_news = "뉴스" in latest or "뉴" in latest
    refers_holdings = any(word in latest for word in ("보유", "포트폴리오", "종목", "보요유종목"))
    return wants_news and refers_holdings


def _stock_names_from_context(chat_request: ChatRequest) -> list[str]:
    text = "\n".join(message_content_text(message) for message in chat_request.messages)
    names: list[str] = []
    for name in _KNOWN_STOCK_NAMES:
        normalized = "NAVER" if name == "네이버" else name
        if name in text and normalized not in names:
            names.append(normalized)
    for match in re.finditer(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s*([가-힣A-Za-z][가-힣A-Za-z0-9&.\- ]{1,20})\s+\d+\s*주", text):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


async def _holdings_news_answer(builder: Builder, chat_request: ChatRequest) -> str | None:
    if not _is_holdings_news_request(chat_request):
        return None
    stock_names = _stock_names_from_context(chat_request)
    if not stock_names:
        return None
    news_tool = await builder.get_function("mcp-news-get-market-news")
    sections: list[str] = []
    for stock_name in stock_names:
        result = await news_tool.ainvoke(stock_name)
        sections.append(f"## {stock_name}\n{chat_response_plain_text(result)}")
    return "보유종목 관련 최신 뉴스입니다.\n\n" + "\n\n".join(sections)


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
        description="히스토리에서 assistant 한 메시지당 최대 문자 수. 긴 잔고 답변을 짧게 잘라 ReAct 입력 부담을 줄임.",
    )
    history_transcript_max_chars: int = Field(
        default=1200,
        ge=200,
        le=500_000,
        description="[Recent conversation] 블록 전체 문자 상한. 초과 시 가장 오래된 메시지부터 제거.",
    )


def _history_line_budget(
    role: UserMessageContentRoleType,
    *,
    user_cap: int,
    assistant_cap: int,
) -> int:
    if role == UserMessageContentRoleType.ASSISTANT:
        return assistant_cap
    return user_cap


def _contextual_user_query(
    chat_request: ChatRequest,
    max_history_messages: int,
    user_message_max_chars: int,
    assistant_message_max_chars: int,
    transcript_max_chars: int,
) -> str:
    transcript_messages = [message for message in chat_request.messages if not is_memory_system_message(message)]
    recent = transcript_messages[-max_history_messages:]
    if len(recent) <= 1:
        return latest_user_plain_text(chat_request.messages)

    def render(msgs: list[Message]) -> str:
        return "\n".join(
            f"{role_short_label(message.role)}: "
            f"{truncate_end(message_content_text(message).strip(), _history_line_budget(message.role, user_cap=user_message_max_chars, assistant_cap=assistant_message_max_chars))}"
            for message in msgs
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
        if inner_name == "news_agent":
            direct = await _holdings_news_answer(builder, chat_request)
            if direct is not None:
                return direct
        inner = await builder.get_function(inner_name)
        payload = ChatRequestOrMessage(
            input_message=_contextual_user_query(
                chat_request,
                config.max_history_messages,
                config.history_message_max_chars,
                config.history_assistant_message_max_chars,
                config.history_transcript_max_chars,
            )
        )
        result = await inner.ainvoke(payload)
        return chat_response_plain_text(result)

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
    description: str = Field(default="Fin-Us supervisor agent")


def _format_history(messages: list[Message], max_msgs: int, per_msg_max: int) -> str:
    user_cap = min(per_msg_max * 2, 1200)
    recent = messages_without_memory(messages)[-max_msgs:]
    lines = [
        f"{role_short_label(m.role)}: "
        f"{truncate_end(message_content_text(m).strip(), user_cap if m.role == UserMessageContentRoleType.USER else per_msg_max)}"
        for m in recent
    ]
    return "\n".join(lines)


def _normalize_branch(raw: str, branches: list[SupervisorBranch]) -> str | None:
    text = raw.strip().strip("`'\" \n\t").lower()
    for branch in branches:
        if branch.name.lower() == text or str(branch.function_name).lower() == text:
            return branch.name
    for branch in branches:
        if branch.name.lower() in text or str(branch.function_name).lower() in text:
            return branch.name
    return None


@register_function(config_type=FinusSupervisorAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_supervisor_agent(config: FinusSupervisorAgentConfig, builder: Builder):
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    branch_functions = {b.name: await builder.get_function(b.function_name) for b in config.branches}

    branch_list = "\n".join(f"- {b.name}: {b.description}" for b in config.branches)
    branch_names = ", ".join(b.name for b in config.branches)

    system_prompt = (
        "당신은 Fin-Us supervisor입니다. 당신의 유일한 역할은 현재 사용자 턴에 가장 적합한 브랜치 하나를 고르는 것입니다.\n\n"
        "규칙:\n"
        "- 최신 메시지가 짧거나 섹터/종목명을 생략했다면, 최근 대화와 같은 주제를 이어가는 것으로 추론하세요 "
        "- 가격, 거래량, 시가총액 순위, 계좌 잔고, 주문, 보유 종목 요청은 trading_agent로 라우팅하세요.\n"
        "- 열린 탐색이나 새로운 스크리닝 요청에는 recommend_agent를 사용하세요. 다만 사용자가 직전 턴의 "
        "섹터/종목 비교를 계속하고 있다면, 그 진행 중인 작업에 맞는 브랜치를 우선하세요.\n"
        "- 허용된 목록의 브랜치 이름 하나만 정확히 출력하세요. 설명과 문장부호는 출력하지 마세요.\n\n"
        f"브랜치:\n{branch_list}\n\n"
        f"허용된 이름: {branch_names}"
    )

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
            allowed = ", ".join(b.name for b in config.branches)
            raise ValueError(
                "Supervisor LLM must output exactly one allowed branch name "
                f"({allowed}); got {response.content!r}",
            )
        logger.info(
            "Supervisor routed to %s (history=%d)",
            selected,
            len(messages_without_memory(chat_request.messages)),
        )
        return selected

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse:
        chat_request = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
        selected = await _choose_branch(chat_request)
        forward = chat_request.model_copy(update={"messages": messages_without_memory(chat_request.messages)})
        result = await branch_functions[selected].ainvoke(forward)
        if isinstance(result, ChatResponse):
            return result
        return ChatResponse.from_string(str(result), usage=Usage())

    yield FunctionInfo.from_fn(_response_fn, description=config.description)


def nat_chat_extra_headers(conversation_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        CONVERSATION_ID_HTTP_HEADER: conversation_id.strip(),
    }


class FinusSqliteTranscriptAgentConfig(FunctionBaseConfig, name="finus_sqlite_transcript_agent"):
    inner_agent_name: FunctionRef = Field(..., description="Agent/workflow to invoke after adding transcript history.")
    db_path: str = Field(
        default=".state/conversations.sqlite3",
        description="SQLite path for persisted chat transcripts. Relative paths are resolved from finus_nat/.",
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
