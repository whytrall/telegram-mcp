from dataclasses import replace

import pytest
from mcp import Client

from telegram_mcp.client import TDLibClient
from telegram_mcp.server import create_server
from telegram_mcp.telegram import READ_METHODS

from .fakes import FakeBrowser, FakeTDLib, message


def server_for(config, transport):
    return create_server(config, lambda c: TDLibClient(c, transport), FakeBrowser)


def pending_message(message_id=100):
    return {
        **message(message_id),
        "is_outgoing": True,
        "sending_state": {"@type": "messageSendingStatePending", "sending_id": 0},
    }


@pytest.mark.parametrize("confirmation", [None, False, "true", 1])
async def test_send_requires_explicit_confirmation_before_authentication(config, confirmation):
    transport = FakeTDLib(authorized=False)
    server = server_for(config, transport)
    async with Client(server) as client:
        arguments = {"chat_id": 42, "text": "Hello"}
        if confirmation is not None:
            arguments["confirmed"] = confirmation
        result = await client.call_tool("send_message", arguments)
        assert result.is_error
        assert not server.telegram_session.login_url
        assert {r["@type"] for r in transport.requests} == {
            "getAuthorizationState",
            "setTdlibParameters",
        }


async def test_send_description_and_annotations_require_confirmation(config):
    async with Client(server_for(config, FakeTDLib())) as client:
        tools = (await client.list_tools()).tools
        send = next(t for t in tools if t.name == "send_message")
        assert "DO NOT use this tool without explicit user confirmation" in send.description
        assert send.annotations.read_only_hint is False
        assert send.annotations.destructive_hint is True
        assert send.annotations.idempotent_hint is False
        status = (await client.call_tool("telegram_status")).structured_content
        assert status["read_only"] is False
        assert status["sending_requires_confirmation"] is True
        assert "sendMessage" not in READ_METHODS and "sendMessageAlbum" not in READ_METHODS


async def test_send_plain_text_to_topic_with_reply_preserves_draft(config):
    transport = FakeTDLib()
    transport.responses["sendMessage"] = pending_message()
    server = server_for(config, transport)
    async with Client(server) as client:
        result = await client.call_tool(
            "send_message",
            {
                "chat_id": 42,
                "text": "Hello 🌍",
                "confirmed": True,
                "forum_topic_id": 1234,
                "reply_to_message_id": 99,
            },
        )
        assert not result.is_error
        assert result.structured_content["status"] == "pending"
        assert result.structured_content["automatic_retry_safe"] is False
        assert "@extra" not in result.structured_content["messages"][0]
        request = transport.requests[-1]
        assert request["@type"] == "sendMessage"
        assert request["chat_id"] == 42
        assert request["topic_id"] == {"@type": "messageTopicForum", "forum_topic_id": 1234}
        assert request["reply_to"] == {"@type": "inputMessageReplyToMessage", "message_id": 99}
        assert request["input_message_content"] == {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText", "text": "Hello 🌍", "entities": []},
            "clear_draft": False,
        }
        assert request["options"]["paid_message_star_count"] == 0
        assert server.telegram_session.has_pending_sends


@pytest.mark.parametrize(
    ("kind", "message_type", "media_key", "media_type"),
    [
        ("photo", "inputMessagePhoto", "photo", "inputPhoto"),
        ("video", "inputMessageVideo", "video", "inputVideo"),
        ("file", "inputMessageDocument", "document", "inputDocument"),
    ],
)
async def test_send_attachment_uses_pinned_tdlib_nested_media_schema(
    config,
    tmp_path,
    kind,
    message_type,
    media_key,
    media_type,
):
    path = tmp_path / f"attachment with spaces.{kind}"
    path.write_bytes(b"synthetic test content")
    transport = FakeTDLib()
    transport.responses["sendMessage"] = pending_message()
    async with Client(server_for(config, transport)) as client:
        result = await client.call_tool(
            "send_message",
            {
                "chat_id": 42,
                "text": "Caption",
                "confirmed": True,
                "attachments": [
                    {"type": kind, "path": str(path), "width": 640, "height": 480, "duration": 2}
                ],
            },
        )
        assert not result.is_error
        content = transport.requests[-1]["input_message_content"]
        assert content["@type"] == message_type
        assert content["caption"]["text"] == "Caption"
        media = content[media_key]
        assert media["@type"] == media_type
        assert media[media_key] == {"@type": "inputFileLocal", "path": str(path)}
        if kind == "file":
            assert media["disable_content_type_detection"] is True
        else:
            assert (media["width"], media["height"]) == (640, 480)
        if kind == "video":
            assert media["duration"] == 2 and media["supports_streaming"] is True


