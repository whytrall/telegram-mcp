import asyncio
import os
import signal
import socket
import sys
from contextlib import asynccontextmanager

import uvicorn
from httpx2 import AsyncClient
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from telegram_mcp.backend import backend_app, read_endpoint, stop_backend
from telegram_mcp.client import TDLibClient
from telegram_mcp.config import Config
from telegram_mcp.proxy import backend_transport
from telegram_mcp.server import create_server

from .fakes import FakeTDLib


@asynccontextmanager
async def running_backend(config, transport, idle_seconds):
    server = create_server(config, lambda c: TDLibClient(c, transport))
    shutdown = asyncio.Event()
    token = "test-only-idle-backend-token-0123456789"
    app = backend_app(server, token, shutdown.set, idle_timeout=idle_seconds)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    endpoint = {"url": f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", "token": token}
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(runner.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if runner.started:
                break
            await asyncio.sleep(0.01)
        assert runner.started
        yield endpoint, shutdown
    finally:
        runner.should_exit = True
        await task
        listener.close()


async def test_idle_waits_for_active_read_and_ignores_health_and_open_streams(config):
    transport = FakeTDLib()
    transport.held_methods.add("getMe")
    async with running_backend(config, transport, 0.3) as (endpoint, shutdown):
        async with Client(backend_transport(endpoint, 10)) as client:
            pending = asyncio.create_task(client.call_tool("get_me"))
            async with asyncio.timeout(5):
                while True:
                    transport.requested.clear()
                    requests = [r for r in transport.requests if r["@type"] == "getMe"]
                    if requests:
                        break
                    await transport.requested.wait()
            await asyncio.sleep(0.6)
            assert not shutdown.is_set() and not pending.done()
            transport.respond(requests[0], {"@type": "user", "id": 7})
            assert (await pending).structured_content["id"] == 7
            async with AsyncClient(
                headers={"Authorization": "Bearer " + endpoint["token"]}
            ) as http:
                for _ in range(5):
                    await http.get(endpoint["url"].removesuffix("/mcp") + "/health")
                    await asyncio.sleep(0.1)
            assert shutdown.is_set()
    assert transport.released
    assert "logOut" not in {r["@type"] for r in transport.requests}


async def test_idle_timer_starts_without_clients_and_zero_disables_it(config):
    transport = FakeTDLib()
    async with running_backend(config, transport, 0.05) as (_, shutdown):
        async with asyncio.timeout(2):
            await shutdown.wait()
    assert transport.released
    transport = FakeTDLib()
    async with running_backend(config, transport, 0) as (_, shutdown):
        await asyncio.sleep(0.15)
        assert not shutdown.is_set()
    assert transport.released


async def test_pending_upload_prevents_idle_shutdown_after_send_returns(config):
    transport = FakeTDLib()
    transport.responses["sendMessage"] = {
        "@type": "message",
        "id": 100,
        "chat_id": 42,
        "sending_state": {"@type": "messageSendingStatePending", "sending_id": 0},
    }
    async with running_backend(config, transport, 0.05) as (endpoint, shutdown):
        async with Client(backend_transport(endpoint, 10)) as client:
            result = await client.call_tool(
                "send_message", {"chat_id": 42, "text": "Hi", "confirmed": True}
            )
            assert result.structured_content["status"] == "pending"
            await asyncio.sleep(0.15)
            assert not shutdown.is_set()
            transport.emit(
                {
                    "@type": "updateMessageSendSucceeded",
                    "old_message_id": 100,
                    "message": {"@type": "message", "id": 200, "chat_id": 42},
                }
            )
            async with asyncio.timeout(2):
                await shutdown.wait()
    assert transport.released


async def test_existing_stdio_connection_restarts_backend_after_idle_shutdown(tmp_path):
    config = Config(data_dir=tmp_path / "idle-session")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telegram_mcp", "serve"],
        env={
            "TELEGRAM_DATA_DIR": str(config.data_dir),
            "TELEGRAM_API_ID": "",
            "TELEGRAM_API_HASH": "",
            "TELEGRAM_IDLE_TIMEOUT": "0.6",
        },
    )
    try:
        async with Client(params, read_timeout_seconds=45) as client:
            assert len((await client.list_tools()).tools) == 13
            await client.call_tool("telegram_status")
            first = read_endpoint(config)
            assert first is not None
            for _ in range(200):
                if read_endpoint(config) is None:
                    break
                await asyncio.sleep(0.05)
            assert read_endpoint(config) is None
            status = (await client.call_tool("telegram_status")).structured_content
            assert not status["authorized"]
            second = read_endpoint(config)
            assert second is not None and second["pid"] != first["pid"]
            # A crash leaves backend.json behind; the proxy must still restart it.
            os.kill(second["pid"], signal.SIGKILL)
            await client.call_tool("telegram_status")
            third = read_endpoint(config)
            assert third is not None and third["pid"] != second["pid"]
            assert len((await client.list_tools()).tools) == 13
    finally:
        await asyncio.to_thread(stop_backend, config)
