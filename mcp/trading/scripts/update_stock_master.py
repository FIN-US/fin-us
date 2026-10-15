#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
STOCKS_PATH = ROOT_DIR / "data" / "stocks.json"

# ---------------------------------------------------------------------------
# "종목코드 형태 토큰 + 수량 토큰" 이름 검사 (PR #392 리뷰)
# ---------------------------------------------------------------------------
# backend의 /buy·/sell 파서(telegram_commands._order_argument_readings)는 `/buy 005930 10 75000`처럼
# 이름 자리가 종목코드 하나면 "005930 10"을 종목명으로 읽는 시장가 해석을 만들지 않는다. 그래서 마스터에
# "ABC123 200" 같은 이름·별칭이 생기면 `/buy ABC123 200 10`은 그 종목의 시장가 주문으로 읽힐 수 없고,
# 두 해석이 모두 실재할 때의 되묻기도 하지 못한다(ABC123이 다른 종목의 코드면 그 종목의 지정가 주문
# 확인이 뜬다). 이 전제는 backend 테스트가 커밋된 stocks.json으로 확인하지만 CI에서만 돈다. 이 스크립트로
# 운영자가 직접 갱신하면 CI 없이 파일이 바뀌므로, 쓰기 전에 같은 검사를 한다.
#
# 두 판정은 backend와 의미가 같아야 한다. 이 스크립트는 backend를 import하지 않고 따로 구현하므로
# (호스트의 python3 하나로 도는 독립 스크립트다) 공유 판정표
# mcp/trading/tests/fixtures/unreadable_numeric_name_policy.json에 두 구현을 함께 묶는다.
# - 코드 형태: backend/stock_code._looks_like_stock_code — 대문자화 후 6·7·9자 영숫자이고 숫자 포함.
# - 수량 토큰: backend TelegramCommandHandler._parse_positive_int — 쉼표를 뗀 int()가 양수.
_CODE_SHAPE_RE = re.compile(r"\A(?:[0-9A-Z]{6,7}|[0-9A-Z]{9})\Z")


def looks_like_stock_code(token):
    value = token.strip().upper()
    return bool(_CODE_SHAPE_RE.match(value)) and any(ch in "0123456789" for ch in value)


