import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple, TypeAlias

import httpx
from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from .pii_guard import (
    mask_tool_result,
    placeholder_kind,
    restore_for_internal,
    unrestorable_placeholders,
)

logger = logging.getLogger(__name__)

# ---- Data-tool ledger for tool enforcement (#152) ----


class DataToolRecord(NamedTuple):
    """Immutable record of one Fin-Us data tool invocation."""

    tool_name: str
    ok: bool          # True when the response contains non-error data
    produced_rows: bool  # True when ok and the response body is non-empty
    empty: bool = False  # True when ok=True but the result set is empty (빈 결과)


# Tools that produce irreversible side effects (writes). When any of these appear in
# the ledger and the gate trips, _run_with_gate skips the retry to prevent duplicate
# writes (e.g. two identical diary entries created for one user request).
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({"finus_save_diary"})


@dataclass
class DataToolLedger:
    """Mutable box tracking Fin-Us data tool calls within a run_subagent invocation.

    Stored as a mutable object (not ContextVar[int] + .set()) so that
    mutations remain visible across asyncio.create_task context copies that
    LangGraph creates internally via copy_context().
    """

    records: list[DataToolRecord] = field(default_factory=list)

    def record(self, tool_name: str, *, ok: bool, produced_rows: bool, empty: bool = False) -> None:
        self.records.append(DataToolRecord(tool_name=tool_name, ok=ok, produced_rows=produced_rows, empty=empty))

    def any_success(self) -> bool:
        """Return True if at least one tool call successfully produced data rows."""
        return any(r.ok and r.produced_rows for r in self.records)

    def had_side_effect(self) -> bool:
        """Return True if a side-effecting (write) tool was recorded.

        Used by _run_with_gate to skip the retry and return the rejection immediately,
        preventing duplicate writes when the gate trips after a diary save.
        """
        return any(r.tool_name in _SIDE_EFFECT_TOOLS for r in self.records)

    def only_empty_reads(self) -> bool:
        """읽기 도구가 실행됐고, 실행된 것이 전부 '성공했지만 빈 결과'인 경우.

        게이트가 트립됐을 때 이 메서드가 True이면 재시도가 무의미하다 —
        빈 결과는 두 번째 시도에서도 바뀌지 않는다. 재시도를 건너뛰고
        _EMPTY_RESULT_REJECTION을 즉시 반환한다.
        """
        reads = [r for r in self.records if r.tool_name not in _SIDE_EFFECT_TOOLS]
        return bool(reads) and all(r.empty for r in reads)


DATA_TOOL_LEDGER: ContextVar["DataToolLedger | None"] = ContextVar(
    "finus_data_tool_ledger", default=None
)


# ---- Reasoning trace for the response footnote (#260) ----


class ToolUse(NamedTuple):
    """각주에 실을 도구 한 건 — 도구명과 결과 상태.

    ``ok``는 원장의 ``DataToolRecord.ok``를 그대로 옮긴 값이다(오류 응답이 아님).
    성공 여부를 함께 싣는 이유: 이름만 보내면 소비자가 실패한 호출까지 "확인한
    자료"로 표시하게 되고, 사용자는 답변이 그 데이터에 근거했다고 읽는다. 실제로는
    아니므로, 근거를 보여준다는 이 기능의 목적과 정반대의 오독이 된다.

    ``empty``는 같은 논리의 세 번째 상태다 — ``ok=True``지만 결과 집합이 비어 있는
    경우(#209). 오류가 아니므로 실패로 적으면 틀리지만, 표시 없이 "확인한 자료"로
    적으면 **답변이 근거하지 않은 데이터**를 근거로 제시하게 된다. 특히
    ``_run_with_gate``가 ``only_empty_reads()``에서 반환하는 `[조회 결과 없음]` 답변은
    본문이 "데이터가 없다"고 말하는데 각주만 "자료를 확인했다"고 말하는 정면 충돌이
    된다. 세 상태를 그대로 넘기고 표기는 소비자가 정한다.

    기본값이 있는 이유: 이 필드를 모르는 구버전 소비자·호출부와 섞여도 깨지지 않게
    한다. 기본값 ``False``는 "빈 결과라는 관측이 없음"이지 "데이터가 있었음"이
    아니므로, ``ok``와 함께 읽어야 한다.
    """

    name: str
    ok: bool
    empty: bool = False


def _tool_use_from_record(record: DataToolRecord) -> ToolUse:
    """원장 기록 한 건을 각주용 :class:`ToolUse`로 옮긴다.

    ``empty``는 ``ok=True``일 때만 의미가 있다 — 오류 응답은 결과 집합이 비었는지를
    말하지 않는다. ``ok=False``면 항상 ``empty=False``로 눕혀, 소비자가 두 필드를
    따로 읽어도 "실패인데 빈 결과"라는 모순된 조합을 보지 않게 한다.
    """
    return ToolUse(record.tool_name, record.ok, record.ok and record.empty)


def _tool_use_rank(tool: ToolUse) -> int:
    """도구 결과의 '좋음' 순위 — 실패(0) < 빈 결과(1) < 데이터 있음(2).

    같은 도구를 여러 번 호출했을 때 어느 결과를 각주에 남길지 정하는 데만 쓴다.
    쓰기 도구(``finus_save_diary``)는 성공해도 ``empty=False``이므로 2가 된다 —
    조회 결과가 아니라 "실제로 저장했다"는 뜻이라 맞는 순위다.
    """
    if not tool.ok:
        return 0
    return 1 if tool.empty else 2


@dataclass
class ReasoningTrace:
    """한 요청에서 코드가 관측한 추론 경로 — 어느 브랜치로 갔고 어떤 도구가 실행됐는지.

    **출처 원칙 (#129와 같은 원칙):** 두 값 모두 코드가 남긴 기록에서만 만든다.

    - ``routed_agent`` — supervisor의 ``_choose_branch``가 반환한 브랜치명. 브랜치
      허용 목록으로 정규화된 값이므로 임의 문자열이 들어올 수 없다.
    - ``tools_used`` — 도구 강제 원장(:class:`DataToolLedger`)에 기록된 도구.
      모든 Fin-Us 도구가 ``_record_to_ledger``를 거치므로 "실제로 실행된 도구"다.
    - ``branch_answer`` — supervisor가 브랜치에서 받아 돌려보낸 답변 본문.
      위 두 값이 **어느 텍스트에 대한 기록인지** 식별하는 값이다 (#294).

    LLM 출력 텍스트를 파싱해서 채우지 않는다. 파싱으로 만들면 "실행된 도구"가 아니라
    "모델이 실행했다고 주장하는 도구"가 되어, 근거로 보여주는 각주가 오히려 환각을
    사용자에게 사실처럼 전달하는 표면이 된다.

    :class:`DataToolLedger`와 같은 이유로 **mutable box**다: ``ContextVar.set()``은
    LangGraph가 내부적으로 만드는 ``copy_context()`` 경계를 넘어 바깥으로 전파되지
    않는다. 최상단에서 박스 하나를 심고, 안쪽에서는 박스를 mutate하기만 한다.
    """

    routed_agent: str | None = None
    tools_used: list[ToolUse] = field(default_factory=list)
    branch_answer: str | None = None

    def record_route(self, branch_name: str) -> None:
        """supervisor가 고른 브랜치명을 기록한다 (마지막 라우팅이 최종 값)."""
        self.routed_agent = branch_name

    def record_branch_answer(self, text: str) -> None:
        """supervisor가 브랜치에서 받아 돌려보내는 답변 본문을 기록한다 (#294).

        **각주 부착 조건의 정본이다** — 이 판정의 근거 설명은 여기 한 곳에 둔다.
        (:func:`~nat_finus_nat.agents._trace_branch_answer`,
        :func:`~nat_finus_nat.agents.with_reasoning_trace`가 이 설명을 참조한다.)

        ``routed_agent``/``tools_used``는 브랜치를 **부르기 전후**로 채워지므로, 그
        값이 있다는 사실만으로는 지금 돌려보내는 텍스트가 그 브랜치가 만든 답변이라는
        보장이 되지 않는다. vendor ``auto_memory_agent``는 예외를 삼키고 ``str(ex)``를
        답변으로 돌려주므로(``router.yml``이 ``verbose: true``), 박스가 채워진 채 본문만
        오류 문자열로 바뀐 응답이 최상위까지 올라온다.

        판정 기준이 "브랜치가 성공했는가"가 아닌 이유는 두 번째 변종 때문이다:
        vendor 그래프는 ``inner_agent`` → ``capture_ai_response`` 순서라, 브랜치가
        성공한 **뒤** 도는 기억 저장이 터지면 ``tools_used``까지 가득 찬 채로 본문만
        오류 문자열이 된다. 브랜치는 실제로 성공했으므로 성공 여부로는 걸러지지 않는다.

        본문을 함께 기록해 두면 최상위가 "이 텍스트가 브랜치가 만든 답변인가"를 직접
        물어볼 수 있다 — vendor의 실패 문자열 형태를 알 필요가 없으므로 #273이
        없애려던 vendor 결합이 되살아나지 않는다.
        """
        self.branch_answer = text

    def record_ledger_tools(self, ledger: "DataToolLedger") -> None:
        """*ledger*의 도구를 첫 호출 순서대로 병합한다 (도구명 기준 중복 제거).

        게이트 재시도(``_run_with_gate``의 2차 시도)는 원장을 새로 만들므로 이
        메서드가 여러 번 호출된다. 중복 제거는 "한 번이라도 실행된 도구" 집합을
        순서를 유지한 채 만든다.

        같은 도구를 여러 번 호출했다면 **가장 좋은 결과**를 남긴다
        (:func:`_tool_use_rank` 기준: 실패 < 빈 결과 < 데이터 있음). 재시도 루프에서
        흔한 경로다 — 1차에서 실패하거나 빈 결과였다가 2차에서 데이터를 얻으면,
        결국 그 데이터를 얻은 것이므로 각주도 그렇게 말해야 한다. 반대 방향으로
        내리지는 않는다: 한 번 얻은 데이터가 뒤 호출의 실패로 사라지지 않는다.
        """
        for record in ledger.records:
            fresh = _tool_use_from_record(record)
            for index, seen in enumerate(self.tools_used):
                if seen.name != fresh.name:
                    continue
                if _tool_use_rank(fresh) > _tool_use_rank(seen):
                    self.tools_used[index] = fresh
                break
            else:
                self.tools_used.append(fresh)


