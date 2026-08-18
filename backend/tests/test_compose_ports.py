"""docker-compose가 무인증 서비스를 전 인터페이스에 게시하지 않는지 고정한다(#285).

`finus-nat`은 `/v1/chat/completions`를 인증 없이 받고, `redis`에는 `requirepass`가
없는데 대기 주문(`RedisPendingOrderStore`)·폴러 offset·`_handled_ahead`(#248)·스케줄러
락처럼 금전 경로의 상태가 들어 있다. 둘 다 컴포즈 내부에서는 네트워크 별칭으로만
불리므로 호스트 게시는 로컬 디버깅 편의일 뿐이고, 그 편의는 루프백으로 충분하다.

검사는 이름을 아는 서비스를 훑는 대신 **전 서비스를 훑고 예외만 허용**하는 방향이다.
그래야 새 서비스를 `"9000:9000"`으로 추가할 때 초록불로 지나가지 않고, 전 인터페이스에
열겠다는 결정을 `_INTENTIONALLY_PUBLIC`에 적는 의식적인 행위로 만든다.
"""

from pathlib import Path

import pytest
import yaml


_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"

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


def test_services_outside_the_allowlist_publish_on_loopback_only(compose_services):
    offenders = {
        name: mapping
        for name, config in compose_services.items()
        if name not in _INTENTIONALLY_PUBLIC
        for mapping in _mappings(config)
        if not mapping.startswith("127.0.0.1:")
    }
    assert offenders == {}, (
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


def test_backend_reaches_dependencies_through_compose_aliases(compose_services):
    """backend는 호스트 게시가 아니라 네트워크 별칭으로 붙는다 — 좁혀도 되는 근거다."""

    environment = compose_services["backend"]["environment"]
    assert environment["REDIS_URL"] == "redis://redis:6379/0"
    assert environment["NAT_BASE_URL"] == "http://finus-nat:8000"
