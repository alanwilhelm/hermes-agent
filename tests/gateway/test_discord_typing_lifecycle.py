from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="c1",
        user_name="alan",
        chat_name="Hermes / #general",
        chat_type="group",
    )


def _make_event(text: str = "hello") -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    adapter = SimpleNamespace(send=AsyncMock(), stop_typing=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:discord:group:c1:u1",
        session_id="sess-1",
        created_at=datetime(2026, 4, 1, 0, 0, 0),
        updated_at=datetime(2026, 4, 1, 0, 1, 0),
        platform=Platform.DISCORD,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._pending_approvals = {}
    runner._pending_messages = {}
    runner._running_agents = {}
    runner._voice_mode = {}
    runner._show_reasoning = False
    runner._session_db = None
    runner._base_url = ""
    runner._model = "openai/gpt-5.4"
    runner._set_session_env = lambda _context: None
    runner._has_setup_skill = lambda: False
    runner._dock_target_for_session = lambda _session_key: None
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._deliver_media_from_response = AsyncMock()
    runner._deliver_docked_response = AsyncMock(side_effect=lambda **kwargs: kwargs["response"])
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    )
    return runner, adapter


@pytest.mark.asyncio
async def test_runner_does_not_stop_discord_typing_before_outer_adapter_delivers_response():
    runner, adapter = _make_runner()
    event = _make_event()

    result = await runner._handle_message_with_agent(event, event.source, "quick-key")

    assert result == "done"
    assert adapter.stop_typing.await_count == 0


@pytest.mark.asyncio
async def test_runner_does_not_stop_discord_typing_from_error_path_before_outer_adapter_cleanup():
    runner, adapter = _make_runner()
    runner._run_agent = AsyncMock(side_effect=RuntimeError("boom"))
    event = _make_event()

    result = await runner._handle_message_with_agent(event, event.source, "quick-key")

    assert "RuntimeError" in result
    assert "boom" in result
    assert adapter.stop_typing.await_count == 0
