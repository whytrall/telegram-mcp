import asyncio
from dataclasses import replace

from mcp import Client

from telegram_mcp.client import TDLibClient
from telegram_mcp.server import create_server
from telegram_mcp.session import TelegramSession

from .fakes import FakeBrowser, FakeTDLib
from .test_browser_auth import AuthTransport


class TrackedBrowser(FakeBrowser):
    def __init__(self, session):
        super().__init__(session)
        self.closed = False

    async def close(self):
        self.closed = True


async def wait_for_request(transport, method):
    async with asyncio.timeout(5):
        while True:
            transport.requested.clear()
            if any(r["@type"] == method for r in transport.requests):
                return
            await transport.requested.wait()


async def test_mcp_logout_revokes_once_preserves_credentials_and_does_not_open_browser(config):
    config.save_credentials()
    credentials = (config.data_dir / "app-credentials.json").read_bytes()
    transport = FakeTDLib()
    pages = []

    def browser(session):
        page = TrackedBrowser(session)
        pages.append(page)
        return page

    server = create_server(config, lambda c: TDLibClient(c, transport), browser)
    async with Client(server) as client:
        assert (await client.call_tool("telegram_status")).structured_content["authorized"]
        result = await client.call_tool("logout")
        assert not result.is_error
        assert result.structured_content == {"logged_out": True, "authorized": False}
        assert transport.released
        status = (await client.call_tool("telegram_status")).structured_content
        assert not status["authorized"] and status["login_url"] is None
        assert not (await client.call_tool("logout")).is_error
        assert [r["@type"] for r in transport.requests].count("logOut") == 1
        assert not pages
        assert (config.data_dir / "app-credentials.json").read_bytes() == credentials


async def test_read_after_logout_starts_fresh_browser_login(config):
    original = FakeTDLib()
    fresh = AuthTransport()
    fresh.responses["getMe"] = {"@type": "user", "id": 7}
    transports = iter((original, fresh))
    pages = []

    def browser(session):
        page = TrackedBrowser(session)
        pages.append(page)
        return page

    server = create_server(config, lambda c: TDLibClient(c, next(transports)), browser)
    async with Client(server) as client:
        await client.call_tool("logout")
        pending = asyncio.create_task(client.call_tool("get_me"))
        await wait_for_request(fresh, "requestQrCodeAuthentication")
        assert len(pages) == 1 and not pending.done()
        fresh.auth("authorizationStateReady")
        async with asyncio.timeout(5):
            assert (await pending).structured_content["id"] == 7
    assert original.released and fresh.released


async def test_logout_during_qr_login_closes_browser_and_aborts_waiting_read(config):
    transport = AuthTransport()
    pages = []

    def browser(session):
        page = TrackedBrowser(session)
        pages.append(page)
        return page

    server = create_server(config, lambda c: TDLibClient(c, transport), browser)
    async with Client(server) as client:
        pending = asyncio.create_task(client.call_tool("get_me"))
        await wait_for_request(transport, "requestQrCodeAuthentication")
        assert not (await client.call_tool("logout")).is_error
        async with asyncio.timeout(5):
            result = await pending
        assert result.is_error and "logged out" in result.content[0].text
        assert len(pages) == 1 and pages[0].closed
        assert transport.released
        assert "getMe" not in {r["@type"] for r in transport.requests}


async def test_logout_waits_for_closed_even_when_caller_disconnects(config):
    transport = FakeTDLib()
    transport.held_methods.add("logOut")
    session = TelegramSession(config, lambda c: TDLibClient(c, transport), TrackedBrowser)
    await session.start()
    try:
        first = asyncio.create_task(session.logout())
        second = asyncio.create_task(session.logout())
        await wait_for_request(transport, "logOut")
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        request = next(r for r in transport.requests if r["@type"] == "logOut")
        transport.respond(request, {"@type": "ok"})
        await asyncio.sleep(0)
        assert not second.done() and not transport.released
        transport.closed = True
        transport.emit(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateClosed"},
            }
        )
        async with asyncio.timeout(5):
            assert (await second)["logged_out"]
        assert transport.released
        assert [r["@type"] for r in transport.requests].count("logOut") == 1
    finally:
        await session.close()


async def test_logout_error_does_not_report_success_or_close_authorized_session(config):
    transport = FakeTDLib()
    transport.responses["logOut"] = {"@type": "error", "code": 500, "message": "Try again"}
    server = create_server(config, lambda c: TDLibClient(c, transport))
    async with Client(server) as client:
        result = await client.call_tool("logout")
        assert result.is_error and "Try again" in result.content[0].text
        assert not transport.released
        assert (await client.call_tool("telegram_status")).structured_content["authorized"]
        del transport.responses["logOut"]
        assert not (await client.call_tool("logout")).is_error


async def test_logout_without_credentials_needs_no_native_library(config):
    def unexpected_client(config):
        raise AssertionError("Logout must not initialize TDLib")

    server = create_server(replace(config, api_id=0, api_hash=""), unexpected_client)
    async with Client(server) as client:
        assert (await client.call_tool("logout")).structured_content["logged_out"]
