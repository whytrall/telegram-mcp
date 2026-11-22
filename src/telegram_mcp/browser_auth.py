from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import qrcode
from qrcode.image.svg import SvgPathFillImage

if TYPE_CHECKING:
    from .session import TelegramSession

ASSETS = Path(__file__).with_name("login")
STAGES = {
    "authorizationStateWaitAppCredentials": "credentials",
    "authorizationStateWaitOtherDeviceConfirmation": "qr",
    "authorizationStateWaitPassword": "password",
    "authorizationStateWaitEmailAddress": "email",
    "authorizationStateWaitEmailCode": "email_code",
    "authorizationStateWaitCode": "code",
    "authorizationStateReady": "ready",
}


class BrowserLogin:
    """Serve a private loopback login page; secrets never become MCP arguments."""

    def __init__(self, session: TelegramSession, opener: Callable[[str], Any] = webbrowser.open):
        self.session = session
        self.opener = opener
        self.prefix = "/" + secrets.token_urlsafe(24) + "/"
        self.url = ""
        self.origin = ""
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        state = self.session.authorization_state
        stage = STAGES.get(state["@type"], "waiting")
        if self.session.error:
            stage = "error" if self.session.config.has_credentials else "credentials"
        return {
            "stage": stage,
            "authorized": self.session.ready,
            "error": self.session.error,
            "revision": hashlib.sha256(state.get("link", "").encode()).hexdigest(),
        }

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        page = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, status: int, data: Any, kind: str = "application/json") -> None:
                if not isinstance(data, bytes):
                    data = (
                        json.dumps(data).encode() if kind == "application/json" else data.encode()
                    )
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; "
                    "script-src 'self'; style-src 'self'; img-src 'self'; "
                    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'",
                )
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def valid_host(self) -> bool:
                return self.headers.get("Host") == urlsplit(page.origin).netloc

            def do_GET(self) -> None:
                if not self.valid_host():
                    self.reply(403, {"error": "Forbidden"})
                    return
                path = urlsplit(self.path).path
                assets = {
                    page.prefix: ("index.html", "text/html; charset=utf-8"),
                    page.prefix + "app.js": ("app.js", "application/javascript; charset=utf-8"),
                    page.prefix + "style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if path in assets:
                    name, kind = assets[path]
                    self.reply(200, (ASSETS / name).read_bytes(), kind)
                elif path == page.prefix + "status":
                    self.reply(200, page.status())
                elif path == page.prefix + "qr.svg":
                    state = page.session.authorization_state
                    if state["@type"] != "authorizationStateWaitOtherDeviceConfirmation":
                        self.reply(404, {"error": "No active QR code"})
                        return
                    image = qrcode.make(state["link"], image_factory=SvgPathFillImage, border=4)
                    self.reply(200, image.to_string(), "image/svg+xml")
                else:
                    self.reply(404, {"error": "Not found"})

            def do_POST(self) -> None:
                if (
                    not self.valid_host()
                    or self.headers.get("Origin") != page.origin
                    or self.headers.get_content_type() != "application/json"
                ):
                    self.reply(403, {"error": "Forbidden"})
                    return
                action = self.path.removeprefix(page.prefix)
                if self.path != page.prefix + action or action not in {
                    "credentials",
                    "password",
                    "email",
                    "email_code",
                    "code",
                    "retry",
                }:
                    self.reply(404, {"error": "Not found"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        raise ValueError()
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError()
                    if action == "credentials":
                        app_id = int(data.get("app_id", ""))
                        app_hash = data.get("app_hash", "")
                        if not isinstance(app_hash, str):
                            raise ValueError()
                        operation = page.session.submit_credentials(app_id, app_hash)
                    elif action == "retry":
                        operation = page.session.retry_login()
                    else:
                        value = data.get("value", "")
                        if not isinstance(value, str) or not value:
                            raise ValueError()
                        operation = page.session.submit_auth(action, value)
                except (ValueError, TypeError):
                    self.reply(400, {"error": "Please check the values and try again."})
                    return
                future = asyncio.run_coroutine_threadsafe(operation, loop)
                try:
                    future.result(timeout=page.session.config.request_timeout + 35)
                    self.reply(200, {"ok": True})
                except Exception:
                    # Never echo submitted credentials, passwords, codes, or a
                    # request payload in an HTTP error or server diagnostic.
                    future.cancel()
                    self.reply(
                        400,
                        {
                            "error": "Telegram could not complete this step. "
                            "Check your details and try again."
                        },
                    )

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._http.server_port}"
        self.url = self.origin + self.prefix
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()
        try:
            await asyncio.to_thread(self.opener, self.url)
        except Exception:
            # Keep the page available; tool/status responses include the URL when
            # automatic browser opening isn't available (for example headless hosts).
            pass

    async def close(self) -> None:
        if self._http is not None:
            await asyncio.to_thread(self._http.shutdown)
            self._http.server_close()
            assert self._thread is not None
            await asyncio.to_thread(self._thread.join)
            self._http = None
