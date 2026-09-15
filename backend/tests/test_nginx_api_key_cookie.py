"""#266 3단계: nginx가 문서 응답에 API 키를 `SameSite=Strict` 쿠키로 실어 보낸다.

2단계는 backend에 정적 키 인증을 걸었지만 추적 중인 Unity WebGL 번들이 그 키를 보내지
못했다 — `ApiClient`가 헤더를 붙이지 않고, 붙이려면 WebGL 재빌드와 `Build/` 커밋이
따라온다. 그래서 인증 기본값이 "꺼짐"이었다. 3단계는 그 전달을 nginx로 옮긴다: envsubst가
채운 키를 문서 응답의 쿠키로 내려 주면 브라우저가 이후 same-origin 요청에 자동으로 붙인다.

이 파일이 고정하는 것은 **배선**이다. 설정 한 줄이 빠지거나 속성 하나가 바뀌어도
`nginx -t`는 통과하고, 드러나는 것은 대시보드가 401로 멈추는 날 아니면 CSRF가 열린
뒤다. 어느 쪽도 파일을 눈으로 읽어야만 보인다.

파서는 `nginx_conf.py`에 있고 그 자체의 검증은 `test_nginx_conf_parse.py`다. 여기서
읽는 텍스트는 **치환 전**이므로 `${FINUS_API_KEY}`가 그대로 남아 있다 — 치환 결과가
nginx 문법에 맞는지는 CI의 `nginx -t` 잡이 키 설정/미설정 양쪽으로 본다.
"""

from pathlib import Path

import pytest
import yaml

from backend.main import API_KEY_COOKIE
from backend.tests import nginx_conf


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# 문서(`index.html`)를 내보내는 location. 쿠키는 여기서만 나간다.
#
# 두 자리를 모두 적는 것은 `location /`의 try_files가 문서를 그대로 내보내기 때문이다.
# 한쪽만 적으면 그 주소로 들어온 브라우저에는 쿠키가 가지 않고, 그건 "대시보드가 401"로만
# 드러난다.
_DOCUMENT_LOCATIONS = frozenset({"= /", "= /index.html"})

# compose가 템플릿을 걸어야 하는 컨테이너 경로. conf.d가 아니라 templates다 —
# 기동 스크립트가 envsubst로 치환한 뒤에야 nginx가 읽을 수 있는 설정이 된다.
_TEMPLATE_MOUNT = "/etc/nginx/templates/default.conf.template"

_COOKIE_DIRECTIVE = "add_header Set-Cookie $finus_api_key_cookie always"
_NO_CACHE_DIRECTIVE = 'add_header Cache-Control "no-cache" always'


@pytest.fixture(scope="module")
def conf():
    return nginx_conf.read_conf()


@pytest.fixture(scope="module")
def server_body(conf):
    return nginx_conf.read_server_body(conf)


@pytest.fixture(scope="module")
def locations(server_body):
    return nginx_conf.read_locations(server_body)


def _map_body(conf, header):
    bodies = [body for block_header, body in nginx_conf.blocks(conf) if block_header == header]
    assert len(bodies) == 1, f"`{header}` 블록이 하나가 아니다({len(bodies)}개)."
    return nginx_conf.directives(bodies[0])


@pytest.fixture(scope="module")
def cookie_map(conf):
    return _map_body(conf, "map $finus_injected_key $finus_api_key_cookie")


@pytest.fixture(scope="module")
def cookie_value(cookie_map):
    """쿠키 문자열을 만드는 `default` 분기의 값."""

    defaults = [d for d in cookie_map if d.startswith("default ")]
    assert len(defaults) == 1, f"map의 default 분기가 하나가 아니다({defaults})."
    return defaults[0][len("default ") :].strip("\"'")


@pytest.fixture(scope="module")
def frontend_service():
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    return compose["services"]["frontend"]


# --- 키가 실제로 주입되는지 -----------------------------------------------


