"""첫 턴의 도구 호출을 API 수준에서 강제하는 ReAct 에이전트 (#394).

벤더 ``react_agent``는 도구 호출을 모델의 텍스트 출력(``Action:``/``Action Input:``)에만
맡긴다. gpt-5.4-mini는 도구가 정상 바인딩되고 프롬프트가 온전해도 첫 턴에 곧바로
``Final Answer:``(또는 형식 없는 평문)를 쓰고 끝냈고, 벤더는 두 경우 모두 정상 종료로
받아들인다 — 파싱 재시도나 ``raise_on_parsing_failure``는 **파싱에 실패했을 때만** 타는
경로라 개입할 틈이 없다. 수치가 없는 "조회 결과가 없어 답할 수 없다"는 답은 검증 게이트도
통과시키므로, 사용자에게는 도구를 한 번도 부르지 않은 거절만 돌아갔다.

그래서 **scratchpad가 빈 첫 턴**만 ``tool_choice="required"``로 도구를 바인딩해 호출한다.
도구 호출은 모델의 선택이 아니라 API 계약이 된다(이후 턴은 아래 #435 참고). 강제 턴은 벤더 LLM 호출 헬퍼를 거치지 않고 runnable을 직접
``ainvoke``한다: ``_stream_llm``은 청크의 ``content``만 이어 붙이고 ``_call_llm``은
``AIMessage(content=...)``로 다시 만들어, 둘 다 ``tool_calls``를 버린다
(``use_native_tool_calling: true``가 빈 응답으로 끝나던 원인이기도 하다).

강제 대상은 ``first_turn_tool_names``로 좁힌다. 메모리 도구처럼 데이터를 만들지 않는
도구를 넣으면 모델이 그것으로 "도구 호출"을 채우고 끝낼 수 있다.

강제는 도구를 고르게 할 뿐 인자까지 보장하지 않는다. 모델은 가끔 KIS 도구를 ``{}``로
골랐고(#436), 그 호출은 오류만 돌려받은 뒤 재조회 없이 시세 없는 답으로 끝났다. 그래서
강제 호출의 인자는 실행 전에 그 도구의 입력 스키마와 도구별 사전 검사로 확인하고, 거부되면
인자 JSON이 깨진 경우와 같이 강제를 다시 시도한다. 마지막 시도까지 거부되면 폴백하지 않고
그 호출을 실행한다 — 오류가 Observation으로 모델에 가고 원장에 실패가 남는다.

**Observation을 받은 뒤의 턴도 도구를 API로 바인딩한다 (#435).** 같은 모델은 텍스트 ReAct만
열려 있는 턴에서 두 번째 조회를 거의 하지 않는다 — 첫 Observation 직후의 입력을 그대로 두고
LLM만 반복 호출한 실측에서, 도구 없이(텍스트 ReAct) 부르면 "삼성전자·SK하이닉스 중 더 싼 쪽"이
6번 중 5번 ``Final Answer``("SK하이닉스는 조회하지 않아 비교할 수 없다")로 끝났고, 같은 입력에
도구만 바인딩하면 8번 모두 SK하이닉스를 조회했다. 그래서 이후 턴은 ``tool_choice``를 강제하지
않고(``auto`` — 답할 수 있으면 끝내야 한다) 첫 턴과 같은 데이터 도구를 바인딩한다(쓰기·메모리 도구는
지금처럼 텍스트 ReAct로만 고른다). 벤더 ``_stream_llm``은 ``tool_calls``를 버리므로 바꿔 끼운다
(벤더 ``use_native_tool_calling: true``가 쓸모없던 이유와 같다). 모델이 고른 도구 호출은 벤더 파서에
자리표시자만 넘기고 파싱 뒤에 실제 인자로 되돌린다 — 인자를 텍스트로 옮기면 값 안의 파서 키워드에
깨진다. 그래서 이후 턴도 벤더의 재시도·직답 수용·scratchpad 경로를 그대로 탄다. 첫 턴 강제가 텍스트
ReAct로 폴백할 때도 같은 바인딩을 쓴다.

**강제 첫 턴은 여러 조회를 한 번에 계획하게 하고, 계획한 조회를 차례로 모두 실행한다 (#443).** 뉴스와 현재가를
함께 묻는 질문에서 첫 턴은 ``parallel_tool_calls=False``라 하나만 고를 수 있었고 매번 뉴스를 골랐다(8/8).
뉴스 Observation을 받은 뒤의 턴은 도구가 바인딩돼 있어도 현재가를 3~4/10만 조회했다 — 나머지는 현재가를 빠뜨리거나
기사 본문에 실린 장중 가격을 현재가로 옮겼다. 같은 질문을 같은 첫 턴 입력에 ``parallel_tool_calls=True``로 부르면
뉴스와 현재가를 함께 골랐고(24/24, 질문 3종), 뉴스만 묻는 질문은 뉴스 하나만 골랐다(16/16). 그래서 첫 턴의 호출
여러 개를 받아, 첫 번째는 바로 실행하고 나머지는 :class:`ToolFirstGraphState`의 ``planned_tool_calls``에 두었다가
LLM을 다시 부르지 않고 하나씩 실행한다. 벤더 tool_node는 그대로 한 번에 하나씩 실행하므로 원장·scratchpad·
Observation 경로는 같다. 같은 호출은 한 번만 두고, 상한(:data:`FIRST_TURN_MAX_PLANNED_CALLS`)을 넘는 호출은
경고를 남기고 버린다(남은 조회는 이후 턴에서 고를 수 있다).

**모든 턴에 오늘 날짜(한국 시간)를 넣는다 (#440).** 이후 턴이 조회를 이어가자, 날짜를 모르는 모델이
기간 인자를 지어내 틀린 수치를 단정했다(52주 최고가를 2024년 한 해로 조회). 날짜는 프롬프트를 포맷할
때마다 시계에서 읽는다.
"""

