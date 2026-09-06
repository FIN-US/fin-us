"""종목코드 추출·판정 공용 상수 및 헬퍼 (#140).

services.py와 telegram_commands.py가 각자 중복 구현하던 정규식과 함수를
이 모듈로 통합한다. 두 파일에서 import해 사용한다.
"""
import json
import logging
import os
import re
from pathlib import Path

from .config import _TRADING_MCP_DIR

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# MCP 응답에서 종목코드 추출
# ──────────────────────────────────────────────────────────────────────────
# resolve_stock_code 응답 형식: "종목명 (코드, 시장)" — mcp-trading/index.js
# 종목명 자체에 괄호가 들어가는 경우가 있어(예: "...1(A)", 닫히지 않는 "(H")
# 단순 괄호 매칭 대신 코드+쉼표 조합에 앵커한다. 종목명에 쉼표가 들어가는 종목은 없다.
# 코드 길이는 6자(3,889종목)·7자 ETN(389종목)·9자 펀드(75종목)가 섞여 있어 상한을 두지 않는다.
# 숫자 불변식(_has_code_digit 적용)을 함께 거쳐야 최종 유효 코드로 인정한다.
_STOCK_CODE_EXTRACT_RE = re.compile(r"\(([0-9A-Z]{6,}),")


def extract_stock_name(resolved_text: str) -> str | None:
    """resolve_stock_code 응답 ``"종목명 (코드, 시장)"``에서 종목명만 뽑는다.

    코드 앵커 **앞부분 전체**가 종목명이다. ``split("(")[0]``으로 자르면 위 주석이
    말하는 그 경우 — 이름 자체에 괄호가 든 종목 — 에서 이름이 잘려 나간다.
    주문 가능 코드(6~7자리 숫자)만 세어도 mcp-trading/data/stocks.json에 178건이
    해당한다(예: ``132030 KODEX 골드선물(H)`` → "KODEX 골드선물").

    잘린 이름은 PendingOrder.stock_name → 승인 메시지 → TradeHistory.stock_name까지
    그대로 간다. 주문 준비(telegram_commands)와 주문 보조(order_assist)가 같은 규칙을
    쓰도록 여기 둔다 — 두 곳이 각자 자르면 같은 종목이 화면마다 다른 이름으로 남는다.
    """
    match = _STOCK_CODE_EXTRACT_RE.search(resolved_text or "")
    if match is None:
        return None
    return resolved_text[: match.start()].strip() or None


# ──────────────────────────────────────────────────────────────────────────
# 입력이 이미 종목코드 형태인지 판정 (MCP 조회 생략 여부)
# ──────────────────────────────────────────────────────────────────────────
# {6,7} → 실제 분포(6·7·9자)에 정확히 맞춤: 9자 펀드 코드 75종목을 커버하되, 8자는
# 종목마스터 4,353종 전수(mcp-trading/data/stocks.json)에 0건이라 제외한다(#140).
#   길이 분포(전수 실측): 6자 3,889종목 / 7자 389종목(ETN) / 9자 75종목(펀드) / 8자 0건
# KIS 국내 종목코드는 6자리 숫자(005930)만이 아니다. 코스닥 스팩·리츠 등 약 18%가
# 영문이 섞인 형태(0001A0)이고, 펀드는 9자(F70100026).
# mcp-trading/stock-master.js의 resolveStock()은 #174 이후 코드 완전일치를 길이 무관으로
# 처리하므로, 9자 펀드 코드를 직접 입력하면 MCP 왕복 없이 바로 반환된다.
# {6,9}처럼 단순 상한만 넓히면 실재하지 않는 8자 입력까지 "코드 형태"로 통과시켜, MCP
# 존재 검증을 건너뛰고 그대로 코드로 확정해 버린다(#151과 별개로 이 PR이 만들면 안 되는
# 부수 효과). _STOCK_CODE_EXTRACT_RE({6,})과 달리 상한이 있다 — 추출 정규식은 MCP
# *응답*에서 코드를 뽑는 것이고, 이 정규식은 *입력*이 코드 형태인지 판단하는 것이라
# 역할이 다르다.
_STOCK_CODE_RE = re.compile(r"\A(?:[0-9A-Z]{6,7}|[0-9A-Z]{9})\Z")

