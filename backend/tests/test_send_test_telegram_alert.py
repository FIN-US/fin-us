"""시연용 테스트 알림 스크립트(backend/scripts/send_test_telegram_alert.py) 테스트.

스크립트가 실제 알림과 같은 렌더링을 타는지, 기본 동작이 보내지 않는지를 본다.
"""

import asyncio

from backend.scripts import send_test_telegram_alert as script
from backend.telegram.notifier import TelegramNotifier
from backend.telegram.presentation import SIGNAL_SCORE_LABEL, TERM_FOOTNOTE_MARK


class _RecordingNotifier(TelegramNotifier):
    """실제 notifier에서 HTTP 한 지점만 막고 나간 본문을 모은다."""

    def __init__(self) -> None:
        super().__init__("token", "123")
        self.posted: list[str] = []

    async def _post_message(self, text, **kwargs):  # type: ignore[override]
        self.posted.append(text)


def _run(argv: list[str], capsys) -> tuple[int, str, _RecordingNotifier]:
    notifier = _RecordingNotifier()
    code = asyncio.run(script.main(argv, notifier=notifier))
    return code, capsys.readouterr().out, notifier


def test_default_is_dry_run_and_renders_real_alert_fields(capsys):
    code, out, notifier = _run([], capsys)

    assert code == 0
    # 기본 동작은 아무것도 보내지 않는다.
    assert notifier.posted == []
    assert "skipped(dry-run" in out
    # 실제 알림의 모양: 긴급 배너, 영향도 줄, 한국어 필드.
    assert "🚨 긴급 알림" in out
    assert "삼성전자 / 공시" in out
    assert f"- {SIGNAL_SCORE_LABEL} -2 (3분기 영업이익 시장 예상치 하회)" in out
    assert "- 판단: 보유 유지 (확신도 0.82)" in out
    assert "- 이유: " in out
    assert "- 긴급도: 높음, " in out
    assert "TEST-" not in out
    # 기본 수준은 초보 — 용어 각주가 붙는다.
    assert f"{TERM_FOOTNOTE_MARK} 영업이익:" in out


def test_test_marker_is_always_in_summary_line(capsys):
    _, out, _ = _run(["--summary", ""], capsys)

    assert f"- 요약: {script.TEST_MARKER}" in out


def test_intermediate_level_drops_term_footnotes(capsys):
    _, out, _ = _run(["--level", "중급"], capsys)

    assert f"- {SIGNAL_SCORE_LABEL} -2" in out
    assert TERM_FOOTNOTE_MARK not in out


def test_arguments_flow_into_rendered_fields(capsys):
    _, out, _ = _run(
        [
            "--stock", "SK하이닉스",
            "--source", "news",
            "--score", "3",
            "--signal-reason", "HBM 공급 계약 보도",
            "--decision", "buy",
            "--urgency", "normal",
        ],
        capsys,
    )

    assert "🔔 알림" in out
    assert "SK하이닉스 / 뉴스" in out
    assert f"- {SIGNAL_SCORE_LABEL} +3 (HBM 공급 계약 보도)" in out
    assert "- 판단: 매수 (확신도 0.82)" in out
    assert "- 긴급도: 보통, " in out


def test_dry_run_flag_wins_over_send(capsys):
    _, _, notifier = _run(["--send", "--dry-run"], capsys)

    assert notifier.posted == []


def test_send_posts_exactly_what_dry_run_printed(capsys):
    """dry-run 미리보기가 실제 전송 본문과 한 글자도 다르지 않아야 사진이 실물을 보여준다."""
    _, dry_out, _ = _run(["--urgency", "normal"], capsys)
    code, _, notifier = _run(["--urgency", "normal", "--send"], capsys)

    assert code == 0
    assert len(notifier.posted) == 1
    assert notifier.posted[0] in dry_out
