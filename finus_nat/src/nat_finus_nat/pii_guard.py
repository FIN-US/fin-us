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

import logging
import re
from contextvars import ContextVar

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
    (사용자 원문 유래)까지 여기서 중립 문구로 갈아엎으면, backend의 왕복이 깨진 채
    DB에 저장된다.
    """
    # 이웃한 ``unmask_response``는 ``box is None``으로 갈리는데 여기는 빈 dict까지 함께
    # 걷어낸다. **동작 차이는 없다** — 빈 박스로 아래 sub()를 돌려도 되돌릴 항목이 없어
    # 원문이 그대로 나온다. 이쪽은 그 경우를 빠른 경로로 접어 둔 것뿐이고, 저쪽은 박스가
    # 비어도 "이 요청 scope인데 매핑에 없는 것"의 판정 흐름을 한 갈래로 유지한다.
    box = PII_MAPPING.get()
    if not text or not box:
        return text
    return _SCOPED_PLACEHOLDER_RE.sub(lambda m: box.get(m.group(0), m.group(0)), text)


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
