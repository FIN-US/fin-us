"""`frontend/nginx.conf`의 레이트리밋이 조용히 무의미해지지 않게 고정한다(#266 1단계).

`/api/v1/analyze`는 무인증이면서 호출 한 번이 LLM 또는 NAT 멀티 에이전트를 태워 직접
과금으로 이어진다. 제한은 프록시 지점인 nginx에 걸려 있는데, nginx 설정은 컨테이너가
뜰 때만 파싱되므로 **문법이 맞는 한 보호가 사라져도 아무것도 빨간불이 되지 않는다.**
CI의 `nginx -t`(.github/workflows/ci.yml)는 문법만 본다 — `limit_req` 한 줄을 지워도
통과한다. 그 간극을 여기서 메운다.

고정하는 것은 숫자가 아니라 **성질**이다. 허용 레이트는 운영하며 조정할 값이라
리터럴로 박아 두면 튜닝할 때마다 근거 없이 빨간불이 되고, 그러면 테스트를 먼저 고치는
습관이 든다. 대신 "analyze가 일반 API보다 엄격한가", "backend로 가는 경로에 제한이
있는가", "정적 자산은 빠져 있는가"처럼 어긋나면 실제로 위험한 관계만 본다.

예외는 `Retry-After` 하나다. 그 값은 조정 가능한 값이 아니라 가장 빡빡한 zone의 간격에서
따라 나오는 값이라, 한쪽만 바뀌면 클라이언트에게 틀린 대기 시간을 알려 주게 된다.

검사 범위는 `frontend/nginx.conf` 한 파일이다. compose가 이 파일을
`/etc/nginx/conf.d/default.conf`로 마운트하므로 실제로 뜨는 것과 같은 내용이지만,
베이스 이미지의 `nginx.conf`(http 블록의 기본값)는 보지 않는다.
"""

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_NGINX_CONF_PATH = _REPO_ROOT / "frontend" / "nginx.conf"

# 정적 서빙 경로 — backend를 거치지 않으므로 제한 대상이 아니다. `/nginx-health`는
# frontend healthcheck용이라(#252 리뷰) 제한에 걸리면 감시가 스스로를 죽인다.
_UNLIMITED_LOCATIONS = frozenset({"/", "~ \\.wasm$", "= /nginx-health"})


def _strip_comments(text):
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


def _blocks(text):
    """*text*의 최상위 `헤더 { 본문 }`을 `(헤더, 본문)` 목록으로 돌려준다.

    중괄호는 따옴표 밖에 있을 때만 센다. 429 응답의
    `return 429 '{"detail":"..."}';`가 문자열 안에 중괄호를 담고 있어, 순진하게 세면
    location 블록이 그 자리에서 닫힌 것으로 보인다.
    """

    blocks = []
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
                blocks.append((header, text[body_start:index]))
                start = index + 1
        elif char == ";" and depth == 0:
            start = index + 1
    assert depth == 0 and quote is None, "nginx.conf의 중괄호나 따옴표 짝이 맞지 않는다."
    return blocks


def _directives(body):
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


