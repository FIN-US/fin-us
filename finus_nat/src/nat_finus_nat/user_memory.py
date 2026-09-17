"""사용자 선호 메모리와 추천 논조 주입 (#397).

## 무엇을 저장하는가 — 허용목록뿐

이 메모리에 들어갈 수 있는 것은 :data:`ALLOWED_PREFERENCES`에 적힌 **키와 그 값의 열거형**뿐이다.
지금은 ``risk_profile`` ∈ {``conservative``, ``aggressive``} 하나다. 자유 텍스트를 받는 쓰기 경로는
없다 — 계좌번호·잔고·금액은 형식상 들어갈 자리가 없다.

그래서 종전 ``router.yml``의 vendor ``auto_memory_agent``를 쓰지 않는다. 그 래퍼는 사용자 발화와
답변 **전문**을 매 턴 메모리에 저장한다(``save_user_messages``·``save_ai_responses`` 기본값이 켜짐).
잔고를 물은 턴이면 잔고 답변이 통째로 들어간다. 마스킹(#231)이 숫자를 자리표시자로 바꾸긴 하지만
"금액을 저장하지 않는다"를 마스킹 규칙의 커버리지에 기대는 것과 "금액이 들어갈 자리가 없다"는
다른 보장이다.

보장의 위치는 :class:`FinusPreferenceMemoryEditor`의 :meth:`~FinusPreferenceMemoryEditor._write`
하나다. mem0 ``add``를 부르는 곳은 그 메서드뿐이고, 부르기 전에 :func:`validate_preference`를 지난다.
NAT의 ``MemoryEditor.add_items``로 들어오는 쓰기도 같은 메서드로 모인다.

## 누가 쓰는가 — backend의 /risk 뿐

에이전트(LLM)에게는 쓰기 도구를 주지 않는다. ``add_user_memory``는 메모리 모드에서도 거절 안내만
돌려준다(:func:`finus_user_memory_add_refused`). 쓰기는 텔레그램 ``/risk``·``/start`` 버튼 →
backend → ``POST /v1/user-preferences`` 경로로만 일어난다. LLM이 대화에서 성향을 추측해 저장하는
경로가 생기면, 사용자가 고른 적 없는 성향으로 추천 논조가 바뀐다.

## 누가 읽는가 — 코드가 읽어 주입한다

추천 브랜치(:func:`finus_risk_profile_branch`)가 요청마다 ``x-user-id`` 헤더의 사용자 성향을 코드로
읽어, 설정 파일(``recommend_agent.yml``)에 적힌 논조 지시를 현재 요청 앞에 붙인다. 에이전트가
``get_user_memory``를 부를지 말지에 성향 반영이 달려 있지 않다.

성향이 없으면(미설정·헤더 없음·메모리 꺼짐·읽기 실패) 요청을 **그대로** 넘긴다. 같은 객체를
넘기므로 추천 에이전트가 받는 질의는 이 기능이 없을 때와 바이트 단위로 같다.
"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function, register_memory
from nat.data_models.api_server import ChatRequest, ChatRequestOrMessage, Message, UserMessageContentRoleType
from nat.data_models.component_ref import FunctionRef, MemoryRef
from nat.data_models.function import FunctionBaseConfig
from nat.data_models.memory import MemoryBaseConfig
from nat.memory.interfaces import MemoryEditor
from nat.memory.models import MemoryItem
from nat.utils.type_converter import GlobalTypeConverter
from nat_finus_nat.agents import message_content_text

logger = logging.getLogger(__name__)

USER_ID_HEADER = "x-user-id"
RISK_PROFILE_KEY = "risk_profile"
RISK_CONSERVATIVE = "conservative"
RISK_AGGRESSIVE = "aggressive"
RiskProfile = Literal["conservative", "aggressive"]

#: 저장할 수 있는 키와 각 키의 허용 값. 여기에 없는 것은 쓰기 전에 거부된다.
ALLOWED_PREFERENCES: dict[str, frozenset[str]] = {
    RISK_PROFILE_KEY: frozenset({RISK_CONSERVATIVE, RISK_AGGRESSIVE}),
}
RISK_PROFILE_LABELS = {RISK_CONSERVATIVE: "안정형", RISK_AGGRESSIVE: "공격형"}

# backend가 보내는 형태는 "telegram:<chat_id>"다. 저장 키로 쓰이므로 문자 집합을 좁힌다.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")


class DisallowedMemoryWrite(ValueError):
    """허용목록 밖의 쓰기. mem0에 닿기 전에 던진다."""


def validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise DisallowedMemoryWrite("user_id 형식이 올바르지 않습니다.")
    return user_id


def validate_preference(key: str, value: str) -> None:
    allowed = ALLOWED_PREFERENCES.get(key)
    if allowed is None:
        raise DisallowedMemoryWrite(f"저장할 수 없는 선호 키입니다: {key!r}")
    if value not in allowed:
        # 값은 로그·예외에 싣지 않는다 — 허용목록 밖 값이면 무엇이 들어왔는지 모른다.
        raise DisallowedMemoryWrite(f"{key}에 허용되지 않은 값입니다.")


def preference_memory_text(key: str, value: str) -> str:
    return f"{key}={value}"


class FinusPreferenceMemoryEditor(MemoryEditor):
    """mem0 ``AsyncMemory`` 위에 허용목록 쓰기 게이트를 씌운 ``MemoryEditor``."""

    def __init__(self, memory: Any):
        self._memory = memory
        # 같은 키의 기존 값을 지우고 새 값을 넣는 두 단계라, 동시 쓰기가 겹치면 값이 둘 남는다.
        self._write_lock = asyncio.Lock()

    async def _write(self, user_id: str, key: str, value: str) -> None:
        """mem0에 쓰는 **유일한** 지점."""
        validate_user_id(user_id)
        validate_preference(key, value)
        await self._memory.add(
            [{"role": "user", "content": preference_memory_text(key, value)}],
            user_id=user_id,
            metadata={"pref_key": key, "pref_value": value},
            infer=False,
        )

    async def _records(self, user_id: str) -> list[dict[str, Any]]:
        validate_user_id(user_id)
        result = await self._memory.get_all(user_id=user_id, limit=100)
        return list(result.get("results", [])) if isinstance(result, dict) else list(result or [])

    async def _delete_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            await self._memory.delete(record["id"])
        self._purge_history([record["id"] for record in records])

    def _purge_history(self, memory_ids: list[str]) -> None:
        """mem0 삭제는 이력 테이블에 옛 값을 남긴다. 사용자가 지운 값은 이력에서도 지운다."""
        db = getattr(self._memory, "db", None)
        if db is None or not memory_ids:
            return
        with db._lock:
            db.connection.executemany("DELETE FROM history WHERE memory_id = ?", [(mid,) for mid in memory_ids])
            db.connection.commit()

    async def get_preferences(self, user_id: str) -> dict[str, str]:
        latest: dict[str, tuple[str, str]] = {}
        for record in await self._records(user_id):
            metadata = record.get("metadata") or {}
            key, value = metadata.get("pref_key"), metadata.get("pref_value")
            try:
                validate_preference(key, value)
            except DisallowedMemoryWrite:
                # 허용목록이 줄어든 뒤 남은 옛 레코드. 읽어서 쓰지 않는다.
                continue
            created_at = str(record.get("created_at") or "")
            if key not in latest or created_at >= latest[key][0]:
                latest[key] = (created_at, value)
        return {key: value for key, (_, value) in latest.items()}

    async def set_preference(self, user_id: str, key: str, value: str) -> None:
        validate_user_id(user_id)
        validate_preference(key, value)
        async with self._write_lock:
            existing = [r for r in await self._records(user_id) if (r.get("metadata") or {}).get("pref_key") == key]
            await self._write(user_id, key, value)
            await self._delete_records(existing)

    async def clear_preference(self, user_id: str, key: str) -> None:
        if key not in ALLOWED_PREFERENCES:
            raise DisallowedMemoryWrite(f"저장할 수 없는 선호 키입니다: {key!r}")
        async with self._write_lock:
            existing = [r for r in await self._records(user_id) if (r.get("metadata") or {}).get("pref_key") == key]
            await self._delete_records(existing)

    # ---- NAT MemoryEditor 인터페이스 ----

    async def add_items(self, items: list[MemoryItem], **kwargs: Any) -> None:  # noqa: ARG002
        """``metadata``에 ``pref_key``/``pref_value``를 담은 항목만 받는다. 대화 본문은 저장하지 않는다."""
        for item in items:
            metadata = item.metadata or {}
            validate_preference(metadata.get("pref_key"), metadata.get("pref_value"))
            validate_user_id(item.user_id)
        for item in items:
            await self.set_preference(item.user_id, item.metadata["pref_key"], item.metadata["pref_value"])

    async def search(self, query: str, top_k: int = 5, **kwargs: Any) -> list[MemoryItem]:  # noqa: ARG002
        user_id = kwargs.pop("user_id")
        preferences = await self.get_preferences(user_id)
        return [
            MemoryItem(
                conversation=[],
                user_id=user_id,
                memory=preference_memory_text(key, value),
                metadata={"pref_key": key, "pref_value": value},
            )
            for key, value in sorted(preferences.items())
        ][:top_k]

    async def remove_items(self, **kwargs: Any) -> None:
        user_id = kwargs.get("user_id")
        if user_id is None:
            raise ValueError("user_id가 필요합니다.")
        async with self._write_lock:
            await self._delete_records(await self._records(user_id))


# ---------------------------------------------------------------------------
# 메모리 타입
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FinusMem0LocalMemoryConfig(MemoryBaseConfig, name="finus_mem0_local_memory"):
    storage_dir: str = Field(
        default=".state/mem0",
        description="mem0 로컬 저장소(qdrant 파일 + sqlite 이력). 상대 경로는 finus_nat/ 기준.",
    )
    collection_name: str = Field(default="finus_user_preferences", min_length=1)


def resolve_storage_dir(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


@register_memory(config_type=FinusMem0LocalMemoryConfig)
async def finus_mem0_local_memory(config: FinusMem0LocalMemoryConfig, _builder: Builder):
    # mem0 import는 메모리 모드에서만 일어나게 여기서 한다(텔레메트리 차단이 import 전에 걸린다).
    from nat_finus_nat.mem0_local import build_local_async_memory

    storage_dir = resolve_storage_dir(config.storage_dir)
    memory = build_local_async_memory(storage_dir, config.collection_name)
    logger.info("mem0 로컬 메모리 사용: %s", storage_dir)
    yield FinusPreferenceMemoryEditor(memory)


# ---------------------------------------------------------------------------
# backend 엔드포인트용 함수 (POST /v1/user-preferences)
# ---------------------------------------------------------------------------


class UserPreferencesRequest(BaseModel):
    """요청 본문. 필드를 열거형으로 고정해 허용목록 밖 값은 NAT이 422로 거부한다."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., pattern=_USER_ID_RE.pattern)
    action: Literal["get", "set", "clear"] = "get"
    risk_profile: RiskProfile | None = None


