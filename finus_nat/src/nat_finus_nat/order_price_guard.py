"""pass-through KIS 주문의 지정가 괴리 가드 (#365).

## 무엇을 막는가

:func:`~nat_finus_nat.pii_guard.restore_params_for_kis`의 종류 검사는 ``QTY``와
``AMOUNT`` 사이의 맞바꿈만 가른다. **같은 ``AMOUNT`` 안의 맞바꿈** — 잔고 결과의
평가금액 자리표시자(``12,345,000원``)를 ``ORD_UNPR``에 싣는 것 — 은 그대로 복원돼 KIS로
간다. 68,900원짜리 주식에 12,345,000원 지정가 주문이 나가는 것이다. 자리표시자가 두
금액을 똑같은 겉모습으로 만들어, 평문이었다면 LLM이 느꼈을 이상함이 사라졌다.

## 왜 주문 검증자(``/v1/verify-order``)가 아니라 이 코드인가

검증자는 지정가 괴리를 **판정하지 않는다.** backend ``evaluate_hard_limits``가 코드로 이미
판정했다고 전제하고 LLM에게 "다시 판정하지 말라"고 지시하며, ``hard_check.passed``도
호출자가 실어 보낸다. pass-through 경로에는 그 코드 판정이 없고 rationale도 없어서,
검증자를 태우면 이상한 단가는 확실히 막지 못하고 정상 단가는 막힐 수 있다(#365 결정
코멘트의 실측). 그래서 backend와 **같은 판정을 같은 규칙으로** 여기서 코드로 한다.

## 판정 의미 — backend ``evaluate_hard_limits``의 거울

- **대상**: 단가가 있는 주문. backend는 ``LIMIT``만 보고 ``MARKET``은 보지 않는다. KIS
  시장가는 단가 0으로 나가므로 단가 0이나 빈 값은 대상이 아니다. 시세 조회 조건 입력
  (``FID_*``)은 마디가 같아도 단가가 아니다.
- **정정·취소**: 원주문 취소(``ORGN_ODNO`` + ``RVSE_CNCL_DVSN_CD=02``)는 새 주문을 만들지
  않으므로 단가와 무관하게 통과한다. 종목코드가 없는 가격 정정은 기준가를 알 수 없어 전용
  사유로 거부하고 "취소 후 재주문"을 안내한다(PR #379 리뷰).
- **기준가**: 주문 직전 조회한 현재가(``stck_prpr``). backend도 시세 조회의 현재가다.
- **식**: ``abs(단가 - 현재가) / 현재가 > 임계값``이면 초과다. 양방향이고 경계는 통과한다.
- **임계값**: 같은 env 이름 :data:`PRICE_GAP_RATIO_ENV`, 같은 기본값, 같은 해석 규칙
  (``backend/config.py``의 ``_float_env``).

두 계층이 갈리지 않도록 판정표 ``backend/tests/fixtures/price_gap_policy.json``을 양쪽
스위트가 함께 읽고, 기본값은 ``finus_nat/tests/test_order_price_guard.py``가 backend
소스를 정적으로 읽어 대조한다.

## fail-closed

검증할 수 없는 주문은 보내지 않는다. 현재가 조회·파싱 실패, 종목코드 없음, 종목코드 없는
가격 정정, 단가를 숫자로 읽지 못함, 기준가를 조회할 수 없는 상품(국내주식 외)이 전부 거부다. "확인하지 못했다"가
"괜찮다"로 바뀌는 지점을 남기지 않는다 — backend ``order_assist``의 규칙과 같다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# backend/config.py ``ORDER_MAX_PRICE_GAP_RATIO = _float_env("ORDER_MAX_PRICE_GAP_RATIO", 0.03)``와
# 같은 이름·같은 기본값이어야 한다. compose에서는 두 서비스가 같은 ./.env를 싣는다
# (docker-compose.yml의 env_file). 둘이 어긋나면 test_order_price_guard.py가 잡는다.
PRICE_GAP_RATIO_ENV = "ORDER_MAX_PRICE_GAP_RATIO"
DEFAULT_MAX_PRICE_GAP_RATIO = 0.03

# 오류 코드 — 에이전트가 붙잡는 식별자다.
ERROR_GAP_EXCEEDED = "kis_order_price_gap_exceeded"
ERROR_UNVERIFIABLE = "kis_order_price_unverifiable"

# 기준가를 조회할 수 있는 상품. 국내주식 현재가 조회(inquire_price) 계약만 확인돼 있다.
# 다른 상품은 기준가를 모르므로 단가 있는 주문을 보내지 않는다(fail-closed).
_QUOTABLE_TOOL = "domestic_stock"

# 주문 **단가** 필드의 마지막 밑줄 마디. pii_guard._PARAM_KIND_BY_SUFFIX와 같은 마디 규약을
# 따르되 금액(*_AMT)과 현재가(*_PRPR)는 뺐다 — 둘 다 주문 단가가 아니라 현재가와 비교하면
# 뜻이 없다. 대소문자는 가리지 않는다(Kis Trading MCP 래퍼는 소문자 인자를 받는다).
_UNIT_PRICE_SUFFIXES = frozenset({"UNPR", "PRC"})

# 시세 조회 **조건** 입력의 접두사. ``FID_ORG_ADJ_PRC``(수정주가 반영 여부 0/1)처럼 마디가
# PRC로 끝나도 주문 단가가 아니다. upstream examples_llm 333개 TR에서 ``*_PRC`` 인자는 전부
# 이 플래그였고 주문 TR에는 FID_ 필드가 없다. 빼지 않으면 읽기 전용 접두사 밖의 시세 조회
# (``investor_trade_by_stock_daily``)가 종목코드 없는 주문으로 오인돼 막힌다(PR #379 리뷰 후속).
_QUOTE_CONDITION_PREFIX = "FID_"

# 원주문을 가리키는 정정·취소 호출. 국내주식·선물옵션 ``order_rvsecncl``의 정정취소구분코드는
# ``01``=정정, ``02``=취소다. 해외주식 ``order_resv``처럼 같은 필드를 ``00``(신규)으로 쓰는 TR이
# 있어, 취소는 원주문번호와 ``02``가 **함께** 있을 때만 인정한다.
_ORIGINAL_ORDER_FIELD = "ORGN_ODNO"
_CANCEL_CODE_FIELD = "RVSE_CNCL_DVSN_CD"
_CANCEL_CODE = "02"

# 주문 params에 env_dv가 없을 때 기준가 조회에 쓸 환경. Kis Trading MCP는 env_dv가 없으면
# "demo"로 실행하므로(tools/base.py의 ``params.pop("env_dv", "demo")``) 같은 값을 써야 주문과
# 조회가 같은 환경·같은 인증을 탄다. "real"로 두면 모의투자만 설정한 배포에서 조회 인증이
# 실패해 모든 지정가 주문이 막힌다.
_DEFAULT_ENV_DV = "demo"

_PRICE_TEXT_RE = re.compile(r"\d+(?:\.\d+)?")
# 기준 현재가를 읽는 자리 — parse_current_price가 쓴다. Kis Trading MCP ``inquire_price``의
# 응답 모양은 실호출로 확인하지 못했고 저장소에 응답 샘플도 없어 키 탐색을 관대하게 뒀다.
# 모의투자 계정으로 실응답을 받아 테스트 픽스처로 고정하고 파서를 좁히는 일은 #381이다.
# 틀려도 결과는 거부(fail-closed)이지 우회가 아니다.
_CURRENT_PRICE_KEY = "stck_prpr"
_CURRENT_PRICE_TEXT_RE = re.compile(
    r"""["']?stck_prpr["']?\s*[:=]\s*["']?([\d,]+(?:\.\d+)?)""", re.IGNORECASE
)


def max_price_gap_ratio() -> float:
    """임계값을 읽는다. 해석 불가·비유한·음수면 기본값으로 되돌린다.

    ``backend/config.py``의 ``_float_env``와 같은 규칙이다. 호출할 때마다 읽는 이유:
    backend는 import 시점에 한 번 읽지만, NAT에서는 테스트가 env를 바꿔 끼울 수 있어야
    하고 매 주문 한 번 읽는 비용은 무시할 수 있다.
    """
    raw = os.environ.get(PRICE_GAP_RATIO_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_PRICE_GAP_RATIO
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    # inf나 음수를 통과시키면 한도가 사실상 사라지거나 모든 주문이 막힌다 — 설정 하나가
    # 조용히 기능을 끄지 않게 한다.
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "%s=%r 을(를) 해석할 수 없어 기본값 %s을(를) 씁니다.",
            PRICE_GAP_RATIO_ENV,
            raw,
            DEFAULT_MAX_PRICE_GAP_RATIO,
        )
        return DEFAULT_MAX_PRICE_GAP_RATIO
    return value


def price_gap_exceeds(price: float, reference: float, ratio: float) -> bool:
    """지정가 *price*가 현재가 *reference*에서 *ratio*보다 멀리 벗어났는가.

    ``backend/order_assist.py``의 ``evaluate_hard_limits``와 같은 식이다. *reference*는
    0보다 커야 한다 — :func:`parse_current_price`가 0 이하를 ``None``으로 거른다.
    """
    return abs(price - reference) / reference > ratio


def _as_price(value: Any) -> float | None:
    """단가 필드 값을 숫자로 읽는다. 값이 없으면 0, 읽지 못하면 ``None``."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        core = value.strip().replace(",", "")
        if not core:
            return 0.0
        if not _PRICE_TEXT_RE.fullmatch(core):
            return None
        number = float(core)
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


