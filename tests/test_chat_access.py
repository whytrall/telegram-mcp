import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from telegram_mcp.backend import ensure_backend, stop_backend
from telegram_mcp.client import TDLibClient
from telegram_mcp.config import Config
from telegram_mcp.server import create_server

from .fakes import FakeTDLib, message
from .test_idle import running_backend


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", frozenset()),
        ("  ", frozenset()),
        ("42,-1001234567890,42", frozenset({42, -1001234567890})),
    ],
)
def test_allowlist_environment_distinguishes_unset_and_empty(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", value)
    assert Config.from_env().allowed_chat_ids == expected


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "42,",
        "42,,43",
        "@username",
        "42 43",
        "null",
        "*",
        "1.0",
        "9007199254740992",
        "-9007199254740992",
    ],
)
def test_invalid_allowlist_fails_closed(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", value)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_CHAT_IDS"):
        Config.from_env()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_chat", {"chat_id": 99}),
        ("get_history", {"chat_id": 99}),
        ("get_history", {"chat_id": 99, "forum_topic_id": 1234}),
        ("get_message", {"chat_id": 99, "message_id": 100}),
        ("list_topics", {"chat_id": 99}),
        ("search_messages", {"chat_id": 99, "query": "hidden"}),
        ("search_messages", {"query": "hidden"}),
        ("get_user", {"user_id": 99}),
        ("resolve_username", {"username": "example"}),
        ("send_message", {"chat_id": 99, "text": "Hi", "confirmed": True}),
        (
            "send_message",
            {
                "chat_id": 99,
                "confirmed": True,
                "attachments": [{"type": "file", "path": "/unreadable-file"}],
            },
        ),
    ],
)
async def test_denied_calls_never_reach_tdlib(config, tool, arguments):
    config = replace(config, allowed_chat_ids=frozenset({42}))
    transport = FakeTDLib()
    server = create_server(config, lambda c: TDLibClient(c, transport))
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
        assert result.is_error
        assert (
            "not allowed" in result.content[0].text or "Chat restrictions" in result.content[0].text
        )
        assert {r["@type"] for r in transport.requests} == {
            "getAuthorizationState",
            "setTdlibParameters",
        }


async def test_allowed_reads_search_and_send_work_and_foreign_previews_are_hidden(config):
    config = replace(config, allowed_chat_ids=frozenset({42}))
    visible = {**message(), "reply_to": {"chat_id": 99, "quote": {"text": "hidden preview"}}}
    hidden = {**message(99, "hidden message"), "chat_id": 99}
    transport = FakeTDLib()
    transport.responses.update(
        {
            "getMe": {"@type": "user", "id": 7},
            "getChat": {"@type": "chat", "id": 42, "title": "Allowed", "last_message": visible},
            "getChatHistory": {"@type": "messages", "messages": [visible, hidden]},
            "getMessage": visible,
            "searchChatMessages": {
                "@type": "foundChatMessages",
                "total_count": 1,
                "messages": [visible],
                "next_from_message_id": 0,
            },
            "sendMessage": visible,
        }
    )
    server = create_server(config, lambda c: TDLibClient(c, transport))
    async with Client(server) as client:
        status = (await client.call_tool("telegram_status")).structured_content
        assert status["allowed_chat_ids"] == [42]
        assert not (await client.call_tool("get_me")).is_error
        for tool, args in [
            ("get_chat", {"chat_id": 42}),
            ("get_history", {"chat_id": 42}),
            ("get_message", {"chat_id": 42, "message_id": 100}),
            ("search_messages", {"chat_id": 42, "query": "hello"}),
            ("send_message", {"chat_id": 42, "text": "Hi", "confirmed": True}),
        ]:
            result = await client.call_tool(tool, args)
            assert not result.is_error
            assert "hidden" not in json.dumps(result.structured_content)
        assert (await client.call_tool("get_history", {"chat_id": 42})).structured_content[
            "messages"
        ][0]["id"] == 100
        transport.responses["getMessage"] = hidden
        assert (await client.call_tool("get_message", {"chat_id": 42, "message_id": 100})).is_error


async def test_chat_discovery_filters_ids_before_fetching_metadata(config):
    config = replace(config, allowed_chat_ids=frozenset({42}))
    transport = FakeTDLib()
    transport.responses.update(
        {
            "getChats": {"@type": "chats", "chat_ids": [99, 42]},
            "searchChatsOnServer": {"@type": "chats", "chat_ids": [99, 42]},
            "getChat": {"@type": "chat", "id": 42, "title": "Allowed"},
        }
    )
    async with Client(create_server(config, lambda c: TDLibClient(c, transport))) as client:
        for tool, args in [("list_chats", {}), ("search_chats", {"query": "example"})]:
            result = await client.call_tool(tool, args)
            assert [c["id"] for c in result.structured_content["chats"]] == [42]
    assert [r["chat_id"] for r in transport.requests if r["@type"] == "getChat"] == [42, 42]


async def test_empty_allowlist_denies_without_starting_login(config):
    config = replace(config, api_id=0, api_hash="", allowed_chat_ids=frozenset())
    transport = FakeTDLib()
    server = create_server(config, lambda c: TDLibClient(c, transport))
    async with Client(server) as client:
        for tool, args in [("list_chats", {}), ("search_chats", {"query": "example"})]:
            assert (await client.call_tool(tool, args)).structured_content == {"chats": []}
        assert (await client.call_tool("get_history", {"chat_id": 42})).is_error
        assert (
            await client.call_tool("send_message", {"chat_id": 42, "text": "Hi", "confirmed": True})
        ).is_error
        assert not server.telegram_session.login_url
        assert not transport.requests


@pytest.mark.parametrize("existing_policy", [None, frozenset({42})])
async def test_shared_backend_rejects_clients_with_different_permissions(config, existing_policy):
    config = replace(config, allowed_chat_ids=existing_policy)
    config.prepare_directories()
    async with running_backend(config, FakeTDLib(), 0) as (endpoint, _):
        (config.data_dir / "backend.json").write_text(json.dumps({**endpoint, "pid": os.getpid()}))
        assert await asyncio.to_thread(ensure_backend, config)
        mismatch = replace(
            config, allowed_chat_ids=frozenset() if existing_policy is None else None
        )
        with pytest.raises(RuntimeError, match="different chat allowlist"):
            await asyncio.to_thread(ensure_backend, mismatch)


@pytest.mark.parametrize("value", ["42,-1001234567890", ""])
async def test_allowlist_propagates_through_stdio_to_backend(tmp_path, value):
    config = Config(
        data_dir=tmp_path / "restricted-session",
        allowed_chat_ids=Config.parse_allowed_chat_ids(value),
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telegram_mcp", "serve"],
        env={
            "TELEGRAM_API_ID": "",
            "TELEGRAM_API_HASH": "",
            "TELEGRAM_DATA_DIR": str(config.data_dir),
            "TELEGRAM_ALLOWED_CHAT_IDS": value,
        },
    )
    try:
        async with Client(params, read_timeout_seconds=45) as client:
            result = await client.call_tool("telegram_status")
            assert result.structured_content["allowed_chat_ids"] == config.chat_allowlist
    finally:
        await asyncio.to_thread(stop_backend, config)