def test_the_key_comes_from_envsubst_not_from_a_literal(server_body):
    """키는 `${FINUS_API_KEY}` 자리표시자로만 들어온다.

    #266이 (a) "빌드 타임에 굽기"를 실격시킨 이유가 바로 이것이다 — 이 파일은 git에
    추적되므로, 여기에 키를 직접 적으면 **키가 저장소에 커밋된다.** 자리표시자가 사라진
    설정은 문법이 멀쩡하고 동작도 하므로, 사람이 파일을 읽지 않으면 드러나지 않는다.

    이 테스트가 잡는 mutation: `set $finus_injected_key "s3cr3t-key";`처럼 값을 박아 넣는 회귀.
    """
    injections = [
        d
        for d in nginx_conf.directives(server_body)
        if d.startswith("set $finus_injected_key ")
    ]

    assert injections == ['set $finus_injected_key "${FINUS_API_KEY}"'], (
        f"키를 주입하는 자리가 자리표시자가 아니다({injections}). 이 파일은 git에 "
        "추적되므로 값을 적으면 키가 저장소에 커밋된다."
    )


def test_the_injected_variable_does_not_collide_with_the_env_var_name(server_body):
    """주입 변수의 이름은 `finus_api_key`가 아니어야 한다 (실측으로 찾은 함정).

    nginx는 변수 이름을 소문자로 접어 다루므로 `$finus_api_key`와 `${FINUS_API_KEY}`가
    **같은 변수**다. 이름을 그렇게 맞춰 두면 주입 줄이 "자기 값을 자기에게 대입"이 되어,
    치환이 일어나지 않은 배포에서 nginx가 **정상 기동한 뒤** 요청마다
    `using uninitialized "finus_api_key" variable` 경고만 남기고 쿠키를 조용히 빼먹는다
    (nginx:alpine에서 실측). 인증을 켰다고 믿는데 브라우저에는 키가 안 가는 상태다.

    이름을 갈라 두면 같은 상황이 기동 실패로 즉시 드러난다 —
    `[emerg] unknown "finus_api_key" variable`(실측 확인). 막는 게 아니라 막는 것처럼
    보이는 상태를 만들지 않는다는 #266 1단계의 원칙과 같다.
    """
    assert not any(
        d.startswith("set $finus_api_key ") for d in nginx_conf.directives(server_body)
    ), (
        "주입 변수 이름이 `finus_api_key`다. nginx가 변수 이름을 소문자로 접으므로 "
        "`${FINUS_API_KEY}`와 같은 변수가 되고, 치환 누락이 기동 실패가 아니라 "
        "조용한 쿠키 누락으로 나타난다."
    )


def test_the_cookie_name_matches_the_backend_constant(cookie_value):
    """쿠키 이름이 backend의 `API_KEY_COOKIE`와 같아야 한다.

    실행 경계를 넘는 계약이라 이 파일과 `backend/main.py`에 리터럴이 두 벌 존재한다.
    한쪽만 바꾸면 양쪽 테스트가 각자 초록불인 채, **인증을 켠 날에야** 대시보드가 401로
    떨어진다(`test_api_key_auth.py`가 NAT의 헤더 이름을 같은 방식으로 대조한다).
    """
    assert cookie_value.startswith(f"{API_KEY_COOKIE}=$finus_injected_key;"), (
        f"쿠키 이름이 backend의 API_KEY_COOKIE({API_KEY_COOKIE!r})로 시작하지 않는다: "
        f"{cookie_value!r}."
    )


def test_no_cookie_is_sent_when_the_key_is_empty(cookie_map):
    """키가 비어 있으면(인증 미설정 = 기본값) Set-Cookie 자체가 나가지 않는다.

    nginx는 값이 빈 `add_header`를 내보내지 않으므로, map의 빈 문자열 분기가 그
    스위치다. 이 분기를 지우면 `finus_api_key=`라는 **빈 쿠키가 브라우저에 심긴다** —
    그러면 나중에 키를 채운 날, 브라우저가 먼저 그 빈 값을 보내고 401이 난다. 키를
    올바르게 설정했는데 401인 상태이므로 원인을 찾기가 특히 어렵다.
    """
    assert "'' ''" in cookie_map, (
        f"빈 키에 대한 분기가 없다({cookie_map}). 인증을 끈 배포에 빈 쿠키가 심긴다."
    )


# --- 쿠키 속성 ------------------------------------------------------------


