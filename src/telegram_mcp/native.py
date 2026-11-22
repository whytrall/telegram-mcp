from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import platform
import sys
import threading
from pathlib import Path
from typing import Any, Protocol


class Transport(Protocol):
    def send(self, request: dict[str, Any]) -> None: ...
    def receive(self, timeout: float) -> dict[str, Any] | None: ...
    def release(self) -> None: ...


class NativeTDLib:
    """One client and one receiver per process, using TDLib's current C ABI."""

    _owner = threading.Lock()

    def __init__(self, library_path: str | None = None) -> None:
        if not self._owner.acquire(blocking=False):
            raise RuntimeError("Only one native TDLib client may run in this process.")
        self._released = False
        self.path = library_path or "<automatic discovery>"
        try:
            self.path = library_path or self.find_library()
            self.lib = ctypes.CDLL(self.path)
            self.lib.td_create_client_id.argtypes = []
            self.lib.td_create_client_id.restype = ctypes.c_int
            self.lib.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
            self.lib.td_send.restype = None
            self.lib.td_receive.argtypes = [ctypes.c_double]
            self.lib.td_receive.restype = ctypes.c_char_p
            self.lib.td_execute.argtypes = [ctypes.c_char_p]
            self.lib.td_execute.restype = ctypes.c_char_p
            # Synchronous execution occurs before the receive thread starts: TDLib
            # invalidates receive/execute pointers on subsequent calls.
            self.lib.td_execute(b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}')
            self.client_id = self.lib.td_create_client_id()
        except (OSError, AttributeError) as exc:
            self.release()
            raise RuntimeError(
                f"Unable to load a compatible libtdjson at {self.path}. "
                "The bundled library requires macOS 13 or newer. For other platforms, "
                "build TDLib with scripts/build_tdlib.sh and set TDLIB_LIBRARY_PATH."
            ) from exc
        except BaseException:
            self.release()
            raise

    @staticmethod
    def bundled_library() -> str | None:
        if sys.platform == "darwin" and platform.machine() in {"arm64", "x86_64"}:
            candidate = Path(__file__).parent / "_vendor" / "macos" / "libtdjson.dylib"
            if candidate.is_file():
                return os.fspath(candidate)
        return None

    @staticmethod
    def find_library() -> str:
        bundled = NativeTDLib.bundled_library()
        if bundled:
            return bundled
        found = ctypes.util.find_library("tdjson")
        if found:
            return found
        for directory in ("/opt/homebrew/lib", "/usr/local/lib", "/usr/lib"):
            for name in ("libtdjson.dylib", "libtdjson.so", "tdjson.dll"):
                candidate = Path(directory) / name
                if candidate.is_file():
                    return os.fspath(candidate)
        raise RuntimeError("TDLib is missing. Set TDLIB_LIBRARY_PATH; see README.md.")

    def send(self, request: dict[str, Any]) -> None:
        self.lib.td_send(self.client_id, json.dumps(request, ensure_ascii=False).encode("utf-8"))

    def receive(self, timeout: float) -> dict[str, Any] | None:
        raw = self.lib.td_receive(timeout)
        if raw is None:
            return None
        # c_char_p copies the native bytes before the next td_receive call.
        event = json.loads(raw)
        return event if event.get("@client_id") == self.client_id else None

    def release(self) -> None:
        # The caller must have observed authorizationStateClosed before release.
        if not self._released:
            self._released = True
            self._owner.release()
