import ctypes.util

import pytest

from telegram_mcp import native
from telegram_mcp.native import NativeTDLib


@pytest.mark.parametrize("architecture", ["arm64", "x86_64"])
def test_macos_prefers_bundled_library_over_system_installation(
    tmp_path, monkeypatch, architecture
):
    package = tmp_path / "telegram_mcp"
    bundled = package / "_vendor" / "macos" / "libtdjson.dylib"
    bundled.parent.mkdir(parents=True)
    bundled.touch()
    monkeypatch.setattr(native, "__file__", str(package / "native.py"))
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native.platform, "machine", lambda: architecture)
    monkeypatch.setattr(ctypes.util, "find_library", lambda _: "/other/libtdjson.dylib")
    assert NativeTDLib.find_library() == str(bundled)


def test_other_platforms_use_system_library(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(ctypes.util, "find_library", lambda _: "libtdjson.so")
    assert NativeTDLib.find_library() == "libtdjson.so"


def test_explicit_library_override_is_not_silently_replaced(tmp_path):
    missing = str(tmp_path / "custom-libtdjson.dylib")
    with pytest.raises(RuntimeError, match="custom-libtdjson"):
        NativeTDLib(missing)
    # A failed load must release native ownership so a corrected path can work.
    assert NativeTDLib._owner.acquire(blocking=False)
    NativeTDLib._owner.release()
