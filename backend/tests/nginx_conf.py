"""`frontend/nginx.conf.template`을 테스트에서 읽기 위한 최소 파서.

nginx 설정은 컨테이너가 뜰 때만 파싱되므로, 문법이 맞는 한 보호가 사라져도 아무것도
빨간불이 되지 않는다. CI의 `nginx -t`는 문법만 본다. 그 간극을 메우는 테스트가 둘 있고
(`test_nginx_rate_limit.py`, `test_nginx_api_key_cookie.py`), 둘 다 같은 파일을 같은
방식으로 훑기 때문에 파서를 여기 한 벌만 둔다. 각자 복사해 두면 한쪽 파서만 고쳐지고,
그러면 같은 파일을 보는 두 테스트가 서로 다른 것을 본다.

이 모듈 자체의 파서 검증은 `test_nginx_conf_parse.py`에 있다 — 아래 함수를 고칠 때
함께 볼 것.

⚠️ 이 파일은 **템플릿**이다. compose가 `/etc/nginx/templates/default.conf.template`로
마운트하고 nginx:alpine의 기동 스크립트가 envsubst로 `${FINUS_API_KEY}`를 치환한다
(#266 3단계). 즉 여기서 읽는 텍스트에는 치환되지 않은 `${...}`가 그대로 남아 있다.
치환 결과가 실제로 nginx 문법에 맞는지는 이 파서가 아니라 CI의 `nginx -t` 잡이 본다.

검사 범위는 이 한 파일이다. 베이스 이미지의 `nginx.conf`(http 블록의 기본값)는 보지
않는다.
"""

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
CONF_PATH = _REPO_ROOT / "frontend" / "nginx.conf.template"


def strip_comments(text):
    """`#` 주석을 지운다 — 단 따옴표 안의 `#`은 건드리지 않는다.

    아래 블록 분해가 `{`/`}`를 세는데, 주석에 중괄호가 들어 있으면(이 설정에는 실제로
    `fetch(..., { mode: "no-cors" })` 예시가 있다) 짝이 어긋나 엉뚱한 곳에서 블록이
    끝난다. 그래서 세기 전에 주석부터 걷어낸다.
    """

    out = []
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
        elif char in "\"'":
            quote = char
            out.append(char)
            index += 1
        elif char == "#":
            while index < len(text) and text[index] != "\n":
                index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def blocks(text):
    """*text*의 최상위 `헤더 { 본문 }`을 `(헤더, 본문)` 목록으로 돌려준다.

    중괄호는 따옴표 밖에 있을 때만 센다. 429 응답의
    `return 429 '{"detail":"..."}';`가 문자열 안에 중괄호를 담고 있어, 순진하게 세면
    location 블록이 그 자리에서 닫힌 것으로 보인다.
    """

    found = []
    quote = None
    depth = 0
    start = 0  # 현재 헤더가 시작되는 위치
    body_start = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
            if depth == 1:
                body_start = index + 1
                header = text[start:index].strip()
        elif char == "}":
            depth -= 1
            if depth == 0:
                found.append((header, text[body_start:index]))
                start = index + 1
        elif char == ";" and depth == 0:
            start = index + 1
    assert depth == 0 and quote is None, "nginx 설정의 중괄호나 따옴표 짝이 맞지 않는다."
    return found


def directives(body):
    """블록 본문에서 **자신의** 지시어만 뽑는다(중첩 블록 안쪽은 제외).

    location 사이에는 상속이 없으므로, 중첩 블록의 내용을 함께 세면 바깥 블록이 갖지도
    않은 제한을 가진 것으로 읽힌다.
    """

    own = []
    quote = None
    depth = 0
    current = []
    for char in body:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char == "{":
            depth += 1
            current = []
        elif char == "}":
            depth -= 1
            current = []
        elif char == ";" and depth == 0:
            own.append(" ".join("".join(current).split()))
            current = []
        elif depth == 0:
            current.append(char)
    return own


def collect_locations(body, found):
    """*body* 아래의 location을 **중첩까지** 모은다.

    nginx는 location 안에 location을 허용한다. 최상위만 훑으면 중첩 블록에 넣은
    `proxy_pass`가 "프록시 경로는 전부 제한 대상" 검사에서 조용히 빠진다.
    """

    for header, nested in blocks(body):
        if header.startswith("location "):
            spec = header[len("location ") :].strip()
            assert spec not in found, f"location 스펙이 중복된다({spec})."
            found[spec] = directives(nested)
        collect_locations(nested, found)
    return found


def read_conf():
    """주석을 걷어낸 설정 전문."""

    return strip_comments(CONF_PATH.read_text(encoding="utf-8"))


def read_server_body(conf):
    """단 하나뿐인 `server` 블록의 본문."""

    servers = [body for header, body in blocks(conf) if header == "server"]
    assert len(servers) == 1, f"server 블록이 하나가 아니다({len(servers)}개)."
    return servers[0]


def read_locations(server_body):
    """`location 스펙` → 그 블록이 **직접** 가진 지시어 목록."""

    return collect_locations(server_body, {})