# ──────────────────────────────────────────────────────────────────────────
# 주문 가능 코드 판정 — mcp-trading/order.js:77-84 정책의 백엔드 복제본
# ──────────────────────────────────────────────────────────────────────────
# order.js의 buildCashOrderBody()는 두 가드가 함께 "숫자 6~7자만 주문 가능"을 이룬다:
#   첫 번째(line 77): /^[0-9A-Z]{6,7}$/i && !/^\d{6,7}$/ → 영숫자 코드 전용 메시지
#   두 번째(line 84): !/^\d{6,7}$/               → 실제 범위 확정 (9자 펀드 포함 거절)
# 이 상수는 두 번째 가드(`/^\d{6,7}$/`)를 백엔드에서 복제한다.
# 백엔드가 조기 거절하므로 영숫자·9자 코드는 /confirm 전에 막힌다.
# 영숫자 코드 주문 미지원은 #73에서 확정된 정책이다.
#
# ⚠️  결합 유지 필수: 이 상수와 mcp-trading/order.js:77-84 가드는 쌍을 이루므로
#     한쪽만 바꾸면 해당 계층에서 조용히 계속 막힌다(#138 참조).
#     영숫자 코드 주문 지원을 검토할 때(#138)는 order.js 두 가드와 이 상수를
#     **모두** 함께 바꿔야 한다.
#
# `\d`는 파이썬에서 전각 숫자까지 매치하므로 JS의 ASCII 전용 `\d`와 어긋나지 않도록
# `[0-9]`로 명시한다. 앵커를 패턴에 넣어 order.js와 형태를 맞춘다.
#
# 기본값(이 상수)은 유지한다 — KIS Open API가 영숫자 PDNO를 order-cash TR에서 실제로
# 수용하는지는 #265 조사에서도 확정되지 않았다("ETN 7자는 order_cash.py 스펙 문구상
# 근거 있음, 9자 펀드·영숫자 6자 신형우선주·신주인수권은 미확인, 전부 문서 근거일 뿐
# 실호출 확인은 아님"). 실계좌 없이는 판정할 수 없으므로 가드를 무조건 열지 않고
# KIS_ALNUM_STOCK_ORDER_ENABLED 플래그 뒤에 둔다 — 아래 is_orderable_stock_code()가
# 텔레그램 주문 경로의 판정 진입점이며, 이 상수는 그 함수의 "플래그 꺼짐(기본값)"
# 분기에서, 그리고 order_assist.check_orderable_code()에서 쓰인다. 후자는 플래그를 보지
# 않는 is_orderable_stock_code_strict()를 거친다(아래 ⚠️ 블록 3번째 복제본 항목 참조).
#
# 이 상수와 mcp-trading/order.js의 ORDERABLE_STOCK_CODE_RE가 같은 판정을 내는지는
# mcp-trading/tests/fixtures/orderable_code_policy.json 공유 판정표가 고정한다(#138).
_ORDERABLE_STOCK_CODE_RE = re.compile(r"\A[0-9]{6,7}\Z")

# 플래그가 켜졌을 때(KIS_ALNUM_STOCK_ORDER_ENABLED=true) 허용하는 범위. _STOCK_CODE_RE
# (입력이 코드 형태인지 판정하는 정규식)와 같은 길이 분포(6·7·9자, 8자는 마스터에
# 0건이라 제외)를 쓴다. mcp-trading/order.js의 플래그 켜짐 분기와 형태를 맞춘다.
# _STOCK_CODE_RE와 패턴이 바이트 단위로 같지만 의도적으로 분리해 둔다 — 하나는 "입력이
# 코드 형태인가", 다른 하나는 "주문을 받을 범위인가"로 역할이 다르고, 한쪽이 바뀌어도
# 다른 쪽이 따라갈 이유가 없다(별칭으로 묶으면 그 구분이 사라진다).
_ORDERABLE_STOCK_CODE_ALNUM_RE = re.compile(r"\A(?:[0-9A-Z]{6,7}|[0-9A-Z]{9})\Z")


