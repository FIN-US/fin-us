"""종목코드 추출·판정 공용 상수 및 헬퍼 (#140).

services.py와 telegram_commands.py가 각자 중복 구현하던 정규식과 함수를
이 모듈로 통합한다. 두 파일에서 import해 사용한다.
"""
import json
import logging
import re

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
_ORDERABLE_STOCK_CODE_RE = re.compile(r"\A[0-9]{6,7}\Z")


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
_MASTER_STOCKS_PATH = _TRADING_MCP_DIR / "data" / "stocks.json"

# 마스터 로드 실패(fail-open)를 "성공했지만 빈 집합"과 구분하기 위한 센티널.
_MASTER_LOAD_FAILED = object()

# 지연 로딩 캐시: None=아직 로드 안 함, frozenset=로드 성공, _MASTER_LOAD_FAILED=로드 실패.
# 요청마다 489K짜리 stocks.json을 다시 파싱하면 지름길을 둔 이유(MCP 왕복 생략)가
# 사라지므로, 프로세스 수명 동안 한 번만 읽고 캐시한다.
_master_codes_cache: object = None


def _load_master_codes():
    """종목마스터 코드 집합을 지연 로딩해 프로세스 메모리에 캐시합니다.

    파일이 없거나 파싱에 실패하면 예외를 던지지 않고 _MASTER_LOAD_FAILED를
    캐시해 반환합니다(fail-open) — 이 경로는 리포트 저장 판정에 쓰이지
    주문에는 쓰이지 않으므로, 마스터를 못 읽는다고 분석 기능 전체가 죽으면
    안 됩니다.

    개별 항목의 결손은 그 항목만 건너뛰고 나머지로 계속하되, 코드를 하나도
    읽지 못한 경우는 로드 실패로 취급합니다(빈 집합은 모든 코드를 거부하므로).
    """
    global _master_codes_cache
    if _master_codes_cache is not None:
        return _master_codes_cache
    try:
        raw = _MASTER_STOCKS_PATH.read_text(encoding="utf-8")
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
            _MASTER_STOCKS_PATH,
            exc,
        )
        _master_codes_cache = _MASTER_LOAD_FAILED
        return _master_codes_cache
    if len(codes) < total:
        # 캐시 때문에 이 경고는 프로세스 수명 동안 한 번만 찍힌다 — 얼마나 건졌는지를
        # 함께 남겨야 "일부만 검증 중"인 상태를 나중에 알아볼 수 있다.
        # 마스터의 코드는 유일하므로(stock-master.js Step 1의 전제) 차이는 곧 누락 건수다.
        logger.warning(
            "종목마스터 일부 항목에서 코드를 읽지 못했습니다: path=%s, %d/%d건만 사용합니다",
            _MASTER_STOCKS_PATH,
            len(codes),
            total,
        )
    _master_codes_cache = codes
    return codes


def _is_known_master_code(code: str) -> bool:
    """code(이미 upper() 정규화된 값)가 종목마스터에 실재하는 코드인지 확인합니다.

    마스터를 읽을 수 없으면(fail-open) 검증을 생략하고 True를 반환해 #151
    이전의 지름길 동작(검증 없이 통과)을 유지합니다.
    """
    codes = _load_master_codes()
    if codes is _MASTER_LOAD_FAILED:
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
