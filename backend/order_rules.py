"""스케줄러 룰 트리거 (#314).

#299(PR #302)가 ``/advise``로 수동 주문 보조를 넣으면서 자료구조는 룰 트리거를 이미
수용하도록 만들어 뒀다(``ProposalTrigger.source``/``rule_id``/``trigger_signal``, 종목코드+룰
단위 냉각 키). 이 모듈은 그 호출부다 — **어떤 감시 신호가 자동 제안을 부르는가**만 정하고,
제안·한도 판정·검증·대기 주문 저장은 전부 :func:`backend.order_assist.run_order_assist`가
그대로 한다. 판정 규칙을 여기에 한 줄이라도 복제하지 않는다.

#299의 세 원칙은 자동 트리거에서도 그대로다. 특히 **확정 버튼 없이는 주문이 나가지 않는다** —
자동 트리거는 사용자가 화면을 보고 있지 않을 때 도는 경로라, 여기서 확정을 생략하면 #299가
세운 안전장치가 통째로 무의미해진다. 이 모듈이 만드는 것은 ``ProposalTrigger`` 하나까지다.

세 가지를 이 파일에서 결정했다.

**1. 룰 정의 위치 — 대상은 관심 종목·보유 종목, 조건은 ``.env``의 전역 룰 하나.**
별도 테이블을 두면 룰 CRUD 명령 표면(추가·삭제·조회)이 따라와야 하는데, 지금 필요한 것은
"자동 제안을 켤지"와 "어느 신호에서 켤지" 두 가지뿐이다. 종목마다 다른 조건이 필요해지면
그때 테이블로 옮긴다 — 그 전까지 테이블은 값이 하나뿐인 스키마 마이그레이션 비용이다.
대상 범위를 관심 종목·보유 종목으로 좁히는 이유는 :func:`in_rule_scope` 주석에 있다.

**2. 거부·충돌 통지 — ``/alerts`` 모드를 따른다.**
사용자가 요청한 적 없는 제안이라 거부를 매번 알리면 소음이다. ``all``에서만 보내고
``urgent``에서는 로그로만 남긴다. 다만 **승인은 모드와 무관하게 반드시 보낸다** — 확정 버튼이
필요할 뿐 아니라, 대기 주문 슬롯은 ``/buy``와 공유하는 하나뿐이라 조용히 잡아 두면 사용자가
친 ``/buy``가 영문 모를 충돌로 막힌다. ``off``는 트리거 자체를 돌리지 않는다(:func:`should_run`).

**3. ``trigger_signal``에는 수치를 넣지 않는다.**
``/v1/propose-order``의 환각 게이트(``fe_branch``)는 ``chat_request.messages``에 이미 등장한
수치를 화이트리스트로 삼는데, 이 엔드포인트는 프롬프트 하나가 곧 transcript 전체다. 즉
프롬프트 본문에 실린 수치는 도구 호출 없이도 게이트를 통과한다. :func:`build_trigger_signal`이
돌려주는 것은 신호 본문이 아니라 **"무엇을 확인하라"는 코드 리터럴 지시 문구**이고, 숫자를
한 자도 담지 않는다(``test_order_rules``가 이 계약을 검사한다).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .config import (
    ORDER_RULE_SOURCES,
    ORDER_RULE_TRIGGER_ENABLED,
    ORDER_RULE_URGENCY_LEVELS,
)

logger = logging.getLogger(__name__)

# 냉각 키(RedisKeys.order_proposal_cooldown)의 룰 자리에 들어가는 값. 수동 요청의
# ``manual``과 갈라지므로 ``/advise``와 자동 제안이 서로의 냉각을 잡아먹지 않는다.
#
# 소스(news/disclosure)마다 다른 id를 주지 않는 것은 의도다. 냉각이 막으려는 것은
# "같은 종목에 대한 반복 제안"이라, 같은 종목의 공시 신호와 뉴스 신호가 한 시간 안에
# 각각 제안을 부르면 냉각이 있으나 마나다.
RULE_ID = "signal_urgency"

# urgency 축의 순서. 한 주기에 여러 종목이 룰을 만족했을 때 어느 것을 고를지에만 쓴다.
# schemas.UrgencyLevel의 리터럴과 짝을 이룬다 — 별칭에 값이 늘면 여기도 늘려야 하고,
# 빠지면 _URGENCY_RANK.get의 기본값 -1로 떨어져 항상 뒤로 밀린다(조용한 오작동이
# 아니라 "덜 급한 것으로 취급"이므로 안전한 방향이다).
_URGENCY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}

# 제안 프롬프트에 실리는 지시 문구. **수치를 넣지 않는다** — 모듈 docstring 3번 참고.
# 신호 본문(current_signal)을 그대로 싣지 않는 것도 같은 이유다. 본문에는 주가·거래량이
# 그대로 들어 있다.
_TRIGGER_INSTRUCTIONS = {
    "news": "뉴스 감시에서 자동으로 트리거되었습니다. 현재가·수급·최신 뉴스를 도구로 직접 확인한 뒤 판단하세요.",
    "disclosure": "공시 감시에서 자동으로 트리거되었습니다. 현재가·수급·해당 공시 내용을 도구로 직접 확인한 뒤 판단하세요.",
}
_DEFAULT_TRIGGER_INSTRUCTION = (
    "자동 감시에서 트리거되었습니다. 현재가·수급·뉴스를 도구로 직접 확인한 뒤 판단하세요."
)

# 자동 제안 메시지 머리말. 사용자가 요청한 적 없는 메시지라, 왜 이게 왔고 무엇을 하지
# 않으면 아무 일도 없는지를 첫 줄에서 말한다.
AUTO_PROPOSAL_NOTICE = (
    "🤖 자동 제안 — 감시 신호로 만들어진 주문 제안입니다. 확정하지 않으면 주문은 나가지 않습니다."
)


@dataclass(frozen=True)
class OrderAssistRule:
    """자동 제안을 부르는 조건 하나.

    ``sources``와 ``urgency_levels``는 둘 다 AND다 — 지정한 소스의 신호가, 지정한 긴급도로
    분석됐을 때만 트리거된다.
    """

    rule_id: str
    sources: frozenset[str]
    urgency_levels: frozenset[str]


@dataclass(frozen=True)
class RuleMatch:
    """룰을 만족한 신호 한 건. 여기에도 수치는 담지 않는다."""

    rule_id: str
    stock: str
    source: str
    urgency: str


def load_rule(
    *,
    enabled: bool = ORDER_RULE_TRIGGER_ENABLED,
    sources: frozenset[str] = ORDER_RULE_SOURCES,
    urgency_levels: frozenset[str] = ORDER_RULE_URGENCY_LEVELS,
) -> OrderAssistRule | None:
    """설정된 룰을 만든다. 자동 제안이 꺼져 있거나 조건이 비면 ``None``.

    조건 집합이 비면 켜 둔 것과 같은 효과를 노린 설정 실수(``ORDER_RULE_SOURCES=``)일
    가능성이 높은데, 빈 집합을 "전부 허용"으로 읽으면 그 실수가 자동 주문 제안을 가장
    넓게 여는 방향으로 작동한다. fail-closed로 끈다.
    """
    if not enabled:
        return None
    if not sources or not urgency_levels:
        logger.warning(
            "자동 제안이 켜져 있지만 트리거 조건이 비어 있어 룰을 만들지 않습니다 "
            "(sources=%s, urgency=%s).",
            sorted(sources),
            sorted(urgency_levels),
        )
        return None
    return OrderAssistRule(
        rule_id=RULE_ID, sources=frozenset(sources), urgency_levels=frozenset(urgency_levels)
    )


def in_rule_scope(stock: str, owned: Iterable[str], watchlist: Iterable[str]) -> bool:
    """이 종목에 자동 제안을 걸어도 되는가.

    보유 종목과 관심 종목만 대상이다. 감시 루프는 둘 다 비었을 때
    ``DEFAULT_MONITOR_STOCKS``로 떨어지는데, 그건 사용자가 고른 종목이 아니라 감시 공백을
    메우는 기본값이다. 거기에 자동 제안을 걸면 관심을 표한 적도 없는 종목의 확정 버튼이
    사용자에게 뜬다 — 수동 ``/advise``에는 없던 종류의 사고다.
    """
    return stock in set(owned) or stock in set(watchlist)


def match_rule(
    rule: OrderAssistRule | None,
    *,
    stock: str,
    source: str,
    analysis_data: Mapping[str, Any] | None,
) -> RuleMatch | None:
    """분석 결과가 룰을 만족하면 :class:`RuleMatch`, 아니면 ``None``.

    순수 함수다. 여기서 무엇도 보내지 않고 무엇도 저장하지 않는다.
    """
    if rule is None or not analysis_data:
        return None
    if source not in rule.sources:
        return None
    raw_urgency = analysis_data.get("urgency")
    urgency = raw_urgency.strip().lower() if isinstance(raw_urgency, str) else ""
    if urgency not in rule.urgency_levels:
        return None
    return RuleMatch(rule_id=rule.rule_id, stock=stock, source=source, urgency=urgency)


def most_urgent(matches: Iterable[RuleMatch]) -> RuleMatch | None:
    """한 주기에 여러 건이 잡혔을 때 제안을 태울 한 건을 고른다.

    한 주기에 한 건만 도는 이유는 자원 절약이 아니라 구조다 — 대기 주문 슬롯은 chat당
    하나뿐이라, 두 번째 승인은 저장되지 못하고 ``conflict``로 끝난다. 그래서 두 번째부터는
    제안 왕복(기본 120초)과 검증 왕복을 태우고도 남는 것이 없다.

    동률이면 감시 순서(보유 → 관심, 그 안에서는 등록 순)의 앞선 것이 남는다. ``max``가 첫
    최댓값을 돌려주므로 같은 입력에는 같은 결과가 나온다.
    """
    ranked = list(matches)
    if not ranked:
        return None
    return max(ranked, key=lambda m: _URGENCY_RANK.get(m.urgency, -1))


def build_trigger_signal(source: str) -> str:
    """제안 프롬프트에 실을 지시 문구. **수치를 담지 않는다** (모듈 docstring 3번)."""
    return _TRIGGER_INSTRUCTIONS.get(source, _DEFAULT_TRIGGER_INSTRUCTION)


def should_run(alert_mode: str) -> bool:
    """이 알림 모드에서 자동 트리거를 돌려도 되는가.

    ``off``는 돌리지 않는다. 승인 결과는 모드와 무관하게 보내야 하는데(확정 버튼이 필요하고,
    대기 주문 슬롯을 조용히 잡으면 사용자의 ``/buy``가 막힌다) 알림을 끈 사용자에게 확정
    버튼을 보내는 것은 그 설정을 정면으로 어기는 일이다. 두 요구가 양립하지 않으므로 제안
    자체를 만들지 않는다.
    """
    return alert_mode != "off"


def should_report_rejection(alert_mode: str) -> bool:
    """거부·충돌을 사용자에게 보낼 것인가.

    ``all``에서만 보낸다. 수동 ``/advise``의 거부는 사용자가 방금 친 명령의 답이라 반드시
    보내야 하지만, 자동 트리거의 거부는 사용자가 존재조차 모르는 시도의 결과다. 감시 주기가
    10분이라 이걸 매번 보내면 "거부했습니다"가 하루 수십 건 쌓인다.
    """
    return alert_mode == "all"


def format_auto_message(message: str) -> str:
    """자동 제안 메시지에 머리말을 붙인다."""
    return f"{AUTO_PROPOSAL_NOTICE}\n\n{message}"
