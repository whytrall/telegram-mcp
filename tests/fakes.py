from __future__ import annotations

import asyncio
import queue
from typing import Any


class FakeBrowser:
    def __init__(self, session):
        self.url = "http://127.0.0.1:12345/test-login/"

    async def start(self):
        pass

    async def close(self):
        pass


class FakeTDLib:
    def __init__(self, *, authorized: bool = True) -> None:
        self.events: queue.Queue = queue.Queue()
        self.requests: list[dict] = []
        self.requested = asyncio.Event()
        self.responses: dict[str, dict] = {}
        self.held_methods: set[str] = set()
        self.authorized = authorized
        self.released = False
        self.closed = False

    def emit(self, event: dict) -> None:
        self.events.put(event)

    def respond(self, request: dict, result: dict) -> None:
        self.emit({**result, "@extra": request.get("@extra")})

    def send(self, request: dict[str, Any]) -> None:
        self.requests.append(request)
        self.requested.set()
        method = request["@type"]
        if method in self.held_methods:
            return
        if method == "getAuthorizationState":
            state = {"@type": "authorizationStateWaitTdlibParameters"}
            self.emit({"@type": "updateAuthorizationState", "authorization_state": state})
            self.respond(request, state)
        elif method == "setTdlibParameters":
            result = self.responses.get(method, {"@type": "ok"})
            self.respond(request, result)
            if result["@type"] != "error":
                self.emit(
                    {
                        "@type": "updateAuthorizationState",
                        "authorization_state": {
                            "@type": (
                                "authorizationStateReady"
                                if self.authorized
                                else "authorizationStateWaitPhoneNumber"
                            )
                        },
                    }
                )
        elif method == "logOut":
            result = self.responses.get(method, {"@type": "ok"})
            self.respond(request, result)
            if result["@type"] != "error":
                self.authorized = False
                self.closed = True
                self.emit(
                    {
                        "@type": "updateAuthorizationState",
                        "authorization_state": {
                            "@type": "authorizationStateClosed",
                        },
                    }
                )
        elif method == "close":
            self.closed = True
            self.emit(
                {
                    "@type": "updateAuthorizationState",
                    "authorization_state": {
                        "@type": "authorizationStateClosed",
                    },
                }
            )
        else:
            self.respond(request, self.responses.get(method, {"@type": "ok"}))

    def receive(self, timeout: float) -> dict | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def release(self) -> None:
        assert self.closed
        self.released = True


def message(message_id: int = 100, text: str = "hello") -> dict:
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": 42,
        "date": 1_800_000_000,
        "sender_id": {"@type": "messageSenderUser", "user_id": 7},
        "content": {
            "@type": "messageText",
            "text": {
                "@type": "formattedText",
                "text": text,
                "entities": [],
            },
        },
    }
