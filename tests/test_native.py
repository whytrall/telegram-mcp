import asyncio
import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from telegram_mcp.backend import stop_backend
from telegram_mcp.config import Config
from telegram_mcp.native import NativeTDLib

NATIVE_LIBRARY = os.getenv("TDLIB_LIBRARY_PATH") or NativeTDLib.bundled_library()


@pytest.mark.skipif(
    not NATIVE_LIBRARY, reason="No bundled TDLib; set TDLIB_LIBRARY_PATH for native smoke test"
)
async def test_real_tdlib_load_receive_and_graceful_close():
    native = NativeTDLib(os.getenv("TDLIB_LIBRARY_PATH"))
    native.send({"@type": "getOption", "name": "version", "@extra": "version"})
    version = None
    async with asyncio.timeout(15):
        while version is None:
            event = await asyncio.to_thread(native.receive, 0.2)
            if event and event.get("@extra") == "version":
                version = event
    assert version["@type"] == "optionValueString"
    assert version["value"]
    native.send({"@type": "close"})
    async with asyncio.timeout(15):
        while True:
            event = await asyncio.to_thread(native.receive, 0.2)
            if (
                event
                and event.get("authorization_state", {}).get("@type") == "authorizationStateClosed"
            ):
                break
    native.release()


@pytest.mark.skipif(
    not NATIVE_LIBRARY, reason="No bundled TDLib; set TDLIB_LIBRARY_PATH for native stdio test"
)
async def test_real_stdio_subprocess_initializes_and_reads_status(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telegram_mcp", "serve"],
        env={
            "TDLIB_LIBRARY_PATH": os.getenv("TDLIB_LIBRARY_PATH", ""),
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "0" * 32,  # Deliberately invalid test credentials.
            "TELEGRAM_DATA_DIR": str(tmp_path / "native-session"),
            "TELEGRAM_DATABASE_KEY": "",
        },
    )
    try:
        async with Client(params, mode="legacy", read_timeout_seconds=45) as client:
            # Codex launches a separate stdio process for each chat. Both must
            # coexist while only the shared backend owns the native database.
            async with Client(params, read_timeout_seconds=45) as second:
                assert len((await client.list_tools()).tools) == 12
                one, two = await asyncio.gather(
                    client.call_tool("telegram_status"),
                    second.call_tool("telegram_status"),
                )
                assert one.structured_content["read_only"] is True
                assert two.structured_content["authorized"] is False
            assert (await client.call_tool("telegram_status")).structured_content[
                "authorized"
            ] is False
    finally:
        await asyncio.to_thread(stop_backend, Config(data_dir=tmp_path / "native-session"))


async def test_stdio_server_discovers_tools_without_app_credentials_or_native_library(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telegram_mcp", "serve"],
        env={
            "TELEGRAM_API_ID": "",
            "TELEGRAM_API_HASH": "",
            "TELEGRAM_DATA_DIR": str(tmp_path / "empty-session"),
            "TDLIB_LIBRARY_PATH": "/not/installed/libtdjson.so",
        },
    )
    try:
        async with Client(params, read_timeout_seconds=45) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 12
            assert all(tool.annotations.read_only_hint == (tool.name != "logout") for tool in tools)
            assert "telegram_login_qr" not in {tool.name for tool in tools}
            status = (await client.call_tool("telegram_status")).structured_content
            assert status["authorized"] is False
            assert status["authorization_state"] == "authorizationStateWaitAppCredentials"
    finally:
        await asyncio.to_thread(stop_backend, Config(data_dir=tmp_path / "empty-session"))