def test_the_cookie_is_samesite_strict(cookie_value):
    """`SameSite=Strict`는 장식이 아니라 이 방식의 안전장치다.

    쿠키 인증은 브라우저가 자격증명을 알아서 붙이므로 CSRF가 따라온다 — 이 API에는
    `POST /api/v1/db/diary` 같은 쓰기 경로가 있다. Strict가 cross-site 요청에서 쿠키를
    떼기 때문에 임의 페이지의 `fetch(..., { mode: "no-cors" })`는 인증 없는 요청이 되어
    401에서 끝난다(#266 1단계 본문이 "CORS는 방어가 되지 않는다"고 적어 둔 그 경로다).

    Lax로 내리면 top-level navigation에 쿠키가 붙어 그 구멍이 다시 열린다. 이 값을
    바꾸는 것은 인증 방식을 바꾸는 일이므로, CSRF 토큰 같은 대체 수단이 함께 와야 한다.
    """
    assert "SameSite=Strict" in cookie_value, (
        f"쿠키에 SameSite=Strict가 없다: {cookie_value!r}. 이 속성이 CSRF와 "
        "cross-site fetch 경로를 막는 유일한 수단이다."
    )


def test_the_cookie_is_http_only(cookie_value):
    """번들은 이 값을 스크립트로 읽을 필요가 없다 — 브라우저가 붙이는 것이 전부다.

    WebSocket 핸드셰이크에도 그대로 붙는다(HttpOnly는 스크립트의 읽기만 막고 네트워크
    계층의 전송은 막지 않는다). 즉 이 속성을 빼서 얻는 것이 없다.
    """
    assert "HttpOnly" in cookie_value, f"쿠키에 HttpOnly가 없다: {cookie_value!r}."


def test_the_cookie_covers_the_api_paths(cookie_value):
    """`Path=/`가 아니면 `/api/`·`/api/v1/ws` 요청에 쿠키가 붙지 않는다.

    쿠키를 내려 주는 것은 문서 응답(`/`)이므로, Path를 생략하면 기본값이 그 문서의
    디렉터리가 된다. 지금은 그것도 `/`라 우연히 동작하지만, 문서 경로가 한 단계라도
    깊어지는 날 대시보드만 조용히 401이 된다.
    """
    assert "Path=/;" in cookie_value, (
        f"쿠키에 Path=/가 없다: {cookie_value!r}. /api/ 요청에 쿠키가 붙지 않는다."
    )


def test_secure_is_conditional_on_the_scheme(conf, cookie_value):
    """`Secure`는 스킴을 보고 붙인다 — 무조건 붙이면 이 배포가 깨진다.

    주 경로는 Tailscale 위의 평문 HTTP(`http://100.x.y.z:8080`)다. 무조건 `Secure`를
    붙이면 브라우저가 쿠키를 아예 저장하지 않아 대시보드가 401로 멈추고, 설정만 보면
    "켠 것처럼" 보인다. 반대로 조건부 배선을 지우면 HTTPS 배포에서 쿠키가 평문 요청에도
    실려 나간다.
    """
    assert "$finus_cookie_secure" in cookie_value, (
        f"쿠키 문자열이 스킴별 Secure 분기를 참조하지 않는다: {cookie_value!r}."
    )
    assert "; Secure" not in cookie_value.replace("$finus_cookie_secure", ""), (
        f"Secure가 무조건 붙어 있다: {cookie_value!r}. 평문 HTTP 배포에서 쿠키가 "
        "저장되지 않아 대시보드가 401로 멈춘다."
    )

    scheme_map = _map_body(conf, "map $scheme $finus_cookie_secure")
    assert "https '; Secure'" in scheme_map, (
        f"https에서 Secure를 붙이는 분기가 없다({scheme_map})."
    )
    assert "default ''" in scheme_map, (
        f"https가 아닐 때 비우는 기본 분기가 없다({scheme_map})."
    )


# --- 어디서 나가는가 ------------------------------------------------------


@pytest.mark.parametrize("spec", sorted(_DOCUMENT_LOCATIONS))
def test_every_document_location_sends_the_cookie(locations, spec):
    """`/`와 `/index.html` 양쪽에서 쿠키가 나가야 한다.

    location 사이에는 상속이 없으므로 한쪽에만 적으면 다른 주소로 들어온 브라우저는
    키를 못 받는다. 인증을 켠 배포에서 "어떤 URL로 열었는지"에 따라 대시보드가 되거나
    안 되는 상태가 되고, 그건 재현이 어려운 종류의 고장이다.
    """
    assert spec in locations, (
        f"`location {spec}`이 없다. 문서를 내보내는 자리가 바뀌었다면 "
        "_DOCUMENT_LOCATIONS를 함께 고칠 것."
    )
    assert _COOKIE_DIRECTIVE in locations[spec], (
        f"`location {spec}`이 API 키 쿠키를 내려 주지 않는다: {locations[spec]}."
    )


