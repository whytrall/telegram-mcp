import json

import pytest

from telegram_mcp.cli import main


def test_explicit_env_file_loads_credentials_without_printing_them(tmp_path, monkeypatch, capsys):
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TDLIB_LIBRARY_PATH"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "private.env"
    env_file.write_text(
        'TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH="private-hash"\n'
        'TDLIB_LIBRARY_PATH="/some/path/libtdjson.dylib"\n'
    )
    monkeypatch.setattr("sys.argv", ["telegram-mcp", "--env-file", str(env_file), "doctor"])
    # load_dotenv mutates os.environ; arrange restoration through monkeypatch.
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TDLIB_LIBRARY_PATH"):
        monkeypatch.setenv(name, "temporary")
        monkeypatch.delenv(name)
    main()
    output = capsys.readouterr()
    assert json.loads(output.out)["credentials_configured"] is True
    assert "private-hash" not in output.out + output.err


def test_missing_env_file_has_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["telegram-mcp", "--env-file", str(tmp_path / "missing.env"), "doctor"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "--env-file does not exist" in capsys.readouterr().err
