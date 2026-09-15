from pathlib import Path

import pytest

from backend.scripts import setup_env


EXAMPLE_ENV = """# LLM
OPENAI_API_KEY=your_openai_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# KIS
KIS_ACCOUNT_NO=1234567801
KIS_ORDER_ENV=demo
KIS_REAL_ORDER_ENABLED=false

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
VISUALIZATION_URL=http://100.x.y.z:8080/
"""


# 실제 .env.example처럼 FINUS_API_KEY 줄을 빈 값으로 둔 예시.
API_KEY_EXAMPLE_ENV = EXAMPLE_ENV + """
# Security
FINUS_API_KEY=
"""


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def answer(*values: str):
    replies = iter(values)
    return lambda _prompt: next(replies)


def written_api_key(root: Path) -> str | None:
    return setup_env.parse_env_values((root / ".env").read_text(encoding="utf-8")).get("FINUS_API_KEY")


def test_write_env_file_creates_env_from_example(tmp_path):
    example_path = write(tmp_path / ".env.example", EXAMPLE_ENV)
    env_path = tmp_path / ".env"

    result = setup_env.write_env_file(
        example_path=example_path,
        env_path=env_path,
        updates={"OPENAI_API_KEY": "sk-live", "KIS_ACCOUNT_NO": "8765432101"},
        timestamp="20260701T120000",
    )

    assert result.backup_path is None
    assert env_path.read_text(encoding="utf-8") == """# LLM
OPENAI_API_KEY=sk-live
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# KIS
KIS_ACCOUNT_NO=8765432101
KIS_ORDER_ENV=demo
KIS_REAL_ORDER_ENABLED=false

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
VISUALIZATION_URL=http://100.x.y.z:8080/
"""


def test_write_env_file_preserves_existing_real_values_and_custom_keys(tmp_path):
    example_path = write(tmp_path / ".env.example", EXAMPLE_ENV)
    env_path = write(
        tmp_path / ".env",
        """OPENAI_API_KEY=sk-existing
KIS_ACCOUNT_NO=1111222201
CUSTOM_FEATURE_FLAG=enabled
""",
    )

    result = setup_env.write_env_file(
        example_path=example_path,
        env_path=env_path,
        updates={"ANTHROPIC_API_KEY": "anthropic-live"},
        timestamp="20260701T120000",
    )

    backup_path = result.backup_path
    # 백업 경로는 Optional이다 — 없으면 아래 read_text가 AttributeError로 죽으며
    # "백업을 안 만들었다"는 실패가 엉뚱한 예외로 보고된다. 먼저 존재를 고정한다.
    assert backup_path is not None
    assert backup_path == tmp_path / ".env.backup.20260701T120000"
    assert backup_path.read_text(encoding="utf-8") == """OPENAI_API_KEY=sk-existing
KIS_ACCOUNT_NO=1111222201
CUSTOM_FEATURE_FLAG=enabled
"""
    rendered = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-existing" in rendered
    assert "ANTHROPIC_API_KEY=anthropic-live" in rendered
    assert "KIS_ACCOUNT_NO=1111222201" in rendered
    assert "# User-added settings" in rendered
    assert "CUSTOM_FEATURE_FLAG=enabled" in rendered


def test_validate_settings_requires_at_least_one_llm_key():
    with pytest.raises(setup_env.ValidationError, match="OPENAI_API_KEY 또는 ANTHROPIC_API_KEY"):
        setup_env.validate_settings(
            {"OPENAI_API_KEY": "your_openai_api_key_here", "ANTHROPIC_API_KEY": ""}
        )


def test_validate_settings_rejects_invalid_urls_and_kis_order_env():
    with pytest.raises(setup_env.ValidationError, match="VISUALIZATION_URL"):
        setup_env.validate_settings({"OPENAI_API_KEY": "sk-live", "VISUALIZATION_URL": "localhost:8080"})

    with pytest.raises(setup_env.ValidationError, match="KIS_ORDER_ENV"):
        setup_env.validate_settings({"OPENAI_API_KEY": "sk-live", "KIS_ORDER_ENV": "paper"})


def test_real_order_enablement_requires_confirmation_phrase():
    with pytest.raises(setup_env.ValidationError, match="실계좌 주문"):
        setup_env.validate_settings(
            {
                "OPENAI_API_KEY": "sk-live",
                "KIS_ORDER_ENV": "real",
                "KIS_REAL_ORDER_ENABLED": "true",
            },
            real_order_confirmation="",
        )

    setup_env.validate_settings(
        {
            "OPENAI_API_KEY": "sk-live",
            "KIS_ORDER_ENV": "real",
            "KIS_REAL_ORDER_ENABLED": "true",
        },
        real_order_confirmation=setup_env.REAL_ORDER_CONFIRMATION,
    )


