import pytest

from telegram_mcp.config import Config
from telegram_mcp.telegram import clean


@pytest.mark.parametrize("timeout", ["nan", "inf", "-1", "0", "nope"])
def test_invalid_timeout(monkeypatch, timeout):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_REQUEST_TIMEOUT", timeout)
    with pytest.raises(ValueError, match="TELEGRAM_REQUEST_TIMEOUT"):
        Config.from_env()


def test_secret_configuration_not_in_repr(config):
    assert config.api_hash not in repr(config)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "nope"])
def test_invalid_idle_timeout(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_IDLE_TIMEOUT", value)
    with pytest.raises(ValueError, match="TELEGRAM_IDLE_TIMEOUT"):
        Config.from_env()


def test_idle_timeout_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_IDLE_TIMEOUT", "0")
    assert Config.from_env().idle_timeout == 0


def test_transport_metadata_and_inline_images_removed():
    assert clean(
        {"@extra": "secret", "content": {"minithumbnail": {"data": "large"}, "text": "hi"}}
    ) == {
        "content": {"text": "hi"},
    }
