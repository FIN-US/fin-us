from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


REAL_ORDER_CONFIRMATION = "실계좌 주문 위험을 이해했습니다"
USER_ADDED_SECTION = "# User-added settings"
SECRET_KEY_PARTS = ("API_KEY", "API_SECRET", "TOKEN", "SECRET")
URL_KEYS = {"KIS_URL", "VISUALIZATION_URL", "OLLAMA_BASE_URL", "OPENAI_API_BASE_URL", "OPENAI_BASE_URL"}
BOOLEAN_KEYS = {"KIS_REAL_ORDER_ENABLED", "DB_ECHO"}
InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

SETUP_GROUPS = (
    ("시장 데이터", ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "DART_API_KEY")),
    (
        "계좌/매매",
        ("KIS_API_KEY", "KIS_API_SECRET", "KIS_ACCOUNT_NO", "KIS_URL", "KIS_ORDER_ENV", "KIS_REAL_ORDER_ENABLED"),
    ),
    ("알림/시각화", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "VISUALIZATION_URL")),
    ("로컬 모델", ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY")),
)


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EnvLine:
    raw: str
    key: str | None = None
    value: str = ""


@dataclass(frozen=True)
class WriteResult:
    env_path: Path
    backup_path: Path | None


def parse_env_lines(text: str) -> list[EnvLine]:
    lines: list[EnvLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            lines.append(EnvLine(raw=raw))
            continue

        key, value = raw.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            lines.append(EnvLine(raw=raw))
            continue
        lines.append(EnvLine(raw=raw, key=normalized_key, value=value.strip()))
    return lines


def parse_env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in parse_env_lines(text):
        if line.key:
            values[line.key] = line.value
    return values


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip()
    return (
        not normalized
        or normalized.startswith("your_")
        or normalized.endswith("_here")
    )


def _render_value(key: str, example_value: str, existing: dict[str, str], updates: dict[str, str]) -> str:
    if key in updates:
        return updates[key]
    existing_value = existing.get(key)
    if existing_value is not None and not is_placeholder(existing_value):
        return existing_value
    return existing_value if existing_value is not None else example_value


def render_env(example_text: str, existing_text: str, updates: dict[str, str]) -> str:
    example_lines = parse_env_lines(example_text)
    existing_values = parse_env_values(existing_text)
    example_keys = {line.key for line in example_lines if line.key}
    rendered: list[str] = []

    for line in example_lines:
        if not line.key:
            rendered.append(line.raw)
            continue
        rendered.append(f"{line.key}={_render_value(line.key, line.value, existing_values, updates)}")

    custom_lines = [
        f"{key}={value}"
        for key, value in existing_values.items()
        if key not in example_keys
    ]
    if custom_lines:
        while rendered and rendered[-1] == "":
            rendered.pop()
        rendered.extend(["", USER_ADDED_SECTION, *custom_lines])

    return "\n".join(rendered) + "\n"


def write_env_file(
    *,
    example_path: Path,
    env_path: Path,
    updates: dict[str, str],
    timestamp: str,
) -> WriteResult:
    example_text = example_path.read_text(encoding="utf-8")
    existing_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    backup_path: Path | None = None

    if env_path.exists():
        backup_path = env_path.with_name(f"{env_path.name}.backup.{timestamp}")
        backup_path.write_text(existing_text, encoding="utf-8")

    env_path.write_text(render_env(example_text, existing_text, updates), encoding="utf-8")
    return WriteResult(env_path=env_path, backup_path=backup_path)


def _effective_values(example_path: Path, env_path: Path) -> dict[str, str]:
    example_values = parse_env_values(example_path.read_text(encoding="utf-8"))
    if not env_path.exists():
        return example_values

    values = dict(example_values)
    for key, value in parse_env_values(env_path.read_text(encoding="utf-8")).items():
        if not is_placeholder(value):
            values[key] = value
    return values


def _prompt_key(
    key: str,
    values: dict[str, str],
    updates: dict[str, str],
    input_fn: InputFn,
) -> None:
    current = values.get(key, "")
    if current and not is_placeholder(current):
        answer = input_fn(f"{key} 기존 값({mask_value(key, current)})을 유지할까요? [Y/n]: ").strip().lower()
        if answer not in {"n", "no"}:
            return

    entered = input_fn(f"{key} 값을 입력하세요(건너뛰려면 Enter): ").strip()
    if entered:
        updates[key] = entered
        values[key] = entered


def collect_interactive_updates(
    values: dict[str, str],
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> tuple[dict[str, str], str]:
    updates: dict[str, str] = {}
    output_fn("== 기본 실행 ==")
    _prompt_key("OPENAI_API_KEY", values, updates, input_fn)
    if is_placeholder(values.get("OPENAI_API_KEY")):
        _prompt_key("ANTHROPIC_API_KEY", values, updates, input_fn)

    real_order_confirmation = ""
    for title, keys in SETUP_GROUPS:
        answer = input_fn(f"{title} 설정을 입력할까요? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            continue
        output_fn(f"== {title} ==")
        for key in keys:
            _prompt_key(key, values, updates, input_fn)

    if _is_true(values.get("KIS_REAL_ORDER_ENABLED")):
        output_fn("실계좌 주문은 실제 주문 제출을 허용할 수 있습니다.")
        real_order_confirmation = input_fn(f"계속하려면 '{REAL_ORDER_CONFIRMATION}'를 입력하세요: ").strip()

    return updates, real_order_confirmation


def run_setup(
    *,
    root_dir: Path,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
    timestamp: str | None = None,
) -> WriteResult:
    example_path = root_dir / ".env.example"
    env_path = root_dir / ".env"
    if not example_path.exists():
        raise FileNotFoundError(f"{example_path} 파일을 찾을 수 없습니다.")

    values = _effective_values(example_path, env_path)
    updates, real_order_confirmation = collect_interactive_updates(
        values,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    validate_settings(values, real_order_confirmation=real_order_confirmation)
    result = write_env_file(
        example_path=example_path,
        env_path=env_path,
        updates=updates,
        timestamp=timestamp or datetime.now().strftime("%Y%m%dT%H%M%S"),
    )

    output_fn(f".env 저장 완료: {result.env_path}")
    if result.backup_path:
        output_fn(f"기존 .env 백업: {result.backup_path}")
    output_fn("다음 명령:")
    output_fn("  bash scripts/setup_deps.sh")
    output_fn("  bash scripts/run_stack.sh")
    return result


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def _is_boolean(value: str) -> bool:
    return value.strip().lower() in {"1", "0", "true", "false", "yes", "no", "y", "n"}


def validate_settings(
    values: dict[str, str],
    *,
    real_order_confirmation: str = "",
) -> None:
    if is_placeholder(values.get("OPENAI_API_KEY")) and is_placeholder(values.get("ANTHROPIC_API_KEY")):
        raise ValidationError("OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 중 하나는 필요합니다.")

    order_env = values.get("KIS_ORDER_ENV", "demo").strip().lower()
    if order_env and order_env not in {"demo", "real"}:
        raise ValidationError("KIS_ORDER_ENV는 demo 또는 real만 사용할 수 있습니다.")

    for key in BOOLEAN_KEYS:
        value = values.get(key)
        if value and not _is_boolean(value):
            raise ValidationError(f"{key}는 true 또는 false 형식이어야 합니다.")

    for key in URL_KEYS:
        value = values.get(key, "").strip()
        if value and not (value.startswith("http://") or value.startswith("https://")):
            raise ValidationError(f"{key}는 http:// 또는 https:// URL이어야 합니다.")

    if _is_true(values.get("KIS_REAL_ORDER_ENABLED")) and real_order_confirmation != REAL_ORDER_CONFIRMATION:
        raise ValidationError("실계좌 주문 활성화에는 확인 문구 입력이 필요합니다.")


def mask_value(key: str, value: str) -> str:
    if not value:
        return ""
    if not any(part in key for part in SECRET_KEY_PARTS):
        return value
    if len(value) <= 4:
        return "****"
    return "********" + value[-4:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the Fin-Us root .env file interactively.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Fin-Us repository root. Defaults to the current checkout root.",
    )
    args = parser.parse_args()
    try:
        run_setup(root_dir=args.root.resolve())
    except (FileNotFoundError, ValidationError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
