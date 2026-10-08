"""Configuration preflight must reject unfilled URL placeholders."""
from unittest.mock import patch

import pytest

from rentbot.config import Config, ConfigurationError


@pytest.fixture
def configured_environment(tmp_path):
    values = {
        "BOT_TOKEN": "123456:" + "A" * 35,
        "ADMIN_IDS": "123456",
        "ADMIN_CHAT_ID": "-1001234567890",
        "CHANNEL_ID": "-1001234567891",
        "CHANNEL_URL": "https://t.me/example_catalog",
        "REQUIRED_CHAT_URL": "https://t.me/example_members",
        "SUPPORT_URL": "https://t.me/example_support",
        "PAYMENT_DETAILS": "Example Bank; Example Recipient",
    }
    # Use an isolated environment and nonexistent file; never read the user's .env.
    with patch.dict("os.environ", values, clear=True):
        yield tmp_path / "missing.env"


def test_complete_configuration_passes_preflight(configured_environment):
    config = Config.from_env(configured_environment)
    assert config.channel_url == "https://t.me/example_catalog"
    assert config.support_url == "https://t.me/example_support"


@pytest.mark.parametrize("key", ["CHANNEL_URL", "REQUIRED_CHAT_URL", "SUPPORT_URL"])
def test_url_placeholder_fails_preflight(configured_environment, monkeypatch, key):
    monkeypatch.setenv(key, "https://t.me/CHANGE_ME_CHANNEL")
    with pytest.raises(ConfigurationError, match=f"Заполните {key} в .env"):
        Config.from_env(configured_environment)
