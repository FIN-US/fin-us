from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REAL_ORDER_CONFIRMATION = "실계좌 주문 위험을 이해했습니다"
USER_ADDED_SECTION = "# User-added settings"
SECRET_KEY_PARTS = ("API_KEY", "API_SECRET", "TOKEN", "SECRET")
URL_KEYS = {"KIS_URL", "VISUALIZATION_URL", "OLLAMA_BASE_URL", "OPENAI_API_BASE_URL", "OPENAI_BASE_URL"}
BOOLEAN_KEYS = {"KIS_REAL_ORDER_ENABLED", "DB_ECHO"}


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
