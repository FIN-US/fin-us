"""ConnectionManager.broadcast의 전송 경로.

WebSocket 소비자가 아직 없어(Unity 번들은 폴링만 사용) 아래 결함들은 운영에서 증상으로
나타난 적이 없다. 소비자가 붙는 시점에야 드러나므로 테스트로 고정해 둔다.
"""

import json

import pytest

from backend.ws_manager import ConnectionManager


class FakeWebSocket:
    """send_text만 갖춘 최소 스텁.

    on_send로 전송 도중 부수효과(다른 커넥션의 disconnect 등)를 주입한다. 실제 소켓에서는
    await 지점에 다른 태스크가 끼어들어 생기는 일을 여기서는 결정적으로 재현한다.
    """

    def __init__(self, name, *, fail=False, on_send=None):
        self.name = name
        self.fail = fail
        self.on_send = on_send
        self.sent = []

    async def send_text(self, payload):
        if self.on_send is not None:
            self.on_send()
        if self.fail:
            raise RuntimeError(f"{self.name} 소켓이 끊김")
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_broadcast_delivers_to_every_connection():
    manager = ConnectionManager()
    a, b = FakeWebSocket("a"), FakeWebSocket("b")
    manager.active_connections.extend([a, b])

    await manager.broadcast({"type": "SYSTEM_PING", "message": "안녕"})

    # 직렬화가 루프 밖으로 나갔으므로 커넥션마다 같은 문자열이 가야 한다.
    assert a.sent == b.sent
    assert json.loads(a.sent[0]) == {"type": "SYSTEM_PING", "message": "안녕"}


@pytest.mark.asyncio
async def test_broadcast_does_not_skip_when_list_shrinks_mid_iteration():
    """전송 도중 목록이 줄어도 남은 커넥션을 건너뛰지 않습니다.

    원본 리스트를 순회하면 첫 커넥션(index 0)이 await 중에 자기 자신을 disconnect할 때
    리스트가 [b, c]로 줄고, 다음 반복이 index 1을 읽어 c로 건너뛴다 — b만 이 브로드캐스트를
    통째로 놓치고, 예외가 아니라서 로그에도 남지 않는다.

    이 테스트가 잡는 mutation: `for connection in list(self.active_connections)`의
    list(...) 스냅샷을 제거하는 회귀.
    """
    manager = ConnectionManager()
    b, c = FakeWebSocket("b"), FakeWebSocket("c")
    # a의 전송 중에 a 자신이 목록에서 빠지는 상황 — 실제로는 a의 수신 루프가
    # WebSocketDisconnect를 받아 manager.disconnect(a)를 부르는 경우다.
    a = FakeWebSocket("a", on_send=lambda: manager.disconnect(a))
    manager.active_connections.extend([a, b, c])

    await manager.broadcast({"type": "SYSTEM_PING"})

    assert len(a.sent) == 1
    assert len(b.sent) == 1, "index가 밀려 b가 건너뛰어졌습니다"
    assert len(c.sent) == 1


@pytest.mark.asyncio
async def test_broadcast_removes_failed_connection_and_keeps_going():
    """전송이 실패한 커넥션은 목록에서 빠지고, 나머지 전송은 계속됩니다."""
    manager = ConnectionManager()
    a = FakeWebSocket("a")
    dead = FakeWebSocket("dead", fail=True)
    c = FakeWebSocket("c")
    manager.active_connections.extend([a, dead, c])

    await manager.broadcast({"type": "SYSTEM_PING"})

    assert manager.active_connections == [a, c]
    # 실패한 커넥션 뒤의 c도 받아야 한다 — 예외가 루프를 깨고 나가면 안 된다.
    assert len(c.sent) == 1

    # 제거됐으므로 다음 브로드캐스트에서는 재시도하지 않는다.
    await manager.broadcast({"type": "SYSTEM_PING"})
    assert len(a.sent) == 2


@pytest.mark.asyncio
async def test_broadcast_with_unserializable_payload_keeps_connections():
    """직렬화 불가 payload는 로그만 남기고 커넥션을 하나도 끊지 않습니다.

    직렬화 실패는 payload를 만든 쪽의 버그이지 커넥션의 문제가 아니다. 루프 안에서
    처리하면 모든 커넥션의 send가 실패해 위 제거 로직이 멀쩡한 소켓을 전부 걷어낸다.

    예외를 올리지 않는 것은 기존 동작과 같다 — 호출처(scheduler)는 브로드캐스트 실패로
    감시 루프가 멈추지 않기를 기대한다.
    """
    manager = ConnectionManager()
    a, b = FakeWebSocket("a"), FakeWebSocket("b")
    manager.active_connections.extend([a, b])

    await manager.broadcast({"type": "SYSTEM_PING", "bad": object()})

    assert manager.active_connections == [a, b]
    assert a.sent == [] and b.sent == []
