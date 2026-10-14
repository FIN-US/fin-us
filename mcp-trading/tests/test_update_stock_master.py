import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_stock_master.py"


def load_update_stock_master():
    spec = importlib.util.spec_from_file_location("update_stock_master", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# KIS 공식 파서(kis_kospi_code_mst.py·kis_kosdaq_code_mst.py) field_specs의 합.
# **일부러 리터럴로 적는다** — 픽스처 행을 SOURCES의 data_width로 만들면 그 값이 틀려도
# 행이 같이 늘어나 테스트가 통과한다. 꼬리 폭은 KIS가 정하는 값이지 우리가 고르는 값이 아니다.
OFFICIAL_TAIL_WIDTH = {"KOSPI": 227, "KOSDAQ": 221}


def source_for(module, market):
    return next(source for source in module.SOURCES if source["market"] == market)


def build_master_row(*, code, name, data_width, marks):
    """.mst 한 행을 만든다 — 단축코드 9자 + 표준코드 12자 + 이름, 그리고 고정폭 꼬리.

    ``marks``는 ``{오프셋: 글자}``. 지정하지 않은 자리는 ``.``으로 채워, 파서가 엉뚱한
    오프셋을 읽으면 기대값과 다른 글자가 나오게 한다.
    """
    tail = ["."] * data_width
    for offset, char in marks.items():
        tail[offset] = char
    return f"{code:<9}{'KR' + code:<12}{name}{''.join(tail)}\n"


def write_master_file(tmp_path, file_name, rows):
    path = tmp_path / file_name
    path.write_text("".join(rows), encoding="cp949")
    return path


def test_download_source_uses_default_tls_verification(monkeypatch, tmp_path):
    module = load_update_stock_master()
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size=-1):
            return b""

    class FakeZipFile:
        def __init__(self, archive_path):
            self.archive_path = archive_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract(self, file_name, target_dir):
            (target_dir / file_name).write_text("", encoding="utf-8")

    def fake_urlopen(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.zipfile, "ZipFile", FakeZipFile)

    source = {
        "market": "KOSPI",
        "url": "https://example.com/kospi.zip",
        "file_name": "kospi_code.mst",
    }

    file_path = module.download_source(source, tmp_path)

    assert file_path == tmp_path / "kospi_code.mst"
    assert captured == {"url": source["url"], "kwargs": {}}


def test_deduplicate_stocks_keeps_latest_market_name_pair():
    module = load_update_stock_master()

    stocks = [
        {"code": "Q570055", "name": "한투 금선물 ETN", "market": "KOSPI", "aliases": []},
        {"code": "Q570121", "name": "한투 금선물 ETN", "market": "KOSPI", "aliases": []},
        {"code": "Q570055", "name": "한투 금선물 ETN", "market": "KOSDAQ", "aliases": []},
    ]

    assert module.deduplicate_stocks(stocks) == [
        {"code": "Q570121", "name": "한투 금선물 ETN", "market": "KOSPI", "aliases": []},
        {"code": "Q570055", "name": "한투 금선물 ETN", "market": "KOSDAQ", "aliases": []},
    ]


def test_write_stocks_rejects_empty_result(tmp_path):
    module = load_update_stock_master()
    stocks_path = tmp_path / "stocks.json"

    with pytest.raises(ValueError, match="비어 있습니다"):
        module.write_stocks([], stocks_path)

    assert not stocks_path.exists()


def test_write_stocks_replaces_target_atomically(monkeypatch, tmp_path):
    module = load_update_stock_master()
    stocks_path = tmp_path / "stocks.json"
    stocks = [{"code": "005930", "name": "삼성전자", "market": "KOSPI", "aliases": []}]
    replace_calls = []
    original_replace = module.os.replace

    def fake_replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        original_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fake_replace)

    module.write_stocks(stocks, stocks_path)

    assert json.loads(stocks_path.read_text(encoding="utf-8")) == stocks
    assert len(replace_calls) == 1
    temp_path, replace_target = replace_calls[0]
    assert temp_path.parent == tmp_path
    assert replace_target == stocks_path
    assert not temp_path.exists()


def test_data_width_matches_official_parser_field_specs():
    """꼬리 폭은 KIS 공식 파서 field_specs의 합이다 — KOSPI 227 / KOSDAQ 221.

    공식 파서(kis_kospi_code_mst.py·kis_kosdaq_code_mst.py)에 적힌 228·222는 개행을 포함한
    폭이다. 그 숫자를 그대로 쓰면 헤더 끝 1글자가 더 잘린다(#138). 되돌리는 변경을 막는다.
    """
    module = load_update_stock_master()

    assert source_for(module, "KOSPI")["data_width"] == 227
    assert source_for(module, "KOSDAQ")["data_width"] == 221


