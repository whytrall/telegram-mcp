from dataclasses import replace

import pytest
from mcp import Client

from telegram_mcp.client import TDLibClient
from telegram_mcp.server import create_server
from telegram_mcp.telegram import READ_METHODS, Telegram

from .fakes import FakeBrowser, FakeTDLib, message

TOOL_NAMES = {
    "telegram_status",
    "logout",
    "get_me",
    "get_user",
    "get_chat",
    "list_topics",
    "list_chats",
    "search_chats",
    "resolve_username",
    "get_history",
    "get_message",
    "search_messages",
}


def make_server(config, transport):
    return create_server(config, lambda conf: TDLibClient(conf, transport))


async def test_real_mcp_client_lists_read_tools_and_logout_and_validates_limits(config):
    transport = FakeTDLib()
    transport.responses["getMe"] = {"@type": "user", "id": 7, "first_name": "Alex"}
    async with Client(make_server(config, transport)) as client:
        result = await client.list_tools()
        assert {t.name for t in result.tools} == TOOL_NAMES
        for tool in result.tools:
            assert tool.annotations.read_only_hint == (tool.name != "logout")
            assert tool.annotations.destructive_hint == (tool.name == "logout")
            assert "ctx" not in tool.input_schema.get("properties", {})
        status = await client.call_tool("telegram_status")
        assert status.structured_content["read_only"] is True
        me = await client.call_tool("get_me")
        assert me.structured_content["id"] == 7
        for limit in (0, 101):
            invalid = await client.call_tool("get_history", {"chat_id": 42, "limit": limit})
            assert invalid.is_error
        unknown = await client.call_tool("send_message", {"chat_id": 42, "text": "hi"})
        assert unknown.is_error
        removed = await client.call_tool("telegram_login_qr")
        assert removed.is_error
    assert transport.released


async def test_chat_history_and_search_are_read_only_through_mcp(config):
    transport = FakeTDLib()
    transport.responses.update(
        {
            "loadChats": {"@type": "error", "code": 404, "message": "All chats loaded"},
            "getChats": {"@type": "chats", "chat_ids": [42]},
            "searchChatsOnServer": {"@type": "chats", "chat_ids": [42]},
            "getChat": {"@type": "chat", "id": 42, "title": "Friends", "unread_count": 3},
            "getChatHistory": {"@type": "messages", "messages": [message(100), message(99)]},
            "getMessage": message(),
            "searchChatMessages": {
                "@type": "foundChatMessages",
                "total_count": 1,
                "messages": [message()],
                "next_from_message_id": 50,
            },
            "searchMessages": {
                "@type": "foundMessages",
                "total_count": 1,
                "messages": [message()],
                "next_offset": "opaque-cursor",
            },
        }
    )
    async with Client(make_server(config, transport)) as client:
        chats = await client.call_tool("list_chats", {"archive": True})
        assert chats.structured_content["chats"][0]["unread_count"] == 3
        history = await client.call_tool("get_history", {"chat_id": 42, "before_message_id": 100})
        assert [m["id"] for m in history.structured_content["messages"]] == [99]
        assert history.structured_content["next_before_message_id"] == 99
        search = await client.call_tool("search_messages", {"query": "hello"})
        assert search.structured_content["next_offset"] == "opaque-cursor"
        await client.call_tool("search_messages", {"query": "hello", "offset": "opaque-cursor"})
        request = transport.requests[-1]
        assert request["offset"] == "opaque-cursor"
        scoped = await client.call_tool("search_messages", {"query": "hello", "chat_id": 42})
        assert scoped.structured_content["next_from_message_id"] == 50
        fetched = await client.call_tool("get_message", {"chat_id": 42, "message_id": 100})
        assert fetched.structured_content["content"]["text"]["text"] == "hello"
    actual = {r["@type"] for r in transport.requests}
    assert actual <= READ_METHODS | {"getAuthorizationState", "setTdlibParameters", "close"}
    assert "viewMessages" not in actual


async def test_unauthorized_server_is_inspectable_but_reads_require_login(config):
    transport = FakeTDLib(authorized=False)
    server = create_server(
        replace(config, login_timeout=0.05),
        lambda c: TDLibClient(c, transport),
        FakeBrowser,
    )
    async with Client(server) as client:
        assert len((await client.list_tools()).tools) == 12
        status = await client.call_tool("telegram_status")
        assert status.structured_content["authorized"] is False
        result = await client.call_tool("get_me")
        assert result.is_error
        assert "login" in result.content[0].text
    assert "getMe" not in {r["@type"] for r in transport.requests}


async def test_service_rejects_writes_even_with_authenticated_client(config):
    transport = FakeTDLib()
    async with TDLibClient(config, transport) as client:
        service = Telegram(client)
        for method in (
            "logOut",
            "sendMessage",
            "editMessageText",
            "deleteMessages",
            "viewMessages",
            "setOption",
            "requestQrCodeAuthentication",
        ):
            with pytest.raises(ValueError, match="read operations"):
                await service._read(method)
        assert len(transport.requests) == 2
