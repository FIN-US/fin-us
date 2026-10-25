"""시연 영상·실물 사진용 텔레그램 테스트 알림.

실제 감시 알림(scheduler.monitor → TelegramNotifier.send_analysis_alert)과 **같은 렌더링
함수**(TelegramNotifier.render_analysis_alert)를 거친다. 문자열을 따로 조립하지 않으므로
배너(🚨/🔔), "📊 영향도" 줄, 한국어 필드(판단·이유·긴급도), /level에 따른 용어 각주가 실제
알림과 같은 모양으로 나온다.

안전장치
- **기본은 dry-run이다.** 렌더링 결과를 출력만 하고 보내지 않는다. 실제로 보내려면
  ``--send``를 명시해야 하고, ``--dry-run``이 함께 있으면 dry-run이 이긴다.
- 받는 곳은 .env의 TELEGRAM_CHAT_ID 하나뿐이다(실제 알림이 가는 그 채팅). 이 스크립트에는
  대상을 바꾸는 인자가 없고, chat id·토큰은 화면에 찍지 않는다.
- 테스트임을 숨기지 않는다. 본문의 "요약:" 줄 맨 앞에 항상 ``[테스트]``가 붙는다 — 끌 수
  있는 인자는 없다. 요약 줄은 실제 알림에도 있는 필드라 본문 형식은 그대로이고, 목록 맨
  끝이라 사진에서 제목·영향도·판단을 가리지 않는다.

사용법 (레포 루트에서)
    # 기본 예시(삼성전자 / 공시 / 영향도 -2 / 보유 유지 / 높음 / 초보)를 출력만
    uv run --project backend python backend/scripts/send_test_telegram_alert.py

    # 값 바꿔 보기 — 중급이면 용어 각주가 빠진다
    uv run --project backend python backend/scripts/send_test_telegram_alert.py \\
        --stock SK하이닉스 --source news --score 3 \\
        --signal-reason "HBM 대규모 공급 계약 보도" \\
        --decision BUY --urgency critical --level 중급

    # 실제 전송(설정된 TELEGRAM_CHAT_ID로 한 통)
    uv run --project backend python backend/scripts/send_test_telegram_alert.py --send

인자
    --stock          종목명(제목 줄). 기본 삼성전자
    --source         신호 출처 news/disclosure/manual. 기본 disclosure(공시)
    --score          영향도 -3~+3. 기본 -2
    --signal-reason  영향도 근거 한 줄
    --decision       BUY/SELL/HOLD (매수/매도/보유 유지). 기본 HOLD
    --confidence     확신도 0~1. 기본 0.82
    --reason         판단 이유
    --urgency        low/normal/high/critical. 기본 high (high·critical이면 🚨 배너)
    --urgency-reason 긴급도 사유
    --level          초보/중급(beginner/intermediate). 기본 초보 — 용어 각주가 붙는다
    --summary        요약. 앞에 [테스트]가 항상 붙는다
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.telegram.notifier import TelegramNotifier, telegram_notifier
from backend.telegram.presentation import (
    DECISION_LABELS,
    LEVEL_BEGINNER,
    URGENCY_LABELS,
    level_label,
    normalize_level,
)

# 요약 줄 앞에 항상 붙는 표시. 인자로 끌 수 없게 상수로 둔다.
TEST_MARKER = "[테스트]"
SCORE_RANGE = range(-3, 4)


def _level(value: str) -> str:
    level = normalize_level(value)
    if level is None:
        raise argparse.ArgumentTypeError(f"알 수 없는 수준: {value} (초보/중급)")
    return level


def _confidence(value: str) -> float:
    number = float(value)
    if not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("확신도는 0~1 사이여야 합니다")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실제 감시 알림과 같은 렌더링으로 텔레그램 테스트 알림을 만든다(기본 dry-run).",
    )
    parser.add_argument("--stock", default="삼성전자")
    parser.add_argument("--source", default="disclosure")
    parser.add_argument("--score", type=int, choices=SCORE_RANGE, default=-2)
    parser.add_argument("--signal-reason", default="3분기 영업이익 시장 예상치 하회")
    parser.add_argument(
        "--decision", type=str.upper, choices=sorted(DECISION_LABELS), default="HOLD"
    )
    parser.add_argument("--confidence", type=_confidence, default=0.82)
    parser.add_argument(
        "--reason",
        default="실적 부진이 일부 주가에 반영돼 있어 추가 하락 여부를 지켜볼 필요가 있음",
    )
    parser.add_argument(
        "--urgency", type=str.lower, choices=list(URGENCY_LABELS), default="high"
    )
    parser.add_argument("--urgency-reason", default="장중 공시로 주가 변동 가능성 큼")
    parser.add_argument("--level", type=_level, default=LEVEL_BEGINNER)
    parser.add_argument("--summary", default="시연용 예시 알림이며 실제 신호가 아닙니다.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="실제로 보낸다. 없으면 dry-run(출력만).",
    )
    # 예전 사용법(--dry-run)을 그대로 받는다. --send와 함께 오면 dry-run이 이긴다.
    parser.add_argument("--dry-run", action="store_true", help="출력만 한다(기본 동작).")
    return parser


def build_analysis_data(args: argparse.Namespace) -> dict[str, Any]:
    """감시 경로가 notifier에 넘기는 analysis_data와 같은 모양."""
    summary = f"{TEST_MARKER} {args.summary}".strip()
    return {
        "summary": summary,
        "details": {
            "decision": args.decision,
            "confidence_score": args.confidence,
            "reason": args.reason,
        },
        "urgency": args.urgency,
        "urgency_reason": args.urgency_reason,
        # 실제 감시에서 긴급 판정을 받은 분석과 같은 플래그. 배너는 urgency로 갈린다.
        "telegram_alert": True,
        "signal_score": args.score,
        "signal_reason": args.signal_reason,
    }


def render_preview(args: argparse.Namespace, notifier: TelegramNotifier) -> str:
    return notifier.render_analysis_alert(
        stock=args.stock,
        source=args.source,
        analysis_data=build_analysis_data(args),
        level=args.level,
    )


async def main(
    argv: list[str] | None = None,
    notifier: TelegramNotifier = telegram_notifier,
) -> int:
    args = build_parser().parse_args(argv)
    send = args.send and not args.dry_run

    # chat id·토큰은 찍지 않는다. 설정 여부만 보인다.
    print("enabled=", notifier.enabled)
    print("level=", level_label(args.level))
    print("message=")
    print(render_preview(args, notifier))

    if not send:
        print("sent= skipped(dry-run, 보내려면 --send)")
        return 0

    # alert_mode="all": 긴급도를 낮춰 🔔 일반 알림을 찍을 때도 게이트에 막히지 않게 한다.
    # 배너는 이 모드와 무관하게 urgency로 정해진다(render_analysis_alert).
    sent = await notifier.send_analysis_alert(
        args.stock,
        args.source,
        build_analysis_data(args),
        alert_mode="all",
        level=args.level,
    )
    print("sent=", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
