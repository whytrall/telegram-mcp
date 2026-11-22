from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .client import TDLibClient, TDLibError

if TYPE_CHECKING:
    from .session import TelegramSession

# Account data operations use this allowlist. Browser authentication lives in
# the session manager; there is intentionally no raw request tool.
READ_METHODS = frozenset(
    {
        "getMe",
        "getUser",
        "getChat",
        "getChats",
        "loadChats",
        "searchChats",
        "searchChatsOnServer",
        "searchPublicChat",
        "getChatHistory",
        "getMessage",
        "searchChatMessages",
        "searchMessages",
        "getForumTopics",
        "getForumTopicHistory",
    }
)


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["photo", "video", "file"]
    path: str = Field(min_length=1, description="Absolute local path readable by the MCP server.")
    width: int = Field(default=0, ge=0, description="Photo/video width; 0 if unknown.")
    height: int = Field(default=0, ge=0, description="Photo/video height; 0 if unknown.")
    duration: int = Field(default=0, ge=0, description="Video duration in seconds; 0 if unknown.")

    def content(self, caption: str) -> dict:
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise ValueError("Attachment paths must be absolute local paths.")
        path = path.resolve()
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError(f"Attachment is not a readable regular file: {self.path}")
        file = {"@type": "inputFileLocal", "path": str(path)}
        result = {"caption": {"@type": "formattedText", "text": caption, "entities": []}}
        if self.type == "file":
            return {
                **result,
                "@type": "inputMessageDocument",
                "document": {
                    "@type": "inputDocument",
                    "document": file,
                    "disable_content_type_detection": True,
                },
            }
        if self.type == "photo":
            return {
                **result,
                "@type": "inputMessagePhoto",
                "photo": {
                    "@type": "inputPhoto",
                    "photo": file,
                    "width": self.width,
                    "height": self.height,
                    "added_sticker_file_ids": [],
                },
            }
        return {
            **result,
            "@type": "inputMessageVideo",
            "video": {
                "@type": "inputVideo",
                "video": file,
                "width": self.width,
                "height": self.height,
                "duration": self.duration,
                "supports_streaming": True,
                "added_sticker_file_ids": [],
            },
        }


def clean(value: Any) -> Any:
    """Drop routing metadata and inline thumbnail bytes from tool results."""
    if isinstance(value, dict):
        return {
            key: clean(item)
            for key, item in value.items()
            if key not in ("@extra", "@client_id", "minithumbnail")
        }
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def message_view(message: dict) -> dict:
    content = message.get("content", {})
    result = {
        key: clean(message[key])
        for key in (
            "id",
            "chat_id",
            "sender_id",
            "date",
            "edit_date",
            "is_outgoing",
            "reply_to",
            "topic_id",
            "message_thread_id",
            "sending_state",
        )
        if key in message
    }
    result["content"] = clean(content)
    return result


def chat_view(chat: dict) -> dict:
    result = {
        key: clean(chat[key])
        for key in (
            "id",
            "title",
            "type",
            "unread_count",
            "last_read_inbox_message_id",
            "last_read_outbox_message_id",
            "unread_mention_count",
            "unread_reaction_count",
            "view_as_topics",
        )
        if key in chat
    }
    if chat.get("last_message"):
        result["last_message"] = message_view(chat["last_message"])
    return result