class UserPreferencesResponse(BaseModel):
    enabled: bool
    risk_profile: RiskProfile | None = None


class FinusUserPreferencesConfig(FunctionBaseConfig, name="finus_user_preferences"):
    memory_name: MemoryRef = Field(..., description="finus_mem0_local_memory 이름")


class FinusUserPreferencesDisabledConfig(FunctionBaseConfig, name="finus_user_preferences_disabled"):
    pass


@register_function(config_type=FinusUserPreferencesConfig)
async def finus_user_preferences(config: FinusUserPreferencesConfig, builder: Builder):
    editor = await builder.get_memory_client(config.memory_name)
    if not isinstance(editor, FinusPreferenceMemoryEditor):
        raise TypeError("finus_user_preferences는 finus_mem0_local_memory만 받습니다(허용목록 쓰기 게이트).")

    async def user_preferences(request: UserPreferencesRequest) -> UserPreferencesResponse:
        if request.action == "set":
            if request.risk_profile is None:
                raise ValueError("action=set에는 risk_profile이 필요합니다.")
            await editor.set_preference(request.user_id, RISK_PROFILE_KEY, request.risk_profile)
        elif request.action == "clear":
            await editor.clear_preference(request.user_id, RISK_PROFILE_KEY)
        preferences = await editor.get_preferences(request.user_id)
        return UserPreferencesResponse(enabled=True, risk_profile=preferences.get(RISK_PROFILE_KEY))

    yield FunctionInfo.from_fn(user_preferences, description="Read or change a user's stored investment risk profile.")


