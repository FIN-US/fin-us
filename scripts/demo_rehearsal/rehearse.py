"""시연 영상 리허설 — 채팅 장면을 실제 사용자 경로로 N회 돌려 장면별 통과율을 잰다.

실행 경로는 텔레그램 사용자 경로와 같다. 다른 것은 두 끝뿐이다.

    가짜 텔레그램 update ─▶ backend TelegramCommandHandler.handle_update   (실제 코드)
                              ├─ 자연어: llm_chat("nat") ─ 마스킹(#230) ─▶ NAT 워크플로(router.yml 전체,
                              │                                          실 LLM·실 MCP, 이 프로세스 안)
                              └─ /earnings: DART·뉴스 MCP(실제) ─▶ llm_chat("nat") ─▶ 같은 워크플로
                           ◀─ 렌더링된 텔레그램 문장(각주·판정 라벨 포함) ─ 가짜 송신기가 받는다

- backend가 NAT을 HTTP로 부르는 한 군데(``_llm_nat_chat``)만 같은 프로세스 안의 워크플로 호출로
  바꿨다. 요청·응답 모양(``ChatResponse``의 ``routed_agent``/``tools_used``)과 conversation-id·
  x-user-id 헤더는 HTTP 경로와 같게 채운다. 그래야 도구 호출과 외부 LLM 입력을 가로채 볼 수 있다.
- 텔레그램으로는 아무것도 보내지 않는다(가짜 송신기). 대화 기록·mem0는 출력 폴더의 임시 파일을
  쓰고, backend DB는 임시 SQLite다. 매매일지 저장(POST /api/v1/db/diary)은 기본으로 가로채
  성공 응답만 돌려준다(``--allow-diary-save``로 끈다).

안전장치
- 주문 가드(guard.py): 원격 KIS MCP·stdio MCP·backend MCP 호출과 backend 주문 게이트웨이에서,
  주문으로 읽히는 호출이면 **보내기 전에** 리허설 전체를 멈춘다(종료 코드 3).
- 장면 사이 간격(기본 4초)과 정답 조회 사이 간격(1.2초)으로 KIS 초당 한도를 피한다. 그래도
  EGW00201/EGW00215가 나면 그 회차는 '인프라 실패'로 따로 센다.
- 키·계좌번호·chat id를 출력하지 않는다. 계좌번호는 모든 출력에서 ``[계좌번호]``로 가린다.

사용법은 README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import grading  # noqa: E402
import guard  # noqa: E402
from guard import OrderGuardTripped  # noqa: E402

DEFAULT_KIS_URL = "http://localhost:3300/sse"
TRUTH_CALL_GAP_SEC = 1.2
STORED_RESP_CHARS = 1500
LLM_PATH_SUFFIXES = ("/chat/completions", "/responses", "/completions")
DIARY_PATH_SUFFIX = "/api/v1/db/diary"

log = logging.getLogger("demo_rehearsal")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


@dataclass
class Scene:
    id: str
    title: str
    text: str
    judge: str
    stocks: list[str] = field(default_factory=list)
    refetch: list[str] = field(default_factory=list)
    follows: str | None = None
    truth: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


def load_scenes(path: Path) -> tuple[list[Scene], dict[str, dict]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    stocks = {name: {"name": name, "code": v["code"], "aliases": list(v.get("aliases") or [])}
              for name, v in (data.get("stocks") or {}).items()}
    known = {"id", "title", "text", "judge", "stocks", "refetch", "follows", "truth"}
    scenes = []
    for raw in data.get("scene") or []:
        scene = Scene(
            id=raw["id"], title=raw.get("title", ""), text=raw["text"], judge=raw["judge"],
            stocks=list(raw.get("stocks") or []), refetch=list(raw.get("refetch") or []),
            follows=raw.get("follows"), truth=list(raw.get("truth") or []),
            params={k: v for k, v in raw.items() if k not in known},
        )
        missing = [s for s in scene.stocks if s not in stocks]
        if missing:
            raise SystemExit(f"{path.name}: {scene.id}의 종목 {missing}에 [stocks] 코드가 없습니다")
        scenes.append(scene)
    return scenes, stocks


# ---------------------------------------------------------------------------
# 환경 준비 — 무거운 import 전에 끝내야 한다(backend.core.config가 import 시점에 env를 읽는다)
# ---------------------------------------------------------------------------


def _main_checkout() -> Path | None:
    """worktree면 메인 체크아웃 경로. .env와 MCP node_modules가 메인에만 있을 수 있다."""
    try:
        common = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    path = Path(common)
    if not path.is_absolute():
        path = (REPO / path).resolve()
    return path.parent if path.name == ".git" else None


def _first_existing(candidates: list[Path | None], probe) -> Path | None:
    for c in candidates:
        if c is not None and probe(c):
            return c
    return None


def _backend_site_packages(roots: list[Path]) -> Path | None:
    for root in roots:
        venv = root / "backend" / ".venv"
        for cand in [venv / "Lib" / "site-packages", *sorted(venv.glob("lib/python3*/site-packages"))]:
            if (cand / "sqlmodel").is_dir():
                return cand
    return None


def bootstrap(args: argparse.Namespace, work_dir: Path) -> dict[str, Any]:
    main = _main_checkout()
    roots = [REPO] + ([main] if main and main != REPO else [])
    env_file = Path(args.env_file) if args.env_file else _first_existing(
        [r / ".env" for r in roots], lambda p: p.is_file())
    vendor = Path(args.vendor_root) if args.vendor_root else _first_existing(
        roots, lambda r: (r / "mcp" / "news" / "node_modules").is_dir())
    if env_file is None or not env_file.is_file():
        raise SystemExit("루트 .env를 찾지 못했습니다(--env-file로 지정).")
    if vendor is None:
        raise SystemExit("MCP node_modules가 있는 체크아웃을 찾지 못했습니다(scripts/install_fin_us_mcp.sh 또는 --vendor-root).")

    from dotenv import load_dotenv

    load_dotenv(env_file)
    work_dir.mkdir(parents=True, exist_ok=True)
    os.environ["FINUS_VENDOR_ROOT"] = str(vendor)
    os.environ["FINUS_KIS_TRADING_MCP_URL"] = args.kis_url
    os.environ.setdefault("FINUS_BACKEND_URL", "http://127.0.0.1:8000")
    # 리허설은 실제 대화 기록·선호 메모리·backend DB를 건드리지 않는다.
    os.environ["FINUS_TRANSCRIPT_DB_PATH"] = str(work_dir / "conversations.sqlite3")
    os.environ["FINUS_MEM0_STORAGE_DIR"] = str(work_dir / "mem0")
    os.environ["DATABASE_URL"] = f"sqlite:///{(work_dir / 'backend.db').as_posix()}"
    for name in ("news", "dart", "trading"):
        os.environ[f"{name.upper()}_MCP_DIR"] = str(vendor / "mcp" / name)

    sys.path.insert(0, str(REPO / "agents" / "src"))
    sys.path.insert(1, str(REPO))
    try:
        import sqlmodel  # noqa: F401
    except ImportError:
        site = Path(args.backend_site_packages) if args.backend_site_packages else _backend_site_packages(roots)
        if site is None:
            raise SystemExit("sqlmodel이 없습니다. `uv sync --project backend`로 backend venv를 만드세요.")
        sys.path.append(str(site))  # 뒤에 붙인다 — 겹치는 패키지는 agents venv 것이 이긴다
    return {"env_file": env_file, "vendor_root": vendor}


def account_variants() -> list[str]:
    digits = "".join(ch for ch in os.environ.get("KIS_ACCOUNT_NO", "") if ch.isdigit())
    if len(digits) < 8:
        return []
    out = [digits, digits[:8]]
    if len(digits) >= 10:
        out.insert(1, f"{digits[:8]}-{digits[8:]}")
    return out


class Redactor:
    def __init__(self, secrets_: list[str]):
        self.secrets = sorted({s for s in secrets_ if s}, key=len, reverse=True)

    def __call__(self, text: Any) -> Any:
        if isinstance(text, str):
            for s in self.secrets:
                text = text.replace(s, "[계좌번호]")
            return text
        if isinstance(text, dict):
            return {k: self(v) for k, v in text.items()}
        if isinstance(text, list):
            return [self(v) for v in text]
        return text


# ---------------------------------------------------------------------------
# 관측: 장면 하나 동안의 호출 기록
# ---------------------------------------------------------------------------


@dataclass
class RunCapture:
    scene: str
    calls: list[dict] = field(default_factory=list)
    llm_bodies: list[str] = field(default_factory=list)
    llm_http_errors: list[int] = field(default_factory=list)
    infra: list[str] = field(default_factory=list)
    diary_save_attempts: int = 0
    routed_agent: str | None = None
    tools_used: list[dict] = field(default_factory=list)
    seq: int = 0

    def add_call(self, server: str, tool: str, api_type: str | None, params: Any, resp: str,
                 error: str | None = None) -> None:
        self.seq += 1
        self.calls.append({"seq": self.seq, "server": server, "tool": tool, "api_type": api_type,
                           "params": params if isinstance(params, dict) else {"raw": str(params)},
                           "resp": resp, "error": error})


CURRENT: RunCapture | None = None
GUARD_TRIP: str | None = None


def trip(where: str) -> None:
    global GUARD_TRIP
    GUARD_TRIP = GUARD_TRIP or where
    raise OrderGuardTripped(where)


def _kis_infra(resp: str) -> str | None:
    if "mcp_timeout" in resp:
        return "KIS MCP 타임아웃"
    if "mcp_call_failed" in resp and any(k in resp for k in (
            "ConnectError", "Connection refused", "All connection attempts failed", "10061", "ConnectTimeout")):
        return "KIS MCP 연결 실패"
    return None


def install_patches(rt: dict[str, Any], *, allow_diary_save: bool) -> None:
    """MCP·외부 LLM·backend 일지 저장 경계에 관측과 주문 가드를 건다."""
    import httpx

    finus_api = rt["finus_api"]
    orig_remote = finus_api._mcp_call_tool_remote
    orig_stdio = finus_api._mcp_call_tool
    rt["orig_remote"] = orig_remote

    async def remote(*, transport, url, tool_name, arguments, timeout_sec):
        args = arguments if isinstance(arguments, dict) else {}
        api_type = args.get("api_type")
        if GUARD_TRIP or guard.is_order_call(tool_name, api_type):
            trip(f"원격 KIS MCP {tool_name}/{api_type}")
        resp = await orig_remote(transport=transport, url=url, tool_name=tool_name, arguments=arguments,
                                 timeout_sec=timeout_sec)
        cap = CURRENT
        if cap is not None:
            cap.add_call("kis", tool_name, api_type, args.get("params") or {}, str(resp))
            if (problem := _kis_infra(str(resp))):
                cap.infra.append(problem)
        return resp

    async def stdio(*, vendor_root, subdir, tool_name, arguments, timeout_sec):
        if GUARD_TRIP or guard.is_order_call(tool_name, (arguments or {}).get("api_type")):
            trip(f"stdio MCP {subdir}/{tool_name}")
        resp = await orig_stdio(vendor_root=vendor_root, subdir=subdir, tool_name=tool_name,
                                arguments=arguments, timeout_sec=timeout_sec)
        cap = CURRENT
        if cap is not None:
            server = Path(subdir).name  # mcp/news → news
            cap.add_call(server, tool_name, None, dict(arguments or {}), str(resp))
        return resp

    finus_api._mcp_call_tool_remote = remote
    finus_api._mcp_call_tool = stdio

    orig_send = httpx.AsyncClient.send

    async def send(self, request, *args, **kwargs):
        cap = CURRENT
        path = request.url.path
        if request.method == "POST" and path.endswith(LLM_PATH_SUFFIXES):
            body = request.content.decode("utf-8", "replace") if request.content else ""
            if cap is not None:
                cap.llm_bodies.append(body)
            resp = await orig_send(self, request, *args, **kwargs)
            if cap is not None and resp.status_code >= 400:
                cap.llm_http_errors.append(resp.status_code)
            return resp
        if request.method == "POST" and path.endswith(DIARY_PATH_SUFFIX) and not allow_diary_save:
            if cap is not None:
                cap.diary_save_attempts += 1
            return httpx.Response(200, json={"status": "success", "data": {
                "id": 0, "created_at": datetime.now().isoformat(timespec="seconds")}}, request=request)
        return await orig_send(self, request, *args, **kwargs)

    httpx.AsyncClient.send = send


# ---------------------------------------------------------------------------
# backend 쪽 대역: 텔레그램 송신기·설정 저장소·NAT 호출·주문 게이트웨이
# ---------------------------------------------------------------------------


class FakeNotifier:
    """텔레그램 대신 보낸 문장을 모은다. 실제 봇·채팅에는 아무것도 가지 않는다."""

    def __init__(self) -> None:
        self.chat_id = ""
        self.sent: list[str] = []

    async def send_text(self, text: str, *, reply_markup: dict | None = None) -> bool:
        self.sent.append(text)
        return True

    async def send_chat_action(self, action: str = "typing") -> bool:
        return True

    async def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> bool:
        return True


class FakeState:
    """설명 수준만 돌려주는 redis 대역(/level 초보 상태를 흉내 낸다)."""

    def __init__(self, level: str) -> None:
        self.level = level

    async def get_telegram_user_level(self) -> str:
        return self.level

    async def set_telegram_user_level(self, level: str) -> None:
        self.level = level


class NoOrderGateway:
    """backend 주문 게이트웨이 자리에 둔다 — 불리는 순간 리허설을 멈춘다."""

    async def place_order(self, *args: Any, **kwargs: Any) -> Any:
        trip("backend 주문 게이트웨이 place_order")


def build_handler(rt: dict[str, Any], level: str):
    from backend.ai import llm
    from backend.ai.mcp_tools import run_mcp_tool
    from backend.ai.user_preferences import nat_user_id_for_conversation
    from backend.core.redis_state import InMemoryPendingOrderStore
    from backend.telegram.commands import TelegramCommandHandler
    from backend.telegram.handlers import base as handler_base
    from nat.builder.context import ContextState
    from nat.data_models.api_server import ChatRequest, Message, UserMessageContentRoleType
    from nat.runtime.user_metadata import RequestAttributes
    from starlette.datastructures import Headers

    workflow = rt["workflow"]

    async def nat_chat_inprocess(user_msg: str, *, conversation_id: str | None = None):
        """backend ``_llm_nat_chat``(HTTP POST /v1/chat/completions)의 같은 프로세스판."""
        cid = (conversation_id or llm.NAT_CONVERSATION_ID).strip()
        headers = {"content-type": "application/json", "conversation-id": cid}
        if (uid := nat_user_id_for_conversation(cid)) is not None:
            headers["x-user-id"] = uid
        state = ContextState()
        attrs = RequestAttributes()
        attrs._request.headers = Headers(headers=headers)
        t1 = state.conversation_id.set(cid)
        t2 = state._metadata.set(attrs)
        try:
            request = ChatRequest(messages=[Message(role=UserMessageContentRoleType.USER, content=user_msg)],
                                  model=llm.NAT_CHAT_MODEL, temperature=0.2, stream=False)
            response = await workflow.ainvoke(request)
        finally:
            state._metadata.reset(t2)
            state.conversation_id.reset(t1)
        if hasattr(response, "model_dump"):
            payload = response.model_dump(mode="json")
        else:
            payload = {"choices": [{"message": {"content": str(response)}}]}
        message = llm._nat_message_from_payload(payload)
        routed, tools = llm._nat_reasoning_trace_from_payload(payload)
        if (cap := CURRENT) is not None:
            cap.routed_agent = routed
            cap.tools_used = [{"name": t.name, "ok": t.ok, "empty": t.empty} for t in tools]
        return llm.NatAnswer(message, routed_agent=routed, tools_used=tools)

    llm._llm_nat_chat = nat_chat_inprocess

    async def backend_mcp_runner(server_params, tool_name: str, arguments: dict[str, Any]) -> str:
        if GUARD_TRIP or guard.is_order_call(tool_name, (arguments or {}).get("api_type")):
            trip(f"backend MCP {tool_name}")
        cap = CURRENT
        try:
            resp = await run_mcp_tool(server_params, tool_name, arguments)
        except Exception as exc:
            if cap is not None:
                cap.add_call("backend", tool_name, None, dict(arguments or {}), "", error=repr(exc)[:500])
            raise
        if cap is not None:
            cap.add_call("backend", tool_name, None, dict(arguments or {}), str(resp))
        return resp

    from backend.telegram.presentation import normalize_level

    normalized = normalize_level(level)
    if normalized is None:
        raise SystemExit(f"알 수 없는 설명 수준: {level} (초보/중급)")
    handler_base.reset_level_cache()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        state_factory=lambda: FakeState(normalized),
        watchlist_repo=object(),
        catalyst_repo=object(),
        mcp_runner=backend_mcp_runner,
        order_gateway=NoOrderGateway(),
        pending_order_store=InMemoryPendingOrderStore(),
    )
    return handler, notifier


# ---------------------------------------------------------------------------
# 정답 조회 — 에이전트와 무관하게 KIS를 직접 부른다(에이전트 기록에는 넣지 않는다)
# ---------------------------------------------------------------------------


def _walk_json(node: Any):
    if isinstance(node, str):
        # MCP 응답은 JSON 문자열 안에 JSON 문자열이 들어 있고, 안쪽은 "The End\n[...]"처럼
        # 앞에 글자가 붙거나 배열 둘이 이어 붙기도 한다 — 최상위 값을 차례로 꺼낸다.
        decoder = json.JSONDecoder()
        i = 0
        while True:
            starts = [j for j in (node.find("[", i), node.find("{", i)) if j >= 0]
            if not starts:
                return
            start = min(starts)
            try:
                value, end = decoder.raw_decode(node, start)
            except ValueError:
                i = start + 1
                continue
            yield from _walk_json(value)
            i = end
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_json(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json(v)


class Truth:
    def __init__(self, rt: dict[str, Any]) -> None:
        self.rt = rt

    async def kis(self, api_type: str, params: dict[str, Any]) -> str:
        if guard.is_order_call("domestic_stock", api_type):
            trip(f"정답 조회 {api_type}")
        finus_api = self.rt["finus_api"]
        params = finus_api._pin_env_dv(finus_api._adjust_domestic_stock_params(api_type, params))
        try:
            resp = await self.rt["orig_remote"](
                transport="sse", url=os.environ["FINUS_KIS_TRADING_MCP_URL"], tool_name="domestic_stock",
                arguments={"api_type": api_type, "params": params}, timeout_sec=60)
        finally:
            await asyncio.sleep(TRUTH_CALL_GAP_SEC)
        return str(resp)

    async def price(self, stock: dict) -> tuple[int | None, str | None]:
        resp = await self.kis("inquire_price", {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock["code"]})
        price = grading.quote_price(resp)
        return price, None if price is not None else resp[:300]

    async def ranking(self) -> tuple[dict[str, int], str | None]:
        # 가격 조건 없는 시가총액 순위 — 대형주 정답 스냅숏(#441 측정과 같은 호출값).
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "0000", "fid_div_cls_code": "0",
                  "fid_input_price_1": "", "fid_input_price_2": "", "fid_vol_cnt": "",
                  "fid_cond_scr_div_code": "20174", "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0"}
        resp = await self.kis("market_cap", params)
        rows = grading.ranking_rows(resp)
        return rows, None if rows else resp[:300]

    async def balance(self) -> tuple[dict[str, Any] | None, str | None]:
        resp = await self.kis("inquire_balance", {"inqr_dvsn": "01"})
        dicts = list(_walk_json(resp))
        if not any("dnca_tot_amt" in d or "pdno" in d for d in dicts):
            return None, resp[:300]
        holdings, amounts = [], set()
        for d in dicts:
            if "pdno" in d and (grading._as_int(d.get("hldg_qty")) or 0) > 0:
                holdings.append({"name": str(d.get("prdt_name") or "").strip(), "code": str(d["pdno"]).strip()})
            for key, value in d.items():
                if key.endswith("_amt") and (n := grading._as_int(value)) is not None and abs(n) >= 10000:
                    amounts.add(abs(n))
        return {"holdings": holdings, "amounts": sorted(amounts)}, None


async def collect_truth(truth: Truth, scene: Scene, stocks: dict[str, dict], phase: str,
                        prior: dict | None = None) -> dict:
    """장면의 정답. *phase*가 post면 시세만 다시 조회해 직전 값과 구간을 만든다."""
    out = prior or {}
    errors = out.setdefault("errors", [])
    if "price" in scene.truth:
        entries = out.setdefault("stocks", [{**stocks[n], "refetch": (n in scene.refetch) if scene.refetch else True}
                                             for n in scene.stocks])
        for entry in entries:
            price, err = await truth.price(entry)
            entry[phase] = price
            if err:
                errors.append(f"{entry['name']} {phase}: {err}")
    if phase != "pre":
        return out
    if "ranking" in scene.truth:
        rows, err = await truth.ranking()
        out["ranking"] = rows
        if err:
            errors.append(f"ranking: {err}")
    if "holdings" in scene.truth or "balance" in scene.truth:
        bal, err = await truth.balance()
        if bal is None:
            errors.append(f"balance: {err}")
            out["holdings"] = None
        else:
            out["holdings"] = bal["holdings"]
            out["amounts"] = bal["amounts"]
        if "balance" in scene.truth:
            out["account_variants"] = account_variants()
    return out


# ---------------------------------------------------------------------------
# 장면 실행
# ---------------------------------------------------------------------------


def _llm_messages_text(body: str) -> list[tuple[str, str]]:
    """외부 LLM 요청 본문 → [(역할, 내용)] — 사람이 읽을 덤프용."""
    try:
        data = json.loads(body)
    except ValueError:
        return [("raw", body)]
    items = data.get("messages") or data.get("input") or []
    out: list[tuple[str, str]] = []
    for m in items if isinstance(items, list) else [items]:
        if not isinstance(m, dict):
            out.append(("?", str(m)))
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in content)
        text = str(content or "")
        if m.get("tool_calls"):
            text += "\n[tool_calls] " + json.dumps(m["tool_calls"], ensure_ascii=False)
        out.append((str(m.get("role", "?")), text))
    return out


def _placeholder_snippets(line: str, *, before: int = 60, after: int = 30) -> list[str]:
    """자리표시자가 든 줄을 화면에 맞게 줄인다. 도구 응답은 한 줄짜리 긴 JSON이라 앞부분만 자르면
    자리표시자가 잘려 나간다 — 자리표시자 주변만 창으로 떼어 낸다."""
    matches = list(grading.PLACEHOLDER_RE.finditer(line))
    if not matches:
        return []
    if len(line) <= 200:
        return [line]
    windows: list[list[int]] = []
    for m in matches:
        s, e = max(0, m.start() - before), min(len(line), m.end() + after)
        if windows and s <= windows[-1][1]:
            windows[-1][1] = e
        else:
            windows.append([s, e])
    return [("…" if s else "") + line[s:e] + ("…" if e < len(line) else "") for s, e in windows[:8]]


def write_llm_dump(path: Path, scene: Scene, rep: int, cap: RunCapture, answer: str, redact: Redactor,
                   verdict: grading.Verdict) -> None:
    requests = [_llm_messages_text(b) for b in cap.llm_bodies]
    models = []
    for b in cap.llm_bodies:
        try:
            models.append(json.loads(b).get("model", "?"))
        except ValueError:
            models.append("?")
    lines = [
        f"# {scene.id} {rep + 1}회차 — 외부 LLM에 실제로 나간 입력 (리허설 도구가 HTTP 요청 본문에서 캡처)",
        f"# 질문: {scene.text}",
        f"# 외부 LLM 요청 {len(requests)}건 · 모델 {sorted(set(models))}",
        f"# 판정: {grading.STATUS_LABELS.get(verdict.status)} {'; '.join(verdict.reasons)}",
        "",
        "## 자리표시자가 들어간 줄만 (화면 분할용 요약)",
        "",
    ]
    seen: set[str] = set()
    for i, msgs in enumerate(requests, 1):
        for role, text in msgs:
            for ln in text.splitlines():
                for snippet in _placeholder_snippets(ln.strip()):
                    if snippet not in seen:
                        seen.add(snippet)
                        lines.append(f"[요청 {i} · {role}] {snippet}")
    if not seen:
        lines.append("(자리표시자가 들어간 줄이 없음)")
    lines += ["", "## 요청 전문", ""]
    for i, msgs in enumerate(requests, 1):
        lines.append(f"=== 요청 {i}/{len(requests)} (메시지 {len(msgs)}개) ===")
        for role, text in msgs:
            lines += [f"[{role}]", text, ""]
    path.write_text(redact("\n".join(lines)), encoding="utf-8")
    path.with_name(path.name.replace("llm_input_", "telegram_")).write_text(
        redact(f"# {scene.id} {rep + 1}회차 — 사용자에게 보인 텔레그램 답\n\n{answer}\n"), encoding="utf-8")


class Rehearsal:
    def __init__(self, rt: dict[str, Any], args: argparse.Namespace, out_dir: Path, scenes: list[Scene],
                 stocks: dict[str, dict], redact: Redactor) -> None:
        self.rt = rt
        self.args = args
        self.out_dir = out_dir
        self.scenes = {s.id: s for s in scenes}
        self.stocks = stocks
        self.redact = redact
        self.truth = Truth(rt)
        self.handler, self.notifier = build_handler(rt, args.level)
        self.update_id = int(time.time())
        self.dump_scenes = set(args.dump_llm_input.split(",")) if args.dump_llm_input else set()

    async def send(self, text: str, chat_id: str) -> tuple[str, str | None]:
        from backend.telegram.handlers.texts import NAT_PROGRESS_MESSAGE, PROGRESS_DONE_MESSAGE

        self.notifier.chat_id = chat_id
        self.notifier.sent.clear()
        self.update_id += 1
        error = None
        try:
            await self.handler.handle_update({"update_id": self.update_id, "message": {
                "message_id": self.update_id, "chat": {"id": chat_id}, "text": text}})
        except OrderGuardTripped:
            raise
        except Exception as exc:  # noqa: BLE001 — 핸들러 예외도 회차 결과다
            error = f"{type(exc).__name__}: {exc}"[:500]
        answer = "\n".join(t for t in self.notifier.sent if t not in (NAT_PROGRESS_MESSAGE, PROGRESS_DONE_MESSAGE))
        return answer, error

    async def run_scene(self, scene: Scene, rep: int, chat_id: str, *, kind: str = "scene",
                        earlier_observed: dict[str, set[int]] | None = None) -> tuple[dict, RunCapture]:
        global CURRENT
        truth: dict = {}
        if kind == "scene" and scene.truth:
            truth = await collect_truth(self.truth, scene, self.stocks, "pre")
        cap = RunCapture(scene.id)
        CURRENT = cap
        started = time.time()
        try:
            answer, error = await self.send(scene.text, chat_id)
        finally:
            CURRENT = None
        sec = round(time.time() - started, 1)
        if kind == "scene" and "price" in scene.truth:
            truth = await collect_truth(self.truth, scene, self.stocks, "post", truth)
        for entry in truth.get("stocks") or []:
            entry["earlier_observed"] = sorted((earlier_observed or {}).get(entry["name"], set()))
        record = {
            "scene": scene.id, "title": scene.title, "kind": kind, "rep": rep, "question": scene.text,
            "judge": scene.judge, "params": scene.params, "started_at": datetime.fromtimestamp(started)
            .isoformat(timespec="seconds"), "sec": sec, "answer": answer, "error": error,
            "routed_agent": cap.routed_agent, "tools_used": cap.tools_used, "calls": cap.calls,
            "llm": {"requests": len(cap.llm_bodies), "http_errors": cap.llm_http_errors,
                    "text": "\n".join(cap.llm_bodies)},
            "truth": truth, "infra": list(dict.fromkeys(cap.infra)), "diary_save_attempts": cap.diary_save_attempts,
        }
        if kind == "scene":
            verdict = grading.grade(record)
            record["verdict"] = verdict.as_dict()
            if scene.id in self.dump_scenes:
                write_llm_dump(self.out_dir / f"llm_input_{scene.id}_{rep + 1}.txt", scene, rep, cap, answer,
                               self.redact, verdict)
        return record, cap

    def stored(self, record: dict) -> dict:
        """jsonl에 남길 모양 — 외부 LLM 입력 전문·계좌번호 후보는 빼고, 응답은 자른다."""
        rec = dict(record)
        rec["llm"] = {k: v for k, v in record["llm"].items() if k != "text"}
        rec["llm"]["chars"] = len(record["llm"].get("text") or "")
        truth = dict(record.get("truth") or {})
        truth.pop("account_variants", None)
        if "amounts" in truth:  # 잔고 금액 값은 남기지 않는다 — 판정은 이미 끝났다
            truth["amounts"] = f"{len(truth['amounts'])}개(값은 저장하지 않음)"
        rec["truth"] = truth
        rec["calls"] = [{**c, "resp": (c.get("resp") or "")[:STORED_RESP_CHARS]} for c in record["calls"]]
        return self.redact(rec)

    def print_line(self, record: dict) -> None:
        v = record.get("verdict") or {}
        status = grading.STATUS_LABELS.get(v.get("status"), "준비") if record["kind"] == "scene" else "선행"
        calls = ", ".join(f"{c['server']}:{c.get('api_type') or c['tool']}" for c in record["calls"])
        print(f"[{record['scene']} {record['rep'] + 1}회차] {status} ({record['sec']}s) "
              f"route={record.get('routed_agent')} calls=[{calls}]", flush=True)
        for r in v.get("reasons") or []:
            print(f"    - {self.redact(r)}", flush=True)
        if record["scene"] != "S11" and record.get("answer"):
            preview = record["answer"][:160].replace("\n", " / ")
            print(f"    답: {self.redact(preview)}", flush=True)

    async def run(self, selected: list[str], repeat: int, jsonl: Path) -> list[dict]:
        records: list[dict] = []
        with jsonl.open("a", encoding="utf-8") as fh:
            for rep in range(repeat):
                rep_chat = _new_chat_id()
                done: set[str] = set()
                observed: dict[str, set[int]] = {}
                for sid in selected:
                    scene = self.scenes[sid]
                    chat = rep_chat if self.args.thread == "rep" else _new_chat_id()
                    if scene.follows and (self.args.thread == "scene" or scene.follows not in done):
                        setup, cap = await self.run_scene(self.scenes[scene.follows], rep, chat, kind="setup")
                        self._observe(cap, observed)
                        fh.write(json.dumps(self.stored(setup), ensure_ascii=False) + "\n")
                        self.print_line(setup)
                        await asyncio.sleep(self.args.gap)
                    record, cap = await self.run_scene(scene, rep, chat, earlier_observed=observed)
                    self._observe(cap, observed)
                    done.add(sid)
                    fh.write(json.dumps(self.stored(record), ensure_ascii=False) + "\n")
                    fh.flush()
                    records.append(self.stored(record))
                    self.print_line(record)
                    await asyncio.sleep(self.args.gap)
        return records

    def _observe(self, cap: RunCapture, observed: dict[str, set[int]]) -> None:
        """같은 대화에서 에이전트가 받은 현재가 — 후속 장면이 직전 턴 값을 말해도 맞게 본다."""
        for name, stock in self.stocks.items():
            for c in grading.price_calls_for({"calls": cap.calls}, stock):
                if (p := grading.quote_price(c.get("resp") or "")) is not None:
                    observed.setdefault(name, set()).add(p)


def _new_chat_id() -> str:
    # 텔레그램 그룹 id 모양(음수)이되 실제로 쓰일 수 없는 범위. 실제 chat id는 쓰지 않는다.
    return str(-(9_900_000_000_000 + secrets.randbelow(10**9)))


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def kis_reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 80), timeout=3):
            return True
    except OSError:
        return False


def write_summary(out_dir: Path, records: list[dict], header: list[str]) -> str:
    body = grading.summarize(records)
    text = "\n".join(["# 데모 리허설 결과", "", *header, "", body])
    (out_dir / "summary.md").write_text(text, encoding="utf-8")
    return text


async def amain(args: argparse.Namespace) -> int:
    scenes, stocks = load_scenes(Path(args.scenes_file))
    by_id = {s.id: s for s in scenes}
    selected = [s.strip() for s in args.scenes.split(",")] if args.scenes else [s.id for s in scenes]
    unknown = [s for s in selected if s not in by_id]
    if unknown:
        raise SystemExit(f"알 수 없는 장면 {unknown}. 자동 장면: {list(by_id)} (S1·S2·S7·S8·S13은 CHECKLIST.md)")
    out_dir = Path(args.out) if args.out else HERE / "out" / datetime.now().strftime("%Y%m%d-%H%M%S")
    work = out_dir / "tmp"
    info = bootstrap(args, work)
    redact = Redactor(account_variants())

    logging.basicConfig(level=logging.WARNING, handlers=[logging.FileHandler(out_dir / "run.log", encoding="utf-8")],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    for name in ("finus_agents", "backend", "demo_rehearsal"):
        logging.getLogger(name).setLevel(logging.INFO)

    if not kis_reachable(args.kis_url):
        print(f"KIS 모의 MCP({urlparse(args.kis_url).netloc})가 응답하지 않습니다. kis-trade-mcp 컨테이너를 띄우세요.")
        return 2

    from nat.builder.workflow_builder import WorkflowBuilder
    from nat.runtime.loader import load_config

    import finus_agents
    from finus_agents import finus_api

    rt: dict[str, Any] = {"finus_api": finus_api}
    install_patches(rt, allow_diary_save=args.allow_diary_save)
    config_path = Path(args.config)
    print(f"finus_agents: {Path(finus_agents.__file__).parent}", flush=True)
    print(f"config: {config_path.name} · 장면 {selected} × {args.repeat}회 · 대화 묶음 {args.thread} · "
          f"간격 {args.gap}s · 결과 {out_dir}", flush=True)

    jsonl = out_dir / "runs.jsonl"
    records: list[dict] = []
    tripped = None
    started = datetime.now()
    config = load_config(config_path)
    async with WorkflowBuilder.from_config(config=config) as builder:
        rt["workflow"] = builder.get_workflow()
        rehearsal = Rehearsal(rt, args, out_dir, scenes, stocks, redact)
        # 연결 확인 겸 정답 조회 한 번 — KIS가 시세를 못 주면 장면을 돌려도 판정이 서지 않는다.
        price, err = await rehearsal.truth.price(stocks[next(iter(stocks))])
        if price is None:
            print(f"KIS 현재가 확인 실패: {redact(err or '')[:200]}")
            return 2
        try:
            records = await rehearsal.run(selected, args.repeat, jsonl)
        except OrderGuardTripped as exc:
            tripped = str(exc)
        if GUARD_TRIP and not tripped:
            tripped = GUARD_TRIP
    if not records and jsonl.exists():
        records = [json.loads(ln) for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    header = [
        f"- 실행: {started:%Y-%m-%d %H:%M} ~ {datetime.now():%H:%M} · 장면 {', '.join(selected)} × {args.repeat}회",
        f"- 경로: backend TelegramCommandHandler → llm_chat(마스킹) → NAT `{config_path.name}`(같은 프로세스) · "
        f"KIS {urlparse(args.kis_url).netloc} · 대화 묶음 `{args.thread}` · 장면 간격 {args.gap}s",
        f"- 원자료: `runs.jsonl` (한 줄 = 한 회차, 실패 회차는 `answer`·`calls`로 원인을 본다)",
    ]
    if tripped:
        header.insert(0, f"> **주문 가드가 리허설을 멈췄다: {tripped}** — 주문은 나가지 않았다(호출 전에 막음).")
    summary = write_summary(out_dir, records, header)
    print("\n" + summary)
    if tripped:
        print(f"!!! 주문 가드 작동: {tripped} — 리허설을 중단했습니다.")
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="시연 영상 리허설: 채팅 장면을 실제 사용자 경로로 N회 돌려 통과율을 잰다.")
    p.add_argument("--scenes", default="", help="쉼표로 구분한 장면 ID(기본: scenes.toml의 전부). 예: S3,S4,S11")
    p.add_argument("--repeat", type=int, default=1, help="장면별 반복 횟수(기본 1)")
    p.add_argument("--gap", type=float, default=4.0, help="장면 사이 간격 초(KIS 한도, 기본 4)")
    p.add_argument("--thread", choices=("rep", "scene"), default="rep",
                   help="rep: 한 회차의 장면을 한 대화로 잇는다(녹화와 같음, 기본) / scene: 장면마다 새 대화")
    p.add_argument("--dump-llm-input", nargs="?", const="S11", default="",
                   help="외부 LLM 입력을 사람이 읽을 파일로 남길 장면(값 없이 주면 S11). 예: --dump-llm-input S11,S9")
    p.add_argument("--level", default="beginner", help="설명 수준(beginner/intermediate, 기본 beginner=초보)")
    p.add_argument("--allow-diary-save", action="store_true", help="S12 매매일지를 실제 backend에 저장한다(기본: 가로챔)")
    p.add_argument("--config", default=str(REPO / "agents" / "configs" / "router.yml"),
                   help="NAT 설정(기본 agents/configs/router.yml — 메모리 모드 전체 라우터)")
    p.add_argument("--scenes-file", default=str(HERE / "scenes.toml"), help="장면 설정 파일")
    p.add_argument("--kis-url", default=DEFAULT_KIS_URL, help=f"KIS 모의 MCP SSE 주소(기본 {DEFAULT_KIS_URL})")
    p.add_argument("--out", default="", help="결과 폴더(기본 scripts/demo_rehearsal/out/<시각>)")
    p.add_argument("--env-file", default="", help="루트 .env 경로(기본: 레포 루트, 없으면 메인 체크아웃)")
    p.add_argument("--vendor-root", default="", help="mcp/*/node_modules가 있는 체크아웃(기본 자동)")
    p.add_argument("--backend-site-packages", default="", help="sqlmodel·redis를 빌릴 backend venv site-packages")
    p.add_argument("--summarize", default="", metavar="RUNS_JSONL", help="실행 없이 기존 runs.jsonl의 요약만 다시 만든다")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.summarize:
        path = Path(args.summarize)
        records = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(write_summary(path.parent, records, [f"- 원자료: `{path.name}`"]))
        return 0
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
