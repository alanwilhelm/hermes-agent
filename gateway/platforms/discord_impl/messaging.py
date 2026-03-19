"""Discord message-operation helpers."""

from __future__ import annotations

from typing import Any, Callable, Optional

from gateway.platforms.base import SendResult
from gateway.platforms.discord_impl.delivery import resolve_channel


def normalize_edit_content(
    content: str,
    *,
    format_message: Optional[Callable[[str], str]] = None,
    max_message_length: int = 2000,
) -> str:
    """Format and truncate edited message content to Discord limits."""
    formatter = format_message or (lambda value: value)
    formatted = formatter(content)
    if len(formatted) > max_message_length:
        return formatted[: max_message_length - 3] + "..."
    return formatted


async def fetch_channel_message(
    client: Any,
    chat_id: str,
    message_id: str,
) -> tuple[Any | None, Any | None, SendResult | None]:
    """Resolve a Discord channel and fetch a specific message from it."""
    channel = await resolve_channel(client, chat_id)
    if not channel:
        return None, None, SendResult(success=False, error=f"Channel {chat_id} not found")

    try:
        message = await channel.fetch_message(int(message_id))
    except Exception as exc:
        return channel, None, SendResult(success=False, error=str(exc))

    return channel, message, None


async def edit_message(
    client: Any,
    chat_id: str,
    message_id: str,
    content: str,
    *,
    format_message: Optional[Callable[[str], str]] = None,
    max_message_length: int = 2000,
) -> SendResult:
    """Edit a previously sent Discord message."""
    if not client:
        return SendResult(success=False, error="Not connected")

    _channel, message, error = await fetch_channel_message(client, chat_id, message_id)
    if error is not None:
        return error

    formatted = normalize_edit_content(
        content,
        format_message=format_message,
        max_message_length=max_message_length,
    )

    try:
        await message.edit(content=formatted)
    except Exception as exc:
        return SendResult(success=False, error=str(exc))

    return SendResult(success=True, message_id=message_id)
