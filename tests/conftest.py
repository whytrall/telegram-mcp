import pytest

from telegram_mcp.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(api_id=12345, api_hash="test-api-hash", data_dir=tmp_path / "session")
