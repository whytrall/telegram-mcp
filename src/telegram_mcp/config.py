from __future__ import annotations

import base64
import json
import math
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "telegram-mcp"
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "telegram-mcp"
        )
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "telegram-mcp"


@dataclass(frozen=True)
class Config:
    api_id: int = 0
    api_hash: str = field(default="", repr=False)
    data_dir: Path = field(default_factory=default_data_dir)
    library_path: str | None = None
    database_key: str = field(default="", repr=False)
    request_timeout: float = 30.0
    login_timeout: float = 300.0

    def __post_init__(self) -> None:
        if not 0 <= self.api_id < 2**31:
            raise ValueError("TELEGRAM_API_ID must be a positive 32-bit integer.")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("TELEGRAM_REQUEST_TIMEOUT must be a finite positive number.")
        if not math.isfinite(self.login_timeout) or self.login_timeout <= 0:
            raise ValueError("TELEGRAM_LOGIN_TIMEOUT must be a finite positive number.")

    @property
    def has_credentials(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash.strip())

    def with_credentials(self, app_id: int, app_hash: str) -> Config:
        if not 0 < app_id < 2**31 or not app_hash.strip():
            raise ValueError("Enter your App ID and App hash from my.telegram.org/apps.")
        return replace(self, api_id=app_id, api_hash=app_hash.strip())

    def load_credentials(self) -> Config:
        if self.has_credentials:
            return self
        try:
            data = json.loads((self.data_dir / "app-credentials.json").read_text())
            return self.with_credentials(int(data["app_id"]), data["app_hash"])
        except FileNotFoundError:
            return self
        except (ValueError, TypeError, KeyError, AttributeError):
            # A browser submission can replace malformed local credentials.
            return self

    def save_credentials(self) -> None:
        import tempfile

        if not self.has_credentials:
            raise ValueError("App credentials are incomplete.")
        self.prepare_directories()
        fd, temporary = tempfile.mkstemp(prefix=".app-credentials-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"app_id": self.api_id, "app_hash": self.api_hash}, stream)
            os.replace(temporary, self.data_dir / "app-credentials.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    @classmethod
    def from_env(cls) -> Config:
        try:
            api_id = int(os.environ.get("TELEGRAM_API_ID") or "0")
        except ValueError:
            raise ValueError("TELEGRAM_API_ID must be an integer.") from None
        try:
            timeout = float(os.environ.get("TELEGRAM_REQUEST_TIMEOUT", "30"))
        except ValueError:
            raise ValueError("TELEGRAM_REQUEST_TIMEOUT must be a number.") from None
        try:
            login_timeout = float(os.environ.get("TELEGRAM_LOGIN_TIMEOUT", "300"))
        except ValueError:
            raise ValueError("TELEGRAM_LOGIN_TIMEOUT must be a number.") from None
        return cls(
            api_id=api_id,
            api_hash=os.environ.get("TELEGRAM_API_HASH", ""),
            data_dir=Path(os.environ.get("TELEGRAM_DATA_DIR", default_data_dir()))
            .expanduser()
            .resolve(),
            library_path=os.environ.get("TDLIB_LIBRARY_PATH") or None,
            database_key=os.environ.get("TELEGRAM_DATABASE_KEY", ""),
            request_timeout=timeout,
            login_timeout=login_timeout,
        )

    def prepare_directories(self) -> None:
        for directory in (self.data_dir, self.data_dir / "database", self.data_dir / "files"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    def tdlib_parameters(self) -> dict:
        if not self.has_credentials:
            raise ValueError("App credentials are needed before initializing TDLib.")
        return {
            "@type": "setTdlibParameters",
            "use_test_dc": False,
            "database_directory": str(self.data_dir / "database"),
            "files_directory": str(self.data_dir / "files"),
            # TDLib JSON encodes bytes as base64, including database encryption keys.
            "database_encryption_key": base64.b64encode(self.database_key.encode()).decode(),
            "use_file_database": True,
            "use_chat_info_database": True,
            "use_message_database": True,
            "use_secret_chats": False,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "system_language_code": "en",
            "device_model": "Telegram MCP",
            "system_version": sys.platform,
            "application_version": "0.1.0",
        }
