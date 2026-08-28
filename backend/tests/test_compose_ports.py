"""docker-compose가 무인증 서비스를 전 인터페이스에 게시하지 않는지 고정한다(#285).

`finus-nat`은 `/v1/chat/completions`를 인증 없이 받고, `redis`에는 `requirepass`가
없는데 대기 주문(`RedisPendingOrderStore`)·폴러 offset·`_handled_ahead`(#248)·스케줄러
락처럼 금전 경로의 상태가 들어 있다. 둘 다 컴포즈 내부에서는 네트워크 별칭으로만
불리므로 호스트 게시는 로컬 디버깅 편의일 뿐이고, 그 편의는 루프백으로 충분하다.

검사는 이름을 아는 서비스를 훑는 대신 **전 서비스를 훑고 예외만 허용**하는 방향이다.
그래야 새 서비스를 `"9000:9000"`으로 추가할 때 초록불로 지나가지 않고, 전 인터페이스에
열겠다는 결정을 `_INTENTIONALLY_PUBLIC`에 적는 의식적인 행위로 만든다.

좁힌 뒤로는 `.env.example`의 `REDIS_URL`·`NAT_BASE_URL`이 호스트에서 백엔드만 띄우는
흐름의 유일한 접속 경로다. compose의 호스트 포트와 그 두 줄, 그리고 `.env`에 키가
없을 때 쓰이는 `backend/config.py`의 기본값은 한쪽만 바뀌면 조용히 끊기므로 서로
맞물려 고정한다.

검사 범위는 `docker-compose.yml` 한 파일이다. `docker compose up`은
`docker-compose.override.yml`이 있으면 자동으로 병합하므로, override 파일을 도입하는
순간 이 가드는 실제로 뜨는 구성이 아니라 그 절반만 보게 된다. 그때 병합 결과를 보도록
함께 고칠 것 — 지금은 override 파일이 없다.
"""

import ast
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from backend.scripts.setup_env import parse_env_values


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"
_CONFIG_PATH = _REPO_ROOT / "backend" / "config.py"

# 전 인터페이스 게시가 의도된 서비스. 여기에 이름을 추가하는 것은 "인증이 있거나,
# 없어도 노출을 감수한다"는 선언이므로 근거를 함께 남길 것.
#   - frontend(8080): 대시보드를 다른 기기에서 여는 것이 사용 목적 그 자체다.
#   - backend(8000): 현행 Unity 번들이 `http://localhost:8000`을 하드코딩해 원격
#     시연이 8000 직접 호출에 기대고 있다. 좁히는 것은 번들이 상대 경로를 쓰게 되는
#     #246 뒤로 남아 있다.
_INTENTIONALLY_PUBLIC = frozenset({"backend", "frontend"})

# 컨테이너가 듣는 포트(매핑의 마지막 필드). 호스트 쪽 게시를 어떻게 좁히든 이쪽이
# 바뀌면 별칭 호출(`redis:6379`·`finus-nat:8000`)과 헬스체크가 깨진다.
# 게시 목록에서 파생시키지 않고 따로 적어야, 기대값을 잘못 고친 것도 같이 잡힌다.
_CONTAINER_PORTS = {
    "backend": ["8000"],
    "finus-nat": ["8000"],
    "frontend": ["80"],
    "redis": ["6379"],
}

# 호스트에서 백엔드만 띄울 때 쓰는 `.env.example`의 값 → 그 값이 가리켜야 하는 서비스.
# 루프백으로 좁힌 뒤 이 두 줄이 유일한 접속 경로가 됐는데, compose의 호스트 포트를
# 바꾸면 조용히 끊긴다. 컨테이너 포트만 따로 보는 검사로는 호스트 쪽이 비므로,
# 두 파일을 여기서 맞물려 고정한다.
_HOST_URL_KEYS = {
    "REDIS_URL": "redis",
    "NAT_BASE_URL": "finus-nat",
}

# `.env`에 키가 없으면 `backend/config.py`의 기본값 리터럴이 호스트 실행의 유일한
# 경로가 되므로, `.env.example`과 같은 주소를 가리켜야 한다.
# `NAT_BASE_URL`은 #305에서 기본값을 8001로 옮기며 들어왔다. 그 전에는 8000이라
# 백엔드 자기 자신을 가리켰고, 어긋난 채로 고정할 수 없어 일부러 빠져 있었다.
_CONFIG_DEFAULT_KEYS = frozenset({"REDIS_URL", "NAT_BASE_URL"})