import json
import logging
import re
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from langchain_core.agents import AgentAction
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts import SystemMessagePromptTemplate
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import ChatRequestOrMessage
from nat.data_models.api_server import ChatResponse
from nat.data_models.api_server import Usage
from nat.data_models.component_ref import FunctionRef
from nat.plugins.langchain.agent.base import AGENT_LOG_PREFIX
from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph
from nat.plugins.langchain.agent.react_agent.agent import ReActGraphState
from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig
from nat.utils.type_converter import GlobalTypeConverter

logger = logging.getLogger(__name__)

# 강제 턴 호출 횟수 상한. 인자 JSON이 깨진 응답(invalid_tool_calls)과 인자가 도구에 맞지 않는
# 응답(_schema_rejection)이 이 상한을 함께 쓴다.
FIRST_TURN_MAX_ATTEMPTS = 2

# 모델이 도구 호출과 함께 낸 본문을 Thought로 옮길 때, 이 키워드가 있으면 벤더 파서가 본문을
# Action/Final Answer로 오인한다. 그런 본문은 버리고 기본 Thought를 쓴다.
_REACT_KEYWORD_RE = re.compile(r"(action|input|observation|final\s+answer)\s*\d*\s*:", re.IGNORECASE)
_DEFAULT_THOUGHT = "요청에 답하려면 데이터를 조회한다."

# 강제 첫 턴에 모델이 한 번에 계획한 조회를 실행하는 상한(#443). 질문 하나가 여러 종목·여러 데이터를
# 묻는 정도면 충분하고, 그 이상은 모델의 과잉 계획으로 보고 버린다 — 정말 필요하면 이후 턴에서 다시 고른다.
FIRST_TURN_MAX_PLANNED_CALLS = 4

# 이후 턴에서 인자 JSON이 깨진 도구 호출(invalid_tool_calls)을 다시 요청하는 횟수 상한(첫 응답 포함).
FOLLOWUP_MAX_ATTEMPTS = 2
_INVALID_TOOL_JSON_RETRY = (
    "직전 도구 호출({name})의 인자 JSON이 깨져 도구를 실행하지 못했습니다({error}). "
    "같은 도구를 올바른 JSON 인자로 다시 호출하세요."
)

# ---- 오늘 날짜 주입 (#440) ----
# 에이전트 입력 어디에도 오늘 날짜가 없어, 기간이 필요한 조회에서 모델이 날짜를 지어냈다(52주 최고가를
# 2024년 한 해나 1년 전 하루로 조회). 한국 시간은 서머타임이 없어 고정 오프셋으로 충분하다.
KST = timezone(timedelta(hours=9), "KST")
_WEEKDAYS_KO = "월화수목금토일"
TODAY_PROMPT = (
    "오늘 날짜(한국 시간): {finus_today}. 오늘·어제·최근·52주·이번 분기처럼 날짜나 기간이 필요한 조회는 "
    "이 날짜를 기준으로 인자를 정하세요."
)