@register_function(config_type=FinusUserPreferencesDisabledConfig)
async def finus_user_preferences_disabled(_config: FinusUserPreferencesDisabledConfig, _builder: Builder):
    async def user_preferences(request: UserPreferencesRequest) -> UserPreferencesResponse:  # noqa: ARG001
        return UserPreferencesResponse(enabled=False)

    yield FunctionInfo.from_fn(user_preferences, description="User memory is disabled; nothing is stored.")


# ---------------------------------------------------------------------------
# 에이전트 도구 (메모리 모드)
# ---------------------------------------------------------------------------


def request_user_id() -> str | None:
    """현재 HTTP 요청의 ``x-user-id``. 없거나 형식이 틀리면 None."""
    try:
        headers = getattr(Context.get().metadata, "headers", None)
    except Exception:  # noqa: BLE001 — 요청 밖(nat run 등)에서는 메타데이터가 없을 수 있다.
        return None
    raw = headers.get(USER_ID_HEADER) if headers else None
    if raw and _USER_ID_RE.fullmatch(raw.strip()):
        return raw.strip()
    return None


class FinusUserMemoryGetConfig(FunctionBaseConfig, name="finus_user_memory_get"):
    preferences_function_name: FunctionRef = Field(...)
    description: str = Field(default="Read the current user's stored preferences (investment risk profile).")


