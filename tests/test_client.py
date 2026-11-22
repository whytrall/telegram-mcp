import asyncio
import base64
from dataclasses import replace

import pytest

from telegram_mcp.client import RequestTimeout, TDLibClient, TDLibError

from .fakes import FakeTDLib


async def test_concurrent_responses_updates_and_shutdown(config):
    transport = FakeTDLib()
    client = TDLibClient(config, transport)
    async with client:
        assert client.ready
        assert len([r for r in transport.requests if r["@type"] == "setTdlibParameters"]) == 1
        transport.held_methods.add("getUser")
        first = asyncio.create_task(client.request("getUser", user_id=1))
        second = asyncio.create_task(client.request("getUser", user_id=2))
        await asyncio.sleep(0)
        requests = [r for r in transport.requests if r["@type"] == "getUser"]
        assert len(requests) == 2
        transport.respond(requests[1], {"@type": "user", "id": 2})
        transport.emit({"@type": "updateNewMessage", "message": {"id": 999}})
        transport.respond(requests[0], {"@type": "user", "id": 1})
        assert (await first)["id"] == 1
        assert (await second)["id"] == 2
        assert not client._pending
    assert transport.released
    assert client._thread is None


async def test_timeout_cancellation_late_response_and_error(config):
    transport = FakeTDLib()
    client = TDLibClient(replace(config, request_timeout=0.1), transport)
    async with client:
        transport.held_methods.add("getUser")
        with pytest.raises(RequestTimeout, match="getUser timed out"):
            await client.request("getUser", user_id=1)
        assert not client._pending
        stale = transport.requests[-1]
        task = asyncio.create_task(client.request("getUser", user_id=2))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        transport.respond(stale, {"@type": "user", "id": 1})
        transport.responses["getChat"] = {"@type": "error", "code": 429, "message": "FLOOD_WAIT_3"}
        with pytest.raises(TDLibError, match="429: FLOOD_WAIT_3"):
            await client.request("getChat", chat_id=1)
        assert not client._pending


async def test_initialization_failure_closes_and_releases_session(config):
    transport = FakeTDLib()
    transport.responses["setTdlibParameters"] = {
        "@type": "error",
        "code": 400,
        "message": "Invalid parameters",
    }
    with pytest.raises(TDLibError, match="Invalid parameters"):
        await TDLibClient(config, transport).start()
    assert transport.released
    async with TDLibClient(config, FakeTDLib()) as client:
        assert client.ready


async def test_session_lock_blocks_second_process_owner(config):
    async with TDLibClient(config, FakeTDLib()):
        second = TDLibClient(config, FakeTDLib())
        with pytest.raises(RuntimeError, match="already in use"):
            await second.start()


async def test_private_directories_and_database_key(config):
    key = "correct horse 🔑"
    transport = FakeTDLib(authorized=False)
    async with TDLibClient(replace(config, database_key=key), transport) as client:
        assert not client.ready
        params = next(r for r in transport.requests if r["@type"] == "setTdlibParameters")
        assert base64.b64decode(params["database_encryption_key"]).decode() == key
        assert params["use_secret_chats"] is False
        assert (config.data_dir.stat().st_mode & 0o777) == 0o700
        assert ((config.data_dir / "database").stat().st_mode & 0o777) == 0o700


def test_406_error_does_not_display_message():
    error = TDLibError(406, "MUST_NOT_DISPLAY")
    assert str(error) == "TDLib error 406"


async def test_auth_change_during_a_request(config):
    transport = FakeTDLib(authorized=False)
    async with TDLibClient(config, transport) as client:
        previous = client.authorization_state
        transport.emit(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {
                    "@type": "authorizationStateReady",
                },
            }
        )
        assert (await client.wait_for_auth_change(previous))["@type"] == "authorizationStateReady"
        assert client.ready