REASONING_TRACE: ContextVar["ReasoningTrace | None"] = ContextVar(
    "finus_reasoning_trace", default=None
)

_ERROR_JSON_PREFIX_RE = re.compile(r'^\s*\{"error"')

# 각 MCP 서버가 조회 결과가 없을 때 출력하는 리터럴 부분문자열.
# 추측한 휴리스틱이 아니라 MCP 소스 코드에서 직접 추출한 문자열이다.
#   mcp-trading/index.js  formatDailyOrderCcldReport 함수:
#     "- 결과: 해당 조건의 주문·체결이 없습니다."
#   mcp-news/index.js     fetchMarketNews 함수:
#     `'${stockName}'에 대한 뉴스를 찾지 못했습니다.`  (종목명 부분 제외한 고정 접미사)
#   mcp-dart/index.js     formatEarningsReport 함수:
#     "- 조회 결과: OpenDART 재무제표 데이터가 없습니다."
_EMPTY_RESULT_LITERALS: dict[str, str] = {
    "finus_mcp_trading_today_orders": "해당 조건의 주문·체결이 없습니다.",
    "finus_market_news": "에 대한 뉴스를 찾지 못했습니다.",
    "finus_earnings_report": "OpenDART 재무제표 데이터가 없습니다.",
}


def _has_empty_result(tool_name: str, stripped: str) -> bool:
    """Return True when the tool response is successful but contains no data rows.

    Detection priority:

    1. Structural (JSON): 모든 도구에 적용된다. JSON 파싱에 성공하고 빈 컨테이너
       (null, 빈 list/dict/str)이면 즉시 True를 반환한다. 그 외에는 다음 분기로
       fall-through한다 — 응답이 JSON으로 감싸인 뒤에도 리터럴 탐지가 유효하게
       유지하기 위해서다.

    2. finus_mcp_trading_balance_rlz_pl: balance-rlz-pl-report.js emits
       "보유 종목이 없습니다." for an empty rows set.  When KIS also returns a
       summary object, "[계좌 집계]" is appended with real account numbers, so
       only the no-summary path is truly empty.

    3. Documented MCP literals (_EMPTY_RESULT_LITERALS): exact substrings from
       each MCP server's source code — not guessed patterns.

    Tools not covered here return False (conservative: do not block unknown
    formats) to avoid false positives on variable-shape responses such as
    finus_account_balance (remote KIS, api_type-dependent) or
    finus_disclosure_signal (all-empty detection would require multi-section
    parsing) or finus_mcp_trading_get_balance (always contains account-level
    summary numbers even when no individual holdings are held).
    """
    # --- 1. Structural: JSON empty container or null ---
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if parsed is None or (isinstance(parsed, (list, dict, str)) and len(parsed) == 0):
            return True
        # 비어있지 않은 JSON이어도 리터럴 검사는 계속한다 — 응답이 JSON으로
        # 감싸이는 순간 리터럴 탐지가 조용히 죽는 것을 막는다.

    # --- 2. balance_rlz_pl: empty holdings without account summary ---
    # balance-rlz-pl-report.js formatBalanceRlzPlReport rows.length===0 branch:
    #   always emits "보유 종목이 없습니다."
    #   appends "[계좌 집계]" block only when summary object exists (real numbers).
    if tool_name == "finus_mcp_trading_balance_rlz_pl":
        return "보유 종목이 없습니다." in stripped and "[계좌 집계]" not in stripped

    # --- 3. Documented literal markers from MCP source code ---
    literal = _EMPTY_RESULT_LITERALS.get(tool_name)
    if literal is not None:
        return literal in stripped

    return False


def _record_to_ledger(tool_name: str, result: str) -> None:
    """Record a completed data-tool call into the current context's ledger.

    Logs ERROR and returns without recording when the ledger box is None —
    this happens when a tool runs in a threadpool that did not inherit the
    ContextVar (e.g., outside a run_subagent context).  The resulting empty
    ledger causes the gate to trip for any numeric claim, which is the
    correct conservative behaviour.

    ``tool_name`` leaves this service: backend renders it in the reasoning
    footnote through ``TOOL_LABELS`` (backend/presentation.py, #260).
    Renaming a tool without updating that map degrades gracefully — the raw
    internal name shows up instead of the Korean label — so the mismatch is
    caught by backend/tests/test_label_drift.py in CI (#282). Keep the name a
    literal or a module-level constant; that check reads it statically.
    """
    ledger = DATA_TOOL_LEDGER.get()
    if ledger is None:
        logger.error(
            "DATA_TOOL_LEDGER not set when %s completed — tool enforcement cannot "
            "track this call. Tool may have run outside a run_subagent context or "
            "in a threadpool that does not propagate contextvars.",
            tool_name,
        )
        return
    stripped = result.strip()
    ok = bool(stripped) and not _ERROR_JSON_PREFIX_RE.match(stripped)
    is_read = ok and tool_name not in _SIDE_EFFECT_TOOLS
    # empty: ok=True였지만 결과 집합이 비어 있음(#209 빈 결과 축)
    is_empty = is_read and _has_empty_result(tool_name, stripped)
    # produced_rows 조건:
    # 1. ok — 오류 응답이 아님
    # 2. 쓰기 도구가 아님 (_SIDE_EFFECT_TOOLS) — 일지 저장 성공이 게이트를 끄지 않도록
    # 3. 빈 결과가 아님 (_has_empty_result) — 성공했지만 데이터가 없는 응답(#209)
    ledger.record(
        tool_name,
        ok=ok,
        produced_rows=is_read and not is_empty,
        empty=is_empty,
    )


def _record_and_mask(tool_name: str, result: str) -> str:
    """원장에는 **원문**을 기록하고, 에이전트에게는 마스킹된 결과를 돌려준다 (#231).

    도구 하나가 결과를 반환하는 자리는 예외 없이 여기를 지난다 — 그래야 새 도구가
    추가될 때 마스킹만 빠지는 일이 생기지 않는다. ``finus_api.py``에 도구 결과를
    돌려주면서 이 함수를 거치지 않는 경로가 생기면
    ``finus_nat/tests/test_pii_guard.py``의 AST 회귀 가드가 잡는다.

    **원장에는 원문을 넣는다.** ``_has_empty_result``는 MCP 소스에서 그대로 가져온
    리터럴("해당 조건의 주문·체결이 없습니다.", "보유 종목이 없습니다.", "[계좌 집계]")과
    JSON 구조를 보고 빈 결과를 판정한다(#209). 마스킹된 텍스트를 넘기면 그 판정이
    마스킹 정규식의 사정에 묶인다 — 원장은 프로세스 안에만 남고 마스킹은 LLM
    컨텍스트로 나가는 값에만 필요하므로, 둘을 갈라 두는 편이 결합이 적다.

    **지금은 두 순서의 결과가 같다.** 현재 리터럴 중 마스킹 정규식에 걸리는 것이
    하나도 없고(금액·수량·10자리 숫자 없음), 마스킹은 JSON 구조와 비어 있음 여부를
    바꾸지 않기 때문이다. 즉 이 순서는 오늘의 버그를 고치는 것이 아니라 **앞으로
    갈라질 여지를 막는** 선택이다. 그 전제가 깨지는 순간
    (``_EMPTY_RESULT_LITERALS``에 "…원"이나 "N주"가 들어오는 순간)을
    ``test_pii_guard.py::test_empty_result_literals_survive_masking``이 잡는다.

    마스킹 대상이 아닌 도구(뉴스·공시·실적·일지 저장)에서는
    :func:`~nat_finus_nat.pii_guard.mask_tool_result`가 원문을 그대로 돌려주므로
    이 함수는 종전과 동일하게 동작한다.
    """
    _record_to_ledger(tool_name, result)
    return mask_tool_result(tool_name, result)