@pytest.fixture(scope="module")
def env_example_values():
    """`setup_env`가 `.env`를 만들 때 쓰는 파서를 그대로 쓴다.

    파서를 따로 두면 둘이 갈라지는 순간 이 검사가 실제 생성 결과와 다른 것을 보게
    된다. 따옴표나 `export ` 접두사를 다루지 않는 것도 그쪽 성질 그대로이고, 필요해지면
    고칠 곳도 그쪽이다(현재 `.env.example`에는 둘 다 없다).
    """

    return parse_env_values(_ENV_EXAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_services():
    document = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    return document["services"]


def _mappings(service_config):
    published = service_config.get("ports", [])
    for mapping in published:
        # 짧은 문자열 문법만 쓰고 있다. 긴 문법(dict)이나 숫자로 바꾸면 아래 검사가
        # 조용히 의미를 잃으므로, 그때는 이 헬퍼를 같이 고치라고 여기서 멈춘다.
        assert isinstance(mapping, str), (
            f"게시 항목이 문자열이 아니다({mapping!r}). 긴 문법으로 바꿨다면 "
            f"host_ip를 읽도록 이 테스트를 함께 갱신할 것."
        )
        yield mapping


def _host_side(mapping):
    """`host_ip:host_port:container_port`에서 `host_ip:host_port`를 떼어 낸다.

    `rsplit(":", 1)[0]`만 쓰면 host_ip를 생략한 2필드 매핑(`"8000:8000"`)에서 호스트
    포트(`"8000"`)가 나와 URL의 netloc과 영원히 어긋난다. 그 조합은 애초에 비교할
    호스트 주소가 없는 경우이므로, 조용히 틀린 값을 만드는 대신 여기서 멈춘다.
    """

    fields = mapping.split(":")
    assert len(fields) == 3, (
        f"게시 매핑이 `host_ip:host_port:container_port` 3필드가 아니다({mapping!r}). "
        f"host_ip를 생략한 매핑은 비교할 호스트 주소가 없으므로, 이 검사에 넣으려면 "
        f"먼저 매핑에 host_ip를 적을 것."
    )
    return ":".join(fields[:2])


def _config_default(name):
    """`backend/config.py`에서 `.env`가 비었을 때 쓰이는 기본값을 읽는다.

    import해서 읽으면 안 된다 — `config.py`는 `load_dotenv()`로 개발자의 실제 `.env`를
    먹으므로, 그 사람 설정이 있으면 기본값이 아니라 그 값을 보게 된다. 고정하려는 것은
    `.env`가 비었을 때 쓰이는 소스의 값이다.

    `os.environ.get(키, 기본값)`을 문자열 메서드로 감싸는 대입이 있어서
    (`NAT_BASE_URL`의 `.rstrip("/")`), 리터럴만 떼어 오면 실제로 쓰이는 값과 어긋난다.
    감싼 메서드를 벗겨 낸 뒤 **그대로 적용해서** 돌려준다.
    """

    for node in ast.walk(ast.parse(_CONFIG_PATH.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        return _apply_wrappers(*_unwrap_env_get(node.value, name))
    raise AssertionError(f"`backend/config.py`에서 {name} 대입을 찾지 못했다.")


def _unwrap_env_get(value, name):
    """`os.environ.get(...)` 호출과, 그것을 감싼 메서드 호출들을 분리한다.

    감싼 순서의 역순으로 쌓이므로 적용할 때 뒤집는다.
    """

    wrappers = []
    while (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr not in {"get", "getenv"}
    ):
        wrappers.append(value)
        value = value.func.value

    assert isinstance(value, ast.Call) and len(value.args) == 2, (
        f"`backend/config.py`의 {name} 대입에서 `os.environ.get(키, 기본값)`을 찾지 "
        f"못했다. 모양을 바꿨다면 이 헬퍼를 함께 갱신할 것."
    )
    return ast.literal_eval(value.args[1]), list(reversed(wrappers))


def _apply_wrappers(default, wrappers):
    """벗겨 낸 문자열 메서드를 기본값에 그대로 적용한다.

    `str`의 메서드만 허용한다 — 임의의 이름을 `getattr`로 부르면 이 헬퍼가 조용히
    엉뚱한 것을 실행하게 되고, 애초에 기본값을 다듬는 자리에 그 이상은 필요 없다.
    """

    for wrapper in wrappers:
        attr = wrapper.func.attr
        assert isinstance(getattr(str, attr, None), type(str.strip)), (
            f"`backend/config.py`의 기본값을 `str`이 아닌 `{attr}`로 감쌌다. "
            f"이 검사가 실제로 쓰이는 값을 보게 하려면 헬퍼를 함께 갱신할 것."
        )
        args = [ast.literal_eval(a) for a in wrapper.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in wrapper.keywords}
        default = getattr(default, attr)(*args, **kwargs)
    return default


def test_services_outside_the_allowlist_publish_on_loopback_only(compose_services):
    offenders = [
        f"{name}: {mapping}"
        for name, config in compose_services.items()
        if name not in _INTENTIONALLY_PUBLIC
        for mapping in _mappings(config)
        if not mapping.startswith("127.0.0.1:")
    ]
    assert offenders == [], (
        f"인증 여부를 따지지 않은 채 전 인터페이스에 게시된 서비스가 있다: {offenders}. "
        f"루프백으로 좁히거나, 노출이 의도라면 근거와 함께 _INTENTIONALLY_PUBLIC에 넣을 것."
    )


def test_allowlist_names_still_exist_in_compose(compose_services):
    """서비스 이름이 바뀌면 허용 목록이 조용히 무의미해지므로 같이 고정한다."""

    assert _INTENTIONALLY_PUBLIC <= compose_services.keys()


def test_container_port_expectations_cover_every_service(compose_services):
    """새 서비스가 아래 컨테이너 포트 검사에서 조용히 빠지지 않게 한다."""

    assert _CONTAINER_PORTS.keys() == compose_services.keys()


@pytest.mark.parametrize("service", sorted(_CONTAINER_PORTS))
def test_container_side_ports_are_unchanged(compose_services, service):
    listening = [
        mapping.rsplit(":", 1)[-1] for mapping in _mappings(compose_services[service])
    ]
    assert listening == _CONTAINER_PORTS[service]


@pytest.mark.parametrize("env_key", sorted(_HOST_URL_KEYS))
def test_env_example_points_at_the_loopback_publish(
    compose_services, env_example_values, env_key
):
    """`.env.example`의 호스트 실행용 URL이 compose가 실제로 게시하는 주소와 같아야 한다.

    루프백으로 좁힌 뒤 호스트에서 백엔드만 띄우는 흐름은 이 두 줄에만 의존한다.
    한쪽만 고치면 조용히 끊기므로 여기서 맞물려 둔다.
    """

    service = _HOST_URL_KEYS[env_key]
    published = list(_mappings(compose_services[service]))
    # 매핑이 하나뿐인 것은 `_CONTAINER_PORTS`가 이미 고정한다.
    host_side = _host_side(published[0])

    assert urlsplit(env_example_values[env_key]).netloc == host_side, (
        f"`.env.example`의 {env_key}가 compose의 `{published[0]}` 게시와 어긋난다. "
        f"둘 중 하나만 고치면 호스트에서 백엔드만 띄우는 흐름이 끊긴다."
    )


@pytest.mark.parametrize("env_key", sorted(_CONFIG_DEFAULT_KEYS))
def test_config_default_matches_the_env_example(env_example_values, env_key):
    """`.env`에 키가 없을 때 쓰이는 코드 기본값도 `.env.example`과 같아야 한다.

    `.env.example`만 맞춰 두면 그 파일을 거치지 않은 설정에서 기본값이 유일한 경로가
    된다. `setup_env`의 `render_env`는 예제에만 있는 새 키를 채워 주지만, 기존 `.env`를
    그대로 쓰는 환경은 그 경로를 거치지 않는다(#305). 어긋나는 방향은 둘 다 실제로
    나왔다 — 호스트 표기(루프백 게시가 IPv4 전용이라 `localhost`가 아니라 `127.0.0.1`)와
    포트(8000은 백엔드 자신이라 finus-nat이 아니다).
    """

    assert _config_default(env_key) == env_example_values[env_key], (
        f"`backend/config.py`의 {env_key} 기본값이 `.env.example`과 어긋난다. "
        f"`.env.example`은 compose 게시와 맞물려 있으므로 기본값도 같이 옮길 것."
    )


def test_backend_reaches_dependencies_through_compose_aliases(compose_services):
    """backend는 호스트 게시가 아니라 네트워크 별칭으로 붙는다 — 좁혀도 되는 근거다."""

    environment = compose_services["backend"]["environment"]
    # `ports`의 문법 가드와 짝을 맞춘다. compose는 `environment:`도 리스트 문법
    # (`- REDIS_URL=...`)을 허용하는데, 그러면 아래가 TypeError로 죽는다.
    assert isinstance(environment, dict), (
        "backend의 `environment:`가 dict가 아니다. 리스트 문법으로 바꿨다면 "
        "키를 읽도록 이 검사를 함께 갱신할 것."
    )
    assert environment["REDIS_URL"] == "redis://redis:6379/0"
    assert environment["NAT_BASE_URL"] == "http://finus-nat:8000"
