from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from anyio import CancelScope

from .browser_auth import BrowserLogin
from .client import TDLibClient
from .config import Config
from .qr import QR_START_STATES


class TelegramSession:
    """Keep one TDLib client and one browser login shared by concurrent MCP calls."""

    def __init__(
        self,
        config: Config,
        client_factory: Callable[[Config], TDLibClient] = TDLibClient,
        browser_factory: Callable[..., BrowserLogin] = BrowserLogin,
    ) -> None:
        self.config = config.load_credentials()
        self._client_factory = client_factory
        self._browser_factory = browser_factory
        self._client: TDLibClient | None = None
        self._browser: BrowserLogin | None = None
        self._login_task: asyncio.Task | None = None
        self._client_available = asyncio.Event()
        self._login_changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._credentials_lock = asyncio.Lock()
        self._closing = False
        self._logging_out = False
        self._logout_task: asyncio.Task | None = None
        self._auth_generation = 0
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self._client is not None
            and self._client.ready
            and not self._closing
            and not self._logging_out
        )

    @property
    def authorization_state(self) -> dict[str, Any]:
        if self._logging_out:
            return {"@type": "authorizationStateLoggingOut"}
        if self._client is None:
            return {
                "@type": (
                    "authorizationStateWaitPhoneNumber"
                    if self.config.has_credentials
                    else "authorizationStateWaitAppCredentials"
                )
            }
        return self._client.authorization_state

    @property
    def login_url(self) -> str | None:
        return self._browser.url if self._browser else None

    async def start(self) -> None:
        if self.config.has_credentials:
            await self._start_client()

    async def _start_client(self) -> None:
        client = self._client_factory(self.config)
        await client.start()
        self._client = client
        self._client_available.set()

    async def submit_credentials(self, app_id: int, app_hash: str) -> None:
        async with self._credentials_lock:
            if self.ready or self._closing or self._logging_out:
                raise RuntimeError("This login is no longer accepting app credentials.")
            config = self.config.with_credentials(app_id, app_hash)
            if self._login_task and not self._login_task.done():
                self._login_task.cancel()
                await asyncio.gather(self._login_task, return_exceptions=True)
            if self._client:
                await self._client.close()
                self._client = None
            self._client_available.clear()
            self.config = config
            self._login_changed.clear()
            try:
                await self._start_client()
                config.save_credentials()
            except Exception:
                self.error = "Unable to initialize Telegram. Check the app credentials and retry."
                self._login_changed.set()
                raise
            self.error = None
            self._new_login_task()

    def _new_login_task(self) -> None:
        self._login_changed.clear()
        self._login_task = asyncio.create_task(self._drive_login())
        # Status/browser-only login may have no tool call awaiting this task.
        self._login_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _drive_login(self) -> None:
        try:
            await self._client_available.wait()
            client = self._client
            assert client is not None
            qr_requested = False
            while not client.ready:
                state = client.authorization_state
                if not qr_requested and state["@type"] in QR_START_STATES:
                    await client.request("requestQrCodeAuthentication", other_user_ids=[])
                    qr_requested = True
                elif state["@type"] in {
                    "authorizationStateWaitRegistration",
                    "authorizationStateWaitPremiumPurchase",
                }:
                    raise RuntimeError(
                        "Complete Telegram's registration or purchase step in an official app, "
                        "then retry login."
                    )
                await client.wait_for_auth_change(state)
            self.error = None
            self._login_changed.set()
        except Exception as exc:
            self.error = str(exc)
            self._login_changed.set()
            raise

    async def begin_login(self, *, expected_generation: int | None = None) -> None:
        async with self._lock:
            if expected_generation is not None and expected_generation != self._auth_generation:
                raise RuntimeError(
                    "Telegram was logged out. Retry the account tool to sign in again."
                )
            if self.ready:
                return
            if self._closing:
                raise RuntimeError("Telegram session is closing.")
            if self._logging_out or self.authorization_state["@type"] in {
                "authorizationStateLoggingOut",
                "authorizationStateClosing",
            }:
                raise RuntimeError(
                    "Telegram logout is still in progress. Retry logout to confirm it."
                )
            if self._client and self.authorization_state["@type"] == "authorizationStateClosed":
                await self._client.close()
                self._client = None
                self._client_available.clear()
                self.error = None
            if self._client is None and self.config.has_credentials:
                await self._start_client()
            if self._login_task and self._login_task.done() and self.error is None:
                self._login_task = None
                self._login_changed.clear()
            if self._browser is None:
                browser = self._browser_factory(self)
                await browser.start()
                self._browser = browser
            if self._login_task is None:
                self._new_login_task()

    async def ensure_authenticated(self) -> None:
        if self._logout_task and not self._logout_task.done():
            await asyncio.shield(self._logout_task)
        generation = self._auth_generation
        if self.ready:
            return
        await self.begin_login(expected_generation=generation)
        # Credential submission may replace the driver task, so wait on readiness
        # and state changes rather than on the identity of a particular task.
        try:
            async with asyncio.timeout(self.config.login_timeout):
                while True:
                    if generation != self._auth_generation:
                        raise RuntimeError(
                            "Telegram was logged out. Retry the account tool to sign in again."
                        )
                    if self.ready:
                        return
                    if self._closing:
                        raise RuntimeError("Telegram session is closing.")
                    if self.error:
                        raise RuntimeError(
                            f"Telegram login needs attention in {self.login_url}: {self.error}"
                        )
                    await self._login_changed.wait()
        except TimeoutError:
            raise RuntimeError(
                f"Complete Telegram login in {self.login_url}, then retry this tool."
            ) from None

    async def request(self, method: str, **parameters: Any) -> dict:
        await self.ensure_authenticated()
        assert self._client is not None
        return await self._client.request(method, **parameters)

    async def logout(self) -> dict:
        async with self._lock:
            if self._closing:
                raise RuntimeError("Telegram session is closing.")
            if self._logout_task is None or self._logout_task.done():
                self._logging_out = True
                self._auth_generation += 1
                self._login_changed.set()
                self._logout_task = asyncio.create_task(self._perform_logout())
                self._logout_task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )
            task = self._logout_task
        # Finishing a requested logout must not depend on one chat staying connected.
        await asyncio.shield(task)
        return {"logged_out": True, "authorized": False}

    async def _perform_logout(self) -> None:
        try:
            async with self._credentials_lock:
                if self._login_task:
                    self._login_task.cancel()
                    await asyncio.gather(self._login_task, return_exceptions=True)
                    self._login_task = None
                if self._browser:
                    await self._browser.close()
                    self._browser = None
                if self._client:
                    await self._client.log_out()
                    self._client = None
                self._client_available.clear()
                self.error = None
        finally:
            self._logging_out = False
            self._login_changed.set()

    async def submit_auth(self, action: str, value: str = "") -> None:
        client = self._client
        if client is None or self._closing or self._logging_out:
            raise RuntimeError("Enter your App ID and App hash first.")
        kind = client.authorization_state["@type"]
        allowed = {
            "password": (
                "authorizationStateWaitPassword",
                "checkAuthenticationPassword",
                "password",
            ),
            "email": (
                "authorizationStateWaitEmailAddress",
                "setAuthenticationEmailAddress",
                "email_address",
            ),
            "code": ("authorizationStateWaitCode", "checkAuthenticationCode", "code"),
            "email_code": (
                "authorizationStateWaitEmailCode",
                "checkAuthenticationEmailCode",
                "code",
            ),
        }
        if action not in allowed or kind != allowed[action][0]:
            raise ValueError("Telegram is not requesting that login step.")
        _, method, key = allowed[action]
        data: Any = value
        if action == "email_code":
            data = {"@type": "emailAddressAuthenticationCode", "code": value}
        await client.request(method, **{key: data})

    async def retry_login(self) -> None:
        async with self._lock:
            if self._logging_out:
                raise RuntimeError("Telegram logout is in progress.")
            if self.ready:
                return
            self.error = None
            self._login_changed.clear()
            if self._login_task is None or self._login_task.done():
                self._new_login_task()

    async def close(self) -> None:
        with CancelScope(shield=True):
            self._closing = True
            self._login_changed.set()
            if self._logout_task and not self._logout_task.done():
                await asyncio.gather(self._logout_task, return_exceptions=True)
            if self._login_task:
                self._login_task.cancel()
                await asyncio.gather(self._login_task, return_exceptions=True)
            if self._browser:
                await self._browser.close()
            if self._client:
                await self._client.close()