def _alnum_stock_order_enabled() -> bool:
    """영숫자·9자 코드 주문 가드 완화 플래그(#138).

    매 호출 os.environ을 다시 읽는다 — 모듈 임포트 시점에 한 번만 굳히면 테스트가
    monkeypatch.setenv로 설정해도 이미 굳은 값을 바꾸지 못한다.
    mcp-trading/order.js의 `process.env.KIS_ALNUM_STOCK_ORDER_ENABLED === "true"`와
    비교 방식을 맞춘다 — 정확히 "true" 문자열만 켠 것으로 인정하고 대소문자·공백은
    관용하지 않는다. 두 계층이 같은 값에 같은 판정을 내려야 한쪽만 열리는 사고를
    막을 수 있다.
    """
    return os.environ.get("KIS_ALNUM_STOCK_ORDER_ENABLED", "") == "true"


def is_orderable_stock_code_strict(code: str) -> bool:
    """플래그와 무관하게 #73 기본 정책(숫자 6~7자)으로만 판정합니다.

    ``_ORDERABLE_STOCK_CODE_RE``를 모듈 밖으로 새어 나가게 두면 "이 상수를 직접 쓰는
    곳이 몇 군데인가"를 grep으로만 알 수 있고, 정책이 바뀔 때 호출부가 각자
    ``fullmatch`` 호출을 복제한다. 진입점을 함수로 고정해 상수는 이 모듈 안에 둔다.

    ``is_orderable_stock_code()``와 달리 KIS_ALNUM_STOCK_ORDER_ENABLED를 보지 않는다 —
    자동 제안 경로(order_assist.check_orderable_code)가 쓰는 하드 한도이며, 이
    비대칭은 의도된 것이다(아래 is_orderable_stock_code() docstring 참조).
    """
    return bool(_ORDERABLE_STOCK_CODE_RE.fullmatch(code))


def is_orderable_stock_code(code: str) -> bool:
    """code(이미 upper() 정규화된 값)가 현재 정책상 주문 가능한 형태인지 판정합니다.

    telegram_commands.py의 주문 준비 경로가 이 함수로 mcp-trading/order.js의
    buildCashOrderBody() 가드를 미리 복제해, 조기 거절로 시세·잔고 조회와 60초 대기
    슬롯 낭비를 막는다(#73).

    ⚠️  결합 유지 필수: 이 함수와 mcp-trading/order.js:77-84(+ 플래그 분기)는 쌍을
        이루므로 한쪽만 바꾸면 그 계층에서 조용히 계속 막힌다. 플래그를 켤 때는 두
        계층에 같은 KIS_ALNUM_STOCK_ORDER_ENABLED 값을 넣어야 한다 — mcp-trading은
        backend가 띄우는 자식 프로세스라 config._MCP_ENV_ALLOWED_PREFIXES의 KIS_
        접두사 통과 목록을 통해 같은 값을 그대로 물려받는다.

        3번째 복제본이 하나 더 있다: order_assist.check_orderable_code()가
        is_orderable_stock_code_strict()를 쓴다(자동 제안 경로의 하드 한도 판정).
        이건 의도된 비대칭이다 — 플래그를 켜도 자동 제안은 숫자 6~7자만 낸다.
        사람이 명시적으로 입력한 코드와 달리 봇이 스스로 고른 종목은 KIS 수용
        여부가 확정될 때까지(#265 실측) 기존 범위에 묶어 둔다.
        test_order_assist.py의 "플래그 켜짐에서도 제안 경로는 숫자만" 테스트가 이
        비대칭을 고정한다.
    """
    if _alnum_stock_order_enabled():
        return bool(_ORDERABLE_STOCK_CODE_ALNUM_RE.fullmatch(code))
    return is_orderable_stock_code_strict(code)


