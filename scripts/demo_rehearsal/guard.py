"""리허설 중 주문을 막는 판정 — 주문으로 읽히는 호출이면 실행 전에 중단한다.

리허설 도구는 실제 사용자 경로(NAT 워크플로·backend 핸들러)를 그대로 돌린다. 그 경로에는
주문 권한이 없어야 정상이지만(조회 전용 래퍼, /buy·/sell 확정 버튼 경로), 리허설이 그 전제를
믿고만 있으면 전제가 깨진 날 모의계좌에 주문이 나간다. 그래서 MCP 호출 지점마다 이 판정을
**원래 호출보다 먼저** 걸고, 걸리면 :class:`OrderGuardTripped`를 던져 리허설 전체를 멈춘다.

판정은 허용 목록이 아니라 거부 목록이다. 조회 TR은 계속 늘어나고(순위·시세·재무 …) 리허설이
그 목록을 따라가지 못하면 정상 조회까지 막혀 리허설 자체가 돌지 않는다. 대신 이름에 주문
표지(``order``·``rvsecncl``)가 있으면 조회 접두어(``inquire_``·``search_``·``find_``)로 시작하지
않는 한 전부 주문으로 본다 — ``inquire_psbl_order``(매수가능조회)는 조회, ``order_cash``·
``order_resv_rvsecncl``·처음 보는 ``order_*``는 주문이다.

표준 라이브러리만 쓴다(CI 단위 테스트 대상).
"""

from __future__ import annotations

import re

# stdio mcp/trading의 주문 도구와 backend 주문 게이트웨이가 부르는 이름.
ORDER_TOOL_NAMES = frozenset({"place_order", "order_cash", "order_credit", "cancel_order", "modify_order"})

_READ_PREFIXES = ("inquire_", "search_", "find_")
_ORDER_MARK = re.compile(r"order|rvsecncl", re.IGNORECASE)


class OrderGuardTripped(BaseException):
    """주문으로 읽히는 호출을 만났다. ``Exception``이 아니라 ``BaseException``인 이유:

    에이전트·도구 래퍼 곳곳의 ``except Exception``이 이 예외를 오류 JSON으로 바꿔 삼키면
    리허설이 멈추지 않고 다음 도구 호출로 넘어간다. 주문 시도는 삼켜도 되는 오류가 아니다.
    """


def is_order_api_type(api_type: str | None) -> bool:
    """KIS ``api_type``이 주문(또는 정정·취소)인가."""
    lower = (api_type or "").strip().lower()
    if not lower or lower.startswith(_READ_PREFIXES):
        return False
    return _ORDER_MARK.search(lower) is not None


def is_order_call(tool_name: str | None, api_type: str | None = None) -> bool:
    """MCP 도구 이름·``api_type`` 조합이 주문인가."""
    name = (tool_name or "").strip().lower()
    if name in ORDER_TOOL_NAMES:
        return True
    if name and not name.startswith(_READ_PREFIXES) and _ORDER_MARK.search(name) and name != "domestic_stock":
        # get_today_daily_orders처럼 조회 도구 이름에도 order가 들어간다 — get_ 접두어는 조회다.
        if not name.startswith("get_"):
            return True
    return is_order_api_type(api_type)