def test_mask_value_hides_secrets_but_leaves_non_secret_values_visible():
    assert setup_env.mask_value("OPENAI_API_KEY", "sk-live-secret") == "********cret"
    assert setup_env.mask_value("KIS_ACCOUNT_NO", "1234567801") == "1234567801"


def test_run_setup_prompts_for_basic_key_and_writes_env(tmp_path):
    write(tmp_path / ".env.example", EXAMPLE_ENV)
    prompts: list[str] = []
    answers = iter(["sk-live", "", "n", "n", "n", "n"])
    messages: list[str] = []

    result = setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=lambda prompt: prompts.append(prompt) or next(answers),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    assert result.backup_path is None
    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith(
        """# LLM
OPENAI_API_KEY=sk-live
ANTHROPIC_API_KEY=your_anthropic_api_key_here
"""
    )
    assert any("기본 AI 설정" in message for message in messages)
    assert any("다음 단계" in message for message in messages)
    assert any("OpenAI API 키" in prompt for prompt in prompts)


def test_run_setup_uses_non_developer_friendly_copy(tmp_path):
    write(tmp_path / ".env.example", EXAMPLE_ENV)
    prompts: list[str] = []
    answers = iter(["sk-live", "n", "n", "n", "n"])
    messages: list[str] = []

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=lambda prompt: prompts.append(prompt) or next(answers),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    joined_messages = "\n".join(messages)
    joined_prompts = "\n".join(prompts)

    assert "Fin-Us 첫 실행 설정" in joined_messages
    assert "모르는 항목은 Enter" in joined_messages
    assert "OpenAI API 키가 있으면 입력하세요" in joined_prompts
    assert "뉴스/공시 데이터를 사용할까요" in joined_prompts
    assert "계좌 조회와 매매 기능을 설정할까요" in joined_prompts
    assert "설정된 기능" in joined_messages
    assert "AI 분석" in joined_messages
    assert "로컬 Ollama 모델" not in joined_messages
    assert "다음 단계" in joined_messages


def test_skipped_optional_defaults_are_not_reported_as_enabled(tmp_path):
    write(
        tmp_path / ".env.example",
        EXAMPLE_ENV
        + """
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_MODEL=gemma4:e4b
OLLAMA_API_KEY=ollama
""",
    )
    answers = iter(["sk-live", "n", "n", "n", "n"])
    messages: list[str] = []

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    assert "로컬 Ollama 모델" not in "\n".join(messages)


def test_run_setup_keeps_existing_real_value_by_default(tmp_path):
    write(tmp_path / ".env.example", EXAMPLE_ENV)
    write(tmp_path / ".env", "OPENAI_API_KEY=sk-existing\n")
    answers = iter(["", "n", "n", "n", "n"])

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _message: None,
        timestamp="20260701T120000",
    )

    assert "OPENAI_API_KEY=sk-existing" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_existing_secret_prompt_uses_readable_label(tmp_path):
    write(tmp_path / ".env.example", EXAMPLE_ENV)
    write(tmp_path / ".env", "OPENAI_API_KEY=sk-existing\n")
    prompts: list[str] = []
    answers = iter(["", "n", "n", "n", "n"])

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=lambda prompt: prompts.append(prompt) or next(answers),
        output_fn=lambda _message: None,
        timestamp="20260701T120000",
    )

    assert "기존 OpenAI API 키가 설정되어 있습니다" in prompts[0]
    assert "OPENAI_API_KEY" not in prompts[0]


def test_new_install_starts_with_api_auth_on(tmp_path):
    """`.env`가 없는 새 설치는 난수 키가 채워져 인증이 켜진 채 시작합니다 (#266).

    잡는 mutation: 키 생성을 건너뛰는 회귀 — FINUS_API_KEY가 예시 그대로 빈 값으로 남는다.
    """
    from backend.main import unsafe_key_characters

    write(tmp_path / ".env.example", API_KEY_EXAMPLE_ENV)
    messages: list[str] = []

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=answer("sk-live", "n", "n", "n", "n"),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    key = written_api_key(tmp_path)
    assert key is not None
    assert not setup_env.is_placeholder(key)
    assert len(key) >= 32
    # 이 값은 nginx 설정에 치환된 뒤 쿠키로 나간다. backend가 경고할 문자가 섞이면 새 설치의
    # 대시보드가 첫 화면부터 401이다 — 판정은 backend의 함수를 그대로 쓴다.
    assert unsafe_key_characters(key) == []
    joined = "\n".join(messages)
    assert "API 인증" in joined
    assert "켜짐" in joined
    # 키 전체를 터미널에 찍지 않는다(다른 비밀값의 mask_value와 같은 규칙).
    assert key not in joined