# ──────────────────────────────────────────────────────────────────────────
# 종목마스터 코드 존재 검증 (#151) — _looks_like_stock_code 지름길이 MCP 존재
# 확인 없이 코드를 확정해 버리는 문제를 로컬 코드 집합 대조로 막는다.
# ──────────────────────────────────────────────────────────────────────────
# 경로를 config._TRADING_MCP_DIR에서 파생시킨다. 백엔드가 MCP를 띄우는 디렉터리와
# 마스터를 읽는 디렉터리가 *같은 계산*에서 나오므로, 백엔드가 읽는 파일은 MCP가
# stock-master.js:8(DEFAULT_STOCKS_PATH)로 읽는 파일과 정의상 같다.
# 이 파일 위치(backend/)에 고정하면 안 된다 — TRADING_MCP_DIR로 MCP 디렉터리를 옮긴
# 구성에서 둘이 갈라진다(backend/Dockerfile: 백엔드 소스는 /app에 bind mount,
# MCP는 이미지 안 /opt/mcp-trading). 특히 레포 바깥을 가리키면 backend 쪽 경로에
# 파일이 없어 fail-open으로 #151 검증이 조용히 꺼진다.
# config는 stock_code를 import하지 않으므로 순환 import는 발생하지 않는다.
#
# 파생 규칙을 순수 함수로 뽑아둔다 — importlib.reload(config) + reload(stock_code)로
# 이 경로 계산을 검증하면 두 모듈의 전역 바인딩을 다른 테스트가 붙잡고 있는 채로
# 영향을 준다. 함수 대상으로 테스트하면 reload 없이 같은 계약을 고정할 수 있다.
def _master_stocks_path(mcp_dir: Path) -> Path:
    return mcp_dir / "data" / "stocks.json"


# 경로를 모듈 상수로 한 번만 굳히지 않는다(과거 _MASTER_STOCKS_PATH 방식) — 그러면
# _TRADING_MCP_DIR이 나중에 바뀌어도(예: 테스트가 몽키패치) 이미 계산된 값은 따라가지
# 않아, importlib.reload 없이는 "마스터 경로가 TRADING_MCP_DIR을 따라가는가"라는
# 배선 자체를 테스트로 고정할 방법이 없다. 대신 _load_master_codes가 매 호출
# _master_stocks_path(_TRADING_MCP_DIR)를 그 자리에서 계산해 쓴다 — _TRADING_MCP_DIR은
# 평범한 모듈 전역이라 매 호출 새로 조회되므로, 테스트가 reload 없이
# stock_code._TRADING_MCP_DIR을 직접 몽키패치하는 것만으로 이 배선을 검증할 수
# 있다(test_master_path_wiring_follows_trading_mcp_dir_without_reload).
# _master_stocks_path 자체는 Path 조인뿐이라 매 호출 다시 계산해도 비용이 없다 —
# 실제 디스크 I/O는 아래 캐시가 여전히 막는다.

# 마스터 로드 실패(fail-open)를 "성공했지만 빈 집합"과 구분하기 위한 센티널.
_MASTER_LOAD_FAILED = object()

# 지연 로딩 캐시: None=아직 로드 안 함, frozenset=로드 성공, _MASTER_LOAD_FAILED=로드 실패.
# 요청마다 489K짜리 stocks.json을 다시 파싱하면 지름길을 둔 이유(MCP 왕복 생략)가
# 사라지므로, 프로세스 수명 동안 한 번만 읽고 캐시한다.
# _master_codes_cache_path는 캐시가 어느 경로에서 만들어졌는지 기록한다 — 경로 계산을
# 매 호출로 바꾸면서 함께 필요해졌다. 경로가 바뀌면(_TRADING_MCP_DIR 변경) 캐시를
# 무효화해야 한다 — 그러지 않으면 이전 경로에서 읽은 캐시를 새 경로에도 그대로
# 돌려줘, 테스트 간 캐시 오염이나 실제 배포에서 경로가 바뀐 뒤에도 옛 마스터를
# 계속 쓰는 문제가 생긴다.
_master_codes_cache: object = None
_master_codes_cache_path: Path | None = None


