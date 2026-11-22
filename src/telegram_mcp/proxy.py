from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

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


@asynccontextmanager
async def backend_connection(config: Config):
    timeout = config.login_timeout + config.request_timeout + 30
    async with AsyncExitStack() as stack:
        for attempt in range(2):
            endpoint = await asyncio.to_thread(ensure_backend, config)
            try:
                backend = await stack.enter_async_context(
                    Client(backend_transport(endpoint, timeout), read_timeout_seconds=timeout)
                )
                break
            except Exception:
                # The idle timer can fire between the health probe and handshake.
                # Retry connection setup only; never replay an issued tool call.
                if attempt:
                    raise
        yield backend


async def run_proxy(config: Config) -> None:
    async with backend_connection(config) as backend:
        catalog = await backend.list_tools()
        instructions = backend.instructions
    schemas = {tool.name: tool.input_schema for tool in catalog.tools}

    async def list_tools(ctx, params):
        async with backend_connection(config) as backend:
            catalog = await backend.list_tools()
        schemas.clear()
        schemas.update({tool.name: tool.input_schema for tool in catalog.tools})
        return catalog

    async def call_tool(ctx, params):
        async with backend_connection(config) as backend:
            return await backend.call_tool(params.name, params.arguments, meta=params.meta)

    server = Server(
        "telegram",
        version="0.1.0",
        instructions=instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=schemas.get,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
