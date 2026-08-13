"""backend/telegram_commands.py 구조 불변식 테스트 (#249).

개별 사이트를 하나씩 회귀 테스트로 고정하는 방식은 "지금 있는" 사이트만 지킨다 —
내일 누군가 새 `except Exception`을 재전파 없이 추가해도 아무 테스트도 못 잡는다
(리뷰에서 실측: 13곳 중 개별 제거로 검출된 곳은 1곳뿐이었다). 이 파일은 소스를
정적으로 파싱해 "TelegramCommandHandler의 모든 `except Exception`은
`except TelegramSendError: raise`를 먼저 두거나 이유와 함께 allowlist에 있어야
한다"는 불변식을 한 번에, 그리고 앞으로도 계속 강제한다.

allowlist에 있는 지점은 다음 중 하나를 만족해 개별 확인했다:
- try 본문이 텔레그램 전송(notifier.send_text 경유, 즉 _send_text_or_raise)을
  호출하지 않아 TelegramSendError가 애초에 발생할 수 없다.
- except 핸들러 자체가 로그만 남기고 사용자에게 재전송하지 않는다(다음 재시도가
  스스로 복구하거나, 실패해도 안전한 best-effort 부가 작업이다).
새 항목을 allowlist에 추가하려면 이 표를 수정해야 하므로 리뷰 대상이 된다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TELEGRAM_COMMANDS_PATH = (
    Path(__file__).resolve().parents[1] / "telegram_commands.py"
)

# (method_name, method 안에서 몇 번째 'except Exception'인가[0-based, 소스 줄 순서]) → 이유.
_ALLOWLIST: dict[tuple[str, int], str] = {
    ("_handle_order_callback", 0): (
        "try 본문이 pending_orders.get()만 호출해 TelegramSendError가 발생할 수 없다. "
        "except도 answer_callback_query로 응답하지 send_text_or_raise로 재전송하지 않는다."
    ),
    ("_handle_watch", 0): (
        "관심종목별 시세 조회 실패를 목록 한 줄('조회 실패')로 흡수한다. try 본문이 "
        "mcp_runner만 호출해 TelegramSendError가 발생할 수 없고, except도 텔레그램 API를 "
        "호출하지 않는다."
    ),
    ("_deliver_order_prompt", 0): (
        "확인 프롬프트 전송 '성공' 이후 prompt_delivered 상태 갱신(pending_orders.set) "
        "실패를 로그만 남긴다. try 본문이 저장소 호출만 하므로 TelegramSendError가 발생할 "
        "수 없고, 실패해도 사용자에게 재전송하지 않는다(다음 재시도가 다시 시도한다)."
    ),
    ("_handle_confirm", 1): (
        "settled 레코드(체결 결과를 이미 전송한 뒤) 정리(delete) 실패를 로그만 남긴다. "
        "try 본문이 저장소 삭제만 하므로 TelegramSendError가 발생할 수 없다."
    ),
    ("_handle_confirm", 3): (
        "403 가드 실패 후 대기 주문 복원(set_if_absent) 실패를 로그만 남긴다. try 본문이 "
        "저장소 호출만 하므로 TelegramSendError가 발생할 수 없다."
    ),
    ("_handle_confirm", 4): (
        "거래 이력 기록(trade_recorder.record, DB 호출) 실패를 로그만 남기고 경고 문구로만 "
        "반영한다. 텔레그램 API를 호출하지 않는다."
    ),
    ("_handle_confirm", 5): (
        "체결 결과(settled) 저장 실패를 로그만 남긴다. try 본문이 저장소 호출만 하므로 "
        "TelegramSendError가 발생할 수 없다."
    ),
    ("_handle_confirm", 6): (
        "체결 결과 전송 성공 후 settled 레코드 정리(delete) 실패를 로그만 남긴다. try 본문이 "
        "저장소 삭제만 하므로 TelegramSendError가 발생할 수 없다."
    ),
}


def _is_bare_exception_handler(handler: ast.ExceptHandler) -> bool:
    """`except Exception` / `except Exception as exc` (다른 이름 없이 정확히 Exception)."""
    return isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def _is_pure_telegram_send_error_reraise(handler: ast.ExceptHandler) -> bool:
    """`except TelegramSendError:` 바로 뒤에 `raise` 한 줄만 있는 순수 재전파인가.

    중간에 로그 등 다른 문장이 있어도(주석은 AST에 안 남으므로 무방) `raise`가
    유일한 statement면 통과시킨다 — 이 저장소의 실제 코드가 `except
    TelegramSendError:\n    raise` 사이에 설명 주석만 두는 패턴이기 때문이다.
    """
    if not (isinstance(handler.type, ast.Name) and handler.type.id == "TelegramSendError"):
        return False
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Raise) and handler.body[0].exc is None


def bare_except_exception_sites(source: str, class_name: str) -> dict[str, list[tuple[int, bool]]]:
    """class_name 클래스의 각 메서드 안에서 'except Exception' 핸들러를 소스 줄 순서로 나열한다.

    반환값: {method_name: [(lineno, guarded), ...]} — guarded는 바로 앞 핸들러가
    순수 TelegramSendError 재전파인지 여부.
    """
    tree = ast.parse(source)
    class_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )

    by_method: dict[str, list[tuple[int, bool]]] = {}
    for method in class_node.body:
        if not isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        found: list[tuple[int, bool]] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Try):
                continue
            handlers = node.handlers
            for i, handler in enumerate(handlers):
                if not _is_bare_exception_handler(handler):
                    continue
                guarded = i > 0 and _is_pure_telegram_send_error_reraise(handlers[i - 1])
                found.append((handler.lineno, guarded))
        if found:
            found.sort(key=lambda item: item[0])
            by_method[method.name] = found
    return by_method


def test_handler_except_exception_reraises_telegram_send_error_or_is_allowlisted():
    """#249: TelegramCommandHandler의 모든 'except Exception'은 TelegramSendError를
    먼저 재전파하거나, 왜 그럴 필요가 없는지 _ALLOWLIST에 남겨야 한다.

    개별 사이트 회귀 테스트(test_telegram_commands.py)보다 이 테스트가 강한 이유:
    지금 있는 13곳뿐 아니라 앞으로 추가되는 'except Exception'도 자동으로 걸린다.
    """
    source = _TELEGRAM_COMMANDS_PATH.read_text(encoding="utf-8")
    by_method = bare_except_exception_sites(source, "TelegramCommandHandler")

    violations = []
    for method_name, handlers in by_method.items():
        for index, (lineno, guarded) in enumerate(handlers):
            if guarded:
                continue
            if (method_name, index) in _ALLOWLIST:
                continue
            violations.append(f"{method_name} (#{index}, line {lineno})")

    assert not violations, (
        "TelegramCommandHandler의 except Exception은 TelegramSendError를 먼저 "
        "재전파하거나(`except TelegramSendError:\\n    raise`) 이유와 함께 "
        "_ALLOWLIST에 등록되어야 한다 (#249). 위반: " + ", ".join(violations)
    )


def test_allowlist_entries_still_exist_in_source():
    """allowlist가 이미 삭제/이동된 사이트를 가리키는 죽은 항목으로 썩지 않게 한다.

    allowlist의 (method, index)가 실제 소스에서 더 이상 존재하지 않으면(리팩터로
    사이트가 줄어들었는데 표를 안 지운 경우) 여기서 알려준다.
    """
    source = _TELEGRAM_COMMANDS_PATH.read_text(encoding="utf-8")
    by_method = bare_except_exception_sites(source, "TelegramCommandHandler")

    stale = [
        key
        for key in _ALLOWLIST
        if key[1] >= len(by_method.get(key[0], []))
    ]
    assert not stale, f"_ALLOWLIST에 더 이상 존재하지 않는 항목: {stale}"


def test_bare_except_exception_sites_helper_detects_unguarded_handler():
    """구조 검사 자체가 공허하지 않음을 직접 확인한다: 재전파 없는 새
    'except Exception'을 섞은 가짜 소스에서 unguarded로 검출되는지 검사한다.

    이게 없으면 위 두 테스트가 "우연히 항상 통과하는" 텅 빈 로직이어도 알 수 없다.
    """
    fake_source = (
        "class TelegramCommandHandler:\n"
        "    async def _guarded(self):\n"
        "        try:\n"
        "            await self.something()\n"
        "        except TelegramSendError:\n"
        "            raise\n"
        "        except Exception as exc:\n"
        "            await self._send_text_or_raise('a')\n"
        "\n"
        "    async def _unguarded(self):\n"
        "        try:\n"
        "            await self.something_else()\n"
        "        except Exception as exc:\n"
        "            await self._send_text_or_raise('b')\n"
    )

    by_method = bare_except_exception_sites(fake_source, "TelegramCommandHandler")

    assert by_method["_guarded"] == [(7, True)]
    assert by_method["_unguarded"] == [(13, False)]
