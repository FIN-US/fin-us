# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""NAT 프로세스 내부에서 만들어지는 계좌 PII의 마스킹 경계 (#231, F-17 / NFR-05).

## 왜 backend 마스킹(#230)으로 막히지 않는가

`backend/services.py`의 `llm_chat()`은 backend가 **보내는 프롬프트**를 마스킹한다.
그런데 NAT 경로에서 backend가 보내는 것은 사용자 발화(`user_msg`) 하나뿐이고,
계좌 데이터는 그 다음 단계에서 NAT 프로세스 **안에서** 생긴다:

    backend --("내 잔고 어때?")--> NAT 라우터 --> trading_agent
        --> KIS/mcp-trading 도구 호출 --> 잔고 리포트
        --> ReAct 컨텍스트에 삽입 --> openai_cloud_llm (api.openai.com)

backend는 3~4단계를 보지 못한다. 그래서 마스킹 지점이 이쪽에도 하나 더 필요하다.

## 경계 (이 모듈이 정의하는 것)

들어오는 쪽 — **마스킹**:
    계좌 범위 도구의 결과 텍스트가 에이전트(=LLM 컨텍스트)로 돌아가기 직전.
    호출 지점은 `finus_api.py`의 `_mask_account_tool_result()` 세 곳뿐이다.

나가는 쪽 — **복원**:
    1. 최상위 워크플로 응답 (`agents.finus_reasoning_trace_agent`) — 사용자에게 나가는 본문.
    2. LLM이 만든 도구 인자 (`finus_api._restore_outbound_arguments()`) — MCP·backend로
       실제 값이 나가야 하는 자리. 여기서 복원하지 않으면 자리표시자가 그대로 주문
       파라미터나 매매일지 본문으로 저장된다.

두 방향 모두 :data:`PII_SESSION`에 담긴 **요청 하나짜리** 매핑을 쓴다. 박스는
최상위 워크플로가 한 번만 심는다 — `DATA_TOOL_LEDGER`/`REASONING_TRACE`와 같은 이유로
`ContextVar.set()`은 안쪽에서 바깥으로 전파되지 않기 때문이다.

## 마스킹 엔진을 왜 import해서 쓰는가