class PriceField(NamedTuple):
    field: str
    price: float


def order_unit_prices(params: Mapping[str, Any]) -> tuple[list[PriceField], list[str]]:
    """``(단가가 0보다 큰 단가 필드, 숫자로 읽지 못한 단가 필드)``.

    단가가 0이거나 비어 있는 필드는 어느 쪽에도 들지 않는다 — 시장가다. 시세 조회 조건
    (``FID_*``)은 단가 필드가 아니다(:data:`_QUOTE_CONDITION_PREFIX`).
    """
    prices: list[PriceField] = []
    unreadable: list[str] = []
    for field, value in params.items():
        name = str(field).strip().upper()
        if name.startswith(_QUOTE_CONDITION_PREFIX):
            continue
        if name.rsplit("_", 1)[-1] not in _UNIT_PRICE_SUFFIXES:
            continue
        number = _as_price(value)
        if number is None:
            unreadable.append(str(field))
        elif number > 0:
            prices.append(PriceField(str(field), number))
    return prices, unreadable


def _find_key(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        for name, value in node.items():
            if isinstance(name, str) and name.lower() == key:
                return value
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def parse_current_price(text: str) -> float | None:
    """현재가 조회 응답에서 ``stck_prpr``를 읽는다. 읽지 못하면 ``None``.

    Kis Trading MCP의 응답 모양을 이 저장소가 통제하지 못하므로 JSON이면 키를 어느 깊이에서든
    찾고, JSON이 아니면 ``stck_prpr: 68900`` 꼴의 텍스트를 찾는다. 오류 JSON
    (``{"error": ...}``)은 현재가가 아니다. 0 이하는 "공짜"가 아니라 "못 읽었다"다 —
    backend ``parse_current_price``와 같은 판정이다.

    어느 쪽이든 읽지 못하면 호출자는 주문을 보내지 않는다. 모양을 잘못 짐작한 대가는
    거부(관측 가능한 실패)이지 우회가 아니다.
    """
    if not text:
        return None
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict) and "error" in parsed:
        return None
    if parsed is not None:
        raw = _find_key(parsed, _CURRENT_PRICE_KEY)
    else:
        match = _CURRENT_PRICE_TEXT_RE.search(text)
        raw = match.group(1) if match else None
    if raw is None:
        return None
    number = _as_price(raw)
    if number is None or number <= 0:
        return None
    return number