@pytest.mark.parametrize("spec", sorted(_DOCUMENT_LOCATIONS))
def test_every_document_location_forbids_a_stale_cached_copy(locations, spec):
    """문서는 매번 재검증돼야 한다 — 안 그러면 쿠키가 전달될 기회 자체가 없다.

    쿠키에는 Max-Age·Expires가 없다(세션 쿠키). 브라우저를 닫으면 사라지는데, 문서에 캐시
    지시가 없으면 브라우저가 Last-Modified 기준 휴리스틱으로 캐싱해 다음 방문의 네비게이션이
    네트워크를 타지 않는다. 쿠키는 없고 문서는 캐시에서 나오는 상태 — **새 쿠키를 받을
    기회가 없다.** 대시보드가 401인데 서버 로그에는 아무 흔적도 없다(PR #363 리뷰).

    키 회전도 같은 이유로 깨진다. `.env`를 고치고 컨테이너를 다시 만들어도, 캐시된 문서를
    쓰는 브라우저는 낡은 쿠키를 계속 보낸다.

    `no-store`가 아니라 `no-cache`인 것은 재검증 응답(304)에도 Set-Cookie가 실리기
    때문이다 — nginx:alpine에서 If-None-Match·If-Modified-Since 양쪽을 실측했다. 본문을
    다시 보내지 않으면서 키는 매번 최신이다.
    """
    assert _NO_CACHE_DIRECTIVE in locations[spec], (
        f"`location {spec}`에 캐시 재검증 지시가 없다: {locations[spec]}. "
        "브라우저가 문서를 캐시에서 꺼내는 순간 Set-Cookie가 전달되지 않는다."
    )


def test_the_bundle_assets_keep_their_cache(locations):
    """캐시 제어는 문서에만 건다 — 번들에 걸면 53MB짜리 .wasm을 매번 다시 받는다.

    쿠키가 필요한 것은 문서 응답 한 번뿐이므로, 재검증 비용도 거기까지다.
    """
    cached = {
        spec
        for spec, directives in locations.items()
        if any(d.startswith("add_header Cache-Control") for d in directives)
    }

    assert cached == set(_DOCUMENT_LOCATIONS), (
        f"캐시 제어가 걸린 location이 문서 경로와 다르다: {sorted(cached)}."
    )


def test_only_the_document_locations_send_the_cookie(locations):
    """정적 자산과 프록시 경로에는 쿠키를 싣지 않는다.

    번들 하나를 받는 동안 요청 수십 개가 나가므로, 그 응답 전부에 Set-Cookie가 따라붙으면
    앞단에 캐시를 두는 순간 **키를 품은 응답이 저장된다.** 쿠키가 필요한 것은 페이지를
    처음 받는 순간 한 번뿐이다.
    """
    senders = {
        spec
        for spec, directives in locations.items()
        if any(d.startswith("add_header Set-Cookie") for d in directives)
    }

    assert senders == set(_DOCUMENT_LOCATIONS), (
        f"쿠키를 내려 주는 location이 문서 경로와 다르다: {sorted(senders)}."
    )


@pytest.mark.parametrize("spec", sorted(_DOCUMENT_LOCATIONS))
def test_the_document_locations_restate_the_security_headers(
    locations, server_body, spec
):
    """`add_header`를 하나라도 둔 location은 server 레벨 헤더 집합을 상속하지 않는다.

    쿠키를 붙이려고 `add_header`를 넣은 대가로 보안 헤더 3종이 **대시보드 문서 응답에서만**
    빠지는 것이 이 방식의 함정이다. 설정 파일에는 아무 표시도 나지 않으므로 server 레벨
    목록을 읽어 그대로 대조한다(`@too_many_requests` 쪽과 같은 검사다).
    """
    inherited = {
        d
        for d in nginx_conf.directives(server_body)
        if d.startswith("add_header X-") or d.startswith("add_header Referrer-Policy")
    }

    missing = inherited - set(locations[spec])
    assert missing == set(), (
        f"`location {spec}`에 다시 적지 않은 server 레벨 보안 헤더가 있다: {sorted(missing)}."
    )


# --- 함께 움직이는 배선 ---------------------------------------------------


