import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _StubDiscordAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.DISCORD)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="msg-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        user_name="tester",
        chat_id="c1",
        chat_type="dm",
    )


def _make_event(text: str, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
        source=source or _make_source(),
        message_id="m1",
    )


def _make_runner(adapter: object) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._is_user_authorized = lambda _source: True
    return runner


@pytest.mark.asyncio
async def test_adapter_queues_discord_text_followup_without_interrupt():
    adapter = _StubDiscordAdapter()
    adapter._message_handler = AsyncMock(return_value=None)
    source = _make_source()
    session_key = build_session_key(source)
    adapter._active_sessions[session_key] = asyncio.Event()

    event = _make_event("follow up", source=source)
    await adapter.handle_message(event)

    assert len(adapter._pending_messages) == 1
    queued_key, queued_event = next(iter(adapter._pending_messages.items()))
    assert queued_key == session_key
    assert queued_event is event
    assert not adapter._active_sessions[queued_key].is_set()


@pytest.mark.asyncio
async def test_adapter_dispatches_steer_command_while_busy():
    adapter = _StubDiscordAdapter()
    source = _make_source()
    session_key = build_session_key(source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._message_handler = AsyncMock(return_value="Steer requested.")
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="msg-2")
    )

    event = _make_event("/steer 0 focus on tests", source=source)
    await adapter.handle_message(event)
    await asyncio.gather(*list(adapter._background_tasks))

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._send_with_retry.assert_awaited_once()
    assert not adapter._active_sessions[session_key].is_set()


@pytest.mark.asyncio
async def test_runner_queues_busy_discord_text_followup():
    adapter = SimpleNamespace(_pending_messages={}, _active_sessions={})
    runner = _make_runner(adapter)
    source = _make_source()
    session_key = build_session_key(source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    result = await runner._handle_message(_make_event("follow up", source=source))

    assert result is None
    running_agent.interrupt.assert_not_called()
    assert adapter._pending_messages[session_key].text == "follow up"


@pytest.mark.asyncio
async def test_handle_steer_command_target_zero_steers_main_agent():
    source = _make_source()
    session_key = build_session_key(source)
    adapter = SimpleNamespace(
        _pending_messages={},
        _active_sessions={session_key: asyncio.Event()},
    )
    runner = _make_runner(adapter)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key=session_key
    )
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    result = await runner._handle_steer_command(
        _make_event("/steer 0 focus on tests", source=source)
    )

    running_agent.interrupt.assert_called_once_with("focus on tests")
    assert adapter._pending_messages[session_key].text == "focus on tests"
    assert adapter._active_sessions[session_key].is_set()
    assert "Steering the main agent" in result