def kst_today_text(now: datetime) -> str:
    """*now*를 한국 시간 날짜와 요일로 적는다. 예: ``2026-10-20(화)``."""
    day = now.astimezone(KST)
    return f"{day:%Y-%m-%d}({_WEEKDAYS_KO[day.weekday()]})"


def _system_clock() -> datetime:
    return datetime.now(KST)


def prompt_with_today(prompt: ChatPromptTemplate, clock: Callable[[], datetime]) -> ChatPromptTemplate:
    """시스템 프롬프트 바로 뒤에 오늘 날짜 시스템 메시지를 넣는다.

    날짜는 partial **함수**로 넣어 프롬프트를 포맷할 때마다(=LLM을 부를 때마다) *clock*을 읽는다.
    문자열로 넣으면 워크플로를 빌드한 날짜에 굳어, 장기 실행 서버가 다음 날부터 어제 날짜를 준다.
    """
    date_message = SystemMessagePromptTemplate.from_template(TODAY_PROMPT)
    with_date = ChatPromptTemplate.from_messages([prompt.messages[0], date_message, *prompt.messages[1:]])
    return with_date.partial(**prompt.partial_variables, finus_today=lambda: kst_today_text(clock()))


# 이후 턴의 네이티브 도구 호출을 벤더 파서에 넘길 때 쓰는 자리표시자 상자.
# 인자를 텍스트로 옮기면 값 안의 " observation"·"Final Answer:" 같은 문자열에 파서가 깨진다. 그래서 파서에는
# 이 상자의 키만 Action Input으로 넘기고, 파싱 뒤 agent_node가 실제 인자로 되돌린다.
_NATIVE_CALLS: ContextVar[dict[str, tuple[dict, object]] | None] = ContextVar("finus_native_tool_calls", default=None)


def react_text_for_tool_call(content: object, call: dict) -> str:
    """네이티브 도구 호출 하나를 벤더 파서가 읽는 텍스트 ReAct 한 단계로 옮긴다.

    이 텍스트는 다음 턴의 scratchpad에 모델 발화로 다시 들어간다. 어떤 도구를 어떤 인자로
    불렀는지가 남아야 모델이 같은 조회를 되풀이하지 않고 남은 조회로 넘어간다.
    """
    thought = content.strip() if isinstance(content, str) else ""
    if not thought or _REACT_KEYWORD_RE.search(thought):
        thought = _DEFAULT_THOUGHT
    thought = " ".join(thought.split())
    tool_input = json.dumps(call.get("args") or {}, ensure_ascii=False, separators=(",", ":"))
    return f"Thought: {thought}\nAction: {call['name']}\nAction Input: {tool_input}"


class ToolFirstGraphState(ReActGraphState):
    """벤더 상태 + 강제 첫 턴이 계획했지만 아직 실행하지 않은 도구 호출 (#443)."""

    planned_tool_calls: list[AgentAction] = Field(default_factory=list)


