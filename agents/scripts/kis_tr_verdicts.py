"""upstream 국내주식 TR 판정표를 다시 만들고, upstream ``main``과 어긋났는지 검사한다 (#380, #442).

조회 전용 래퍼(``finus_account_balance_readonly``)의 국내주식 허용 목록
(``finus_api._READONLY_DOMESTIC_STOCK_API_EXACT``)은 이 판정표의 ``read`` 행에서 나온다.
판정표는 ``agents/tests/fixtures/kis_domestic_stock_tr_verdicts.json``이고, 테스트가
허용 목록과 판정표를 양방향으로 대조한다. upstream이 TR을 늘렸을 때 이 스크립트로 판정표를
다시 만들고, 새 ``read`` 행을 허용 목록에 옮기면 된다(테스트가 빠진 것을 가리킨다).

사용법::

    # 판정표 재생성 — upstream을 HTTP로 받는다(git clone 불필요). ref는 브랜치·태그·커밋.
    python agents/scripts/kis_tr_verdicts.py --fetch main > agents/tests/fixtures/kis_domestic_stock_tr_verdicts.json

    # 이미 받아 둔 체크아웃에서 재생성(examples_llm, "MCP/Kis Trading MCP/configs"가 있어야 한다)
    python agents/scripts/kis_tr_verdicts.py open-trading-api > agents/tests/fixtures/kis_domestic_stock_tr_verdicts.json

    # drift 검사 — upstream main으로 다시 판정해 고정 판정표와 다르면 종료 코드 1,
    # upstream을 받지 못하면(네트워크·HTTP 오류) 종료 코드 2 (#442)
    python agents/scripts/kis_tr_verdicts.py --check main [--out fresh.json]

**왜 drift 검사가 필요한가 (#442).** Kis Trading MCP 서버는 예제 버전을 고정할 수 없다. 서버는
도구 호출마다 ``configs/<도구>.json``의 ``github_url``(``.../tree/main/examples_llm/...``)을
raw URL로 바꿔 예제를 받아 실행하고(``tools/base.py``의 ``_download_api_code``), ``kis_auth.py``도
``main`` 고정 URL로 받는다. 설정 파일은 이미지 빌드 때 ``COPY``로 구워지고, 브랜치·커밋을 바꾸는
환경변수나 로컬 예제 경로 옵션은 없다. 그래서 판정표의 ``upstream_commit``은 서버가 실제로
실행하는 코드를 보증하지 못한다. ``--check``가 그 간격을 메운다: upstream ``main``에서 공개 시세
TR이 계좌 입력을 새로 받게 되면, 그 TR이 ``pii_guard.PUBLIC_KIS_QUOTE_APIS``에 있어 마스킹 없이
외부 LLM으로 나가기 전에 이 검사가 빨개진다(``.github/workflows/kis-upstream-drift.yml``).
같은 검사가 래퍼의 ``find_api_detail`` 설명 정정 표(``finus_api._KIS_PARAM_DESCRIPTION_FIXES``, #446)에
적어 둔 upstream 원문이 서버 설정에 아직 그대로인지도 대조한다(``[설명 정정]``).

판정 규칙 — 전부 upstream 소스에서 기계적으로 읽는다. 사람이 이름을 보고 고르지 않는다.

1. **전송 방식.** 예제 소스에 ``API_URL``이 없으면 웹소켓 실시간 구독이다. REST 조회가
   아니므로 판정 대상에서 뺀다(``websocket``, 허용하지 않는다).
2. **쓰기.** ``ka._url_fetch(..., postFlag=True)``(HTTP POST)이거나, tr_id 중 하나라도 계좌
   계열(T/V/C로 시작)이면서 ``U``로 끝나면 ``write``다. KIS 규약에서 계좌 계열 tr_id의 끝
   글자는 ``R``=조회, ``U``=변경이다.
3. **조회.** GET이고 tr_id가 전부 조회형 — 계좌 계열이면 ``R``로 끝나고, 시세·정보 계열(F/H로
   시작)은 조회 전용 — 이면 ``read``다.
4. 위 어디에도 들지 않으면 ``ambiguous``다. 허용하지 않는다(fail-closed). 지금은 0건이다.

``account_input``은 그 TR이 계좌번호(``CANO``)·계좌상품코드(``ACNT_PRDT_CD``)·HTS ID
(``USER_ID``·``MY_HTSID``) 등 서버가 주입하는 값을 입력으로 받는지다(규칙은 ``_account_input``).
Kis Trading MCP가 서버 env로 채우는 값이라, 이런 조회는 **이 계좌·이 사용자**의 데이터를
돌려준다. 허용 범위를 넓힐 때 PR 본문에 따로 적는 목록의 근거다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_TOOL = "domestic_stock"
_REPO = "koreainvestment/open-trading-api"
_EXAMPLES_DIR = f"examples_llm/{_TOOL}"
_CONFIG_PATH = f"MCP/Kis Trading MCP/configs/{_TOOL}.json"
_PINNED_TABLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "kis_domestic_stock_tr_verdicts.json"
# Kis Trading MCP의 특수 api_type — KIS TR이 아니라 예제 디렉터리·판정표에 없다(``tools/base.py``).
_SPECIAL_API_TYPES = frozenset({"find_stock_code", "find_api_detail"})


def _tr_kind(tr_id: str) -> str:
    if tr_id[0] in "TVC":
        return {"R": "read", "U": "write"}.get(tr_id[-1], "unknown")
    if tr_id[0] in "FH":
        return "read"
    return "unknown"


# Kis Trading MCP 서버(``MCP/Kis Trading MCP/tools/base.py``)가 서버 env로 강제 주입하는 인자와
# 같은 규칙이다: 고정 매핑 4종(cano·acnt_prdt_cd·my_htsid·user_id)과, 예제 소스에서 찾는 동적 매핑
# ``(\w+)=\w*\.(my_\w+)``(``_extract_trenv_params_from_example``). 판정이 서버보다 좁으면 서버가
# 계좌·사용자 값을 채우는 TR을 "계좌 입력 없음"으로 잘못 적는다.
_SERVER_INJECTED_PARAMS = ("cano", "user_id", "acnt_prdt_cd", "my_htsid")
_SERVER_TRENV_PATTERN = re.compile(r"(\w+)=\w*\.(my_\w+)")


def _account_input(src: str) -> str | None:
    for name in _SERVER_INJECTED_PARAMS:
        if re.search(rf"\b{name}\b\s*[:=]", src, re.IGNORECASE):
            return name.upper()
    dynamic = _SERVER_TRENV_PATTERN.search(src)
    if dynamic:
        return dynamic.group(1).upper()
    return None


def build(root: Path, commit: str | None = None) -> dict:
    ex_dir = root / _EXAMPLES_DIR
    config_path = root / _CONFIG_PATH
    mcp_apis = json.loads(config_path.read_text(encoding="utf-8"))["apis"]
    rows = []
    for tr_dir in sorted(p for p in ex_dir.iterdir() if p.is_dir()):
        name = tr_dir.name
        src = (tr_dir / f"{name}.py").read_text(encoding="utf-8")
        rest = re.search(r'API_URL\s*=\s*"([^"]+)"', src) is not None
        tr_ids = sorted(set(re.findall(r'tr_id\s*=\s*"([A-Z0-9]+)"', src)))
        post = re.search(r"postFlag\s*=\s*True", src) is not None
        banner = re.search(r"#\s*\[국내주식\]\s*([^\n]+)", src)
        kinds = {_tr_kind(t) for t in tr_ids}
        if not rest:
            verdict = "websocket"
        elif post or "write" in kinds:
            verdict = "write"
        elif kinds == {"read"}:
            verdict = "read"
        else:
            verdict = "ambiguous"
        rows.append({
            "api_type": name,
            "tr_ids": tr_ids,
            "http_method": ("POST" if post else "GET") if rest else None,
            "verdict": verdict,
            "account_input": _account_input(src),
            "in_mcp_config": name in mcp_apis,
            "title": banner.group(1).strip() if banner else "",
        })
    if commit is None:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
    return {
        "_meta": {
            "source": "koreainvestment/open-trading-api examples_llm/domestic_stock",
            "upstream_commit": commit,
            "mcp_config_api_types": len(mcp_apis),
            "generator": "agents/scripts/kis_tr_verdicts.py (판정 규칙은 그 docstring)",
        },
        "trs": rows,
    }


def render(table: dict) -> str:
    """한 TR을 한 줄로 쓴다 — 리뷰에서 행 단위 diff로 읽히게."""
    meta = json.dumps(table["_meta"], ensure_ascii=False, indent=2).replace("\n", "\n  ")
    rows = ",\n    ".join(json.dumps(r, ensure_ascii=False) for r in table["trs"])
    return f'{{\n  "_meta": {meta},\n  "trs": [\n    {rows}\n  ]\n}}\n'


# ---------------------------------------------------------------------------
# upstream 받기 (네트워크) — git 없이 서버가 받는 것과 같은 raw 파일을 받는다
# ---------------------------------------------------------------------------

_HTTP_ATTEMPTS = 3
_HTTP_BACKOFF_SECONDS = 2.0
# 다시 보내도 결과가 같은 응답 — 없는 ref·경로(404), 잘못된 ref(422). 재시도하지 않는다.
_HTTP_NO_RETRY = frozenset({404, 422})
_sleep = time.sleep  # 테스트가 바꿔 끼운다


def _http_get(url: str) -> bytes:
    """``url``을 받는다. 일시 오류(연결 실패·타임아웃·5xx 등)는 백오프하며 최대 3번 시도한다."""
    request = urllib.request.Request(url, headers={"User-Agent": "fin-us-kis-tr-verdicts"})
    token = os.environ.get("GITHUB_TOKEN")
    # 토큰은 API 호출(레이트 리밋 완화)에만 싣는다. raw 호스트에는 보내지 않는다.
    # unredirected 헤더라 리다이렉트된 요청(다른 호스트일 수 있다)에는 실리지 않는다.
    if token and url.startswith("https://api.github.com/"):
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — 고정된 https 호스트
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _HTTP_NO_RETRY or attempt == _HTTP_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == _HTTP_ATTEMPTS:
                raise
        _sleep(_HTTP_BACKOFF_SECONDS * attempt)
    raise AssertionError("도달하지 않는다")


def fetch_upstream(ref: str, dest: Path) -> str:
    """``ref``를 커밋 SHA로 풀고, 그 커밋의 국내주식 예제와 MCP 설정을 ``dest``에 받는다.

    브랜치 이름이 아니라 SHA로 받는다 — 받는 도중 ``main``이 움직여도 한 커밋의 파일만 섞인다.
    """
    sha = _http_get(f"https://api.github.com/repos/{_REPO}/commits/{urllib.parse.quote(ref)}").decode()
    sha = json.loads(sha)["sha"]
    listing = json.loads(_http_get(
        f"https://api.github.com/repos/{_REPO}/contents/{_EXAMPLES_DIR}?ref={sha}"
    ))
    names = sorted(entry["name"] for entry in listing if entry["type"] == "dir")
    if not names:
        raise RuntimeError(f"upstream {sha}에서 {_EXAMPLES_DIR} 예제를 하나도 찾지 못했다")
    raw = f"https://raw.githubusercontent.com/{_REPO}/{sha}"
    for name in names:
        path = dest / _EXAMPLES_DIR / name / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_http_get(f"{raw}/{_EXAMPLES_DIR}/{name}/{name}.py"))
    config = dest / _CONFIG_PATH
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(_http_get(f"{raw}/{urllib.parse.quote(_CONFIG_PATH)}"))
    return sha


# ---------------------------------------------------------------------------
# drift 판정 (오프라인) — 테스트가 이 함수들을 직접 부른다
# ---------------------------------------------------------------------------

def diff_tables(pinned: dict, fresh: dict) -> list[str]:
    """두 판정표의 행 차이를 사람이 읽는 줄로 돌려준다. ``upstream_commit``은 비교하지 않는다."""
    old = {r["api_type"]: r for r in pinned["trs"]}
    new = {r["api_type"]: r for r in fresh["trs"]}
    lines = [f"추가된 TR: {name} {new[name]}" for name in sorted(new.keys() - old.keys())]
    lines += [f"사라진 TR: {name}" for name in sorted(old.keys() - new.keys())]
    for name in sorted(old.keys() & new.keys()):
        for field in sorted(old[name].keys() | new[name].keys()):
            before, after = old[name].get(field), new[name].get(field)
            if before != after:
                lines.append(f"바뀐 TR: {name}.{field}: {before!r} -> {after!r}")
    before_n = pinned["_meta"].get("mcp_config_api_types")
    after_n = fresh["_meta"].get("mcp_config_api_types")
    if before_n != after_n:
        lines.append(f"MCP 설정 api 수: {before_n} -> {after_n}")
    return lines


def public_quote_violations(table: dict, public_apis: frozenset[tuple[str, str]]) -> list[str]:
    """공개 시세 목록(마스킹 예외)의 TR이 판정표에서 공개 시세가 아니면 그 이유를 돌려준다.

    공개 시세의 조건: 판정표에 있고, ``read``이고, 서버가 계좌·사용자 값을 주입하는 입력이 없고,
    TR ID가 전부 시세·정보 계열(F·H)이다. 하나라도 어긋나면 그 TR의 응답은 계좌 데이터일 수 있어
    마스킹 예외로 둘 수 없다.
    """
    rows = {r["api_type"]: r for r in table["trs"]}
    problems = []
    for tool, api_type in sorted(public_apis):
        if tool != _TOOL:
            problems.append(f"{tool}/{api_type}: 판정표는 {_TOOL}만 다룬다 — 근거 없는 공개 시세")
            continue
        if api_type in _SPECIAL_API_TYPES:
            continue
        row = rows.get(api_type)
        if row is None:
            problems.append(f"{api_type}: 판정표(upstream 예제)에 없다")
            continue
        if row["verdict"] != "read":
            problems.append(f"{api_type}: 판정이 read가 아니다 ({row['verdict']})")
        if row["account_input"]:
            problems.append(f"{api_type}: 계좌·사용자 입력을 받는다 ({row['account_input']})")
        non_quote = [t for t in row["tr_ids"] if t[0] not in "FH"]
        if non_quote:
            problems.append(f"{api_type}: 시세 계열(F·H)이 아닌 TR ID가 있다 {non_quote}")
    return problems


def config_url_mismatches(root: Path) -> list[str]:
    """서버 설정의 ``github_url``이 판정한 예제 경로(``.../tree/main/examples_llm/domestic_stock/<api>``)를
    가리키는지 본다. 다른 경로·브랜치를 가리키면 판정표가 서버가 실행하는 파일을 본 것이 아니다."""
    apis = json.loads((root / _CONFIG_PATH).read_text(encoding="utf-8"))["apis"]
    problems = []
    for api_type, info in sorted(apis.items()):
        if api_type in _SPECIAL_API_TYPES:
            continue
        expected = f"https://github.com/{_REPO}/tree/main/{_EXAMPLES_DIR}/{api_type}"
        url = info.get("github_url")
        if url != expected:
            problems.append(f"{api_type}: github_url {url!r} (기대 {expected!r})")
    return problems


def description_fix_mismatches(root: Path, fixes: dict) -> list[str]:
    """래퍼의 ``find_api_detail`` 설명 정정 표(``finus_api._KIS_PARAM_DESCRIPTION_FIXES``, #446)가
    바로잡는다고 기록한 upstream 원문이 서버 설정에 아직 그대로 있는지 본다.

    원문이 바뀌면 래퍼는 정정을 멈추고 경고만 남긴다(fail-safe). 이 검사는 그 사실을 사람에게
    알린다 — upstream이 설명을 고쳤으면 정정 항목을 지우고, 다른 뜻으로 바꿨으면 정정을 다시 판단한다.
    """
    apis = json.loads((root / _CONFIG_PATH).read_text(encoding="utf-8"))["apis"]
    problems = []
    for (tool, api_type), params in sorted(fixes.items()):
        if tool != _TOOL:
            problems.append(f"{tool}/{api_type}: 이 검사는 {_TOOL} 설정만 받는다 — 대조하지 못했다")
            continue
        info = apis.get(api_type)
        if info is None:
            problems.append(f"{api_type}: 서버 설정에 없다")
            continue
        for name, fix in sorted(params.items()):
            current = ((info.get("params") or {}).get(name) or {}).get("description")
            if current != fix.upstream:
                problems.append(f"{api_type}.{name}: upstream 설명 {current!r} (정정 표의 원문 {fix.upstream!r})")
    return problems


def check(
    root: Path, sha: str, pinned: dict, public_apis: frozenset[tuple[str, str]],
    description_fixes: dict | None = None,
) -> tuple[dict, list[str]]:
    """받아 둔 upstream으로 판정표를 다시 만들어 고정 판정표·공개 시세 목록과 대조한다.

    *description_fixes*를 주면 설명 정정 표의 upstream 원문도 대조한다(``main``은 항상 준다).
    """
    fresh = build(root, commit=sha)
    problems = [f"[개인정보 경계] {p}" for p in public_quote_violations(fresh, public_apis)]
    problems += [f"[서버 설정] {p}" for p in config_url_mismatches(root)]
    problems += [f"[판정표 drift] {p}" for p in diff_tables(pinned, fresh)]
    if description_fixes is not None:
        problems += [f"[설명 정정] {p}" for p in description_fix_mismatches(root, description_fixes)]
    return fresh, problems


def _public_quote_apis() -> frozenset[tuple[str, str]]:
    from finus_agents.pii_guard import PUBLIC_KIS_QUOTE_APIS

    return PUBLIC_KIS_QUOTE_APIS


def _description_fixes() -> dict:
    from finus_agents.finus_api import _KIS_PARAM_DESCRIPTION_FIXES

    return _KIS_PARAM_DESCRIPTION_FIXES


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("root", nargs="?", type=Path, help="받아 둔 open-trading-api 체크아웃")
    mode.add_argument("--fetch", metavar="REF", help="upstream REF를 받아 판정표를 stdout에 쓴다")
    mode.add_argument("--check", metavar="REF", help="upstream REF로 다시 판정해 고정 판정표와 대조한다")
    parser.add_argument("--out", type=Path, help="--check: 다시 만든 판정표를 이 경로에 쓴다")
    args = parser.parse_args(argv)

    if args.root is not None:
        sys.stdout.write(render(build(args.root)))
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 받기 실패(네트워크·HTTP 오류·응답 모양)는 drift가 아니다 — 종료 코드 2로 가른다.
        # 워크플로 로그에서 "upstream이 바뀌었다"와 "GitHub에 닿지 못했다"를 헷갈리지 않게 한다.
        try:
            sha = fetch_upstream(args.fetch or args.check, root)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:  # URLError·HTTPError·JSON 오류 포함
            print(f"upstream 받기 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if args.fetch:
            sys.stdout.write(render(build(root, commit=sha)))
            return 0
        pinned = json.loads(_PINNED_TABLE.read_text(encoding="utf-8"))
        fresh, problems = check(root, sha, pinned, _public_quote_apis(), _description_fixes())
    if args.out:
        args.out.write_text(render(fresh), encoding="utf-8")
    pinned_sha = pinned["_meta"]["upstream_commit"]
    if not problems:
        print(f"upstream {sha}: 고정 판정표({pinned_sha})와 같다")
        return 0
    print(f"upstream {sha}가 고정 판정표({pinned_sha})와 다르다 — {len(problems)}건")
    for line in problems:
        print(f"  {line}")
    print(
        "\n조치: [개인정보 경계]가 있으면 그 TR을 pii_guard.PUBLIC_KIS_QUOTE_APIS에서 먼저 뺀다(fail-closed).\n"
        "[설명 정정]이 있으면 finus_api._KIS_PARAM_DESCRIPTION_FIXES의 해당 항목을 upstream 새 설명에 맞춰 "
        "지우거나 다시 판단한다(그동안 래퍼는 정정하지 않고 경고만 남긴다).\n"
        "그다음 `python agents/scripts/kis_tr_verdicts.py --fetch main > "
        "agents/tests/fixtures/kis_domestic_stock_tr_verdicts.json`로 판정표를 다시 만들고 테스트를 맞춘다."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