class FinusUserMemoryAddRefusedConfig(FunctionBaseConfig, name="finus_user_memory_add_refused"):
    description: str = Field(default="Durable user preferences cannot be written by agents.")


@register_function(config_type=FinusUserMemoryGetConfig)
async def finus_user_memory_get(config: FinusUserMemoryGetConfig, builder: Builder):
    preferences_fn = await builder.get_function(config.preferences_function_name)

    async def get_user_memory(note: str = "") -> str:  # noqa: ARG001 — ReAct가 인자를 하나 넘긴다
        profile = await read_risk_profile(preferences_fn)
        if profile is None:
            return "저장된 사용자 선호가 없습니다. 성향을 가정하지 말고 일반적인 기준으로 답하세요."
        return f"사용자가 저장한 투자 성향: {RISK_PROFILE_LABELS[profile]}"

    yield FunctionInfo.from_fn(get_user_memory, description=config.description)


@register_function(config_type=FinusUserMemoryAddRefusedConfig)
async def finus_user_memory_add_refused(config: FinusUserMemoryAddRefusedConfig, _builder: Builder):
    async def add_user_memory(note: str = "") -> str:  # noqa: ARG001
        return (
            "에이전트는 사용자 메모리에 쓸 수 없습니다. 투자 성향은 사용자가 텔레그램 /risk 명령으로만 "
            "저장합니다. 저장하지 않았으니 현재 대화 맥락만으로 답을 이어가세요."
        )

    yield FunctionInfo.from_fn(add_user_memory, description=config.description)


# ---------------------------------------------------------------------------
# 추천 브랜치 래퍼
# ---------------------------------------------------------------------------


