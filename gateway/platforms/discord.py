from __future__ import annotations

"""
Discord platform adapter.

Uses discord.py library for:
- Receiving messages from servers and DMs
- Sending responses back
- Handling threads and channels
"""

import asyncio
import json
import logging
import os
import struct
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

logger = logging.getLogger(__name__)

VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}

try:
    import discord
    from discord import Message as DiscordMessage, Intents
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    DiscordMessage = Any
    Intents = Any
    commands = None

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
import re

from gateway.platforms.helpers import MessageDeduplicator, ThreadParticipationTracker
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_url,
    cache_audio_from_url,
    cache_document_from_bytes,
    SUPPORTED_DOCUMENT_TYPES,
)
from gateway.platforms.discord_impl import config as discord_config
from gateway.platforms.discord_impl import command_sessions as discord_command_sessions
from gateway.platforms.discord_impl import delivery as discord_delivery
from gateway.platforms.discord_impl import history as discord_history
from gateway.platforms.discord_impl import intake as discord_intake
from gateway.platforms.discord_impl import interactions as discord_interactions
from gateway.platforms.discord_impl import messaging as discord_messaging
from gateway.platforms.discord_impl import native_commands as discord_native_commands
from gateway.platforms.discord_impl import permissions as discord_permissions
from gateway.platforms.discord_impl import runtime_state as discord_runtime_state
from gateway.platforms.discord_impl import state as discord_state
from gateway.platforms.discord_impl import threads as discord_threads
from tools.url_safety import is_safe_url


def check_discord_requirements() -> bool:
    """Check if Discord dependencies are available."""
    return DISCORD_AVAILABLE


