from __future__ import annotations

import argparse
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


REAL_ORDER_CONFIRMATION = "실계좌 주문 위험을 이해했습니다"
USER_ADDED_SECTION = "# User-added settings"
SECRET_KEY_PARTS = ("API_KEY", "API_SECRET", "TOKEN", "SECRET")
URL_KEYS = {"KIS_URL", "VISUALIZATION_URL", "OLLAMA_BASE_URL", "OPENAI_API_BASE_URL", "OPENAI_BASE_URL"}
BOOLEAN_KEYS = {"KIS_REAL_ORDER_ENABLED", "DB_ECHO"}
API_KEY_ENV = "FINUS_API_KEY"
InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

FIELD_LABELS = {
    "OPENAI_API_KEY": "OpenAI API 키",
    "ANTHROPIC_API_KEY": "Anthropic API 키",
    "NAVER_CLIENT_ID": "Naver 검색 API Client ID",
    "NAVER_CLIENT_SECRET": "Naver 검색 API Client Secret",
    "DART_API_KEY": "OpenDART API 키",
    "KIS_API_KEY": "한국투자증권 API 키",
    "KIS_API_SECRET": "한국투자증권 API Secret",
    "KIS_ACCOUNT_NO": "한국투자증권 계좌번호",
    "KIS_URL": "한국투자증권 API 주소",
    "KIS_ORDER_ENV": "투자 환경(demo 또는 real)",
    "KIS_REAL_ORDER_ENABLED": "실계좌 주문 허용 여부(true 또는 false)",
    "TELEGRAM_BOT_TOKEN": "Telegram 봇 토큰",
    "TELEGRAM_CHAT_ID": "Telegram 채팅 ID",
    "VISUALIZATION_URL": "포트폴리오 시각화 URL",
    "OLLAMA_BASE_URL": "Ollama API 주소",
    "OLLAMA_MODEL": "Ollama 모델 이름",
    "OLLAMA_API_KEY": "Ollama API 키",
}

SETUP_GROUPS = (
    (
        "뉴스/공시 데이터",
        "뉴스/공시 데이터를 사용할까요? Naver 뉴스와 OpenDART 공시 조회에 필요합니다.",
        ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "DART_API_KEY"),
    ),
    (
        "계좌 조회와 매매",
        "계좌 조회와 매매 기능을 설정할까요? KIS 계좌 조회, 잔고 확인, 주문 기능에 필요합니다.",
        ("KIS_API_KEY", "KIS_API_SECRET", "KIS_ACCOUNT_NO", "KIS_URL", "KIS_ORDER_ENV", "KIS_REAL_ORDER_ENABLED"),
    ),
    (
        "Telegram 알림과 시각화",
        "Telegram 알림이나 포트폴리오 시각화 링크를 사용할까요?",
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "VISUALIZATION_URL"),
    ),
    (
        "로컬 Ollama 모델",
        "로컬 Ollama 모델을 사용할까요? 로컬 LLM 서버를 따로 실행하는 경우에만 필요합니다.",
        ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY"),
    ),
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
    label = FIELD_LABELS.get(key, key)
    if current and not is_placeholder(current):
        answer = input_fn(f"기존 {label}가 설정되어 있습니다({mask_value(key, current)}). 그대로 사용할까요? [Y/n]: ").strip().lower()
        if answer not in {"n", "no"}:
            return

    entered = input_fn(f"{label}가 있으면 입력하세요. 없거나 나중에 설정하려면 Enter를 누르세요: ").strip()
    if entered:
        updates[key] = entered
        values[key] = entered