class Telegram:
    def __init__(self, client: TDLibClient | TelegramSession) -> None:
        self.client = client

    async def _read(self, method: str, **parameters: Any) -> dict:
        if method not in READ_METHODS:
            raise ValueError("Only the explicitly allowed read operations are supported.")
        return await self.client.request(method, **parameters)

    def status(self) -> dict:
        return {
            "read_only": False,
            "sending_requires_confirmation": True,
            "authorized": self.client.ready,
            "authorization_state": self.client.authorization_state["@type"],
            "login_url": getattr(self.client, "login_url", None),
        }

    async def get_me(self) -> dict:
        return clean(await self._read("getMe"))

    async def send_message(
        self,
        chat_id: int,
        text: str = "",
        attachments: list[Attachment] | None = None,
        *,
        confirmed: bool = False,
        forum_topic_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        if confirmed is not True:
            raise ValueError("Explicit user confirmation is required before sending a message.")
        attachments = attachments or []
        if not text.strip() and not attachments:
            raise ValueError("Provide message text or at least one attachment.")
        if len(text) > (1024 if attachments else 4096):
            raise ValueError("Text exceeds 1024 caption characters or 4096 text-only characters.")
        if len(attachments) > 10:
            raise ValueError("At most 10 attachments can be sent at once.")
        if any(a.type == "file" for a in attachments) and any(
            a.type != "file" for a in attachments
        ):
            raise ValueError(
                "Send files separately from photo/video albums; Telegram cannot mix them."
            )
        if forum_topic_id is not None and not 1 <= forum_topic_id < 2**31:
            raise ValueError("forum_topic_id must be a positive 32-bit topic ID.")
        if reply_to_message_id is not None and reply_to_message_id <= 0:
            raise ValueError("reply_to_message_id must be positive.")
        # Validate every file before starting authentication or submitting anything.
        contents = [a.content(text if i == 0 else "") for i, a in enumerate(attachments)]
        if not contents:
            contents = [
                {
                    "@type": "inputMessageText",
                    "text": {"@type": "formattedText", "text": text, "entities": []},
                    "clear_draft": False,
                }
            ]
        parameters = {
            "chat_id": chat_id,
            "topic_id": {"@type": "messageTopicForum", "forum_topic_id": forum_topic_id}
            if forum_topic_id is not None
            else None,
            "reply_to": {"@type": "inputMessageReplyToMessage", "message_id": reply_to_message_id}
            if reply_to_message_id is not None
            else None,
            "options": {"@type": "messageSendOptions", "paid_message_star_count": 0},
        }
        album = len(contents) > 1
        method = "sendMessageAlbum" if album else "sendMessage"
        parameters["input_message_contents" if album else "input_message_content"] = (
            contents if album else contents[0]
        )
        try:
            result = await self.client.request(method, **parameters)
        except TDLibError:
            raise
        except RuntimeError as exc:
            raise RuntimeError(
                f"Send outcome is unknown: {exc} "
                "Do not retry automatically; check chat history first."
            ) from None
        messages = result.get("messages", []) if album else [result]
        if not messages or any(
            not isinstance(m, dict) or m.get("@type") != "message" for m in messages
        ):
            raise RuntimeError("Unexpected send response. Check chat history before retrying.")
        states = {
            m.get("sending_state", {}).get("@type") for m in messages if m.get("sending_state")
        }
        status = (
            "failed" if "messageSendingStateFailed" in states else "pending" if states else "sent"
        )
        if len(messages) != len(contents):
            status = "partial"
        return {
            "status": status,
            "messages": [message_view(m) for m in messages],
            "automatic_retry_safe": False,
        }

    async def get_user(self, user_id: int) -> dict:
        return clean(await self._read("getUser", user_id=user_id))

    async def get_chat(self, chat_id: int) -> dict:
        return chat_view(await self._read("getChat", chat_id=chat_id))

    async def list_topics(
        self,
        chat_id: int,
        limit: int = 30,
        query: str = "",
        offset_date: int = 0,
        offset_message_id: int = 0,
        offset_forum_topic_id: int = 0,
    ) -> dict:
        result = await self._read(
            "getForumTopics",
            chat_id=chat_id,
            query=query,
            limit=limit,
            offset_date=offset_date,
            offset_message_id=offset_message_id,
            offset_forum_topic_id=offset_forum_topic_id,
        )
        topics = []
        for topic in result["topics"]:
            info = topic["info"]
            item = {
                "id": info["forum_topic_id"],
                "name": info["name"],
                **{
                    key: info[key]
                    for key in ("is_general", "is_closed", "is_hidden")
                    if key in info
                },
                **{key: topic[key] for key in ("is_pinned", "unread_count") if key in topic},
            }
            if topic.get("last_message"):
                item["last_message_id"] = topic["last_message"]["id"]
                item["last_message_date"] = topic["last_message"]["date"]
            topics.append(item)
        return {
            "total_count": result["total_count"],
            "topics": topics,
            "next_offset_date": result["next_offset_date"],
            "next_offset_message_id": result["next_offset_message_id"],
            "next_offset_forum_topic_id": result["next_offset_forum_topic_id"],
        }

    async def _chats(self, ids: list[int]) -> list[dict]:
        # Keep native request fan-out bounded even for large chat lists.
        return [await self.get_chat(chat_id) for chat_id in ids]

    async def list_chats(self, limit: int = 30, archive: bool = False) -> dict:
        chat_list = {"@type": "chatListArchive" if archive else "chatListMain"}
        try:
            await self._read("loadChats", chat_list=chat_list, limit=limit)
        except TDLibError as exc:
            if exc.code != 404:  # 404 means the entire list has been loaded.
                raise
        result = await self._read("getChats", chat_list=chat_list, limit=limit)
        return {"chats": await self._chats(result["chat_ids"])}

    async def search_chats(self, query: str, limit: int = 30) -> dict:
        # Server-backed search also finds account chats not in the local cache.
        result = await self._read("searchChatsOnServer", query=query, limit=limit)
        return {"chats": await self._chats(result["chat_ids"])}

    async def resolve_username(self, username: str) -> dict:
        username = username.removeprefix("@").strip()
        if not username or "/" in username:
            raise ValueError("Provide a Telegram username, such as @telegram, rather than a URL.")
        return chat_view(await self._read("searchPublicChat", username=username))

    async def get_history(
        self,
        chat_id: int,
        limit: int = 30,
        before_message_id: int = 0,
        forum_topic_id: int | None = None,
    ) -> dict:
        # TDLib's from_message_id is inclusive; drop it for an exclusive cursor.
        parameters = {
            "chat_id": chat_id,
            "from_message_id": before_message_id,
            "offset": 0,
            "limit": limit,
        }
        if forum_topic_id is not None:
            result = await self._read(
                "getForumTopicHistory",
                **parameters,
                forum_topic_id=forum_topic_id,
            )
        else:
            result = await self._read("getChatHistory", **parameters, only_local=False)
        messages = [
            item
            for item in result.get("messages", [])
            if not before_message_id or item["id"] < before_message_id
        ]
        return {
            "messages": [message_view(item) for item in messages],
            # Short batches do not indicate end-of-history; TDLib may return fewer
            # messages while it fills its local database.
            "next_before_message_id": messages[-1]["id"] if messages else None,
        }

    async def get_message(self, chat_id: int, message_id: int) -> dict:
        return message_view(await self._read("getMessage", chat_id=chat_id, message_id=message_id))

    async def search_messages(
        self,
        query: str,
        limit: int = 30,
        chat_id: int | None = None,
        offset: str = "",
        from_message_id: int = 0,
    ) -> dict:
        if chat_id is not None:
            result = await self._read(
                "searchChatMessages",
                chat_id=chat_id,
                query=query,
                from_message_id=from_message_id,
                offset=0,
                limit=limit,
            )
            return {
                "total_count": result["total_count"],
                "messages": [message_view(item) for item in result["messages"]],
                "next_from_message_id": result.get("next_from_message_id", 0),
            }
        result = await self._read("searchMessages", query=query, offset=offset, limit=limit)
        return {
            "total_count": result["total_count"],
            "messages": [message_view(item) for item in result["messages"]],
            "next_offset": result.get("next_offset", ""),
        }
