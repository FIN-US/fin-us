"""upstream 국내주식 TR 판정표를 다시 만든다 (#380).

조회 전용 래퍼(``finus_account_balance_readonly``)의 국내주식 허용 목록
(``finus_api._READONLY_DOMESTIC_STOCK_API_EXACT``)은 이 판정표의 ``read`` 행에서 나온다.
판정표는 ``finus_nat/tests/fixtures/kis_domestic_stock_tr_verdicts.json``이고, 테스트가
허용 목록과 판정표를 양방향으로 대조한다. upstream이 TR을 늘렸을 때 이 스크립트로 판정표를
다시 만들고, 새 ``read`` 행을 허용 목록에 옮기면 된다(테스트가 빠진 것을 가리킨다).

사용법::

    git clone --depth 1 --filter=blob:none --sparse https://github.com/koreainvestment/open-trading-api
    git -C open-trading-api sparse-checkout set examples_llm "MCP/Kis Trading MCP/configs"
    python finus_nat/scripts/kis_tr_verdicts.py open-trading-api > finus_nat/tests/fixtures/kis_domestic_stock_tr_verdicts.json

판정 규칙 — 전부 upstream 소스에서 기계적으로 읽는다. 사람이 이름을 보고 고르지 않는다.

1. **전송 방식.** 예제 소스에 ``API_URL``이 없으면 웹소켓 실시간 구독이다. REST 조회가
   아니므로 판정 대상에서 뺀다(``websocket``, 허용하지 않는다).
2. **쓰기.** ``ka._url_fetch(..., postFlag=True)``(HTTP POST)이거나, tr_id 중 하나라도 계좌
   계열(T/V/C로 시작)이면서 ``U``로 끝나면 ``write``다. KIS 규약에서 계좌 계열 tr_id의 끝
   글자는 ``R``=조회, ``U``=변경이다.
3. **조회.** GET이고 tr_id가 전부 조회형 — 계좌 계열이면 ``R``로 끝나고, 시세·정보 계열(F/H로
   시작)은 조회 전용 — 이면 ``read``다.
4. 위 어디에도 들지 않으면 ``ambiguous``다. 허용하지 않는다(fail-closed). 지금은 0건이다.

``account_input``은 그 TR이 계좌번호(``CANO``)나 HTS ID(``USER_ID``)를 입력으로 받는지다.
Kis Trading MCP가 서버 env로 채우는 값이라, 이런 조회는 **이 계좌·이 사용자**의 데이터를
돌려준다. 허용 범위를 넓힐 때 PR 본문에 따로 적는 목록의 근거다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_TOOL = "domestic_stock"


def _tr_kind(tr_id: str) -> str:
    if tr_id[0] in "TVC":
        return {"R": "read", "U": "write"}.get(tr_id[-1], "unknown")
    if tr_id[0] in "FH":
        return "read"
    return "unknown"


def _account_input(src: str) -> str | None:
    if re.search(r"\bCANO\b\s*[:=]", src, re.IGNORECASE):
        return "CANO"
    if re.search(r"\bUSER_ID\b\s*[:=]", src, re.IGNORECASE):
        return "USER_ID"
    return None


def build(root: Path) -> dict:
    ex_dir = root / "examples_llm" / _TOOL
    config_path = root / "MCP" / "Kis Trading MCP" / "configs" / f"{_TOOL}.json"
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
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    return {
        "_meta": {
            "source": "koreainvestment/open-trading-api examples_llm/domestic_stock",
            "upstream_commit": commit,
            "mcp_config_api_types": len(mcp_apis),
            "generator": "finus_nat/scripts/kis_tr_verdicts.py (판정 규칙은 그 docstring)",
        },
        "trs": rows,
    }


def render(table: dict) -> str:
    """한 TR을 한 줄로 쓴다 — 리뷰에서 행 단위 diff로 읽히게."""
    meta = json.dumps(table["_meta"], ensure_ascii=False, indent=2).replace("\n", "\n  ")
    rows = ",\n    ".join(json.dumps(r, ensure_ascii=False) for r in table["trs"])
    return f'{{\n  "_meta": {meta},\n  "trs": [\n    {rows}\n  ]\n}}\n'


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.write(render(build(Path(sys.argv[1]))))