def _param(params: Mapping[str, Any], name: str) -> str:
    """대소문자를 가리지 않고 파라미터 값을 문자열로 꺼낸다. 없으면 빈 문자열."""
    for key, value in params.items():
        if str(key).strip().upper() == name and value is not None:
            return str(value).strip()
    return ""


class GapRejection(NamedTuple):
    """주문을 보내지 않는 이유. 호출자가 오류 JSON으로 옮긴다.

    가격 원값은 싣지 않는다 — 오류 JSON은 Observation으로 LLM 컨텍스트에 재유입된다.
    원값은 로그에만 남긴다.
    """

    error: str
    reason: str
    fields: tuple[str, ...]
    hint: str


_UNVERIFIABLE_HINTS: dict[str, str] = {
    "unit_price_not_a_number": (
        "단가 필드 값을 숫자로 읽지 못해 주문을 보내지 않았습니다. 콤마·단위 없이 숫자만 "
        "넣으세요(시장가는 0)."
    ),
    "unsupported_product": (
        "지정가 괴리 검증은 국내주식(domestic_stock)만 지원합니다. 이 상품은 기준 현재가를 "
        "확인할 수 없어 단가가 있는 주문을 이 도구로 보낼 수 없습니다. 재시도하지 말고 "
        "사용자에게 안내하세요."
    ),
    "stock_code_missing": (
        "단가가 있는 주문에는 종목코드(PDNO, 6자리)가 필요합니다. 기준 현재가를 조회할 수 없어 "
        "주문을 보내지 않았습니다. 종목명(stock_name)만으로는 검증할 수 없으니 PDNO에 종목코드를 "
        "넣어 다시 시도하세요."
    ),
    "amend_stock_code_unknown": (
        "이 정정 주문에는 원주문의 종목코드가 없어(국내주식 정정취소 TR에는 PDNO 필드가 "
        "없습니다) 새 단가를 현재가와 대조할 수 없었고, 그래서 보내지 않았습니다. 채팅에서는 "
        "가격 정정을 할 수 없습니다. PDNO를 추가해 재시도하지 말고, 원주문을 취소"
        "(RVSE_CNCL_DVSN_CD=02)한 뒤 새 단가로 새 주문을 내거나 사용자에게 그렇게 안내하세요."
    ),
    "current_price_unavailable": (
        "주문 직전 이 종목의 현재가를 확인하지 못해 단가를 검증할 수 없었고, 검증할 수 없는 "
        "주문은 보내지 않습니다. 같은 호출을 바로 반복하지 말고, 잠시 후 다시 시도하거나 "
        "사용자에게 현재가 확인 실패로 주문하지 못했다고 알리세요."
    ),
}


