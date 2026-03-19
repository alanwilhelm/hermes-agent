"""Discord message intake and preflight helpers."""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional

try:
    import discord
except ImportError:  # pragma: no cover - import guard
    discord = None


def should_filter_bot_message(is_bot: bool, policy: str, is_mentioned: bool) -> bool:
    """Return whether a bot-authored message should be filtered."""
    if not is_bot:
        return False
    if policy == "none":
        return True
    if policy == "mentions" and not is_mentioned:
        return True
    return False


def should_skip_for_mention(
    require_mention: bool,
    is_free_channel: bool,
    in_bot_thread: bool,
    is_mentioned: bool,
) -> bool:
    """Return whether a guild message should be skipped for mention gating."""
    return require_mention and not is_free_channel and not in_bot_thread and not is_mentioned


def strip_mention(content: str, bot_user_id: int) -> str:
    """Strip direct bot mention syntax from the message content."""
    content = content.replace(f"<@{bot_user_id}>", "").strip()
    content = content.replace(f"<@!{bot_user_id}>", "").strip()
    return content


def classify_message_type(content: str, attachments: Iterable[Any]) -> str:
    """Return the normalized Discord message type string."""
    if content.startswith("/"):
        return "command"

    supported_document_types = {
        ".pdf", ".txt", ".md", ".py", ".js", ".json", ".yaml", ".yml",
        ".xml", ".csv", ".log", ".html", ".css", ".ts", ".tsx", ".jsx",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".doc", ".docx",
        ".xls", ".xlsx", ".ppt", ".pptx",
    }

    for attachment in attachments:
        content_type = getattr(attachment, "content_type", None)
        if content_type:
            if content_type.startswith("image/"):
                return "photo"
            if content_type.startswith("video/"):
                return "video"
            if content_type.startswith("audio/"):
                return "audio"

        filename = getattr(attachment, "filename", None)
        if filename:
            _, ext = os.path.splitext(filename)
            if ext.lower() in supported_document_types:
                return "document"

        if content_type and not any(
            content_type.startswith(prefix) for prefix in ("image/", "video/", "audio/")
        ):
            return "document"

    return "text"


def get_parent_channel_id(channel: Any) -> Optional[str]:
    """Return the parent channel ID for a Discord thread-like channel, if present."""
    parent = getattr(channel, "parent", None)
    if parent is not None and getattr(parent, "id", None) is not None:
        return str(parent.id)
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None:
        return str(parent_id)
    return None


def is_forum_parent(channel: Any) -> bool:
    """Best-effort check for whether a Discord channel is a forum channel."""
    if channel is None:
        return False

    forum_cls = getattr(discord, "ForumChannel", None) if discord else None
    if forum_cls and isinstance(channel, forum_cls):
        return True

    channel_type = getattr(channel, "type", None)
    if channel_type is not None:
        type_value = getattr(channel_type, "value", channel_type)
        if type_value == 15:
            return True

    return False


def format_thread_chat_name(thread: Any, is_forum_fn: Callable[[Any], bool]) -> str:
    """Build a readable chat name for thread-like Discord channels."""
    thread_name = getattr(thread, "name", None) or str(getattr(thread, "id", "thread"))
    parent = getattr(thread, "parent", None)
    guild = getattr(thread, "guild", None) or getattr(parent, "guild", None)
    guild_name = getattr(guild, "name", None)
    parent_name = getattr(parent, "name", None)

    if is_forum_fn(parent) and guild_name and parent_name:
        return f"{guild_name} / {parent_name} / {thread_name}"
    if parent_name and guild_name:
        return f"{guild_name} / #{parent_name} / {thread_name}"
    if parent_name:
        return f"{parent_name} / {thread_name}"
    return thread_name
