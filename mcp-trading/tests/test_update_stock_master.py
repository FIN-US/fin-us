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
