from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import uvicorn
from filelock import FileLock
from starlette.responses import JSONResponse

from .config import Config
from .server import create_server


def read_endpoint(config: Config) -> dict | None:
    try:
        data = json.loads((config.data_dir / "backend.json").read_text())
        url = urlsplit(data["url"])
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.path != "/mcp"
            or not isinstance(data["token"], str)
            or len(data["token"]) < 32
        ):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def backend_request(endpoint: dict, path: str, *, method: str = "GET") -> dict:
    origin = endpoint["url"].removesuffix("/mcp")
    request = Request(
        origin + path,
        method=method,
        headers={
            "Authorization": "Bearer " + endpoint["token"],
        },
    )
    with urlopen(request, timeout=1) as response:
        return json.load(response)


def is_running(endpoint: dict | None) -> bool:
    if endpoint is None:
        return False
    try:
        return backend_request(endpoint, "/health").get("service") == "telegram-mcp"
    except (OSError, URLError, ValueError):
        return False


def ensure_backend(config: Config) -> dict:
    """Autostart one backend per data directory, even across simultaneous chats."""
    config.prepare_directories()
    with FileLock(str(config.data_dir / "backend-start.lock"), timeout=45):
        endpoint = read_endpoint(config)
        if is_running(endpoint):
            return endpoint
        environment = os.environ.copy()
        environment.update(
            {
                "TELEGRAM_DATA_DIR": str(config.data_dir),
                "TELEGRAM_API_ID": str(config.api_id),
                "TELEGRAM_API_HASH": config.api_hash,
                "TELEGRAM_DATABASE_KEY": config.database_key,
                "TELEGRAM_REQUEST_TIMEOUT": str(config.request_timeout),
                "TELEGRAM_LOGIN_TIMEOUT": str(config.login_timeout),
            }
        )
        if config.library_path:
            environment["TDLIB_LIBRARY_PATH"] = config.library_path
        log_path = config.data_dir / "backend.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "telegram_mcp", "serve-backend"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=environment,
                start_new_session=True,
            )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            endpoint = read_endpoint(config)
            if is_running(endpoint):
                return endpoint
            if process.poll() is not None:
                raise RuntimeError(f"Telegram backend could not start. See {log_path}.")
            time.sleep(0.1)
        # This process was created by this call and has never become ready.
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError(f"Telegram backend startup timed out. See {log_path}.")


def stop_backend(config: Config) -> bool:
    with FileLock(str(config.data_dir / "backend-start.lock"), timeout=45):
        endpoint = read_endpoint(config)
        if not is_running(endpoint):
            return False
        backend_request(endpoint, "/shutdown", method="POST")
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if read_endpoint(config) != endpoint:
                return True
            time.sleep(0.1)
        raise RuntimeError("Telegram backend is still closing its database.")


class PrivateBackend:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            expected = ("Bearer " + self.token).encode()
            if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def backend_app(server, token: str, shutdown):
    @server.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"service": "telegram-mcp", "pid": os.getpid()})

    @server.custom_route("/shutdown", methods=["POST"])
    async def stop(request):
        shutdown()
        return JSONResponse({"ok": True})

    return PrivateBackend(server.streamable_http_app(json_response=True), token)


def run_backend(config: Config) -> None:
    config.prepare_directories()
    token = secrets.token_urlsafe(32)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    runner = None

    def shutdown():
        if runner:
            runner.should_exit = True

    app = backend_app(create_server(config), token, shutdown)
    runner = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    )
    endpoint = {"url": f"http://127.0.0.1:{port}/mcp", "token": token, "pid": os.getpid()}
    fd, temporary = tempfile.mkstemp(prefix=".backend-", dir=config.data_dir)
    path = config.data_dir / "backend.json"
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(endpoint, stream)
        os.replace(temporary, path)
        runner.run(sockets=[listener])
    finally:
        listener.close()
        Path(temporary).unlink(missing_ok=True)
        if read_endpoint(config) == endpoint:
            path.unlink(missing_ok=True)