class ToolFirstReActAgentGraph(ReActAgentGraph):
    """첫 턴은 도구 호출을 강제하고, 이후 턴은 도구를 바인딩한 채(강제 없이) 벤더 ReAct 그래프에 맡긴다."""

    def __init__(self, *, first_turn_tools, clock: Callable[[], datetime] | None = None, **kwargs):
        super().__init__(**kwargs)
        # 벤더 __init__이 만든 self.agent는 (tools/tool_names를 채운 프롬프트) | LLM이다.
        # 같은 프롬프트를 쓰고 LLM 바인딩만 바꿔 끼워, 강제 턴이 보는 지시가 평소 턴과 갈라지지 않게 한다.
        # 오늘 날짜(#440)는 이 프롬프트에 넣는다 — 강제 첫 턴·이후 턴·폴백이 모두 이 프롬프트를 지나므로
        # 진입 경로(라우터 브랜치, 성향 브랜치, CLI)와 무관하게 모든 ReAct 턴이 같은 날짜를 본다.
        prompt = prompt_with_today(self.agent.first, clock or _system_clock)
        # 벤더가 붙인 정지 시퀀스(gpt-5 계열이 아닌 모델의 "Observation:")는 이후 턴에도 유지한다.
        vendor_bind = dict(getattr(self.agent.last, "kwargs", None) or {})
        if "tools" in vendor_bind:
            raise ValueError("finus_tool_first_react_agent는 도구를 직접 바인딩한다 — use_native_tool_calling은 false로 둔다")
        self.first_turn_agent = prompt | self.llm.bind_tools(
            first_turn_tools,
            tool_choice="required",
            # #443: 질문이 묻는 조회를 한 번에 계획하게 한다. 벤더 tool_node는 한 번에 하나만 실행하므로
            # 두 번째부터는 planned_tool_calls에 두고 agent_node가 하나씩 꺼낸다.
            parallel_tool_calls=True,
        )
        # #435: Observation 뒤의 턴. 강제하지 않되(답할 수 있으면 끝낸다) 데이터 도구를 API로 열어 둔다.
        # 쓰기·메모리 도구는 API로 열지 않는다 — 일지 저장처럼 부수효과가 있는 도구는 지금처럼 모델이
        # 텍스트 ReAct로 명시적으로 고를 때만 나간다(diary_agent.yml 주석과 같은 원칙). 프롬프트의 도구
        # 목록은 그대로라 텍스트로는 여전히 고를 수 있다.
        # 벤더 agent_node가 self.agent를 부르므로 이것을 바꿔 끼우고, tool_calls는 _stream_llm에서 받는다.
        self.agent = prompt | self.llm.bind_tools(
            first_turn_tools,
            parallel_tool_calls=False,
            **vendor_bind,
        )

    def _call_with_schema_args(self, call: dict) -> dict:
        """도구 호출의 인자에서 그 도구의 입력 스키마에 없는 필드를 걷어낸 사본을 돌려준다.

        실측(#435)에서 모델은 뉴스 도구를 ``{"stock_name": ..., "success": true, "articles": [지어낸 기사]}``
        로 불렀다. 도구는 스키마 밖의 필드를 버리고 실제 뉴스를 돌려줬지만, 지어낸 기사가 Action Input으로
        scratchpad에 남아 다음 턴의 답에 그대로 섞였다. scratchpad에는 도구가 실제로 받는 인자만 남긴다.
        스키마가 추가 필드를 받는 도구(``extra="allow"``)이거나 스키마를 알 수 없으면 그대로 둔다.
        """
        tool = self.tools_dict.get(call.get("name"))
        schema = getattr(tool, "args_schema", None)
        args = call.get("args") or {}
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)) or not isinstance(args, dict):
            return call
        if schema.model_config.get("extra") == "allow":
            return call
        kept = {key: value for key, value in args.items() if key in schema.model_fields}
        if kept != args:
            logger.warning("%s 도구 %s 호출에서 스키마 밖의 인자를 버렸다: %s", AGENT_LOG_PREFIX, call.get("name"),
                           sorted(set(args) - set(kept)))
        return {**call, "args": kept}

    async def _invoke_retrying_broken_tool_json(self, runnable, inputs):
        """LLM을 부르고, 인자 JSON이 깨진 도구 호출만 돌아오면 그 사실을 알려 다시 부른다.

        langchain_openai는 인자 JSON을 파싱하지 못한 호출을 ``invalid_tool_calls``로 빼고 ``tool_calls``를
        비운다. 그대로 두면 본문이 빈 응답이 되고, 벤더 재시도는 "답할 수 없으면 Final Answer로 답하라"를
        덧붙여 남은 조회 없이 끝난다. 첫 턴(#394)과 같이 모델의 일회성 실수로 보고, 무엇이 깨졌는지를
        scratchpad에 덧붙여 같은 도구를 다시 부르게 한다.
        """
        for attempt in range(1, FOLLOWUP_MAX_ATTEMPTS + 1):
            output = await runnable.ainvoke(inputs, config=self._runnable_config)
            if output.tool_calls or not output.invalid_tool_calls:
                return output
            bad = output.invalid_tool_calls[0]
            logger.warning(
                "%s 도구 호출의 인자를 파싱하지 못했다 (시도 %d/%d%s): %s",
                AGENT_LOG_PREFIX,
                attempt,
                FOLLOWUP_MAX_ATTEMPTS,
                ", 상한이라 벤더 재시도로 넘긴다" if attempt == FOLLOWUP_MAX_ATTEMPTS else "",
                [(call.get("name"), call.get("error")) for call in output.invalid_tool_calls],
            )
            inputs = {
                **inputs,
                "agent_scratchpad": [
                    *(inputs.get("agent_scratchpad") or []),
                    AIMessage(content=f"Action: {bad.get('name')}\nAction Input: {bad.get('args')}"),
                    HumanMessage(content=_INVALID_TOOL_JSON_RETRY.format(name=bad.get("name"), error=bad.get("error"))),
                ],
            }
        return output

    async def _stream_llm(self, runnable, inputs):
        """벤더 ``_stream_llm``을 대신한다 — 도구 호출을 버리지 않고 벤더 파서에 넘긴다.

        벤더 구현은 스트림 청크의 ``content``만 이어 붙여 ``tool_calls``를 잃는다. 여기서는 응답을
        통째로 받아, 도구 호출이 있으면 그 호출을 :data:`_NATIVE_CALLS` 상자에 맡기고 상자의 키를
        Action Input으로 쓴 텍스트를 돌려준다. 실제 인자는 :meth:`_vendor_agent_node`가 파싱 뒤에 되돌린다.
        """
        output = await self._invoke_retrying_broken_tool_json(runnable, inputs)
        additional_kwargs = {}
        reasoning = (output.additional_kwargs or {}).get("reasoning_content")
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        if not output.tool_calls:
            content = output.content if isinstance(output.content, str) else ""
            return AIMessage(content=content, additional_kwargs=additional_kwargs)
        if len(output.tool_calls) > 1:
            # parallel_tool_calls=False라도 공급자가 여러 개를 줄 수 있다. 벤더 tool_node는 하나만 실행한다.
            logger.warning("%s 도구 호출 %d개 중 첫 번째만 실행한다 — 버린 호출: %s", AGENT_LOG_PREFIX,
                           len(output.tool_calls), [call.get("name") for call in output.tool_calls[1:]])
        call = self._call_with_schema_args(output.tool_calls[0])
        box = _NATIVE_CALLS.get()
        if box is None:
            # 상자 없이 불린 경우(이 클래스 밖의 호출자) — 텍스트로 옮긴다.
            return AIMessage(content=react_text_for_tool_call(output.content, call), additional_kwargs=additional_kwargs)
        key = f"finus-native-call-{len(box) + 1}"
        box[key] = (call, output.content)
        content = f"Thought: {_DEFAULT_THOUGHT}\nAction: {call['name']}\nAction Input: {key}"
        return AIMessage(content=content, additional_kwargs=additional_kwargs)

    async def _vendor_agent_node(self, state: ReActGraphState):
        """벤더 ``agent_node``를 돌리고, 네이티브 도구 호출의 자리표시자를 실제 인자로 되돌린다."""
        box: dict[str, tuple[dict, object]] = {}
        token = _NATIVE_CALLS.set(box)
        try:
            state = await super().agent_node(state)
        finally:
            _NATIVE_CALLS.reset(token)
        if box and state.agent_scratchpad:
            last = state.agent_scratchpad[-1]
            key = str(last.tool_input).strip()
            if key in box:
                call, content = box[key]
                state.agent_scratchpad[-1] = AgentAction(
                    tool=call["name"],
                    tool_input=json.dumps(call.get("args") or {}, ensure_ascii=False),
                    log=react_text_for_tool_call(content, call),
                )
        return state

    def _schema_rejection(self, call: dict) -> str | None:
        """강제 호출의 인자가 고른 도구에서 오류만 낼 것이면 그 사유를, 아니면 None을 돌려준다.

        ``tool_choice="required"``는 도구를 **고르게** 할 뿐 인자가 쓸 만한지는 보장하지 않는다.
        스키마의 필드가 전부 선택이면 ``{}``도 API 수준에서는 유효한 호출이다. 그래서 (1) 도구
        입력 스키마 검증과 (2) 입력 모델이 ``forced_call_rejection()``을 두면 그 사전 검사를
        실행 전에 돌린다. (2)를 스키마 검증에 넣지 않는 것은 강제 턴 밖의 같은 호출이 래퍼까지
        가서 원장에 실패로 남게 하려는 것이다(KIS 래퍼, #436).

        판정할 수 없는 경우(예상 밖 예외)는 None — 막지 않고 실행에 맡긴다.
        """
        tool = self.tools_dict.get(call.get("name"))
        schema = getattr(tool, "args_schema", None)
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            return None
        try:
            parsed = schema.model_validate(call.get("args") or {})
            precheck = getattr(parsed, "forced_call_rejection", None)
            reason = precheck() if callable(precheck) else None
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(입력)'}: {e['msg']}" for e in exc.errors())
        except Exception:  # noqa: BLE001 — 사전 검사의 버그가 강제 턴을 막지 않게 한다
            logger.warning("%s 강제 도구 호출 인자 검사 중 예외 — 검사 없이 실행한다", AGENT_LOG_PREFIX, exc_info=True)
            return None
        return f"{call.get('name')}: {reason}" if reason else None

    def _planned_actions(self, output) -> list[AgentAction]:
        """강제 첫 턴 응답의 도구 호출들을 실행 순서대로 AgentAction으로 옮긴다 (#443).

        스키마 밖 인자는 걷어내고(#435), 걷어낸 뒤 같은 호출은 한 번만 남기며, 상한을 넘는 호출은 경고와 함께
        버린다. 모델 본문(Thought)은 첫 호출에만 붙인다 — 나머지는 모델 발화가 아니라 계획의 연속이다.
        """
        actions: list[AgentAction] = []
        seen: set[str] = set()
        dropped: list[str] = []
        for call in output.tool_calls:
            call = self._call_with_schema_args(call)
            tool_input = json.dumps(call.get("args") or {}, ensure_ascii=False)
            key = f"{call['name']}\n{tool_input}"
            if key in seen:
                continue
            seen.add(key)
            if len(actions) >= FIRST_TURN_MAX_PLANNED_CALLS:
                dropped.append(call["name"])
                continue
            actions.append(AgentAction(
                tool=call["name"],
                tool_input=tool_input,
                log=react_text_for_tool_call(output.content if not actions else "", call),
            ))
        if dropped:
            logger.warning("%s 강제 첫 턴의 도구 호출이 상한(%d)을 넘어 버렸다: %s", AGENT_LOG_PREFIX,
                           FIRST_TURN_MAX_PLANNED_CALLS, dropped)
        return actions

    async def agent_node(self, state: ToolFirstGraphState):
        # 타입 표기가 동작을 정한다: LangGraph는 노드 함수의 상태 타입 표기를 그 노드의 입력 스키마로 읽어
        # 그래프 채널에 더한다. 그래서 벤더 build_graph(ReActGraphState) 그대로 planned_tool_calls가 턴 사이에
        # 남는다. ReActGraphState로 바꿔 적으면 이 노드가 그 필드 없는 상태를 받아 실패한다.
        # 강제 첫 턴이 계획한 조회가 남았으면 LLM을 부르지 않고 다음 조회를 낸다(#443).
        if getattr(state, "planned_tool_calls", None):
            state.agent_scratchpad += [state.planned_tool_calls.pop(0)]
            return state
        # 이미 도구를 부른 뒤이거나 입력이 비었으면(벤더가 NO_INPUT 응답을 만든다) 벤더 경로.
        if state.agent_scratchpad or not state.messages or not str(state.messages[-1].content).strip():
            return await self._vendor_agent_node(state)

        inputs = {"question": str(state.messages[-1].content), "chat_history": self._get_chat_history(state.messages)}
        for attempt in range(1, FIRST_TURN_MAX_ATTEMPTS + 1):
            # 벤더 _call_llm/_stream_llm을 쓰지 않는다 — 둘 다 응답을 content만으로 재조립해 tool_calls를 버린다.
            output = await self.first_turn_agent.ainvoke(inputs, config=self._runnable_config)
            if output.tool_calls:
                # 계획한 호출 중 하나라도 오류만 낼 인자면 계획 전체를 다시 강제한다(#443).
                rejection = next(filter(None, map(self._schema_rejection, output.tool_calls)), None)
                # JSON은 온전하지만 도구가 오류만 낼 인자다(#436: KIS 도구를 {}로 고름). 그대로
                # 실행하면 모델은 그 오류를 보고 재조회 없이 답을 끝냈다 — 다시 강제한다.
                # 마지막 시도면 실행한다: 폴백하면 도구 없이 답하고(#394) 원장에도 남지 않는다.
                if rejection is not None:
                    logger.warning(
                        "%s 강제 도구 호출의 인자가 도구에 맞지 않는다 (시도 %d/%d%s): %s",
                        AGENT_LOG_PREFIX,
                        attempt,
                        FIRST_TURN_MAX_ATTEMPTS,
                        ", 상한이라 그대로 실행한다" if attempt == FIRST_TURN_MAX_ATTEMPTS else "",
                        rejection,
                    )
                    if attempt < FIRST_TURN_MAX_ATTEMPTS:
                        continue
                break
            if not output.invalid_tool_calls:
                # tool_choice를 무시하는 공급자(OpenAI 호환 프록시 등)다. 여기서 막으면 에이전트가
                # 아예 답하지 못하므로 벤더 경로로 넘기되, #394 증상이 조용히 재발하지 않게 남긴다.
                logger.warning(
                    "%s tool_choice=required 응답에 도구 호출이 없어 텍스트 ReAct로 폴백한다 (#394)",
                    AGENT_LOG_PREFIX,
                )
                return await self._vendor_agent_node(state)
            # 도구는 골랐지만 인자 JSON이 깨져 langchain_openai가 invalid_tool_calls로 뺐다.
            # 공급자 문제가 아니라 모델의 일회성 실수이므로 강제 호출을 다시 시도한다.
            logger.warning(
                "%s 강제 도구 호출의 인자를 파싱하지 못했다 (시도 %d/%d): %s",
                AGENT_LOG_PREFIX,
                attempt,
                FIRST_TURN_MAX_ATTEMPTS,
                [(call.get("name"), call.get("error")) for call in output.invalid_tool_calls],
            )
        else:
            logger.warning("%s 강제 도구 호출이 계속 유효하지 않아 텍스트 ReAct로 폴백한다 (#394)", AGENT_LOG_PREFIX)
            return await self._vendor_agent_node(state)

        # 다음 턴의 scratchpad에 각 log가 모델 발화로 들어간다. 텍스트 ReAct 형식으로 남겨야
        # 모델이 이어지는 턴에서도 같은 형식(Action/Final Answer)을 따른다.
        first, *rest = self._planned_actions(output)
        state.agent_scratchpad += [first]
        state.planned_tool_calls = rest
        return state