def _enabled_capabilities(values: dict[str, str], configured_keys: set[str]) -> list[str]:
    capabilities = []
    if not (is_placeholder(values.get("OPENAI_API_KEY")) and is_placeholder(values.get("ANTHROPIC_API_KEY"))):
        capabilities.append("AI 분석")
    if configured_keys.intersection({"NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "DART_API_KEY"}) and not (
        is_placeholder(values.get("NAVER_CLIENT_ID"))
        or is_placeholder(values.get("NAVER_CLIENT_SECRET"))
        or is_placeholder(values.get("DART_API_KEY"))
    ):
        capabilities.append("뉴스/공시 데이터")
    if configured_keys.intersection({"KIS_API_KEY", "KIS_API_SECRET", "KIS_ACCOUNT_NO"}) and not (
        is_placeholder(values.get("KIS_API_KEY"))
        or is_placeholder(values.get("KIS_API_SECRET"))
        or is_placeholder(values.get("KIS_ACCOUNT_NO"))
    ):
        capabilities.append("계좌 조회와 매매")
    if configured_keys.intersection({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}) and not (
        is_placeholder(values.get("TELEGRAM_BOT_TOKEN")) or is_placeholder(values.get("TELEGRAM_CHAT_ID"))
    ):
        capabilities.append("Telegram 알림")
    if configured_keys.intersection({"OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY"}) and not (
        is_placeholder(values.get("OLLAMA_BASE_URL")) or is_placeholder(values.get("OLLAMA_MODEL"))
    ):
        capabilities.append("로컬 Ollama 모델")
    return capabilities


def collect_interactive_updates(
    values: dict[str, str],
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> tuple[dict[str, str], str]:
    updates: dict[str, str] = {}
    output_fn("Fin-Us 첫 실행 설정을 시작합니다.")
    output_fn("모르는 항목은 Enter로 건너뛸 수 있습니다. 나중에 다시 실행해도 됩니다.")
    output_fn("최소한 OpenAI 또는 Anthropic API 키 중 하나는 필요합니다.")
    output_fn("")
    output_fn("== 기본 AI 설정 ==")
    _prompt_key("OPENAI_API_KEY", values, updates, input_fn)
    if is_placeholder(values.get("OPENAI_API_KEY")):
        _prompt_key("ANTHROPIC_API_KEY", values, updates, input_fn)

    real_order_confirmation = ""
    for title, description, keys in SETUP_GROUPS:
        answer = input_fn(f"{description} [y/N]: ").strip().lower()
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

    existing_values = parse_env_values(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    values = _effective_values(example_path, env_path)
    updates, real_order_confirmation = collect_interactive_updates(
        values,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    validate_settings(values, real_order_confirmation=real_order_confirmation)
    # write_env_file이 .env를 만들므로 새 설치 판정은 반드시 그 전에 한다.
    api_key_generated = _fill_api_key_for_new_install(env_path, values, updates)
    result = write_env_file(
        example_path=example_path,
        env_path=env_path,
        updates=updates,
        timestamp=timestamp or datetime.now().strftime("%Y%m%dT%H%M%S"),
    )

    output_fn(f".env 저장 완료: {result.env_path}")
    if result.backup_path:
        output_fn(f"기존 .env 백업: {result.backup_path}")
    capabilities = _enabled_capabilities(values, set(existing_values) | set(updates))
    output_fn("설정된 기능:")
    output_fn("  " + (", ".join(capabilities) if capabilities else "아직 설정된 선택 기능이 없습니다."))
    _report_api_auth(values, generated=api_key_generated, output_fn=output_fn)
    output_fn("다음 단계:")
    output_fn("  bash scripts/setup_deps.sh")
    output_fn("  bash scripts/run_stack.sh")
    return result


def generate_api_key() -> str:
    """FINUS_API_KEY로 쓸 난수 키를 만듭니다.

    token_urlsafe의 문자 집합은 `A-Z a-z 0-9 - _`뿐이다. 이 값은 nginx 설정 텍스트에 치환된 뒤
    쿠키 값으로 나가는데, 그 두 자리에서 깨지는 문자(backend/main.py의 _UNSAFE_KEY_CHARS)가
    하나도 없다. 길이는 .env.example이 권하는 명령과 같다.
    """
    return secrets.token_urlsafe(32)


def _fill_api_key_for_new_install(env_path: Path, values: dict[str, str], updates: dict[str, str]) -> bool:
    """`.env`가 아직 없는 새 설치에서만 FINUS_API_KEY를 채웁니다 (#266).

    정적 키에는 비어 있지 않은 기본값이라는 것이 없다 — 코드나 .env.example에 적힌 값은
    아무나 아는 키다. 그래서 "처음부터 켜짐"은 설정 파일을 처음 만드는 이 자리에서만 만들 수
    있다.

    이미 `.env`가 있으면 비어 있어도 채우지 않는다. 그 배포는 지금 무인증으로 동작 중이고,
    설정 스크립트를 다시 돌렸다는 이유로 인증이 켜지면 헤더 없이 부르던 호출과 Unity 에디터
    플레이 모드가 이유 모를 401이 된다. 켜는 것은 그 운영자의 명시적 행위로 남긴다.

    예시 파일에 키 줄이 없으면 채우지 않는다. render_env는 예시에 있는 키만 쓰므로, 채웠다고
    보고하면서 실제로는 파일에 없는 상태가 된다.
    """
    if env_path.exists() or API_KEY_ENV not in values:
        return False
    key = generate_api_key()
    updates[API_KEY_ENV] = key
    values[API_KEY_ENV] = key
    return True


def _report_api_auth(values: dict[str, str], *, generated: bool, output_fn: OutputFn) -> None:
    output_fn("API 인증:")
    if generated:
        masked = mask_value(API_KEY_ENV, values[API_KEY_ENV])
        output_fn(f"  켜짐 — 새 설치라 {API_KEY_ENV}에 난수 키를 만들어 넣었습니다({masked}).")
        output_fn("  대시보드(8080)는 그대로 동작합니다. curl 등으로 /api/를 직접 부를 때는 X-API-Key 헤더에 이 값을 실으세요.")
        output_fn(f"  Unity 에디터 플레이 모드는 키를 싣지 못하므로, 에디터로 테스트하는 동안에는 .env의 {API_KEY_ENV}를 비우세요.")
    elif is_placeholder(values.get(API_KEY_ENV)):
        output_fn(f"  꺼짐 — {API_KEY_ENV}가 비어 있어 /api/ 전체가 무인증입니다. 켜려면 값을 채우세요(.env.example 참고).")
    else:
        output_fn("  켜짐")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fin-Us 첫 실행 설정을 도와 루트 .env 파일을 준비합니다.",
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="도움말을 보여주고 종료합니다.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="설정할 프로젝트 폴더입니다. 기본값은 현재 Fin-Us 체크아웃입니다.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        run_setup(root_dir=args.root.resolve())
    except (FileNotFoundError, ValidationError) as exc:
        print(f"오류: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
