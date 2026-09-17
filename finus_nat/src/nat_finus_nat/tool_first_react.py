"""첫 턴의 도구 호출을 API 수준에서 강제하는 ReAct 에이전트 (#394).

벤더 ``react_agent``는 도구 호출을 모델의 텍스트 출력(``Action:``/``Action Input:``)에만
맡긴다. gpt-5.4-mini는 도구가 정상 바인딩되고 프롬프트가 온전해도 첫 턴에 곧바로
``Final Answer:``(또는 형식 없는 평문)를 쓰고 끝냈고, 벤더는 두 경우 모두 정상 종료로
받아들인다 — 파싱 재시도나 ``raise_on_parsing_failure``는 **파싱에 실패했을 때만** 타는
경로라 개입할 틈이 없다. 수치가 없는 "조회 결과가 없어 답할 수 없다"는 답은 검증 게이트도
통과시키므로, 사용자에게는 도구를 한 번도 부르지 않은 거절만 돌아갔다.

그래서 **scratchpad가 빈 첫 턴**만 ``tool_choice="required"``로 도구를 바인딩해 호출한다.
도구 호출은 모델의 선택이 아니라 API 계약이 되고, 이후 턴(Observation을 받은 뒤)은 벤더의
텍스트 ReAct를 그대로 탄다. 강제 턴은 벤더 LLM 호출 헬퍼를 거치지 않고 runnable을 직접
``ainvoke``한다: ``_stream_llm``은 청크의 ``content``만 이어 붙이고 ``_call_llm``은
``AIMessage(content=...)``로 다시 만들어, 둘 다 ``tool_calls``를 버린다
(``use_native_tool_calling: true``가 빈 응답으로 끝나던 원인이기도 하다).

강제 대상은 ``first_turn_tool_names``로 좁힌다. 메모리 도구처럼 데이터를 만들지 않는
도구를 넣으면 모델이 그것으로 "도구 호출"을 채우고 끝낼 수 있다.
"""

import json
import logging

from langchain_core.agents import AgentAction
from pydantic import Field
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


class ToolFirstReActAgentGraph(ReActAgentGraph):
    """첫 턴만 도구 호출을 강제하고 나머지는 벤더 ReAct 그래프에 맡긴다."""

    def __init__(self, *, first_turn_tools, **kwargs):
        super().__init__(**kwargs)
        # 벤더 __init__이 만든 self.agent는 (tools/tool_names를 채운 프롬프트) | LLM이다.
        # 같은 프롬프트를 쓰고 LLM 바인딩만 바꿔 끼워, 강제 턴이 보는 지시가 평소 턴과 갈라지지 않게 한다.
        self.first_turn_agent = self.agent.first | self.llm.bind_tools(
            first_turn_tools,
            tool_choice="required",
            parallel_tool_calls=False,  # 벤더 tool_node는 한 턴에 도구 하나만 실행한다
        )

    async def agent_node(self, state: ReActGraphState):
        # 이미 도구를 부른 뒤이거나 입력이 비었으면(벤더가 NO_INPUT 응답을 만든다) 벤더 경로.
        if state.agent_scratchpad or not state.messages or not str(state.messages[-1].content).strip():
            return await super().agent_node(state)

        # 벤더 _call_llm/_stream_llm을 쓰지 않는다 — 둘 다 응답을 content만으로 재조립해 tool_calls를 버린다.
        output = await self.first_turn_agent.ainvoke(
            {"question": str(state.messages[-1].content), "chat_history": self._get_chat_history(state.messages)},
            config=self._runnable_config,
        )
        if not output.tool_calls:
            # tool_choice를 무시하는 공급자(OpenAI 호환 프록시 등)다. 여기서 막으면 에이전트가
            # 아예 답하지 못하므로 벤더 경로로 넘기되, #394 증상이 조용히 재발하지 않게 남긴다.
            logger.warning(
                "%s tool_choice=required 응답에 tool_calls가 없어 텍스트 ReAct로 폴백한다 (#394)",
                AGENT_LOG_PREFIX,
            )
            return await super().agent_node(state)

        call = output.tool_calls[0]
        tool_input = json.dumps(call.get("args") or {}, ensure_ascii=False)
        thought = str(output.content).strip() or "요청에 답하려면 먼저 데이터를 조회한다."
        # 다음 턴의 scratchpad에 이 log가 모델 발화로 들어간다. 텍스트 ReAct 형식으로 남겨야
        # 모델이 이어지는 턴에서도 같은 형식(Action/Final Answer)을 따른다.
        state.agent_scratchpad += [
            AgentAction(
                tool=call["name"],
                tool_input=tool_input,
                log=f"Thought: {thought}\nAction: {call['name']}\nAction Input: {tool_input}",
            )
        ]
        return state


class FinusToolFirstReActAgentConfig(ReActAgentWorkflowConfig, name="finus_tool_first_react_agent"):
    """벤더 ``react_agent`` 설정 + 첫 턴에 강제로 고를 도구 목록."""

    first_turn_tool_names: list[FunctionRef] = Field(
        ...,
        min_length=1,
        description="첫 턴에 tool_choice=required로 바인딩할 도구. tool_names의 부분집합이어야 한다.",
    )

    @model_validator(mode="after")
    def _first_turn_tools_are_agent_tools(self):
        agent_tools = {str(name) for name in self.tool_names}
        stray = [str(name) for name in self.first_turn_tool_names if str(name) not in agent_tools]
        if stray:
            raise ValueError(f"first_turn_tool_names는 tool_names에 있어야 합니다: {stray}")
        return self


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
    wanted = {str(name) for name in config.first_turn_tool_names}
    first_turn_tools = [tool for tool in tools if tool.name in wanted]
    if {tool.name for tool in first_turn_tools} != wanted:
        raise ValueError(f"first_turn_tool_names 중 빌드되지 않은 도구가 있습니다: {sorted(wanted)}")
    logger.info("%s 첫 턴 강제 도구=%s 전체 도구=%s", AGENT_LOG_PREFIX,
                [tool.name for tool in first_turn_tools], [tool.name for tool in tools])

    graph = await ToolFirstReActAgentGraph(
        first_turn_tools=first_turn_tools,
        llm=llm,
        prompt=prompt,
        tools=tools,
        use_tool_schema=config.include_tool_input_schema_in_tool_description,
        detailed_logs=config.verbose,
        log_response_max_chars=config.log_response_max_chars,
        retry_agent_response_parsing_errors=config.retry_agent_response_parsing_errors,
        parse_agent_response_max_retries=config.parse_agent_response_max_retries,
        tool_call_max_retries=config.tool_call_max_retries,
        pass_tool_call_errors_to_agent=config.pass_tool_call_errors_to_agent,
        normalize_tool_input_quotes=config.normalize_tool_input_quotes,
        raise_on_parsing_failure=config.raise_on_parsing_failure,
        use_native_tool_calling=config.use_native_tool_calling).build_graph()

    async def _response_fn(chat_request_or_message: ChatRequestOrMessage) -> ChatResponse | str:
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

    yield FunctionInfo.from_fn(_response_fn, description=config.description)
