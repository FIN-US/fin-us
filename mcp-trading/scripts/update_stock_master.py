#!/usr/bin/env python3
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
STOCKS_PATH = ROOT_DIR / "data" / "stocks.json"
SOURCES = [
    {
        "market": "KOSPI",
        "url": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
        "file_name": "kospi_code.mst",
        "tail_width": 228,
    },
    {
        "market": "KOSDAQ",
        "url": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
        "file_name": "kosdaq_code.mst",
        "tail_width": 222,
    },
]


def load_existing_aliases():
    if not STOCKS_PATH.exists():
        return {}
    stocks = json.loads(STOCKS_PATH.read_text(encoding="utf-8"))
    aliases_by_code = {}
    for stock in stocks:
        aliases = stock.get("aliases")
        if isinstance(aliases, list):
            aliases_by_code[str(stock.get("code", ""))] = aliases
    return aliases_by_code


def download_source(source, target_dir):
    archive_path = target_dir / f"{source['market'].lower()}_code.mst.zip"
    with urllib.request.urlopen(source["url"]) as response:
        with archive_path.open("wb") as archive_file:
            shutil.copyfileobj(response, archive_file)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extract(source["file_name"], target_dir)
    return target_dir / source["file_name"]


def parse_master_rows(file_path, *, market, tail_width, aliases_by_code):
    stocks = []
    with file_path.open(encoding="cp949") as file:
        for row in file:
            header = row[: len(row.rstrip("\n")) - tail_width]
            code = header[:9].strip()
            name = header[21:].strip()
            if not code or not name:
                continue
            stocks.append(
                {
                    "code": code,
                    "name": name,
                    "market": market,
                    "aliases": aliases_by_code.get(code, []),
                }
            )
    return stocks


def deduplicate_stocks(stocks):
    deduped_by_market_name = {}
    for stock in stocks:
        key = (stock["market"], stock["name"])
        deduped_by_market_name[key] = stock
    return list(deduped_by_market_name.values())


def write_stocks(stocks, stocks_path=STOCKS_PATH):
    if not stocks:
        raise ValueError("종목 마스터 갱신 결과가 비어 있습니다.")

    next_text = json.dumps(stocks, ensure_ascii=False, indent=2) + "\n"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=stocks_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(next_text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, stocks_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main():
    aliases_by_code = load_existing_aliases()
    with tempfile.TemporaryDirectory() as temp_name:
        temp_dir = Path(temp_name)
        stocks = []
        for source in SOURCES:
            file_path = download_source(source, temp_dir)
            stocks.extend(
                parse_master_rows(
                    file_path,
                    market=source["market"],
                    tail_width=source["tail_width"],
                    aliases_by_code=aliases_by_code,
                )
            )

    stocks = deduplicate_stocks(stocks)
    stocks.sort(key=lambda stock: (stock["market"], stock["code"]))
    write_stocks(stocks)
    print(f"updated {STOCKS_PATH} with {len(stocks)} stocks")


if __name__ == "__main__":
    main()
