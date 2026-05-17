# REPL: NAT ``/v1/chat/completions`` 호출. 로컬 ``messages`` + 서버 ``finus_sqlite_transcript_agent``(SQLite) 동시에 사용.
# NAT는 ``conversation-id`` 헤더로 세션을 구분합니다.
# run.sh를 구동하기 위한 테스트 코드에 가깝습니다.

from __future__ import annotations

import html
import json
import os
import re
import sys
import uuid
import urllib.request

from nat_finus_nat.agents import nat_chat_extra_headers

MODEL = (
    os.environ.get("FINUS_CHAT_MODEL")
    or os.environ.get("NAT_CHAT_MODEL")
    or os.environ.get("OPENAI_CHAT_MODEL")
    or ""
)
INITIAL = (os.environ.get("FINUS_CHAT_INITIAL") or "").strip()
USE_STREAM = os.environ.get("FINUS_CHAT_STREAM", "1").strip().lower() not in ("0", "false", "no")
SHOW_COT = os.environ.get("FINUS_CHAT_COT", "1").strip().lower() not in ("0", "false", "no")
_USE_COLOR = os.environ.get("FINUS_CHAT_COLOR", "1").strip().lower() not in ("0", "false", "no") and sys.stderr.isatty()

_TAG_RE = re.compile(r"<[^>]+>")
_USER_PROMPT = "User > "


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _decode_stdin(raw: bytes) -> str:
    return raw.decode(sys.stdin.encoding or "utf-8")


def _read_user_line() -> str:
    """Read one line.

    Interactive: prompt on stderr, ``input()`` on stdin (readline/backspace work; stdout stays for replies).
    Pipes: prompt on stdout + binary readline.
    """
    if sys.stdin.isatty():
        try:
            import readline  # noqa: F401, PLC0415
        except ImportError:
            pass
        print(_USER_PROMPT, end="", flush=True, file=sys.stderr)
        return input()

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return input(_USER_PROMPT)

    print(_USER_PROMPT, end="", flush=True)
    raw = buffer.readline()
    if raw == b"":
        raise EOFError
    return _decode_stdin(raw.rstrip(b"\r\n"))


def _dim(s: str) -> str:
    if not _USE_COLOR:
        return s
    return f"\033[2m{s}\033[0m"


def _bold(s: str) -> str:
    if not _USE_COLOR:
        return s
    return f"\033[1m{s}\033[0m"


def _strip_markup(payload: str, *, max_len: int = 12000) -> str:
    t = html.unescape(payload or "")
    t = _TAG_RE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 3] + "..."
    return t


def _print_intermediate(obj: dict) -> None:
    name = obj.get("name") or "(step)"
    step_type = obj.get("type") or ""
    payload = _strip_markup(str(obj.get("payload") or ""))
    print(file=sys.stderr)
    print(_bold(f"━━ CoT · {name}") + (f"  [{step_type}]" if step_type else ""), file=sys.stderr)
    if payload:
        print(_dim(payload), file=sys.stderr)
    print(_dim("━━"), file=sys.stderr)


def _handle_sse_line(line: str, parts: list[str]) -> None:
    line = line.strip()
    if not line:
        return

    if line.startswith("intermediate_data:"):
        if not SHOW_COT:
            return
        raw = line[len("intermediate_data:") :].strip()
        obj = json.loads(raw)
        _print_intermediate(obj)
        return

    if line.startswith("data:"):
        raw = line[len("data:") :].strip()
        if raw == "[DONE]":
            return
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("code") and obj.get("message"):
            print(f"NAT error: {obj.get('message')}", file=sys.stderr)
            return
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            c = delta.get("content")
            if isinstance(c, str) and c:
                parts.append(c)
        return

    if line.startswith("{") and '"code"' in line and '"message"' in line:
        obj = json.loads(line)
        print(f"NAT error: {obj.get('message')}", file=sys.stderr)


def post_stream(messages: list[dict], *, base_url: str, conversation_id: str) -> str:
    body: dict = {"messages": messages, "stream": True}
    if MODEL:
        body["model"] = MODEL
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=nat_chat_extra_headers(conversation_id),
        method="POST",
    )
    parts: list[str] = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        enc = resp.headers.get_content_charset() or "utf-8"
        buf = ""
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk.decode(enc, errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                _handle_sse_line(line, parts)
        if buf.strip():
            for piece in buf.split("\n"):
                _handle_sse_line(piece, parts)
    return "".join(parts)


def post_json(messages: list[dict], *, base_url: str, conversation_id: str) -> dict:
    body: dict = {"messages": messages, "stream": False}
    if MODEL:
        body["model"] = MODEL
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=nat_chat_extra_headers(conversation_id),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def assistant_text(data: dict) -> str:
    ch = data.get("choices") or []
    if not ch:
        return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
    msg = ch[0].get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return str(c)


def complete_turn(messages: list[dict], *, base_url: str, conversation_id: str) -> str:
    if USE_STREAM:
        return post_stream(messages, base_url=base_url, conversation_id=conversation_id)
    return assistant_text(post_json(messages, base_url=base_url, conversation_id=conversation_id))


def main() -> None:
    _configure_stdio()
    base_url = os.environ["FINUS_NAT_URL"].rstrip("/")
    conversation_id = (os.environ.get("FINUS_CHAT_CONVERSATION_ID") or "").strip() or str(uuid.uuid4())
    os.environ["FINUS_CHAT_CONVERSATION_ID"] = conversation_id

    mode = "streaming + CoT on stderr" if USE_STREAM else "non-streaming"
    if USE_STREAM and not SHOW_COT:
        mode = "streaming (CoT hidden; FINUS_CHAT_COT=0)"
    print(f"Connected to {base_url} ({mode}). Commands: /exit, /reset", file=sys.stderr)
    print(
        _dim(
            f"conversation-id={conversation_id} (SQLite transcript; export FINUS_CHAT_CONVERSATION_ID=… to reuse; "
            "/reset starts a new id)"
        ),
        file=sys.stderr,
    )
    if USE_STREAM:
        print(_dim("Tip: FINUS_CHAT_COT=0 hides step traces; FINUS_CHAT_STREAM=0 uses one JSON response."), file=sys.stderr)

    messages: list[dict] = []
    if INITIAL:
        messages.append({"role": "user", "content": INITIAL})
        reply = complete_turn(messages, base_url=base_url, conversation_id=conversation_id)
        if not reply.strip() and USE_STREAM:
            print("(empty assistant reply; check NAT logs)", file=sys.stderr)
        print(reply)
        print()
        messages.append({"role": "assistant", "content": reply})

    while True:
        line = _read_user_line().strip()
        if not line:
            continue
        low = line.lower()
        if low in ("/exit", "/quit", "exit", "exit()", "quit", "q"):
            break
        if low == "/reset":
            messages.clear()
            conversation_id = str(uuid.uuid4())
            os.environ["FINUS_CHAT_CONVERSATION_ID"] = conversation_id
            print(_dim(f"(conversation cleared; new conversation-id={conversation_id})"), file=sys.stderr)
            continue
        messages.append({"role": "user", "content": line})
        reply = complete_turn(messages, base_url=base_url, conversation_id=conversation_id)
        if not reply.strip() and USE_STREAM:
            print("(empty assistant reply; check NAT logs)", file=sys.stderr)
        print(reply)
        print()
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
