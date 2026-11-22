import asyncio
import json
from dataclasses import replace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from mcp import Client

from telegram_mcp.browser_auth import BrowserLogin
from telegram_mcp.client import TDLibClient
from telegram_mcp.config import Config
from telegram_mcp.server import create_server
from telegram_mcp.session import TelegramSession

from .fakes import FakeTDLib


class AuthTransport(FakeTDLib):
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
        if request["@type"] == "requestQrCodeAuthentication":
            self.auth(
                "authorizationStateWaitOtherDeviceConfirmation", link="tg://login?token=first"
            )
        elif request["@type"] in (
            "checkAuthenticationPassword",
            "checkAuthenticationCode",
            "checkAuthenticationEmailCode",
        ):
            if self.responses.get(request["@type"], {}).get("@type") != "error":
                self.auth("authorizationStateReady")


async def http(page, path="", data=None, headers=None):
    request = Request(page.url + path)
    if data is not None:
        request.data = json.dumps(data).encode()
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", page.origin)
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    def read():
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    return await asyncio.to_thread(read)


async def wait_stage(page, expected):
    async with asyncio.timeout(5):
        while True:
            previous = page.session.authorization_state
            if page.status()["stage"] == expected:
                return
            if page.session._client:
                await page.session._client.wait_for_auth_change(previous)
            else:
                await page.session._client_available.wait()


def fixture_server(config, transport):
    opened = []
    pages = []
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def browser_factory(session):
        page = BrowserLogin(
            session,
            opener=lambda url: (
                opened.append(url),
                loop.call_soon_threadsafe(started.set),
            ),
        )
        pages.append(page)
        return page

    server = create_server(config, lambda c: TDLibClient(c, transport), browser_factory)
    return server, opened, pages, started


async def test_first_account_call_collects_app_credentials_qr_password_and_resumes(config):
    config = replace(config, api_id=0, api_hash="")
    transport = AuthTransport()
    transport.responses["getMe"] = {"@type": "user", "id": 7, "first_name": "Alex"}
    server, opened, pages, started = fixture_server(config, transport)
    async with Client(server) as client:
        assert not transport.requests  # Credential-less server starts without TDLib.
        assert len((await client.list_tools()).tools) == 12
        assert (await client.call_tool("telegram_status")).structured_content["authorized"] is False
        assert not opened  # Diagnostics do not start login.
        pending = asyncio.create_task(client.call_tool("get_me"))
        async with asyncio.timeout(5):
            await started.wait()
        page = pages[0]
        assert page.status()["stage"] == "credentials"
        assert not pending.done()
        status, body, headers = await http(page)
        assert status == 200
        assert b'name="app_id"' in body and b'name="app_hash"' in body
        assert headers["Cache-Control"] == "no-store"
        status, body, _ = await http(
            page,
            "credentials",
            {
                "app_id": 12345,
                "app_hash": "private-app-hash",
            },
        )
        assert status == 200
        assert b"private-app-hash" not in body
        await wait_stage(page, "qr")
        status, svg, _ = await http(page, "qr.svg")
        assert status == 200 and b"<svg" in svg
        previous = page.session.authorization_state
        transport.auth("authorizationStateWaitOtherDeviceConfirmation", link="tg://login?token=new")
        async with asyncio.timeout(5):
            await page.session._client.wait_for_auth_change(previous)
        assert (await http(page, "qr.svg"))[1] != svg
        assert "link" not in json.loads((await http(page, "status"))[1])
        transport.auth("authorizationStateWaitPassword")
        await wait_stage(page, "password")
        assert (await http(page, "qr.svg"))[0] == 404
        status, body, _ = await http(page, "password", {"value": "  private password  "})
        assert status == 200
        assert b"private password" not in body
        async with asyncio.timeout(5):
            result = await pending
        assert result.structured_content["id"] == 7
        assert page.status()["authorized"] is True
        assert len(opened) == 1
        request = next(r for r in transport.requests if r["@type"] == "checkAuthenticationPassword")
        assert request["password"] == "  private password  "
    assert transport.released
    saved = Config(data_dir=config.data_dir).load_credentials()
    assert saved.api_id == 12345 and saved.api_hash == "private-app-hash"
    assert (config.data_dir / "app-credentials.json").stat().st_mode & 0o777 == 0o600