def _gap_exceeded_hint(ratio: float) -> str:
    return (
        f"주문 단가가 현재가에서 허용 괴리({ratio:.1%})보다 멀어 주문을 보내지 않았습니다. "
        "잔고 조회 결과의 평가금액·매입금액 같은 다른 금액을 단가로 옮겨 적었을 가능성이 "
        "큽니다 — 금액 자리표시자는 겉모습이 같아 서로 구분되지 않습니다. inquire_price로 "
        "이 종목의 현재가를 다시 조회해 단가를 확인하세요. 사용자가 직접 괴리가 큰 지정가를 "
        "요청했다면 한도 때문에 주문할 수 없다고 안내하세요. 같은 단가로 재시도하지 마세요."
    )


QuoteFetcher = Callable[[str, str], Awaitable[str]]


def _is_cancel_of_original_order(params: Mapping[str, Any]) -> bool:
    """원주문번호와 취소 코드(``02``)가 함께 실린 호출인가.

    둘 중 하나라도 없으면 취소로 보지 않는다 — 이 면제가 가격 검사를 우회하는 통로가 되지
    않게 좁게 잡는다(:data:`_CANCEL_CODE` 위 주석).
    """
    return bool(_param(params, _ORIGINAL_ORDER_FIELD)) and (
        _param(params, _CANCEL_CODE_FIELD) == _CANCEL_CODE
    )


