"""잔고 응답 파싱과 Portfolio 테이블 동기화(보유 종목·시세)."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import select, Session

from backend.core.config import TRADING_MCP_PARAMS
from backend.core.database import engine
from backend.core.models import Portfolio
# SQLite에서 읽은 naive UTC와 tz-aware now를 같은 축으로 맞추는 보정(#196).
# 같은 보정을 여기서 다시 정의하면 한쪽만 고쳐질 때 두 모듈의 시각 축이 갈린다.
# 모듈 밖에서 쓰므로 밑줄 없는 공개 이름이다(PR #367 리뷰).
from backend.repositories.filtered_signal import as_utc_naive
from backend.scheduler import deps

logger = logging.getLogger(__name__)


# "- 종목명 (코드) ..." 형식의 잔고 종목 줄을 검증하는 정규식.
# 줄에 "(코드)" 그룹이 없는 경우(예: "- 삼성전자 · 3주")를 계약 위반으로 거부한다.
# greedy .+가 마지막 " (코드)" 직전까지를 이름으로 잡으므로 이전의 rsplit("(", 1)과
# 동일하게 다중 괄호 이름(CJ4우(전환), 룩셈부르크코어오피스(파생형)(A))을 올바르게
# 처리한다. 단, 코드 자리 검증은 아래 문자 클래스로 rsplit보다 엄격해 "(합성)" 같은
# 비코드 괄호를 코드로 인정하지 않는 점에서 rsplit과 의도적으로 다르다(아래 설명 참고).
# ^- 가 "- " 접두사를 소비하므로 종목명 중간의 "- "(한국 - 전력)는 보존된다.
# "(코드)" 뒤의 구분자(· 또는 :)는 매칭 대상에 포함하지 않아 포맷 변형에 유연하게
# 대응한다.
#
# 코드 클래스를 [0-9A-Z]{6,}으로 한정하는 이유:
#   - stock_code.py의 _STOCK_CODE_EXTRACT_RE와 동일한 컨벤션. 다만 이 정규식 자체를
#     stock_code.py로 통합하지는 않았다 — 추출 대상 포맷이 다르다("이름 (코드, 시장)"이
#     아니라 "- 이름 (코드) · N주").
#   - 하한 6: KIS 표준 6자리 코드(005930 등). 상한 없음({6,}): ETN 7자리(368종목),
#     9자리(89종목 예: F70102B96)까지 포함.
#   - [^()]*를 쓰면 두 가지 문제가 생긴다:
#     1) 빈 코드 통과 — "- 삼성전자 () · 3주"의 빈 괄호가 코드 자리에 매치돼
#        (name="삼성전자", code="") 무효 줄이 살아남는다.
#     2) KODEX 오염 — "- KODEX 200 (합성) · 3주"에서 "(합성)"이 코드 자리에 들어가
#        이름이 "KODEX 200"으로 잘려 이슈 #157이 막으려던 오염이 재현된다.
STOCK_LINE_RE = re.compile(r"^- (?P<name>.+) \((?P<code>[0-9A-Z]{6,})\)")


def extract_stocks_from_balance(balance_text: str) -> list[str]:
    """mcp-trading의 get_balance 결과에서 종목명 리스트를 추출합니다.

    _parse_balance_holdings에 파싱을 위임하고 이름만 모아 반환합니다.
    형식 위반 줄 경고와 파싱 실패 경고는 _parse_balance_holdings가 담당합니다.

    [보유 종목 리스트] 섹션이 없으면 [] 반환(마커 부재 === 파싱 불가).
    잘림 안내 문구는 STOCK_LINE_RE에 매치되지 않아 파싱 단계에서 걸러집니다.

    현재는 테스트에서만 사용하며 프로덕션 경로는 _parse_balance_holdings를 직접 호출한다.
    """
    return [h.name for h in _parse_balance_holdings(balance_text)]


# mcp/trading/balance.js의 formatTruncationNote()가 잘림 시 항상 덧붙이는 문구 중,
# truncated 사유(max_pages/time_budget/no_cursor/repeated_cursor/error, 그리고
# 목록에 없는 사유의 fallback "연속조회가 완료되지 않아")에 관계없이 고정으로 남는
# 부분만 매칭한다:
#   `\n\n[안내] ${reason} 조회가 중단되어 일부 보유 종목이 위 목록에서 ...`
# ${reason}만 사유별로 바뀌고 앞뒤 리터럴은 고정이므로, 사유 문구 자체(예: "페이지
# 상한")를 매칭 대상에 넣지 않아도 모든 현재·미래 사유를 잡는다. 사유별 문구까지
# 매칭하면 새 사유가 추가될 때마다 이 목록도 함께 고쳐야 해서 더 잘 깨진다.
#
# 판별은 "조회가 중단되어" 하나로만 한다. "[안내]"는 mcp/trading/index.js formatPaginationTruncationNote()의
# 모의투자 실현손익 안내와도 공유하는 표지라 get_balance 밖에서도 등장할 수 있어
# 판별력이 없고, 실제 discriminator는 잘림 문구에만 존재하는 이 접미사다. 고정할
# 리터럴이 하나로 줄어 아래 결합 테스트도 이 한 줄만 지키면 된다.
#
# get_balance는 MCP 텍스트 응답만 backend로 넘어오고 truncated 값 자체(구조화된
# 필드)는 MCP 경계를 넘어오지 않으므로, 이 함수는 문자열 매칭에 의존할 수밖에
# 없다. mcp/trading/balance.js의 리터럴이 바뀌면 이 매칭도 함께 깨진다 — 그
# 결합을 mcp/trading/tests/balance.test.js의 잘림 노트 테스트가 `조회가 중단되어`를
# 직접 단언해 고정하고, backend/tests/test_balance_parser.py가 픽스처로 재현한다.
_BALANCE_TRUNCATION_MARKER = "조회가 중단되어"

# balance.js formatBalanceReport()가 보유 종목 섹션을 구분하는 헤더 리터럴.
# 이 함수는 보유 종목이 0건이어도 이 마커와 "보유 종목이 없습니다."를
# 항상 출력한다. 따라서 이 마커가 없다는 것은 "보유 0건"이 아니라 "응답을 읽지 못함"이다.
# run_mcp_tool은 MCP content가 비면 예외 대신 빈 문자열을 반환하므로(ai/mcp_tools.py),
# 이 경로는 실재한다. _sync_portfolio_from_balance가 이 마커 부재를 계약 위반으로
# 처리해, 보유 0건으로 오인한 전 종목 삭제를 막는다.
_BALANCE_HOLDINGS_MARKER = "[보유 종목 리스트]"


def is_balance_truncated(balance_text: str) -> bool:
    """get_balance 응답에 mcp/trading/balance.js의 잘림 안내 문구가 포함되어 있는지 확인합니다.

    extract_stocks_from_balance()는 이 문구를 의도적으로 무시하므로(안내 문구가
    종목명으로 오인식되지 않도록), 잘림 여부는 이 함수로 별도 확인해야 한다.
    """
    return _BALANCE_TRUNCATION_MARKER in balance_text


def truncation_notice(report_text: str) -> str:
    """잘림 안내 줄("[안내] <사유> 조회가 중단되어 ...")을 그대로 돌려줍니다. 없으면 "".

    잘림 사유(time_budget/max_pages/error/...)마다 운영 대응이 다르므로 경고에 이 줄을
    싣는다. 사유 문자열을 따로 파싱하지 않으므로 JS에 새 사유가 추가돼도 따라간다.
    잔고(balance.js)와 실현손익(balance-rlz-pl-report.js) 두 포맷이 같은 판별자를 쓴다.
    """
    return next(
        (
            line.strip()
            for line in report_text.splitlines()
            if _BALANCE_TRUNCATION_MARKER in line
        ),
        "",
    )


# balance.js formatBalanceReport()의 종목 줄 형식에서 수량과 평단가를 추출하는 정규식.
#
# 수량(hldg_qty): "- 삼성전자 (005930) · 3주" 줄 끝의 "N주" 부분.
# 코드 닫는 괄호 직후 공백+중점+공백("· ")을 앵커로 사용한다. formatQuantity가
# toLocaleString("ko-KR")로 쉼표를 삽입하므로 "1,234주" 형태도 허용한다.
# · 는 U+00B7(MIDDLE DOT)로, balance.js 템플릿 리터럴에서 그대로 쓰는 문자다.
_QTY_RE = re.compile(r"\)\s*·\s*([\d,]+)주")

# 평단가(pchs_avg_pric): "  평단가 67,000원 → ..." 줄.
# formatAmount가 toLocaleString("ko-KR")로 쉼표를 삽입하고 "원"을 붙인다.
# 평단가는 매수 단가의 가중평균이라 정수가 아닌 것이 정상이다(예: "66,666.67원").
# mcp/trading/tests/balance.test.js가 "평단가 66,666.67원"을 리터럴로 단언하므로
# 소수점 허용은 가정이 아니라 계약이다. (?:\.\d+)?로 정수·소수 양쪽을 잡는다.
# "-원" 형태(parseNumber가 null을 반환하는 경우)는 [\d,]+에 매치되지 않아
# 기본값 0.0이 사용된다.
# _QTY_RE(수량)는 hldg_qty가 항상 정수이므로 소수점 허용이 필요 없다.
_AVG_PRICE_RE = re.compile(r"평단가\s+([\d,]+(?:\.\d+)?)원")


# ---------------------------------------------------------------------------
# 시세 소스: get_balance_rlz_pl (inquire-balance-rlz-pl, TTTC8494R) — #196
# ---------------------------------------------------------------------------
#
# get_balance(TTTC8434R)의 텍스트 리포트에는 현재가가 없다. 반면 실현손익 조회의
# 리포트는 종목 줄 바로 아래에 "현재가 71,200원"을 낸다(balance-rlz-pl-report.js formatBalanceRlzPlReport()).
# 이슈가 검토한 다른 선택지는 둘 다 막혀 있다: 옵션 A(get_balance output1에 현재가
# 필드가 있는지 실계좌 실측)는 계좌 없이 확인할 수 없고, 옵션 B(종목마다
# get_stock_quote, 호출당 1.1초 대기)는 유지보수자가 거부했다.
#
# 이미 등록된 도구를 텍스트로 읽는 대가는 명확하다 — 포맷이 바뀌면 조용히 0건이
# 된다. 그래서 마커·정규식은 공유 픽스처(mcp/trading/tests/fixtures/
# balance_rlz_pl_report.json)로 고정하고, 파싱 실패는 "쓰지 않음"으로 처리한다.

# 실현손익 리포트의 보유 종목 섹션 헤더.
#
# balance.js의 "[보유 종목 리스트]"와 **의미가 다르다**: 저쪽은 0건에도 마커를 항상
# 내므로 마커 부재가 곧 계약 위반이지만, 이쪽은 rows.length === 0이면 마커 자체를
# 내지 않고 "- 보유 종목이 없습니다."만 낸다(balance-rlz-pl-report.js formatBalanceRlzPlReport()).
# 그래서 마커 부재를 곧바로 오류로 보면 정상적인 빈 계좌가 10분마다 error 로그를
# 쌓는다 — _RLZ_PL_EMPTY_MARKER를 먼저 확인해 두 경우를 가른다.
# 두 리터럴은 부분 문자열로 겹치지 않는다("[보유 종목 리스트]"에는 "[보유 종목]"이
# 들어 있지 않다 — "종목" 다음이 "]"가 아니라 공백이다).
_RLZ_PL_HOLDINGS_MARKER = "[보유 종목]"
_RLZ_PL_EMPTY_MARKER = "보유 종목이 없습니다"

# 모의투자 계좌 대체 응답의 표지. get_balance_rlz_pl은 실전 계좌 전용이라
# (mcp/trading/index.js의 get_balance_rlz_pl registerTool 설명) 모의투자(openapivts)
# 에서는 index.js가 TR을 호출하지 않고 get_balance 요약을 대신 돌려주면서 이 문구를
# 덧붙인다(index.js의 getBalanceRlzPl 첫 분기).
#
# 줄 번호가 아니라 **심볼 이름으로** 가리킨다. 이 주석이 필요해지는 시점은 index.js가
# 바뀐 뒤인데, 줄 번호는 그때 이미 다른 코드를 가리키고 있다.
#
# 이걸 따로 잡지 않으면 그 응답에는 "[보유 종목]" 마커가 없으므로 마커 부재 가드가
# error를 남긴다 — 모의투자 배포에서는 그게 매 주기의 정상 동작이라 error가 아니다.
# 시세를 못 얻는다는 결론은 같지만, 로그 수준과 운영자가 읽을 원인이 다르다.
#
# ⚠️ **JS 리터럴과의 결합**: 아래 문자열은 index.js의 getBalanceRlzPl가 붙이는 안내 문구
# (balance-rlz-pl-report.js의 PAPER_RLZ_PL_FALLBACK_NOTE)의 부분 문자열이다. agents의
# 매매일지 조회 묶음도 같은 문자열(finus_api._PAPER_RLZ_PL_FALLBACK_MARKER)로 이 응답을 가려내
# 잔고를 재사용한다(#408). 세 곳의 일치는 공유 계약 픽스처
# mcp/trading/tests/fixtures/paper_rlz_pl_fallback.json을 JS·backend·NAT 스위트가 함께 읽어
# 고정한다(backend는 test_scheduler.py의 test_paper_fallback_marker_matches_shared_contract).
#
# 무력화되면 어떻게 되는가: 모의투자 응답이 이 검사를 통과해 마커 부재 가드까지
# 흘러가고, 거기서 logger.error가 난다. 모의투자는 기본 개발 구성이므로 **모든
# 모의투자 배포에서 10분마다 영구히** error가 쌓인다. 아래 _PAPER_FALLBACK_SKIP_CYCLES의
# 호출 건너뛰기도 이 표지에 걸려 있어 함께 꺼진다. 다만 폭발 반경은 로그와 호출량으로
# 한정된다 — 그 경로도 None을 반환해 DB에는 아무것도 쓰지 않으므로 데이터가 망가지지는 않는다.
_RLZ_PL_PAPER_FALLBACK_MARKER = "잔고 요약으로 대체했습니다"

# 종목 줄: "1. 삼성전자 (005930) · 현금"
# STOCK_LINE_RE와 같은 이유로 코드 자리를 [0-9A-Z]{6,}로 제한한다 — [^()]*를 쓰면
# "(합성)" 같은 이름 속 괄호가 코드로 잡혀 종목명이 잘린다(#157). name의 .+가
# 탐욕적이라 마지막 " (코드) · "까지 물러나므로 이름 안의 괄호는 이름에 남는다.
# pdno가 비어 formatter가 "-"를 낸 줄은 코드 자리에 매치되지 않아 형식 위반으로 걸린다.
_RLZ_PL_STOCK_LINE_RE = re.compile(r"^\d+\. (?P<name>.+) \((?P<code>[0-9A-Z]{6,})\) · ")
# pdno가 비어 formatter가 "(-)"를 낸 줄. 위 정규식에 매치되지 않는다는 점은 같지만
# **원인이 다르다**: 이쪽은 응답에 코드가 없는 데이터 조건이고, 저쪽(형식 위반)은 JS
# 포맷터 회귀 신호다. 한 메시지로 묶으면 운영자가 있지도 않은 JS 회귀를 찾아 나선다.
_RLZ_PL_NO_CODE_LINE_RE = re.compile(r"^\d+\. (?P<name>.+) \(-\) · ")
# 종목 줄 판별용 접두사. "- "로 판별하는 balance 파서와 달리 이쪽은 번호 매김이다.
_RLZ_PL_INDEX_PREFIX_RE = re.compile(r"^\d+\. ")
# 현재가: "   보유 10주 | 현재가 71,200원 | 평가 712,000원"
# formatWon이 toLocaleString("ko-KR")로 쉼표를 넣는다. prpr이 비면 "현재가 -"가 되고
# 이 정규식에 매치되지 않아 그 종목은 결과에서 빠진다 — "-"를 0으로 읽으면 없던 시세가
# 생긴다(#122의 "0과 모름 구분"). 소수 허용은 _AVG_PRICE_RE와 같은 이유의 보수적 선택이다.
#
# **정규식만으로는 부족하다**: formatWon은 undefined·null·""에만 "-"를 내므로
# (formatters.js:7-10) prpr이 "0"이면 "현재가 0원"이 되어 이 정규식에 매치된다.
# 0원을 걸러 내는 일은 정규식이 아니라 _parse_rlz_pl_quotes의 값 검사가 맡는다.
_RLZ_PL_PRICE_RE = re.compile(r"현재가\s+([\d,]+(?:\.\d+)?)원")

# get_balance_rlz_pl에 도구 인자 time_budget_ms로 넘기는 연속조회 시간 예산(ms) — #369.
#
# 도구의 기본 예산은 NAT의 120초 호출자를 기준으로 잡은 90초다(mcp/trading/index.js의
# BALANCE_RLZ_PL_TIME_BUDGET_MS). 이 호출은 run_mcp_tool이 MCP_TOOL_TIMEOUT_SECONDS(30초,
# backend/ai/mcp_tools.py)에서 끊으므로, 기본 예산으로 부르면 연속조회가 30초를 넘는 계좌는 JS가
# 잘림 안내를 내기 전에 서브프로세스째 끊겨 504 실패로 빠진다. 예산을 30초 안으로 낮추면 그
# 계좌는 예산에서 멈춰 잘림 안내를 받고, _sync_portfolio_prices_from_rlz_pl의 잘림 경고가
# 사유("조회 시간 예산을 초과하여")를 그대로 싣는다. 시세를 못 얻는 결과는 504 때와 같다 —
# 잘린 응답은 쓰지 않는다. 달라지는 것은 원인이 로그에 드러나고, 호출이 30초 타임아웃이
# 아니라 예산 근처에서 끝난다는 점이다.
#
# 값: 30초에서 판정 직전에 나간 요청 1회의 kisAxios 타임아웃(8초)과 서브프로세스
# 기동·핸드셰이크(2초)를 빼면 20초이고, 2초를 남겨 18초로 잡았다. 예산 판정은 페이지
# 경계에서만 하므로 최악은 18 + 8 + 2 = 28초다. 기동 2초는 보수적 추정이다 — backend와 같은
# 경로(TRADING_MCP_PARAMS + stdio_client)로 로컬(Windows)에서 잰 기동·핸드셰이크는
# 0.55~0.67초였다(8회, PR #378 리뷰 반영 때 실측). "예산 + 8초 + 2초 < 호출 타임아웃"은
# test_scheduler.py가 세 값을 읽어 고정한다. 도구가 받는 범위는 1,000~90,000ms 정수다.
#
# 대가(PR #378 리뷰): 기본 예산으로는 약 28초까지 끝나던 연속조회가 이제 18초에서 잘린다.
# 연속조회가 18~28초 걸리는 계좌는 전에는 받던 시세를 매 주기 못 받고, TTL(30분)이 지나면
# price_known=false로 남는다. 같은 30초 상한 아래의 get_balance 선례(balance.js의
# BALANCE_TIME_BUDGET_MS, 15초)보다 여유를 더 써서 18초로 잡은 것은 이 구간을 좁히기 위해서다.
#
# 제외한 경우: 토큰이 판정 경계에서 만료되면 kisApiGet이 GET 앞에 토큰 발급(최대 8초)을
# 붙이고, 발급 락 경합이면 DEFAULT_LOCK_WAIT_MS(10초, mcp/trading/token-cache.js)가 더 붙어
# 30초를 넘을 수 있다. 드물고, balance.js의 15초 산정도 같은 전제를 둔다.
#
# 페이지 간 지연(KIS_BALANCE_RLZ_PL_PAGE_DELAY_MS)이 있으면 최악은 18초 + max(8초, 지연) +
# 기동이다. fetchAllPaged가 대기 뒤 예산을 다시 봐서(#307) 예산이 끝난 뒤에는 요청을 내지
# 않지만, 이미 들어간 대기는 끝까지 기다린다. 기본값 0이나 8초 이하 지연이면 위 28초가
# 그대로다. 그 env의 상한은 도구 기본 예산(90초) 기준이라 약 10초를 넘는 지연도 받아들여지고,
# 그런 설정에서는 이 호출이 여전히 30초에 걸린다 — #210에 기록했다.
_RLZ_PL_TIME_BUDGET_MS = 18_000

# get_balance_rlz_pl 연속 실패 횟수. get_balance의 _balance_failure_streak와 같은
# 문제를 같은 방식으로 막는다 — 다만 이쪽은 상시 실패가 **정상인** 배포가 있다.
# 모의투자 계좌에서는 도구가 매번 대체 응답을 주고, 실전 계좌라도 권한이 없으면 매
# 주기 실패한다. 그 배포에서 error를 10분마다 쌓으면 로그가 무의미해진다.
_quote_failure_streak = 0
_last_quote_error: str | None = None

# 모의투자 대체 응답 연속 횟수. 실패 경로(_quote_failure_streak)보다 이쪽이 억제가 더
# 필요하다 — 실패는 드물지만 모의투자 대체 응답은 **기본 개발 구성에서 매 주기 항상**
# 온다. 억제하지 않으면 10분마다 같은 info가 영구히 쌓여 로그가 신호를 잃는다.
_paper_fallback_streak = 0

# 실현손익 연속조회 잘림 연속 횟수(#369, PR #378 리뷰). 예산(_RLZ_PL_TIME_BUDGET_MS)을 넘는
# 계좌는 매 주기 잘리므로 _quote_failure_streak와 같은 규칙(첫 회, 사유가 바뀐 회, 이후 6회마다)으로
# 억제한다. 잘리지 않은 응답을 받으면 0으로 되돌린다.
_rlz_truncation_streak = 0
# 직전 잘림 경고의 사유(truncation_notice 결과). _last_quote_error와 같은 역할이다. 잘리지 않은
# 응답에서 되돌리지 않는다 — 그 뒤 첫 잘림은 연속 1회라 사유와 무관하게 warning이 나가므로
# 되돌려도 관측 가능한 차이가 없다.
_last_rlz_truncation_notice: str | None = None

# 모의투자 대체 응답을 받은 뒤 도구 호출 자체를 건너뛸 주기 수. 위 억제는 로그만 줄일 뿐
# 호출은 줄이지 못한다 — 모의투자 분기(index.js의 getBalanceRlzPl 첫 분기)는 TR 대신
# getBalance()를 다시 불러 전체 잔고를 페이징하고, 백엔드는 그 응답을 받은 뒤에야 버린다.
# 그대로 두면 같은 주기의 get_balance 동기화와 합쳐 잔고 TR 호출과 node 서브프로세스
# 기동이 기본 개발 구성에서 매 주기 두 배가 된다.
#
# 5 = 한 번 받고 다섯 주기를 쉰다 → 호출은 약 1시간(10분 × 6)에 한 번. 영구히 끊지 않는
# 것은 보수적 선택이다: 대체 응답이 끝나면 최대 한 시간 안에 시세 갱신이 돌아온다.
# 이 카운터는 _refresh_portfolio_prices(스케줄러 경로)만 본다. _sync_portfolio_prices_from_rlz_pl
# 은 받은 텍스트를 처리할 뿐 호출 여부를 정하지 않는다.
_PAPER_FALLBACK_SKIP_CYCLES = 5
_paper_fallback_skip_remaining = 0

# 코드 없음·현재가 없음 경고의 연속 횟수. 둘 다 **데이터 조건**이라 한 번 생기면
# 그 종목을 팔기 전까지 매 주기 그대로 재현된다. 시세 갱신은 monitor_market_task 안에서
# 10분 간격으로 장 운영 시간 게이트 없이 돌므로(하루 약 144회, 그중 100회 남짓이 장 밖),
# 억제하지 않으면 같은 종목 이름을 부르는 warning이 영구히 쌓여 로그가 신호를 잃는다.
# _paper_fallback_streak와 같은 규칙(첫 회 + 이후 6회마다 ≈ 1시간에 한 번)을 쓴다.
#
# divergent(매매구분별 현재가 엇갈림)에는 일부러 걸지 않는다. 그쪽은 데이터 조건이
# 아니라 "prpr은 행마다 같다"는 전제를 반증하는 단 하나의 증거 경로이고, 실계좌에서
# 관측된 적이 없어 상시로 도는 경고가 아니다. 억제했다가 첫 발생을 놓치면 이 브랜치가
# 그 경고를 남긴 이유 자체가 사라진다.
_codeless_streak = 0
_priceless_streak = 0

# ---------------------------------------------------------------------------
# 모의투자 시세: 잔고 평가금액 ÷ 수량
# ---------------------------------------------------------------------------
#
# 모의투자(openapivts)에서는 실현손익 TR이 없어 위 시세 소스가 영영 비고, current_price가
# 채워지지 않는다(Unity 파이 차트가 current_price × quantity로 비중을 내므로 아무것도 못
# 그린다). 그 배포에서 index.js의 getBalanceRlzPl은 get_balance 리포트를 그대로 돌려주는데
# (_RLZ_PL_PAPER_FALLBACK_MARKER 주석), 그 리포트의 종목 블록에는 "평가금액 {evlu_amt}원"이
# 있다. KIS inquire-balance의 evlu_amt는 현재가 × 보유수량이므로 수량으로 나누면 현재가다.
#
# 종목별 get_stock_quote를 부르는 대안은 고르지 않았다. 종목 수만큼 KIS 호출이 늘고(모의투자
# 초당 한도, #474처럼 동시에 부르면 EGW00215), #196에서 유지보수자가 이미 거부한 선택지다.
# 이 경로는 **추가 호출이 0건**이다 — 대체 응답 자체와, 건너뛰는 주기에는 같은 주기의
# get_balance 응답(monitor_market_task가 이미 받은 것)을 읽을 뿐이다.
#
# **모의투자 대체 응답을 받은 배포에서만** 돈다. 실전 계좌의 시세는 여전히 실현손익 TR
# 하나에서만 온다. 시세를 쓰는 줄(_apply_quotes)은 두 경로가 공유하므로 "price_updated_at을
# 찍는 곳은 한 줄"이라는 #196의 불변식은 그대로다.
#
# 의심스러운 값은 쓰지 않는다(#196과 같은 규율): 잘린 응답, 평가금액이 "-"·0원·소수·
# 마스킹("*")·쉼표 위치가 어긋난 값, 수량 0, 평가금액이 수량으로 나누어떨어지지 않는 경우
# (현재가 × 수량이 아니라는 뜻이다), 같은 코드가 서로 다른 값을 내는 경우.
#
# 형식은 mcp/trading/balance.js formatBalanceReport()의 종목 블록이고, 공유 픽스처
# mcp/trading/tests/fixtures/balance_report.json으로 고정한다(test_scheduler.py).
#   줄1: "- {prdt_name} ({pdno}) · {hldg_qty}주"
#   줄2: "  평단가 {pchs_avg_pric}원 → 평가금액 {evlu_amt}원"
# toLocaleString("ko-KR")의 세 자리 쉼표만 허용한다. 줄 끝($)에 고정해 "210,000.5원"처럼
# 정수부만 잘라 읽는 일이 없게 한다.
_PAPER_QTY_RE = re.compile(r"\) · (?P<qty>\d{1,3}(?:,\d{3})*)주$")
_PAPER_EVAL_AMOUNT_RE = re.compile(r" → 평가금액 (?P<amount>\d{1,3}(?:,\d{3})*)원$")

# 평가금액으로 현재가를 얻지 못한 종목 경고의 연속 횟수. _priceless_streak와 같은 이유·규칙
# (첫 회 + 이후 6회마다)이다 — 데이터 조건이라 한 번 생기면 매 주기 그대로 재현된다.
_paper_unpriced_streak = 0


@dataclass(frozen=True)
class _BalanceHolding:
    """_parse_balance_holdings의 파싱 결과 단위."""
    code: str
    name: str
    quantity: int
    avg_price: float


def _parse_balance_holdings(balance_text: str) -> list[_BalanceHolding]:
    """get_balance 텍스트에서 보유 종목의 코드·이름·수량·평단가를 파싱합니다.

    mcp/trading/balance.js의 formatBalanceReport()가 생성하는 3줄 블록 형식에
    의존합니다:
      줄1: "- {prdt_name} ({pdno}) · {hldg_qty}주"
      줄2: "  평단가 {pchs_avg_pric}원 → 평가금액 {evlu_amt}원"
      줄3: "  손익 ... · 수익률 ..."

    current_price는 이 포맷에 포함되지 않아 파싱하지 않습니다.
    inquire-balance(TTTC8434R) output1에 현재가 필드가 없기 때문입니다
    (이슈 #122 후속 이슈 참고).

    잘림 가드와 마커 부재 가드는 호출처(_sync_portfolio_from_balance)가 담당하므로
    이 함수는 있는 그대로 파싱합니다. _BALANCE_HOLDINGS_MARKER가 없으면 [] 반환.

    형식 위반 줄(코드 괄호 누락 등)은 모아서 호출당 1회 경고 로그를 남깁니다.
    수량·평단가 파싱이 실패한 줄도 경고를 남기고 기본값(0)을 사용합니다.
    extract_stocks_from_balance는 이 함수에 파싱을 위임합니다.
    """
    holdings: list[_BalanceHolding] = []
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        return holdings

    section = balance_text.split(_BALANCE_HOLDINGS_MARKER)[1]
    lines = section.split("\n")
    malformed: list[str] = []

    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue

        match = STOCK_LINE_RE.match(line)
        if match is None:
            malformed.append(line)
            continue

        name = match.group("name").strip()
        code = match.group("code")
        if not name:
            continue

        # 수량: "- 삼성전자 (005930) · 3주" → 3
        qty_match = _QTY_RE.search(line)
        quantity = 0
        if qty_match:
            try:
                quantity = int(qty_match.group(1).replace(",", ""))
            except ValueError:
                logger.warning("[%s] 수량 파싱 실패(ValueError). quantity=0으로 저장합니다: %r", name, line)
        else:
            logger.warning("[%s] 수량 정규식이 매치되지 않아 quantity=0으로 저장합니다: %r", name, line)

        # 평단가: 다음 줄 "  평단가 67,000원 → ..." → 67000.0 (소수 포함)
        avg_price = 0.0
        if i + 1 < len(lines):
            avg_match = _AVG_PRICE_RE.search(lines[i + 1])
            if avg_match:
                try:
                    avg_price = float(avg_match.group(1).replace(",", ""))
                except ValueError:
                    logger.warning(
                        "[%s] 평단가 파싱 실패(ValueError). avg_price=0으로 저장합니다: %r",
                        name, lines[i + 1],
                    )
            else:
                logger.warning(
                    "[%s] 평단가 정규식이 매치되지 않아 avg_price=0으로 저장합니다: %r",
                    name, lines[i + 1] if i + 1 < len(lines) else "(다음 줄 없음)",
                )

        holdings.append(_BalanceHolding(code=code, name=name, quantity=quantity, avg_price=avg_price))

    if malformed:
        logger.warning(
            "예상치 못한 잔고 종목 줄 형식 %d건을 건너뛰었습니다(예시 최대 3건): %r",
            len(malformed), malformed[:3],
        )
    return holdings


def _sync_portfolio_from_balance(
    balance_text: str,
    session: Session,
    *,
    holdings: list[_BalanceHolding] | None = None,
) -> int | None:
    """get_balance 응답을 바탕으로 Portfolio 테이블을 동기화합니다.

    전략: stock_code 기준 upsert (#196). 잔고에 있는 종목은 기존 행을 갱신하고,
    없던 종목만 새로 삽입하며, 보유하지 않게 된 종목의 행은 삭제합니다. 매 잔고
    조회마다 실행되므로 "잔고에서 사라진 종목은 제거된다"는 동작은 전량 교체
    시절과 동일합니다.

    upsert여야 하는 이유는 **잔고 응답에 없는 필드를 보존**하기 위해서입니다.
    전량 교체(DELETE + INSERT)는 그런 필드를 매 주기 기본값으로 되돌립니다.
    current_price는 _sync_portfolio_prices_from_rlz_pl이 따로 채우므로, 전량 교체로
    돌아가면 10분 주기마다 그 값이 null로 덮여 price_known 플래그가 "시세를 채웠는데도
    모름"으로 잘못 나가는 회귀가 됩니다.

    잔고가 잘린 경우(is_balance_truncated) 동기화를 수행하면 아직 파악되지 않은
    보유 종목이 삭제될 수 있으므로, 동기화를 건너뛰고 기존 데이터를 보존합니다.

    current_price와 price_updated_at은 이 함수가 **읽지도 쓰지도 않습니다.**
    get_balance(inquire-balance, TTTC8434R) output1에 현재가 필드가 있는지는 여전히
    미확인이고, 시세를 쓰는 경로는 _apply_quotes 하나뿐입니다(#196 — 실전은 실현손익 TR,
    모의투자는 _sync_portfolio_prices_from_paper_balance가 그리로 모읍니다).
    두 함수가 같은 컬럼을 쓰면 10분 주기마다 시세가 null로 덮이거나, 나이를 모르는
    값에 새 타임스탬프가 찍혀 "모르는데 안다"가 됩니다. 그래서 신규 행은 모델 기본값
    그대로 둘 다 null이고, 기존 행의 값은 손대지 않은 채 보존됩니다.
    null 수익률이 "실제 0%"와 혼동되지 않도록 /api/v1/portfolio 응답에서
    return_rate: null로 구분됩니다(이슈 #122).

    updated_at은 반대로 매 주기 갱신됩니다 — 그 값의 뜻은 "잔고를 마지막으로 확인한
    시각"이지 "시세를 마지막으로 갱신한 시각"이 아닙니다. 시세 나이는 price_updated_at
    으로만 잽니다(#196).

    holdings: 호출처에서 이미 _parse_balance_holdings를 실행한 경우 그 결과를 전달하면
    재파싱을 건너뜁니다. None이면 balance_text에서 직접 파싱합니다.
    잘림·마커 가드는 holdings 인자 유무에 관계없이 항상 balance_text를 검사합니다.

    반환값(이슈 #229): 동기화를 실제로 수행했으면 동기화한 보유 종목 수(int, 0 이상)를,
    잘림·마커 부재로 건너뛰었으면 None을 반환합니다. 이 값은 잔고 응답의 항목 수이지
    테이블 행 수가 아닙니다 — upsert 후에는 삽입이 0건일 수 있고, 한 응답에 같은
    코드가 두 번 오면 반환값이 행 수보다 큽니다. bool 대신 개수를 반환하는 이유는
    호출처(monitor_market_task)가 WebSocket으로 PORTFOLIO_UPDATE를 브로드캐스트할 때
    holdings_count를 함께 실어야 하는데, bool만으로는 호출처가 그 값을 얻기 위해
    holdings 리스트를 별도로 들고 있거나 다시 세야 하기 때문입니다. int 반환값 자체가
    "동기화 성공 여부"와 "성공 시 종목 수"를 한 번에 전달합니다.
    """
    if is_balance_truncated(balance_text):
        logger.warning(
            "잔고 연속조회가 잘려 Portfolio 동기화를 건너뜁니다. 기존 데이터를 유지합니다."
        )
        return None

    # 마커가 없으면 "보유 0건"이 아니라 "응답을 읽지 못함"이다.
    # balance.js는 보유 종목이 없어도 마커와 "보유 종목이 없습니다."를 항상 출력한다
    # (balance.js formatBalanceReport()). run_mcp_tool은 MCP content가 비면 예외 대신 ""를
    # 반환하므로(ai/mcp_tools.py) 이 경로는 실재한다. upsert로 바뀐 뒤에도 "잔고에 없는
    # 종목은 삭제"는 그대로라 빈 응답으로 동기화하면 전 종목이 지워지고 되돌릴 수
    # 없다. 기존 데이터를 보존하고 error 로그로 운영자에게 알린다.
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        logger.error(
            "잔고 응답에서 %r 섹션을 찾지 못해 Portfolio 동기화를 건너뜁니다 "
            "(응답 길이 %d). 기존 데이터를 유지합니다.",
            _BALANCE_HOLDINGS_MARKER,
            len(balance_text),
        )
        return None


    # 호출처에서 이미 파싱한 결과가 있으면 재파싱을 건너뛴다(경고 중복 방지).
    if holdings is None:
        holdings = _parse_balance_holdings(balance_text)

    # stock_code 기준 upsert (#196). 기존 행을 코드별로 모은다.
    #
    # dict[str, list[Portfolio]]인 이유: Portfolio.stock_code에 유일성 제약이 없어
    # (models.py는 index=True만 선언한다) 같은 코드의 행이 둘 이상 존재할 수
    # 있는 상태를 스키마가 막아주지 않는다. 첫 행만 갱신 대상으로 남기고 나머지는
    # 아래에서 삭제해, 동기화가 돌 때마다 테이블이 "코드당 1행"으로 수렴한다.
    # 이 정리가 없으면 upsert가 중복 중 어느 행을 갱신했는지에 따라 API 응답이
    # 달라져 비결정적이 된다.
    existing: dict[str, list[Portfolio]] = {}
    for row in session.exec(select(Portfolio)).all():
        existing.setdefault(row.stock_code, []).append(row)

    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    for h in holdings:
        rows = existing.get(h.code)
        if rows:
            row = rows[0]
            # 잔고 응답에서 온 필드만 갱신한다. current_price·price_updated_at은
            # 여기에 없으므로 건드리지 않는다 — 근거는 이 함수 docstring의
            # "upsert여야 하는 이유"와 #196의 시세 경로 분리.
            row.stock_name = h.name
            row.quantity = h.quantity
            row.avg_price = h.avg_price
        else:
            # 신규 행. current_price·price_updated_at을 넘기지 않으므로 모델 기본값
            # None이 된다 — 전량 교체 시절 명시적으로 current_price=None을 넘기던 것과
            # 결과가 같고, 다음 시세 갱신 주기가 두 값을 함께 채운다(#196).
            row = Portfolio(
                stock_code=h.code,
                stock_name=h.name,
                quantity=h.quantity,
                avg_price=h.avg_price,
            )
            session.add(row)
            # 방금 만든 행도 existing에 등록한다. 한 응답 안에 같은 코드가 두 번
            # 나오면(KIS output1은 pdno가 키라 정상 응답에서는 없는 일이지만
            # 스키마가 막아주지 않는다) 등록하지 않을 경우 두 번째 항목이 또 하나의
            # 새 행을 만들어, 이 함수가 스스로 중복을 만들어 낸다.
            existing[h.code] = [row]
        # updated_at은 값 변화와 무관하게 매번 갱신한다 — 전량 교체 시절과 같은
        # 의미("잔고를 마지막으로 확인한 시각")를 유지하기 위해서다.
        row.updated_at = now
        seen.add(h.code)

    # 보유하지 않게 된 종목 제거 + 코드 중복 행 정리(위 주석 참고).
    # 전량 교체 시절의 "잔고에 없는 종목은 사라진다" 동작을 그대로 유지한다.
    for code, rows in existing.items():
        stale = rows if code not in seen else rows[1:]
        for row in stale:
            session.delete(row)

    # 커밋 경계는 전량 교체 시절과 동일하다 — 한 동기화가 한 트랜잭션이고,
    # 갱신·삽입·삭제가 함께 커밋되거나 함께 롤백된다.
    session.commit()
    # 반환값은 동기화한 보유 종목 수(len(holdings))로 유지한다. 갱신/삽입을 나눠
    # 세면 호출처(#229의 PORTFOLIO_UPDATE holdings_count)가 보는 값이 바뀐다.
    logger.info("Portfolio 동기화 완료: %d개 종목", len(holdings))
    return len(holdings)


def _parse_paper_balance_quotes(balance_text: str) -> dict[str, float]:
    """get_balance 텍스트에서 종목코드 → 현재가(평가금액 ÷ 수량)를 파싱합니다.

    모의투자 배포 전용이다(_PAPER_EVAL_AMOUNT_RE 위 주석). 잘림·마커 가드는 호출처
    (_sync_portfolio_prices_from_paper_balance)가 맡고, 이 함수는 값 검사만 한다.

    값을 얻지 못한 종목은 결과에서 **뺀다** — 0이나 근사값으로 채우지 않는다(#122의
    "0과 모름 구분"). 평가금액이 수량으로 나누어떨어지지 않으면 그 값은 현재가 × 수량이
    아니므로 반올림해 쓰지 않는다. 같은 코드가 두 번 나와 값이 엇갈리면 어느 쪽도 쓰지
    않는다(KIS output1은 pdno가 키라 정상 응답에서는 없는 일이다).
    """
    global _paper_unpriced_streak

    quotes: dict[str, float] = {}
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        return quotes

    lines = balance_text.split(_BALANCE_HOLDINGS_MARKER)[1].split("\n")
    unpriced: list[str] = []
    conflicted: set[str] = set()

    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        match = STOCK_LINE_RE.match(line)
        if match is None:
            # 형식 위반 줄 경고는 같은 텍스트를 먼저 파싱하는 _parse_balance_holdings가 낸다.
            continue
        code = match.group("code")
        name = match.group("name").strip()

        price: float | None = None
        qty_match = _PAPER_QTY_RE.search(line)
        amount_match = (
            _PAPER_EVAL_AMOUNT_RE.search(lines[i + 1]) if i + 1 < len(lines) else None
        )
        if qty_match is not None and amount_match is not None:
            quantity = int(qty_match.group("qty").replace(",", ""))
            amount = int(amount_match.group("amount").replace(",", ""))
            if quantity > 0 and amount > 0 and amount % quantity == 0:
                price = float(amount // quantity)

        if price is None:
            unpriced.append(name)
            continue
        if code in conflicted:
            continue
        if code in quotes and quotes[code] != price:
            del quotes[code]
            conflicted.add(code)
            unpriced.append(name)
            continue
        quotes[code] = price

    if unpriced:
        _paper_unpriced_streak += 1
        if _paper_unpriced_streak == 1 or _paper_unpriced_streak % 6 == 0:
            logger.warning(
                "잔고 평가금액으로 현재가를 얻지 못한 종목 %d건은 시세를 갱신하지 않습니다"
                "(%d회 연속, 예시 최대 3건): %r. 평가금액이 비었거나 0원·소수이거나 수량으로 "
                "나누어떨어지지 않는 경우입니다.",
                len(unpriced), _paper_unpriced_streak, unpriced[:3],
            )
        else:
            logger.debug(
                "평가금액으로 현재가를 얻지 못한 종목 %d건이 %d회 연속됩니다.",
                len(unpriced), _paper_unpriced_streak,
            )
    else:
        _paper_unpriced_streak = 0
    return quotes


def _sync_portfolio_prices_from_paper_balance(balance_text: str, session: Session) -> int | None:
    """모의투자 배포에서 get_balance 텍스트의 평가금액으로 Portfolio 시세를 채웁니다.

    호출처는 둘이고 둘 다 모의투자 대체 응답을 받은 뒤에만 부른다:
      - _sync_portfolio_prices_from_rlz_pl의 대체 응답 분기(대체 응답 본문이 곧 get_balance
        리포트다)
      - _refresh_portfolio_prices의 건너뛰는 주기(같은 주기의 get_balance 응답)

    반환값 규칙은 _sync_portfolio_prices_from_rlz_pl과 같다 — int는 갱신한 행 수, None은
    응답을 믿을 수 없어 아무것도 쓰지 않은 경우. 잘림·마커 부재는 같은 텍스트로 도는 잔고
    동기화(_sync_portfolio_from_balance, monitor의 잘림 경고)가 이미 warning·error로 알리므로
    여기서는 debug로만 남긴다.
    """
    if is_balance_truncated(balance_text):
        logger.debug("잔고 연속조회가 잘려 모의투자 시세 갱신을 건너뜁니다.")
        return None
    if _BALANCE_HOLDINGS_MARKER not in balance_text:
        logger.debug("잔고 응답에 보유 종목 섹션이 없어 모의투자 시세 갱신을 건너뜁니다.")
        return None

    quotes = _parse_paper_balance_quotes(balance_text)
    if not quotes:
        return 0
    return _apply_quotes(quotes, session)


def _parse_rlz_pl_quotes(report_text: str) -> dict[str, float]:
    """get_balance_rlz_pl 텍스트에서 종목코드 → 현재가를 파싱합니다 (#196).

    mcp/trading/balance-rlz-pl-report.js의 formatBalanceRlzPlReport()가 만드는 4줄
    블록 형식에 의존합니다:
      줄1: "{n}. {prdt_name} ({pdno}) · {trad_dvsn_name}"
      줄2: "   보유 {hldg_qty}주 | 현재가 {prpr}원 | 평가 {evlu_amt}원"
      줄3: "   평가손익 ... | 매입가 ..."
      줄4: "   금일 매수 ... | 전일대비 ..."
    현재가는 줄2에서만 읽습니다 — 줄3의 "매입가"를 현재가로 오인하지 않도록 검색
    범위를 바로 다음 한 줄로 한정합니다(_parse_balance_holdings가 평단가를 다루는
    방식과 같습니다).

    **반환 키가 코드인 이유(중복 행 처리)**: KIS는 같은 종목이라도 매매구분
    (trad_dvsn_name: 현금/신용 등)마다 행을 따로 줍니다. 즉 한 응답에 같은 pdno가
    여러 번 나오는 것이 정상입니다. prpr은 그 종목의 시장 가격이므로 어느 행에서 읽어도
    같은 값일 **것으로 보고**, 코드를 키로 삼아 **처음으로 유효한 시세가 있는 행**을
    채택하고 그 뒤의 중복은 버립니다. 첫 행이 아니라 "처음으로 유효한 행"인 이유는
    앞선 행의 prpr이 비었거나 "0"일 수 있기 때문입니다 — 그런 행은 시세가 아니라
    "모름"이므로(아래 0원 처리 참고) 뒤 행이 값을 주면 그 값을 씁니다.
    합산할 값이 아니므로 더하면 안 되고(그러면 현재가가 행 수만큼 부풀고), Portfolio는
    코드당 1행이라 매매구분별로 나눠 저장할 자리도 없습니다. 값이 서로 다르게 온다면
    그것은 응답 자체의 이상이지 이 함수가 조정할 문제가 아니며, 그 경우에도 처음으로
    유효한 값을 쓰는 편이 결정적입니다.

    **이 전제는 추론이지 관측이 아닙니다.** TTTC8494R 실계좌 응답을 아직 아무도 보지
    못했으므로, 행마다 prpr이 같다는 보장도 prpr이 채워져 온다는 보장도 없습니다.
    그래서 어긋난 값을 조용히 버리지 않고 warning으로 남깁니다 — 실측 전까지 이 전제를
    반증할 수 있는 경로는 프로덕션 로그뿐입니다.

    잘림·마커·모의투자 가드는 호출처(_sync_portfolio_prices_from_rlz_pl)가 담당하므로
    이 함수는 있는 그대로 파싱합니다. 마커가 없으면 {} 반환.

    현재가를 읽지 못한 종목은 **결과에서 뺍니다.** prpr이 비면 formatWon이 "-"를
    내는데 그것을 0으로 읽으면 없던 시세가 생깁니다(#122의 "0과 모름 구분"). 빠진
    종목의 기존 값은 호출처가 손대지 않으므로 그대로 남고, TTL이 지나면 스스로
    "모름"이 됩니다 — 신선도 게이트를 붙인 이유가 그것입니다.

    **코드 없음·현재가 없음 경고는 연속 횟수로 억제합니다.** 둘 다 응답이 그렇게 오는
    데이터 조건이라 한 번 생기면 그 종목을 정리하기 전까지 매 주기 그대로 재현되는데,
    이 경로는 장 운영 시간 게이트 없이 10분마다 돕니다. 억제하지 않으면 같은 종목
    이름을 부르는 warning이 하루 100여 건씩 영구히 쌓입니다
    (_codeless_streak·_priceless_streak 주석 참고). divergent는 일부러 억제하지
    않습니다 — 그 이유도 같은 주석에 적었습니다.
    """
    global _codeless_streak, _priceless_streak

    quotes: dict[str, float] = {}
    if _RLZ_PL_HOLDINGS_MARKER not in report_text:
        return quotes

    section = report_text.split(_RLZ_PL_HOLDINGS_MARKER)[1]
    lines = section.split("\n")
    malformed: list[str] = []
    codeless: list[str] = []
    # (코드, 종목명)으로 모읍니다. 이름만 모으면 나중 행이 시세를 채워 준 종목까지
    # "현재가를 읽지 못했다"고 이름을 부르게 됩니다 — append 시점에는 그 종목이
    # quotes에 없는 것이 맞지만, 뒤에 오는 매매구분 행이 값을 줄 수 있기 때문입니다.
    priceless: list[tuple[str, str]] = []
    divergent: list[tuple[str, float, float]] = []

    for i, line in enumerate(lines):
        if _RLZ_PL_INDEX_PREFIX_RE.match(line) is None:
            continue

        match = _RLZ_PL_STOCK_LINE_RE.match(line)
        if match is None:
            no_code = _RLZ_PL_NO_CODE_LINE_RE.match(line)
            if no_code is not None:
                codeless.append(no_code.group("name").strip())
            else:
                malformed.append(line)
            continue

        name = match.group("name").strip()
        code = match.group("code")
        # 종목명이 비었는지는 보지 않는다. formatter가 `${row.prdt_name || "-"}`를 내므로
        # (balance-rlz-pl-report.js formatBalanceRlzPlReport()) 빈 이름은 사실상 나오지 않고, 설령 공백뿐인
        # prdt_name으로 도달하더라도 name은 로그 표시용일 뿐이다 — 그것 때문에 멀쩡한
        # 코드의 시세를 버리면 이름 하나 때문에 값을 잃는다.
        # 중복 코드 판정보다 **먼저** 값을 읽습니다. 두 번째 행을 곧바로 버리면
        # "prpr은 행마다 같다"는 전제가 틀렸을 때 그 사실이 흔적 없이 사라집니다.
        price_line = lines[i + 1] if i + 1 < len(lines) else ""
        price_match = _RLZ_PL_PRICE_RE.search(price_line)
        price: float | None = None
        if price_match is not None:
            try:
                parsed = float(price_match.group(1).replace(",", ""))
            except ValueError:
                # 숫자·쉼표만 매치된 문자열이라 도달하기 어렵지만, 도달했다면 값을
                # 채우는 대신 "모름"으로 둡니다.
                parsed = None
            if parsed is not None and parsed > 0:
                price = parsed
            # parsed가 0 이하이면 price는 None으로 남습니다. prpr이 "0"이면 formatWon이
            # "0원"을 내므로 위 정규식은 매치된다(formatters.js:7-10은 undefined·null·""
            # 에만 "-"를 낸다). 그러나 0원짜리 보유 종목은 없습니다 — 거래정지·장 시작
            # 전·신규 상장처럼 시세가 아직 없는 상태이지 "현재가가 0원"이 아닙니다.
            # 그대로 쓰면 평가액 0원·수익률 -100%가 price_known=True로, 즉 **신선하고
            # 확실한 값**으로 나갑니다 — 이 이슈가 없애려는 결함 그 자체입니다.
            # "-"와 같은 취급으로 결과에서 뺍니다(#122의 "0과 모름 구분"). 음수는 KIS가
            # 낼 값이 아니지만 같은 이유로 함께 막습니다.

        if code in quotes:
            # 매매구분이 다른 같은 종목의 뒤 행. 이미 채택한 값을 그대로 씁니다
            # (docstring 참고). 다만 값이 **다르면** 기록합니다 — 그 전제가 틀렸다는 유일한 증거라
            # 조용히 버리면 실계좌 실측 전까지 영영 반증할 수 없습니다.
            if price is not None and price != quotes[code]:
                divergent.append((code, quotes[code], price))
            continue

        if price is None:
            priceless.append((code, name))
            continue
        quotes[code] = price

    if malformed:
        logger.warning(
            "예상치 못한 실현손익 종목 줄 형식 %d건을 건너뛰었습니다(예시 최대 3건): %r. "
            "포맷터(balance-rlz-pl-report.js) 회귀일 수 있습니다.",
            len(malformed), malformed[:3],
        )
    if codeless:
        _codeless_streak += 1
        if _codeless_streak == 1 or _codeless_streak % 6 == 0:
            logger.warning(
                "pdno가 비어 종목 코드를 읽지 못한 %d건은 시세를 갱신하지 않습니다(%d회 연속, "
                "예시 최대 3건): %r. 형식 위반이 아니라 응답에 코드가 없는 데이터 조건입니다 "
                "— 포맷터 회귀가 아닙니다.",
                len(codeless), _codeless_streak, codeless[:3],
            )
        else:
            logger.debug(
                "코드 없는 종목 줄 %d건이 %d회 연속됩니다.", len(codeless), _codeless_streak
            )
    else:
        _codeless_streak = 0
    # 끝까지 값을 못 얻은 종목만 부릅니다. 같은 코드의 뒤 행이 시세를 채워 줬다면
    # 그 종목은 갱신되었으므로 이름을 부르면 거짓입니다(예: prpr "0" 행 뒤에
    # 71,200원 행이 오면 quotes에는 71200이 들어 있는데도 이름이 불렸습니다).
    unpriced = [name for code, name in priceless if code not in quotes]
    if unpriced:
        _priceless_streak += 1
        if _priceless_streak == 1 or _priceless_streak % 6 == 0:
            logger.warning(
                "현재가를 읽지 못한 종목 %d건은 시세를 갱신하지 않습니다(%d회 연속, "
                "예시 최대 3건): %r",
                len(unpriced), _priceless_streak, unpriced[:3],
            )
        else:
            logger.debug(
                "현재가 없는 종목 %d건이 %d회 연속됩니다.", len(unpriced), _priceless_streak
            )
    else:
        _priceless_streak = 0
    if divergent:
        # 이 로그가 존재하는 이유가 곧 이 로그의 내용입니다. 아무도 TTTC8494R 실계좌
        # 응답을 본 적이 없으므로 "prpr은 매매구분과 무관한 시장 가격"이라는 전제는
        # 추론이지 관측이 아닙니다. 실계좌 실측 전까지 그 전제를 반증할 수 있는 경로는
        # 프로덕션 로그뿐이라, 어긋난 값을 조용히 버리면 증거 자체가 사라집니다.
        logger.warning(
            "같은 종목의 매매구분별 행에서 현재가가 엇갈립니다 %d건 — 처음으로 유효한 시세가 "
            "있는 행의 값을 채택했습니다 "
            "(코드, 채택값, 버린 값, 예시 최대 3건): %r. prpr이 행마다 같다는 전제는 아직 "
            "실계좌로 확인된 바 없으며, 이 경고가 실측 전까지 그 전제를 반증할 수 있는 "
            "유일한 경로입니다 — 보이면 이슈로 올려 주세요.",
            len(divergent), divergent[:3],
        )
    return quotes


def _sync_portfolio_prices_from_rlz_pl(report_text: str, session: Session) -> int | None:
    """get_balance_rlz_pl 응답으로 Portfolio의 시세와 그 갱신 시각을 채웁니다 (#196).

    이 함수만 current_price와 price_updated_at을 씁니다. 잔고 동기화
    (_sync_portfolio_from_balance)는 두 컬럼을 읽지도 쓰지도 않습니다 — 그래야
    10분 주기 잔고 동기화가 방금 채운 시세를 null로 되돌리지 않습니다.

    **행을 만들지도 지우지도 않습니다.** 어떤 종목을 보유하는지는 잔고 동기화의 몫이고,
    이쪽이 없는 코드를 새로 넣으면 잔고에 없는 종목이 시세만 가진 유령 행으로 남습니다.
    응답에 있으나 테이블에 없는 코드는 건너뜁니다 — 다음 잔고 동기화가 행을 넣어 주면
    그 다음 주기에 시세가 붙습니다.

    가드는 PR #342가 잔고 동기화에 세운 것과 같은 규율입니다 — **의심스러우면 쓰지
    않는다.** 다만 파괴성의 형태가 다릅니다: 잔고 쪽은 잘못 쓰면 행이 지워지고,
    이쪽은 잘못 쓰면 틀린 시세가 "신선함"으로 단언됩니다. 후자가 이 이슈가 없애려는
    결함 그 자체이므로 같은 강도로 막습니다.

    반환값:
      - int: 시세를 갱신한 행 수 (0 포함 — 보유 0건이거나 매칭된 코드가 없을 때)
      - None: 응답을 신뢰할 수 없어 아무것도 쓰지 않고 건너뛴 경우

    건너뛰는 세 경우와 로그 수준:
      1) 모의투자 대체 응답 — 그 배포에서는 매 주기의 정상 동작이므로 info이고,
         게다가 **항상** 오므로 연속 횟수로 억제한다(받은 응답 기준 첫 회와 이후 6회마다).
         억제 강도가 실패 경로보다 높은 것이 맞다 — 실패는 드물고 이쪽은 상시다.
         호출 자체를 줄이는 일은 호출처(_refresh_portfolio_prices)가 맡는다.
         실현손익 시세는 없지만 대체 응답 본문(get_balance 리포트)의 평가금액 ÷ 수량으로
         시세를 채운다(_sync_portfolio_prices_from_paper_balance). 그 반환값을 그대로 돌려준다.
      2) 연속조회 잘림 — 부분 응답이라 warning. 얻은 종목만 써도 행이 지워지지는
         않지만, 잘린 응답은 마지막 블록이 중간에서 끊겨 있을 수 있고 "의심스러운
         응답으로는 쓰지 않는다"를 잔고 동기화와 갈라 둘 이유가 없습니다.
      3) 마커 부재(빈 계좌 문구도 없음) — 응답을 읽지 못한 것이므로 error.
    """
    global _paper_fallback_streak, _rlz_truncation_streak, _last_rlz_truncation_notice

    # 순서가 중요합니다. 모의투자 대체 응답에는 "[보유 종목]" 마커가 없으므로, 이
    # 검사를 뒤로 미루면 마커 부재 가드가 먼저 걸려 정상 상황에 error가 남습니다.
    if _RLZ_PL_PAPER_FALLBACK_MARKER in report_text:
        _paper_fallback_streak += 1
        # _quote_failure_streak와 같은 규칙이되 이쪽이 더 절실합니다 — 실패는 드물지만
        # 모의투자에서는 이 분기가 **매 주기 정상**입니다. 이 카운터는 "받은 대체 응답 수"
        # 라서, 스케줄러 경로에서는 _PAPER_FALLBACK_SKIP_CYCLES가 호출을 약 1시간에 한 번으로
        # 줄인 뒤 첫 회 + 약 6시간에 한 번이 됩니다.
        if _paper_fallback_streak == 1 or _paper_fallback_streak % 6 == 0:
            logger.info(
                "모의투자 계좌는 실현손익 TR을 지원하지 않아 잔고 평가금액(÷ 수량)으로 시세를 "
                "채웁니다(%d회 연속).",
                _paper_fallback_streak,
            )
        else:
            logger.debug(
                "모의투자 대체 응답이 %d회 연속됩니다.", _paper_fallback_streak
            )
        # 대체 응답 본문은 get_balance 리포트 + 안내 문구다(index.js의 getBalanceRlzPl 첫 분기).
        # 안내 줄은 "- "로 시작하지 않고 잘림 판별자("조회가 중단되어")도 없어 그대로 넘긴다.
        return _sync_portfolio_prices_from_paper_balance(report_text, session)

    if _paper_fallback_streak:
        logger.info(
            "실현손익 TR 응답으로 돌아왔습니다. 직전까지 %d회 연속 모의투자 대체 응답이었습니다.",
            _paper_fallback_streak,
        )
        _paper_fallback_streak = 0

    if is_balance_truncated(report_text):
        # 사유(시간 예산·페이지 상한·오류)를 JS 안내 줄 그대로 싣는다. #369에서 예산을 낮춘 뒤
        # 이 경고가 "예산 초과"를 드러내는 곳이다(PR #378 리뷰).
        #
        # 억제 규칙은 _quote_failure_streak와 같다 — 첫 회, 사유(안내 줄)가 바뀐 회, 이후 6회마다만
        # warning이다. 연속조회가 예산을 넘는 계좌는 매 주기 잘리는데, 예산을 낮추기 전 그 계좌가
        # 빠지던 504 실패 경로는 그 규칙으로 억제돼 있었다 — 억제 없이 두면 이 PR이 10분마다
        # 쌓이는 warning을 새로 만든다. 사유 변경을 곧바로 올리는 이유: 시간 예산에서 오류로
        # 바뀌면 운영 대응이 달라지는데, 횟수로만 억제하면 6회째(최대 50분 뒤)까지 debug로만
        # 남는다(PR #378 리뷰).
        _rlz_truncation_streak += 1
        notice = truncation_notice(report_text)
        if (
            _rlz_truncation_streak == 1
            or notice != _last_rlz_truncation_notice
            or _rlz_truncation_streak % 6 == 0
        ):
            logger.warning(
                "실현손익 연속조회가 잘려 이번 주기 시세 갱신을 건너뜁니다(%d회 연속) — %s "
                "기존 시세와 갱신 시각을 유지합니다.",
                _rlz_truncation_streak,
                notice,
            )
        else:
            logger.debug(
                "실현손익 연속조회 잘림이 %d회 연속됩니다 — %s", _rlz_truncation_streak, notice
            )
        _last_rlz_truncation_notice = notice
        return None

    _rlz_truncation_streak = 0

    if _RLZ_PL_HOLDINGS_MARKER not in report_text:
        # 마커가 없는 정상 경우가 하나 있습니다: 보유 0건. balance.js와 달리 이
        # 포맷터는 0건이면 마커를 아예 내지 않습니다(balance-rlz-pl-report.js formatBalanceRlzPlReport()).
        if _RLZ_PL_EMPTY_MARKER in report_text:
            logger.info("실현손익 응답에 보유 종목이 없어 갱신할 시세가 없습니다.")
            return 0
        logger.error(
            "실현손익 응답에서 %r 섹션을 찾지 못해 시세 갱신을 건너뜁니다 "
            "(응답 길이 %d). 기존 시세와 갱신 시각을 유지합니다.",
            _RLZ_PL_HOLDINGS_MARKER,
            len(report_text),
        )
        return None

    quotes = _parse_rlz_pl_quotes(report_text)
    if not quotes:
        # 마커는 있는데 한 건도 파싱되지 않았습니다. 포맷 변경이거나 전 종목 prpr
        # 누락이며, 어느 쪽이든 쓸 값이 없습니다. DB에 손대지 않고 0을 반환합니다
        # (응답 자체는 읽었으므로 None이 아닙니다).
        #
        # 전 종목이 현재가 없음·코드 없음인 데이터 조건이면 파서가 방금 그 상황을 연속
        # 횟수로 억제한 경고로 이미 설명했습니다(카운터가 0이 아니면 이번 파싱에서 해당
        # 종목이 있었다는 뜻입니다). 여기서 매 주기 warning을 더하면 그 억제가 무의미해져
        # 하루 약 144건이 그대로 쌓입니다(PR #367 리뷰). 파서가 설명하지 않는 경우 — 형식
        # 위반뿐이거나 종목 줄이 아예 없는 경우 — 에만 warning을 냅니다.
        if _priceless_streak or _codeless_streak:
            logger.debug(
                "실현손익 응답에서 현재가를 한 건도 읽지 못했습니다(현재가 없음·코드 없음 경고 참고)."
            )
        else:
            logger.warning(
                "실현손익 응답에서 현재가를 한 건도 읽지 못했습니다. 기존 시세를 유지합니다."
            )
        return 0

    return _apply_quotes(quotes, session)


def _apply_quotes(quotes: dict[str, float], session: Session) -> int:
    """종목코드 → 현재가를 Portfolio에 쓰고 price_updated_at을 찍습니다 (#196).

    시세를 쓰는 곳은 이 함수 하나다. 실전(_sync_portfolio_prices_from_rlz_pl)과
    모의투자(_sync_portfolio_prices_from_paper_balance) 경로가 값 검사를 마친 뒤 여기로 모인다 —
    스탬프를 찍는 줄이 하나여야 "시세를 갱신한 시각"의 뜻이 갈리지 않는다.
    행을 만들지도 지우지도 않으며, 반환값은 갱신한 행 수다.
    """
    now = datetime.now(timezone.utc)
    updated = 0
    # pop이 아니라 get으로 읽고 매칭된 코드를 따로 모은다. Portfolio.stock_code에는
    # 아직 유니크 제약이 없어(models.Portfolio 주석) 같은 코드의 행이 둘 이상일 수
    # 있는데, pop으로 소비하면 두 번째 행부터는 시세도 스탬프도 낡은 채 남는다.
    # 잔고 동기화가 중복을 한 행으로 수렴시키지만 잘림·마커 부재·도구 실패 때는 돌지
    # 않고, 이 시세 경로는 의도적으로 balance_ok 밖에서 돈다 — 즉 수렴을 기다릴 수 없다.
    matched: set[str] = set()
    for row in session.exec(select(Portfolio)).all():
        price = quotes.get(row.stock_code)
        if price is None:
            # 이 종목은 이번 응답에 없습니다. 기존 값을 지우지 않습니다 — 지우면
            # 일시적인 누락이 곧바로 "시세 없음"이 됩니다. 대신 price_updated_at을
            # 갱신하지 않으므로 TTL이 지나면 is_price_fresh가 스스로 "모름"으로
            # 내립니다(#196).
            continue
        matched.add(row.stock_code)
        row.current_price = price
        # 이 스탬프를 찍는 곳은 이 줄 하나뿐입니다. 다른 경로가 찍으면 "시세를 갱신한
        # 시각"이라는 뜻이 무너지고 신선도 판정이 거짓말이 됩니다.
        #
        # as_utc_naive를 통과시켜 "UTC" 계약을 관례가 아니라 구조로 만듭니다.
        # SQLAlchemy의 SQLite DATETIME은 오프셋을 **변환하지 않고 버립니다** —
        # +09:00의 15:00을 넣으면 15:00이 그대로 저장돼 조용히 9시간이 틀어집니다.
        # 지금은 now가 항상 UTC라 도달하지 않지만, 이 한 줄이 그 사고를 구조적으로
        # 막습니다(is_price_fresh·format_price_updated_at도 같은 헬퍼를 씁니다).
        row.price_updated_at = as_utc_naive(now)
        updated += 1

    session.commit()
    leftover = sorted(set(quotes) - matched)
    if leftover:
        # 잔고 동기화가 아직 넣지 않은 코드입니다. 다음 주기에 행이 생기면 붙습니다.
        logger.info(
            "시세 응답의 %d개 종목이 Portfolio에 없어 건너뛰었습니다: %r",
            len(leftover), leftover[:5],
        )
    # 종목 수가 아니라 **행 수**다. stock_code에 유니크 제약이 없어 같은 코드의 행이
    # 둘 이상일 수 있고, 그때 이 값은 종목 수보다 크다(docstring의 반환값 설명과 일치).
    logger.info("Portfolio 시세 갱신 완료: %d개 행", updated)
    return updated


async def _refresh_portfolio_prices(balance_text: str | None = None) -> int | None:
    """get_balance_rlz_pl을 호출해 Portfolio 시세를 갱신합니다 (#196).

    도구 호출 실패는 "이번 주기에 시세를 못 얻었다"이지 "시세를 영영 모른다"가
    아닙니다. 예외를 삼키고 None을 반환해 잔고 동기화와 감시 루프가 그대로 돌게 합니다.
    낡은 값은 지우지 않습니다 — price_updated_at이 그대로 남아 TTL이 지나면
    is_price_fresh가 알아서 "모름"으로 내립니다.

    실패 로그를 억제하는 이유는 get_balance(_balance_failure_streak)와 같지만 사정이
    더 무겁습니다. 이 도구는 **실전 계좌 전용**(mcp/trading/index.js의
    get_balance_rlz_pl registerTool 설명)이라
    모의투자 배포에서는 매 주기 대체 응답이 오고, 권한이 없는 실전 계좌에서는 매 주기
    실패합니다. 그런 배포에서 10분마다 error를 쌓으면 로그가 신호를 잃습니다. 수준도
    error가 아니라 warning입니다 — 시세를 못 얻는 것은 감시도 주문도 막지 않습니다.

    DB 쪽 예외는 삼키지 않고 호출처로 올립니다. 그쪽이 이미 "동기화 실패는 감시에
    영향을 주지 않는다"는 격리를 갖고 있고, 여기서 또 삼키면 실패가 두 겹으로
    조용해집니다.

    모의투자 대체 응답을 받으면 이후 _PAPER_FALLBACK_SKIP_CYCLES 주기는 도구를 부르지
    않습니다. 로그 억제만으로는 모의투자 분기가 매 주기 다시 페이징하는 잔고 조회가
    줄지 않기 때문입니다(상수 주석 참고).

    연속조회 시간 예산은 도구 기본값(90초, NAT 호출자 기준)이 아니라
    _RLZ_PL_TIME_BUDGET_MS(18초)를 time_budget_ms 인자로 넘깁니다(#369). run_mcp_tool이
    30초에서 끊기 때문입니다(backend/ai/mcp_tools.py의 MCP_TOOL_TIMEOUT_SECONDS). 예산을 넘는
    계좌는 504 타임아웃이 아니라 도구의 잘림 안내를 받고, _sync_portfolio_prices_from_rlz_pl의
    잘림 경고가 사유를 싣습니다. 값의 근거, 18~28초 계좌가 치르는 대가, 페이지 간 지연이
    있을 때의 한도(#210)는 상수 주석에 있습니다.

    balance_text: 같은 주기에 monitor_market_task가 받은 get_balance 응답(실패했으면 None).
    모의투자 대체 응답 뒤 도구를 건너뛰는 주기에만 읽습니다 — 그 주기에도 잔고 평가금액으로
    시세를 채워야 price_updated_at이 TTL(30분) 안에 머뭅니다. 건너뛰기는 한 시간에 다섯 주기라
    이것 없이는 모의투자 시세가 대부분의 시간 "모름"이 됩니다. 추가 KIS 호출은 없습니다.
    실전 배포에서는 건너뛰는 주기가 없으므로 이 인자를 읽지 않습니다.
    """
    global _quote_failure_streak, _last_quote_error, _paper_fallback_skip_remaining

    if _paper_fallback_skip_remaining > 0:
        _paper_fallback_skip_remaining -= 1
        logger.debug(
            "모의투자 대체 응답을 받은 뒤라 이번 주기 시세 도구 호출을 건너뛰고 같은 주기 "
            "잔고 평가금액으로 시세를 채웁니다(남은 %d주기).",
            _paper_fallback_skip_remaining,
        )
        if balance_text is None:
            return None
        with Session(engine) as session:
            return _sync_portfolio_prices_from_paper_balance(balance_text, session)

    try:
        report_text = await deps.run_mcp_tool(
            TRADING_MCP_PARAMS,
            "get_balance_rlz_pl",
            {"time_budget_ms": _RLZ_PL_TIME_BUDGET_MS},
        )
    except Exception as e:
        signature = f"{type(e).__name__}:{e}"
        _quote_failure_streak += 1
        # 6회 = 약 1시간(10분 주기). 첫 실패는 즉시, 원인이 바뀌면 즉시, 이후는
        # 1시간에 한 번만 올립니다 — _balance_failure_streak와 같은 규칙입니다.
        if (
            _quote_failure_streak == 1
            or signature != _last_quote_error
            or _quote_failure_streak % 6 == 0
        ):
            logger.warning(
                "시세 조회(get_balance_rlz_pl)에 실패해 이번 주기 시세 갱신을 건너뜁니다 "
                "(기존 시세는 유지되며 TTL이 지나면 '모름'으로 내려갑니다, %d회 연속): %s",
                _quote_failure_streak,
                e,
            )
        else:
            logger.debug(
                "시세 조회 실패가 %d회 연속됩니다: %s", _quote_failure_streak, e
            )
        _last_quote_error = signature
        return None

    if _quote_failure_streak:
        logger.info(
            "시세 조회가 복구되었습니다. 직전까지 %d회 연속 실패해 시세 갱신을 건너뛰었습니다.",
            _quote_failure_streak,
        )
        _quote_failure_streak = 0
        _last_quote_error = None

    if _RLZ_PL_PAPER_FALLBACK_MARKER in report_text:
        _paper_fallback_skip_remaining = _PAPER_FALLBACK_SKIP_CYCLES

    with Session(engine) as session:
        return _sync_portfolio_prices_from_rlz_pl(report_text, session)


# ---------------------------------------------------------------------------
# 시세 신선도 게이트 (#196)
# ---------------------------------------------------------------------------

# Portfolio.current_price를 "안다"고 말할 수 있는 최대 나이.
#
# 값의 근거는 갱신 주기와의 배수 관계다. 시세 갱신은 감시 잡(market_monitoring)에
# 얹혀 돌므로 주기가 10분이고(start_scheduler의 interval, minutes=10), 이 상수는 그
# 3배다. 배수마다 뜻이 있다:
#   1배(10분): 주기와 같아 지터 몇 초만으로 신선/비신선이 뒤집힌다. 정상 갱신 중에도
#              화면이 "앎"과 "모름" 사이를 깜빡인다.
#   2배(20분): 갱신을 한 번 놓치면 곧바로 경계에 닿아 여유가 없다. KIS 일시 장애는
#              드문 일이 아니다 — _balance_failure_streak가 그 전제 위에 있다.
#   3배(30분): 연속 두 번의 갱신 실패까지 견딘다. 세 번 연속 실패했다면 그때는 실제로
#              "모른다"고 말하는 편이 맞다.
# 주기를 바꾸면 이 상수도 함께 봐야 한다 — 숫자 30이 아니라 이 배수 관계가 근거다.
#
# env로 빼지 않는다(리뷰어 요청). 배포마다 다르게 둘 이유가 없고, 운영 중 넓히면
# 이 게이트가 없애려는 "모르는데 안다"가 설정 한 줄로 조용히 되살아난다.
PRICE_FRESHNESS_TTL = timedelta(minutes=30)


def is_price_fresh(price_updated_at: datetime | None, *, now: datetime) -> bool:
    """Portfolio.current_price를 지금 "안다"고 말해도 되는지 판정한다 (#196).

    NULL 분기와 TTL 분기를 한 함수에 묶는 이유: 두 판정이 각자의 호출처에 흩어지면
    한쪽만 고쳐져 "TTL은 지켰는데 NULL은 신선으로 통과"하는 상태가 생긴다. 그 상태가
    바로 이 이슈가 없애려는 결함이다.

    price_updated_at이 NULL이면 **무조건 False**다. 이 컬럼이 없던 시절의 행은
    current_price가 채워져 있어도 그것이 언제 채워진 값인지 알 수 없다 — 나이를 모르는
    값을 신선하다고 단언하면 낡은 시세가 현재가로 나간다(#122·#162의 "0과 모름 구분"과
    같은 기준). 신선하다고 말할 수 없는 것이지 값이 없다는 뜻은 아니며, 다음 시세 갱신
    주기가 진짜 값을 채우면 그 행은 즉시 신선해진다.

    미래 시각(now보다 뒤)은 **TTL만큼만** 신선으로 판정된다. 시계 역행이나 KIS/로컬
    시각 오차로 조금 앞선 값은 "방금 갱신됨"으로 읽는 편이 안전하다 — 반대로 막으면
    시계가 조금 어긋난 배포에서 모든 시세가 영구히 "모름"이 된다. 그러나 하한이 없으면
    한참 미래의 스탬프는 **영원히** 신선해진다. 시계가 크게 틀어졌거나 잘못된 값이
    한 번 쓰이면 그 행의 시세는 다시는 "모름"으로 내려가지 않는다 — 이 게이트가
    없애려는 상태 그 자체다. 그래서 위아래 대칭으로 TTL을 건다.
    """
    if price_updated_at is None:
        return False
    # SQLite의 DATETIME은 오프셋을 저장하지 않아 읽어 온 값은 naive UTC다. now는
    # datetime.now(timezone.utc)라 aware이므로, 맞추지 않고 빼면
    # "can't compare offset-naive and offset-aware datetimes"로 죽는다.
    age = as_utc_naive(now) - as_utc_naive(price_updated_at)
    return -PRICE_FRESHNESS_TTL <= age <= PRICE_FRESHNESS_TTL


def format_price_updated_at(price_updated_at: datetime | None) -> str:
    """API 응답에 실을 시세 갱신 시각 문자열. NULL이면 "" (#196).

    is_price_fresh와 같은 자리에 두는 이유: 응답은 판정(bool)과 근거(시각)를 함께
    내리는데, 둘이 다른 모듈에 있으면 한쪽만 축(naive/aware)이 바뀌어도 눈에 띄지 않는다.

    저장된 값은 naive UTC이므로 그대로 isoformat()하면 오프셋 없는 문자열이 나가고,
    소비자는 그것을 자기 로컬 시각으로 읽는다. tzinfo를 붙여 "+00:00"이 실리게 한다.
    """
    if price_updated_at is None:
        return ""
    return as_utc_naive(price_updated_at).replace(tzinfo=timezone.utc).isoformat()
