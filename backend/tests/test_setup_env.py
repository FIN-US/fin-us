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


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


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

    assert result.backup_path == tmp_path / ".env.backup.20260701T120000"
    assert result.backup_path.read_text(encoding="utf-8") == """OPENAI_API_KEY=sk-existing
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
