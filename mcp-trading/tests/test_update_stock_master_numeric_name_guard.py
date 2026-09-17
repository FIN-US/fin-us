"""종목 마스터 갱신 스크립트의 "코드 형태 토큰 + 숫자" 이름 검사 (PR #392 리뷰, #393에서 이동).

backend의 /buy·/sell 파서는 이름 자리가 종목코드 하나면 시장가 해석을 만들지 않는다
(telegram_commands._order_argument_readings). 그래서 마스터에 "ABC123 200" 같은 이름·별칭이
생기면 그 종목은 이름으로 시장가 주문할 수 없고 모호한 입력을 되묻지도 못한다. 커밋된
stocks.json은 backend test_telegram_commands의 전수 테스트가 보지만, 운영자가
``mcp-trading/scripts/update_stock_master.py``로 직접 갱신하면 CI 없이 파일이 바뀐다. 그래서
스크립트가 쓰기 전에 같은 검사를 한다.

두 구현은 공유 판정표 ``fixtures/unreadable_numeric_name_policy.json``으로 묶인다. 이 파일은
스크립트 쪽 판정을 표와 대조하고, backend 파서 쪽 대조(와 표 자체의 양쪽 판정 포함 검사)는 backend를
import해야 하므로 ``backend/tests/test_stock_master_numeric_name_guard.py``에 남아 있다. 두 쪽이 같은
표를 읽으므로 어느 한 구현만 바뀌어도 그쪽 스위트가 red가 된다.

이 파일은 스크립트처럼 표준 라이브러리와 pytest만 쓴다 — CI의 mcp-trading 스크립트 pytest 잡이 backend
의존성 없이 돌린다(#393).
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

_MCP_TRADING_DIR = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _MCP_TRADING_DIR / "scripts" / "update_stock_master.py"
_POLICY_PATH = _MCP_TRADING_DIR / "tests" / "fixtures" / "unreadable_numeric_name_policy.json"
_STOCKS_PATH = _MCP_TRADING_DIR / "data" / "stocks.json"
_CASES = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["cases"]
_CASE_IDS = [case["label"] for case in _CASES]


def _load_script():
    spec = importlib.util.spec_from_file_location("update_stock_master", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load_script()


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_update_script_agrees_with_the_policy_table(script, case):
    """스크립트의 판정이 공유 판정표와 일치한다 — backend와 따로 구현한 판정이 어긋나지 않는다.

    이 테스트가 잡는 mutation: 스크립트 코드 형태 판정에서 숫자 요구 제거(KIWOOM 200이 걸린다),
    대문자화 제거(abc123 200을 놓친다), 수량 판정의 쉼표 처리 제거(005930 10,000을 놓친다).
    """
    assert script.is_unreadable_numeric_name(case["label"]) is case["unreadable"]


def test_committed_stock_master_has_no_unreadable_numeric_name(script):
    """커밋된 마스터는 스크립트 검사도 통과한다(현재 0건)."""
    stocks = json.loads(_STOCKS_PATH.read_text(encoding="utf-8"))
    assert script.find_unreadable_numeric_names(stocks) == []


def test_find_checks_names_and_aliases(script):
    stocks = [
        {"code": "005930", "name": "삼성전자", "market": "KOSPI", "aliases": ["SEC123 1"]},
        {"code": "0099A0", "name": "ABC123 200", "market": "KOSDAQ", "aliases": []},
        {"code": "069500", "name": "KODEX 200", "market": "KOSPI"},
    ]
    assert script.find_unreadable_numeric_names(stocks) == [
        ("005930", "SEC123 1"),
        ("0099A0", "ABC123 200"),
    ]


_OLD_MASTER = [{"code": "005930", "name": "삼성전자", "market": "KOSPI", "aliases": []}]
_CLEAN_ROWS = [{"code": "069500", "name": "KODEX 200", "market": "KOSPI", "aliases": []}]
_VIOLATING_ROWS = [
    {"code": "069500", "name": "KODEX 200", "market": "KOSPI", "aliases": []},
    {"code": "0099A0", "name": "ABC123 200", "market": "KOSDAQ", "aliases": []},
]


def _run_main(script, monkeypatch, tmp_path, rows, argv, capsys):
    stocks_path = tmp_path / "stocks.json"
    stocks_path.write_text(json.dumps(_OLD_MASTER, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(script, "STOCKS_PATH", stocks_path)
    monkeypatch.setattr(script, "SOURCES", [{"market": "KOSPI", "tail_width": 0}])
    monkeypatch.setattr(script, "download_source", lambda source, target_dir: target_dir / "x.mst")
    monkeypatch.setattr(script, "parse_master_rows", lambda file_path, **kwargs: list(rows))
    code = script.main(argv)
    return code, json.loads(stocks_path.read_text(encoding="utf-8")), capsys.readouterr()


def test_update_script_refuses_to_write_a_master_with_an_unreadable_numeric_name(
    script, monkeypatch, tmp_path, capsys
):
    """위반이 있으면 기본은 파일을 쓰지 않고 0이 아닌 코드로 끝내며 종목을 나열한다.

    이 테스트가 잡는 mutation: main에서 검사 호출 제거, 검사 결과를 무시하고 씀.
    """
    code, written, out = _run_main(script, monkeypatch, tmp_path, _VIOLATING_ROWS, [], capsys)

    assert code != 0
    assert written == _OLD_MASTER
    assert "0099A0\tABC123 200" in out.err
    assert "--allow-unreadable-numeric-names" in out.err


def test_update_script_writes_with_the_explicit_allow_flag_and_warns(script, monkeypatch, tmp_path, capsys):
    """플래그를 주면 경고를 남기고 쓴다 — KRX가 그런 이름을 상장해도 마스터 갱신이 막히지 않는다.

    이 테스트가 잡는 mutation: 플래그를 무시하고 항상 거절.
    """
    code, written, out = _run_main(
        script, monkeypatch, tmp_path, _VIOLATING_ROWS, ["--allow-unreadable-numeric-names"], capsys
    )

    assert code == 0
    assert [stock["code"] for stock in written] == ["0099A0", "069500"]
    assert "0099A0\tABC123 200" in out.err
    assert "경고" in out.err


def test_update_script_writes_a_clean_master(script, monkeypatch, tmp_path, capsys):
    code, written, out = _run_main(script, monkeypatch, tmp_path, _CLEAN_ROWS, [], capsys)

    assert code == 0
    assert written == _CLEAN_ROWS
    assert out.err == ""


def test_check_reports_to_the_given_stream(script):
    stream = io.StringIO()
    assert script.check_unreadable_numeric_names(_VIOLATING_ROWS, allow=False, stream=stream) is False
    assert "ABC123 200" in stream.getvalue()
