import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.telegram_notifier import telegram_notifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a manual urgent Telegram alert through the backend notifier.",
    )
    parser.add_argument("--stock", default="TEST-삼성전자")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--summary", default="Docker Compose Telegram 테스트 메시지입니다.")
    parser.add_argument("--reason", default="긴급 알림 전송 경로 테스트")
    parser.add_argument("--urgency-reason", default="수동 테스트")
    parser.add_argument("--dry-run", action="store_true", help="Print the message without sending it.")
    return parser


async def main() -> int:
    args = build_parser().parse_args()
    analysis_data = {
        "summary": args.summary,
        "details": {
            "decision": "HOLD",
            "confidence_score": 0.82,
            "reason": args.reason,
        },
        "urgency": "critical",
        "urgency_reason": args.urgency_reason,
        "telegram_alert": True,
    }

    print("enabled=", telegram_notifier.enabled)
    message = telegram_notifier.format_analysis_alert(
        stock=args.stock,
        source=args.source,
        analysis_data=analysis_data,
    )
    print("message=")
    print(message)

    if args.dry_run:
        print("sent= skipped(dry-run)")
        return 0

    sent = await telegram_notifier.send_analysis_alert(args.stock, args.source, analysis_data)
    print("sent=", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
