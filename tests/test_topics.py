from mcp import Client

from telegram_mcp.client import TDLibClient
from telegram_mcp.server import create_server

from .fakes import FakeTDLib, message


async def test_history_stays_scoped_to_requested_bot_topic(config):
    transport = FakeTDLib()
    entry = {
        **message(100, "Topic-specific message"),
        "topic_id": {
            "@type": "messageTopicForum",
            "forum_topic_id": 1234,
        },
    }
    transport.responses["getForumTopicHistory"] = {"@type": "messages", "messages": [entry]}
    async with Client(create_server(config, lambda c: TDLibClient(c, transport))) as client:
        result = await client.call_tool(
            "get_history",
            {
                "chat_id": 42,
                "forum_topic_id": 1234,
                "limit": 5,
                "before_message_id": 101,
            },
        )
        assert not result.is_error
        data = result.structured_content
        assert data["messages"][0]["content"]["text"]["text"] == "Topic-specific message"
        assert data["messages"][0]["topic_id"]["forum_topic_id"] == 1234
        assert data["next_before_message_id"] == 100
        request = transport.requests[-1]
        assert request["@type"] == "getForumTopicHistory"
        assert request["chat_id"] == 42
        assert request["forum_topic_id"] == 1234
        assert request["from_message_id"] == 101
        assert request["limit"] == 5
        assert "only_local" not in request
        assert "getChatHistory" not in {r["@type"] for r in transport.requests}
        invalid = await client.call_tool(
            "get_history",
            {
                "chat_id": 42,
                "forum_topic_id": 0,
            },
        )
        assert invalid.is_error


async def test_bot_sidebar_topics_and_pagination_through_mcp(config):
    transport = FakeTDLib()
    transport.responses["getForumTopics"] = {
        "@type": "forumTopics",
        "total_count": 2,
        "topics": [
            {
                "info": {
                    "forum_topic_id": 1234,
                    "name": "Пример темы",
                    "is_general": False,
                    "is_closed": False,
                    "is_hidden": False,
                },
                "last_message": message(),
                "is_pinned": True,
                "unread_count": 3,
            }
        ],
        "next_offset_date": 1800000000,
        "next_offset_message_id": 100,
        "next_offset_forum_topic_id": 1234,
    }
    async with Client(create_server(config, lambda c: TDLibClient(c, transport))) as client:
        result = await client.call_tool("list_topics", {"chat_id": 42})
        assert not result.is_error
        data = result.structured_content
        assert data["topics"][0]["name"] == "Пример темы"
        assert data["topics"][0]["id"] == 1234
        assert data["topics"][0]["is_pinned"] is True
        assert data["topics"][0]["unread_count"] == 3
        assert "content" not in data["topics"][0]
        await client.call_tool(
            "list_topics",
            {
                "chat_id": 42,
                "query": "темы",
                "limit": 5,
                "offset_date": data["next_offset_date"],
                "offset_message_id": data["next_offset_message_id"],
                "offset_forum_topic_id": data["next_offset_forum_topic_id"],
            },
        )
        request = transport.requests[-1]
        assert request["@type"] == "getForumTopics"
        assert request["query"] == "темы"
        assert request["limit"] == 5
        assert request["offset_date"] == 1800000000
        assert request["offset_message_id"] == 100
        assert request["offset_forum_topic_id"] == 1234
        for arguments in ({"limit": 101}, {"offset_forum_topic_id": -1}):
            invalid = await client.call_tool("list_topics", {"chat_id": 42, **arguments})
            assert invalid.is_error