class FinusToolFirstReActAgentConfig(ReActAgentWorkflowConfig, name="finus_tool_first_react_agent"):
    """벤더 ``react_agent`` 설정 + API로 바인딩할 데이터 도구 목록."""

    first_turn_tool_names: list[FunctionRef] = Field(
        ...,
        min_length=1,
        description=(
            "API(bind_tools)로 여는 데이터 조회 도구. 첫 턴에는 tool_choice=required로 이 중 하나 이상을 강제하고(여러 개면 차례로 실행, #443), "
            "Observation 뒤의 턴에도 강제 없이 이 도구들을 API로 연다(#435). tool_names의 부분집합이어야 하며 "
            "쓰기·메모리 도구는 넣을 수 없다 — 그런 도구는 텍스트 ReAct로만 고른다."
        ),
    )

    @model_validator(mode="after")
    def _first_turn_tools_are_agent_tools(self):
        agent_tools = {str(name) for name in self.tool_names}
        stray = [str(name) for name in self.first_turn_tool_names if str(name) not in agent_tools]
        if stray:
            raise ValueError(f"first_turn_tool_names는 tool_names에 있어야 합니다: {stray}")
        return self


def non_data_tool_config_types() -> tuple[type, ...]:
    """API로 열면 안 되는 도구의 설정 타입: 외부 상태를 바꾸는 쓰기 도구와 데이터를 만들지 않는 메모리 도구.

    이름이 아니라 타입으로 가린다 — 설정 파일에서 도구 이름은 바꿔 붙일 수 있지만 타입은 구현을 가리킨다.
    """
    from finus_agents.agents import FinusMemoryDisabledConfig
    from finus_agents.finus_api import FinusSaveDiaryConfig
    from finus_agents.user_memory import FinusUserMemoryAddRefusedConfig
    from finus_agents.user_memory import FinusUserMemoryGetConfig

    return (FinusSaveDiaryConfig, FinusMemoryDisabledConfig, FinusUserMemoryAddRefusedConfig, FinusUserMemoryGetConfig)