class VoiceReceiver:
    """Captures and decodes voice audio from a Discord voice channel.

    Attaches to a VoiceClient's socket listener, decrypts RTP packets
    (NaCl transport + DAVE E2EE), decodes Opus to PCM, and buffers
    per-user audio.  A polling loop detects silence and delivers
    completed utterances via a callback.
    """

    SILENCE_THRESHOLD = 1.5    # seconds of silence → end of utterance
    MIN_SPEECH_DURATION = 0.5  # minimum seconds to process (skip noise)
    SAMPLE_RATE = 48000        # Discord native rate
    CHANNELS = 2               # Discord sends stereo

    def __init__(self, voice_client, allowed_user_ids: set = None):
        self._vc = voice_client
        self._allowed_user_ids = allowed_user_ids or set()
        self._running = False

        # Decryption
        self._secret_key: Optional[bytes] = None
        self._dave_session = None
        self._bot_ssrc: int = 0

        # SSRC -> user_id mapping (populated from SPEAKING events)
        self._ssrc_to_user: Dict[int, int] = {}
        self._lock = threading.Lock()

        # Per-user audio buffers
        self._buffers: Dict[int, bytearray] = defaultdict(bytearray)
        self._last_packet_time: Dict[int, float] = {}

        # Opus decoder per SSRC (each user needs own decoder state)
        self._decoders: Dict[int, object] = {}

        # Pause flag: don't capture while bot is playing TTS
        self._paused = False

        # Debug logging counter (instance-level to avoid cross-instance races)
        self._packet_debug_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start listening for voice packets."""
        conn = self._vc._connection
        self._secret_key = bytes(conn.secret_key)
        self._dave_session = conn.dave_session
        self._bot_ssrc = conn.ssrc

        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        self._running = True
        logger.info("VoiceReceiver started (bot_ssrc=%d)", self._bot_ssrc)

    def stop(self):
        """Stop listening and clean up."""
        self._running = False
        try:
            self._vc._connection.remove_socket_listener(self._on_packet)
        except Exception:
            pass
        with self._lock:
            self._buffers.clear()
            self._last_packet_time.clear()
            self._decoders.clear()
            self._ssrc_to_user.clear()
        logger.info("VoiceReceiver stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ------------------------------------------------------------------
    # SSRC -> user_id mapping via SPEAKING opcode hook
    # ------------------------------------------------------------------

    def map_ssrc(self, ssrc: int, user_id: int):
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def _install_speaking_hook(self, conn):
        """Wrap the voice websocket hook to capture SPEAKING events (op 5).

        VoiceConnectionState stores the hook as ``conn.hook`` (public attr).
        It is passed to DiscordVoiceWebSocket on each (re)connect, so we
        must wrap it on the VoiceConnectionState level AND on the current
        live websocket instance.
        """
        original_hook = conn.hook
        receiver_self = self

        async def wrapped_hook(ws, msg):
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d", {})
                ssrc = data.get("ssrc")
                user_id = data.get("user_id")
                if ssrc and user_id:
                    logger.info("SPEAKING event: ssrc=%d -> user=%s", ssrc, user_id)
                    receiver_self.map_ssrc(int(ssrc), int(user_id))
            if original_hook:
                await original_hook(ws, msg)

        # Set on connection state (for future reconnects)
        conn.hook = wrapped_hook
        # Set on the current live websocket (for immediate effect)
        try:
            from discord.utils import MISSING
            if hasattr(conn, 'ws') and conn.ws is not MISSING:
                conn.ws._hook = wrapped_hook
                logger.info("Speaking hook installed on live websocket")
        except Exception as e:
            logger.warning("Could not install hook on live ws: %s", e)

    # ------------------------------------------------------------------
    # Packet handler (called from SocketReader thread)
    # ------------------------------------------------------------------

    def _on_packet(self, data: bytes):
        if not self._running or self._paused:
            return

        # Log first few raw packets for debugging
        self._packet_debug_count += 1
        if self._packet_debug_count <= 5:
            logger.debug(
                "Raw UDP packet: len=%d, first_bytes=%s",
                len(data), data[:4].hex() if len(data) >= 4 else "short",
            )

        if len(data) < 16:
            return

        # RTP version check: top 2 bits must be 10 (version 2).
        # Lower bits may vary (padding, extension, CSRC count).
        # Payload type (byte 1 lower 7 bits) = 0x78 (120) for voice.
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            if self._packet_debug_count <= 5:
                logger.debug("Skipped non-RTP: byte0=0x%02x byte1=0x%02x", data[0], data[1])
            return

        first_byte = data[0]
        _, _, seq, timestamp, ssrc = struct.unpack_from(">BBHII", data, 0)

        # Skip bot's own audio
        if ssrc == self._bot_ssrc:
            return

        # Calculate dynamic RTP header size (RFC 9335 / rtpsize mode)
        cc = first_byte & 0x0F  # CSRC count
        has_extension = bool(first_byte & 0x10)  # extension bit
        header_size = 12 + (4 * cc) + (4 if has_extension else 0)

        if len(data) < header_size + 4:  # need at least header + nonce
            return

        # Read extension length from preamble (for skipping after decrypt)
        ext_data_len = 0
        if has_extension:
            ext_preamble_offset = 12 + (4 * cc)
            ext_words = struct.unpack_from(">H", data, ext_preamble_offset + 2)[0]
            ext_data_len = ext_words * 4

        if self._packet_debug_count <= 10:
            with self._lock:
                known_user = self._ssrc_to_user.get(ssrc, "unknown")
            logger.debug(
                "RTP packet: ssrc=%d, seq=%d, user=%s, hdr=%d, ext_data=%d",
                ssrc, seq, known_user, header_size, ext_data_len,
            )

        header = bytes(data[:header_size])
        payload_with_nonce = data[header_size:]

        # --- NaCl transport decrypt (aead_xchacha20_poly1305_rtpsize) ---
        if len(payload_with_nonce) < 4:
            return
        nonce = bytearray(24)
        nonce[:4] = payload_with_nonce[-4:]
        encrypted = bytes(payload_with_nonce[:-4])

        try:
            import nacl.secret  # noqa: delayed import – only in voice path
            box = nacl.secret.Aead(self._secret_key)
            decrypted = box.decrypt(encrypted, header, bytes(nonce))
        except Exception as e:
            if self._packet_debug_count <= 10:
                logger.warning("NaCl decrypt failed: %s (hdr=%d, enc=%d)", e, header_size, len(encrypted))
            return

        # Skip encrypted extension data to get the actual opus payload
        if ext_data_len and len(decrypted) > ext_data_len:
            decrypted = decrypted[ext_data_len:]

        # --- DAVE E2EE decrypt ---
        if self._dave_session:
            with self._lock:
                user_id = self._ssrc_to_user.get(ssrc, 0)
            if user_id:
                try:
                    import davey
                    decrypted = self._dave_session.decrypt(
                        user_id, davey.MediaType.audio, decrypted
                    )
                except Exception as e:
                    # Unencrypted passthrough — use NaCl-decrypted data as-is
                    if "Unencrypted" not in str(e):
                        if self._packet_debug_count <= 10:
                            logger.warning("DAVE decrypt failed for ssrc=%d: %s", ssrc, e)
                        return
            # If SSRC unknown (no SPEAKING event yet), skip DAVE and try
            # Opus decode directly — audio may be in passthrough mode.
            # Buffer will get a user_id when SPEAKING event arrives later.

        # --- Opus decode -> PCM ---
        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = discord.opus.Decoder()
            pcm = self._decoders[ssrc].decode(decrypted)
            with self._lock:
                self._buffers[ssrc].extend(pcm)
                self._last_packet_time[ssrc] = time.monotonic()
        except Exception as e:
            logger.debug("Opus decode error for SSRC %s: %s", ssrc, e)
            return

    # ------------------------------------------------------------------
    # Silence detection
    # ------------------------------------------------------------------

    def _infer_user_for_ssrc(self, ssrc: int) -> int:
        """Try to infer user_id for an unmapped SSRC.

        When the bot rejoins a voice channel, Discord may not resend
        SPEAKING events for users already speaking.  If exactly one
        allowed user is in the channel, map the SSRC to them.
        """
        try:
            channel = self._vc.channel
            if not channel:
                return 0
            bot_id = self._vc.user.id if self._vc.user else 0
            allowed = self._allowed_user_ids
            candidates = [
                m.id for m in channel.members
                if m.id != bot_id and (not allowed or str(m.id) in allowed)
            ]
            if len(candidates) == 1:
                uid = candidates[0]
                self._ssrc_to_user[ssrc] = uid
                logger.info("Auto-mapped ssrc=%d -> user=%d (sole allowed member)", ssrc, uid)
                return uid
        except Exception:
            pass
        return 0

    def check_silence(self) -> list:
        """Return list of (user_id, pcm_bytes) for completed utterances."""
        now = time.monotonic()
        completed = []

        with self._lock:
            ssrc_user_map = dict(self._ssrc_to_user)
            ssrc_list = list(self._buffers.keys())

            for ssrc in ssrc_list:
                last_time = self._last_packet_time.get(ssrc, now)
                silence_duration = now - last_time
                buf = self._buffers[ssrc]
                # 48kHz, 16-bit, stereo = 192000 bytes/sec
                buf_duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * 2)

                if silence_duration >= self.SILENCE_THRESHOLD and buf_duration >= self.MIN_SPEECH_DURATION:
                    user_id = ssrc_user_map.get(ssrc, 0)
                    if not user_id:
                        # SSRC not mapped (SPEAKING event missing after bot rejoin).
                        # Infer from allowed users in the voice channel.
                        user_id = self._infer_user_for_ssrc(ssrc)
                    if user_id:
                        completed.append((user_id, bytes(buf)))
                    self._buffers[ssrc] = bytearray()
                    self._last_packet_time.pop(ssrc, None)
                elif silence_duration >= self.SILENCE_THRESHOLD * 2:
                    # Stale buffer with no valid user — discard
                    self._buffers.pop(ssrc, None)
                    self._last_packet_time.pop(ssrc, None)

        return completed

    # ------------------------------------------------------------------
    # PCM -> WAV conversion (for Whisper STT)
    # ------------------------------------------------------------------

    @staticmethod
    def pcm_to_wav(pcm_data: bytes, output_path: str,
                   src_rate: int = 48000, src_channels: int = 2):
        """Convert raw PCM to 16kHz mono WAV via ffmpeg."""
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_data)
            pcm_path = f.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "s16le",
                    "-ar", str(src_rate),
                    "-ac", str(src_channels),
                    "-i", pcm_path,
                    "-ar", "16000",
                    "-ac", "1",
                    output_path,
                ],
                check=True,
                timeout=10,
            )
        finally:
            try:
                os.unlink(pcm_path)
            except OSError:
                pass


class DiscordAdapter(BasePlatformAdapter):
    """
    Discord bot adapter.

    Handles:
    - Receiving messages from servers and DMs
    - Sending responses with Discord markdown
    - Thread support
    - Native slash commands (/ask, /reset, /status, /stop)
    - Button-based exec approvals
    - Auto-threading for long conversations
    - Reaction-based feedback
    """

    # Discord message limits
    MAX_MESSAGE_LENGTH = 2000
    _SPLIT_THRESHOLD = 1900  # near the 2000-char split point

    # Auto-disconnect from voice channel after this many seconds of inactivity
    VOICE_TIMEOUT = 300

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._client: Optional[commands.Bot] = None
        self._ready_event = asyncio.Event()
        self._discord_policy: Optional[discord_config.DiscordPolicyConfig] = None
        self._discord_policy_overrides: Dict[str, Any] = {}
        self._component_runtime = discord_interactions.create_component_runtime()
        self._allowed_user_ids: set = set()  # For button approval authorization
        self._resolve_exec_approval: Optional[Callable[..., Any]] = None
        # Voice channel state (per-guild)
        self._voice_clients: Dict[int, Any] = {}  # guild_id -> VoiceClient
        # Text batching: merge rapid successive messages (Telegram-style)
        self._text_batch_delay_seconds = float(os.getenv("HERMES_DISCORD_TEXT_BATCH_DELAY_SECONDS", "0.6"))
        self._text_batch_split_delay_seconds = float(os.getenv("HERMES_DISCORD_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
        self._voice_text_channels: Dict[int, int] = {}  # guild_id -> text_channel_id
        self._voice_timeout_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> timeout task
        # Phase 2: voice listening
        self._voice_receivers: Dict[int, VoiceReceiver] = {}  # guild_id -> VoiceReceiver
        self._voice_listen_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> listen loop
        self._voice_input_callback: Optional[Callable] = None  # set by run.py
        self._on_voice_disconnect: Optional[Callable] = None  # set by run.py
        # Track threads where the bot has participated so follow-up messages
        # in those threads don't require @mention.  Persisted to disk so the
        # set survives gateway restarts.
        self._threads = ThreadParticipationTracker("discord")
        # Persistent typing indicator loops per channel (DMs don't reliably
        # show the standard typing gateway event for bots)
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        self._bot_task: Optional[asyncio.Task] = None
        self._thread_bindings, self._activation_overrides = self._load_runtime_state()
        self._post_connect_task: Optional[asyncio.Task] = None
        # Dedup cache: prevents duplicate bot responses when Discord
        # RESUME replays events after reconnects.
        self._dedup = MessageDeduplicator()
        # Reply threading mode: "off" (no replies), "first" (reply on first
        # chunk only, default), "all" (reply-reference on every chunk).
        self._reply_to_mode: str = getattr(config, 'reply_to_mode', 'first') or 'first'
        # Back-compat alias for older helpers/tests that still inspect the raw set.
        self._bot_participated_threads = self._threads._threads
        self._MAX_TRACKED_THREADS = 500

    def _get_discord_policy(self) -> discord_config.DiscordPolicyConfig:
        """Return the adapter's cached Discord policy snapshot."""
        if self._discord_policy is None:
            self._discord_policy = discord_config.load_policy_config(
                self.config,
                overrides=self._discord_policy_overrides,
            )
        return self._discord_policy

    def apply_runtime_policy_overrides(
        self,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> discord_config.DiscordPolicyConfig:
        """Apply runtime-only policy overrides for the active Discord connection."""
        self._discord_policy_overrides = dict(overrides or {})
        self._discord_policy = None
        policy = self._get_discord_policy()
        self._allowed_user_ids = set(policy.allowed_users)
        return policy

    async def connect(self) -> bool:
        """Connect to Discord and start receiving events."""
        if not DISCORD_AVAILABLE:
            logger.error("[%s] discord.py not installed. Run: pip install discord.py", self.name)
            return False

        # Load opus codec for voice channel support
        if not discord.opus.is_loaded():
            import ctypes.util
            opus_path = ctypes.util.find_library("opus")
            # ctypes.util.find_library fails on macOS with Homebrew-installed libs,
            # so fall back to known Homebrew paths if needed.
            if not opus_path:
                import sys
                _homebrew_paths = (
                    "/opt/homebrew/lib/libopus.dylib",  # Apple Silicon
                    "/usr/local/lib/libopus.dylib",     # Intel Mac
                )
                if sys.platform == "darwin":
                    for _hp in _homebrew_paths:
                        if os.path.isfile(_hp):
                            opus_path = _hp
                            break
            if opus_path:
                try:
                    discord.opus.load_opus(opus_path)
                except Exception:
                    logger.warning("Opus codec found at %s but failed to load", opus_path)
            if not discord.opus.is_loaded():
                logger.warning("Opus codec not found — voice channel playback disabled")

        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False

        try:
            if not self._acquire_platform_lock('discord-bot-token', self.config.token, 'Discord bot token'):
                return False

            # Parse allowed user entries (may contain usernames or IDs)
            self._allowed_user_ids = set(self._get_discord_policy().allowed_users)

            # Set up intents.
            # Message Content is required for normal text replies.
            # Server Members is only needed when the allowlist contains usernames
            # that must be resolved to numeric IDs. Requesting privileged intents
            # that aren't enabled in the Discord Developer Portal can prevent the
            # bot from coming online at all, so avoid requesting members intent
            # unless it is actually necessary.
            intents = Intents.default()
            intents.message_content = True
            intents.dm_messages = True
            intents.guild_messages = True
            intents.members = any(not entry.isdigit() for entry in self._allowed_user_ids)
            intents.voice_states = True

            # Resolve proxy (DISCORD_PROXY > generic env vars > macOS system proxy)
            from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_bot
            proxy_url = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
            if proxy_url:
                logger.info("[%s] Using proxy for Discord: %s", self.name, proxy_url)

            # Create bot — proxy= for HTTP, connector= for SOCKS
            self._client = commands.Bot(
                command_prefix="!",  # Not really used, we handle raw messages
                intents=intents,
                **proxy_kwargs_for_bot(proxy_url),
            )
            adapter_self = self  # capture for closure

            # Register event handlers
            @self._client.event
            async def on_ready():
                logger.info("[%s] Connected as %s", adapter_self.name, adapter_self._client.user)

                # Resolve any usernames in the allowed list to numeric IDs
                await adapter_self._resolve_allowed_usernames()
                adapter_self._ready_event.set()

                if adapter_self._post_connect_task and not adapter_self._post_connect_task.done():
                    adapter_self._post_connect_task.cancel()
                adapter_self._post_connect_task = asyncio.create_task(
                    adapter_self._run_post_connect_initialization()
                )

            @self._client.event
            async def on_message(message: DiscordMessage):
                # Dedup: Discord RESUME replays events after reconnects (#4777)
                if adapter_self._dedup.is_duplicate(str(message.id)):
                    return

                # Always ignore our own messages
                if message.author == self._client.user:
                    return

                # Ignore Discord system messages (thread renames, pins, member joins, etc.)
                # Allow both default and reply types — replies have a distinct MessageType.
                if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                    return

                # Check if the message author is in the allowed user list
                if not self._is_allowed_user(str(message.author.id)):
                    return

                # Bot message filtering (DISCORD_ALLOW_BOTS):
                #   "none"     — ignore all other bots (default)
                #   "mentions" — accept bot messages only when they @mention us
                #   "all"      — accept all bot messages
                is_mentioned = bool(self._client.user and self._client.user in message.mentions)
                policy = adapter_self._get_discord_policy()
                if discord_intake.should_filter_bot_message(
                    is_bot=getattr(message.author, "bot", False),
                    policy=policy.bot_filter_policy,
                    is_mentioned=is_mentioned,
                ):
                    return
                # Multi-agent filtering: if the message mentions specific bots
                # but NOT this bot, the sender is talking to another agent —
                # stay silent.  Messages with no bot mentions (general chat)
                # still fall through to _handle_message for the existing
                # DISCORD_REQUIRE_MENTION check.
                if not isinstance(message.channel, discord.DMChannel) and message.mentions:
                    _self_mentioned = (
                        self._client.user is not None
                        and self._client.user in message.mentions
                    )
                    _other_bots_mentioned = any(
                        m.bot and m != self._client.user
                        for m in message.mentions
                    )
                    # If other bots are mentioned but we're not → not for us
                    if _other_bots_mentioned and not _self_mentioned:
                        return
                    # If humans are mentioned but we're not → not for us
                    # (preserves old DISCORD_IGNORE_NO_MENTION=true behavior)
                    _ignore_no_mention = os.getenv(
                        "DISCORD_IGNORE_NO_MENTION", "true"
                    ).lower() in ("true", "1", "yes")
                    if _ignore_no_mention and not _self_mentioned and not _other_bots_mentioned:
                        return

                await self._handle_message(message)

            @self._client.event
            async def on_voice_state_update(member, before, after):
                """Track voice channel join/leave events."""
                # Only track channels where the bot is connected
                bot_guild_ids = set(adapter_self._voice_clients.keys())
                if not bot_guild_ids:
                    return
                guild_id = member.guild.id
                if guild_id not in bot_guild_ids:
                    return
                # Ignore the bot itself
                if member == adapter_self._client.user:
                    return

                joined = before.channel is None and after.channel is not None
                left = before.channel is not None and after.channel is None
                switched = (
                    before.channel is not None
                    and after.channel is not None
                    and before.channel != after.channel
                )

                if joined or left or switched:
                    logger.info(
                        "Voice state: %s (%d) %s (guild %d)",
                        member.display_name,
                        member.id,
                        "joined " + after.channel.name if joined
                        else "left " + before.channel.name if left
                        else f"moved {before.channel.name} -> {after.channel.name}",
                        guild_id,
                    )

            # Register slash commands
            self._register_slash_commands()

            # Start the bot in background
            self._bot_task = asyncio.create_task(self._client.start(self.config.token))

            # Wait for ready
            await asyncio.wait_for(self._ready_event.wait(), timeout=30)

            self._running = True
            return True

        except asyncio.TimeoutError:
            logger.error("[%s] Timeout waiting for connection to Discord", self.name, exc_info=True)
            self._release_platform_lock()
            return False
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to connect to Discord: %s", self.name, e, exc_info=True)
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        # Clean up all active voice connections before closing the client
        for guild_id in list(self._voice_clients.keys()):
            try:
                await self.leave_voice_channel(guild_id)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.debug("[%s] Error leaving voice channel %s: %s", self.name, guild_id, e)

        if self._client:
            try:
                await self._client.close()
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning("[%s] Error during disconnect: %s", self.name, e, exc_info=True)

        if self._post_connect_task and not self._post_connect_task.done():
            self._post_connect_task.cancel()
            try:
                await self._post_connect_task
            except asyncio.CancelledError:
                pass

        self._running = False
        self._client = None
        self._ready_event.clear()
        self._post_connect_task = None

        self._release_platform_lock()

        logger.info("[%s] Disconnected", self.name)

    async def _run_post_connect_initialization(self) -> None:
        """Finish non-critical startup work after Discord is connected."""
        if not self._client:
            return
        try:
            synced = await asyncio.wait_for(self._client.tree.sync(), timeout=30)
            logger.info("[%s] Synced %d slash command(s)", self.name, len(synced))
        except asyncio.TimeoutError:
            logger.warning("[%s] Slash command sync timed out after 30s", self.name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning("[%s] Slash command sync failed: %s", self.name, e, exc_info=True)

    async def _add_reaction(self, message: Any, emoji: str) -> bool:
        """Add an emoji reaction to a Discord message."""
        if not message or not hasattr(message, "add_reaction"):
            return False
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            logger.debug("[%s] add_reaction failed (%s): %s", self.name, emoji, e)
            return False

    async def _remove_reaction(self, message: Any, emoji: str) -> bool:
        """Remove the bot's own emoji reaction from a Discord message."""
        if not message or not hasattr(message, "remove_reaction") or not self._client or not self._client.user:
            return False
        try:
            await message.remove_reaction(emoji, self._client.user)
            return True
        except Exception as e:
            logger.debug("[%s] remove_reaction failed (%s): %s", self.name, emoji, e)
            return False

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("DISCORD_REACTIONS", "true").lower() not in ("false", "0", "no")

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction for normal Discord message events."""
        if not self._reactions_enabled():
            return
        message = event.raw_message
        if hasattr(message, "add_reaction"):
            await self._add_reaction(message, "👀")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction."""
        if not self._reactions_enabled():
            return
        message = event.raw_message
        if hasattr(message, "add_reaction"):
            await self._remove_reaction(message, "👀")
            if outcome == ProcessingOutcome.SUCCESS:
                await self._add_reaction(message, "✅")
            elif outcome == ProcessingOutcome.FAILURE:
                await self._add_reaction(message, "❌")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message to a Discord channel or thread.

        When metadata contains a thread_id, the message is sent to that
        thread instead of the parent channel identified by chat_id.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            target_id = metadata.get("thread_id") if metadata and metadata.get("thread_id") else chat_id
            channel = await discord_delivery.resolve_channel(self._client, target_id)
            if not channel:
                target_kind = "Thread" if target_id != chat_id else "Channel"
                return SendResult(success=False, error=f"{target_kind} {target_id} not found")

            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
            message_ids = []
            reference = None

            if reply_to and self._reply_to_mode != "off":
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    reference = ref_msg
                except Exception as e:
                    logger.debug("Could not fetch reply-to message: %s", e)

            for i, chunk in enumerate(chunks):
                if self._reply_to_mode == "all":
                    chunk_reference = reference
                else:  # "first" (default) or "off"
                    chunk_reference = reference if i == 0 else None
                msg = await discord_delivery.send_text_message(
                    channel,
                    chunk,
                    reference=chunk_reference,
                )
                message_ids.append(str(msg.id))

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids}
            )

        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send Discord message: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent Discord message."""
        result = await discord_messaging.edit_message(
            self._client,
            chat_id,
            message_id,
            content,
            format_message=self.format_message,
            max_message_length=self.MAX_MESSAGE_LENGTH,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to edit Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> SendResult:
        """Delete a Discord message."""
        result = await discord_messaging.delete_message(
            self._client,
            chat_id,
            message_id,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to delete Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def add_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: Any,
    ) -> SendResult:
        """Add a reaction to a Discord message."""
        result = await discord_messaging.add_reaction(
            self._client,
            chat_id,
            message_id,
            emoji,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to add reaction to Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def remove_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: Any,
    ) -> SendResult:
        """Remove the connected bot's reaction from a Discord message."""
        result = await discord_messaging.remove_reaction(
            self._client,
            chat_id,
            message_id,
            emoji,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to remove reaction from Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def _send_file_attachment(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Send a local file as a Discord attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        channel = await discord_delivery.resolve_channel(self._client, chat_id)
        if not channel:
            return SendResult(success=False, error=f"Channel {chat_id} not found")

        discord_mod = sys.modules.get("discord", discord)
        if discord_mod is None:  # pragma: no cover - import guard
            return SendResult(success=False, error="discord.py not installed")

        filename = file_name or os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            file = discord_mod.File(fh, filename=filename)
            msg = await channel.send(content=caption if caption else None, file=file)
        return SendResult(success=True, message_id=str(msg.id))

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """Play auto-TTS audio.

        When the bot is in a voice channel for this chat's guild, play
        directly in the VC instead of sending as a file attachment.
        """
        for gid, text_ch_id in self._voice_text_channels.items():
            if str(text_ch_id) == str(chat_id) and self.is_in_voice_channel(gid):
                logger.info("[%s] Playing TTS in voice channel (guild=%d)", self.name, gid)
                success = await self.play_in_voice_channel(gid, audio_path)
                return SendResult(success=success)
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a Discord file attachment."""
        try:
            import io

            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")

            filename = os.path.basename(audio_path)

            with open(audio_path, "rb") as f:
                file_data = f.read()

            # Try sending as a native voice message via raw API (flags=8192).
            try:
                import base64

                duration_secs = 5.0
                try:
                    from mutagen.oggopus import OggOpus
                    info = OggOpus(audio_path)
                    duration_secs = info.info.length
                except Exception:
                    duration_secs = max(1.0, len(file_data) / 2000.0)

                waveform_bytes = bytes([128] * 256)
                waveform_b64 = base64.b64encode(waveform_bytes).decode()

                import json as _json
                payload = _json.dumps({
                    "flags": 8192,
                    "attachments": [{
                        "id": "0",
                        "filename": "voice-message.ogg",
                        "duration_secs": round(duration_secs, 2),
                        "waveform": waveform_b64,
                    }],
                })
                form = [
                    {"name": "payload_json", "value": payload},
                    {
                        "name": "files[0]",
                        "value": file_data,
                        "filename": "voice-message.ogg",
                        "content_type": "audio/ogg",
                    },
                ]
                msg_data = await self._client.http.request(
                    discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
                    form=form,
                )
                return SendResult(success=True, message_id=str(msg_data["id"]))
            except Exception as voice_err:
                logger.debug("Voice message flag failed, falling back to file: %s", voice_err)
                file = discord.File(io.BytesIO(file_data), filename=filename)
                msg = await channel.send(file=file)
                return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send audio, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_voice(chat_id, audio_path, caption, reply_to, metadata=metadata)

    # ------------------------------------------------------------------
    # Voice channel methods (join / leave / play)
    # ------------------------------------------------------------------

    async def join_voice_channel(self, channel) -> bool:
        """Join a Discord voice channel. Returns True on success."""
        if not self._client or not DISCORD_AVAILABLE:
            return False
        guild_id = channel.guild.id

        # Already connected in this guild?
        existing = self._voice_clients.get(guild_id)
        if existing and existing.is_connected():
            if existing.channel.id == channel.id:
                self._reset_voice_timeout(guild_id)
                return True
            await existing.move_to(channel)
            self._reset_voice_timeout(guild_id)
            return True

        vc = await channel.connect()
        self._voice_clients[guild_id] = vc
        self._reset_voice_timeout(guild_id)

        # Start voice receiver (Phase 2: listen to users)
        try:
            receiver = VoiceReceiver(vc, allowed_user_ids=self._allowed_user_ids)
            receiver.start()
            self._voice_receivers[guild_id] = receiver
            self._voice_listen_tasks[guild_id] = asyncio.ensure_future(
                self._voice_listen_loop(guild_id)
            )
        except Exception as e:
            logger.warning("Voice receiver failed to start: %s", e)

        return True

    async def leave_voice_channel(self, guild_id: int) -> None:
        """Disconnect from the voice channel in a guild."""
        # Stop voice receiver first
        receiver = self._voice_receivers.pop(guild_id, None)
        if receiver:
            receiver.stop()
        listen_task = self._voice_listen_tasks.pop(guild_id, None)
        if listen_task:
            listen_task.cancel()

        vc = self._voice_clients.pop(guild_id, None)
        if vc and vc.is_connected():
            await vc.disconnect()
        task = self._voice_timeout_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        self._voice_text_channels.pop(guild_id, None)

    # Maximum seconds to wait for voice playback before giving up
    PLAYBACK_TIMEOUT = 120

    async def play_in_voice_channel(self, guild_id: int, audio_path: str) -> bool:
        """Play an audio file in the connected voice channel."""
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return False

        # Pause voice receiver while playing (echo prevention)
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            receiver.pause()

        try:
            # Wait for current playback to finish (with timeout)
            wait_start = time.monotonic()
            while vc.is_playing():
                if time.monotonic() - wait_start > self.PLAYBACK_TIMEOUT:
                    logger.warning("Timed out waiting for previous playback to finish")
                    vc.stop()
                    break
                await asyncio.sleep(0.1)

            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _after(error):
                if error:
                    logger.error("Voice playback error: %s", error)
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(audio_path)
            source = discord.PCMVolumeTransformer(source, volume=1.0)
            vc.play(source, after=_after)
            try:
                await asyncio.wait_for(done.wait(), timeout=self.PLAYBACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Voice playback timed out after %ds", self.PLAYBACK_TIMEOUT)
                vc.stop()
            self._reset_voice_timeout(guild_id)
            return True
        finally:
            if receiver:
                receiver.resume()

    async def get_user_voice_channel(self, guild_id: int, user_id: str):
        """Return the voice channel the user is currently in, or None."""
        if not self._client:
            return None
        guild = self._client.get_guild(guild_id)
        if not guild:
            return None
        member = guild.get_member(int(user_id))
        if not member or not member.voice:
            return None
        return member.voice.channel

    def _reset_voice_timeout(self, guild_id: int) -> None:
        """Reset the auto-disconnect inactivity timer."""
        task = self._voice_timeout_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        self._voice_timeout_tasks[guild_id] = asyncio.ensure_future(
            self._voice_timeout_handler(guild_id)
        )

    async def _voice_timeout_handler(self, guild_id: int) -> None:
        """Auto-disconnect after VOICE_TIMEOUT seconds of inactivity."""
        try:
            await asyncio.sleep(self.VOICE_TIMEOUT)
        except asyncio.CancelledError:
            return
        text_ch_id = self._voice_text_channels.get(guild_id)
        await self.leave_voice_channel(guild_id)
        # Notify the runner so it can clean up voice_mode state
        if self._on_voice_disconnect and text_ch_id:
            try:
                self._on_voice_disconnect(str(text_ch_id))
            except Exception:
                pass
        if text_ch_id and self._client:
            ch = self._client.get_channel(text_ch_id)
            if ch:
                try:
                    await ch.send("Left voice channel (inactivity timeout).")
                except Exception:
                    pass

    def is_in_voice_channel(self, guild_id: int) -> bool:
        """Check if the bot is connected to a voice channel in this guild."""
        vc = self._voice_clients.get(guild_id)
        return vc is not None and vc.is_connected()

    def get_voice_channel_info(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Return voice channel awareness info for the given guild.

        Returns None if the bot is not in a voice channel.  Otherwise
        returns a dict with channel name, member list, count, and
        currently-speaking user IDs (from SSRC mapping).
        """
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return None

        channel = vc.channel
        if not channel:
            return None

        # Members currently in the voice channel (includes bot)
        members_info = []
        bot_user = self._client.user if self._client else None
        for m in channel.members:
            if bot_user and m.id == bot_user.id:
                continue  # skip the bot itself
            members_info.append({
                "user_id": m.id,
                "display_name": m.display_name,
                "is_bot": m.bot,
            })

        # Currently speaking users (from SSRC mapping + active buffers)
        speaking_user_ids: set = set()
        receiver = self._voice_receivers.get(guild_id)
        if receiver:
            import time as _time
            now = _time.monotonic()
            with receiver._lock:
                for ssrc, last_t in receiver._last_packet_time.items():
                    # Consider "speaking" if audio received within last 2 seconds
                    if now - last_t < 2.0:
                        uid = receiver._ssrc_to_user.get(ssrc)
                        if uid:
                            speaking_user_ids.add(uid)

        # Tag speaking status on members
        for info in members_info:
            info["is_speaking"] = info["user_id"] in speaking_user_ids

        return {
            "channel_name": channel.name,
            "member_count": len(members_info),
            "members": members_info,
            "speaking_count": len(speaking_user_ids),
        }

    def get_voice_channel_context(self, guild_id: int) -> str:
        """Return a human-readable voice channel context string.

        Suitable for injection into the system/ephemeral prompt so the
        agent is always aware of voice channel state.
        """
        info = self.get_voice_channel_info(guild_id)
        if not info:
            return ""

        parts = [f"[Voice channel: #{info['channel_name']} — {info['member_count']} participant(s)]"]
        for m in info["members"]:
            status = " (speaking)" if m["is_speaking"] else ""
            parts.append(f"  - {m['display_name']}{status}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Voice listening (Phase 2)
    # ------------------------------------------------------------------

    # UDP keepalive interval in seconds — prevents Discord from dropping
    # the UDP route after ~60s of silence.
    _KEEPALIVE_INTERVAL = 15

    async def _voice_listen_loop(self, guild_id: int):
        """Periodically check for completed utterances and process them."""
        receiver = self._voice_receivers.get(guild_id)
        if not receiver:
            return
        last_keepalive = time.monotonic()
        try:
            while receiver._running:
                await asyncio.sleep(0.2)

                # Send periodic UDP keepalive to prevent Discord from
                # dropping the UDP session after ~60s of silence.
                now = time.monotonic()
                if now - last_keepalive >= self._KEEPALIVE_INTERVAL:
                    last_keepalive = now
                    try:
                        vc = self._voice_clients.get(guild_id)
                        if vc and vc.is_connected():
                            vc._connection.send_packet(b'\xf8\xff\xfe')
                    except Exception:
                        pass

                completed = receiver.check_silence()
                for user_id, pcm_data in completed:
                    if not self._is_allowed_user(str(user_id)):
                        continue
                    await self._process_voice_input(guild_id, user_id, pcm_data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Voice listen loop error: %s", e, exc_info=True)

    async def _process_voice_input(self, guild_id: int, user_id: int, pcm_data: bytes):
        """Convert PCM -> WAV -> STT -> callback."""
        from tools.voice_mode import is_whisper_hallucination

        tmp_f = tempfile.NamedTemporaryFile(suffix=".wav", prefix="vc_listen_", delete=False)
        wav_path = tmp_f.name
        tmp_f.close()
        try:
            await asyncio.to_thread(VoiceReceiver.pcm_to_wav, pcm_data, wav_path)

            from tools.transcription_tools import transcribe_audio
            result = await asyncio.to_thread(transcribe_audio, wav_path)

            if not result.get("success"):
                return
            transcript = result.get("transcript", "").strip()
            if not transcript or is_whisper_hallucination(transcript):
                return

            logger.info("Voice input from user %d: %s", user_id, transcript[:100])

            if self._voice_input_callback:
                await self._voice_input_callback(
                    guild_id=guild_id,
                    user_id=user_id,
                    transcript=transcript,
                )
        except Exception as e:
            logger.warning("Voice input processing failed: %s", e, exc_info=True)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _is_allowed_user(self, user_id: str) -> bool:
        """Check if user is in DISCORD_ALLOWED_USERS."""
        if not self._allowed_user_ids:
            return True
        return user_id in self._allowed_user_ids

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file natively as a Discord file attachment."""
        try:
            return await self._send_file_attachment(chat_id, image_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Image file not found: {image_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local image, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Discord file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        if not is_safe_url(image_url):
            logger.warning("[%s] Blocked unsafe image URL during Discord send_image", self.name)
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)

        try:
            import aiohttp

            channel = await discord_delivery.resolve_channel(self._client, chat_id)
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            # Download the image and send as a Discord file attachment
            # (Discord renders attachments inline, unlike plain URLs)
            from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
            _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
            _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
            async with aiohttp.ClientSession(**_sess_kw) as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30), **_req_kw) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to download image: HTTP {resp.status}")

                    image_data = await resp.read()

                    # Determine filename from URL or content type
                    content_type = resp.headers.get("content-type", "image/png")
                    ext = "png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = "jpg"
                    elif "gif" in content_type:
                        ext = "gif"
                    elif "webp" in content_type:
                        ext = "webp"

                    import io
                    file = discord.File(io.BytesIO(image_data), filename=f"image.{ext}")

                    msg = await channel.send(
                        content=caption if caption else None,
                        file=file,
                    )
                    return SendResult(success=True, message_id=str(msg.id))

        except ImportError:
            logger.warning(
                "[%s] aiohttp not installed, falling back to URL. Run: pip install aiohttp",
                self.name,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[%s] Failed to send image attachment, falling back to URL: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local video file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, video_path, caption)
        except FileNotFoundError:
            return SendResult(success=False, error=f"Video file not found: {video_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send local video, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an arbitrary file natively as a Discord attachment."""
        try:
            return await self._send_file_attachment(chat_id, file_path, caption, file_name=file_name)
        except FileNotFoundError:
            return SendResult(success=False, error=f"File not found: {file_path}")
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to send document, falling back to base adapter: %s", self.name, e, exc_info=True)
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to, metadata=metadata)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Start a persistent typing indicator for a channel.

        Discord's TYPING_START gateway event is unreliable in DMs for bots.
        Instead, start a background loop that hits the typing endpoint every
        8 seconds (typing indicator lasts ~10s). The loop is cancelled when
        stop_typing() is called (after the response is sent).
        """
        await discord_delivery.start_typing(
            self._client,
            chat_id,
            self._typing_tasks,
        )

    async def stop_typing(self, chat_id: str) -> None:
        """Stop the persistent typing indicator for a channel."""
        await discord_delivery.stop_typing(chat_id, self._typing_tasks)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Discord channel."""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}

        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))

            if not channel:
                return {"name": str(chat_id), "type": "dm"}

            # Determine channel type
            if isinstance(channel, discord.DMChannel):
                chat_type = "dm"
                name = channel.recipient.name if channel.recipient else str(chat_id)
            elif isinstance(channel, discord.Thread):
                chat_type = "thread"
                name = channel.name
            elif isinstance(channel, discord.TextChannel):
                chat_type = "channel"
                name = f"#{channel.name}"
                if channel.guild:
                    name = f"{channel.guild.name} / {name}"
            else:
                chat_type = "channel"
                name = getattr(channel, "name", str(chat_id))

            return {
                "name": name,
                "type": chat_type,
                "guild_id": str(channel.guild.id) if hasattr(channel, "guild") and channel.guild else None,
                "guild_name": channel.guild.name if hasattr(channel, "guild") and channel.guild else None,
            }
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("[%s] Failed to get chat info for %s: %s", self.name, chat_id, e, exc_info=True)
            return {"name": str(chat_id), "type": "dm", "error": str(e)}

    async def fetch_channel_history(
        self,
        channel_id: str,
        limit: int = 100,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> list:
        """Fetch bounded message history."""
        if not self._client:
            return []
        messages = await discord_history.fetch_history(
            self._client,
            channel_id,
            limit=limit,
            before=before,
            after=after,
        )
        return [asdict(message) for message in messages]

    async def list_threads(
        self,
        channel_id: str,
        include_archived: bool = False,
        limit: int = 100,
        before: Optional[Any] = None,
        private: bool = False,
        joined: bool = False,
    ) -> list:
        """List active and optionally archived threads for a channel."""
        if not self._client:
            return []
        return await discord_messaging.list_threads(
            self._client,
            channel_id,
            include_archived=include_archived,
            limit=limit,
            before=before,
            private=private,
            joined=joined,
        )

    async def reply_in_thread(
        self,
        thread_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a message to a Discord thread."""
        result = await discord_messaging.reply_in_thread(
            self._client,
            thread_id,
            content,
            reply_to=reply_to,
            format_message=self.format_message,
            truncate_message=self.truncate_message,
            max_message_length=self.MAX_MESSAGE_LENGTH,
            send_text_message=discord_delivery.send_text_message,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to reply in Discord thread %s: %s",
                self.name,
                thread_id,
                result.error,
            )
        return result

    async def search_channel_history(
        self,
        channel_id: str,
        query: str,
        limit: int = 50,
        author_id: Optional[str] = None,
    ) -> list:
        """Search channel history."""
        if not self._client:
            return []
        messages = await discord_history.search_history(
            self._client,
            channel_id,
            query,
            limit=limit,
            author_id=author_id,
        )
        return [asdict(message) for message in messages]

    async def get_channel_permissions(self, channel_id: str) -> Optional[dict]:
        """Get bot permissions for a channel."""
        if not self._client:
            return None
        permissions = await discord_permissions.check_channel_permissions(self._client, channel_id)
        return asdict(permissions) if permissions else None

    async def list_pins(
        self,
        channel_id: str,
        limit: int = 50,
        before: Optional[Any] = None,
        oldest_first: bool = False,
    ) -> list:
        """List pinned messages for a Discord channel or thread."""
        if not self._client:
            return []
        return await discord_messaging.list_pins(
            self._client,
            channel_id,
            limit=limit,
            before=before,
            oldest_first=oldest_first,
        )

    async def list_reactions(
        self,
        chat_id: str,
        message_id: str,
        limit: int = 100,
    ) -> list:
        """List reactions for a Discord message."""
        if not self._client:
            return []
        return await discord_messaging.list_reactions(
            self._client,
            chat_id,
            message_id,
            limit=limit,
        )

    async def pin_message(
        self,
        chat_id: str,
        message_id: str,
        reason: Optional[str] = None,
    ) -> SendResult:
        """Pin a Discord message."""
        result = await discord_messaging.pin_message(
            self._client,
            chat_id,
            message_id,
            reason=reason,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to pin Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def unpin_message(
        self,
        chat_id: str,
        message_id: str,
        reason: Optional[str] = None,
    ) -> SendResult:
        """Unpin a Discord message."""
        result = await discord_messaging.unpin_message(
            self._client,
            chat_id,
            message_id,
            reason=reason,
        )
        if not result.success and result.error:
            logger.error(
                "[%s] Failed to unpin Discord message %s: %s",
                self.name,
                message_id,
                result.error,
            )
        return result

    async def get_accessible_channels(self, guild_id: Optional[str] = None) -> list:
        """List accessible channels."""
        if not self._client:
            return []
        channels = await discord_permissions.list_accessible_channels(self._client, guild_id=guild_id)
        return [asdict(channel) for channel in channels]

    async def _resolve_allowed_usernames(self) -> None:
        """
        Resolve non-numeric entries in DISCORD_ALLOWED_USERS to Discord user IDs.

        Users can specify usernames (e.g. "teknium") or display names instead of
        raw numeric IDs.  After resolution, the env var and internal set are updated
        so authorization checks work with IDs only.
        """
        if not self._allowed_user_ids or not self._client:
            return

        numeric_ids = set()
        to_resolve = set()

        for entry in self._allowed_user_ids:
            if entry.isdigit():
                numeric_ids.add(entry)
            else:
                to_resolve.add(entry.lower())

        if not to_resolve:
            return

        print(f"[{self.name}] Resolving {len(to_resolve)} username(s): {', '.join(to_resolve)}")
        resolved_count = 0

        for guild in self._client.guilds:
            # Fetch full member list (requires members intent)
            try:
                members = guild.members
                if len(members) < guild.member_count:
                    members = [m async for m in guild.fetch_members(limit=None)]
            except Exception as e:
                logger.warning("Failed to fetch members for guild %s: %s", guild.name, e)
                continue

            for member in members:
                name_lower = member.name.lower()
                display_lower = member.display_name.lower()
                global_lower = (member.global_name or "").lower()

                matched = name_lower in to_resolve or display_lower in to_resolve or global_lower in to_resolve
                if matched:
                    uid = str(member.id)
                    numeric_ids.add(uid)
                    resolved_count += 1
                    matched_name = name_lower if name_lower in to_resolve else (
                        display_lower if display_lower in to_resolve else global_lower
                    )
                    to_resolve.discard(matched_name)
                    print(f"[{self.name}] Resolved '{matched_name}' -> {uid} ({member.name}#{member.discriminator})")

            if not to_resolve:
                break

        if to_resolve:
            print(f"[{self.name}] Could not resolve usernames: {', '.join(to_resolve)}")

        # Update internal set and env var so gateway auth checks use IDs
        self._allowed_user_ids = numeric_ids
        os.environ["DISCORD_ALLOWED_USERS"] = ",".join(sorted(numeric_ids))
        if resolved_count:
            print(f"[{self.name}] Updated DISCORD_ALLOWED_USERS with {resolved_count} resolved ID(s)")

    def format_message(self, content: str) -> str:
        """
        Format message for Discord.

        Discord uses its own markdown variant.
        """
        # Discord markdown is fairly standard, no special escaping needed
        return content

    async def _run_simple_slash(
        self,
        interaction: discord.Interaction,
        command_text: str,
        followup_msg: str | None = None,
    ) -> None:
        """Common handler for simple slash commands that dispatch a command string.

        Defers the interaction (shows "thinking..."), dispatches the command,
        then cleans up the deferred response.  If *followup_msg* is provided
        the "thinking..." indicator is replaced with that text; otherwise it
        is deleted so the channel isn't cluttered.
        """
        await interaction.response.defer(ephemeral=True)
        response = await self._invoke_native_slash_command(interaction, command_text)
        if response:
            await self._send_native_slash_content(interaction, response)
            return
        if followup_msg:
            try:
                await interaction.followup.send(followup_msg, ephemeral=True)
            except Exception as e:
                logger.debug("Discord followup failed: %s", e)

    async def _invoke_native_slash_command(
        self,
        interaction: discord.Interaction,
        command_text: str,
    ) -> str | None:
        """Run a native slash command directly through the gateway handler."""
        if not self._message_handler:
            return None
        event = self._build_slash_event(interaction, command_text)
        return await self._message_handler(event)

    async def _send_native_slash_content(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        """Send a native slash response back through the Discord interaction."""
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
        response = getattr(interaction, "response", None)
        followup = getattr(interaction, "followup", None)

        first_chunk_sent = False
        if response is not None and hasattr(response, "send_message"):
            is_done = getattr(response, "is_done", None)
            if not callable(is_done) or not is_done():
                await response.send_message(chunks[0], ephemeral=True)
                first_chunk_sent = True

        start_index = 1 if first_chunk_sent else 0
        if followup is not None and hasattr(followup, "send"):
            for chunk in chunks[start_index:]:
                await followup.send(chunk, ephemeral=True)
            return

        if not first_chunk_sent and response is not None and hasattr(response, "send_message"):
            await response.send_message(chunks[0], ephemeral=True)

    def _register_slash_commands(self) -> None:
        """Register Discord slash commands on the command tree."""
        if not self._client:
            return

        tree = self._client.tree
        discord_interactions.register_slash_commands(tree, self)

        @tree.command(name="queue", description="Queue a prompt for the next turn (doesn't interrupt)")
        @discord.app_commands.describe(prompt="The prompt to queue")
        async def slash_queue(interaction: discord.Interaction, prompt: str):
            await self._run_simple_slash(interaction, f"/queue {prompt}", "Queued for the next turn.")

        @tree.command(name="background", description="Run a prompt in the background")
        @discord.app_commands.describe(prompt="The prompt to run in the background")
        async def slash_background(interaction: discord.Interaction, prompt: str):
            await self._run_simple_slash(interaction, f"/background {prompt}", "Background task started~")

        @tree.command(name="btw", description="Ephemeral side question using session context")
        @discord.app_commands.describe(question="Your side question (no tools, not persisted)")
        async def slash_btw(interaction: discord.Interaction, question: str):
            await self._run_simple_slash(interaction, f"/btw {question}")

        # Register installed skills as native slash commands (parity with
        # Telegram, which uses telegram_menu_commands() in commands.py).
        # Discord allows up to 100 application commands globally.
        _DISCORD_CMD_LIMIT = 100
        try:
            from hermes_cli.commands import discord_skill_commands

            existing_names = {cmd.name for cmd in tree.get_commands()}
            remaining_slots = max(0, _DISCORD_CMD_LIMIT - len(existing_names))

            skill_entries, skipped = discord_skill_commands(
                max_slots=remaining_slots,
                reserved_names=existing_names,
            )

            for discord_name, description, cmd_key in skill_entries:
                # Closure factory to capture cmd_key per iteration
                def _make_skill_handler(_key: str):
                    async def _skill_slash(interaction: discord.Interaction, args: str = ""):
                        await self._run_simple_slash(interaction, f"{_key} {args}".strip())
                    return _skill_slash

                handler = _make_skill_handler(cmd_key)
                handler.__name__ = f"skill_{discord_name.replace('-', '_')}"

                cmd = discord.app_commands.Command(
                    name=discord_name,
                    description=description,
                    callback=handler,
                )
                discord.app_commands.describe(args="Optional arguments for the skill")(cmd)
                tree.add_command(cmd)

            if skipped:
                logger.warning(
                    "[%s] Discord slash command limit reached (%d): %d skill(s) not registered",
                    self.name, _DISCORD_CMD_LIMIT, skipped,
                )
        except Exception as exc:
            logger.warning("[%s] Failed to register skill slash commands: %s", self.name, exc)

    def _build_slash_event(self, interaction: discord.Interaction, text: str) -> MessageEvent:
        """Build a MessageEvent from a Discord slash command interaction."""
        return discord_command_sessions.build_slash_event(self, interaction, text)

    # ------------------------------------------------------------------
    # Thread creation helpers
    # ------------------------------------------------------------------

    async def _handle_thread_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> None:
        """Create a Discord thread from a slash command and start a session in it."""
        await discord_threads.handle_thread_create_slash(
            self,
            interaction,
            name,
            message,
            auto_archive_duration,
        )
    async def _dispatch_thread_session(
        self,
        interaction: discord.Interaction,
        thread_id: str,
        thread_name: str,
        text: str,
    ) -> None:
        """Build a MessageEvent pointing at a thread and send it through handle_message."""
        guild_name = ""
        if hasattr(interaction, "guild") and interaction.guild:
            guild_name = interaction.guild.name

        chat_name = f"{guild_name} / {thread_name}" if guild_name else thread_name

        # Inherit forum topic when the thread was created inside a forum channel.
        _chan = getattr(interaction, "channel", None)
        chat_topic = self._get_effective_topic(_chan, is_thread=True) if _chan else None

        source = self.build_source(
            chat_id=thread_id,
            chat_name=chat_name,
            chat_type="thread",
            user_id=str(interaction.user.id),
            user_name=interaction.user.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )

        _parent_id = str(getattr(getattr(interaction, "channel", None), "parent_id", "") or "")
        _skills = self._resolve_channel_skills(thread_id, _parent_id or None)
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=interaction,
            auto_skill=_skills,
        )
        await self.handle_message(event)

    def _resolve_channel_skills(self, channel_id: str, parent_id: str | None = None) -> list[str] | None:
        """Look up auto-skill bindings for a Discord channel/forum thread.

        Config format (in platform extra):
            channel_skill_bindings:
              - id: "123456"
                skills: ["skill-a", "skill-b"]
        Also checks parent_id so forum threads inherit the forum's bindings.
        """
        bindings = self.config.extra.get("channel_skill_bindings", [])
        if not bindings:
            return None
        ids_to_check = {channel_id}
        if parent_id:
            ids_to_check.add(parent_id)
        for entry in bindings:
            entry_id = str(entry.get("id", ""))
            if entry_id in ids_to_check:
                skills = entry.get("skills") or entry.get("skill")
                if isinstance(skills, str):
                    return [skills]
                if isinstance(skills, list) and skills:
                    return list(dict.fromkeys(skills))  # dedup, preserve order
        return None
    def _thread_parent_channel(self, channel: Any) -> Any:
        """Return the parent text channel when invoked from a thread."""
        return discord_threads.thread_parent_channel(channel)

    async def _resolve_interaction_channel(self, interaction: discord.Interaction) -> Optional[Any]:
        """Return the interaction channel, fetching it if the payload is partial."""
        return await discord_threads.resolve_interaction_channel(self._client, interaction)

    async def _create_thread(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        message: str = "",
        auto_archive_duration: int = 1440,
    ) -> Dict[str, Any]:
        """Create a thread in the current Discord channel.

        Tries ``parent_channel.create_thread()`` first.  If Discord rejects
        that (e.g. permission issues), falls back to sending a seed message
        and creating the thread from it.
        """
        return await discord_threads.create_thread(
            self._client,
            interaction,
            name,
            message,
            auto_archive_duration,
            discord_threads.resolve_interaction_channel,
        )

    # ------------------------------------------------------------------
    # Auto-thread helpers
    # ------------------------------------------------------------------

    async def _auto_create_thread(self, message: 'DiscordMessage') -> Optional[Any]:
        """Create a thread from a user message for auto-threading.

        Returns the created thread object, or ``None`` on failure.
        """
        return await discord_threads.auto_create_thread(message)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
        approval_id: Optional[str] = None,
    ) -> SendResult:
        """
        Send a button-based exec approval prompt for a dangerous command.

        The buttons call ``resolve_gateway_approval()`` to unblock the waiting
        agent thread — this replaces the text-based ``/approve`` flow on Discord.
        """
        if not self._client or not DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")

        try:
            # Resolve channel — use thread_id from metadata if present
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]

            channel = await discord_delivery.resolve_channel(self._client, target_id)
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            # Discord embed description limit is 4096; show full command up to that
            max_desc = 4088
            cmd_display = command if len(command) <= max_desc else command[: max_desc - 3] + "..."
            embed = discord.Embed(
                title="⚠️ Command Approval Required",
                description=f"```\n{cmd_display}\n```",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Reason", value=description, inline=False)
            approval_id = str(approval_id or session_key or "").strip()
            view = discord_interactions.create_exec_approval_view(
                self,
                approval_id=approval_id,
                allowed_user_ids=self._allowed_user_ids,
                runtime=self._component_runtime,
            )
            if approval_id:
                embed.set_footer(text=f"Approval ID: {approval_id}")
            else:
                return SendResult(success=False, error="Missing approval ID")

            msg = await channel.send(embed=embed, view=view)
            if hasattr(view, "bind_message"):
                view.bind_message(str(msg.id))
            return SendResult(success=True, message_id=str(msg.id))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_update_prompt(
        self, chat_id: str, prompt: str, default: str = "",
        session_key: str = "",
    ) -> SendResult:
        """Send an interactive button-based update prompt (Yes / No).

        Used by the gateway ``/update`` watcher when ``hermes update --gateway``
        needs user input (stash restore, config migration).
        """
        if not self._client or not DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")
        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                channel = await self._client.fetch_channel(int(chat_id))

            default_hint = f" (default: {default})" if default else ""
            embed = discord.Embed(
                title="⚕ Update Needs Your Input",
                description=f"{prompt}{default_hint}",
                color=discord.Color.gold(),
            )
            view = UpdatePromptView(
                session_key=session_key,
                allowed_user_ids=self._allowed_user_ids,
            )
            msg = await channel.send(embed=embed, view=view)
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive select-menu model picker.

        Two-step drill-down: provider dropdown → model dropdown.
        Uses Discord embeds + Select menus via ``ModelPickerView``.
        """
        if not self._client or not DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")

        try:
            # Resolve target channel (use thread_id if present)
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]

            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))

            try:
                from hermes_cli.providers import get_label
                provider_label = get_label(current_provider)
            except Exception:
                provider_label = current_provider

            embed = discord.Embed(
                title="⚙ Model Configuration",
                description=(
                    f"Current model: `{current_model or 'unknown'}`\n"
                    f"Provider: {provider_label}\n\n"
                    f"Select a provider:"
                ),
                color=discord.Color.blue(),
            )

            view = ModelPickerView(
                providers=providers,
                current_model=current_model,
                current_provider=current_provider,
                session_key=session_key,
                on_model_selected=on_model_selected,
                allowed_user_ids=self._allowed_user_ids,
            )

            msg = await channel.send(embed=embed, view=view)
            if hasattr(view, "bind_message"):
                view.bind_message(str(msg.id))
            return SendResult(success=True, message_id=str(msg.id))

        except Exception as e:
            logger.warning("[%s] send_model_picker failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    def _get_parent_channel_id(self, channel: Any) -> Optional[str]:
        """Return the parent channel ID for a Discord thread-like channel, if present."""
        return discord_intake.get_parent_channel_id(channel)

    @staticmethod
    def _runtime_state_path():
        """Path to the persisted Discord runtime controls."""
        return discord_runtime_state.runtime_state_path()

    @classmethod
    def _load_runtime_state(cls) -> tuple[dict[str, discord_runtime_state.DiscordThreadBinding], dict[str, str]]:
        """Load persisted Discord runtime controls."""
        return discord_runtime_state.load_runtime_state()

    def _save_runtime_state(self) -> None:
        """Persist Discord thread bindings and activation overrides."""
        discord_runtime_state.save_runtime_state(
            self._thread_bindings,
            self._activation_overrides,
        )

    def get_thread_binding(self, thread_id: str) -> Optional[discord_runtime_state.DiscordThreadBinding]:
        """Return the persisted focus binding for a thread, if any."""
        return self._thread_bindings.get(str(thread_id))

    def list_thread_bindings(self, parent_chat_id: Optional[str] = None) -> list[discord_runtime_state.DiscordThreadBinding]:
        """Return all thread bindings, optionally scoped to one parent channel."""
        bindings = list(self._thread_bindings.values())
        if parent_chat_id is None:
            return sorted(bindings, key=lambda binding: (binding.parent_chat_id or "", binding.chat_name, binding.thread_id))
        target_parent = str(parent_chat_id)
        return sorted(
            [binding for binding in bindings if str(binding.parent_chat_id or "") == target_parent],
            key=lambda binding: (binding.chat_name, binding.thread_id),
        )

    def focus_thread_binding(
        self,
        *,
        thread_id: str,
        session_key: str,
        chat_name: str,
        parent_chat_id: Optional[str],
        bound_by: str,
        idle_timeout_minutes: Optional[int] = None,
        max_age_minutes: Optional[int] = None,
    ) -> discord_runtime_state.DiscordThreadBinding:
        """Create or update a persisted focus binding for a Discord thread."""
        now = datetime.now().isoformat()
        existing = self._thread_bindings.get(str(thread_id))
        binding = discord_runtime_state.DiscordThreadBinding(
            thread_id=str(thread_id),
            session_key=session_key,
            chat_id=str(thread_id),
            parent_chat_id=str(parent_chat_id) if parent_chat_id else None,
            chat_name=chat_name,
            bound_by=bound_by,
            bound_at=existing.bound_at if existing and existing.bound_at else now,
            last_activity_at=now,
            idle_timeout_minutes=(
                int(idle_timeout_minutes)
                if idle_timeout_minutes is not None
                else (existing.idle_timeout_minutes if existing else discord_runtime_state.DEFAULT_THREAD_BINDING_IDLE_MINUTES)
            ),
            max_age_minutes=(
                int(max_age_minutes)
                if max_age_minutes is not None
                else (existing.max_age_minutes if existing else discord_runtime_state.DEFAULT_THREAD_BINDING_MAX_AGE_MINUTES)
            ),
        )
        self._thread_bindings[binding.thread_id] = binding
        self._track_thread(binding.thread_id)
        self._save_runtime_state()
        return binding

    def unfocus_thread_binding(self, thread_id: str) -> Optional[discord_runtime_state.DiscordThreadBinding]:
        """Remove a persisted focus binding for a thread."""
        binding = self._thread_bindings.pop(str(thread_id), None)
        if binding:
            self._bot_participated_threads.discard(str(thread_id))
            self._save_participated_threads()
            self._save_runtime_state()
        return binding

    def update_thread_binding_limits(
        self,
        thread_id: str,
        *,
        idle_timeout_minutes: Optional[int] = None,
        max_age_minutes: Optional[int] = None,
    ) -> Optional[discord_runtime_state.DiscordThreadBinding]:
        """Update idle/max-age settings for an existing thread binding."""
        existing = self._thread_bindings.get(str(thread_id))
        if existing is None:
            return None
        updated = discord_runtime_state.DiscordThreadBinding(
            thread_id=existing.thread_id,
            session_key=existing.session_key,
            chat_id=existing.chat_id,
            parent_chat_id=existing.parent_chat_id,
            chat_name=existing.chat_name,
            bound_by=existing.bound_by,
            bound_at=existing.bound_at,
            last_activity_at=existing.last_activity_at,
            idle_timeout_minutes=(
                int(idle_timeout_minutes)
                if idle_timeout_minutes is not None
                else existing.idle_timeout_minutes
            ),
            max_age_minutes=(
                int(max_age_minutes)
                if max_age_minutes is not None
                else existing.max_age_minutes
            ),
        )
        self._thread_bindings[updated.thread_id] = updated
        self._save_runtime_state()
        return updated

    def touch_thread_binding(
        self,
        thread_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[discord_runtime_state.DiscordThreadBinding]:
        """Refresh last-activity time for a thread binding."""
        binding = self._thread_bindings.get(str(thread_id))
        if binding is None:
            return None
        updated = discord_runtime_state.touch_binding(binding, now=now)
        self._thread_bindings[updated.thread_id] = updated
        self._save_runtime_state()
        return updated

    def expire_thread_binding_if_needed(
        self,
        thread_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Expire a focused thread binding when idle/max-age thresholds are reached."""
        binding = self._thread_bindings.get(str(thread_id))
        if binding is None:
            return None
        reason = discord_runtime_state.binding_expiration_reason(binding, now=now)
        if reason is None:
            return None
        self.unfocus_thread_binding(thread_id)
        return reason

    def get_activation_mode(self, chat_id: str) -> Optional[str]:
        """Return the Discord activation override for a chat, if any."""
        return self._activation_overrides.get(str(chat_id))

    def set_activation_mode(self, chat_id: str, mode: str) -> Optional[str]:
        """Set or clear the Discord activation override for a chat."""
        normalized = str(mode or "").strip().lower()
        key = str(chat_id)
        if normalized in {"", "inherit", "default", "reset"}:
            previous = self._activation_overrides.pop(key, None)
            self._save_runtime_state()
            return previous
        if normalized not in {"mention", "always"}:
            raise ValueError(f"Unsupported activation mode: {mode}")
        self._activation_overrides[key] = normalized
        self._save_runtime_state()
        return normalized

    def _is_forum_parent(self, channel: Any) -> bool:
        """Best-effort check for whether a Discord channel is a forum channel."""
        return discord_intake.is_forum_parent(channel)

    def _get_effective_topic(self, channel: Any, is_thread: bool = False) -> Optional[str]:
        """Return the channel topic, falling back to the parent forum's topic for forum threads."""
        topic = getattr(channel, "topic", None)
        if not topic and is_thread:
            parent = getattr(channel, "parent", None)
            if parent and self._is_forum_parent(parent):
                topic = getattr(parent, "topic", None)
        return topic

    def _format_thread_chat_name(self, thread: Any) -> str:
        """Build a readable chat name for thread-like Discord channels, including forum context when available."""
        return discord_intake.format_thread_chat_name(thread, self._is_forum_parent)

    # ------------------------------------------------------------------
    # Thread participation persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _thread_state_path():
        """Path to the persisted thread participation set."""
        return discord_state.thread_state_path()

    @classmethod
    def _load_participated_threads(cls) -> set:
        """Load persisted thread IDs from disk."""
        return discord_state.load_participated_threads()

    def _save_participated_threads(self) -> None:
        """Persist the current thread set (compat wrapper around tracker storage)."""
        self._threads._threads = set(self._bot_participated_threads)
        self._threads._save()
        self._bot_participated_threads = self._threads._threads

    def _track_thread(self, thread_id: str) -> None:
        """Add a thread to the participation set and persist."""
        self._threads.mark(thread_id)
        self._bot_participated_threads = self._threads._threads

    async def _handle_message(self, message: DiscordMessage) -> None:
        """Handle incoming Discord messages."""
        # In server channels (not DMs), require the bot to be @mentioned
        # UNLESS the channel is in the free-response list or the message is
        # in a thread where the bot has already participated.
        #
        # Config (all settable via discord.* in config.yaml or DISCORD_* env vars):
        #   discord.require_mention: Require @mention in server channels (default: true)
        #   discord.free_response_channels: Channel IDs where bot responds without mention
        #   discord.ignored_channels: Channel IDs where bot NEVER responds (even when mentioned)
        #   discord.allowed_channels: If set, bot ONLY responds in these channels (whitelist)
        #   discord.no_thread_channels: Channel IDs where bot responds directly without creating thread
        #   discord.auto_thread: Auto-create thread on @mention in channels (default: true)

        thread_id = None
        parent_channel_id = None
        is_thread = isinstance(message.channel, discord.Thread) or getattr(message.channel, "parent", None) is not None
        policy = self._get_discord_policy()
        active_binding = None
        if is_thread:
            thread_id = str(message.channel.id)
            parent_channel_id = self._get_parent_channel_id(message.channel)
            self.expire_thread_binding_if_needed(thread_id)
            active_binding = self.get_thread_binding(thread_id)

        if not isinstance(message.channel, discord.DMChannel):
            channel_ids = {str(message.channel.id)}
            free_channels = policy.free_response_channels
            if parent_channel_id:
                channel_ids.add(parent_channel_id)

            allowed_channels_raw = os.getenv("DISCORD_ALLOWED_CHANNELS", "")
            if allowed_channels_raw:
                allowed_channels = {ch.strip() for ch in allowed_channels_raw.split(",") if ch.strip()}
                if not (channel_ids & allowed_channels):
                    logger.debug("[%s] Ignoring message in non-allowed channel: %s", self.name, channel_ids)
                    return

            ignored_channels_raw = os.getenv("DISCORD_IGNORED_CHANNELS", "")
            ignored_channels = {ch.strip() for ch in ignored_channels_raw.split(",") if ch.strip()}
            if channel_ids & ignored_channels:
                logger.debug("[%s] Ignoring message in ignored channel: %s", self.name, channel_ids)
                return

            require_mention = policy.require_mention
            is_free_channel = bool(channel_ids & free_channels)
            for channel_id in channel_ids:
                activation_mode = self.get_activation_mode(channel_id)
                if activation_mode == "always":
                    require_mention = False
                    is_free_channel = True
                    break
                if activation_mode == "mention":
                    require_mention = True
                    is_free_channel = False

            # Skip the mention check if the message is in a thread where
            # the bot has previously participated (auto-created or replied in).
            in_bot_thread = bool(
                is_thread
                and thread_id
                and (
                    thread_id in self._threads
                    or active_binding is not None
                )
            )
            is_mentioned = bool(self._client.user and self._client.user in message.mentions)

            if discord_intake.should_skip_for_mention(
                require_mention=require_mention,
                is_free_channel=is_free_channel,
                in_bot_thread=in_bot_thread,
                is_mentioned=is_mentioned,
            ):
                return

            if is_mentioned and self._client.user:
                message.content = discord_intake.strip_mention(message.content, self._client.user.id)

        inline_command, remaining_text = discord_native_commands.extract_inline_shortcut(
            message.content
        )

        # Auto-thread: when enabled, automatically create a thread for every
        # @mention in a text channel so each conversation is isolated (like Slack).
        # Messages already inside threads or DMs are unaffected.
        # no_thread_channels: channels where bot responds directly without thread.
        auto_threaded_channel = None
        if not is_thread and not isinstance(message.channel, discord.DMChannel):
            no_thread_channels_raw = os.getenv("DISCORD_NO_THREAD_CHANNELS", "")
            no_thread_channels = {ch.strip() for ch in no_thread_channels_raw.split(",") if ch.strip()}
            skip_thread = bool(channel_ids & no_thread_channels)
            auto_thread = policy.auto_thread
            if auto_thread and not skip_thread:
                thread = await self._auto_create_thread(message)
                if thread:
                    is_thread = True
                    thread_id = str(thread.id)
                    auto_threaded_channel = thread
                    self._threads.mark(thread_id)

        # Determine message type
        msg_type = MessageType(
            discord_intake.classify_message_type(message.content, message.attachments)
        )

        # When auto-threading kicked in, route responses to the new thread
        effective_channel = auto_threaded_channel or message.channel

        # Determine chat type
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "dm"
            chat_name = message.author.name
        elif is_thread:
            chat_type = "thread"
            chat_name = self._format_thread_chat_name(effective_channel)
        else:
            chat_type = "group"
            chat_name = getattr(message.channel, "name", str(message.channel.id))
            if hasattr(message.channel, "guild") and message.channel.guild:
                chat_name = f"{message.channel.guild.name} / #{chat_name}"

        # Get channel topic (if available - TextChannels have topics, DMs/threads don't).
        # For threads whose parent is a forum channel, inherit the parent's topic
        # so forum descriptions (e.g. project instructions) appear in the session context.
        chat_topic = self._get_effective_topic(message.channel, is_thread=is_thread)

        # Build source
        source = self.build_source(
            chat_id=str(effective_channel.id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            thread_id=thread_id,
            chat_topic=chat_topic,
        )

        # Build media URLs -- download image attachments to local cache so the
        # vision tool can access them reliably (Discord CDN URLs can expire).
        media_urls = []
        media_types = []
        pending_text_injection: Optional[str] = None
        for att in message.attachments:
            content_type = att.content_type or "unknown"
            if content_type.startswith("image/"):
                try:
                    # Determine extension from content type (image/png -> .png)
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    cached_path = await cache_image_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user image: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache image attachment: {e}", flush=True)
                    # Fall back to the CDN URL if caching fails
                    media_urls.append(att.url)
                    media_types.append(content_type)
            elif content_type.startswith("audio/"):
                try:
                    ext = "." + content_type.split("/")[-1].split(";")[0]
                    if ext not in (".ogg", ".mp3", ".wav", ".webm", ".m4a"):
                        ext = ".ogg"
                    cached_path = await cache_audio_from_url(att.url, ext=ext)
                    media_urls.append(cached_path)
                    media_types.append(content_type)
                    print(f"[Discord] Cached user audio: {cached_path}", flush=True)
                except Exception as e:
                    print(f"[Discord] Failed to cache audio attachment: {e}", flush=True)
                    media_urls.append(att.url)
                    media_types.append(content_type)
            else:
                # Document attachments: download, cache, and optionally inject text
                ext = ""
                if att.filename:
                    _, ext = os.path.splitext(att.filename)
                    ext = ext.lower()
                if not ext and content_type:
                    mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                    ext = mime_to_ext.get(content_type, "")
                if ext not in SUPPORTED_DOCUMENT_TYPES:
                    logger.warning(
                        "[Discord] Unsupported document type '%s' (%s), skipping",
                        ext or "unknown", content_type,
                    )
                else:
                    MAX_DOC_BYTES = 32 * 1024 * 1024
                    if att.size and att.size > MAX_DOC_BYTES:
                        logger.warning(
                            "[Discord] Document too large (%s bytes), skipping: %s",
                            att.size, att.filename,
                        )
                    else:
                        try:
                            import aiohttp
                            from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
                            _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
                            _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
                            async with aiohttp.ClientSession(**_sess_kw) as session:
                                async with session.get(
                                    att.url,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                    **_req_kw,
                                ) as resp:
                                    if resp.status != 200:
                                        raise Exception(f"HTTP {resp.status}")
                                    raw_bytes = await resp.read()
                            cached_path = cache_document_from_bytes(
                                raw_bytes, att.filename or f"document{ext}"
                            )
                            doc_mime = SUPPORTED_DOCUMENT_TYPES[ext]
                            media_urls.append(cached_path)
                            media_types.append(doc_mime)
                            logger.info("[Discord] Cached user document: %s", cached_path)
                            # Inject text content for plain-text documents (capped at 100 KB)
                            MAX_TEXT_INJECT_BYTES = 100 * 1024
                            if ext in (".md", ".txt", ".log") and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                                try:
                                    text_content = raw_bytes.decode("utf-8")
                                    display_name = att.filename or f"document{ext}"
                                    display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                                    injection = f"[Content of {display_name}]:\n{text_content}"
                                    if pending_text_injection:
                                        pending_text_injection = f"{pending_text_injection}\n\n{injection}"
                                    else:
                                        pending_text_injection = injection
                                except UnicodeDecodeError:
                                    pass
                        except Exception as e:
                            logger.warning(
                                "[Discord] Failed to cache document %s: %s",
                                att.filename, e, exc_info=True,
                            )

        event_text = message.content
        if pending_text_injection:
            event_text = f"{pending_text_injection}\n\n{event_text}" if event_text else pending_text_injection
        if msg_type == MessageType.DOCUMENT and not media_urls and not pending_text_injection:
            msg_type = MessageType.TEXT

        # Defense-in-depth: prevent empty user messages from entering session
        # (can happen when user sends @mention-only with no other text)
        if not event_text or not event_text.strip():
            event_text = "(The user sent a message with no text content)"
        if event_text.startswith("/"):
            effective_msg_type = MessageType.COMMAND
        elif any(mt.startswith("image/") for mt in media_types):
            effective_msg_type = MessageType.PHOTO
        elif any(mt.startswith("video/") for mt in media_types):
            effective_msg_type = MessageType.VIDEO
        elif any(mt.startswith("audio/") for mt in media_types):
            effective_msg_type = MessageType.AUDIO
        elif media_urls:
            effective_msg_type = MessageType.DOCUMENT
        else:
            effective_msg_type = MessageType.TEXT

        _chan = message.channel
        _parent_id = str(getattr(_chan, "parent_id", "") or "")
        _chan_id = str(getattr(_chan, "id", ""))
        _skills = self._resolve_channel_skills(_chan_id, _parent_id or None)
        event = MessageEvent(
            text=event_text,
            message_type=effective_msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.id),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=str(message.reference.message_id) if message.reference else None,
            timestamp=message.created_at,
            auto_skill=_skills,
        )

        if inline_command:
            inline_event = MessageEvent(
                text=f"/{inline_command}",
                message_type=MessageType.COMMAND,
                source=source,
                raw_message=message,
                message_id=str(message.id),
                reply_to_message_id=event.reply_to_message_id,
                timestamp=message.created_at,
                metadata={"is_inline_shortcut": True},
            )
            await self.handle_message(inline_event)
            if not remaining_text and not media_urls:
                return
            event.text = remaining_text
            event.message_type = MessageType.TEXT

        # Track thread participation so the bot won't require @mention for
        # follow-up messages in threads it has already engaged in.
        if thread_id:
            self._track_thread(thread_id)
            if active_binding is not None:
                self.touch_thread_binding(thread_id)

        # Only batch plain text messages — commands, media, etc. dispatch
        # immediately since they won't be split by the Discord client.
        if msg_type == MessageType.TEXT and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    # ------------------------------------------------------------------
    # Text message aggregation (handles Discord client-side splits)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When Discord splits a long user message at 2000 chars, the chunks
        arrive within a few hundred milliseconds.  This merges them into
        a single event before dispatching.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.

        Uses a longer delay when the latest chunk is near Discord's 2000-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[Discord] Flushing text batch %s (%d chars)",
                key, len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)


# ---------------------------------------------------------------------------
# Discord UI Components (outside the adapter class)
# ---------------------------------------------------------------------------

if DISCORD_AVAILABLE:

    _DISCORD_BUTTON_STYLE_SECONDARY = getattr(
        discord.ButtonStyle,
        "grey",
        getattr(discord.ButtonStyle, "gray", discord.ButtonStyle.blurple),
    )

    class ExecApprovalView(discord.ui.View):
        """
        Interactive button view for exec approval of dangerous commands.

        Shows four buttons: Allow Once, Allow Session, Always Allow, Deny.
        Clicking a button calls ``resolve_gateway_approval()`` to unblock the
        waiting agent thread — the same mechanism as the text ``/approve`` flow.
        Only users in the allowed list can click.  Times out after 5 minutes.
        """

        def __init__(self, session_key: str, allowed_user_ids: set):
            super().__init__(timeout=300)  # 5-minute timeout
            self.session_key = session_key
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Verify the user clicking is authorized."""
            if not self.allowed_user_ids:
                return True  # No allowlist = anyone can approve
            return str(interaction.user.id) in self.allowed_user_ids

        async def _resolve(
            self, interaction: discord.Interaction, choice: str,
            color: discord.Color, label: str,
        ):
            """Resolve the approval via the gateway approval queue and update the embed."""
            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return

            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to approve commands~", ephemeral=True
                )
                return

            self.resolved = True

            # Update the embed with the decision
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{label} by {interaction.user.display_name}")

            # Disable all buttons
            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            # Unblock the waiting agent thread via the gateway approval queue
            try:
                from tools.approval import resolve_gateway_approval
                count = resolve_gateway_approval(self.session_key, choice)
                logger.info(
                    "Discord button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                    count, self.session_key, choice, interaction.user.display_name,
                )
            except Exception as exc:
                logger.error("Failed to resolve gateway approval from button: %s", exc)

        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "once", discord.Color.green(), "Approved once")

        @discord.ui.button(label="Allow Session", style=_DISCORD_BUTTON_STYLE_SECONDARY)
        async def allow_session(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "session", discord.Color.blue(), "Approved for session")

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "always", discord.Color.purple(), "Approved permanently")

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "deny", discord.Color.red(), "Denied")

        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True

    class UpdatePromptView(discord.ui.View):
        """Interactive Yes/No buttons for ``hermes update`` prompts.

        Clicking a button writes the answer to ``.update_response`` so the
        detached update process can pick it up.  Only authorized users can
        click.  Times out after 5 minutes (the update process also has a
        5-minute timeout on its side).
        """

        def __init__(self, session_key: str, allowed_user_ids: set):
            super().__init__(timeout=300)
            self.session_key = session_key
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            if not self.allowed_user_ids:
                return True
            return str(interaction.user.id) in self.allowed_user_ids

        async def _respond(
            self, interaction: discord.Interaction, answer: str,
            color: discord.Color, label: str,
        ):
            if self.resolved:
                await interaction.response.send_message(
                    "Already answered~", ephemeral=True
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self.resolved = True

            # Update embed
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{label} by {interaction.user.display_name}")

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)

            # Write response file
            try:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
                response_path = home / ".update_response"
                tmp = response_path.with_suffix(".tmp")
                tmp.write_text(answer)
                tmp.replace(response_path)
                logger.info(
                    "Discord update prompt answered '%s' by %s",
                    answer, interaction.user.display_name,
                )
            except Exception as exc:
                logger.error("Failed to write update response: %s", exc)

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.green, emoji="✓")
        async def yes_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._respond(interaction, "y", discord.Color.green(), "Yes")

        @discord.ui.button(label="No", style=discord.ButtonStyle.red, emoji="✗")
        async def no_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._respond(interaction, "n", discord.Color.red(), "No")

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True

    class ModelPickerView(discord.ui.View):
        """Interactive select-menu view for model switching.

        Two-step drill-down: provider dropdown → model dropdown.
        Edits the original message in-place as the user navigates.
        Times out after 2 minutes.
        """

        def __init__(
            self,
            providers: list,
            current_model: str,
            current_provider: str,
            session_key: str,
            on_model_selected,
            allowed_user_ids: set,
        ):
            super().__init__(timeout=120)
            self.providers = providers
            self.current_model = current_model
            self.current_provider = current_provider
            self.session_key = session_key
            self.on_model_selected = on_model_selected
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False
            self._selected_provider: str = ""

            self._build_provider_select()

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            if not self.allowed_user_ids:
                return True
            return str(interaction.user.id) in self.allowed_user_ids

        def _build_provider_select(self):
            """Build the provider dropdown menu."""
            self.clear_items()
            options = []
            for p in self.providers:
                count = p.get("total_models", len(p.get("models", [])))
                label = f"{p['name']} ({count} models)"
                desc = "current" if p.get("is_current") else None
                options.append(
                    discord.SelectOption(
                        label=label[:100],
                        value=p["slug"],
                        description=desc,
                    )
                )
            if not options:
                return

            select = discord.ui.Select(
                placeholder="Choose a provider...",
                options=options[:25],
                custom_id="model_provider_select",
            )
            select.callback = self._on_provider_selected
            self.add_item(select)

            cancel_btn = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.red, custom_id="model_cancel"
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)

        def _build_model_select(self, provider_slug: str):
            """Build the model dropdown for a specific provider."""
            self.clear_items()
            provider = next(
                (p for p in self.providers if p["slug"] == provider_slug), None
            )
            if not provider:
                return

            models = provider.get("models", [])
            options = []
            for model_id in models[:25]:
                short = model_id.split("/")[-1] if "/" in model_id else model_id
                options.append(
                    discord.SelectOption(
                        label=short[:100],
                        value=model_id[:100],
                    )
                )
            if not options:
                return

            select = discord.ui.Select(
                placeholder=f"Choose a model from {provider.get('name', provider_slug)}...",
                options=options,
                custom_id="model_model_select",
            )
            select.callback = self._on_model_selected
            self.add_item(select)

            back_btn = discord.ui.Button(
                label="◀ Back", style=_DISCORD_BUTTON_STYLE_SECONDARY, custom_id="model_back"
            )
            back_btn.callback = self._on_back
            self.add_item(back_btn)

            cancel_btn = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.red, custom_id="model_cancel2"
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)

        async def _on_provider_selected(self, interaction: discord.Interaction):
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            provider_slug = interaction.data["values"][0]
            self._selected_provider = provider_slug
            provider = next(
                (p for p in self.providers if p["slug"] == provider_slug), None
            )
            pname = provider.get("name", provider_slug) if provider else provider_slug

            self._build_model_select(provider_slug)

            total = provider.get("total_models", 0) if provider else 0
            shown = min(len(provider.get("models", [])), 25) if provider else 0
            extra = f"\n*{total - shown} more available — type `/model <name>` directly*" if total > shown else ""

            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description=f"Provider: **{pname}**\nSelect a model:{extra}",
                    color=discord.Color.blue(),
                ),
                view=self,
            )

        async def _on_model_selected(self, interaction: discord.Interaction):
            if self.resolved:
                await interaction.response.send_message(
                    "Already resolved~", ephemeral=True
                )
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self.resolved = True
            model_id = interaction.data["values"][0]

            try:
                result_text = await self.on_model_selected(
                    str(interaction.channel_id),
                    model_id,
                    self._selected_provider,
                )
            except Exception as exc:
                result_text = f"Error switching model: {exc}"

            self.clear_items()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Switched",
                    description=result_text,
                    color=discord.Color.green(),
                ),
                view=self,
            )

        async def _on_back(self, interaction: discord.Interaction):
            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized~", ephemeral=True
                )
                return

            self._build_provider_select()

            try:
                from hermes_cli.providers import get_label
                provider_label = get_label(self.current_provider)
            except Exception:
                provider_label = self.current_provider

            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description=(
                        f"Current model: `{self.current_model or 'unknown'}`\n"
                        f"Provider: {provider_label}\n\n"
                        f"Select a provider:"
                    ),
                    color=discord.Color.blue(),
                ),
                view=self,
            )

        async def _on_cancel(self, interaction: discord.Interaction):
            self.resolved = True
            self.clear_items()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚙ Model Configuration",
                    description="Model selection cancelled.",
                    color=discord.Color.greyple(),
                ),
                view=self,
            )

        async def on_timeout(self):
            self.resolved = True
            self.clear_items()
