from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import importlib
import sys

import pytest


def _load_messaging_module():
    class FakeIntents:
        @staticmethod
        def default():
            return SimpleNamespace()

    discord_mod = ModuleType("discord")
    discord_mod.__file__ = "mock-discord.py"
    discord_mod.Message = type("Message", (), {})
    discord_mod.Intents = FakeIntents
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *a, **k: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1,
        primary=2,
        danger=3,
        green=1,
        blurple=2,
        red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1,
        green=lambda: 2,
        blue=lambda: 3,
        red=lambda: 4,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus = SimpleNamespace(
        is_loaded=lambda: True,
        load_opus=lambda *_args, **_kwargs: None,
        Decoder=MagicMock,
    )
    discord_mod.FFmpegPCMAudio = MagicMock
    discord_mod.PCMVolumeTransformer = MagicMock
    discord_mod.http = SimpleNamespace(Route=MagicMock)

    sys.modules["discord"] = discord_mod
    ext_mod = ModuleType("discord.ext")
    commands_mod = ModuleType("discord.ext.commands")
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules["discord.ext"] = ext_mod
    sys.modules["discord.ext.commands"] = commands_mod
    importlib.reload(importlib.import_module("gateway.platforms.discord_impl.delivery"))
    return importlib.reload(importlib.import_module("gateway.platforms.discord_impl.messaging"))


messaging = _load_messaging_module()


def _make_client(channel):
    return SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=None if channel is None else channel),
    )


def test_normalize_edit_content_truncates_to_limit():
    result = messaging.normalize_edit_content(
        "a" * 25,
        format_message=lambda value: value.upper(),
        max_message_length=10,
    )

    assert result == "AAAAAAA..."


@pytest.mark.asyncio
async def test_edit_message_returns_success():
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    client = _make_client(channel)

    result = await messaging.edit_message(
        client,
        "123",
        "456",
        "hello",
        format_message=lambda value: f"**{value}**",
        max_message_length=2000,
    )

    assert result.success is True
    assert result.message_id == "456"
    channel.fetch_message.assert_awaited_once_with(456)
    message.edit.assert_awaited_once_with(content="**hello**")


@pytest.mark.asyncio
async def test_edit_message_returns_missing_channel_error():
    result = await messaging.edit_message(
        _make_client(None),
        "123",
        "456",
        "hello",
    )

    assert result.success is False
    assert result.error == "Channel 123 not found"


@pytest.mark.asyncio
async def test_edit_message_returns_fetch_error():
    channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=RuntimeError("missing message")))
    client = _make_client(channel)

    result = await messaging.edit_message(client, "123", "456", "hello")

    assert result.success is False
    assert result.error == "missing message"


@pytest.mark.asyncio
async def test_edit_message_truncates_oversized_content():
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    client = _make_client(channel)

    result = await messaging.edit_message(
        client,
        "123",
        "456",
        "a" * 20,
        max_message_length=10,
    )

    assert result.success is True
    message.edit.assert_awaited_once_with(content="aaaaaaa...")