@pytest.fixture(scope="module")
def conf():
    return _strip_comments(_NGINX_CONF_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server_body(conf):
    servers = [body for header, body in _blocks(conf) if header == "server"]
    assert len(servers) == 1, f"server 블록이 하나가 아니다({len(servers)}개)."
    return servers[0]


@pytest.fixture(scope="module")
def locations(server_body):
    found = {}
    for header, body in _blocks(server_body):
        if not header.startswith("location "):
            continue
        found[header[len("location ") :].strip()] = _directives(body)
    return found


@pytest.fixture(scope="module")
def zone_rates(conf):
    """`limit_req_zone` 이름 → 분당 허용 건수."""

    rates = {}
    for directive in _directives(conf):
        if not directive.startswith("limit_req_zone "):
            continue
        fields = dict(
            field.split("=", 1) for field in directive.split() if "=" in field
        )
        name = fields["zone"].split(":", 1)[0]
        rate = fields["rate"]
        assert rate.endswith(("r/s", "r/m")), f"모르는 rate 표기다({rate})."
        value = int(rate[: -len("r/s")])
        rates[name] = value * 60 if rate.endswith("r/s") else value
    return rates


def test_strip_comments_keeps_a_hash_inside_a_quoted_string():
    """따옴표 안의 `#`을 주석으로 오해하면 그 뒤가 통째로 사라진다."""

    assert _strip_comments('return 200 "a#b"; # 꼬리') == 'return 200 "a#b"; '


def test_blocks_ignores_braces_inside_a_quoted_string():
    """429 본문의 `{"detail":...}`가 블록을 조기에 닫지 않아야 한다."""

    parsed = _blocks('location @x { return 429 \'{"detail":"d"}\'; } server { listen 80; }')

    assert [header for header, _ in parsed] == ["location @x", "server"]
    assert 'return 429 \'{"detail":"d"}\'' in _directives(parsed[0][1])


def test_directives_excludes_the_bodies_of_nested_blocks():
    """중첩 블록 안의 지시어를 바깥 것으로 세면 없는 보호가 있는 것으로 읽힌다."""

    body = " listen 80; location /a { limit_req zone=z; } server_tokens off; "

    assert _directives(body) == ["listen 80", "server_tokens off"]


def _limited_zones(directives):
    """*directives*의 `limit_req`가 참조하는 zone 이름들."""

    return {
        field.split("=", 1)[1]
        for directive in directives
        if directive.startswith("limit_req ")
        for field in directive.split()
        if field.startswith("zone=")
    }


def test_analyze_has_its_own_stricter_zone(locations, zone_rates):
    """analyze는 일반 API보다 낮은 레이트여야 한다 — 여기가 과금이 걸린 경로다.

    analyze location은 일반 zone도 함께 참조한다(전체 API 예산 안에 있어야 하므로).
    그래서 "analyze가 쓰지 않는 zone"과 비교하면 비교 대상이 비어 버린다. 양쪽에서
    실제로 적용되는 **가장 엄격한** 레이트끼리 견준다.
    """

    analyze = _limited_zones(locations["= /api/v1/analyze"])
    general = _limited_zones(locations["/api/"])
    assert analyze, (
        "`/api/v1/analyze`에 limit_req가 없다. location 사이에는 상속이 없으므로 "
        "`location /api/`에 걸어 둔 것은 여기로 따라오지 않는다."
    )
    assert general, "`location /api/`에 limit_req가 없다."

    strictest = min(zone_rates[name] for name in analyze)
    baseline = min(zone_rates[name] for name in general)
    assert strictest < baseline, (
        f"analyze에 적용되는 가장 엄격한 레이트({strictest}r/m)가 일반 API "
        f"({baseline}r/m)보다 낮지 않다. 별도 zone을 두는 이유가 사라진다."
    )


def test_analyze_also_caps_concurrency(locations):
    """레이트만으로는 못 막는다 — 한 건이 proxy_read_timeout 300s만큼 살아 있다.

    간격을 지키며 천천히 밀어 넣어도 진행 중인 LLM 호출은 계속 쌓이므로, 동시 연결
    상한이 실제 비용의 뚜껑이다.
    """

    assert any(
        d.startswith("limit_conn ") for d in locations["= /api/v1/analyze"]
    ), "`/api/v1/analyze`에 limit_conn이 없다."


def test_every_proxied_path_is_rate_limited(locations):
    """backend로 가는 경로는 전부 제한 대상이다.

    새 프록시 location을 제한 없이 추가하면 그것이 곧 우회로가 되므로, 개별 경로 이름을
    적어 두는 대신 `proxy_pass`가 있는 블록을 전부 훑는다.
    """

    unguarded = [
        spec
        for spec, directives in locations.items()
        if any(d.startswith("proxy_pass ") for d in directives)
        and not any(d.startswith("limit_req ") for d in directives)
    ]
    assert unguarded == [], (
        f"backend로 프록시하면서 limit_req가 없는 경로가 있다: {unguarded}. "
        f"제한 없는 프록시 경로는 그 자체로 레이트리밋의 우회로다."
    )


@pytest.mark.parametrize("spec", sorted(_UNLIMITED_LOCATIONS))
def test_static_and_health_paths_stay_unlimited(locations, spec):
    """정적 자산과 `/nginx-health`는 제한에 걸리면 안 된다.

    번들 하나를 받는 데 요청 수십 개가 나가므로 정적 자산에 제한이 걸리면 대시보드가
    로딩 중에 깨지고, `/nginx-health`가 429가 되면 healthcheck가 스스로를 죽인다.
    """

    limits = [
        d for d in locations[spec] if d.startswith(("limit_req ", "limit_conn "))
    ]
    assert limits == [], f"`location {spec}`에 제한이 걸려 있다: {limits}."


def test_no_limit_sits_at_server_level(server_body):
    """server 레벨에 제한을 두면 정적 자산까지 한꺼번에 걸린다.

    location으로 내려 적는 것이 정적 자산을 빼는 유일한 수단이므로, 위의 정적 경로
    검사가 의미를 가지려면 이쪽도 함께 봐야 한다.
    """

    limits = [
        d for d in _directives(server_body) if d.startswith(("limit_req ", "limit_conn "))
    ]
    assert limits == [], f"server 레벨에 제한이 걸려 있다: {limits}."


def test_over_limit_status_is_429_not_the_default_503(server_body):
    """기본값 503은 "서버가 잠깐 죽었다"와 구분되지 않아 재시도 정책을 세울 수 없다."""

    directives = _directives(server_body)
    assert "limit_req_status 429" in directives
    assert "limit_conn_status 429" in directives


def test_over_limit_body_carries_detail_for_the_unity_banner(locations):
    """429 본문이 `{"detail": ...}`여야 Unity 배너에 읽히는 문구가 나간다.

    `ApiClient.ExtractErrorMessage`는 실패 응답을 `ErrorDetailResponse`로 파싱해 그
    문자열을 그대로 싣고, 실패하면 "분석 실패: ... status=429" 같은 기계적인 문구로
    떨어진다. nginx 기본 HTML 에러 페이지가 그 경우다. 번들을 다시 굽지 않고 안내를
    바꿀 수 있는 자리가 여기뿐이므로 함께 고정한다.
    """

    body = " ".join(locations["@too_many_requests"])

    assert "default_type application/json" in body
    assert '"detail"' in body, (
        "429 본문에 detail 키가 없다. Unity 쪽은 이 키만 읽으므로, 본문 모양을 바꾸려면 "
        "`ApiClient.ExtractErrorMessage`와 함께 볼 것(번들 재빌드가 따라온다)."
    )


def test_retry_after_matches_the_strictest_zone_interval(locations, zone_rates):
    """`Retry-After`는 가장 빡빡한 zone의 간격과 같아야 한다.

    한쪽만 바뀌면 클라이언트에게 틀린 대기 시간을 알려 준다 — 너무 짧으면 곧바로 다시
    막히고, 너무 길면 쓸 수 있는데도 기다린다.
    """

    header = [
        d for d in locations["@too_many_requests"] if d.startswith("add_header Retry-After ")
    ]
    assert len(header) == 1, f"Retry-After 헤더가 하나가 아니다({header})."
    seconds = int(header[0].split()[2])

    assert seconds == 60 // min(zone_rates.values()), (
        f"Retry-After({seconds}s)가 가장 빡빡한 zone의 간격과 다르다({zone_rates})."
    )


def test_the_over_limit_location_restates_the_security_headers(locations, server_body):
    """`add_header`를 하나라도 둔 location은 server 레벨 헤더 집합을 상속하지 않는다.

    429 응답에만 보안 헤더가 빠지는 것을 눈으로 잡기는 어렵다(설정 파일에는 아무
    표시도 나지 않는다). 그래서 server 레벨 목록을 읽어 그대로 대조한다.
    """

    inherited = {
        d for d in _directives(server_body) if d.startswith("add_header X-") or d.startswith("add_header Referrer-Policy")
    }
    over_limit = set(locations["@too_many_requests"])

    missing = inherited - over_limit
    assert missing == set(), (
        f"429 location에 다시 적지 않은 server 레벨 보안 헤더가 있다: {sorted(missing)}."
    )