def parse_positive_int(token):
    try:
        value = int(token.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def is_unreadable_numeric_name(label):
    """backend 파서가 이 이름을 시장가 해석의 종목명으로 만들 수 없으면 True."""
    parts = str(label).split()
    return (
        len(parts) == 2
        and looks_like_stock_code(parts[0])
        and parse_positive_int(parts[1]) is not None
    )


def find_unreadable_numeric_names(stocks):
    """(종목코드, 이름 또는 별칭) 목록. 비어 있어야 backend 파서의 전제가 선다."""
    found = []
    for stock in stocks:
        aliases = stock.get("aliases")
        labels = [stock.get("name", "")] + (aliases if isinstance(aliases, list) else [])
        for label in labels:
            if is_unreadable_numeric_name(label):
                found.append((str(stock.get("code", "")), str(label)))
    return found


def check_unreadable_numeric_names(stocks, *, allow, stream=None):
    """쓰기 전 검사. 쓰면 되면 True.

    위반이 있으면 기본은 쓰지 않는다(False). ``allow``면 경고만 남기고 쓴다 — KRX가 그런 이름을
    상장하면 마스터 갱신 자체가 막혀 신규 종목을 받지 못하는 것이 더 나쁘기 때문이다.
    """
    stream = stream if stream is not None else sys.stderr
    violations = find_unreadable_numeric_names(stocks)
    if not violations:
        return True
    print(
        "종목명·별칭이 '종목코드 형태 토큰 + 숫자'인 종목이 있습니다. backend의 /buy·/sell 파서는 "
        "이 이름을 시장가 주문의 종목명으로 읽지 못하고, 모호한 입력을 되묻지도 못합니다 "
        "(`/buy <앞 토큰> <숫자> <수량>`은 앞 토큰 종목코드의 지정가 주문으로 읽힙니다).",
        file=stream,
    )
    for code, label in violations:
        print(f"  {code}\t{label}", file=stream)
    if not allow:
        print(
            "stocks.json을 쓰지 않았습니다. backend/telegram/commands.py _order_argument_readings의 "
            "종목코드 이름 자리 분기를 먼저 재검토하거나, 알고 넘기려면 "
            "--allow-unreadable-numeric-names로 다시 실행하세요.",
            file=stream,
        )
        return False
    print(
        "경고: --allow-unreadable-numeric-names로 그대로 씁니다. 위 종목은 종목코드로만 주문하도록 "
        "안내하세요.",
        file=stream,
    )
    return True


# .mst 한 행은 "가변폭 헤더(단축코드·표준코드·한글명) + 고정폭 꼬리"다. 헤더가 어디서 끝나는지는
# 꼬리 폭으로만 정해지므로 data_width가 이 파서의 유일한 기준점이다.
#
# data_width는 KIS 공식 파서(kis_kospi_code_mst.py·kis_kosdaq_code_mst.py)의 field_specs 합이다
# — KOSPI 227자(70필드) / KOSDAQ 221자(64필드). 공식 파서에 적힌 228·222는 개행을 포함한 폭이다
# (`row[0:len(row) - 228]`·`row[-228:]`의 row가 개행을 달고 있다). 이 스크립트는 개행을 먼저 떼고
# 자르므로 1 작은 값을 써야 한다. 전에는 228·222를 그대로 빼서 헤더 끝 1글자를 더 잘라냈고,
# 이름이 필드를 꽉 채운 종목에서 닫는 괄호가 사라졌다(#138).
#
# field_offsets는 꼬리 안에서 종목 분류 필드가 앉은 자리다. 공식 field_specs의 누적 폭으로 계산한
# 값이고, **두 시장의 레이아웃이 달라 시장별로 둔다** — 같은 필드가 다른 오프셋에 있다.
# 꼬리에는 ETP 상품구분코드(KOSPI [22:23] / KOSDAQ [18:19])도 있지만, scrt_grp_cls_code가 ETF를
# EF, ETN을 EN으로 이미 갈라 주고 따로 쓸 곳이 없어 넣지 않았다.
_KOSPI_FIELD_OFFSETS = {
    "scrt_grp_cls_code": (0, 2),  # 증권그룹구분코드: ST 주권 / EF ETF / EN ETN / RT 리츠 / BC 수익증권 ...
    "etpr_undt_objt_co_yn": (29, 30),  # 기업인수목적회사여부(스팩): Y/N
    "prst_cls_code": (158, 159),  # 우선주 구분 코드: 0 해당없음 / 1 구형우선주 / 2 신형우선주
}
_KOSDAQ_FIELD_OFFSETS = {
    "scrt_grp_cls_code": (0, 2),
    "etpr_undt_objt_co_yn": (24, 25),
    "prst_cls_code": (153, 154),
}
# stocks.json에 실리는 분류 필드 이름. 두 시장이 같은 집합을 내보내야 소비자가 시장을 보고
# 필드 유무를 갈라 읽지 않는다.
CLASSIFICATION_FIELDS = tuple(_KOSPI_FIELD_OFFSETS)

SOURCES = [
    {
        "market": "KOSPI",
        "url": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
        "file_name": "kospi_code.mst",
        "data_width": 227,
        "field_offsets": _KOSPI_FIELD_OFFSETS,
    },
    {
        "market": "KOSDAQ",
        "url": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
        "file_name": "kosdaq_code.mst",
        "data_width": 221,
        "field_offsets": _KOSDAQ_FIELD_OFFSETS,
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


def parse_master_rows(file_path, *, market, data_width, field_offsets, aliases_by_code):
    stocks = []
    with file_path.open(encoding="cp949") as file:
        for row in file:
            line = row.rstrip("\n")
            header_width = len(line) - data_width
            # 헤더 폭이 이름 자리(21자) 아래로 내려가면 종목명이 빈 문자열로 나오고 그 행은
            # 조용히 버려진다. 꼬리 폭이 틀렸거나 마스터 레이아웃이 바뀐 것이므로, 한 행씩
            # 흘리는 대신 끊는다 — 마스터 일부가 빠진 stocks.json이 쓰이는 게 더 나쁘다.
            if header_width < 22:
                raise ValueError(
                    f"{market} 마스터 행이 너무 짧습니다(행 {len(line)}자, 꼬리 {data_width}자). "
                    "KIS 마스터 레이아웃이 바뀌었는지 확인하세요."
                )
            header, tail = line[:header_width], line[header_width:]
            code = header[:9].strip()
            name = header[21:].strip()
            if not code or not name:
                continue
            stock = {
                "code": code,
                "name": name,
                "market": market,
                "aliases": aliases_by_code.get(code, []),
            }
            for field, (start, end) in field_offsets.items():
                stock[field] = tail[start:end].strip()
            stocks.append(stock)
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


def parse_args(argv):
    parser = argparse.ArgumentParser(description="KIS 공개 종목 마스터로 mcp/trading/data/stocks.json을 갱신한다.")
    parser.add_argument(
        "--allow-unreadable-numeric-names",
        action="store_true",
        help=(
            "종목명·별칭이 '종목코드 형태 토큰 + 숫자'인 종목이 있어도 경고만 하고 쓴다. "
            "backend 파서가 그 종목을 이름으로 시장가 주문하지 못하고 되묻지도 못한다."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
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
                    data_width=source["data_width"],
                    field_offsets=source["field_offsets"],
                    aliases_by_code=aliases_by_code,
                )
            )

    stocks = deduplicate_stocks(stocks)
    stocks.sort(key=lambda stock: (stock["market"], stock["code"]))
    if not check_unreadable_numeric_names(stocks, allow=args.allow_unreadable_numeric_names):
        return 2
    write_stocks(stocks, STOCKS_PATH)
    print(f"updated {STOCKS_PATH} with {len(stocks)} stocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