# Remote MCP / doc 한도
_MCP_DOC_MAX_CHARS = 14_000 #텍스트 최대 길이
_MCP_LIST_TOOLS_GRACE_SEC = 10.0 #툴 호출 시 timeout 방지
_SSE_CONNECT_CAP = 60.0 # SSE 연결 타임아웃
_SSE_CONNECT_FLOOR = 5.0 # SSE 연결 타임아웃 하한

_MCP_ENV_ALLOWED_KEYS = frozenset({
    "PATH",
    "NODE_ENV",
    "NODE_OPTIONS",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "DART_API_KEY",
})
# backend/config.py의 _MCP_ENV_ALLOWED_PREFIXES와 같은 규칙. 한쪽만 바꾸면
# 같은 .env로 backend 경로와 NAT 경로의 동작이 갈린다(#130). KIS_API_KEY 등
# 개별 KIS_* 키 나열은 이제 KIS_ 접두사에 포함되므로 위 목록에서 제거했다.
# KIS_만 추가하면 자식 네임스페이스(KIS_)가 양쪽에서 일치한다. FINUS_ 범위는
# 의도적으로 다르다 — backend는 FINUS_KIS_로 좁히고, NAT의 FINUS_는 이 PR
# 이전부터 있던 기존 범위라 이번 변경의 대상이 아니다.
_MCP_ENV_ALLOWED_PREFIXES = ("FIN_US_", "FINUS_", "KIS_")

McpCallArguments: TypeAlias = dict[str, Any]

# 에러 응답 JSON 문자열을 일관된 포맷으로 변환합니다.
def _err_json(error: str, **extra: Any) -> str:
    return json.dumps({"error": error, **extra}, ensure_ascii=False)

# ReAct 프롬프트(ChatPromptTemplate)에서 ``{``/``}``가 변수로 해석되지 않게 이스케이프합니다.
def _langchain_escape_braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")

