import asyncio
import socket

import uvicorn
from httpx2 import AsyncClient
from mcp import Client

from telegram_mcp.backend import backend_app
from telegram_mcp.client import TDLibClient
from telegram_mcp.proxy import backend_transport
from telegram_mcp.server import create_server

from .fakes import FakeTDLib


async def test_http_clients_share_one_tdlib_session_and_logout(config):
    transport = FakeTDLib()
    transport.responses["getMe"] = {"@type": "user", "id": 7}
    transport.responses["getChat"] = {"@type": "chat", "id": 42, "title": "Friends"}
    created = []

    def factory(conf):
        created.append(conf)
        return TDLibClient(conf, transport)

    server = create_server(config, factory)
    token = "test-only-transport-token-0123456789"
    app = backend_app(server, token, lambda: None)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    endpoint = {"url": f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", "token": token}
    runner = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    async with server.session_manager.run():
        task = asyncio.create_task(runner.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if runner.started:
                    break
                await asyncio.sleep(0.01)
            assert runner.started
            async with AsyncClient() as http:
                denied = await http.get(endpoint["url"].removesuffix("/mcp") + "/health")
                assert denied.status_code == 401
            async with Client(backend_transport(endpoint, 10), mode="legacy") as first:
                async with Client(backend_transport(endpoint, 10)) as second:
                    one, two = await asyncio.gather(
                        first.call_tool("get_me"),
                        second.call_tool("get_chat", {"chat_id": 42}),
                    )
                    assert one.structured_content["id"] == 7
                    assert two.structured_content["id"] == 42
                    assert len(created) == 1
                    assert not transport.released
                    tools = (await second.list_tools()).tools
                    assert len(tools) == 12
                    assert all(t.annotations.read_only_hint == (t.name != "logout") for t in tools)
                    result = await second.call_tool("logout")
                    assert result.structured_content == {"logged_out": True, "authorized": False}
                    assert transport.released
                    assert not (await first.call_tool("telegram_status")).structured_content[
                        "authorized"
                    ]
                assert not (await first.call_tool("telegram_status")).structured_content[
                    "authorized"
                ]
        finally:
            runner.should_exit = True
            await task
            listener.close()
    assert transport.released