def reject_non_data_api_tools(tool_names, get_function_config) -> None:
    """``first_turn_tool_names``에 쓰기·메모리 도구가 있으면 빌드를 멈춘다(설정 실수 시 fail-closed).

    이 집합은 첫 턴 강제와 이후 턴 API 바인딩에 쓰인다. 쓰기 도구가 들어가면 사용자가 요청하지 않은
    저장이 API 경로로 나갈 수 있고, 메모리 도구가 들어가면 조회 없이 강제 호출을 채울 수 있다.
    """
    banned = non_data_tool_config_types()
    offending = sorted(str(name) for name in tool_names if isinstance(get_function_config(name), banned))
    if offending:
        raise ValueError(f"first_turn_tool_names에 쓰기·메모리 도구를 넣을 수 없습니다: {offending}")


def graph_kwargs(config: ReActAgentWorkflowConfig, *, llm, prompt, tools) -> dict:
    """벤더 ``react_agent_workflow``가 ``ReActAgentGraph``에 넘기는 인자를 그대로 옮긴다.

    ``callbacks``는 벤더 워크플로도 넘기지 않는다. NAT를 올려 생성자 인자가 늘면 여기서
    빠진 인자는 조용히 기본값으로 동작하므로, 시그니처와의 일치를
    ``test_graph_kwargs_cover_vendor_graph_signature``가 지킨다.
    """
    return {
        "llm": llm,
        "prompt": prompt,
        "tools": tools,
        "use_tool_schema": config.include_tool_input_schema_in_tool_description,
        "detailed_logs": config.verbose,
        "log_response_max_chars": config.log_response_max_chars,
        "retry_agent_response_parsing_errors": config.retry_agent_response_parsing_errors,
        "parse_agent_response_max_retries": config.parse_agent_response_max_retries,
        "tool_call_max_retries": config.tool_call_max_retries,
        "pass_tool_call_errors_to_agent": config.pass_tool_call_errors_to_agent,
        "normalize_tool_input_quotes": config.normalize_tool_input_quotes,
        "raise_on_parsing_failure": config.raise_on_parsing_failure,
        "use_native_tool_calling": config.use_native_tool_calling,
    }


