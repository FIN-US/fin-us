"""NAT 프로세스 안에서 도구 결과를 마스킹하는 계층 (#231, F-17 / NFR-05).

#230이 backend에 세운 마스킹 계층(`backend/services.py`의 `llm_chat`)은 backend가
NAT에 보내는 `user_msg`만 본다. 그런데 계좌·잔고 데이터는 backend가 아니라 **NAT
프로세스 안에서** MCP 도구 호출로 생성돼, 그대로 ReAct 컨텍스트에 실려
`openai_cloud_llm`(`configs/common.yml`)으로 나간다. 즉 유출량이 가장 큰 경로가
backend 계층 바깥에 있었다.

이 모듈은 그 경로를 NAT 안에서 닫는다. **옵션 A(도구 결과 마스킹)** 를 택했고,
옵션 B·C를 택하지 않은 근거는 `docs/nfr-05-pii-masking.md`에 적었다.

## 배선 — 세 지점

1. **박스 심기**: `agents.finus_reasoning_trace_agent`가 요청 하나마다
   :data:`PII_MAPPING`에 빈 dict를 심는다. 두 라우터 config(`router.yml`,
   `router_nomemory.yml`) 모두 이 함수를 최상위 `workflow`로 쓰므로, config가 무엇이든
   박스는 같은 자리에서 생긴다 — `REASONING_TRACE`와 같은 이유의 같은 자리다(#273).
2. **나갈 때 마스킹**: `finus_api`의 계좌 계열 도구가 결과를 돌려주기 직전
   :func:`mask_tool_result`를 거친다. 매핑은 박스에 **누적**된다.
3. **돌아올 때 역치환**: 같은 `finus_reasoning_trace_agent`가 최종 응답 본문에
   :func:`unmask_response`를 적용한다.

## 인자 방향 (#338)

위 셋은 도구 **결과**만 다룬다. 그런데 에이전트는 마스킹된 잔고를 보고 그 수량으로
주문을 내려 하고, 그때 자리표시자가 KIS `params`에 실린다. 목적지가 외부 LLM이 아니라
KIS MCP이므로 여기서도 되돌리는 것이 맞다 — 다만 무조건 되돌리면 종류가 어긋난 값까지
숫자가 되어 조용한 오주문이 가능해지므로, :func:`restore_params_for_kis`가 종류 검사를
함께 건다. 근거는 그 함수의 docstring과 `docs/nfr-05-pii-masking.md`.

## ContextVar 방향성

`DATA_TOOL_LEDGER`와 같은 제약을 받는다 — `ContextVar.set()`은 LangGraph의
`copy_context()` 경계를 넘어 **바깥으로 전파되지 않는다**. 그래서 박스는 최상위에서
한 번만 심고(1), 안쪽 도구는 그 dict를 **mutate만** 한다(2). 안쪽에서 `set()`을 하면
바깥의 역치환(3)은 그 매핑을 영영 보지 못한다.

## 왜 backend의 `unmask_pii`를 그대로 쓰지 않는가

backend `llm_chat`은 NAT을 부르기 **전에** `user_msg`를 자기 매핑으로 마스킹하고,
NAT 응답을 받은 **뒤에** 자기 매핑으로 역치환한다. 즉 NAT이 보는 요청과 NAT이
돌려주는 응답에는 **backend가 만든 자리표시자**가 섞여 있다.

`unmask_pii`는 매핑에 없는 자리표시자를 전부 "(이전 금액 1)" 같은 중립 문구로
바꾼다(fail-open). NAT 경계에서 그대로 쓰면 backend의 자리표시자까지 중립 문구로
갈아엎어, backend가 돌려놓을 원값이 사라진다 — 왕복이 깨진다.

그래서 :func:`unmask_response`는 **자기가 만든 것만** 건드린다:

- 박스에 있는 자리표시자 → 원값으로 복원.
- 박스에 없지만 **scope가 이 요청에서 발급된 것** → LLM이 번호를 지어냈거나 변형한
  경우다. 중립 문구로 치환한다(내부 토큰이 화면에 나가는 것을 막는 #230의 방어선).
- 그 외 scope → **손대지 않는다.** backend(또는 이전 턴)의 것이므로 그대로 통과시켜
  backend의 `unmask_pii`가 판정하게 둔다.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, NamedTuple

from .pii_mask import _FALLBACK_LABEL, mask_pii

logger = logging.getLogger(__name__)

# 요청 하나 동안 누적되는 {자리표시자: 원값} 박스. 최상위 에이전트가 심고 도구가 채운다.
# None은 "박스가 없다"는 뜻이며, 그때의 동작은 mask_tool_result docstring 참고.
PII_MAPPING: ContextVar[dict[str, str] | None] = ContextVar("PII_MAPPING", default=None)


# ---------------------------------------------------------------------------
# 마스킹 대상 도구
# ---------------------------------------------------------------------------

# 계좌 자격증명으로 조회한 데이터를 돌려주는 도구들 — 결과가 LLM 컨텍스트에 들어가기
# 전에 마스킹한다. finus_api.py가 원장에 쓰는 도구 이름과 같은 문자열이어야 한다.
#
# **왜 이 목록이 도구 단위이고 api_type 단위가 아닌가 (fail-closed)**
#
# ``finus_account_balance``는 Kis Trading MCP pass-through라, 같은 도구가 잔고
# (``inquire_balance``)와 시세(``inquire_price``)를 모두 서비스한다. api_type으로
# 갈라 시세만 마스킹에서 빼면 "계좌 계열 TR 목록"을 이쪽에서 관리해야 하는데, 그
# 목록의 출처는 **외부 MCP 서버**(upstream ``open-trading-api``)라 이 저장소가 통제하지
# 못한다. 새 TR이 추가될 때마다 목록에서 빠지고, 빠진 항목은 조용한 유출이 된다.
# 그래서 도구 단위로 전부 마스킹한다 — 새 TR의 기본값은 "마스킹됨"이다.
#
# 감수하는 비용: 시세(현재가·호가·차트)도 자리표시자로 나가므로 LLM은 절대 가격 수준을
# 근거로 판단할 수 없다. 이는 #230이 (a) 방식을 택하며 이미 감수하고 문서화한 한계와
# **같은 종류의** 비용이고, 왕복이 무손실이라 **사용자가 보는 최종 값은 원값 그대로**다.
# 품질 저하가 실제로 관측되면 api_type 면제 목록을 별도 이슈로 검토한다.
#
# 목록에 없는 도구(뉴스·공시·실적)는 마스킹하지 않는다. 공개 정보이고, 기사 본문의
# 금액까지 자리표시자로 바꾸면 얻는 것 없이 분석만 망가진다.
MASKED_TOOLS: frozenset[str] = frozenset({
    "finus_account_balance",             # Kis Trading MCP pass-through (readonly 래퍼도 이 원장 키를 쓴다)
    "finus_mcp_trading_get_balance",     # 잔고·보유종목
    "finus_mcp_trading_balance_rlz_pl",  # 실현손익
    "finus_mcp_trading_today_orders",    # 당일 주문·체결
    "finus_list_diaries",                # 저장된 매매일지 본문(금액·수량 포함)
    "finus_save_diary",                  # 저장 응답이 방금 역치환한 본문을 되비춘다 — 아래 참고
})

# **왜 `finus_save_diary`가 조회 도구도 아닌데 목록에 있는가**
#
# 이 도구는 저장 직전 `restore_for_internal`로 제목·본문을 **원값으로 되돌린다**. 그런데
# backend `POST /api/v1/db/diary`는 `{"status": "success", "data": <Diary 전체>}`를
# 돌려주고(`backend/main.py`), `Diary`에는 방금 되돌린 `title`·`content`가 그대로 들어
# 있다. 그 응답을 Observation으로 돌려주면 이 계층이 막은 평문이 **같은 요청 안에서**
# 컨텍스트에 재유입돼 다음 턴에 외부 LLM으로 나간다.
#
# 성공 경로는 `finus_api`가 반환값을 `{"id", "created_at"}`로 좁혀 되비춤 자체를 없앴지만,
# 목록에도 넣어 둔다 — 오류 경로(`diary_api_http_error`의 `detail`은 backend 422 응답에
# 실린 요청 본문을 그대로 담을 수 있다)까지 한 번에 덮는 fail-closed 쪽이다.

# 자리표시자 형식은 pii_mask._PLACEHOLDER_RE와 같지만 **scope를 캡처**한다 — 위
# docstring의 "자기가 만든 것만" 판정에 필요하다. 얼터네이션을 같은 출처
# (_FALLBACK_LABEL)에서 조립하므로 종류가 늘어도 두 정규식이 함께 따라간다.
# 둘이 정말 같은 것을 매치하는지는 test_pii_guard.py가 고정한다.
_SCOPED_PLACEHOLDER_RE = re.compile(
    r"<(" + "|".join(_FALLBACK_LABEL) + r")_([0-9a-f]{6})_(\d+)>"
)


def install_mapping_box() -> dict[str, str]:
    """요청 하나짜리 매핑 박스를 만들어 돌려준다. 호출자가 :data:`PII_MAPPING`에 심는다."""
    return {}


def mask_tool_result(tool_name: str, result: str) -> str:
    """*tool_name*이 마스킹 대상이면 *result*를 마스킹하고 매핑을 박스에 누적한다.

    대상이 아니면 *result*를 그대로 돌려준다.

    박스가 없으면(ContextVar를 물려받지 못한 스레드풀 등 — ``_record_to_ledger``가
    같은 상황을 ERROR로 남긴다) **마스킹은 그대로 하고** 매핑만 버린다. 유출을 막는
    쪽을 우선하는 fail-closed 선택이다. 그 결과 그 요청의 응답에서는 자리표시자가
    원값으로 돌아오지 못하고 중립 문구가 되지만, 그것은 관측 가능한 품질 저하이고
    반대(매핑을 살리려 마스킹을 건너뛰기)는 관측되지 않는 유출이다.
    """
    if tool_name not in MASKED_TOOLS:
        return result

    masked, mapping = mask_pii(result)
    if not mapping:
        return masked

    box = PII_MAPPING.get()
    if box is None:
        logger.error(
            "PII_MAPPING box not set when %s completed — %d placeholder(s) cannot be "
            "restored and will degrade to neutral phrases. The tool may have run outside "
            "the finus_reasoning_trace_agent context or in a threadpool that does not "
            "propagate contextvars.",
            tool_name,
            len(mapping),
        )
        return masked

    # 같은 자리표시자가 서로 다른 호출에서 재발급될 일은 없다 — mask_pii 호출마다 scope를
    # 새로 뽑기 때문이다(pii_mask._Counter). 따라서 update는 덮어쓰지 않고 누적만 한다.
    box.update(mapping)
    return masked


def restore_for_internal(text: str) -> str:
    """자리표시자를 원값으로 되돌린다 — **우리 저장소로 나가는 값** 전용.

    ``finus_save_diary``가 backend DB에 쓰는 제목·본문이 이 함수를 거친다. 그 본문은
    LLM이 **마스킹된 잔고를 보고** 쓴 것이라, 그대로 저장하면
    ``<AMOUNT_9f2a1c_1>``가 DB에 영구히 박힌다 — 요청이 끝나면 매핑이 사라지므로
    복구 불가능한 손상이다. 저장 목적지가 외부 LLM이 아니라 우리 backend이므로
    여기서는 원값으로 되돌리는 것이 맞다.

    박스에 있는 것만 되돌리고 나머지는 손대지 않는다 — backend가 만든 자리표시자
    (사용자 원문 유래)까지 여기서 중립 문구로 갈아엎으면, 원값 대신 "(이전 금액 1)"이
    DB에 박힌다. 손상의 종류만 바뀌고 소실은 그대로다.

    **그래서 이 함수만으로는 저장이 안전하지 않다.** 손대지 않고 통과시킨 것은 원값이
    어디에도 없는 토큰이고, 저장은 backend 왕복의 바깥이라 나중에 복원될 기회가 없다.
    호출자는 결과를 :func:`unrestorable_placeholders`에 한 번 더 태워, 남은 것이 있으면
    저장 자체를 거부해야 한다(#339). 그 함수의 docstring에 두 종류의 잔여 토큰을 적었다.
    """
    # 이웃한 ``unmask_response``는 ``box is None``으로 갈리는데 여기는 빈 dict까지 함께
    # 걷어낸다. **동작 차이는 없다** — 빈 박스로 아래 sub()를 돌려도 되돌릴 항목이 없어
    # 원문이 그대로 나온다. 이쪽은 그 경우를 빠른 경로로 접어 둔 것뿐이고, 저쪽은 박스가
    # 비어도 "이 요청 scope인데 매핑에 없는 것"의 판정 흐름을 한 갈래로 유지한다.
    box = PII_MAPPING.get()
    if not text or not box:
        return text
    return _SCOPED_PLACEHOLDER_RE.sub(lambda m: box.get(m.group(0), m.group(0)), text)


def unrestorable_placeholders(text: str) -> list[str]:
    """*text*에 남은 **되돌릴 수 없는** 자리표시자를 등장 순서대로 돌려준다 (#339).

    :func:`restore_for_internal`을 **거친 뒤의** 값에 쓴다. 그 함수는 이 요청의 박스에
    있는 것만 되돌리므로, 남아 있는 것은 두 종류뿐이고 **둘 다 원값이 어디에도 없다**:

    - **낯선 scope** — backend ``llm_chat``이 NAT을 부르기 전에 사용자 발화를 자기
      매핑으로 마스킹한 것이다(#230). 응답 본문이라면 backend가 돌려받아 역치환하지만,
      저장은 그 왕복의 **바깥**이다 — ``POST /api/v1/db/diary``는 저장만 하고
      ``unmask_pii``를 타지 않는다(``backend/main.py::create_db_diary``). 요청이 끝나면
      backend의 매핑은 지역 변수와 함께 사라진다.
    - **이 요청 scope인데 박스에 없음** — LLM이 번호를 지어냈거나(``<AMOUNT_9f2a1c_9>``)
      박스를 물려받지 못한 실행 경로에서 매핑이 유실된 경우다.

    둘을 갈라 한쪽만 막을 이유가 없다. 저장 관점에서 차이는 "누가 만들었는가"뿐이고,
    결과는 같다 — 원값이 없는 토큰이 ``Diary.content``에 **영구히** 박힌다. 사용자가
    나중에 일지를 열면 자기가 쓴 금액 대신 내부 토큰을 본다.

    호출자(``finus_save_diary``)는 이 목록이 비어 있지 않으면 **저장하지 않는다.**
    조용한 원본 소실보다 시끄러운 실패가 낫다는 판정이다(#339 방향 3). 근거와
    택하지 않은 두 방향은 ``docs/nfr-05-pii-masking.md``에 적었다.
    """
    if not text:
        return []
    return [m.group(0) for m in _SCOPED_PLACEHOLDER_RE.finditer(text)]


def placeholder_kind(placeholder: str) -> str:
    """자리표시자에서 종류(``ACCOUNT``/``AMOUNT``/``QTY``)를 뽑는다.

    :func:`unrestorable_placeholders`가 돌려준 값을 넣는다. 호출자가 문자열을 직접
    쪼개면 자리표시자 규약이 바뀔 때 그 자리만 조용히 어긋나므로, 형식을 아는
    :data:`_SCOPED_PLACEHOLDER_RE`에서 캡처한 값을 그대로 쓴다.

    형식에 맞지 않는 문자열은 빈 문자열을 돌려준다 — 위 함수의 출력만 넣는 한
    도달하지 않지만, 종류를 알 수 없다는 이유로 호출자를 죽이지는 않는다.
    """
    match = _SCOPED_PLACEHOLDER_RE.fullmatch(placeholder)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# 도구 **인자** 방향 — KIS 주문 파라미터 역치환 (#338)
# ---------------------------------------------------------------------------

# 자리표시자가 올 수 있는 KIS 파라미터의 **마지막 밑줄 마디** → 그 자리에 허용되는
# 자리표시자 종류. 필드 이름 전체가 아니라 마지막 마디로 판정한다.
#
# **왜 필드 이름 목록이 아니라 접미 마디인가 (fail-closed)**
#
# 이슈(#338)가 지적한 위험은 "수량 필드 목록을 하드코딩하면 KIS가 필드를 늘릴 때
# 조용히 새는 쪽으로 무너진다"였다. KIS TR 필드는 마디 규약이 일정해서
# (``ORD_QTY``·``CNCL_QTY``, ``ORD_UNPR``·``STCK_PRPR``, ``TOT_EVLU_AMT``) 새 필드도
# 같은 마디를 쓴다 — 목록이 아니라 규약을 보면 새 필드가 자동으로 덮인다.
#
# 규약을 벗어난 새 필드가 나오면 아래 판정은 "모른다"가 되고, 모르는 필드에 실린
# 자리표시자는 **복원하지 않고 거부한다**. 즉 드리프트의 결과는 유출이나 오주문이
# 아니라 이 계층이 내는 시끄러운 실패다 — 이슈가 걱정한 방향의 반대다.
#
# 갱신 책임: KIS가 새 마디를 쓰는 수량·금액 필드를 도입하면 여기에 추가한다. 추가를
# 잊으면 그 필드를 쓰는 주문이 거부되며(관측 가능), 잘못된 값이 나가지는 않는다.
_PARAM_KIND_BY_SUFFIX: dict[str, str] = {
    "QTY": "QTY",      # ORD_QTY, CNCL_QTY, RVSE_QTY, SLL_QTY …
    "UNPR": "AMOUNT",  # ORD_UNPR (주문 단가)
    "PRPR": "AMOUNT",  # STCK_PRPR (현재가)
    "PRC": "AMOUNT",   # *_PRC (가격)
    "AMT": "AMOUNT",   # TOT_EVLU_AMT, ORD_AMT …
}

# 복원된 원값이 KIS 파라미터로 나갈 수 있는 모양. 접미사·콤마를 벗긴 뒤 이것에
# 맞아야 한다. 맞지 않으면 거부한다 — 아래 _as_kis_number 참고.
_KIS_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class ParamRejection(NamedTuple):
    """:func:`restore_params_for_kis`가 복원을 거부한 파라미터 하나.

    *placeholder*는 **로그 전용**이다. 호출자가 만드는 오류 JSON은 Observation으로
    LLM 컨텍스트에 재유입되므로, 거기에 내부 토큰을 실으면 에이전트가 그것을 답변에
    옮겨 적을 여지를 준다 — ``finus_save_diary``의 거부 가드가 같은 이유로 같은
    구분을 한다.
    """

    field: str
    reason: str
    placeholder: str


def _expected_kind(field: str) -> str | None:
    """*field*(KIS 파라미터 이름)에 허용되는 자리표시자 종류. 모르면 ``None``."""
    return _PARAM_KIND_BY_SUFFIX.get(field.strip().upper().rsplit("_", 1)[-1])


def _as_kis_number(value: str) -> str | None:
    """원값을 KIS가 받는 숫자 문자열로 정규화한다. 못 하면 ``None``.

    자리표시자의 원값은 사람이 읽는 표기 그대로다 — ``mask_pii``의 인식기가 매치한
    구간을 통째로 매핑에 넣기 때문이다. 종류별로 남는 군더더기가 다르다:

    - ``AMOUNT`` — ``_AMOUNT_WON_RE``·``_AMOUNT_UNIT_RE``는 ``원``을 매치에 **포함**한다
      (``"12,345,000원"``). ``_LABELED_AMOUNT_RE``는 라벨을 빼고 숫자만 담지만 콤마는
      남는다(``"1,234,567"``).
    - ``QTY`` — ``_QTY_RE``는 ``(?=주)`` lookahead라 ``주``는 매치 밖이지만, 잔고
      리포트가 ``toLocaleString("ko-KR")``을 쓰므로 콤마가 들어온다(``"1,234"``).

    그대로 KIS ``params``에 넣으면 전부 거부된다. 그래서 접미사와 콤마를 벗겨 순수
    숫자로 만든다.

    **벗겨서 숫자가 되지 않으면 복원하지 않는다.** 한글 수사 표기(``"3천만원"`` —
    ``_AMOUNT_UNIT_RE``가 만드는 매핑이고, ``finus_list_diaries``가 돌려주는 일지
    본문에 실제로 들어 있다)를 숫자로 해석하려면 수사 파서가 필요한데, 그 파서가
    한 자리라도 틀리면 **자릿수가 어긋난 주문**이 조용히 나간다. 이 이슈가 피하려던
    바로 그 맞바꿈이라, 해석하지 않고 거부한다.
    """
    core = value.strip().removesuffix("원").strip().replace(",", "")
    if not core or not _KIS_NUMBER_RE.fullmatch(core):
        return None
    return core


def _restore_one_param(
    field: str, value: str, box: dict[str, str],
) -> tuple[str, ParamRejection | None]:
    """파라미터 값 하나를 복원한다. 거부하면 *value*를 그대로 돌려주고 사유를 함께 낸다."""
    matches = list(_SCOPED_PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value, None

    stripped = value.strip()
    if len(matches) > 1 or matches[0].group(0) != stripped:
        # "10<QTY_..._1>"·"<QTY_..._1>주"처럼 자리표시자가 값의 **일부**인 형태. 어디까지가
        # 값이고 어디부터가 군더더기인지 이쪽이 판정할 근거가 없다.
        return value, ParamRejection(field, "placeholder_is_not_the_whole_value", stripped)

    placeholder = stripped
    kind = matches[0].group(1)
    expected = _expected_kind(field)
    if expected is None:
        # 위 _PARAM_KIND_BY_SUFFIX 주석의 fail-closed 지점. ``CANO``·``ACNT_PRDT_CD``
        # 같은 계좌 필드도 여기로 떨어진다 — mcp-trading이 ``KIS_ACCOUNT_NO`` env로
        # 채우는 값이라(``mcp-trading/balance.js``의 ``buildBalanceParams()``) LLM 인자로
        # 올 이유가 없고, 원값의 하이픈 유무가 원문 표기를 따라가므로 ``CANO``/
        # ``ACNT_PRDT_CD`` 분리와 어긋날 수 있다.
        return value, ParamRejection(field, "field_does_not_accept_placeholders", placeholder)
    if kind != expected:
        # 이 이슈의 본체다. 역치환만 붙이면 ``ORD_QTY``에 실린 ``AMOUNT`` 자리표시자가
        # 금액으로 복원돼 **의미가 어긋난 주문이 조용히** 나간다. 종류 검사가 그
        # 맞바꿈을 되돌린다 — 여전히 시끄럽게 실패한다.
        return value, ParamRejection(field, f"expected_{expected}_placeholder_got_{kind}", placeholder)

    original = box.get(placeholder)
    if original is None:
        # 낯선 scope(backend가 사용자 발화를 마스킹한 것)이거나 LLM이 지어낸 번호다.
        # 원값이 이 프로세스 어디에도 없으므로 복원할 수 없다 —
        # ``unrestorable_placeholders``가 저장 경로에서 다루는 것과 같은 두 종류다.
        return value, ParamRejection(field, "no_mapping_in_this_request", placeholder)

    number = _as_kis_number(original)
    if number is None:
        return value, ParamRejection(field, "restored_value_is_not_a_number", placeholder)
    return number, None


def restore_params_for_kis(
    params: Mapping[str, Any], *, tool_name: str, api_type: str,
) -> tuple[dict[str, Any], list[ParamRejection]]:
    """KIS ``params``의 자리표시자를 숫자 원값으로 되돌린다 (#338, 후보 1).

    마스킹은 도구 **결과**에만 걸려 있었고 인자 방향에는 역치환이 없었다. 그래서
    에이전트는 자기가 방금 본 잔고 수량으로 주문을 낼 수 없었다 — "보유 전량 매도"가
    깨지는 자리다. 목적지가 외부 LLM이 아니라 KIS MCP이므로 되돌리는 것이 맞다.

    다만 **역치환만** 붙이면 이 이슈가 우려한 맞바꿈이 생긴다: 지금은 자리표시자가
    실리면 MCP가 거부해 주문이 안 나가는데(시끄러운 실패), 무조건 되돌리면 종류가
    어긋난 값도 숫자가 되어 **조용한 오주문**이 가능해진다. 그래서 되돌리기 전에
    검사를 건다 — 넷 다 fail-closed다:

    - 값 전체가 자리표시자 하나여야 한다.
    - 필드의 접미 마디가 요구하는 종류와 자리표시자의 종류가 같아야 한다.
    - 원값이 이 요청의 박스에 있어야 한다.
    - 원값이 숫자로 정규화돼야 한다.

    넷 중 하나라도 어긋나면 그 필드는 **복원하지 않고** 사유를 함께 돌려준다.
    호출자는 사유가 하나라도 있으면 호출 자체를 중단해야 한다 — 일부만 복원해
    보내면 반쯤 맞는 주문이 나간다.

    **막는 것은 종류 *간* 맞바꿈뿐이다.** 같은 종류 안에서 값이 뒤바뀌는 것
    (평가금액 자리표시자를 ``ORD_UNPR``에 싣는 등)은 둘 다 ``AMOUNT``라 그대로
    복원돼 나간다. 자리표시자는 숫자의 타당성 신호를 지우므로 — 평문이었다면
    "주가가 1,234만원"에서 LLM이 느꼈을 이상함이 사라진다 — 이 계층이 만든 위험이
    맞지만, 값이 의미상 맞는지는 종류가 아니라 종목·문맥을 봐야 하는 별도 판정이다
    (#365). 이 함수의 계약은 거기까지가 아니다.

    자리표시자가 없는 값은 손대지 않는다. 정상 경로(에이전트가 종목코드·구분값을
    직접 적는 경우)는 이 함수를 그대로 통과한다.

    *tool_name*·*api_type*은 거부 로그에만 쓴다. 한 요청에서 KIS 호출이 여러 번 나가면
    필드 이름과 사유만으로는 어느 호출이 막혔는지 짚을 수 없다 — 호출자가 이미 아는
    값이므로 받아서 함께 남긴다 (PR #364 리뷰).
    """
    restored: dict[str, Any] = {}
    rejections: list[ParamRejection] = []
    box = PII_MAPPING.get() or {}

    for field, value in params.items():
        if isinstance(value, str):
            new_value, rejection = _restore_one_param(field, value, box)
            if rejection is not None:
                rejections.append(rejection)
            # 거부된 필드도 원값을 그대로 실어 둔다 — 호출자가 호출을 중단하므로 MCP까지
            # 가지 않고, 로그에 원래 인자가 남는 편이 진단에 낫다.
            restored[field] = new_value
            continue

        # 문자열이 아닌 값(중첩 dict·리스트) 안에 자리표시자가 숨어 있는 경우. KIS
        # params는 평평한 스칼라 dict라 정상 경로에서는 나오지 않지만, 나온다면
        # 위 규칙들을 적용할 필드 이름이 없다 — 복원하지 않고 거부한다.
        found = _SCOPED_PLACEHOLDER_RE.search(json.dumps(value, ensure_ascii=False, default=str))
        if found is not None:
            rejections.append(
                ParamRejection(field, "placeholder_inside_a_non_string_value", found.group(0))
            )
        restored[field] = value

    if rejections:
        logger.warning(
            "KIS params 자리표시자 복원을 거부했습니다 — tool=%s api_type=%s %s",
            tool_name,
            api_type,
            [(r.field, r.reason, r.placeholder) for r in rejections],
        )
    return restored, rejections


def unmask_response(text: str) -> str:
    """최종 응답 본문의 자리표시자 중 **이 요청이 만든 것만** 처리한다.

    - 박스에 있으면 원값으로 복원한다.
    - 박스에 없지만 scope가 이 요청에서 발급된 것이면 중립 문구로 치환한다(내부 토큰
      노출 차단). LLM이 번호를 지어냈거나(``<AMOUNT_9f2a1c_9>``) 한 경우다.
    - scope가 낯설면 손대지 않는다 — backend가 마스킹한 것이거나 SQLite 히스토리에
      남은 이전 턴의 것이다. backend의 ``unmask_pii``가 판정할 몫이다.

    모듈 docstring의 "왜 backend의 unmask_pii를 그대로 쓰지 않는가" 참고.
    """
    box = PII_MAPPING.get()
    if not text or box is None:
        return text

    # 자리표시자는 <KIND_scope_n> 이므로 뒤에서 두 번째 조각이 scope다.
    own_scopes = {ph.rsplit("_", 2)[-2] for ph in box}
    missing: list[str] = []
    # 중립 문구 번호 규칙은 pii_mask._restore와 같다 — 같은 자리표시자는 같은 서수,
    # 종류별로 1부터. 두 계층의 사용자 노출 문구가 갈리지 않게 맞춘다.
    ordinals: dict[str, int] = {}
    kind_counts: dict[str, int] = {}

    def _restore(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        value = box.get(placeholder)
        if value is not None:
            return value
        if match.group(2) not in own_scopes:
            return placeholder
        missing.append(placeholder)
        kind = match.group(1)
        ordinal = ordinals.get(placeholder)
        if ordinal is None:
            ordinal = kind_counts.get(kind, 0) + 1
            kind_counts[kind] = ordinal
            ordinals[placeholder] = ordinal
        return f"(이전 {_FALLBACK_LABEL.get(kind, '값')} {ordinal})"

    try:
        restored = _SCOPED_PLACEHOLDER_RE.sub(_restore, text)
    except Exception:  # noqa: BLE001
        # pii_mask.unmask_pii와 같은 fail-open 원칙 — 역치환 실패가 답변 전체를 죽이면
        # 안 된다. 순수 문자열 연산이라 실패할 일은 없지만 방어선을 같은 모양으로 둔다.
        logger.exception("PII 자리표시자 역치환 중 예상치 못한 오류가 발생했습니다.")
        return text

    if missing:
        logger.warning(
            "NAT 응답에서 이 요청의 scope를 가졌지만 매핑에 없는 자리표시자를 발견해 "
            "중립 문구로 치환했습니다: %s",
            missing,
        )
    return restored
