import importlib.util
import json
import stat
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_clients", Path(__file__).parents[1] / "scripts" / "configure_clients.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def files(tmp_path):
    launcher = tmp_path / "install with spaces" / ".venv" / "bin" / "telegram-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    return launcher, tmp_path / "claude.json", tmp_path / "codex" / "config.toml"


def test_registration_preserves_settings_and_is_repeatable(files):
    launcher, claude_path, codex_path = files
    claude_path.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "other-server"}}})
    )
    codex_path.parent.mkdir()
    codex_path.write_text(
        '# Personal settings\nmodel = "chosen-model"\n'
        '[mcp_servers.other]\ncommand = "other-server"\n'
        '[mcp_servers.telegram]\nurl = "https://old.example/mcp"\n'
    )
    installer.configure(*files)
    claude = json.loads(claude_path.read_text())
    codex = tomllib.loads(codex_path.read_text())
    assert claude["theme"] == "dark" and codex["model"] == "chosen-model"
    assert "# Personal settings" in codex_path.read_text()
    assert claude["mcpServers"]["other"]["command"] == "other-server"
    assert codex["mcp_servers"]["other"]["command"] == "other-server"
    assert claude["mcpServers"]["telegram"] == {
        "type": "stdio",
        "command": str(launcher),
        "args": ["serve"],
    }
    assert codex["mcp_servers"]["telegram"] == {
        "command": str(launcher),
        "args": ["serve"],
        "startup_timeout_sec": 45,
        "tool_timeout_sec": 360,
    }
    previous = claude_path.read_bytes(), codex_path.read_bytes()
    installer.configure(*files)
    assert previous == (claude_path.read_bytes(), codex_path.read_bytes())
    assert stat.S_IMODE(claude_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(codex_path.stat().st_mode) == 0o600


def test_registration_creates_missing_configurations(files):
    installer.configure(*files)
    assert json.loads(files[1].read_text())["mcpServers"]["telegram"]
    assert tomllib.loads(files[2].read_text())["mcp_servers"]["telegram"]


@pytest.mark.parametrize(
    ("claude", "codex"),
    [("{invalid", ""), ("{}", "[invalid"), ('{"mcpServers": []}', ""), ("[]", "")],
)
def test_malformed_configuration_leaves_both_files_untouched(files, claude, codex):
    _, claude_path, codex_path = files
    codex_path.parent.mkdir()
    claude_path.write_text(claude)
    codex_path.write_text(codex)
    with pytest.raises((ValueError, TypeError)):
        installer.configure(*files)
    assert claude_path.read_text() == claude
    assert codex_path.read_text() == codex