def test_parse_master_rows_keeps_the_last_header_character(tmp_path):
    """이름이 필드를 꽉 채워도 마지막 글자가 살아남는다.

    이 테스트가 잡는 mutation: data_width를 228로(= 공식 파서의 개행 포함 폭) 되돌리면
    닫는 괄호가 사라져 red가 된다.
    """
    module = load_update_stock_master()
    source = source_for(module, "KOSPI")
    rows = [
        build_master_row(
            code="0000D0",
            name="TIGER 미국채커버드콜(합성)",
            data_width=OFFICIAL_TAIL_WIDTH["KOSPI"],
            marks={0: "E", 1: "F", 29: "N", 158: "0"},
        ),
        build_master_row(
            code="005930",
            name="삼성전자",
            data_width=OFFICIAL_TAIL_WIDTH["KOSPI"],
            marks={0: "S", 1: "T", 29: "N", 158: "0"},
        ),
    ]
    file_path = write_master_file(tmp_path, "kospi_code.mst", rows)

    stocks = module.parse_master_rows(
        file_path,
        market="KOSPI",
        data_width=source["data_width"],
        field_offsets=source["field_offsets"],
        aliases_by_code={},
    )

    assert [stock["name"] for stock in stocks] == ["TIGER 미국채커버드콜(합성)", "삼성전자"]
    assert [stock["code"] for stock in stocks] == ["0000D0", "005930"]


def test_parse_master_rows_reads_classification_fields_at_market_offsets(tmp_path):
    """분류 필드는 시장별 오프셋에서 읽는다 — 두 시장의 꼬리 레이아웃이 다르다.

    각 행의 꼬리에 **다른 시장의 오프셋 자리**에 어긋난 글자를 심어 둔다. 한 시장의
    오프셋 맵을 다른 시장에 적용하면 기대값과 다른 글자가 나와 red가 된다.
    """
    module = load_update_stock_master()
    kospi = source_for(module, "KOSPI")
    kosdaq = source_for(module, "KOSDAQ")

    # KOSPI 신형우선주. KOSDAQ 자리(24·153)에는 어긋난 글자를 심는다.
    kospi_rows = [
        build_master_row(
            code="00088K",
            name="한화3우B",
            data_width=OFFICIAL_TAIL_WIDTH["KOSPI"],
            marks={0: "S", 1: "T", 29: "N", 158: "2", 24: "Y", 153: "9"},
        )
    ]
    # KOSDAQ 스팩. KOSPI 자리(29·158)에는 어긋난 글자를 심는다.
    kosdaq_rows = [
        build_master_row(
            code="0004Y0",
            name="디비금융제14호스팩",
            data_width=OFFICIAL_TAIL_WIDTH["KOSDAQ"],
            marks={0: "S", 1: "T", 24: "Y", 153: "0", 29: "N", 158: "2"},
        )
    ]

    parsed = {}
    for market, source, rows, file_name in (
        ("KOSPI", kospi, kospi_rows, "kospi_code.mst"),
        ("KOSDAQ", kosdaq, kosdaq_rows, "kosdaq_code.mst"),
    ):
        file_path = write_master_file(tmp_path, file_name, rows)
        (parsed[market],) = module.parse_master_rows(
            file_path,
            market=market,
            data_width=source["data_width"],
            field_offsets=source["field_offsets"],
            aliases_by_code={},
        )

    assert parsed["KOSPI"] == {
        "code": "00088K",
        "name": "한화3우B",
        "market": "KOSPI",
        "aliases": [],
        "scrt_grp_cls_code": "ST",
        "etpr_undt_objt_co_yn": "N",
        "prst_cls_code": "2",
    }
    assert parsed["KOSDAQ"] == {
        "code": "0004Y0",
        "name": "디비금융제14호스팩",
        "market": "KOSDAQ",
        "aliases": [],
        "scrt_grp_cls_code": "ST",
        "etpr_undt_objt_co_yn": "Y",
        "prst_cls_code": "0",
    }


def test_both_markets_emit_the_same_classification_field_names():
    """두 시장이 같은 필드 집합을 내보낸다 — 소비자가 market을 보고 갈라 읽지 않아도 된다."""
    module = load_update_stock_master()

    names = {
        source["market"]: sorted(source["field_offsets"]) for source in module.SOURCES
    }

    assert list(names) == ["KOSPI", "KOSDAQ"]
    assert names["KOSPI"] == names["KOSDAQ"] == sorted(module.CLASSIFICATION_FIELDS)


def test_parse_master_rows_rejects_a_row_too_short_for_the_tail(tmp_path):
    """꼬리 폭이 행보다 길면 끊는다 — 종목명이 빈 문자열로 접혀 조용히 버려지는 걸 막는다."""
    module = load_update_stock_master()
    source = source_for(module, "KOSPI")
    file_path = write_master_file(tmp_path, "kospi_code.mst", ["005930   KR005930   삼성전자\n"])

    with pytest.raises(ValueError, match="마스터 행이 너무 짧습니다"):
        module.parse_master_rows(
            file_path,
            market="KOSPI",
            data_width=source["data_width"],
            field_offsets=source["field_offsets"],
            aliases_by_code={},
        )
