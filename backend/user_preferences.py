"""텔레그램 사용자의 투자 성향 (#397) — NAT 사용자 선호 메모리의 backend 쪽 절반.

성향 값은 backend가 저장하지 않는다. 저장소는 NAT 프로세스 안의 mem0 로컬 메모리 하나이고
(``finus_nat/src/nat_finus_nat/user_memory.py``), backend는 ``POST /v1/user-preferences``로 읽고 쓴다.
설명 수준(/level)처럼 redis에 두지 않는 이유는 성향의 소비자가 NAT의 추천 브랜치뿐이라서다 —
backend에 사본을 두면 두 저장소가 어긋날 수 있고, NAT이 요청마다 backend를 되묻는 경로가 생긴다.

## 사용자 식별자

이 봇은 채팅 하나만 받는다(``notifier.chat_id``). 그래서 사용자 식별자는 ``telegram:<chat_id>``이고,
이 값은 채팅 폴백이 NAT에 보내는 대화 스레드 id와 같은 문자열이다. :func:`nat_user_id_for_conversation`이
그 형태의 스레드에만 ``x-user-id``를 싣게 한다 — 실적 스레드(``telegram:<id>:earnings:…``)나 스케줄러
(``morning-briefing:…``)는 사용자 발화가 아니므로 성향을 적용하지 않는다.
"""
import re
from typing import Any

import httpx

from .config import NAT_BASE_URL

RISK_CONSERVATIVE = "conservative"
RISK_AGGRESSIVE = "aggressive"
RISK_PROFILES = (RISK_CONSERVATIVE, RISK_AGGRESSIVE)
RISK_PROFILE_LABELS = {RISK_CONSERVATIVE: "안정형", RISK_AGGRESSIVE: "공격형"}
# /risk의 인자와 버튼 콜백 데이터가 같은 표를 탄다. "해제"는 None(저장값 삭제)으로 간다.
RISK_CLEAR = "clear"
_RISK_ALIASES = {
    "안정형": RISK_CONSERVATIVE,
    "안정": RISK_CONSERVATIVE,
    "보수형": RISK_CONSERVATIVE,
    RISK_CONSERVATIVE: RISK_CONSERVATIVE,
    "공격형": RISK_AGGRESSIVE,
    "공격": RISK_AGGRESSIVE,
    RISK_AGGRESSIVE: RISK_AGGRESSIVE,
    "해제": RISK_CLEAR,
    "삭제": RISK_CLEAR,
    "설정안함": RISK_CLEAR,
    RISK_CLEAR: RISK_CLEAR,
}

USER_PREFERENCES_TIMEOUT_SECONDS = 10.0
_TELEGRAM_USER_THREAD_RE = re.compile(r"^telegram:-?\d+$")


class UserMemoryDisabled(RuntimeError):
    """NAT이 메모리 없이(FINUS_MEM0_ENABLED=0) 떠 있다."""


def normalize_risk_choice(raw: str) -> str | None:
    """사용자 입력·콜백 데이터 → ``conservative``/``aggressive``/``clear``. 모르는 값은 None."""
    return _RISK_ALIASES.get(raw.strip().lower().replace(" ", ""))


def telegram_user_id(chat_id: str) -> str:
    return f"telegram:{str(chat_id).strip()}"


def nat_user_id_for_conversation(conversation_id: str | None) -> str | None:
    """채팅 폴백 스레드(``telegram:<chat_id>``)일 때만 그 값을 사용자 식별자로 돌려준다."""
    if conversation_id and _TELEGRAM_USER_THREAD_RE.fullmatch(conversation_id.strip()):
        return conversation_id.strip()
    return None


async def request_risk_profile(
    user_id: str,
    choice: str | None = None,
    *,
    timeout: float = USER_PREFERENCES_TIMEOUT_SECONDS,
) -> str | None:
    """*choice*가 None이면 조회, ``clear``면 해제, 성향 값이면 저장. 적용 뒤의 성향을 돌려준다.

    NAT이 메모리 없이 떠 있으면 :class:`UserMemoryDisabled`. 그 외 실패는 예외 그대로 올린다 —
    저장했다고 답했는데 저장되지 않은 상태가 가장 나쁘므로 호출부가 실패를 사용자에게 알린다.
    """
    payload: dict[str, Any] = {"user_id": user_id, "action": "get"}
    if choice == RISK_CLEAR:
        payload["action"] = "clear"
    elif choice is not None:
        if choice not in RISK_PROFILES:
            raise ValueError(f"지원하지 않는 투자 성향입니다: {choice!r}")
        payload.update(action="set", risk_profile=choice)

    url = f"{NAT_BASE_URL}/v1/user-preferences"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        response = await client.post(url, headers={"Content-Type": "application/json"}, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"NAT /v1/user-preferences 응답 {response.status_code}")
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        raise RuntimeError("NAT /v1/user-preferences 응답 형식 오류")
    if not body["enabled"]:
        raise UserMemoryDisabled("사용자 메모리가 꺼져 있습니다.")
    profile = body.get("risk_profile")
    if profile is not None and profile not in RISK_PROFILES:
        raise RuntimeError("NAT /v1/user-preferences 응답에 알 수 없는 성향 값")
    return profile