def test_generated_api_key_differs_per_install(tmp_path):
    """잡는 mutation: 난수 대신 고정 문자열을 넣는 회귀 — 모든 설치가 같은 키를 갖는다."""
    keys = set()
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        write(root / ".env.example", API_KEY_EXAMPLE_ENV)
        setup_env.run_setup(
            root_dir=root,
            input_fn=answer("sk-live", "n", "n", "n", "n"),
            output_fn=lambda _message: None,
            timestamp="20260701T120000",
        )
        keys.add(written_api_key(root))

    assert len(keys) == 2


@pytest.mark.parametrize(
    "existing_env",
    [
        "OPENAI_API_KEY=sk-existing\nFINUS_API_KEY=\n",
        # 2단계 이전의 .env — 키 줄 자체가 없다.
        "OPENAI_API_KEY=sk-existing\n",
    ],
)
def test_existing_env_is_not_switched_on_by_rerunning_setup(tmp_path, existing_env):
    """이미 `.env`가 있는 배포는 키가 비어 있어도 채우지 않습니다.

    운영자가 모르는 사이에 인증이 켜지면 헤더 없이 부르던 호출이 이유 모를 401이 된다.
    잡는 mutation: 새 설치 판정(`.env` 존재 여부)을 빼는 회귀.
    """
    write(tmp_path / ".env.example", API_KEY_EXAMPLE_ENV)
    write(tmp_path / ".env", existing_env)
    messages: list[str] = []

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=answer("", "n", "n", "n", "n"),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    assert written_api_key(tmp_path) == ""
    assert "꺼짐" in "\n".join(messages)


def test_rerunning_setup_keeps_the_generated_api_key(tmp_path):
    """새 설치 뒤 설정을 다시 돌려도 키가 바뀌지 않습니다.

    바뀌면 backend·frontend 컨테이너를 다시 만들기 전까지 대시보드가 낡은 키를 보내고,
    X-API-Key를 적어 둔 호출도 전부 끊긴다.
    """
    write(tmp_path / ".env.example", API_KEY_EXAMPLE_ENV)
    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=answer("sk-live", "n", "n", "n", "n"),
        output_fn=lambda _message: None,
        timestamp="20260701T120000",
    )
    first = written_api_key(tmp_path)

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=answer("", "n", "n", "n", "n"),
        output_fn=lambda _message: None,
        timestamp="20260701T120001",
    )

    assert written_api_key(tmp_path) == first


def test_api_auth_is_not_reported_on_when_example_has_no_key_line(tmp_path):
    """예시 파일에 키 줄이 없으면 켜졌다고 알리지 않습니다.

    render_env는 예시에 있는 키만 쓰므로, 생성만 하고 보고하면 파일에는 없는 키를 켰다고
    말하게 된다. 잡는 mutation: 예시에 키 줄이 있는지 보는 조건을 빼는 회귀.
    """
    write(tmp_path / ".env.example", EXAMPLE_ENV)
    messages: list[str] = []

    setup_env.run_setup(
        root_dir=tmp_path,
        input_fn=answer("sk-live", "n", "n", "n", "n"),
        output_fn=messages.append,
        timestamp="20260701T120000",
    )

    assert written_api_key(tmp_path) is None
    assert "켜짐" not in "\n".join(messages)


def test_shell_wrapper_invokes_python_setup_script_through_backend_uv_project():
    wrapper = Path("scripts/setup_env.sh").read_text(encoding="utf-8")

    assert "uv run --project" in wrapper
    assert "backend" in wrapper
    assert "backend/scripts/setup_env.py" in wrapper


def test_help_text_is_korean_and_user_friendly():
    help_text = setup_env.build_parser().format_help()

    assert "Fin-Us 첫 실행 설정" in help_text
    assert "설정할 프로젝트 폴더" in help_text
    assert "도움말을 보여주고 종료합니다" in help_text


def test_readme_and_existing_scripts_point_to_setup_env_command():
    readme = Path("README.md").read_text(encoding="utf-8")
    check_env = Path("scripts/check_env.sh").read_text(encoding="utf-8")
    compose_up = Path("scripts/_compose_up.sh").read_text(encoding="utf-8")

    assert "bash scripts/setup_env.sh" in readme
    assert "bash scripts/setup_env.sh" in check_env
    assert "scripts/setup_env.sh" in compose_up