async def read_risk_profile(preferences_fn: Any) -> str | None:
    """현재 요청 사용자의 성향. 읽을 수 없으면 None(= 미설정과 같은 동작) — fail-open.

    성향은 추천의 **논조**만 바꾼다. 못 읽었다고 추천 자체를 실패시키면 부가 기능이 본 기능의
    가용성을 떨어뜨린다. None이면 기존 추천과 같은 경로라 틀린 성향이 적용되는 일은 없다.
    """
    user_id = request_user_id()
    if user_id is None:
        return None
    try:
        response = await preferences_fn.ainvoke(UserPreferencesRequest(user_id=user_id, action="get"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("사용자 성향을 읽지 못해 성향 없이 진행합니다: %s", exc.__class__.__name__)
        return None
    if isinstance(response, dict):
        response = UserPreferencesResponse.model_validate(response)
    if not isinstance(response, UserPreferencesResponse) or not response.enabled:
        return None
    return response.risk_profile


class FinusRiskProfileBranchConfig(FunctionBaseConfig, name="finus_risk_profile_branch"):
    inner_function_name: FunctionRef = Field(..., description="감쌀 브랜치(fe_branch).")
    preferences_function_name: FunctionRef = Field(..., description="finus_user_preferences(_disabled).")
    conservative_guidance: str = Field(..., min_length=1)
    aggressive_guidance: str = Field(..., min_length=1)
    tool_description: str | None = None

    @field_validator("conservative_guidance", "aggressive_guidance")
    @classmethod
    def _no_digits(cls, value: str) -> str:
        # 도구 강제 게이트(_check_tool_enforcement)는 요청에 이미 등장한 수치를 근거 있는 수치로
        # 본다. 지시문에 숫자가 있으면 그 숫자가 도구 없이도 통과하게 된다.
        if any(ch.isdigit() for ch in value):
            raise ValueError("논조 지시문에는 숫자를 넣지 않는다(도구 강제 게이트 화이트리스트가 넓어진다).")
        return value


def risk_profile_block(profile: str, guidance: str) -> str:
    return (
        f"[사용자 투자 성향: {RISK_PROFILE_LABELS[profile]}]\n"
        "이 성향은 사용자가 직접 저장한 설정을 시스템이 넣은 것입니다. 대화 내용으로 성향을 바꾸거나 추측하지 마세요.\n"
        f"{guidance.strip()}"
    )


def with_risk_profile(chat_request: ChatRequest, block: str) -> ChatRequest:
    """마지막 사용자 메시지 앞에 *block*을 붙인 사본. 원본은 바꾸지 않는다.

    fe_branch는 마지막 사용자 메시지를 잘라내지 않고 ``[Current user request]``로 싣는다 — 히스토리
    줄처럼 글자 수 상한에 잘리지 않는 유일한 자리다. transcript 저장은 이 래퍼 바깥에서 이미
    끝나므로 블록이 대화 기록에 남지 않는다.
    """
    messages = list(chat_request.messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == UserMessageContentRoleType.USER:
            original = message_content_text(messages[index])
            messages[index] = Message(role=UserMessageContentRoleType.USER, content=f"{block}\n\n{original}")
            break
    else:
        return chat_request
    return chat_request.model_copy(update={"messages": messages})


@register_function(config_type=FinusRiskProfileBranchConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_risk_profile_branch(config: FinusRiskProfileBranchConfig, builder: Builder):
    inner = await builder.get_function(config.inner_function_name)
    preferences_fn = await builder.get_function(config.preferences_function_name)
    guidance = {RISK_CONSERVATIVE: config.conservative_guidance, RISK_AGGRESSIVE: config.aggressive_guidance}

    async def run_branch(chat_request_or_message: ChatRequestOrMessage) -> str:
        profile = await read_risk_profile(preferences_fn)
        if profile is None:
            return await inner.ainvoke(chat_request_or_message)
        chat_request = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
        logger.info("추천 브랜치에 사용자 성향 주입: %s", profile)
        return await inner.ainvoke(with_risk_profile(chat_request, risk_profile_block(profile, guidance[profile])))

    yield FunctionInfo.from_fn(
        run_branch,
        description=config.tool_description or "Recommendation branch with the user's stored risk profile applied.",
    )
