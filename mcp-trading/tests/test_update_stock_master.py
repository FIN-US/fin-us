import importlib.util
from pathlib import Path


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