@register_function(config_type=FinusToolFirstReActAgentConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_tool_first_react_agent(config: FinusToolFirstReActAgentConfig, builder: Builder):
    """벤더 ``react_agent_workflow``와 같은 흐름에 그래프 클래스만 바꾼다.

    벤더 함수는 ``ReActAgentGraph``를 함수 안에서 직접 import해 쓰므로 클래스만 주입할 수
    없다. 모듈 속성을 바꿔치기하면 같은 프로세스의 다른 react_agent까지 바뀌어 복제를 택했다.
    """
    from langchain_core.messages import trim_messages

    prompt = create_react_agent_prompt(config)
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    tools = await builder.get_tools(tool_names=config.tool_names, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    if not tools:
        raise ValueError(f"No tools specified for ReAct Agent '{config.llm_name}'")
    reject_non_data_api_tools(config.first_turn_tool_names, builder.get_function_config)
    wanted = {str(name) for name in config.first_turn_tool_names}
    first_turn_tools = [tool for tool in tools if tool.name in wanted]
    missing = wanted - {tool.name for tool in first_turn_tools}
    if missing:
        raise ValueError(f"first_turn_tool_names 중 빌드되지 않은 도구가 있습니다: {sorted(missing)}")
    logger.info("%s 첫 턴 강제 도구=%s 전체 도구=%s", AGENT_LOG_PREFIX,
                [tool.name for tool in first_turn_tools], [tool.name for tool in tools])

    graph = await ToolFirstReActAgentGraph(
        first_turn_tools=first_turn_tools,
        **graph_kwargs(config, llm=llm, prompt=prompt, tools=tools),
    ).build_graph()

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse | str:
        try:
            message = GlobalTypeConverter.get().convert(chat_request_or_message, to_type=ChatRequest)
            messages = trim_messages(messages=[m.model_dump() for m in message.messages],
                                     max_tokens=config.max_history,
                                     strategy="last",
                                     token_counter=len,
                                     start_on="human",
                                     include_system=True)
            state = await graph.ainvoke(ReActGraphState(messages=messages),
                                        config={"recursion_limit": (config.max_tool_calls + 1) * 2})
            content = str(ReActGraphState(**state).messages[-1].content)
            prompt_tokens = sum(len(str(msg.content).split()) for msg in message.messages)
            completion_tokens = len(content.split()) if content else 0
            response = ChatResponse.from_string(content,
                                                usage=Usage(prompt_tokens=prompt_tokens,
                                                            completion_tokens=completion_tokens,
                                                            total_tokens=prompt_tokens + completion_tokens))
            if chat_request_or_message.is_string:
                return GlobalTypeConverter.get().convert(response, to_type=str)
            return response
        except Exception as ex:
            # 벤더 react_agent_workflow와 같은 로그 한 줄을 남긴다 — 운영 로그에서 두 타입이 같은 모습으로 보이게.
            logger.error("%s ReAct Agent failed with exception: %s", AGENT_LOG_PREFIX, str(ex))
            raise

    yield FunctionInfo.from_fn(_response_fn, description=config.description)