async def test_concurrent_reads_share_login_and_cancellation_does_not_close_browser(config):
    transport = AuthTransport()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, opened, pages, started = fixture_server(config, transport)
    async with Client(server) as client:
        first = asyncio.create_task(client.call_tool("get_me"))
        second = asyncio.create_task(client.call_tool("get_me"))
        async with asyncio.timeout(5):
            await started.wait()
        await wait_stage(pages[0], "qr")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        transport.auth("authorizationStateReady")
        async with asyncio.timeout(5):
            assert (await second).structured_content["id"] == 7
        assert len(opened) == 1
        assert (
            len([r for r in transport.requests if r["@type"] == "requestQrCodeAuthentication"]) == 1
        )


async def test_already_authenticated_calls_do_not_open_browser(config):
    transport = FakeTDLib()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, opened, _, _ = fixture_server(config, transport)
    async with Client(server) as client:
        assert (await client.call_tool("get_me")).structured_content["id"] == 7
        assert not opened


async def test_saved_app_credentials_restore_session_without_browser(config):
    config.save_credentials()
    transport = FakeTDLib()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, opened, _, _ = fixture_server(replace(config, api_id=0, api_hash=""), transport)
    async with Client(server) as client:
        assert (await client.call_tool("get_me")).structured_content["id"] == 7
        params = next(r for r in transport.requests if r["@type"] == "setTdlibParameters")
        assert params["api_id"] == config.api_id
        assert params["api_hash"] == config.api_hash
        assert not opened


async def test_account_call_reauthenticates_after_previous_login_expires(config):
    transport = AuthTransport()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, opened, pages, started = fixture_server(config, transport)
    async with Client(server) as client:
        first = asyncio.create_task(client.call_tool("get_me"))
        async with asyncio.timeout(5):
            await started.wait()
        await wait_stage(pages[0], "qr")
        transport.auth("authorizationStateReady")
        async with asyncio.timeout(5):
            assert (await first).structured_content["id"] == 7
        td = pages[0].session._client
        previous = td.authorization_state
        transport.auth("authorizationStateWaitPhoneNumber")
        async with asyncio.timeout(5):
            await td.wait_for_auth_change(previous)
        second = asyncio.create_task(client.call_tool("get_me"))
        await wait_stage(pages[0], "qr")
        transport.auth("authorizationStateReady")
        async with asyncio.timeout(5):
            assert (await second).structured_content["id"] == 7
        assert (
            len([r for r in transport.requests if r["@type"] == "requestQrCodeAuthentication"]) == 2
        )
        assert len(opened) == 1


async def test_timeout_keeps_page_available_and_retry_after_scan_works(config):
    transport = AuthTransport()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, opened, pages, _ = fixture_server(replace(config, login_timeout=0.05), transport)
    async with Client(server) as client:
        result = await client.call_tool("get_me")
        assert result.is_error
        assert pages[0].url in result.content[0].text
        assert (await http(pages[0]))[0] == 200
        transport.auth("authorizationStateReady")
        await wait_stage(pages[0], "ready")
        assert (await client.call_tool("get_me")).structured_content["id"] == 7
        assert len(opened) == 1


async def test_browser_routes_reject_cross_origin_and_unknown_auth_actions(config):
    session = TelegramSession(replace(config, api_id=0, api_hash=""))
    page = BrowserLogin(session, opener=lambda url: False)
    await page.start()
    try:
        assert (
            await http(
                page,
                "credentials",
                {"app_id": 1, "app_hash": "hash"},
                {"Origin": "https://other.example"},
            )
        )[0] == 403
        assert (await http(page, headers={"Host": "other.example"}))[0] == 403
        assert (await http(page, "sendMessage", {}))[0] == 404
        assert (await http(page, "password", {"value": "private"}))[0] == 400
    finally:
        await page.close()


async def test_bad_password_can_be_corrected_without_restarting_login(config):
    transport = AuthTransport()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    server, _, pages, started = fixture_server(config, transport)
    async with Client(server) as client:
        pending = asyncio.create_task(client.call_tool("get_me"))
        async with asyncio.timeout(5):
            await started.wait()
        page = pages[0]
        await wait_stage(page, "qr")
        transport.auth("authorizationStateWaitPassword")
        await wait_stage(page, "password")
        transport.responses["checkAuthenticationPassword"] = {
            "@type": "error",
            "code": 400,
            "message": "PASSWORD_HASH_INVALID",
        }
        status, body, _ = await http(page, "password", {"value": "wrong password"})
        assert status == 400 and b"wrong password" not in body
        assert not pending.done()
        del transport.responses["checkAuthenticationPassword"]
        assert (await http(page, "password", {"value": "right password"}))[0] == 200
        async with asyncio.timeout(5):
            assert (await pending).structured_content["id"] == 7
