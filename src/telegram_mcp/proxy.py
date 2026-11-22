from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from httpx2 import AsyncClient, Timeout
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .backend import ensure_backend
from .config import Config


@asynccontextmanager
async def backend_transport(endpoint: dict, read_seconds: float):
    async with AsyncClient(
        headers={"Authorization": "Bearer " + endpoint["token"]},
        timeout=Timeout(read_seconds, connect=10),
    ) as http:
        async with streamable_http_client(endpoint["url"], http_client=http) as streams:
            yield streams


async def run_proxy(config: Config) -> None:
    endpoint = await asyncio.to_thread(ensure_backend, config)
    timeout = config.login_timeout + config.request_timeout + 30
    async with Client(
        backend_transport(endpoint, timeout), read_timeout_seconds=timeout
    ) as backend:
        catalog = await backend.list_tools()
        schemas = {tool.name: tool.input_schema for tool in catalog.tools}

        async def list_tools(ctx, params):
            return catalog

        async def call_tool(ctx, params):
            return await backend.call_tool(params.name, params.arguments, meta=params.meta)

        server = Server(
            "telegram",
            version="0.1.0",
            instructions=backend.instructions,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
            get_tool_input_schema=schemas.get,
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
