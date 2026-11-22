import asyncio
import io

import pytest

from telegram_mcp import cli
from telegram_mcp.client import TDLibClient
from telegram_mcp.qr import TerminalQR

from .fakes import FakeTDLib


class LoginTransport(FakeTDLib):
    def __init__(self):
        super().__init__(authorized=False)

    def auth(self, kind, **fields):
        self.emit(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {
                    "@type": kind,
                    **fields,
                },
            }
        )

    def send(self, request):
        super().send(request)
        kind = request["@type"]
        if kind == "requestQrCodeAuthentication":
            self.auth(
                "authorizationStateWaitOtherDeviceConfirmation", link="tg://login?token=first"
            )
        elif kind == "setAuthenticationPhoneNumber":
            self.auth("authorizationStateWaitCode")
        elif kind in ("checkAuthenticationPassword", "checkAuthenticationCode"):
            self.auth("authorizationStateReady")


async def test_qr_refresh_then_two_step_password(config, monkeypatch, capsys):
    transport = LoginTransport()
    monkeypatch.setattr(cli, "TDLibClient", lambda c: TDLibClient(c, transport))
    rendered = []

    def render(self, link):
        rendered.append(link)
        if len(rendered) == 1:
            transport.auth(
                "authorizationStateWaitOtherDeviceConfirmation", link="tg://login?token=second"
            )
        else:
            transport.auth("authorizationStateWaitPassword")

    async def prompt(label, *, secret=False):
        assert "password" in label
        assert secret
        return "  secret with spaces  "

    monkeypatch.setattr(TerminalQR, "render", render)
    monkeypatch.setattr(cli, "prompt", prompt)
    async with asyncio.timeout(5):
        await cli.login(config)
    assert rendered == ["tg://login?token=first", "tg://login?token=second"]
    methods = [r["@type"] for r in transport.requests]
    assert methods.count("requestQrCodeAuthentication") == 1
    assert "setAuthenticationPhoneNumber" not in methods
    password = next(r for r in transport.requests if r["@type"] == "checkAuthenticationPassword")
    assert password["password"] == "  secret with spaces  "
    output = capsys.readouterr()
    assert "Logged in" in output.out
    assert "token=" not in output.out + output.err
    assert "secret with spaces" not in output.out + output.err
    assert transport.released


async def test_phone_fallback_does_not_request_qr(config, monkeypatch):
    transport = LoginTransport()
    monkeypatch.setattr(cli, "TDLibClient", lambda c: TDLibClient(c, transport))
    prompts = []

    async def prompt(label, *, secret=False):
        prompts.append((label, secret))
        return "12345" if secret else "+1234567890"

    monkeypatch.setattr(cli, "prompt", prompt)
    async with asyncio.timeout(5):
        await cli.login(config, method="phone")
    assert len(prompts) == 2
    assert prompts[0][1] is False
    assert prompts[1][1] is True
    assert "requestQrCodeAuthentication" not in [r["@type"] for r in transport.requests]
    assert transport.released


async def test_qr_cancellation_closes_native_session(config, monkeypatch):
    transport = LoginTransport()
    monkeypatch.setattr(cli, "TDLibClient", lambda c: TDLibClient(c, transport))
    displayed = asyncio.Event()
    monkeypatch.setattr(TerminalQR, "render", lambda *args: displayed.set())
    task = asyncio.create_task(cli.login(config))
    async with asyncio.timeout(5):
        await displayed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert transport.released


async def test_qr_request_failure_exits_without_retry_loop(config, monkeypatch):
    transport = FakeTDLib(authorized=False)
    transport.responses["requestQrCodeAuthentication"] = {
        "@type": "error",
        "code": 429,
        "message": "FLOOD_WAIT_60",
    }
    monkeypatch.setattr(cli, "TDLibClient", lambda c: TDLibClient(c, transport))
    with pytest.raises(RuntimeError, match="QR login failed.*--phone"):
        await cli.login(config)
    assert len([r for r in transport.requests if r["@type"] == "requestQrCodeAuthentication"]) == 1
    assert transport.released


async def test_secret_prompt_preserves_password_whitespace(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda label: " password ")
    assert await cli.prompt("Password: ", secret=True) == " password "


def test_qr_terminal_draws_and_replaces_old_code():
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    renderer = TerminalQR(terminal)
    renderer.render("tg://login?token=first")
    first = terminal.getvalue()
    assert "Link Desktop Device" in first
    assert "█" in first
    assert "\x1b[30;47m" in first
    assert "tg://" not in first
    renderer.render("tg://login?token=second")
    assert "F\x1b[J" in terminal.getvalue()[len(first) :]
    renderer.clear()
    assert renderer._lines == 0