def _load_master_codes():
    """종목마스터 코드 집합을 지연 로딩해 프로세스 메모리에 캐시합니다.

    파일이 없거나 파싱에 실패하면 예외를 던지지 않고 _MASTER_LOAD_FAILED를
    캐시해 반환합니다(fail-open) — 이 경로는 리포트 저장 판정에 쓰이지
    주문에는 쓰이지 않으므로, 마스터를 못 읽는다고 분석 기능 전체가 죽으면
    안 됩니다.

    개별 항목의 결손은 그 항목만 건너뛰고 나머지로 계속하되, 코드를 하나도
    읽지 못한 경우는 로드 실패로 취급합니다(빈 집합은 모든 코드를 거부하므로).
    """
    global _master_codes_cache, _master_codes_cache_path
    path = _master_stocks_path(_TRADING_MCP_DIR)
    if _master_codes_cache is not None and _master_codes_cache_path == path:
        return _master_codes_cache
    try:
        raw = path.read_text(encoding="utf-8")
        stocks = json.loads(raw)
        # 항목 단위로 건너뛴다. 마스터는 외부 KIS 파일에서 생성되므로 code 키가 빠진
        # 항목이 섞일 수 있는데, 하나의 KeyError로 4,353건 전체 검증이 fail-open되면
        # 그 한 건 때문에 #151이 통째로 꺼진다. stock-master.js도 String(s.code ?? "")로
        # 항목 단위로 방어한다.
        codes = frozenset(
            str(stock["code"]).upper()
            for stock in stocks
            if isinstance(stock, dict) and stock.get("code")
        )
        total = len(stocks)
        if not codes:
            # 파일 형태가 통째로 바뀌어 코드를 하나도 못 읽으면 빈 집합이 되는데, 그러면
            # _is_known_master_code가 모든 코드를 조용히 거부한다 — fail-open보다 나쁘다.
            # 센티널과 구분되지 않으므로 여기서 로드 실패로 되돌린다.
            raise ValueError("종목마스터에서 코드를 하나도 읽지 못했습니다")
    except Exception as exc:
        logger.warning(
            "종목마스터 로드 실패, 지름길 존재 검증을 건너뜁니다(fail-open): path=%s, error=%s",
            path,
            exc,
        )
        _master_codes_cache = _MASTER_LOAD_FAILED
        _master_codes_cache_path = path
        return _master_codes_cache
    if len(codes) < total:
        # 캐시 때문에 이 경고는 프로세스 수명 동안 한 번만 찍힌다 — 얼마나 건졌는지를
        # 함께 남겨야 "일부만 검증 중"인 상태를 나중에 알아볼 수 있다.
        # 마스터의 코드는 유일하므로(stock-master.js Step 1의 전제) 차이는 곧 누락 건수다.
        logger.warning(
            "종목마스터 일부 항목에서 코드를 읽지 못했습니다: path=%s, %d/%d건만 사용합니다",
            path,
            len(codes),
            total,
        )
    _master_codes_cache = codes
    _master_codes_cache_path = path
    return codes


def _is_known_master_code(code: str) -> bool:
    """code(이미 upper() 정규화된 값)가 종목마스터에 실재하는 코드인지 확인합니다.

    마스터를 읽을 수 없으면(fail-open) 검증을 생략하고 True를 반환해 #151
    이전의 지름길 동작(검증 없이 통과)을 유지합니다.
    """
    codes = _load_master_codes()
    # _load_master_codes의 반환값은 frozenset 아니면 _MASTER_LOAD_FAILED뿐이라
    # 이 판정은 `is _MASTER_LOAD_FAILED`와 대상 집합이 같다. 캐시 변수가 object로
    # 선언돼 있어 센티널 비교로는 타입이 좁혀지지 않으므로 isinstance로 판정한다.
    if not isinstance(codes, frozenset):
        return True
    return code in codes


