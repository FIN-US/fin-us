"""docker-compose가 무인증 서비스를 전 인터페이스에 게시하지 않는지 고정한다(#285).

`finus-nat`은 `/v1/chat/completions`를 인증 없이 받고, `redis`에는 `requirepass`가
없는데 대기 주문(`RedisPendingOrderStore`)·폴러 offset·`_handled_ahead`(#248)·스케줄러
락처럼 금전 경로의 상태가 들어 있다. 둘 다 컴포즈 내부에서는 네트워크 별칭으로만
불리므로 호스트 게시는 로컬 디버깅 편의일 뿐이고, 그 편의는 루프백으로 충분하다.

`backend`(8000)·`frontend`(8080)는 아직 전 인터페이스에 열려 있는 것이 의도된
상태다 — 현행 Unity 번들이 `http://localhost:8000`을 하드코딩해 원격 시연이 8000
직접 호출에 기대고 있어, 좁히는 것은 #246의 번들 교체 뒤로 미뤄져 있다.
"""

from pathlib import Path

import pytest
import yaml


_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"

# 인증이 없어 루프백 밖으로 나가면 안 되는 서비스 → 기대하는 게시 목록.
_LOOPBACK_ONLY = {
    "finus-nat": ["127.0.0.1:8001:8000"],
    "redis": ["127.0.0.1:6379:6379"],
}


@pytest.fixture(scope="module")
def compose_services():
    document = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    return document["services"]


@pytest.mark.parametrize("service", sorted(_LOOPBACK_ONLY))
def test_unauthenticated_services_publish_on_loopback_only(compose_services, service):
    assert compose_services[service]["ports"] == _LOOPBACK_ONLY[service]


@pytest.mark.parametrize("service", sorted(_LOOPBACK_ONLY))
def test_unauthenticated_services_keep_container_port_unchanged(
    compose_services, service
):
    """컨테이너가 듣는 포트는 그대로여야 별칭 호출(`redis:6379`·`finus-nat:8000`)이 산다.

    호스트 쪽만 좁히는 변경이므로 매핑의 컨테이너 측(마지막 필드)이 바뀌면 회귀다.
    """

    def container_ports(mappings):
        return [mapping.rsplit(":", 1)[-1] for mapping in mappings]

    assert container_ports(compose_services[service]["ports"]) == container_ports(
        _LOOPBACK_ONLY[service]
    )


def test_backend_reaches_dependencies_through_compose_aliases(compose_services):
    """backend는 호스트 게시가 아니라 네트워크 별칭으로 붙는다 — 좁혀도 영향이 없는 근거다."""

    environment = compose_services["backend"]["environment"]
    assert environment["REDIS_URL"] == "redis://redis:6379/0"
    assert environment["NAT_BASE_URL"] == "http://finus-nat:8000"
