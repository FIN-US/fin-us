"""`frontend/nginx.conf.template`의 레이트리밋이 조용히 무의미해지지 않게 고정한다(#266 1단계).

`/api/v1/analyze`는 호출 한 번이 LLM 또는 NAT 멀티 에이전트를 태워 직접 과금으로
이어진다. 제한은 프록시 지점인 nginx에 걸려 있는데, nginx 설정은 컨테이너가 뜰 때만
파싱되므로 **문법이 맞는 한 보호가 사라져도 아무것도 빨간불이 되지 않는다.**
CI의 `nginx -t`(.github/workflows/ci.yml)는 문법만 본다 — `limit_req` 한 줄을 지워도
통과한다. 그 간극을 여기서 메운다.

고정하는 것은 숫자가 아니라 **성질**이다. 허용 레이트는 운영하며 조정할 값이라
리터럴로 박아 두면 튜닝할 때마다 근거 없이 빨간불이 되고, 그러면 테스트를 먼저 고치는
습관이 든다. 대신 "analyze가 일반 API보다 엄격한가", "backend로 가는 경로에 제한이
있는가", "정적 자산은 빠져 있는가"처럼 어긋나면 실제로 위험한 관계만 본다.

예외는 `Retry-After`다. 레이트 초과 쪽 값은 조정 가능한 값이 아니라 가장 빡빡한 zone의
간격에서 따라 나오는 값이라, 한쪽만 바뀌면 클라이언트에게 틀린 대기 시간을 알려 주게 된다.
동시 실행 초과 쪽은 반대로 **헤더가 없는 것**을 고정한다 — 서버가 알 수 없는 값이라 지어내면
안 된다(PR #349 리뷰).

설정 파일을 읽는 파서는 `nginx_conf.py`에 있다(같은 파일을 훑는 검사가
`test_nginx_api_key_cookie.py`에도 있어 한 벌만 둔다). 파서 자체의 검증은
`test_nginx_conf_parse.py`다.
"""

import pytest

from backend.tests import nginx_conf


# 정적 서빙 경로 — backend를 거치지 않으므로 제한 대상이 아니다. `/nginx-health`는
# frontend healthcheck용이라(#252 리뷰) 제한에 걸리면 감시가 스스로를 죽인다.
# `= /`·`= /index.html`은 문서를 내보내며 API 키 쿠키를 싣는 자리다(#266 3단계) — 여기에
# 제한이 걸리면 새로 고침 몇 번으로 대시보드가 아예 열리지 않는다.
_UNLIMITED_LOCATIONS = frozenset(
    {"/", "= /", "= /index.html", "~ \\.wasm$", "= /nginx-health"}
)

# (제한 지시어 접두사, 내부 상태 코드, 응답을 만드는 named location).
#
# 두 거절은 성질이 다르다. limit_req는 잠깐 기다리면 풀리지만, limit_conn은 진행 중인
# 분석이 끝나야 풀리고 그건 `proxy_read_timeout 300s`까지 갈 수 있다. 같은 안내를 내면
# conn 쪽에서 거짓말이 되므로(PR #349 리뷰) 내부 코드를 갈라 다른 본문으로 보낸다.
# 430은 IANA 미할당이고 `error_page`가 가로채므로 클라이언트에게는 둘 다 429로 나간다.
_REJECTION_ROUTES = frozenset(
    {
        ("limit_req", "429", "@too_many_requests"),
        ("limit_conn", "430", "@analysis_in_flight"),
    }
)


@pytest.fixture(scope="module")
def conf():
    return nginx_conf.read_conf()


@pytest.fixture(scope="module")
def server_body(conf):
    return nginx_conf.read_server_body(conf)


@pytest.fixture(scope="module")
def locations(server_body):
    return nginx_conf.read_locations(server_body)


@pytest.fixture(scope="module")
def zone_rates(conf):
    """`limit_req_zone` 이름 → 분당 허용 건수."""

    rates = {}
    for directive in nginx_conf.directives(conf):
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
        d
        for d in nginx_conf.directives(server_body)
        if d.startswith(("limit_req ", "limit_conn "))
    ]
    assert limits == [], f"server 레벨에 제한이 걸려 있다: {limits}."