async def check_order_price_gap(
    *,
    tool_name: str,
    api_type: str,
    params: Mapping[str, Any],
    fetch_quote: QuoteFetcher,
) -> GapRejection | None:
    """주문 계열 호출 하나의 지정가 괴리를 판정한다. 보내도 되면 ``None``.

    호출자가 "주문일 수 있는 api_type"(읽기 전용 allowlist 밖)에만 부른다.
    *fetch_quote*는 ``(종목코드, env_dv)``를 받아 현재가 조회 응답 원문을 돌려준다.
    *api_type*은 로그에만 쓴다.
    """
    if _is_cancel_of_original_order(params):
        # 취소는 새 주문을 만들지 않는다. KIS 정정취소 TR은 취소에도 주문단가를 필수로 받아
        # (upstream order_rvsecncl) 원주문가가 실리는 것이 정상인데, 그것을 단가로 막으면 오주문을
        # 거둬들이는 경로가 막힌다 — 안전 방향으로도 역효과다(PR #379 리뷰).
        return None
    prices, unreadable = order_unit_prices(params)
    if unreadable:
        return GapRejection(
            ERROR_UNVERIFIABLE,
            "unit_price_not_a_number",
            tuple(unreadable),
            _UNVERIFIABLE_HINTS["unit_price_not_a_number"],
        )
    if not prices:
        return None  # 시장가이거나 단가가 없는 TR — backend도 괴리를 보지 않는다.

    fields = tuple(p.field for p in prices)

    def _unverifiable(reason: str) -> GapRejection:
        logger.warning(
            "지정가 괴리를 검증할 수 없어 주문을 보내지 않았습니다 — tool=%s api_type=%s "
            "reason=%s fields=%s",
            tool_name,
            api_type,
            reason,
            fields,
        )
        return GapRejection(ERROR_UNVERIFIABLE, reason, fields, _UNVERIFIABLE_HINTS[reason])

    if tool_name != _QUOTABLE_TOOL:
        return _unverifiable("unsupported_product")

    stock_code = _param(params, "PDNO")
    if not stock_code:
        # 원주문을 가리키는 호출(정정)에 "PDNO를 넣으라"고 안내하면 틀린다 — 국내주식 정정취소
        # TR에는 그 필드가 없어, MCP가 인자를 거부하거나 원주문과 무관하게 LLM이 고른 종목의
        # 현재가로 괴리를 재게 된다(PR #379 리뷰). 원주문 조회로 종목을 찾는 대신 거부한다 —
        # 그 조회 응답도 실측하지 않은 모양이라(#381) 종목을 잘못 짚으면 통과 쪽으로 무너진다.
        if _param(params, _ORIGINAL_ORDER_FIELD):
            return _unverifiable("amend_stock_code_unknown")
        return _unverifiable("stock_code_missing")

    env_dv = _param(params, "ENV_DV") or _DEFAULT_ENV_DV
    try:
        quote = await fetch_quote(stock_code, env_dv)
    except Exception:  # noqa: BLE001 — 어떤 실패든 "괜찮다"로 흐르면 안 된다.
        logger.exception("지정가 괴리 검증용 현재가 조회가 예외로 끝났습니다 (PDNO=%s)", stock_code)
        return _unverifiable("current_price_unavailable")

    reference = parse_current_price(quote)
    if reference is None:
        logger.warning(
            "현재가 조회 응답에서 stck_prpr를 읽지 못했습니다 (PDNO=%s): %s",
            stock_code,
            (quote or "")[:300],
        )
        return _unverifiable("current_price_unavailable")

    ratio = max_price_gap_ratio()
    exceeded = tuple(p for p in prices if price_gap_exceeds(p.price, reference, ratio))
    if exceeded:
        logger.warning(
            "지정가 괴리 초과로 주문을 보내지 않았습니다 — tool=%s api_type=%s PDNO=%s "
            "현재가=%s 허용=%s 단가=%s",
            tool_name,
            api_type,
            stock_code,
            reference,
            ratio,
            [(p.field, p.price) for p in exceeded],
        )
        return GapRejection(
            ERROR_GAP_EXCEEDED,
            "limit_price_gap_exceeded",
            tuple(p.field for p in exceeded),
            _gap_exceeded_hint(ratio),
        )
    return None
