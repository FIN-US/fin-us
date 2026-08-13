"""backend/stock_code.py 공용 모듈 테스트 (#140).

_STOCK_CODE_EXTRACT_RE, _STOCK_CODE_RE / _looks_like_stock_code, _ORDERABLE_STOCK_CODE_RE에 대한
순수 단위 테스트. 각 호출부 고유 동작(services.py의 빈 문자열 폴백,
telegram_commands.py의 조기 거절)은 각자의 테스트 파일에 남아 있다.
"""
import json
import logging
from pathlib import Path

from backend import stock_code
from backend.stock_code import (
    _ORDERABLE_STOCK_CODE_RE,
    _STOCK_CODE_EXTRACT_RE,
    _has_code_digit,
    _is_known_master_code,
    _is_unresolved_echo,
    _looks_like_stock_code,
    _master_stocks_path,
)

# 종목마스터 실제 데이터(4,353종 전수) — 판정 함수가 이름·별칭을 코드로 오인하지
# 않는지 검증하는 데 쓴다. 이 파일은 레포에 커밋돼 있어야 한다.
_STOCKS_JSON_PATH = Path(__file__).resolve().parents[2] / "mcp-trading" / "data" / "stocks.json"


# ──────────────────────────────────────────────────────────────────────────
# _STOCK_CODE_EXTRACT_RE — MCP 응답에서 코드 추출
# ──────────────────────────────────────────────────────────────────────────