# ──────────────────────────────────────────────────────────────────────────
# 미해석 에코 판정 (#151) — stock-master.js Step 3
# ──────────────────────────────────────────────────────────────────────────
# resolveStock은 코드 형태 입력이 마스터에 코드로도 이름·별칭으로도 없으면
# market="UNKNOWN"으로 입력을 그대로 되돌려준다(stock-master.js:91-93).
# 존재 검증이 아니라 에코이므로 이걸 성공으로 오인하면 #151이 재발한다.
# 실제 마스터의 market은 KOSPI/KOSDAQ뿐이라(mcp-trading/data/stocks.json 전수 확인)
# "(코드, UNKNOWN)" 조합은 이 에코의 신뢰 가능한 신호다.
#
# _STOCK_CODE_EXTRACT_RE와 같은 "괄호+코드" 지점에 앵커한다. 문자열 끝에 앵커하면
# index.js:559가 응답 뒤에 무언가를 덧붙이는 순간 코드 추출은 계속 성공하는데
# 에코 감지만 조용히 멈춰 #151이 되돌아온다.
_UNRESOLVED_ECHO_RE = re.compile(r"\([0-9A-Z]{6,}, UNKNOWN\)")


def _is_unresolved_echo(resolved_text: str) -> bool:
    """resolve_stock_code 응답이 stock-master.js Step 3의 미해석 에코인지 판정합니다.

    리포트 저장(services._resolve_stock_code)과 주문 준비(telegram_commands)가
    같은 판정을 쓰도록 여기 둔다 — 한쪽에만 적용하면 같은 미해석 입력이 위험도가
    높은 주문 경로로만 통과한다.
    """
    return bool(_UNRESOLVED_ECHO_RE.search(resolved_text))


def _has_code_digit(value: str) -> bool:
    """실제 종목코드는 항상 숫자를 포함한다(종목마스터 4,353종 전수 확인, 0건 예외).

    입력 판정(_looks_like_stock_code)과 MCP 추출 결과 검증(_resolve_stock_code)이
    같은 불변식을 쓰도록 한 곳에 둔다.
    """
    return any(ch in "0123456789" for ch in value)


def _looks_like_stock_code(stock: str) -> bool:
    """이미 종목코드 형태여서 MCP 조회를 생략해도 되는지 판정합니다.

    주의(#174 이후): stock-master.js의 resolveStock은 #150과 반대로 코드 완전일치를
    이름·별칭 매칭보다 *먼저* 시도한다. 다만 Step 1은 stock.code만 비교하므로, 마스터에
    *이름·별칭*으로 존재하는 코드 형태 입력은 여전히 Step 2가 잡아 다른 코드를 돌려준다.
    이 함수는 마스터를 볼 수 없어 존재 검증 없이 코드로 확정하므로(#151), 그런 입력에
    대해서는 순서 반전 이후에도 JS보다 공격적이다.

    _has_code_digit이 안전망 역할을 한다 — 코드 형태 이름 3종(SIMPAC/INVENI/WISCOM)이
    모두 숫자를 포함하지 않아 여기서 걸러지고 MCP로 넘어간다.
    숫자를 포함한 6·7·9자 이름이 신규 상장되면 이 안전망이 뚫린다. mcp-trading/tests/
    stock-master.test.js의 "exactly 3 master stock names..." 테스트가 부분적인 신호다 —
    다만 그 테스트가 쓰는 CODE_SHAPE_PATTERN은 `/^[A-Z0-9]{6,7}$/i`라 6·7자만 덮고
    9자는 검사하지 않는다. 6·7·9자 전 범위를 덮는 신호는 backend/tests/test_stock_code.py의
    test_no_master_name_is_shadowed_by_looks_like_stock_code다 — 이 테스트가 깨지면
    이 함수를 재검토해야 한다.

    #140 영향: {6,7} → {6,7}|{9} 확대로 F70100026 같은 9자 펀드 코드가 이제 지름길을 탄다.
    이 함수 자체는 마스터를 보지 않으므로 실재 여부를 가리지 못한다 — 그 존재
    검증은 이 함수의 True를 소비하는 쪽(services._resolve_stock_code의
    _is_known_master_code 호출)이 담당한다(#151).
    """
    value = stock.strip().upper()
    return bool(_STOCK_CODE_RE.match(value)) and _has_code_digit(value)