def test_compose_mounts_the_template_where_envsubst_looks(frontend_service):
    """compose는 conf.d가 아니라 templates에 걸어야 한다.

    conf.d에 직접 걸면 치환이 일어나지 않고 `${FINUS_API_KEY}`가 문자 그대로 남는다.
    그러면 nginx는 그것을 정의되지 않은 변수로 보고 기동에 실패하거나, 최악의 경우
    자리표시자를 그대로 쿠키 값으로 내보낸다. 어느 쪽이든 인증을 쓰지 않는 배포에서도
    깨진다 — 정적 화면까지 함께 죽는다.
    """
    targets = [
        volume.split(":")[1]
        for volume in frontend_service["volumes"]
        if volume.count(":") >= 2 and "nginx.conf" in volume
    ]

    assert targets == [_TEMPLATE_MOUNT], (
        f"nginx 설정의 마운트 지점이 {_TEMPLATE_MOUNT}가 아니다({targets})."
    )


def test_compose_always_defines_the_key_variable(frontend_service):
    """`FINUS_API_KEY: ${FINUS_API_KEY:-}`의 `:-`가 없으면 frontend가 뜨지 않는다.

    envsubst는 **환경에 정의된** 변수만 치환한다. `.env`에 키가 없어 compose가 변수를
    아예 넘기지 않으면 자리표시자가 그대로 남고, 그 설정은 nginx 문법이 아니다. 인증을
    쓰지 않는 배포(기본값)가 정확히 그 상태이므로, 이 한 글자가 "인증을 안 쓰는 사람도
    대시보드를 볼 수 있다"를 지탱한다.

    치환 대상을 `^FINUS_`로 좁히는 것도 함께 본다. 필터의 기본값은 "환경에 정의된 모든
    변수"라, 컨테이너 환경에 nginx 변수와 같은 이름이 생기는 날 설정의 `$host`·`$uri`가
    조용히 치환된다.
    """
    environment = frontend_service["environment"]

    assert environment.get("FINUS_API_KEY") == "${FINUS_API_KEY:-}", (
        f"frontend가 FINUS_API_KEY를 기본값과 함께 넘기지 않는다({environment})."
    )
    assert environment.get("NGINX_ENVSUBST_FILTER") == "^FINUS_", (
        f"envsubst 치환 대상이 FINUS_*로 좁혀져 있지 않다({environment})."
    )


@pytest.fixture(scope="module")
def nginx_ci_job():
    workflow = yaml.safe_load(_CI_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]["nginx-config-check"]


def test_the_ci_syntax_job_checks_the_template_after_substitution(nginx_ci_job):
    """CI의 `nginx -t` 잡도 templates 경로에 걸어야 한다.

    conf.d에 직접 걸면 치환 전 텍스트를 검증하게 되어, **실제로 뜨는 것과 다른 파일을
    보는 잡**이 된다. 그러면 치환 결과에서만 깨지는 문법 오류가 초록불로 지나간다.
    """
    run = " ".join(step.get("run", "") for step in nginx_ci_job["steps"])

    assert f"nginx.conf.template:{_TEMPLATE_MOUNT}" in run, (
        f"CI가 템플릿을 {_TEMPLATE_MOUNT}에 걸지 않는다."
    )
    assert "/etc/nginx/conf.d/default.conf" not in run, (
        "CI가 아직 conf.d에 직접 걸고 있다 — 치환 전 텍스트를 검증하는 잡이 된다."
    )


def test_the_ci_syntax_job_covers_both_the_empty_and_the_filled_key(nginx_ci_job):
    """빈 키와 채운 키는 치환 결과가 다르므로 양쪽을 다 돌아야 한다.

    빈 값은 `set $finus_injected_key "";`가 되고 쿠키 map이 빈 문자열 분기를 타므로,
    한쪽만 검증하면 다른 쪽에서만 깨지는 문법 오류가 초록불로 지나간다.

    매트릭스 **항목 수**가 아니라 **값의 집합**을 본다(PR #363 리뷰). 개수만 세면 두 항목을
    모두 같은 값으로 바꾸는 회귀가 통과하고, 그건 정확히 이 검사가 막으려던 것이다.
    """
    keys = [entry["api_key"] for entry in nginx_ci_job["strategy"]["matrix"]["include"]]

    assert "" in keys, "빈 키(= 인증 미설정, 기본 배포)를 도는 항목이 없다."
    assert any(key for key in keys), "채운 키를 도는 항목이 없다."