@pytest.mark.parametrize(("kind", "status", "handler"), sorted(_REJECTION_ROUTES))
def test_each_rejection_kind_is_wired_to_its_own_handler(
    server_body, locations, kind, status, handler
):
    """거절 종류마다 상태 코드와 그것을 받는 named location이 짝지어져 있어야 한다.

    기본값 503은 "서버가 잠깐 죽었다"와 구분되지 않아 재시도 정책을 세울 수 없다.
    그리고 **`error_page` 줄이 이 검사의 핵심이다** — 그 한 줄만 지우면 named location은
    그대로 남은 채 아무 데서도 참조되지 않게 되고, 응답은 nginx 기본 HTML로 조용히
    돌아간다(PR #349 리뷰). 블록 본문만 보는 검사로는 잡히지 않아 여기서 배선을 본다.
    """

    directives = nginx_conf.directives(server_body)

    assert f"{kind}_status {status}" in directives, (
        f"{kind} 거절의 상태 코드가 {status}가 아니다. 종류별로 코드가 갈려 있어야 "
        f"error_page가 서로 다른 본문으로 보낼 수 있다."
    )
    assert f"error_page {status} = {handler}" in directives, (
        f"{status}를 {handler}로 보내는 error_page가 없다. 이 줄이 없으면 "
        f"{handler} 블록이 남아 있어도 응답은 nginx 기본 HTML이다."
    )


@pytest.mark.parametrize("handler", sorted(h for _, _, h in _REJECTION_ROUTES))
def test_every_rejection_handler_answers_429_with_a_detail_body(locations, handler):
    """본문이 `{"detail": ...}`여야 Unity 배너에 읽히는 문구가 나간다.

    `ApiClient.ExtractErrorMessage`는 실패 응답을 `ErrorDetailResponse`로 파싱해 그
    문자열을 그대로 싣고, 실패하면 "분석 실패: ... status=429" 같은 기계적인 문구로
    떨어진다. nginx 기본 HTML 에러 페이지가 그 경우다. 번들을 다시 굽지 않고 안내를
    바꿀 수 있는 자리가 여기뿐이므로 함께 고정한다.

    내부 코드(430)가 클라이언트에게 새 나가지 않는 것도 여기서 본다 — 핸들러가 실제로
    내보내는 코드는 둘 다 429다.
    """

    body = " ".join(locations[handler])

    assert "default_type application/json" in body
    assert "return 429 " in body, (
        f"{handler}가 429로 응답하지 않는다. 내부 코드를 그대로 내보내면 클라이언트가 "
        f"모르는 상태 코드를 받는다."
    )
    assert '"detail"' in body, (
        "본문에 detail 키가 없다. Unity 쪽은 이 키만 읽으므로, 본문 모양을 바꾸려면 "
        "`ApiClient.ExtractErrorMessage`와 함께 볼 것(번들 재빌드가 따라온다)."
    )


def test_retry_after_matches_the_strictest_zone_interval(locations, zone_rates):
    """레이트 초과의 `Retry-After`는 가장 빡빡한 zone의 간격과 같아야 한다.

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


def test_the_concurrency_rejection_does_not_guess_a_retry_after(locations):
    """동시 실행 거절에는 `Retry-After`를 붙이지 않는다(PR #349 리뷰).

    언제 풀릴지는 진행 중인 분석의 남은 시간에 달렸는데 서버가 그걸 모른다. zone의
    레이트에서 따온 값을 그대로 쓰면 그 시각에 다시 429이고(레이트가 아니라 슬롯이
    빈 자리다), 상한인 `proxy_read_timeout 300s`를 적으면 대개 필요 이상으로
    기다리게 한다. 모르는 값을 지어내느니 선택 헤더를 빼는 쪽이 맞다.

    이 검사가 없으면 위 레이트 쪽 값을 복사해 넣는 "정리"가 조용히 통과한다.
    """

    guessed = [
        d
        for d in locations["@analysis_in_flight"]
        if d.startswith("add_header Retry-After ")
    ]
    assert guessed == [], (
        f"동시 실행 거절에 Retry-After가 붙어 있다({guessed}). 서버가 알 수 없는 값이다."
    )


@pytest.mark.parametrize("handler", sorted(h for _, _, h in _REJECTION_ROUTES))
def test_the_rejection_handlers_restate_the_security_headers(
    locations, server_body, handler
):
    """`add_header`를 하나라도 둔 location은 server 레벨 헤더 집합을 상속하지 않는다.

    거절 응답에만 보안 헤더가 빠지는 것을 눈으로 잡기는 어렵다(설정 파일에는 아무
    표시도 나지 않는다). 그래서 server 레벨 목록을 읽어 그대로 대조한다.
    """

    inherited = {
        d
        for d in nginx_conf.directives(server_body)
        if d.startswith("add_header X-") or d.startswith("add_header Referrer-Policy")
    }

    missing = inherited - set(locations[handler])
    assert missing == set(), (
        f"{handler}에 다시 적지 않은 server 레벨 보안 헤더가 있다: {sorted(missing)}."
    )