class TestStockCodeExtractRe:
    def test_numeric_six_char(self):
        """6자리 숫자 코드(KIS 표준)를 추출한다."""
        m = _STOCK_CODE_EXTRACT_RE.search("삼성전자 (005930, KOSPI)")
        assert m is not None
        assert m.group(1) == "005930"

    def test_alphanumeric_six_char(self):
        """영숫자 6자 ETN 코드를 추출한다."""
        m = _STOCK_CODE_EXTRACT_RE.search("덕양에너젠 (0001A0, KOSDAQ)")
        assert m is not None
        assert m.group(1) == "0001A0"

    def test_seven_char_etn(self):
        """7자 ETN 코드를 추출한다(389종목)."""
        m = _STOCK_CODE_EXTRACT_RE.search(
            "신한 레버리지 다우존스지수 선물 ETN(H) (Q500020, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "Q500020"

    def test_nine_char_fund_code(self):
        """9자 펀드 코드(75종목)를 추출한다 — {6,7}로 좁히면 조용히 실패하는 경로."""
        m = _STOCK_CODE_EXTRACT_RE.search(
            "한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "F70100026"

    def test_parentheses_in_stock_name_anchors_on_code_comma(self):
        """종목명 안에 닫히지 않는 괄호가 있어도 코드+쉼표 앵커가 올바른 코드를 뽑는다.

        종목마스터 실제 항목: "KIWOOM 엔비디아미국30년국채혼합액티브(H (0015E0, KOSPI)"
        """
        m = _STOCK_CODE_EXTRACT_RE.search(
            "KIWOOM 엔비디아미국30년국채혼합액티브(H (0015E0, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "0015E0"

    def test_no_match_returns_none(self):
        """코드+쉼표 패턴이 없으면 None을 반환한다."""
        assert _STOCK_CODE_EXTRACT_RE.search("종목을 찾지 못했습니다") is None


# ──────────────────────────────────────────────────────────────────────────
# _has_code_digit
# ──────────────────────────────────────────────────────────────────────────

class TestHasCodeDigit:
    def test_all_digits(self):
        assert _has_code_digit("005930")

    def test_mixed_alphanumeric(self):
        assert _has_code_digit("0001A0")

    def test_no_digits_returns_false(self):
        assert not _has_code_digit("SAMSUNG")
        assert not _has_code_digit("SIMPAC")
        assert not _has_code_digit("")


# ──────────────────────────────────────────────────────────────────────────
# _looks_like_stock_code — 입력 판정 (MCP 조회 생략 여부)
# ──────────────────────────────────────────────────────────────────────────

class TestLooksLikeStockCode:
    # 수용 케이스
    def test_six_char_numeric(self):
        assert _looks_like_stock_code("005930")

    def test_six_char_alphanumeric(self):
        assert _looks_like_stock_code("0001A0")

    def test_seven_char_etn(self):
        assert _looks_like_stock_code("Q500020")

    def test_nine_char_fund_code(self):
        """{6,7} → {6,9} 확대의 핵심 회귀 가드.

        뮤테이션 ①: _STOCK_CODE_RE를 {6,7}로 되돌리면 이 케이스가 red가 된다.
        F70100026은 종목마스터 실제 항목(코드 길이 9, 펀드 75종 중 하나).
        #140 이전에는 False → MCP 서브프로세스를 불필요하게 호출했다.
        """
        assert _looks_like_stock_code("F70100026")

    def test_lowercase_input_normalised(self):
        """소문자 입력도 upper() 정규화 후 판정한다."""
        assert _looks_like_stock_code("0001a0")  # 6자 영숫자, 소문자
        assert _looks_like_stock_code("f70100026")  # 9자 펀드, 소문자

    def test_leading_trailing_whitespace_stripped(self):
        assert _looks_like_stock_code("  005930  ")

    # 거절 케이스
    def test_rejects_all_alpha_no_digit(self):
        """숫자가 없는 6~7자 영문은 종목명으로 보고 MCP로 넘긴다."""
        assert not _looks_like_stock_code("SAMSUNG")
        assert not _looks_like_stock_code("SIMPAC")  # 코드 형태 이름, 숫자 없음

    def test_rejects_korean_name(self):
        assert not _looks_like_stock_code("삼성전자")

    def test_rejects_empty_string(self):
        assert not _looks_like_stock_code("")

    def test_rejects_too_short(self):
        assert not _looks_like_stock_code("00593")  # 5자

    def test_rejects_too_long(self):
        """10자 이상은 종목코드가 아니다(현재 마스터 최장 9자)."""
        assert not _looks_like_stock_code("F701000260")  # 10자

    def test_rejects_eight_char(self):
        """8자는 종목마스터 4,353종 전수(mcp-trading/data/stocks.json)에 0건이라 거절한다.

        뮤테이션 ①: _STOCK_CODE_RE를 {6,9} 단순 상한 확대로 되돌리면 이 케이스가
        red가 된다. 실재하지 않는 8자 입력이 코드 형태로 통과하면 MCP 존재 검증을
        생략하고 그대로 코드로 확정돼 버리므로(#151), 8자는 명시적으로 막아야 한다.
        """
        assert not _looks_like_stock_code("12345678")

    def test_rejects_string_with_space_inside(self):
        """내부 공백이 있으면 strip() 후에도 코드 형태가 아니다."""
        assert not _looks_like_stock_code("005 930")


# ──────────────────────────────────────────────────────────────────────────
# _ORDERABLE_STOCK_CODE_RE — 주문 가능 코드 판정
# mcp-trading/order.js:77-84 정책의 백엔드 복제본
# ──────────────────────────────────────────────────────────────────────────

class TestOrderableStockCodeRe:
    """뮤테이션 ②: _ORDERABLE_STOCK_CODE_RE 판정을 무력화(항상 True 반환 등)하면
    test_rejects_alphanumeric / test_rejects_nine_char_fund 가 red가 된다.
    """

    def test_accepts_six_char_numeric(self):
        assert _ORDERABLE_STOCK_CODE_RE.fullmatch("005930")

    def test_accepts_seven_char_numeric(self):
        assert _ORDERABLE_STOCK_CODE_RE.fullmatch("1234567")

    def test_rejects_alphanumeric(self):
        """영숫자 코드는 order.js가 전용 메시지로 거절한다 — 백엔드도 동일하게 거절."""
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("0001A0")

    def test_rejects_nine_char_fund(self):
        r"""9자 펀드 코드는 order.js 두 번째 가드(/^\d{6,7}$/)에서 거절된다.

        _looks_like_stock_code가 True를 반환하도록 확대됐어도(_STOCK_CODE_RE {6,9}),
        주문 가능 여부는 여전히 숫자 6~7자로 제한된다 — 정책 불변.
        """
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("F70100026")

    def test_rejects_five_char(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("00593")

    def test_rejects_eight_char(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("12345678")

    def test_rejects_empty(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("")

    def test_rejects_fullwidth_digits(self):
        r"""전각 숫자(０ 등)는 [0-9] 패턴에 매치되지 않아야 한다.

        Python의 `\d`는 전각 숫자를 포함하므로 `[0-9]`로 명시한 이유를 고정한다.
        """
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("００５９３０")


# ──────────────────────────────────────────────────────────────────────────
# _is_known_master_code — 지름길 코드가 종목마스터에 실재하는지 대조 (#151)
# ──────────────────────────────────────────────────────────────────────────

class TestIsKnownMasterCode:
    """_looks_like_stock_code 지름길이 MCP 존재 확인 없이 코드를 확정해 버리던
    문제(#151)를 로컬 종목마스터 코드 집합과 대조해 막는다.
    """

    def test_known_numeric_code_returns_true(self):
        """실측: 005930(삼성전자)은 종목마스터에 실재한다."""
        assert _is_known_master_code("005930") is True

    def test_known_nine_char_fund_code_returns_true(self):
        """#140 회귀 가드: 마스터에 실재하는 9자 펀드 코드는 계속 지름길을 통과해야 한다."""
        assert _is_known_master_code("F70100026") is True

    def test_unknown_numeric_code_returns_false(self):
        """#151 재현 케이스: 마스터에 없는 숫자 6자는 존재하지 않는다고 판정해야 한다."""
        assert _is_known_master_code("999999") is False

    def test_unknown_alphanumeric_code_returns_false(self):
        assert _is_known_master_code("ZZZZ99") is False

    def test_fail_open_when_master_file_missing(self, tmp_path, monkeypatch, caplog):
        """마스터 파일을 읽을 수 없으면 예외를 던지지 않고 검증을 생략(True)한다.

        이 경로는 리포트 저장 판정이지 주문이 아니므로, 마스터를 못 읽는다고
        분석 기능 전체가 죽으면 안 된다(fail-open).
        뮤테이션 ③: fail-open을 예외 전파로 바꾸면 이 테스트가 예외로 red가 되어야 한다.
        """
        # 경로는 이제 _TRADING_MCP_DIR에서 매 호출 파생되므로(#151 리뷰 후속), 없는
        # 파일을 가리키려면 마스터가 없는 빈 디렉터리로 _TRADING_MCP_DIR을 옮긴다.
        monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", tmp_path)
        with caplog.at_level(logging.WARNING, logger=stock_code.logger.name):
            assert _is_known_master_code("999999") is True
        assert "종목마스터 로드 실패" in caplog.text

    def test_fail_open_when_master_file_malformed(self, tmp_path, monkeypatch):
        """마스터 파일이 있어도 JSON 파싱에 실패하면 마찬가지로 fail-open한다."""
        broken_path = tmp_path / "data" / "stocks.json"
        broken_path.parent.mkdir(parents=True)
        broken_path.write_text("이것은 JSON이 아닙니다", encoding="utf-8")
        monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", tmp_path)
        assert _is_known_master_code("999999") is True

    def test_entry_without_code_does_not_disable_validation(
        self, tmp_path, monkeypatch, caplog
    ):
        """code 키가 빠진 항목 하나가 마스터 전체 검증을 꺼뜨리면 안 된다.

        마스터는 외부 KIS 파일에서 생성되므로 형태가 변할 수 있다. 항목 전체를 한
        try로 묶으면 결손 1건의 KeyError가 4,353건 전부를 fail-open으로 만들고,
        실패는 캐시되므로 경고조차 프로세스 수명 동안 한 번만 찍힌다.
        """
        master = tmp_path / "data" / "stocks.json"
        master.parent.mkdir(parents=True)
        master.write_text(
            json.dumps(
                [
                    {"code": "005930", "name": "삼성전자", "market": "KOSPI"},
                    {"name": "코드 없는 항목", "market": "KOSPI"},
                    {"code": "", "name": "빈 코드", "market": "KOSPI"},
                    "리스트가 아닌 항목",
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", tmp_path)

        with caplog.at_level(logging.WARNING, logger=stock_code.logger.name):
            # 읽어낸 항목은 정상 대조되고, 못 읽은 항목 때문에 fail-open되지 않는다.
            assert _is_known_master_code("005930") is True
            assert _is_known_master_code("999999") is False

        assert "종목마스터 로드 실패" not in caplog.text
        assert "일부 항목에서 코드를 읽지 못했습니다" in caplog.text
        assert "1/4건" in caplog.text

    def test_fail_open_when_no_code_could_be_read(self, tmp_path, monkeypatch, caplog):
        """코드를 하나도 못 읽으면(마스터 형태가 통째로 바뀜) 로드 실패로 취급한다.

        빈 집합을 성공으로 캐시하면 _is_known_master_code가 *모든* 코드를 조용히
        거부한다 — 실재하는 종목까지 저장이 막히므로 fail-open보다 나쁘다.
        뮤테이션: `if not codes: raise`를 지우면 아래 True 단정이 red가 된다.
        """
        master = tmp_path / "data" / "stocks.json"
        master.parent.mkdir(parents=True)
        master.write_text(
            json.dumps({"stocks": [{"code": "005930"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", tmp_path)

        with caplog.at_level(logging.WARNING, logger=stock_code.logger.name):
            assert _is_known_master_code("005930") is True

        assert "종목마스터 로드 실패" in caplog.text

    def test_master_codes_loaded_from_disk_at_most_once(self, monkeypatch):
        """지름길 경로가 요청마다 stocks.json(489K)을 다시 파싱하면 지름길을 둔
        이유(MCP 왕복 생략)가 무색해진다 — 첫 호출 이후로는 캐시만 써야 한다.
        """
        read_calls = []
        original_read_text = Path.read_text
        expected_path = stock_code._master_stocks_path(stock_code._TRADING_MCP_DIR)

        def counting_read_text(self, *args, **kwargs):
            if self == expected_path:
                read_calls.append(1)
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        assert _is_known_master_code("005930") is True
        assert _is_known_master_code("999999") is False
        assert _is_known_master_code("F70100026") is True

        assert len(read_calls) == 1


# ──────────────────────────────────────────────────────────────────────────
# _MASTER_STOCKS_PATH — 백엔드가 읽는 마스터가 MCP가 보는 마스터와 같은가
# ──────────────────────────────────────────────────────────────────────────

def test_master_stocks_path_follows_trading_mcp_dir(tmp_path):
    """마스터 경로는 백엔드가 MCP를 띄우는 디렉터리(TRADING_MCP_DIR)에서 파생돼야 한다.

    stock_code.py 위치에 고정하면 개발 환경에서는 두 경로가 우연히 같아 문제가
    드러나지 않지만, backend/Dockerfile은 백엔드 소스(/app bind mount)와 MCP
    디렉터리(/opt/mcp-trading 이미지 스냅샷)를 실제로 갈라놓는다. TRADING_MCP_DIR이
    레포 바깥을 가리키면 backend 쪽 경로에 파일이 없어 fail-open으로 #151 검증이
    조용히 꺼진다.

    파생 규칙은 _master_stocks_path 순수 함수로 뽑혀 있어(#151 리뷰), 모듈을
    importlib.reload하지 않고도 서로 다른 mcp_dir 입력에 대해 같은 계약을 고정할 수
    있다 — reload는 다른 테스트가 붙잡은 config/stock_code 바인딩까지 영향권이라
    다음 사람이 테스트를 추가할 때 밟기 쉬운 지뢰였다.

    뮤테이션: _master_stocks_path가 mcp_dir 인자를 무시하고 하드코딩 경로를
    반환하면 이 테스트가 red가 된다.
    """
    mcp_dir = tmp_path / "opt" / "mcp-trading"
    assert _master_stocks_path(mcp_dir) == mcp_dir / "data" / "stocks.json"

    other_dir = tmp_path / "another" / "mcp-trading"
    assert _master_stocks_path(other_dir) == other_dir / "data" / "stocks.json"


def test_master_path_wiring_follows_trading_mcp_dir_without_reload(tmp_path, monkeypatch):
    """_master_stocks_path 자체가 아니라, 그 함수를 *실제로 소비하는* _load_master_codes가
    하드코딩 경로 대신 _TRADING_MCP_DIR에서 파생한 경로를 읽는지를 고정한다.

    위 test_master_stocks_path_follows_trading_mcp_dir는 순수 함수의 계약만 고정할 뿐,
    소비자(_load_master_codes)가 실제로 그 함수를 _TRADING_MCP_DIR로 호출하는지는
    별도로 확인하지 않는다 — 소비자 쪽 배선이 `Path(__file__).resolve().parents[1] /
    "mcp-trading"`처럼 하드코딩으로 되돌아가도 순수 함수 테스트는 계속 통과한다.
    개발 환경에서는 두 경로가 우연히 같은 값으로 계산되므로 단순 경로 비교
    (`_MASTER_STOCKS_PATH == _master_stocks_path(_TRADING_MCP_DIR)`)로도 이 배선
    풀림을 못 잡는다 — 실제로 마스터를 갈아끼워 다른 내용을 읽는지까지 확인해야 한다.

    reload 없이 stock_code._TRADING_MCP_DIR을 직접 몽키패치하는 것만으로 검증할 수
    있다 — _load_master_codes가 매 호출 _master_stocks_path(_TRADING_MCP_DIR)를 그
    자리에서 계산하도록 바뀌었기 때문이다(#151 리뷰 후속). _TRADING_MCP_DIR은 평범한
    모듈 전역이라 몽키패치가 다음 호출에 곧바로 반영된다.

    뮤테이션: _load_master_codes 안의 `_master_stocks_path(_TRADING_MCP_DIR)`를
    `_master_stocks_path(Path(__file__).resolve().parents[1] / "mcp-trading")`처럼
    하드코딩으로 되돌리면, 아래 두 단정 모두 실제 마스터(4,353종)를 읽게 되어 red가
    된다 — "111111"은 실제 마스터에 없고 "005930"은 실제 마스터에 있으므로.
    """
    mcp_dir = tmp_path / "custom-mcp"
    (mcp_dir / "data").mkdir(parents=True)
    (mcp_dir / "data" / "stocks.json").write_text(
        json.dumps(
            [{"code": "111111", "name": "가짜종목", "market": "KOSPI"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", mcp_dir)

    # 커스텀 경로의 마스터에만 있는 코드는 알려진 코드로 판정된다.
    assert _is_known_master_code("111111") is True
    # 실제 마스터(mcp-trading/data/stocks.json)에만 있는 코드는 이 커스텀 경로에
    # 없으므로 알려지지 않은 코드로 판정된다 — 하드코딩 경로로 되돌아가면 이 코드가
    # 실제 마스터에서 발견돼 True가 되어 버린다.
    assert _is_known_master_code("005930") is False


def test_master_code_cache_invalidates_when_path_changes(tmp_path, monkeypatch):
    """캐시가 경로별로 무효화되는지 확인한다 — _load_master_codes의
    `if _master_codes_cache is not None and _master_codes_cache_path == path:`에서
    `and _master_codes_cache_path == path` 절이 실제로 지키는 계약이다.

    conftest.py의 autouse 픽스처(_clear_master_code_cache)가 매 테스트 시작·종료마다
    캐시를 비우므로, 테스트 사이에는 이 절이 있으나 없으나 차이가 드러나지 않는다 —
    이 절이 지키는 건 "한 테스트 안에서 _TRADING_MCP_DIR이 바뀌는" 경우다. 그러지
    않으면 경로가 바뀐 뒤에도 이전 경로에서 읽은 캐시를 그대로 돌려준다.

    양방향을 모두 단언한다 — 새 경로의 코드가 보이는지(A의 캐시를 그대로 썼다면
    안 보임)와 이전 경로의 코드가 더 이상 보이지 않는지(A의 캐시를 그대로 썼다면
    계속 보임) 중 한쪽만 확인하면 반쪽짜리 계약이 된다.

    뮤테이션: `and _master_codes_cache_path == path`를 지우면(캐시 유효성 검사를
    `_master_codes_cache is not None`만으로 완화하면) 아래 두 단정 모두 red가 된다 —
    경로 B로 바꾼 뒤에도 경로 A에서 읽은 캐시를 그대로 반환하기 때문이다.
    """
    dir_a = tmp_path / "mcp-a"
    (dir_a / "data").mkdir(parents=True)
    (dir_a / "data" / "stocks.json").write_text(
        json.dumps([{"code": "111111", "name": "A마스터종목", "market": "KOSPI"}], ensure_ascii=False),
        encoding="utf-8",
    )
    dir_b = tmp_path / "mcp-b"
    (dir_b / "data").mkdir(parents=True)
    (dir_b / "data" / "stocks.json").write_text(
        json.dumps([{"code": "222222", "name": "B마스터종목", "market": "KOSPI"}], ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", dir_a)
    assert _is_known_master_code("111111") is True

    monkeypatch.setattr(stock_code, "_TRADING_MCP_DIR", dir_b)
    # 캐시가 무효화되지 않으면 A의 캐시(111111만 포함)를 그대로 돌려줘 False가 된다.
    assert _is_known_master_code("222222") is True
    # 캐시가 무효화되지 않으면 A의 캐시를 그대로 돌려줘 True가 된다.
    assert _is_known_master_code("111111") is False


# ──────────────────────────────────────────────────────────────────────────
# _is_unresolved_echo — stock-master.js Step 3 미해석 에코 판정 (#151)
# ──────────────────────────────────────────────────────────────────────────

class TestIsUnresolvedEcho:
    """리포트 저장(services)과 주문 준비(telegram_commands)가 공유하는 판정."""

    def test_detects_numeric_echo(self):
        assert _is_unresolved_echo("999999 (999999, UNKNOWN)") is True

    def test_detects_alphanumeric_echo(self):
        assert _is_unresolved_echo("ZZZZ99 (ZZZZ99, UNKNOWN)") is True
        assert _is_unresolved_echo("Q999999 (Q999999, UNKNOWN)") is True

    def test_rejects_resolved_master_entry(self):
        assert _is_unresolved_echo("삼성전자 (005930, KOSPI)") is False
        assert _is_unresolved_echo("한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)") is False

    def test_detects_echo_with_trailing_text(self):
        """문자열 끝이 아니라 _STOCK_CODE_EXTRACT_RE와 같은 지점에 앵커한다.

        endswith(", UNKNOWN)")로 앵커하면 index.js:559가 응답 뒤에 무언가를 덧붙이는
        순간 코드 추출은 계속 성공하는데 에코 감지만 조용히 멈춰 #151이 되돌아온다.
        뮤테이션: 판정을 endswith로 되돌리면 이 케이스가 red가 된다.
        """
        assert _is_unresolved_echo("999999 (999999, UNKNOWN)\n(조회 시각: 10:00)") is True

    def test_rejects_unknown_without_code_shape(self):
        """"UNKNOWN"이 코드 자리 없이 등장하는 문장은 에코가 아니다."""
        assert _is_unresolved_echo("시장 정보를 알 수 없습니다 (UNKNOWN)") is False


# ──────────────────────────────────────────────────────────────────────────
# 종목마스터 전수 회귀 가드 — 이름·별칭이 _looks_like_stock_code에 오인되지 않는가
# ──────────────────────────────────────────────────────────────────────────

def test_no_master_name_is_shadowed_by_looks_like_stock_code():
    """마스터의 어떤 이름·별칭도 _looks_like_stock_code가 코드로 오인하지 않아야 한다.

    오인하면 MCP 조회를 건너뛰어 마스터의 *다른* 종목을 가린다(#151).
    #140이 판정 범위를 9자까지 넓혔으므로 이 불변식도 9자를 포함해야 한다 —
    mcp-trading의 CODE_SHAPE_PATTERN은 `/^[A-Z0-9]{6,7}$/i`라 9자를 검사하지 못하므로
    (mcp-trading/tests/stock-master.test.js의 "exactly 3 master stock names..." 테스트는
    부분적인 신호일 뿐이다), 6·7·9자 전 범위를 덮는 신호는 이 테스트다.

    뮤테이션 ③: _looks_like_stock_code가 항상 True를 반환하도록 무력화하면 이
    테스트가 red가 돼야 한다. red가 되지 않으면 이 테스트는 공허하다.
    """
    assert _STOCKS_JSON_PATH.exists(), f"stocks.json이 없습니다: {_STOCKS_JSON_PATH}"
    stocks = json.loads(_STOCKS_JSON_PATH.read_text(encoding="utf-8"))

    shadowed = []
    for stock in stocks:
        code = stock["code"]
        candidates = [stock["name"], *stock.get("aliases", [])]
        for name in candidates:
            if _looks_like_stock_code(name):
                shadowed.append((name, code))

    assert shadowed == [], (
        "다음 마스터 이름·별칭이 _looks_like_stock_code에 종목코드로 오인됩니다 "
        f"(이름, 오인된 이름이 속한 실제 코드): {shadowed}"
    )