`backend/pii_mask.py`가 F-17의 마스킹 엔진이다(#230). finus_nat이 같은 정규식을
복사하면 두 계층의 판정이 조용히 갈라진다 — 실제로 #330·#333·#334가 그 정규식을
연달아 고쳤고, 복사본이 있었다면 그 수정이 NAT 경로에만 반영되지 않았을 것이다.
그래서 이 모듈은 **파일 경로로 로드해 재사용만** 한다. `backend/pii_mask.py`는
한 줄도 고치지 않는다(진행 중인 #332와 겹치지 않게 하려는 목적도 있다).

finus_nat은 backend와 별도 패키지·별도 컨테이너라 `import backend.pii_mask`가 성립하지
않는다. 그래서 :func:`_load_pii_mask`가 파일 위치를 직접 찾는다. Docker 이미지에는
`finus_nat/Dockerfile`이 `backend/pii_mask.py` 한 파일만 복사해 넣는다.

## 알려진 한계 (이번 범위에서 남기는 것)

- **도구 호출마다 scope가 다르다.** `mask_pii`는 호출당 새 nonce를 뽑으므로, 같은
  금액이 두 도구 결과에 나와도 서로 다른 자리표시자가 된다. LLM은 두 값이 같다는 것을
  알 수 없다. 값 단위 동일성이 필요해지면 별도 이슈로 다룬다.
- **이전 턴의 자리표시자는 복원되지 않는다.** SQLite 대화 기록에는 마스킹된 답변이
  저장되고, 다음 턴의 세션 매핑에는 그 scope가 없다. `unmask_pii`의 fail-open 경로가
  "(이전 금액 1)" 같은 중립 문구로 바꾼다 — 조용한 오답 대신 관측 가능한 저하다.
  (반대로 저장을 원문으로 되돌리면 다음 턴 히스토리가 평문으로 OpenAI에 재전송된다.)
"""

import importlib.util
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class PiiMaskUnavailableError(RuntimeError):
    """마스킹 엔진(`backend/pii_mask.py`)을 로드하거나 적용하지 못했다.

    호출부는 이 예외를 **원본 데이터를 반환하지 않는 방향**으로 처리해야 한다
    (fail-closed). 유출보다 기능 실패가 낫다는 것이 F-17의 전제다.
    """


class _PiiMaskModule(Protocol):
    """`backend/pii_mask.py`가 제공해야 하는 최소 공개 API (#230)."""

    def mask_pii(self, text: str | None) -> tuple[str | None, dict[str, str]]: ...

    def unmask_pii(self, text: str | None, mapping: dict[str, str]) -> str | None: ...


# 로드된 모듈 캐시. None은 "아직 시도하지 않음"이다 — 실패는 캐시하지 않는다.
# 배포 도중 파일이 늦게 마운트되는 경우(볼륨 마운트 순서)에 재시도가 가능해야 한다.
_MASKER: Any = None

#: `backend/pii_mask.py`를 직접 가리키는 경로 재정의. 컨테이너 배치가 아래 탐색과
#: 다를 때만 쓴다.
_PATH_ENV = "FINUS_PII_MASK_PATH"

#: 파일 위치를 로드된 모듈 이름과 분리한다 — `backend` 패키지로 등록하면 backend
#: 프로세스와 이름이 겹쳐 진단이 어려워진다.
_MODULE_NAME = "nat_finus_nat._vendored_backend_pii_mask"


def _finus_nat_root() -> Path:
    """finus_nat 패키지 루트 (`src/nat_finus_nat/pii.py` -> `finus_nat/`)."""
    return Path(__file__).resolve().parents[2]


def _pii_mask_candidates() -> Iterator[Path]:
    """`backend/pii_mask.py` 후보 경로를 우선순위대로 낸다.

    ``finus_api.fin_us_vendor_root()``와 같은 규칙을 의도적으로 **복제**한다.
    그 함수는 `nat.*`를 import하는 모듈에 있어서, 이 모듈이 그것을 import하면
    NAT 런타임 없이는 마스킹 계층을 단위 테스트할 수 없게 된다. 규칙이 갈라지면
    후보 목록이 한 칸 늘어날 뿐 마스킹이 조용히 꺼지지는 않는다 —
    어느 후보에서도 못 찾으면 fail-closed로 떨어진다.
    """
    override = os.environ.get(_PATH_ENV, "").strip()
    if override:
        yield Path(override).expanduser().resolve()
        return

    vendor_root = os.environ.get("FINUS_VENDOR_ROOT", "").strip()
    if vendor_root:
        # Docker: FINUS_VENDOR_ROOT=/workspace, Dockerfile이 여기에 파일을 복사한다.
        yield Path(vendor_root).expanduser().resolve() / "backend" / "pii_mask.py"

    integrate_root = _finus_nat_root().parent
    yield integrate_root / "backend" / "pii_mask.py"          # 저장소 체크아웃(로컬 실행)
    yield integrate_root / "fin-us" / "backend" / "pii_mask.py"  # finus_nat이 형제 디렉터리인 배치


def _load_pii_mask() -> Any:
    """`backend/pii_mask.py`를 파일 경로로 로드해 캐시한다.

    Raises:
        PiiMaskUnavailableError: 후보 경로 어디에도 파일이 없거나 로드에 실패했을 때.
    """
    global _MASKER
    if _MASKER is not None:
        return _MASKER

    tried: list[str] = []
    for candidate in _pii_mask_candidates():
        tried.append(str(candidate))
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        # sys.modules 등록은 실행 **전에** 해야 한다 — dataclass/typing 등 일부 구문이
        # 자기 모듈을 sys.modules에서 되찾는다.
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            sys.modules.pop(_MODULE_NAME, None)
            raise PiiMaskUnavailableError(
                f"backend/pii_mask.py 로드 실패: {candidate} ({exc})"
            ) from exc
        if not (hasattr(module, "mask_pii") and hasattr(module, "unmask_pii")):
            sys.modules.pop(_MODULE_NAME, None)
            raise PiiMaskUnavailableError(
                f"{candidate}에 mask_pii/unmask_pii가 없습니다 — F-17 마스킹 엔진이 아닙니다."
            )
        _MASKER = module
        logger.info("PII 마스킹 엔진을 로드했습니다: %s", candidate)
        return _MASKER

    raise PiiMaskUnavailableError(
        "backend/pii_mask.py를 찾지 못했습니다. 탐색 경로: "
        + ", ".join(tried)
        + f". 배치가 다르면 {_PATH_ENV}로 파일 경로를 직접 지정하세요."
    )


@dataclass
class PiiSession:
    """요청 하나 동안의 자리표시자 -> 원값 매핑.

    ``mask_pii``는 호출마다 새 매핑을 돌려주므로, 한 요청에서 도구를 여러 번 부르면
    매핑도 여러 개가 된다. 복원은 요청 끝에서 한 번에 일어나야 하므로 여기서 누적한다.
    scope nonce가 호출마다 달라 키가 겹치지 않는다(`backend/pii_mask.py`의 `_Counter`
    docstring 참고).
    """

    mapping: dict[str, str] = field(default_factory=dict)

    def mask(self, text: str) -> str:
        """*text*를 마스킹하고 매핑을 이 세션에 누적한다.

        Raises:
            PiiMaskUnavailableError: 마스킹 엔진을 쓸 수 없을 때.
        """
        masker = _load_pii_mask()
        try:
            masked, mapping = masker.mask_pii(text)
        except Exception as exc:  # noqa: BLE001
            raise PiiMaskUnavailableError(f"마스킹 적용 실패: {exc}") from exc
        self.mapping.update(mapping)
        return masked if masked is not None else text

    def unmask(self, text: str) -> str:
        """사용자에게 나가는 본문의 자리표시자를 원값으로 되돌린다.

        ``unmask_pii``는 예외를 던지지 않는다(fail-open). 매핑에 없는 자리표시자는
        중립 문구로 바뀐다 — 내부 토큰이 화면에 그대로 노출되는 것을 막기 위해서다.
        엔진을 못 불러오면 마스킹도 되지 않았다는 뜻이므로 원문을 그대로 돌려준다.
        """
        if not text:
            return text
        try:
            masker = _load_pii_mask()
            restored = masker.unmask_pii(text, self.mapping)
        except PiiMaskUnavailableError:
            logger.error("PII 역치환 불가 — 마스킹 엔진을 로드하지 못했습니다.")
            return text
        return restored if restored is not None else text

    def restore_literal(self, text: str) -> str:
        """기계가 읽을 값(도구 인자)에서 **아는 자리표시자만** 원값으로 되돌린다.

        :meth:`unmask`와 달리 모르는 자리표시자를 "(이전 금액 1)" 같은 한국어
        중립 문구로 바꾸지 않는다. 사용자 화면이라면 그 대체가 맞지만, 주문 수량이나
        가격 파라미터 자리에서는 한국어 문구가 토큰보다 나을 게 없고 실패 원인만
        가린다. 여기서는 손대지 않고 남겨 호출 실패로 드러나게 한다.
        """
        if not text or not self.mapping:
            return text
        restored = text
        for placeholder, original in self.mapping.items():
            if placeholder in restored:
                restored = restored.replace(placeholder, original)
        return restored


#: 요청 하나짜리 마스킹 세션 상자. 최상위 워크플로가 심고, 안쪽은 읽기만 한다.
PII_SESSION: ContextVar[PiiSession | None] = ContextVar("finus_pii_session", default=None)


# ---- 마스킹 대상 판정 ----

#: 결과 전체가 사용자 계좌 데이터인 도구들 — 도구 이름만으로 마스킹이 결정된다.
#: mcp-trading은 서버 전체가 KIS 계좌 TR 래퍼라 도구 단위로 판정할 수 있다
#: (balance.js / balance-rlz-pl-report.js / order.js 모두 CANO 기반).
#: 매매일지 2종은 사용자가 적은 자기 거래 기록이라 금액·수량이 그대로 들어 있다.
#:
#: 여기 **없는** 도구는 마스킹되지 않는다. 뉴스(`finus_market_news`)와 공시·실적
#: (`finus_disclosure_signal`, `finus_earnings_report`)은 공개 정보이고 F-17의 보호
#: 대상이 아니다 — 가리면 분석만 망가지고 막히는 유출은 없다.
#: KIS pass-through(`finus_account_balance`)는 도구 단위로 정할 수 없어 여기 두지 않고
#: :func:`kis_api_type_is_account_scoped`가 api_type 단위로 가른다.
ACCOUNT_SCOPED_TOOLS: frozenset[str] = frozenset({
    "finus_mcp_trading_get_balance",
    "finus_mcp_trading_balance_rlz_pl",
    "finus_mcp_trading_today_orders",
    "finus_list_diaries",
    "finus_save_diary",
})

# KIS Trading MCP pass-through(`finus_account_balance`)는 한 도구가 계좌 TR과 시세 TR을
# 모두 태운다. 그래서 api_type 단위로 갈라야 한다.
#
# 방향은 **allowlist(fail-closed)** 다 — `_READONLY_API_ALLOWLIST_PREFIXES`와 같은 방식.
# 모르는 api_type은 마스킹한다. 반대로 deny-list로 두면 KIS가 새 계좌 TR을 추가하는
# 순간 조용히 평문으로 나간다.
#
# 여기에 올릴 수 있는 것은 **공개 시장 데이터**뿐이다 — 시세·호가·체결·지수·수급·순위·
# 종목검색. 이것들은 F-17의 보호 대상(계좌번호·보유수량·계좌 금액)이 아니고, 가리면
# "삼성전자 지금 얼마야?" 같은 가장 흔한 질문에서 에이전트가 답을 만들 수 없다.
#
# 운영자 추가 방법: 새 **시세** TR이 아래 접두사에 걸리지 않으면 접두사를 넓히거나
# _PUBLIC_MARKET_API_EXACT에 정확한 이름을 추가하세요. 계좌 TR은 절대 추가하지 마세요.
_PUBLIC_MARKET_API_PREFIXES: tuple[str, ...] = (
    "inquire_price",        # 현재가
    "inquire_daily_price",  # 일자별 시세
    "inquire_asking_price",  # 호가/예상체결
    "inquire_ccnl",         # 체결가 시세(계좌 체결내역 inquire_daily_ccld와 다르다)
    "inquire_time_",        # 시간대별 체결·차트
    "inquire_daily_itemchartprice",
    "inquire_index",        # 지수
    "inquire_investor",     # 투자자별 수급(시장 통계)
    "inquire_member",       # 회원사 수급
    "inquire_elw_",         # ELW 시세
    "search_",              # 종목 검색
    "ranking_",             # 각종 순위
    "volume_rank",          # 거래량 순위
    "market_cap",           # 시가총액
    "fluctuation",          # 등락률 순위
    "quotations",           # 시세 계열 경로 표기
)
_PUBLIC_MARKET_API_EXACT: frozenset[str] = frozenset({
    "find_api_detail",  # TR 스키마 조회 — 계좌 값이 실리지 않는다.
})


def kis_api_type_is_account_scoped(api_type: str) -> bool:
    """KIS pass-through의 *api_type*이 계좌 데이터를 낼 수 있으면 True (fail-closed).

    공개 시장 데이터 allowlist에 없으면 전부 True다. 잘못 True로 판정하면 시세가
    가려져 답변 품질이 떨어지고, 잘못 False로 판정하면 계좌 데이터가 평문으로
    OpenAI에 나간다 — 비대칭이므로 모르는 쪽은 True로 눕힌다.
    """
    lower = (api_type or "").strip().lower()
    if not lower:
        return True
    if lower in _PUBLIC_MARKET_API_EXACT:
        return False
    return not any(lower.startswith(prefix) for prefix in _PUBLIC_MARKET_API_PREFIXES)


# ---- 호출부가 쓰는 얇은 진입점 ----


def current_session() -> PiiSession | None:
    return PII_SESSION.get()


def mask_account_text(text: str) -> str:
    """계좌 범위 도구 결과를 마스킹해 돌려준다.

    세션 상자가 없으면(최상위 워크플로를 거치지 않은 호출 — `nat run` 단발 호출,
    직접 함수 호출 등) 일회용 세션으로 마스킹하고 ERROR를 남긴다. 자리표시자를
    되돌릴 방법이 없어 답변 품질은 떨어지지만, 상자가 없다고 원값을 흘리는 것보다
    낫다. `_record_to_ledger`가 원장 상자 부재를 다루는 방식과 같은 판단이다.

    Raises:
        PiiMaskUnavailableError: 마스킹 엔진을 쓸 수 없을 때. 호출부는 원본 대신
            오류를 반환해야 한다.
    """
    if not text:
        return text
    session = PII_SESSION.get()
    if session is None:
        logger.error(
            "PII_SESSION이 없는 상태로 계좌 도구 결과를 마스킹합니다 — 자리표시자를 "
            "복원할 수 없습니다. 최상위 워크플로(finus_reasoning_trace_agent) 밖에서 "
            "도구가 실행된 경우입니다."
        )
        session = PiiSession()
    return session.mask(text)


def restore_outbound(value: Any) -> Any:
    """LLM이 만든 값(도구 인자)에서 아는 자리표시자를 원값으로 되돌린다.

    dict/list를 재귀적으로 훑어 문자열만 손댄다. 세션이 없거나 매핑이 비면 입력을
    그대로 돌려준다 — 마스킹한 적이 없으면 되돌릴 것도 없다.
    """
    session = PII_SESSION.get()
    if session is None or not session.mapping:
        return value
    return _restore_outbound_with(value, session)


def _restore_outbound_with(value: Any, session: PiiSession) -> Any:
    if isinstance(value, str):
        return session.restore_literal(value)
    if isinstance(value, dict):
        return {key: _restore_outbound_with(item, session) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_outbound_with(item, session) for item in value]
    return value


def masking_unavailable_error_json(tool_name: str, detail: str) -> str:
    """fail-closed 응답 본문 — 원본 데이터 대신 에이전트에게 돌려줄 오류 JSON.

    `finus_api._err_json`과 같은 모양(`{"error": ...}`)이라 `_ERROR_JSON_PREFIX_RE`
    판정과 원장 기록이 그대로 동작한다. 그 함수를 쓰지 않는 이유는 순환 import
    때문이다 — `finus_api`가 이 모듈을 import한다.
    """
    return json.dumps(
        {
            "error": "pii_masking_unavailable",
            "tool": tool_name,
            "detail": detail,
            "hint": (
                "계좌 데이터를 마스킹할 수 없어 조회 결과를 반환하지 않았습니다(#231). "
                "재시도하지 말고 사용자에게 일시적인 오류임을 안내하세요."
            ),
        },
        ensure_ascii=False,
    )