# vendor_root를 절대경로로 정규화합니다(없으면 기본 탐색).
def finus_nat_example_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def fin_us_vendor_root() -> Path:
    env = os.environ.get("FINUS_VENDOR_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    integrate_root = finus_nat_example_root().parent
    flat = integrate_root
    if ((flat / "mcp-news" / "index.js").is_file()
            and (flat / "mcp-trading" / "index.js").is_file()):
        return flat
    fin_us_home = integrate_root / "fin-us"
    if ((fin_us_home / "mcp-news" / "index.js").is_file()
            and (fin_us_home / "mcp-trading" / "index.js").is_file()):
        return fin_us_home
    return finus_nat_example_root() / "vendor" / "fin_us"


def _resolve_vendor_root(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return fin_us_vendor_root()


# stdio MCP 서버에 필요한 Node 의존성 설치 여부를 확인합니다.
def _node_deps_ready(server_dir: Path) -> bool:
    return (server_dir / "node_modules" / "@modelcontextprotocol" / "sdk").is_dir()


def _mcp_child_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _MCP_ENV_ALLOWED_KEYS or key.startswith(_MCP_ENV_ALLOWED_PREFIXES)
    }


# MCP call_tool 결과의 첫 텍스트 블록을 추출합니다(stdio/remote 공용).
def _mcp_call_tool_first_text(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    if not blocks:
        return ""
    block0 = blocks[0]
    return getattr(block0, "text", str(block0))


async def _run_mcp_timed(inner, *, tool_name: str, timeout_sec: float) -> str:
    """Run MCP inner coroutine; return tool text or JSON error (never raise to the agent)."""
    try:
        return await asyncio.wait_for(inner(), timeout=timeout_sec)
    except TimeoutError:
        return _err_json("mcp_timeout", tool=tool_name)
    except asyncio.CancelledError:
        raise
    except BaseExceptionGroup as exc:
        sub = exc.exceptions[0] if exc.exceptions else exc
        return _err_json("mcp_call_failed", tool=tool_name, detail=str(sub))
    except Exception as exc:  # noqa: BLE001
        return _err_json("mcp_call_failed", tool=tool_name, detail=str(exc))


# 작업 timeout에서 SSE 연결 타임아웃을 적당히 산출합니다.
def _sse_connect_timeout(operation_timeout: float) -> float:
    return min(_SSE_CONNECT_CAP, max(_SSE_CONNECT_FLOOR, operation_timeout * 0.25))


# Kis 계열 원격 MCP 한 곳에 대한 초기화된 ClientSession을 컨텍스트로 제공합니다.
@asynccontextmanager
async def _remote_mcp_session(
    *,
    transport: Literal["sse", "streamable-http"],
    url: str,
    operation_timeout: float,
):
    conn = _sse_connect_timeout(operation_timeout)
    if transport == "sse":
        async with sse_client(url=url, timeout=conn, sse_read_timeout=operation_timeout) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return
    async with httpx.AsyncClient() as http_client:
        async with streamable_http_client(url=url, http_client=http_client) as (read, write, _get_sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


# stdio MCP(예: mcp-news) 도구 하나를 호출하고 결과 텍스트(또는 에러 JSON)를 반환합니다.
async def _mcp_call_tool(
    *,
    vendor_root: Path,
    subdir: str,
    tool_name: str,
    arguments: McpCallArguments,
    timeout_sec: float,
) -> str:
    server_dir = (vendor_root / subdir).resolve()
    script = (server_dir / "index.js").resolve()
    if not script.is_file():
        return _err_json(
            "mcp_server_script_missing",
            path=str(script),
            hint=(f"Expected MCP under {vendor_root}; run scripts/install_fin_us_mcp.sh "
                  "or set FINUS_VENDOR_ROOT."),
        )
    if not _node_deps_ready(server_dir):
        news = vendor_root / "mcp-news"
        trade = vendor_root / "mcp-trading"
        dart = vendor_root / "mcp-dart"
        return _err_json(
            "mcp_node_dependencies_missing",
            path=str(server_dir),
            detail="ERR_MODULE_NOT_FOUND @modelcontextprotocol/sdk — node_modules not installed.",
            hint=(
                f"Run scripts/install_fin_us_mcp.sh from repo root, or: "
                f"cd {news} && npm ci; cd {trade} && npm ci; cd {dart} && npm ci"
            ),
        )

    params = StdioServerParameters(
        command="node",
        args=[str(script)],
        env=_mcp_child_env(),
        cwd=str(server_dir),
    )

    # 실제 stdio 세션 안에서 단일 tool 호출을 수행하는 inner.
    async def _inner() -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return _mcp_call_tool_first_text(await session.call_tool(tool_name, arguments))

    return await _run_mcp_timed(_inner, tool_name=tool_name, timeout_sec=timeout_sec)


# 원격 MCP(SSE/streamable-http) 도구 하나를 호출하고 텍스트(또는 에러 JSON)를 반환합니다.
async def _mcp_call_tool_remote(
    *,
    transport: Literal["sse", "streamable-http"],
    url: str,
    tool_name: str,
    arguments: McpCallArguments,
    timeout_sec: float,
) -> str:
    if not url.strip():
        return _err_json(
            "mcp_url_empty",
            tool=tool_name,
            hint="Set mcp_url on finus_account_balance (Kis Trading MCP) config.",
        )

    # 실제 원격 세션을 열고 단일 tool 호출을 수행하는 inner.
    async def _inner() -> str:
        async with _remote_mcp_session(transport=transport, url=url, operation_timeout=timeout_sec) as session:
            return _mcp_call_tool_first_text(await session.call_tool(tool_name, arguments))

    return await _run_mcp_timed(_inner, tool_name=tool_name, timeout_sec=timeout_sec)


# mcp-news stdio MCP에서 종목 단위 조회 도구를 공통 호출합니다.
async def _mcp_news_stock(
    *,
    vendor_root: str | None,
    timeout_sec: float,
    tool_name: str,
    stock_name: str,
) -> str:
    return await _mcp_call_tool(
        vendor_root=_resolve_vendor_root(vendor_root),
        subdir="mcp-news",
        tool_name=tool_name,
        arguments={"stock_name": stock_name},
        timeout_sec=timeout_sec,
    )


async def _mcp_dart_stock(
    *,
    vendor_root: str | None,
    timeout_sec: float,
    stock_name: str,
) -> str:
    return await _mcp_call_tool(
        vendor_root=_resolve_vendor_root(vendor_root),
        subdir="mcp-dart",
        tool_name="get_disclosure_signal",
        arguments={"stock_name": stock_name},
        timeout_sec=timeout_sec,
    )


async def _mcp_dart_earnings_stock(
    *,
    vendor_root: str | None,
    timeout_sec: float,
    stock_name: str,
    period: str | None = None,
) -> str:
    arguments = {"stock_name": stock_name}
    if period:
        arguments["period"] = period
    return await _mcp_call_tool(
        vendor_root=_resolve_vendor_root(vendor_root),
        subdir="mcp-dart",
        tool_name="get_earnings_report",
        arguments=arguments,
        timeout_sec=timeout_sec,
    )


async def _mcp_trading_call(
    *,
    vendor_root: str | None,
    timeout_sec: float,
    tool_name: str,
    arguments: McpCallArguments,
) -> str:
    return await _mcp_call_tool(
        vendor_root=_resolve_vendor_root(vendor_root),
        subdir="mcp-trading",
        tool_name=tool_name,
        arguments=arguments,
        timeout_sec=timeout_sec,
    )


# =========== registered NAT components ==========

class _FinusMcpStdioConfig(FunctionBaseConfig):
    vendor_root: str | None = Field(default=None, description="Override parent dir containing MCP subdirectory (stdio).")
    timeout_sec: float = Field(default=120.0, ge=5.0, le=600.0)


class FinusMarketNewsConfig(_FinusMcpStdioConfig, name="finus_market_news"):
    pass


class FinusDisclosureSignalConfig(FinusMarketNewsConfig, name="finus_disclosure_signal"):
    """mcp-dart stdio MCP — OpenDART 지분공시 signal."""


class FinusEarningsReportConfig(FinusMarketNewsConfig, name="finus_earnings_report"):
    """mcp-dart stdio MCP — OpenDART 실적 리포트."""


class FinusMcpTradingConfig(_FinusMcpStdioConfig, name="finus_mcp_trading_base"):
    """fin-us/mcp-trading stdio MCP 공통 설정 (vendor_root, timeout_sec)."""


class FinusMcpTradingTodayOrdersConfig(FinusMcpTradingConfig, name="finus_mcp_trading_today_orders"):
    """mcp-trading ``get_today_daily_orders`` — 당일 주문·체결 전체 조회."""


class FinusMcpTradingBalanceRlzPlConfig(FinusMcpTradingConfig, name="finus_mcp_trading_balance_rlz_pl"):
    """mcp-trading ``get_balance_rlz_pl`` — 잔고·실현손익 조회 (실전 계좌)."""


class FinusMcpTradingGetBalanceConfig(FinusMcpTradingConfig, name="finus_mcp_trading_get_balance"):
    """mcp-trading ``get_balance`` — 계좌 잔고·보유종목 요약 (inquire-balance, 연속조회)."""


class FinusSaveDiaryConfig(FunctionBaseConfig, name="finus_save_diary"):
    """Fin-Us backend ``POST /api/v1/db/diary`` — 매매일지 저장."""

    backend_url: str = Field(
        default="",
        description="Backend base URL without path. Falls back to FINUS_BACKEND_URL, then http://127.0.0.1:8000.",
    )
    timeout_sec: float = Field(default=60.0, ge=5.0, le=300.0)


class FinusListDiariesConfig(FunctionBaseConfig, name="finus_list_diaries"):
    """Fin-Us backend ``GET /api/v1/db/diary`` — 저장된 매매일지 목록 조회."""

    backend_url: str = Field(
        default="",
        description="Backend base URL without path. Falls back to FINUS_BACKEND_URL, then http://127.0.0.1:8000.",
    )
    timeout_sec: float = Field(default=60.0, ge=5.0, le=300.0)


def _finus_backend_base_url(config_url: str) -> str:
    return (config_url or os.getenv("FINUS_BACKEND_URL") or "http://127.0.0.1:8000").strip().rstrip("/")


def _finus_backend_headers() -> dict[str, str]:
    """backend 호출에 실을 API 키 헤더 (#266 2단계).

    NAT는 컴포즈 내부에서 backend의 `/api/v1/db/diary`를 부르는 **비브라우저**
    클라이언트다. backend가 인증을 켠 배포(`FINUS_API_KEY` 설정)에서 이 헤더가 없으면
    매매일지 저장·조회가 401로 떨어진다.

    키가 없으면 헤더 자체를 붙이지 않는다. 빈 값을 보내면 backend는 그것을 "틀린 키"로
    보므로, 인증이 꺼진 배포에서까지 깨진다. 헤더 이름은 backend/main.py의
    API_KEY_HEADER와 같아야 한다 — 어긋나면 인증을 켠 날에야 드러난다.
    """
    key = (os.getenv("FINUS_API_KEY") or "").strip()
    return {"X-API-Key": key} if key else {}


# ========== Kis Trading remote MCP ==========
class FinusReactToolInput(BaseModel):
    """ReAct Action Input — 문자열 JSON·Kis ``params`` 래핑을 허용합니다."""

    tool_input: str | dict[str, Any] | None = Field(default=None, description="레거시 JSON 한 줄.")
    tool_name: str | None = Field(default=None, description="Kis Trading MCP용(무시 가능).")
    api_type: str | None = Field(default=None, description="Kis Trading MCP용(무시 가능).")
    params: dict[str, Any] | None = Field(default=None, description="Kis 형식이면 여기 필드를 사용.")

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _coerce_whole_string_payload(cls, data: Any) -> Any:
        if isinstance(data, str):
            if not data.strip():
                return {}
            parsed = _parse_leading_json_object(data)
            if parsed is None:
                raise ValueError(
                    "Action Input에는 JSON 객체 한 덩어리만 넣으세요. "
                    "Kis ``tool_name``/``api_type`` 대신 도구 스키마 필드만 사용하세요."
                )
            data = parsed
        if isinstance(data, dict):
            nested = data.get("params")
            if isinstance(nested, dict):
                merged = dict(nested)
                for key, value in data.items():
                    if key in ("tool_name", "api_type", "params", "tool_input"):
                        continue
                    if value is not None and key not in merged:
                        merged[key] = value
                return merged
        return data


class FinusMcpTradingTodayOrdersInput(FinusReactToolInput):
    trade_date: str = Field(default="", description="YYYYMMDD. 생략 시 당일(KST).")
    stock_name: str = Field(default="", description="특정 종목만 조회할 때.")
    ccld_dvsn: str = Field(default="00", description="00 전체 / 01 체결 / 02 미체결.")
    sll_buy_dvsn: str = Field(default="00", description="00 전체 / 01 매도 / 02 매수.")


class FinusMcpTradingStockNameInput(FinusReactToolInput):
    stock_name: str = Field(default="", description="특정 종목만 조회할 때. 생략 시 전체.")


class FinusSaveDiaryInput(FinusReactToolInput):
    title: str = Field(default="", description="일지 제목.")
    content: str = Field(default="", description="일지 본문.")


class FinusListDiariesInput(FinusReactToolInput):
    pass


class FinusMcpTradingGetBalanceInput(FinusReactToolInput):
    pass


def _parse_leading_json_object(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t:
        return None
    i = t.find("{")
    if i < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(t[i:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _finus_react_input_converter(model_cls: type[BaseModel]) -> Callable[[str], BaseModel]:
    """ReAct ``Action Input`` 문자열을 NAT type_converter가 Pydantic 모델로 변환하도록 등록."""

    def _convert(value: str) -> BaseModel:
        return model_cls.model_validate(value)

    return _convert


class KisTradingMcpCallInput(BaseModel):
    """Kis Trading MCP call_tool 입력. 표준 도구는 모두 {api_type, params} (MCP list_tools·각 도구 설명 참고)."""

    tool_name: str | None = Field(
        default=None,
        description=(
            "MCP 도구 이름: domestic_stock, overseas_stock, domestic_bond, domestic_futureoption, "
            "overseas_futureoption, elw, etfetn, auth. 비우면 YAML ``trading_tool_name`` 기본값."
        ),
    )
    tool_input: str | dict[str, Any] | None = Field(
        default="",
        description='레거시: JSON 한 줄 또는 {"api_type","params"} dict.',
    )
    api_type: str | None = Field(default=None, description="TR 식별자(find_api_detail, inquire_price, …).")
    params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "해당 api_type용 params. domestic_stock + inquire_balance 는 서버에서 흔한 필수값을 기본 채움 "
            "(env_dv, inqr_dvsn, afhr_flpr_yn 등). 그래도 오류면 find_api_detail 로 스키마 확인 후 수정."
        ),
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_whole_string_payload(cls, data: Any) -> Any:
        if isinstance(data, str):
            parsed = _parse_leading_json_object(data)
            if parsed is None:
                raise ValueError(
                    "KisTradingMcpCallInput: JSON 객체가 필요합니다. Action Input에는 JSON 한 덩어리만 두고 "
                    "그 뒤에는 줄을 바꾼 뒤 Thought/Action을 이어 쓰세요."
                )
            return parsed
        return data


def _coerce_api_type_params_payload(tool_input: Any, kwargs: dict[str, Any]) -> McpCallArguments | None:
    nested = kwargs.get("tool_input")
    if isinstance(nested, dict) and nested.get("api_type") is not None:
        return dict(nested)
    at_kw, pr_kw = kwargs.get("api_type"), kwargs.get("params")
    if at_kw is not None:
        return {"api_type": at_kw, "params": pr_kw if isinstance(pr_kw, dict) else {}}
    if isinstance(tool_input, dict) and tool_input.get("api_type") is not None:
        return dict(tool_input)
    if isinstance(tool_input, str):
        return _parse_leading_json_object(tool_input)
    return None


# 조회 전용 래퍼(finus_account_balance_readonly)가 허용하는 MCP tool_name 허용 목록 (fail-closed).
# auth는 조회 목적이 없고 인증 상태를 바꿀 수 있어 제외한다.
_READONLY_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    "domestic_stock", "overseas_stock", "domestic_bond",
    "domestic_futureoption", "overseas_futureoption", "elw", "etfetn",
})

# Kis Trading MCP list_tools에 실제로 존재하는 도구 이름 전체 집합.
# readonly 허용 목록 + auth — 전체 권한 래퍼(finus_account_balance)만 auth를 허용한다.
# FinusAccountBalanceConfig._ALLOWED_TOOL_NAMES의 기본값으로 사용한다.
_KIS_TRADING_TOOL_NAMES: frozenset[str] = _READONLY_TOOL_ALLOWLIST | {"auth"}


class FinusAccountBalanceConfig(FunctionBaseConfig, name="finus_account_balance"):
    """원격 Kis Trading MCP — `call_tool` 에 `api_type`·`params` 만 전달.

    `tool_name` 생략 시 YAML `trading_tool_name`. Upstream: `open-trading-api`/`MCP/Kis Trading MCP`.
    """

    # 이 Config가 허용하는 trading_tool_name 집합 — 서브클래스가 좁힐 수 있다.
    # FinusAccountBalanceReadonlyConfig는 _READONLY_TOOL_ALLOWLIST로 오버라이드해 auth를 제외한다.
    _ALLOWED_TOOL_NAMES: ClassVar[frozenset[str]] = _KIS_TRADING_TOOL_NAMES

    timeout_sec: float = Field(default=180.0, ge=5.0, le=600.0)
    mcp_transport: Literal["sse", "streamable-http"] = Field(
        default="sse",
        description="Kis Trading MCP Docker: 호스트 포트 매핑에서는 보통 host.docker.internal:3300/sse.",
    )
    mcp_url: str = Field(
        default="http://host.docker.internal:3300/sse",
        description="MCP URL. 호스트 포트 매핑 시 ``FINUS_KIS_TRADING_MCP_URL`` 등으로 덮어씁니다.",
    )
    trading_tool_name: str = Field(
        default="domestic_stock",
        description=(
            "``tool_name`` 미지정 시 사용할 MCP 도구 기본값. domestic_stock, overseas_stock, auth 등 "
            "``list_tools`` 이름과 동일해야 합니다."
        ),
    )

    @model_validator(mode="after")
    def _validate_trading_tool_name(self) -> "FinusAccountBalanceConfig":
        """FINUS_KIS_TRADING_TOOL_NAME 환경변수 등으로 잘못된 값이 들어올 때 설정 시점에 감지한다.

        허용 목록 밖의 값이면 Config 로드 시점에 ValidationError를 발생시켜 조기에 알린다.
        서브클래스는 _ALLOWED_TOOL_NAMES를 오버라이드해 허용 범위를 좁힌다
        (예: readonly 래퍼는 auth를 제외한 7종만 허용).
        빈 문자열은 런타임에 ``kis_tool_name_missing`` 에러로 처리되므로 여기서는 허용한다.
        """
        name = (self.trading_tool_name or "").strip().lower()
        if name and name not in self._ALLOWED_TOOL_NAMES:
            raise ValueError(
                f"trading_tool_name={self.trading_tool_name!r}는 이 래퍼가 허용하지 않는 "
                f"Kis MCP 도구 이름입니다. 허용 목록: {sorted(self._ALLOWED_TOOL_NAMES)}. "
                f"MCP 서버에 새 도구가 추가되었다면 finus_api.py의 _KIS_TRADING_TOOL_NAMES에 등록하세요."
            )
        return self


def _adjust_domestic_stock_params(api_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Kis Trading MCP / KIS TR별로 흔한 잘못된 인자만 보정(그 외는 MCP·공식 문서 그대로)."""
    out = dict(params)
    api = (api_type or "").strip().lower()
    if api == "inquire_balance":
        out.setdefault("env_dv", "real")
        out.setdefault("inqr_dvsn", "01")
        out.setdefault("afhr_flpr_yn", "N")
        out.setdefault("unpr_dvsn", "01")
        out.setdefault("fund_sttl_icld_yn", "N")
        out.setdefault("fncg_amt_auto_rdpt_yn", "N")
        out.setdefault("prcs_dvsn", "01")
    if api == "inquire_account_balance":
        # MCP Python 래퍼가 env_dv 키워드를 받지 않음(TypeError).
        out.pop("env_dv", None)
        # 공식 예제는 inqr_dvsn_1 기본 "". LLM이 자주 넣는 "00"은 OPSQ2002 INVALID INPUT_FILED_SIZE 유발.
        if str(out.get("inqr_dvsn_1", "")).strip() == "00":
            out["inqr_dvsn_1"] = ""
    return out


def _prepare_kis_trading_mcp_call(
    inp: KisTradingMcpCallInput,
    config: FinusAccountBalanceConfig,
) -> str | tuple[str, McpCallArguments]:
    kw: dict[str, Any] = {}
    if inp.api_type is not None:
        kw["api_type"] = inp.api_type
    if inp.params is not None:
        kw["params"] = inp.params
    ti = inp.tool_input if inp.tool_input is not None else ""
    blob = _coerce_api_type_params_payload(ti, kw)
    if not blob:
        return _err_json(
            "kis_mcp_missing_arguments",
            hint='Provide api_type and params, e.g. {"api_type":"find_api_detail","params":{"api_type":"inquire_balance"}}',
        )
    blob = dict(blob)
    tool_from_blob = blob.pop("tool_name", None)
    # tool_name을 소문자로 정규화한다 (#220).
    # _is_readonly_tool_name·_adjust_domestic_stock_params 판정뿐 아니라
    # _mcp_call_tool_remote에 전달되는 값도 MCP 서버가 기대하는 소문자여야 한다.
    tool_name = (
        (inp.tool_name or "").strip().lower()
        or (str(tool_from_blob).strip().lower() if tool_from_blob is not None else "")
        or (config.trading_tool_name or "").strip().lower()
    )
    if not tool_name:
        return _err_json(
            "kis_tool_name_missing",
            hint="Pass tool_name in Action Input or set trading_tool_name in YAML (see MCP list_tools).",
        )
    api_type = blob.get("api_type")
    if api_type is None or str(api_type).strip() == "":
        return _err_json("kis_mcp_missing_api_type", tool=tool_name, blob_keys=list(blob.keys()))
    raw_params = blob.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if tool_name == "domestic_stock":
        params = _adjust_domestic_stock_params(str(api_type).strip(), params)
    return tool_name, {"api_type": str(api_type).strip(), "params": params}


# KIS Trading MCP 도구 가이드
_KIS_TRADING_TOOL_GUIDE = """
### Fin-Us KIS 호출 규칙
- ReAct 형식: `Action Input:` 한 줄에는 JSON만 두세요. JSON 뒤에 같은 줄·바로 다음 줄에 설명을 붙이면 도구 입력이 깨질 수 있습니다. 설명은 Observation 이후 또는 `Final Answer:` 에 쓰세요.
- 국내 주식 기본 tool_name은 `domestic_stock`입니다.
- 잔고/포트폴리오 `api_type="inquire_balance"`: 서버가 흔한 필수값을 기본 채웁니다. 직접 넣을 때 예시는
  `{"env_dv":"real","inqr_dvsn":"01","afhr_flpr_yn":"N","unpr_dvsn":"01","fund_sttl_icld_yn":"N","fncg_amt_auto_rdpt_yn":"N","prcs_dvsn":"01"}` 입니다.
- 투자계좌자산현황 `api_type="inquire_account_balance"`는 MCP 래퍼 제약상 `env_dv`를 넣지 마세요. `inqr_dvsn_1`은 기본 `""`이며 `"00"`을 추측해서 넣지 마세요.
- 현재가 조회 `api_type="inquire_price"`의 국내 주식 기본값은 `{"fid_cond_mrkt_div_code":"J","fid_input_iscd":"종목코드","env_dv":"real"}` 입니다.
- 알 수 없는 필드, INVALID INPUT, 필드 크기 오류, 필수 파라미터 오류가 나오면 실패로 끝내지 말고 `api_type="find_api_detail"`을 호출해 해당 `api_type`의 필드를 확인한 뒤 같은 사용자 요청을 다시 실행하세요.
- 성공 조건은 사용자가 요청한 데이터 조회가 정상 Observation으로 돌아오고, 그 Observation을 근거로 최종 답변하는 것입니다.
""".strip()


# ---- 조회 전용 KIS Trading MCP 래퍼 (#66) ----

# #205/#209 원장 판정 키 — finus_account_balance(전체 권한)와 readonly 래퍼 양쪽이 공유한다.
# 변경 시 #205/#209 원장 판정 로직과 함께 반드시 동기화할 것.
_KIS_BALANCE_LEDGER_NAME: str = "finus_account_balance"

# 허용 목록(allowlist) 방식 — fail-closed: 목록에 없는 api_type은 기본 차단.
#
# 설계 근거:
#   KIS TR 이름 규칙: 조회 계열은 예외 없이 ``inquire_`` 또는 ``search_`` 로 시작한다.
#   스키마 조회(find_api_detail)도 읽기 전용이다 — 에이전트가 이 도구로 TR 스키마를
#   발견하더라도, 실제 호출 시 api_type이 이 허용 목록을 통과해야 하므로 이중 차단이 된다.
#   결과적으로 원격 MCP 서버가 새 TR을 추가해도 허용 목록에 명시하기 전까지는 차단된다.
#
# 허용 접두사:
#   inquire_  — 잔고·시세·체결·투자자·일봉·분봉 등 모든 조회 TR
#   search_   — 종목 코드 검색 등 검색 TR
#
# 허용 정확 값:
#   find_api_detail — TR 스키마 조회 (에이전트 자기교정 루프용; 읽기 전용)
#
# 운영자 추가 방법: 새 조회 TR이 위 접두사 패턴을 벗어나면
# _READONLY_API_ALLOWLIST_EXACT에 추가하거나 접두사를 확장하세요.
_READONLY_API_ALLOWLIST_PREFIXES: tuple[str, ...] = ("inquire_", "search_")
_READONLY_API_ALLOWLIST_EXACT: frozenset[str] = frozenset({"find_api_detail"})

# _READONLY_TOOL_ALLOWLIST는 _KIS_TRADING_TOOL_NAMES와 함께 위에서 정의되어 있다.
# (FinusAccountBalanceConfig 클래스 정의 직전 — 단일 출처 유지를 위해 이동)


def _is_readonly_api_type(api_type: str) -> bool:
    """Return True when *api_type* is a permitted read-only KIS Trading API (allowlist, fail-closed).

    Allowed:
    - ``inquire_*`` prefix — balance, price, chart, investor, and all other inquiry TRs
    - ``search_*`` prefix — stock code and other search TRs
    - ``find_api_detail`` — TR schema lookup (safe: discovering schemas does not execute orders)

    Any api_type not in the above list returns False, including all order-execution types
    (``order_cash``, ``order_sell``, ``modify_order``, ``cancel_order``, novel future types, …).
    """
    lower = (api_type or "").strip().lower()
    return (
        any(lower.startswith(p) for p in _READONLY_API_ALLOWLIST_PREFIXES)
        or lower in _READONLY_API_ALLOWLIST_EXACT
    )


def _is_readonly_tool_name(tool_name: str) -> bool:
    """Return True when *tool_name* is in the readonly asset-class allowlist (fail-closed).

    Allowed: domestic_stock, overseas_stock, domestic_bond, domestic_futureoption,
    overseas_futureoption, elw, etfetn.

    Excluded: auth (인증 상태를 바꿀 수 있어 조회 전용 래퍼에서 허용하지 않는다).
    Any unknown tool_name returns False.
    """
    return (tool_name or "").strip().lower() in _READONLY_TOOL_ALLOWLIST


# ---- 두 KIS 래퍼(전체/readonly) 공통 헬퍼 (#220) ----

async def _call_kis_mcp_and_record(
    tool_name: str,
    arguments: McpCallArguments,
    config: FinusAccountBalanceConfig,
) -> str:
    """원격 MCP 호출 후 원장에 기록한다 — finus_account_balance·readonly 공유.

    두 래퍼가 개별로 _mcp_call_tool_remote + _record_to_ledger(_KIS_BALANCE_LEDGER_NAME)를
    중복 구현하는 것을 이 헬퍼 한 곳으로 모은다.
    """
    result = await _mcp_call_tool_remote(
        transport=config.mcp_transport,
        url=config.mcp_url,
        tool_name=tool_name,
        arguments=arguments,
        timeout_sec=config.timeout_sec,
    )
    return _record_and_mask(_KIS_BALANCE_LEDGER_NAME, result)


async def _build_kis_mcp_description(doc: str, config: FinusAccountBalanceConfig) -> str:
    """description 문자열 조립 — finus_account_balance·readonly 공유 (#220).

    doc + _KIS_TRADING_TOOL_GUIDE + (원격 MCP list_tools 문서) 를 합쳐
    LangChain 중괄호 이스케이프 적용 후 반환한다.
    """
    base = doc.strip()
    remote = await _fetch_mcp_tool_documentation(config)
    merged = (
        f"{base}\n\n{_KIS_TRADING_TOOL_GUIDE}\n\n{remote}"
        if remote
        else f"{base}\n\n{_KIS_TRADING_TOOL_GUIDE}"
    )
    return _langchain_escape_braces(merged)


class FinusAccountBalanceReadonlyConfig(FinusAccountBalanceConfig, name="finus_account_balance_readonly"):
    """finus_account_balance 조회 전용 래퍼 — allowlist 방식 api_type 차단 (#66).

    비-trading 에이전트(news/recommend/strategy/diary)가 잔고·시세 조회 능력은 유지하되
    허용 목록(inquire_*, search_*, find_api_detail)에 없는 api_type은 fail-closed로 차단한다.
    """

    # readonly 래퍼는 auth를 런타임에 차단하므로, 설정 시점에도 받지 않는다.
    _ALLOWED_TOOL_NAMES: ClassVar[frozenset[str]] = _READONLY_TOOL_ALLOWLIST


@register_function(config_type=FinusAccountBalanceReadonlyConfig)
async def finus_account_balance_readonly(config: FinusAccountBalanceReadonlyConfig, _builder: Builder):

    async def get_account_balance_readonly(inp: KisTradingMcpCallInput) -> str:
        """Kis Trading MCP 조회 전용 래퍼 — tool_name·api_type 허용 목록으로 이중 차단 (#66).

        허용 tool_name: domestic_stock, overseas_stock, domestic_bond, domestic_futureoption,
        overseas_futureoption, elw, etfetn (auth 제외 — 인증 상태 변경 가능).
        허용 api_type(pass-through): inquire_*(잔고·시세·체결 등), search_*, find_api_detail.
        차단(fail-closed): 목록 밖의 tool_name 또는 api_type(order_* 계열 포함).
        """
        prepared = _prepare_kis_trading_mcp_call(inp, config)
        if isinstance(prepared, str):
            return _record_and_mask(_KIS_BALANCE_LEDGER_NAME, prepared)
        tool_name, arguments = prepared
        if not _is_readonly_tool_name(tool_name):
            logger.warning("readonly gate blocked tool_name=%r", tool_name)
            result = _err_json(
                "kis_tool_name_not_allowed_readonly",
                tool_name=tool_name,
                hint=(
                    "이 도구는 조회 전용입니다. "
                    "domestic_stock, overseas_stock, domestic_bond, domestic_futureoption, "
                    "overseas_futureoption, elw, etfetn 중 하나를 사용하세요. "
                    "주문 실행은 이 에이전트의 권한이 아니므로 재시도하지 말고 사용자에게 안내하세요."
                ),
            )
            return _record_and_mask(_KIS_BALANCE_LEDGER_NAME, result)
        api_type = str(arguments.get("api_type", ""))
        if not _is_readonly_api_type(api_type):
            logger.warning(
                "readonly gate blocked api_type=%r (allowlist prefixes: %s, exact: %s)",
                api_type, _READONLY_API_ALLOWLIST_PREFIXES, sorted(_READONLY_API_ALLOWLIST_EXACT),
            )
            result = _err_json(
                "kis_api_type_not_allowed_readonly",
                api_type=api_type,
                hint=(
                    "이 도구는 조회 전용입니다. inquire_*/search_*/find_api_detail 만 사용하세요. "
                    "주문 실행은 이 에이전트의 권한이 아니므로 재시도하지 말고 사용자에게 안내하세요."
                ),
            )
            return _record_and_mask(_KIS_BALANCE_LEDGER_NAME, result)
        return await _call_kis_mcp_and_record(tool_name, arguments, config)

    description = await _build_kis_mcp_description(get_account_balance_readonly.__doc__ or "", config)
    yield FunctionInfo.from_fn(
        get_account_balance_readonly,
        description=description,
        input_schema=KisTradingMcpCallInput,
    )


def _mcp_news_stock_function_info(
    *,
    vendor_root: str | None,
    timeout_sec: float,
    mcp_tool: str,
    fn_doc: str,
    ledger_tool_name: str,  # required — omitting silently disables ledger tracking
) -> FunctionInfo:
    async def by_stock(stock_name: str) -> str:
        result = await _mcp_news_stock(
            vendor_root=vendor_root,
            timeout_sec=timeout_sec,
            tool_name=mcp_tool,
            stock_name=stock_name,
        )
        return _record_and_mask(ledger_tool_name, result)

    by_stock.__doc__ = fn_doc
    return FunctionInfo.from_fn(by_stock, description=fn_doc)


@register_function(config_type=FinusMarketNewsConfig)
async def finus_market_news(config: FinusMarketNewsConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-news stdio MCP: 종목명 기준 최신 시장 뉴스(헤드라인·요약). "
        "stock_name 예: 삼성전자"
    )
    yield _mcp_news_stock_function_info(
        vendor_root=config.vendor_root,
        timeout_sec=config.timeout_sec,
        mcp_tool="get_market_news",
        fn_doc=doc,
        ledger_tool_name="finus_market_news",
    )


@register_function(config_type=FinusDisclosureSignalConfig)
async def finus_disclosure_signal(config: FinusDisclosureSignalConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-dart stdio MCP: OpenDART 공식 API 지분공시 signal "
        "(5% 룰·임원/주요주주). stock_name 예: 삼성전자, 005930. DART_API_KEY 필요."
    )

    async def get_disclosure_signal(stock_name: str) -> str:
        result = await _mcp_dart_stock(
            vendor_root=config.vendor_root,
            timeout_sec=config.timeout_sec,
            stock_name=stock_name,
        )
        return _record_and_mask("finus_disclosure_signal", result)

    get_disclosure_signal.__doc__ = doc
    yield FunctionInfo.from_fn(get_disclosure_signal, description=doc)


@register_function(config_type=FinusEarningsReportConfig)
async def finus_earnings_report(config: FinusEarningsReportConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-dart stdio MCP: OpenDART 공식 API 실적 리포트 "
        "(매출·영업이익·순이익 YoY). stock_name 예: 삼성전자, 005930. "
        "period 선택 예: 2025Q1, 2025Q2, 2025Q3, 2025FY. DART_API_KEY 필요."
    )

    async def get_earnings_report(stock_name: str, period: str | None = None) -> str:
        result = await _mcp_dart_earnings_stock(
            vendor_root=config.vendor_root,
            timeout_sec=config.timeout_sec,
            stock_name=stock_name,
            period=period,
        )
        return _record_and_mask("finus_earnings_report", result)

    get_earnings_report.__doc__ = doc
    yield FunctionInfo.from_fn(get_earnings_report, description=doc)


@register_function(config_type=FinusMcpTradingTodayOrdersConfig)
async def finus_mcp_trading_today_orders(config: FinusMcpTradingTodayOrdersConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-trading stdio MCP: 당일(KST) 국내주식 주문·체결 내역 전체 조회 "
        "(get_today_daily_orders, inquire-daily-ccld v1_국내주식-005, 연속조회 포함). "
        "trade_date(YYYYMMDD), stock_name, ccld_dvsn(00/01/02), sll_buy_dvsn(00/01/02) 선택."
    )

    async def get_today_daily_orders(inp: FinusMcpTradingTodayOrdersInput) -> str:
        arguments: McpCallArguments = {}
        if inp.trade_date.strip():
            arguments["trade_date"] = inp.trade_date.strip()
        if inp.stock_name.strip():
            arguments["stock_name"] = inp.stock_name.strip()
        if inp.ccld_dvsn.strip():
            arguments["ccld_dvsn"] = inp.ccld_dvsn.strip()
        if inp.sll_buy_dvsn.strip():
            arguments["sll_buy_dvsn"] = inp.sll_buy_dvsn.strip()
        result = await _mcp_trading_call(
            vendor_root=config.vendor_root,
            timeout_sec=config.timeout_sec,
            tool_name="get_today_daily_orders",
            arguments=arguments,
        )
        return _record_and_mask("finus_mcp_trading_today_orders", result)

    get_today_daily_orders.__doc__ = doc
    yield FunctionInfo.from_fn(
        get_today_daily_orders,
        description=doc,
        input_schema=FinusMcpTradingTodayOrdersInput,
        converters=[_finus_react_input_converter(FinusMcpTradingTodayOrdersInput)],
    )


@register_function(config_type=FinusMcpTradingGetBalanceConfig)
async def finus_mcp_trading_get_balance(config: FinusMcpTradingGetBalanceConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-trading stdio MCP: 계좌 잔고·보유종목 요약 조회 "
        "(get_balance, inquire-balance v1_국내주식-006, 연속조회 포함). 파라미터 없음."
    )

    async def get_balance(inp: FinusMcpTradingGetBalanceInput) -> str:  # noqa: ARG001
        result = await _mcp_trading_call(
            vendor_root=config.vendor_root,
            timeout_sec=config.timeout_sec,
            tool_name="get_balance",
            arguments={},
        )
        return _record_and_mask("finus_mcp_trading_get_balance", result)

    get_balance.__doc__ = doc
    yield FunctionInfo.from_fn(
        get_balance,
        description=doc,
        input_schema=FinusMcpTradingGetBalanceInput,
        converters=[_finus_react_input_converter(FinusMcpTradingGetBalanceInput)],
    )


@register_function(config_type=FinusMcpTradingBalanceRlzPlConfig)
async def finus_mcp_trading_balance_rlz_pl(config: FinusMcpTradingBalanceRlzPlConfig, _builder: Builder):
    doc = (
        "Fin-Us mcp-trading stdio MCP: 주식잔고조회_실현손익 "
        "(get_balance_rlz_pl, inquire-balance-rlz-pl v1_국내주식-041). "
        "stock_name 생략 시 전체 보유 종목. 모의투자 URL 미지원."
    )

    async def get_balance_rlz_pl(inp: FinusMcpTradingStockNameInput) -> str:
        arguments: McpCallArguments = {}
        if inp.stock_name.strip():
            arguments["stock_name"] = inp.stock_name.strip()
        result = await _mcp_trading_call(
            vendor_root=config.vendor_root,
            timeout_sec=config.timeout_sec,
            tool_name="get_balance_rlz_pl",
            arguments=arguments,
        )
        return _record_and_mask("finus_mcp_trading_balance_rlz_pl", result)

    get_balance_rlz_pl.__doc__ = doc
    yield FunctionInfo.from_fn(
        get_balance_rlz_pl,
        description=doc,
        input_schema=FinusMcpTradingStockNameInput,
        converters=[_finus_react_input_converter(FinusMcpTradingStockNameInput)],
    )


@register_function(config_type=FinusSaveDiaryConfig)
async def finus_save_diary(config: FinusSaveDiaryConfig, _builder: Builder):
    base_url = _finus_backend_base_url(config.backend_url)

    async def save_trading_diary(inp: FinusSaveDiaryInput) -> str:
        """Fin-Us backend db에 매매일지를 저장합니다.

        Args:
            title: 일지 제목 (예: ``매매일지 2026-05-24``).
            content: 일지 본문(종목·매매 구분·금액·변동률·회고 등).

        Returns:
            저장된 일지 메타데이터 JSON 문자열. 실패 시 ``error`` 필드를 가진 JSON.
        """
        # 역치환 후 저장한다 (#231). 일지 본문은 LLM이 **마스킹된 잔고를 보고** 쓴
        # 것이라, 그대로 넘기면 "<AMOUNT_9f2a1c_1>주 매수"가 backend DB에 영구히
        # 박힌다 — 요청이 끝나면 매핑이 사라져 복구할 수 없다. 목적지가 외부 LLM이
        # 아니라 우리 backend이므로 여기서 원값으로 되돌리는 것이 맞다.
        # restore_for_internal은 이 요청이 만든 자리표시자만 되돌린다. 통과시킨 나머지는
        # 아래 거부 가드가 받는다 (#339).
        title = restore_for_internal((inp.title or "").strip())
        content = restore_for_internal((inp.content or "").strip())
        if not title:
            result = _err_json("diary_title_required", hint="제목이 비어 있습니다. 비어 있지 않은 제목을 넣으세요.")
            return _record_and_mask("finus_save_diary", result)
        if not content:
            result = _err_json("diary_content_required", hint="본문이 비어 있습니다. 비어 있지 않은 일지 본문을 넣으세요.")
            return _record_and_mask("finus_save_diary", result)

        # 되돌리지 못한 자리표시자가 남아 있으면 **저장하지 않는다** (#339 방향 3).
        # 여기서 세는 것은 **이 요청이 발급한 scope**의 토큰뿐이다 — LLM이 번호를
        # 지어냈거나 박스를 물려받지 못해 매핑이 유실된 것이고, 원값이 이 프로세스
        # 어디에도 없다. 그대로 넣으면 값이 내부 토큰으로 영구히 대체되므로, 조용한
        # 원본 소실 대신 시끄러운 실패를 택한다.
        #
        # 낯선 scope(backend가 사용자 발화를 마스킹해 만든 것, #230)는 더 이상 여기서
        # 거부하지 않는다 (#354). backend가 llm_chat의 매핑을 요청 범위로 들고 있다가
        # POST 시점에 되돌리므로(backend/pii_registry.py), 여기서 막으면 그 복구를
        # 우리가 막는 셈이 된다. 되돌릴 수 없는 낯선 scope는 backend가 같은 자리에서
        # 422로 거부한다 — 판정을 값이 있는 쪽이 한다. 근거는 docs/nfr-05-pii-masking.md.
        leftover = unrestorable_placeholders(title) + unrestorable_placeholders(content)
        if leftover:
            # 자리표시자 원문은 로그에만 남긴다. 오류 JSON은 Observation으로 LLM
            # 컨텍스트에 재유입되므로, 거기에 토큰을 실으면 에이전트가 그대로 답변에
            # 옮겨 적을 여지를 준다 — 지금 막으려는 것과 같은 종류의 오염이다.
            logger.warning(
                "매매일지 저장을 거부했습니다 — 되돌리지 못한 자리표시자 %d개: %s",
                len(leftover),
                leftover,
            )
            result = _err_json(
                "diary_unrestorable_placeholder",
                count=len(leftover),
                kinds=sorted({placeholder_kind(ph) for ph in leftover}),
                hint=(
                    "일지 본문·제목에 원값을 알 수 없는 내부 자리표시자가 남아 있어 "
                    "저장하지 않았습니다. 그대로 저장하면 값이 복구 불가능하게 사라집니다. "
                    "이 자리표시자는 이 요청의 조회 도구 결과에서 온 것인데 원값을 잃었거나 "
                    "번호가 실제와 다른 것입니다 — 사용자에게 다시 묻지 말고 둘 중 하나로 "
                    "진행하세요: (1) 해당 잔고·주문 조회 도구를 다시 호출해 그 결과에 나온 "
                    "자리표시자를 그대로 옮겨 적은 뒤 다시 저장한다, (2) 해당 금액·수량을 "
                    "본문·제목에서 빼고 다시 저장한다."
                ),
            )
            return _record_and_mask("finus_save_diary", result)

        url = f"{base_url}/api/v1/db/diary"
        try:
            async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
                resp = await client.post(
                    url,
                    json={"title": title, "content": content},
                    headers=_finus_backend_headers(),
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            result = _err_json(
                "diary_api_http_error",
                status_code=exc.response.status_code,
                detail=exc.response.text[:500],
                url=url,
            )
            return _record_and_mask("finus_save_diary", result)
        except Exception as exc:  # noqa: BLE001
            result = _err_json("diary_api_request_failed", detail=str(exc), url=url)
            return _record_and_mask("finus_save_diary", result)

        if body.get("status") != "success":
            result = _err_json("diary_api_error", response=body)
            return _record_and_mask("finus_save_diary", result)
        # 저장 성공 응답은 **메타데이터만** 돌려준다 (#231, PR #335 리뷰). backend는
        # `{"status": "success", "data": <Diary 전체>}`를 주는데, 그 `data`에는 방금
        # 위에서 역치환한 `title`·`content`가 평문 그대로 들어 있다. 그대로 반환하면
        # 이 이슈가 막은 평문이 같은 요청 안에서 Observation으로 컨텍스트에 재유입돼
        # 다음 턴에 외부 LLM으로 나간다. 에이전트가 저장 확인에 필요한 것은 식별자와
        # 시각뿐이므로 되비춤 자체를 없앤다.
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}
        saved = {"id": data.get("id"), "created_at": data.get("created_at")}
        result = json.dumps(saved, ensure_ascii=False)
        return _record_and_mask("finus_save_diary", result)

    yield FunctionInfo.from_fn(
        save_trading_diary,
        description=save_trading_diary.__doc__,
        input_schema=FinusSaveDiaryInput,
        converters=[_finus_react_input_converter(FinusSaveDiaryInput)],
    )


@register_function(config_type=FinusListDiariesConfig)
async def finus_list_diaries(config: FinusListDiariesConfig, _builder: Builder):
    base_url = _finus_backend_base_url(config.backend_url)

    async def list_trading_diaries(inp: FinusListDiariesInput) -> str:  # noqa: ARG001
        """Fin-Us backend에 저장된 매매일지 목록을 조회합니다.

        Returns:
            일지 배열 JSON 문자열. 실패 시 ``error`` 필드를 가진 JSON.
        """
        url = f"{base_url}/api/v1/db/diary"
        try:
            async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
                resp = await client.get(url, headers=_finus_backend_headers())
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            result = _err_json(
                "diary_api_http_error",
                status_code=exc.response.status_code,
                detail=exc.response.text[:500],
                url=url,
            )
            return _record_and_mask("finus_list_diaries", result)
        except Exception as exc:  # noqa: BLE001
            result = _err_json("diary_api_request_failed", detail=str(exc), url=url)
            return _record_and_mask("finus_list_diaries", result)

        if body.get("status") != "success":
            result = _err_json("diary_api_error", response=body)
            return _record_and_mask("finus_list_diaries", result)
        result = json.dumps(body.get("data"), ensure_ascii=False)
        return _record_and_mask("finus_list_diaries", result)

    yield FunctionInfo.from_fn(
        list_trading_diaries,
        description=list_trading_diaries.__doc__,
        input_schema=FinusListDiariesInput,
        converters=[_finus_react_input_converter(FinusListDiariesInput)],
    )


def _mcp_tool_input_schema_json(tool: Any) -> str | None:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        return json.dumps(schema.model_json_schema(), indent=2, ensure_ascii=False)
    if isinstance(schema, dict):
        return json.dumps(schema, indent=2, ensure_ascii=False)
    if hasattr(schema, "schema_json"):
        return str(schema.schema_json(indent=2))
    return json.dumps({"raw": str(schema)}, indent=2, ensure_ascii=False)


# MCP tool 한 개의 이름/설명/inputSchema를 사람이 읽기 좋은 마크다운 블록으로 만듭니다.
def _format_one_mcp_tool(tool: Any) -> str:
    name = getattr(tool, "name", "?")
    desc = (getattr(tool, "description", None) or "").strip()
    schema = _mcp_tool_input_schema_json(tool)
    lines = [f"### MCP tool `{name}`"]
    if desc:
        lines.append(desc)
    if schema:
        lines.append("inputSchema:")
        lines.append(schema)
    return "\n".join(lines)


# list_tools 결과 전체를 LLM용 도큐먼트로 합칩니다.
def _build_list_tools_doc(lr: Any) -> str:
    tools = list(getattr(lr, "tools", None) or [])
    header = (
        "### Kis Trading MCP (list_tools)\n"
        f"{_KIS_TRADING_TOOL_GUIDE}\n\n"
        "각 MCP 도구는 인자 객체 ``api_type`` + ``params`` 만 받습니다. 새 TR은 먼저 ``find_api_detail`` 로 필드 확인.\n"
        "NAT 도구 호출 시 **tool_name** 으로 아래 목록 중 하나를 고르세요(비우면 YAML ``trading_tool_name`` 기본값).\n"
        "모의투자는 MCP 설명대로 ``params.env_dv`` 를 ``demo`` 로 넣습니다.\n"
    )
    if not tools:
        return header + "(서버가 빈 도구 목록을 반환했습니다.)"
    parts = [header, "### 노출된 MCP 도구\n"]
    for t in tools:
        parts.append(_format_one_mcp_tool(t))
        parts.append("")
    return "\n".join(parts).strip()


async def _fetch_mcp_tool_documentation(config: FinusAccountBalanceConfig) -> str | None:
    if (os.environ.get("FINUS_SKIP_MCP_LIST_TOOLS") or "").strip().lower() in ("1", "true", "yes"):
        return None
    url = (config.mcp_url or "").strip()
    if not url:
        return None
    timeout_sec = min(25.0, max(5.0, float(config.timeout_sec)))

    async def _list_doc() -> str:
        async with _remote_mcp_session(
            transport=config.mcp_transport, url=url, operation_timeout=timeout_sec,
        ) as session:
            return _build_list_tools_doc(await session.list_tools())

    try:
        text = await asyncio.wait_for(_list_doc(), timeout=timeout_sec + _MCP_LIST_TOOLS_GRACE_SEC)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if len(text) > _MCP_DOC_MAX_CHARS:
        return text[: _MCP_DOC_MAX_CHARS - 100] + "\n…(truncated)"
    return text


@register_function(config_type=FinusAccountBalanceConfig)
async def finus_account_balance(config: FinusAccountBalanceConfig, _builder: Builder):

    async def get_account_balance(inp: KisTradingMcpCallInput) -> str:
        """Kis Trading MCP ``call_tool``: ``tool_name``(선택) + ``api_type`` + ``params``; 레거시 ``tool_input`` JSON.

        Defaults:
        - ``inquire_balance``: 서버가 ``env_dv``·``inqr_dvsn`` 및 TR 필수 필드(``afhr_flpr_yn`` 등)를 기본 채움.
        - ``inquire_account_balance``: omit ``env_dv``; keep ``inqr_dvsn_1`` empty unless ``find_api_detail`` says otherwise.

        Self-correction:
        On API/validation errors, call ``find_api_detail`` for the failed ``api_type``, fix ``params``, and retry before
        producing the final answer.
        """
        prepared = _prepare_kis_trading_mcp_call(inp, config)
        if isinstance(prepared, str):
            return _record_and_mask(_KIS_BALANCE_LEDGER_NAME, prepared)
        tool_name, arguments = prepared
        return await _call_kis_mcp_and_record(tool_name, arguments, config)

    description = await _build_kis_mcp_description(get_account_balance.__doc__ or "", config)
    yield FunctionInfo.from_fn(
        get_account_balance,
        description=description,
        input_schema=KisTradingMcpCallInput,
    )
