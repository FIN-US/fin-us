"""마스킹 매핑의 수명을 `llm_chat` 지역 변수에서 **요청 범위**로 끌어올린다 (#354).

`llm_chat`은 요청마다 `mask_pii(user_msg)`를 돌리고, 그 매핑을 자기 지역 변수로만
들고 있다가 응답을 역치환한 뒤 버렸다. 그래서 사용자가 발화에 적은 금액·수량은
**매매일지에 저장할 수 없었다**:

    사용자: "300만원 벌었어, 일지 써줘"
      → backend `llm_chat`이 NAT을 부르기 전에 마스킹      ... `<AMOUNT_be3f1a_1>`  (#230)
      → LLM이 그 발화를 보고 일지 본문을 쓴다              ... 본문에 backend 토큰이 실린다
      → NAT `finus_save_diary`는 낯선 scope라 통과시킨다   ... "backend가 판정할 몫"
      → `POST /api/v1/db/diary`에는 되돌릴 매핑이 없다     ... 지역 변수와 함께 사라졌다

#339(PR #351)는 이 지점에서 **저장을 거부**해 손상만 막았다 — 조용한 원본 소실을
시끄러운 실패로 바꿨을 뿐, 기능은 회복되지 않았다. 사용자가 값을 다시 적어도 새 scope로
다시 마스킹돼 똑같이 거부됐다. 이 모듈이 그 숙제(방향 1)를 닫는다.

## 왜 ContextVar가 아니라 프로세스 등록소인가

이슈는 "ContextVar 등"으로 요청 범위 승격을 제안했지만, **ContextVar만으로는 이 문제가
풀리지 않는다.** 저장은 같은 요청 안에서 일어나는 함수 호출이 아니라 **별개의 HTTP
요청**이다:

    [요청 A] 프론트/텔레그램 → backend `llm_chat` → (NAT 왕복 대기)
                                                     [요청 B] NAT → backend POST /api/v1/db/diary

요청 B는 uvicorn이 새로 만든 asyncio 태스크에서 처리되므로 요청 A의 컨텍스트를
물려받지 않는다. 요청 A가 `ContextVar.set()`으로 심은 매핑은 요청 B에서 보이지 않는다.

그래서 **자리표시자에 이미 실려 있는 scope**(`<AMOUNT_be3f1a_1>`의 `be3f1a`)를 키로
쓰는 프로세스 등록소를 둔다. 상관관계 식별자를 NAT을 관통해 새로 배선할 필요가 없다 —
저장 요청이 들고 오는 본문 자체가 어느 매핑으로 되돌려야 하는지 말해 준다.

## 동시 호출이 서로의 매핑을 덮지 않는 이유

`mask_pii` 호출마다 scope를 CSPRNG로 새로 뽑는다(`pii_mask._Counter`). **scope가
충돌하지 않는 한** 동시에 도는 `llm_chat` 둘은 서로 다른 키에 등록되고, 한쪽의
`<AMOUNT_aaa111_1>`은 다른 쪽 매핑을 조회할 수 없다. 종전에 매핑을 모듈 전역 dict
하나로 두지 않은 이유가 그대로 지켜진다 — 전역인 것은 **scope별 칸막이가 있는
등록소**이고, 매핑 자체는 여전히 호출마다 별개다.
(회귀: `test_pii_registry.py::TestConcurrentRequests`)

충돌하면(24비트, 16^6 ≈ 1,678만 가지) 나중 등록이 앞선 것을 덮고 먼저 끝나는 쪽의
`finally`가 그 키를 지운다. **그때 나오는 것은 틀린 값이 아니라 저장 거부다** — 조회는
scope와 자리표시자 전체 문자열을 함께 맞춰야 성공하므로(`restore_live_placeholders`),
남의 매핑을 맞히면 번호가 없어 `unrestorable`로 떨어지고 호출자가 422로 거부한다.
즉 충돌의 결과는 이 모듈이 이미 감수한 실패 모드와 같은 것이고, 조용한 오답이 아니다.

## 수명 — 요청 범위, 그 이상은 아니다

:func:`active_mapping`이 컨텍스트 매니저인 것이 계약이다. `llm_chat`은 provider 호출을
이 블록으로 감싸고, 블록을 벗어나면 `finally`에서 등록이 지워진다. 즉 등록소에 있는
것은 **지금 응답을 기다리고 있는 요청들의 매핑**뿐이고, TTL이나 만료 청소가 필요 없다.

수명을 늘리는 방향(예: 대화별 캐시)은 일부러 택하지 않았다. 매핑은 사용자 발화의
평문 금액·계좌번호이므로, 살아 있는 시간이 길어지는 만큼 프로세스 메모리에 평문이
남는 창이 넓어진다. 되돌리기가 필요한 창은 "그 발화를 처리하는 왕복" 하나로 충분하다.

## 남는 것

- **워커가 여럿이면 성립하지 않는다.** 등록소는 프로세스 안에 있다. 지금 backend는
  단일 uvicorn 프로세스로 뜨므로(`backend/Dockerfile`의 CMD에 `--workers` 없음)
  요청 A와 요청 B가 같은 프로세스에 들어온다. `--workers`를 늘리면 요청 B가 다른
  워커에 배정될 수 있고, 그때는 등록소가 비어 있어 저장이 거부된다 — 조용한 손상이
  아니라 실패이므로 안전 방향이지만, 워커를 늘릴 때 함께 봐야 하는 자리다
  (외부 저장소로 옮기려면 Redis가 이미 있다: `backend/redis_state.py`).
- **되돌리기는 살아 있는 scope에만 걸린다.** 이전 턴 자리표시자(NAT SQLite 히스토리에서
  올라온 것)나 LLM이 지어낸 번호는 등록소에 없다. 그 값은 이 프로세스 어디에도 없으므로
  #339의 판정이 그대로 유지된다 — 호출자가 저장을 거부한다.
- **되돌리기는 인증을 켠 배포를 전제로 한다.** `create_db_diary`는 호출자를 구분하지 않고
  본문의 자리표시자만 보고 되돌린 뒤 그 평문을 응답 `data`에 실어 돌려준다. 즉 살아 있는
  scope를 맞히는 요청은 **다른 사용자 발화의 평문을 읽는 오라클**이 된다. 유일한 보호는
  `main.py`의 `require_api_key`인데, `FINUS_API_KEY`가 비면 통과가 기본값이다
  (`_log_api_auth_state`가 그 상태를 경고로 올린다). 실전 공격은 아니다 — scope는 24비트고
  수명이 왕복 한 번뿐이라 창이 극히 좁다 — 지만, 이 모듈 이전에는 **없던 정보 흐름**이므로
  무인증 배포에서 새로 생기는 노출로 적어 둔다.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .pii_mask import _FALLBACK_LABEL

logger = logging.getLogger(__name__)

# 자리표시자 형식은 pii_mask._PLACEHOLDER_RE와 같은 것을 매치하지만 **scope를 캡처**한다.
# 등록소 조회 키가 scope이기 때문이다. 얼터네이션을 같은 출처(_FALLBACK_LABEL)에서
# 조립하므로 종류가 늘어도 두 정규식이 함께 따라간다 — 둘이 정말 같은 것을 매치하는지는
# test_pii_registry.py가 고정한다. (finus_nat/pii_guard.py의 _SCOPED_PLACEHOLDER_RE와
# 같은 이유의 같은 정규식이다. 그쪽은 NAT 프로세스에 있어 import로 공유할 수 없다 —
# pii_mask.py를 바이트 복제한 것과 같은 제약이다.)
_SCOPED_PLACEHOLDER_RE = re.compile(
    r"<(" + "|".join(_FALLBACK_LABEL) + r")_([0-9a-f]{6})_(\d+)>"
)

# {scope: {자리표시자: 원값}}. 값은 llm_chat이 들고 있는 매핑에서 갈라 담은 dict다.
_REGISTRY: dict[str, dict[str, str]] = {}

# _REGISTRY 자체의 뮤테이션을 감싼다. 단일 이벤트 루프 안에서는 await 없는 dict 조작이라
# 락이 없어도 되지만, 이 등록소를 읽는 쪽(create_db_diary)과 쓰는 쪽(llm_chat)이 언제나
# 같은 스레드라는 보장이 코드에 없다 — 스케줄러는 to_thread를 쓰고(backend/scheduler.py),
# FastAPI는 동기 라우트를 스레드풀에서 돌린다. 경합이 실제로 생기면 증상은
# "저장이 조용히 거부됨"이라 원인을 되짚기 어려운 종류이므로, 값싼 락으로 미리 닫는다.
_LOCK = threading.Lock()

# 등록소에 이만큼이 동시에 살아 있으면 수명 관리가 새고 있다는 신호다. active_mapping의
# finally가 항상 지우므로 정상 동작에서는 "지금 LLM 응답을 기다리는 요청 수"를 넘지
# 않는다 — 그 수가 64를 넘는 부하라면 등록소 누수든 아니든 알아야 하는 상태다.
_LEAK_WARN_SIZE = 64

# 위 경고를 **한 번 넘을 때 한 줄만** 남기기 위한 걸쇠. 매 진입마다 크기를 재서 그대로
# 경고하면, 임계를 넘긴 부하 구간에서 요청당 한 줄씩 쌓여 정작 누수 신호가 자기 로그에
# 묻힌다. 경계에서 63↔64를 오가는 것만으로도 같은 일이 벌어지므로 "넘는 순간"을 재는
# 것으로는 부족하고, 등록소가 **완전히 빌 때** 걸쇠를 푼다 — 한 차례의 과부하 구간에
# 정확히 한 줄이 남고, 상황이 해소된 뒤 다시 나면 그때 다시 한 줄이 남는다.
_leak_warned = False


def _scope_of(placeholder: str) -> str | None:
    """자리표시자에서 scope를 뽑는다. 형식에 맞지 않으면 None."""
    match = _SCOPED_PLACEHOLDER_RE.fullmatch(placeholder)
    return match.group(2) if match else None


@contextmanager
def active_mapping(mapping: dict[str, str]) -> Iterator[None]:
    """*mapping*을 그 scope로 등록소에 올리고, 블록을 벗어나면 지운다.

    `llm_chat`이 provider 호출을 이 블록으로 감싼다. 블록 안에서만 다른 요청
    (`create_db_diary`)이 :func:`restore_live_placeholders`로 그 매핑을 조회할 수 있다.

    빈 매핑은 등록하지 않는다 — 마스킹된 것이 없으면 되돌릴 것도 없다. 그래야 마스킹
    대상이 없는 대다수 호출이 등록소를 건드리지 않고 지나간다.

    한 매핑의 자리표시자는 모두 같은 scope를 갖는다(`pii_mask._Counter`가 호출당 하나를
    뽑는다). 그런데도 scope별로 갈라 등록하는 것은 그 불변을 이 모듈이 **가정하지 않게**
    하려는 것이다 — 나중에 매핑을 합치는 경로가 생기면 여기서 조용히 한쪽이 사라지는
    대신 양쪽이 모두 등록된다.
    """
    grouped: dict[str, dict[str, str]] = {}
    for placeholder, value in mapping.items():
        scope = _scope_of(placeholder)
        if scope is None:
            # pii_mask가 만든 자리표시자는 전부 이 형식이다. 도달한다면 자리표시자
            # 규약이 갈린 것이므로 조용히 넘기지 않는다.
            logger.error(
                "자리표시자 형식을 알아볼 수 없어 요청 범위 등록에서 제외했습니다 — "
                "pii_mask와 pii_registry의 자리표시자 규약이 어긋났을 수 있습니다."
            )
            continue
        grouped.setdefault(scope, {})[placeholder] = value

    if not grouped:
        yield
        return

    global _leak_warned

    with _LOCK:
        for scope, entries in grouped.items():
            _REGISTRY[scope] = entries
        size = len(_REGISTRY)
        # 걸쇠 판정은 락 안에서 한다. 밖으로 빼면 임계를 함께 넘은 요청 여럿이 모두
        # "아직 안 남겼다"를 읽고 같은 줄을 중복해서 남긴다 — 억제하려던 것이 그대로다.
        should_warn = size >= _LEAK_WARN_SIZE and not _leak_warned
        if should_warn:
            _leak_warned = True
    if should_warn:
        logger.warning(
            "요청 범위 마스킹 등록소에 매핑 %d개가 동시에 살아 있습니다 — "
            "active_mapping을 벗어나지 못한 요청이 쌓이고 있는지 확인하세요. "
            "이 경고는 등록소가 다시 빌 때까지 한 번만 남습니다.",
            size,
        )
    try:
        yield
    finally:
        with _LOCK:
            for scope in grouped:
                _REGISTRY.pop(scope, None)
            if not _REGISTRY:
                _leak_warned = False


def restore_live_placeholders(text: str) -> tuple[str, list[str]]:
    """*text*의 자리표시자 중 **살아 있는 요청의 것만** 원값으로 되돌린다.

    돌려주는 것은 ``(되돌린 텍스트, 되돌리지 못한 자리표시자 목록)``이다. 목록이 비어
    있지 않으면 호출자가 판정해야 한다 — `create_db_diary`는 저장을 거부한다(#339의
    판정을 그대로 유지한다).

    되돌리지 못하는 것은 두 종류이고, 저장 관점에서 차이가 없다 — **둘 다 원값이 이
    프로세스 어디에도 없다**:

    - **등록소에 없는 scope** — 이전 턴의 자리표시자(NAT SQLite 히스토리·Mem0에 남아
      다음 턴에 올라온 것)이거나, 이미 끝난 요청의 것이다.
    - **살아 있는 scope인데 그 매핑에 없는 번호** — LLM이 번호를 지어냈다
      (``<AMOUNT_be3f1a_9>``).

    `unmask_pii`처럼 중립 문구로 바꾸지 **않는다.** 이 함수의 소비자는 사용자에게 보여
    줄 텍스트가 아니라 **DB에 영구히 남을 값**이다. "(이전 금액 1)"을 저장하면 손상의
    종류만 바뀌고 소실은 그대로다(#339). 원문을 그대로 남겨 호출자가 거부하게 한다.
    """
    if not text:
        return text, []

    unrestorable: list[str] = []

    with _LOCK:
        # sub() 콜백이 도는 동안 다른 요청이 등록을 지울 수 있다. 이 텍스트에 실제로 나온
        # scope의 매핑만 미리 떠서 락을 놓는다 — 콜백 안에서 락을 잡으면 재진입 위험만
        # 늘고, 등록된 매핑 dict는 그 뒤 바뀌지 않으므로 참조를 들고 나가도 값이 흔들리지
        # 않는다.
        snapshot = {
            scope: _REGISTRY[scope]
            for scope in {m.group(2) for m in _SCOPED_PLACEHOLDER_RE.finditer(text)}
            if scope in _REGISTRY
        }

    def _restore(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        value = snapshot.get(match.group(2), {}).get(placeholder)
        if value is None:
            unrestorable.append(placeholder)
            return placeholder
        return value

    return _SCOPED_PLACEHOLDER_RE.sub(_restore, text), unrestorable


def placeholder_kind(placeholder: str) -> str:
    """자리표시자에서 종류(``ACCOUNT``/``AMOUNT``/``QTY``)를 뽑는다.

    :func:`restore_live_placeholders`가 돌려준 목록의 원소를 넣는다. 오류 응답에는
    자리표시자 원문 대신 이 종류만 싣는다 — 근거는 `create_db_diary` 참고.

    형식에 맞지 않는 문자열은 빈 문자열을 돌려준다. 위 함수의 출력만 넣는 한 도달하지
    않지만, 종류를 알 수 없다는 이유로 호출자를 죽이지는 않는다.
    """
    match = _SCOPED_PLACEHOLDER_RE.fullmatch(placeholder)
    return match.group(1) if match else ""
