from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from typing import Any

from filelock import FileLock, Timeout

from .config import Config
from .native import NativeTDLib, Transport


class TDLibError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        # TDLib explicitly forbids displaying/processing the message for code 406.
        super().__init__(f"TDLib error {code}" if code == 406 else f"TDLib error {code}: {message}")


class RequestTimeout(RuntimeError):
    pass


class TDLibClient:
    def __init__(self, config: Config, transport: Transport | None = None) -> None:
        self.config = config
        self._transport = transport
        self._pending: dict[str, asyncio.Future] = {}
        self._sending: set[tuple[int, int]] = set()
        self._settled_sends: deque[tuple[int, int]] = deque(maxlen=1024)
        self._auth: dict[str, Any] = {"@type": "starting"}
        self._auth_changed = asyncio.Condition()
        self._initialized = asyncio.Event()
        self._closed = asyncio.Event()
        self._failure: Exception | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initialize_task: asyncio.Task | None = None
        self._closing = False
        self._lock = FileLock(str(config.data_dir / "session.lock"))

    @property
    def authorization_state(self) -> dict[str, Any]:
        return dict(self._auth)

    @property
    def ready(self) -> bool:
        return self._auth["@type"] == "authorizationStateReady" and not self._closing

    @property
    def has_pending_sends(self) -> bool:
        return bool(self._sending)

    async def __aenter__(self) -> TDLibClient:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def start(self) -> None:
        self.config.prepare_directories()
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError(
                "This Telegram session is already in use. Stop the other server/login process "
                "or use a different TELEGRAM_DATA_DIR."
            ) from None
        try:
            self._transport = self._transport or NativeTDLib(self.config.library_path)
            self._loop = asyncio.get_running_loop()
            self._thread = threading.Thread(
                target=self._receive_loop, daemon=True, name="tdlib-receive"
            )
            self._thread.start()
            state = await self.request("getAuthorizationState")
            if self._auth["@type"] == "starting":
                self._on_auth(state)
            await asyncio.wait_for(self._initialized.wait(), self.config.request_timeout)
            if self._failure:
                raise self._failure
        except BaseException:
            if self._thread:
                await self.close()
            else:
                self._lock.release()
            raise

    def _receive_loop(self) -> None:
        assert self._transport is not None
        try:
            while not self._stop.is_set():
                event = self._transport.receive(0.2)
                if event:
                    self._loop.call_soon_threadsafe(self._on_event, event)
        except Exception:
            self._loop.call_soon_threadsafe(
                self._fail, RuntimeError("TDLib receive loop failed; restart the server.")
            )

    def _fail(self, error: Exception) -> None:
        self._failure = error
        self._sending.clear()
        self._initialized.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        asyncio.create_task(self._notify(self._auth_changed))

    def _on_event(self, event: dict[str, Any]) -> None:
        extra = event.get("@extra")
        future = self._pending.get(extra) if isinstance(extra, str) else None
        if future is not None and not future.done():
            if event.get("@type") == "error":
                future.set_exception(TDLibError(event["code"], event.get("message", "")))
            else:
                future.set_result(event)
        event_type = event.get("@type")
        if event_type in {"updateMessageSendSucceeded", "updateMessageSendFailed"}:
            message = event["message"]
            key = (message["chat_id"], event["old_message_id"])
            self._sending.discard(key)
            self._settled_sends.append(key)
        elif event_type in {"message", "messages", "updateNewMessage"}:
            messages = (
                event.get("messages", [])
                if event_type == "messages"
                else [event["message"]]
                if event_type == "updateNewMessage"
                else [event]
            )
            for message in messages:
                state = (message.get("sending_state") or {}) if message else {}
                if state.get("@type") == "messageSendingStatePending":
                    key = (message["chat_id"], message["id"])
                    if key not in self._settled_sends:
                        self._sending.add(key)
        elif event_type == "updateDeleteMessages":
            for message_id in event["message_ids"]:
                key = (event["chat_id"], message_id)
                self._sending.discard(key)
                self._settled_sends.append(key)
        if event_type == "updateAuthorizationState":
            self._on_auth(event["authorization_state"])

    @staticmethod
    async def _notify(condition: asyncio.Condition) -> None:
        async with condition:
            condition.notify_all()

    def _on_auth(self, state: dict[str, Any]) -> None:
        self._auth = state
        kind = state["@type"]
        if kind == "authorizationStateWaitTdlibParameters":
            if self._initialize_task is None:
                self._initialize_task = asyncio.create_task(self._set_parameters())
        else:
            self._initialized.set()
        if kind == "authorizationStateClosed":
            self._closed.set()
            self._fail(RuntimeError("TDLib closed; restart the server."))
        asyncio.create_task(self._notify(self._auth_changed))

    async def _set_parameters(self) -> None:
        try:
            parameters = self.config.tdlib_parameters()
            await self.request(parameters.pop("@type"), **parameters)
        except Exception as exc:
            self._fail(exc)

    async def wait_for_auth_change(self, previous: dict) -> dict:
        async with self._auth_changed:
            await self._auth_changed.wait_for(
                lambda: self._auth != previous or self._failure is not None
            )
        if self._failure:
            raise self._failure
        return self.authorization_state

    async def request(self, method: str, **parameters: Any) -> dict[str, Any]:
        if self._transport is None or self._closed.is_set():
            raise RuntimeError("TDLib is not running.")
        if self._failure:
            raise self._failure
        if self._closing and method != "close":
            raise RuntimeError("TDLib is closing.")
        extra = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[extra] = future
        try:
            self._transport.send({"@type": method, **parameters, "@extra": extra})
            return await asyncio.wait_for(future, self.config.request_timeout)
        except TimeoutError:
            raise RequestTimeout(f"{method} timed out.") from None
        finally:
            self._pending.pop(extra, None)

    async def close(self) -> None:
        if self._thread is None:
            return
        self._closing = True
        if self._initialize_task and not self._initialize_task.done():
            self._initialize_task.cancel()
            await asyncio.gather(self._initialize_task, return_exceptions=True)
        try:
            if not self._closed.is_set():
                # Send close without awaiting its response; the terminal auth update
                # is the guarantee that TDLib has flushed and closed its databases.
                assert self._transport is not None
                self._transport.send({"@type": "close"})
                await asyncio.wait_for(self._closed.wait(), 30)
        finally:
            self._stop.set()
            await asyncio.to_thread(self._thread.join)
            self._fail(RuntimeError("TDLib client stopped."))
            if self._closed.is_set():
                assert self._transport is not None
                self._transport.release()
            self._lock.release()
            self._thread = None

    async def log_out(self) -> None:
        """Revoke this Telegram session and wait for TDLib's terminal state."""
        if not self._closed.is_set() and self._auth["@type"] not in {
            "authorizationStateLoggingOut",
            "authorizationStateClosing",
        }:
            try:
                await self.request("logOut")
            except RuntimeError:
                # TDLib can deliver the terminal auth update before the response.
                if not self._closed.is_set():
                    raise
        try:
            await asyncio.wait_for(self._closed.wait(), self.config.request_timeout)
        except TimeoutError:
            raise RequestTimeout(
                "Logout is not confirmed yet. Retry logout to check completion."
            ) from None
        await self.close()