@pytest.mark.parametrize("kinds", [["photo", "video"], ["file", "file"]])
async def test_send_media_and_document_albums(config, tmp_path, kinds):
    attachments = []
    for i, kind in enumerate(kinds):
        path = tmp_path / f"attachment-{i}"
        path.write_bytes(b"synthetic test content")
        attachments.append({"type": kind, "path": str(path)})
    transport = FakeTDLib()
    transport.responses["sendMessageAlbum"] = {
        "@type": "messages",
        "messages": [pending_message(100), pending_message(101)],
    }
    server = server_for(config, transport)
    async with Client(server) as client:
        result = await client.call_tool(
            "send_message",
            {
                "chat_id": 42,
                "text": "Album caption",
                "confirmed": True,
                "attachments": attachments,
            },
        )
        assert not result.is_error
        assert len(result.structured_content["messages"]) == 2
        request = transport.requests[-1]
        assert request["@type"] == "sendMessageAlbum"
        assert [c["caption"]["text"] for c in request["input_message_contents"]] == [
            "Album caption",
            "",
        ]
        assert server.telegram_session.has_pending_sends
        assert sum(r["@type"].startswith("sendMessage") for r in transport.requests) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": "  "},
        {"text": "x" * 4097},
        {"attachments": [{"type": "photo", "path": "relative.jpg"}]},
        {"attachments": [{"type": "photo", "path": "https://example.com/photo.jpg"}]},
        {"attachments": [{"type": "unknown", "path": "/test"}]},
        {"attachments": [{"type": "file", "path": "/test", "raw_tdlib": {}}]},
        {"attachments": [{"type": "file", "path": "/test"}] * 11},
        {"text": "Hi", "forum_topic_id": 0},
        {"text": "Hi", "reply_to_message_id": -1},
    ],
)
async def test_invalid_send_input_makes_no_write_requests(config, arguments):
    transport = FakeTDLib()
    async with Client(server_for(config, transport)) as client:
        result = await client.call_tool(
            "send_message", {"chat_id": 42, "confirmed": True, **arguments}
        )
        assert result.is_error
        assert not any(r["@type"].startswith("sendMessage") for r in transport.requests)


async def test_all_files_validated_before_any_send(config, tmp_path):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"synthetic test content")
    transport = FakeTDLib()
    async with Client(server_for(config, transport)) as client:
        for attachments, text in [
            (
                [
                    {"type": "photo", "path": str(good)},
                    {"type": "photo", "path": str(tmp_path / "missing")},
                ],
                "",
            ),
            ([{"type": "file", "path": str(good)}, {"type": "photo", "path": str(good)}], ""),
            ([{"type": "photo", "path": str(tmp_path)}], ""),
            ([{"type": "photo", "path": str(good)}], "x" * 1025),
        ]:
            result = await client.call_tool(
                "send_message",
                {
                    "chat_id": 42,
                    "confirmed": True,
                    "attachments": attachments,
                    "text": text,
                },
            )
            assert result.is_error
        assert not any(r["@type"].startswith("sendMessage") for r in transport.requests)


async def test_send_timeout_is_ambiguous_and_never_retried(config):
    transport = FakeTDLib()
    transport.held_methods.add("sendMessage")
    async with Client(server_for(replace(config, request_timeout=0.05), transport)) as client:
        result = await client.call_tool(
            "send_message", {"chat_id": 42, "text": "Hi", "confirmed": True}
        )
        assert result.is_error
        assert (
            "unknown" in result.content[0].text
            and "Do not retry automatically" in result.content[0].text
        )
        assert sum(r["@type"] == "sendMessage" for r in transport.requests) == 1


async def test_send_rate_limit_is_reported_without_retry(config):
    transport = FakeTDLib()
    transport.responses["sendMessage"] = {"@type": "error", "code": 429, "message": "FLOOD_WAIT_3"}
    async with Client(server_for(config, transport)) as client:
        result = await client.call_tool(
            "send_message", {"chat_id": 42, "text": "Hi", "confirmed": True}
        )
        assert result.is_error and "FLOOD_WAIT_3" in result.content[0].text
        assert sum(r["@type"] == "sendMessage" for r in transport.requests) == 1


@pytest.mark.parametrize("update", ["updateMessageSendSucceeded", "updateMessageSendFailed"])
@pytest.mark.parametrize("response_first", [True, False])
async def test_pending_sends_clear_even_when_terminal_update_precedes_response(
    config, update, response_first
):
    transport = FakeTDLib()
    async with TDLibClient(config, transport) as client:
        response = pending_message()
        terminal = {"@type": update, "old_message_id": 100, "message": message(200)}
        first, second = (response, terminal) if response_first else (terminal, response)
        client._on_event(first)
        assert client.has_pending_sends is response_first
        client._on_event(second)
        assert not client.has_pending_sends
