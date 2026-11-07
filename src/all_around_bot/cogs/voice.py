"""Voice playback: queue YouTube audio and manage it with buttons."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import struct
import tempfile
import threading
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from discord.voice_state import has_dave

try:
    import davey
except ImportError:  # discord.py disables voice entirely without it
    davey = None

if TYPE_CHECKING:
    from ..bot import AllAroundBot

log = logging.getLogger(__name__)

YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

# Previous keeps a small trail so /skip and ⏮ stay useful without leaking memory.
MAX_HISTORY = 50

# One /play should not be able to enqueue an endless playlist.
MAX_PLAYLIST_TRACKS = 100

# Queue/timing snapshot persisted next to bot.db. The checkpoint interval bounds
# how much playback position is lost if the host process is killed abruptly.
STATE_VERSION = 1
STATE_FILE = "voice_sessions.json"
CHECKPOINT_INTERVAL = 30.0

# A dropped voice connection is usually transient (network handover, voice server
# migration). Retry before giving up and leaving the session parked.
RECONNECT_DELAYS = (2.0, 5.0, 10.0, 20.0)

PLAYLIST_OPTIONS = {
    **YTDLP_OPTIONS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "ignoreerrors": True,
    "playlistend": MAX_PLAYLIST_TRACKS,
}

# discord.py can't find Homebrew's libopus via ctypes.find_library on macOS.
OPUS_CANDIDATES = (
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
    "libopus.so.0",
    "libopus.so",
    "opus",
)


def ensure_opus() -> bool:
    """Load libopus if discord.py could not find it on its own."""
    if discord.opus.is_loaded():
        return True

    for candidate in OPUS_CANDIDATES:
        try:
            discord.opus.load_opus(candidate)
        except Exception:
            continue
        if discord.opus.is_loaded():
            log.info("Loaded libopus from %s", candidate)
            return True
    return False


def _davey_version() -> str:
    try:
        return version("davey")
    except PackageNotFoundError:
        return "missing"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "live"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_position(seconds: float) -> str:
    """Timestamp inside a track (0:00 is valid, unlike format_duration)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def tone_pcm(seconds: float = 3.0, frequency: float = 440.0, volume: float = 0.3) -> io.BytesIO:
    """Build a raw 16-bit 48kHz stereo sine wave for testing the voice path."""
    rate = 48000
    step = 2 * math.pi * frequency / rate
    frames = bytearray()
    for index in range(int(rate * seconds)):
        sample = int(volume * 32767 * math.sin(step * index))
        frames += struct.pack("<h", sample) * 2
    return io.BytesIO(bytes(frames))


def extract(query: str) -> dict[str, Any]:
    """Resolve a URL or search term to a direct audio stream (blocking)."""
    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
        info = ydl.extract_info(target, download=False)

    if info is None:
        raise RuntimeError("no result")

    if "entries" in info:
        entries = [entry for entry in info["entries"] or [] if entry]
        if not entries:
            raise RuntimeError("no result")
        info = entries[0]

    return {
        "url": info["url"],
        "title": info.get("title") or "Unknown title",
        "page_url": info.get("webpage_url") or query,
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
    }


def is_playlist_url(query: str) -> bool:
    """True for playlist links, but not for a watch link that happens to be in one."""
    if not query.startswith(("http://", "https://")):
        return False

    parsed = urlparse(query)
    if parsed.path.rstrip("/").endswith("/playlist"):
        return True
    return "list" in parse_qs(parsed.query) and "v" not in parse_qs(parsed.query)


def _entry_page_url(entry: dict[str, Any]) -> str | None:
    page = entry.get("webpage_url") or entry.get("url")
    if page and page.startswith(("http://", "https://")):
        return page
    # extract_flat sometimes reports only the video ID.
    video_id = entry.get("id")
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else page


def extract_playlist(url: str) -> list[dict[str, Any]]:
    """List a playlist without resolving every stream (blocking).

    Entries are resolved lazily when they start playing, so enqueueing a long
    playlist does not take minutes and stream URLs stay fresh.
    """
    with yt_dlp.YoutubeDL(PLAYLIST_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("no result")

    entries = [entry for entry in info.get("entries") or [] if entry]
    if not entries:
        raise RuntimeError("playlist is empty")

    tracks = []
    for entry in entries:
        page_url = _entry_page_url(entry)
        if page_url is None:
            continue
        tracks.append(
            {
                "title": entry.get("title") or "Unknown title",
                "page_url": page_url,
                "duration": entry.get("duration"),
            }
        )

    if not tracks:
        raise RuntimeError("playlist is empty")
    return tracks


@dataclass
class Track:
    title: str
    page_url: str
    stream_url: str | None  # None until a playlist entry is resolved at play time
    duration: float | None
    requester_id: int
    requester_name: str


def _track_payload(track: Track) -> dict[str, Any]:
    """Serialize a track without the direct stream URL (it expires)."""
    return {
        "title": track.title,
        "page_url": track.page_url,
        "duration": track.duration,
        "requester_id": track.requester_id,
        "requester_name": track.requester_name,
    }


def _track_from_payload(data: Any) -> Track | None:
    if not isinstance(data, dict):
        return None
    page_url = data.get("page_url")
    if not isinstance(page_url, str) or not page_url:
        return None

    duration = data.get("duration")
    requester_id = data.get("requester_id")
    return Track(
        title=str(data.get("title") or "Unknown title"),
        page_url=page_url,
        stream_url=None,
        duration=float(duration) if isinstance(duration, int | float) else None,
        requester_id=int(requester_id) if isinstance(requester_id, int) else 0,
        requester_name=str(data.get("requester_name") or "unknown"),
    )


class VoiceStateStore:
    """Crash-safe JSON snapshot of the per-guild voice sessions.

    Only one writer exists (the cog's save lock), so a fixed temporary file plus
    ``os.replace`` is enough to make the file swap atomic.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # _save_state() writes from a worker thread, while _save_now() can be
        # called synchronously during shutdown. Serialize both writers.
        self._write_lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("Could not read voice state %s: %s", self.path, exc)
            return None

        try:
            data = json.loads(raw)
        except ValueError as exc:
            log.warning("Ignoring corrupt voice state %s: %s", self.path, exc)
            return None
        if not isinstance(data, dict):
            log.warning("Ignoring voice state %s: expected a JSON object", self.path)
            return None
        return data

    def save(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)


class QueueState:
    """Per-guild playback order. Pure state so it can be tested without Discord."""

    def __init__(self) -> None:
        self.queue: deque[Track] = deque()
        self.history: list[Track] = []
        self.current: Track | None = None
        self.stay_after_end = False

    def enqueue(self, track: Track, *, stay_after_end: bool) -> int:
        self.queue.append(track)
        self.stay_after_end = stay_after_end
        return len(self.queue)

    def start_next(self) -> Track | None:
        if self.current is not None:
            self.history.append(self.current)
            del self.history[:-MAX_HISTORY]
        self.current = self.queue.popleft() if self.queue else None
        return self.current

    def previous(self) -> Track | None:
        if not self.history:
            return None
        if self.current is not None:
            self.queue.appendleft(self.current)
        self.current = self.history.pop()
        return self.current

    def jump(self, index: int) -> Track | None:
        """Play the 1-based position in the upcoming queue, discarding earlier ones."""
        if index < 1 or index > len(self.queue):
            return None
        for _ in range(index - 1):
            self.queue.popleft()
        return self.start_next()

    def clear(self) -> None:
        self.queue.clear()
        self.history.clear()
        self.current = None


def _shorten(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_queue(
    state: QueueState,
    limit: int = 15,
    *,
    stopped: bool = False,
    position: float = 0.0,
) -> str:
    """Render the queue for /queue, keeping the message well under 2000 chars."""
    lines: list[str] = []

    if state.current is not None:
        track = state.current
        label = f"**Stopped at** `{format_position(position)}` —" if stopped else "**Now playing:**"
        lines.append(
            f"{label} {_shorten(track.title)} — `{format_duration(track.duration)}`"
            f" (requested by <@{track.requester_id}>)"
        )

    upcoming = list(state.queue)
    if upcoming:
        lines.append("**Up next:**")
        for index, track in enumerate(upcoming[:limit], start=1):
            lines.append(f"{index}. {_shorten(track.title)} — `{format_duration(track.duration)}`")
        if len(upcoming) > limit:
            lines.append(f"…and {len(upcoming) - limit} more")
    elif state.current is not None:
        lines.append("_Nothing else is queued._")

    if state.stay_after_end:
        lines.append("-# Staying in the channel after the queue ends.")

    return "\n".join(lines)


@dataclass
class Session:
    guild_id: int
    channel_id: int
    # The text channel where /play started the session. Follow-up /play calls
    # elsewhere are rejected so the player stays in one place.
    text_channel_id: int
    view: PlayerView
    state: QueueState = field(default_factory=QueueState)
    generation: int = 0
    message: discord.Message | None = None
    # /stop parks the session instead of discarding it: `stopped` marks it as
    # disconnected, `offset` is the saved timestamp inside `state.current`.
    stopped: bool = False
    offset: float = 0.0
    started_at: float | None = None
    # Persisted across restarts. `resume_on_start` means this session was actively
    # playing when it was saved; `restore_paused` remembers a paused player when
    # the voice client is gone (disconnect/restart).
    resume_on_start: bool = False
    restore_paused: bool = False
    message_id: int | None = None


def _voice_diagnostics(voice_client: discord.VoiceClient) -> list[str]:
    """Human-readable dump of the parts of the voice stack that can cause silence."""
    connection = getattr(voice_client, "_connection", None)
    session = getattr(connection, "dave_session", None)

    lines = [
        f"channel: {voice_client.channel.mention} ({voice_client.channel.id})",
        f"connected: {voice_client.is_connected()}",
        f"playing: {voice_client.is_playing()}  paused: {voice_client.is_paused()}",
        f"ws latency: {voice_client.average_latency * 1000:.0f} ms",
        f"encryption mode: {voice_client.mode}",
        f"libopus: {discord.opus.is_loaded()}",
    ]

    if connection is not None:
        lines += [
            f"davey installed: {has_dave} (version {_davey_version()})",
            f"dave protocol: {getattr(connection, 'dave_protocol_version', 'n/a')}",
            f"dave session: {'yes' if session is not None else 'no'}",
            f"dave ready: {getattr(session, 'ready', 'n/a')}",
            f"can encrypt: {getattr(connection, 'can_encrypt', 'n/a')}",
            f"ssrc: {getattr(connection, 'ssrc', 'n/a')}",
        ]

    guild = voice_client.guild
    state = guild.me.voice if guild.me else None
    if state is not None:
        lines += [
            f"self muted: {state.self_mute}  self deafened: {state.self_deaf}",
            f"server muted: {state.mute}  server deafened: {state.deaf}",
        ]

    return lines


class PlayerView(discord.ui.View):
    """Buttons attached to the now-playing message."""

    def __init__(self, cog: Voice, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

        self.previous_button = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.secondary)
        self.pause_button = discord.ui.Button(emoji="⏸️", style=discord.ButtonStyle.secondary)
        self.skip_button = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary)

        self.previous_button.callback = self._previous
        self.pause_button.callback = self._pause
        self.skip_button.callback = self._skip

        self.add_item(self.previous_button)
        self.add_item(self.pause_button)
        self.add_item(self.skip_button)

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_previous(interaction, self.guild_id)

    async def _pause(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_pause(interaction, self.guild_id)

    async def _skip(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_skip(interaction, self.guild_id)


class Voice(commands.Cog):
    def __init__(self, bot: AllAroundBot) -> None:
        self.bot = bot
        self._sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_error: dict[int, str] = {}
        self._expected_leave: set[int] = set()
        self._store: VoiceStateStore | None = None
        self._save_lock = asyncio.Lock()
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._resumed = False

        if not ensure_opus():
            log.warning("libopus not found - /play will fail until it is installed")

        # davey 0.2.0 changed how DAVE keys transition: process_welcome() no longer
        # installs the key, that now needs prepare_transition()/execute_transition(),
        # which discord.py 2.7.x does not call. The session then never becomes ready,
        # audio is sent unencrypted into an E2EE channel, and Discord drops it.
        if davey is not None and hasattr(davey.DaveSession, "prepare_transition"):
            log.error(
                "davey >= 0.2.0 is incompatible with discord.py 2.7.x: DAVE sessions "
                "never become ready and voice audio is silently dropped. Pin davey<0.2.0."
            )

    # --- persistence -----------------------------------------------------

    async def cog_load(self) -> None:
        path = self.bot.settings.database_path.parent / STATE_FILE
        self._store = VoiceStateStore(path)
        self._load_sessions()
        self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    def cog_unload(self) -> None:
        task = getattr(self, "_checkpoint_task", None)
        if task is not None:
            task.cancel()
        for pending in list(getattr(self, "_tasks", ())):
            pending.cancel()
        self._save_now()

    def flush(self) -> None:
        """Synchronous final save, called by the bot before shutdown."""
        self._save_now()

    def _load_sessions(self) -> None:
        store = getattr(self, "_store", None)
        if store is None:
            return

        data = store.load()
        if data is None:
            return
        if data.get("version") != STATE_VERSION:
            log.warning(
                "Ignoring voice state version %r (expected %s)",
                data.get("version"),
                STATE_VERSION,
            )
            return

        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, list):
            log.warning("Ignoring voice state: 'sessions' is not a list")
            return

        loaded = 0
        resumable = 0
        for raw in raw_sessions:
            if not isinstance(raw, dict):
                continue
            try:
                guild_id = int(raw["guild_id"])
                channel_id = int(raw["channel_id"])
                text_channel_id = int(raw["text_channel_id"])
            except (KeyError, TypeError, ValueError):
                continue

            current = _track_from_payload(raw.get("current"))
            raw_queue = raw.get("queue")
            raw_history = raw.get("history")
            queue = [
                track
                for item in (raw_queue if isinstance(raw_queue, list) else [])
                if (track := _track_from_payload(item)) is not None
            ]
            history = [
                track
                for item in (raw_history if isinstance(raw_history, list) else [])
                if (track := _track_from_payload(item)) is not None
            ]
            if current is None and not queue:
                continue

            session = Session(
                guild_id=guild_id,
                channel_id=channel_id,
                text_channel_id=text_channel_id,
                view=PlayerView(self, guild_id),
            )
            session.state.current = current
            session.state.queue = deque(queue)
            session.state.history = history[-MAX_HISTORY:]
            session.state.stay_after_end = raw.get("stay_after_end") is True
            try:
                session.offset = max(0.0, float(raw.get("offset") or 0.0))
            except (TypeError, ValueError):
                session.offset = 0.0
            session.started_at = None
            # The process just started, so there is no voice connection yet.
            session.stopped = True
            session.resume_on_start = raw.get("resume_on_start") is True
            session.restore_paused = raw.get("paused") is True
            message_id = raw.get("message_id")
            if isinstance(message_id, int):
                session.message_id = message_id

            self._sessions[guild_id] = session
            loaded += 1
            if session.resume_on_start:
                resumable += 1

        log.info(
            "Loaded %d persisted voice session(s); %d will resume",
            loaded,
            resumable,
        )

    def _session_payload(self, session: Session) -> dict[str, Any] | None:
        if session.state.current is None and not session.state.queue:
            return None

        voice_client = self._voice_client(session.guild_id)
        paused = (
            bool(
                voice_client is not None
                and voice_client.is_connected()
                and voice_client.is_paused()
            )
            or session.restore_paused
        )
        message_id = getattr(session.message, "id", None) or session.message_id

        return {
            "guild_id": session.guild_id,
            "channel_id": session.channel_id,
            "text_channel_id": session.text_channel_id,
            "message_id": message_id,
            "stopped": session.stopped,
            "offset": round(max(0.0, self._position(session)), 3),
            "paused": paused,
            "resume_on_start": session.resume_on_start,
            "stay_after_end": session.state.stay_after_end,
            "current": (
                _track_payload(session.state.current) if session.state.current is not None else None
            ),
            "queue": [_track_payload(track) for track in session.state.queue],
            "history": [_track_payload(track) for track in session.state.history],
        }

    def _snapshot(self) -> dict[str, Any]:
        sessions = [
            payload
            for session in self._sessions.values()
            if (payload := self._session_payload(session)) is not None
        ]
        return {"version": STATE_VERSION, "sessions": sessions}

    async def _save_state(self) -> None:
        store = getattr(self, "_store", None)
        if store is None:
            return

        lock = getattr(self, "_save_lock", None)
        if lock is None:
            lock = self._save_lock = asyncio.Lock()
        try:
            async with lock:
                payload = self._snapshot()
                await asyncio.to_thread(store.save, payload)
        except Exception:
            log.warning("Could not save voice state", exc_info=True)

    def _save_now(self) -> None:
        store = getattr(self, "_store", None)
        if store is None:
            return
        try:
            store.save(self._snapshot())
        except Exception:
            log.warning("Could not save voice state", exc_info=True)

    async def _checkpoint_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(CHECKPOINT_INTERVAL)
                if any(session.started_at is not None for session in self._sessions.values()):
                    await self._save_state()
        except asyncio.CancelledError:
            raise

    async def _attach_player_message(self, session: Session) -> None:
        """Reconnect a loaded session to the player message it used before restart."""
        if session.message is not None or session.message_id is None:
            return
        channel = self.bot.get_channel(session.text_channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            session.message = await channel.fetch_message(session.message_id)
        except discord.HTTPException:
            session.message = None

    async def _restore_player_message(self, session: Session, *, note: str | None = None) -> None:
        await self._attach_player_message(session)
        channel = self.bot.get_channel(session.text_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return

        session.view = PlayerView(self, session.guild_id)
        if session.message is not None:
            if note is None:
                await self._update_player_message(session)
            else:
                with contextlib.suppress(discord.HTTPException):
                    await session.message.edit(content=note, view=None)
            return

        try:
            session.message = await channel.send(
                note or self._player_text(session),
                view=None if note is not None else session.view,
            )
        except discord.HTTPException as exc:
            log.warning("Could not restore player message in guild %s: %s", session.guild_id, exc)
            return

        if note is None:
            await self._update_player_message(session)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        tasks = getattr(self, "_tasks", None)
        if tasks is None:
            tasks = self._tasks = set()
        tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("Background voice task failed", exc_info=error)

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        # Gateway shutdowns do not always unload cogs, so flush here as well.
        self._save_now()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if getattr(self, "_resumed", False):
            return
        self._resumed = True
        for session in list(self._sessions.values()):
            if session.resume_on_start and session.stopped:
                try:
                    await self._resume_session(session)
                except Exception:
                    log.exception("Could not resume voice in guild %s", session.guild_id)

    async def _resume_session(self, session: Session) -> None:
        """Rejoin the saved channel and continue from the last checkpoint."""
        if not session.resume_on_start:
            return

        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            log.warning("Cannot resume voice in unknown guild %s", session.guild_id)
            await self._save_state()
            return

        channel = guild.get_channel(session.channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            log.warning(
                "Voice channel %s in guild %s is gone; keeping the queue parked",
                session.channel_id,
                session.guild_id,
            )
            await self._restore_player_message(
                session,
                note="⏹️ The saved voice channel is gone. Run `/play` to resume.",
            )
            await self._save_state()
            return

        paused = session.restore_paused
        async with self._lock(session.guild_id):
            if self._sessions.get(session.guild_id) is not session or not session.resume_on_start:
                return
            try:
                await self._connect(guild, channel)
            except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                log.warning("Could not resume voice in guild %s: %s", session.guild_id, exc)
                session.stopped = True
                session.started_at = None
                await self._restore_player_message(
                    session,
                    note=f"⏹️ Could not rejoin <#{session.channel_id}>: {exc}",
                )
                await self._save_state()
                return

            session.channel_id = channel.id
            session.stopped = False
            current = session.state.current
            if current is not None:
                current.stream_url = None
                if not await self._play_track(session, current, offset=session.offset):
                    await self._skip_locked(session)
            elif session.state.queue:
                await self._skip_locked(session)
            else:
                session.stopped = True
                await self._save_state()
                return

            if self._sessions.get(session.guild_id) is not session:
                return

            if paused:
                voice_client = self._voice_client(session.guild_id)
                if voice_client is not None and voice_client.is_playing():
                    session.offset = self._position(session)
                    session.started_at = None
                    session.restore_paused = True
                    voice_client.pause()

            await self._restore_player_message(session)
            await self._save_state()

    def _should_reconnect(self, session: Session) -> bool:
        if self._sessions.get(session.guild_id) is not session or not session.stopped:
            return False
        # /stop clears resume_on_start; an idle stay-after-end waiter has no
        # tracks to save and may still rejoin.
        return session.resume_on_start or (
            session.state.current is None and not session.state.queue
        )

    async def _reconnect(self, session: Session, *, paused: bool = False) -> None:
        """Retry after an unexpected voice drop, keeping the queue intact."""
        guild_id = session.guild_id
        for attempt, delay in enumerate(RECONNECT_DELAYS, start=1):
            await asyncio.sleep(delay)
            if not self._should_reconnect(session):
                return

            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(session.channel_id) if guild is not None else None
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                log.warning("Cannot reconnect in guild %s: channel is gone", guild_id)
                break

            log.info("Reconnecting to voice in guild %s (attempt %d)", guild_id, attempt)
            async with self._lock(guild_id):
                if not self._should_reconnect(session):
                    return
                try:
                    await self._connect(guild, channel)
                except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                    log.warning(
                        "Voice reconnect attempt %d in guild %s failed: %s",
                        attempt,
                        guild_id,
                        exc,
                    )
                    continue

                session.channel_id = channel.id
                session.stopped = False
                current = session.state.current
                if current is not None:
                    current.stream_url = None
                    if not await self._play_track(session, current, offset=session.offset):
                        await self._skip_locked(session)
                elif session.state.queue:
                    await self._skip_locked(session)
                else:
                    # Reconnected while idle (e.g. a stay-after-end waiter).
                    await self._restore_player_message(session)
                    await self._save_state()
                    return

                if self._sessions.get(guild_id) is not session:
                    return

                if paused:
                    voice_client = self._voice_client(guild_id)
                    if voice_client is not None and voice_client.is_playing():
                        session.offset = self._position(session)
                        session.started_at = None
                        session.restore_paused = True
                        voice_client.pause()

                await self._restore_player_message(session)
                await self._save_state()
                return

        if self._sessions.get(guild_id) is session:
            session.stopped = True
            session.started_at = None
            log.warning("Voice reconnect in guild %s failed; queue is parked", guild_id)
            if session.message is not None:
                with contextlib.suppress(discord.HTTPException):
                    await session.message.edit(
                        content="⏹️ Disconnected. Run `/play` to resume.",
                        view=None,
                    )
            await self._save_state()

    # --- helpers ---------------------------------------------------------

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock

    def _voice_client(self, guild_id: int) -> discord.VoiceClient | None:
        bot = getattr(self, "bot", None)
        if bot is None:
            return None
        guild = bot.get_guild(guild_id)
        return guild.voice_client if guild is not None else None

    def _channel_rejection(
        self, session: Session, voice_channel_id: int | None, text_channel_id: int | None
    ) -> str | None:
        """Every control lives in the session's voice channel and text channel."""
        if voice_channel_id != session.channel_id:
            return f"Join <#{session.channel_id}> to control playback."
        if text_channel_id != session.text_channel_id:
            return f"Controls for this session are handled in <#{session.text_channel_id}>."
        return None

    def _play_rejection(
        self, session: Session | None, voice_channel_id: int | None, text_channel_id: int | None
    ) -> str | None:
        """Return why a playing session must be left alone, else None."""
        if session is None or session.stopped or session.state.current is None:
            return None
        return self._channel_rejection(session, voice_channel_id, text_channel_id)

    def _control_rejection(self, interaction: discord.Interaction, session: Session) -> str | None:
        voice = getattr(interaction.user, "voice", None)
        return self._channel_rejection(
            session,
            voice.channel.id if voice and voice.channel else None,
            interaction.channel_id,
        )

    async def _connect(
        self, guild: discord.Guild, channel: discord.abc.Connectable
    ) -> discord.VoiceClient:
        voice_client = guild.voice_client
        if voice_client is not None and not voice_client.is_connected():
            # A stale client keeps guild.voice_client occupied; clear it first.
            with contextlib.suppress(discord.HTTPException):
                await voice_client.disconnect(force=True)
            voice_client = None

        if voice_client is None:
            # Deafen is receive-only: the bot still transmits normally, it just
            # doesn't download everyone else's audio.
            return await channel.connect(self_deaf=True)
        if voice_client.channel != channel:
            await voice_client.move_to(channel)
        return voice_client

    async def _wait_for_dave(self, voice_client: discord.VoiceClient, timeout: float = 5.0) -> None:
        """Give the MLS handshake a moment; audio sent before it settles is dropped."""
        connection = getattr(voice_client, "_connection", None)
        if connection is None or not getattr(connection, "dave_protocol_version", 0):
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if getattr(connection, "can_encrypt", False):
                log.debug("DAVE session ready")
                return
            await asyncio.sleep(0.1)

        log.warning("DAVE session still not ready after %.0fs", timeout)

    def _log_encryption_state(self, voice_client: discord.VoiceClient) -> None:
        connection = getattr(voice_client, "_connection", None)
        if connection is None:
            return
        session = getattr(connection, "dave_session", None)
        log.info(
            "Voice state: mode=%s dave_proto=%s dave_ready=%s can_encrypt=%s ssrc=%s",
            voice_client.mode,
            getattr(connection, "dave_protocol_version", "?"),
            getattr(session, "ready", "?"),
            getattr(connection, "can_encrypt", "?"),
            getattr(connection, "ssrc", "?"),
        )
        if getattr(connection, "dave_protocol_version", 0) and not getattr(
            connection, "can_encrypt", False
        ):
            log.warning(
                "DAVE is active but the encryption session is not ready; "
                "Discord may drop outgoing audio"
            )

    def _position(self, session: Session) -> float:
        if session.started_at is None:
            return session.offset
        return session.offset + (time.monotonic() - session.started_at)

    def _stopped_text(self, session: Session) -> str:
        track = session.state.current
        if track is None:
            return "⏹️ Stopped. Run `/play` to start something."
        return (
            f"⏹️ Stopped at `{format_position(session.offset)}` — **{track.title}**\n"
            "-# Run `/play` to continue, or `/clearqueue` to discard the queue."
        )

    def _player_text(self, session: Session) -> str:
        track = session.state.current
        if track is None:
            voice_client = self._voice_client(session.guild_id)
            if voice_client is not None and voice_client.is_connected():
                return "🎧 Waiting for tracks — run `/play` with a link or search."
            return "⏹️ Queue finished."

        voice_client = self._voice_client(session.guild_id)
        icon = "⏸️" if voice_client is not None and voice_client.is_paused() else "▶️"

        meta = f"Requested by <@{track.requester_id}>"
        if session.state.queue:
            meta += f" • {len(session.state.queue)} in queue"
        if session.state.stay_after_end:
            meta += " • staying after queue"

        return f"{icon} **{track.title}** — `{format_duration(track.duration)}`\n-# {meta}"

    async def _update_player_message(self, session: Session) -> None:
        if session.message is None:
            return

        voice_client = self._voice_client(session.guild_id)
        paused = voice_client is not None and voice_client.is_paused()
        playing = session.state.current is not None

        session.view.pause_button.emoji = "▶️" if paused else "⏸️"
        session.view.pause_button.disabled = not playing
        session.view.skip_button.disabled = not playing
        session.view.previous_button.disabled = not session.state.history

        with contextlib.suppress(discord.HTTPException):
            await session.message.edit(content=self._player_text(session), view=session.view)

    async def _publish_player_message(
        self, session: Session, interaction: discord.Interaction
    ) -> None:
        """Keep one player message, moving it when a stopped session resumes elsewhere."""
        content = self._player_text(session)

        if session.message is not None and interaction.channel_id == session.text_channel_id:
            with contextlib.suppress(discord.HTTPException):
                await session.message.edit(content=content, view=session.view)
            return

        if session.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await session.message.edit(
                    content=f"⏹️ Moved to <#{interaction.channel_id}>.", view=None
                )

        session.text_channel_id = interaction.channel_id
        session.view = PlayerView(self, session.guild_id)
        session.message = await interaction.followup.send(content, view=session.view, wait=True)

    async def _stop_locked(self, session: Session) -> None:
        """Park the session: save the position, disconnect, keep the queue."""
        session.offset = self._position(session)
        session.started_at = None
        session.stopped = True
        session.resume_on_start = False
        session.restore_paused = False
        session.generation += 1

        voice_client = self._voice_client(session.guild_id)
        if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()

        if session.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await session.message.edit(content=self._stopped_text(session), view=None)

        if voice_client is not None and voice_client.is_connected():
            self._expected_leave.add(session.guild_id)
            try:
                with contextlib.suppress(discord.HTTPException):
                    await voice_client.disconnect(force=False)
            finally:
                self._expected_leave.discard(session.guild_id)

        await self._save_state()

    async def _teardown(self, session: Session, note: str) -> None:
        session.generation += 1
        session.resume_on_start = False
        if self._sessions.get(session.guild_id) is session:
            self._sessions.pop(session.guild_id, None)

        if session.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await session.message.edit(content=f"⏹️ {note}", view=None)

        voice_client = self._voice_client(session.guild_id)
        if voice_client is not None and voice_client.is_connected():
            self._expected_leave.add(session.guild_id)
            try:
                with contextlib.suppress(discord.HTTPException):
                    await voice_client.disconnect(force=False)
            finally:
                self._expected_leave.discard(session.guild_id)

        await self._save_state()

    # --- playback --------------------------------------------------------

    async def _play_track(self, session: Session, track: Track, *, offset: float = 0.0) -> bool:
        """Start a track. Returns False if it could not be resolved and was skipped."""
        voice_client = self._voice_client(session.guild_id)
        if voice_client is None or not voice_client.is_connected():
            await self._teardown(session, "Voice connection lost.")
            return True

        if track.stream_url is None:
            # Playlist entries are flat at enqueue time; resolve just in time so
            # the direct URL is fresh and playlists enqueue instantly.
            try:
                info = await asyncio.to_thread(extract, track.page_url)
            except Exception as exc:
                log.warning("Skipping unplayable track %r: %s", track.title, exc)
                self._last_error[session.guild_id] = f"{track.title}: {exc}"
                session.state.current = None
                session.offset = 0.0
                session.started_at = None
                return False
            track.stream_url = info["url"]
            track.duration = info["duration"] or track.duration

        await self._wait_for_dave(voice_client)
        self._log_encryption_state(voice_client)

        session.state.current = track
        session.offset = max(0.0, offset)
        session.resume_on_start = True
        session.restore_paused = False
        session.generation += 1
        generation = session.generation
        self._last_error.pop(session.guild_id, None)

        stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed by the after callback

        def after(error: Exception | None) -> None:
            try:
                stderr.seek(0)
                tail = stderr.read().decode("utf-8", "replace").strip()
                if tail:
                    log.warning("FFmpeg stderr (guild %s): %s", session.guild_id, tail[-1500:])
            except Exception:
                log.debug("Could not read FFmpeg stderr", exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    stderr.close()

            if error is not None:
                self._last_error[session.guild_id] = str(error)
                log.warning("Playback error in guild %s: %s", session.guild_id, error)

            loop = self.bot.loop
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._handle_track_end(session.guild_id, generation), loop
                )

        before_options = FFMPEG_BEFORE_OPTIONS
        if session.offset > 0.5:
            # -ss before -i seeks on the input (resume after /stop).
            before_options = f"-ss {session.offset:.3f} {before_options}"

        source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=before_options,
            options=FFMPEG_OPTIONS,
            stderr=stderr,
        )

        log.info(
            "Playing in guild %s channel %s: %s",
            session.guild_id,
            session.channel_id,
            track.title,
        )

        # Auto-resume can run against a voice client that is already playing
        # (e.g. after an extension reload); replace that source instead of
        # hitting "Already playing" and tearing down the queue.
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        try:
            voice_client.play(source, after=after)
        except discord.ClientException as exc:
            with contextlib.suppress(Exception):
                stderr.close()
            await self._teardown(session, f"Playback failed: {exc}")
            return True

        session.started_at = time.monotonic()
        await self._update_player_message(session)
        await self._save_state()
        return True

    async def _handle_track_end(self, guild_id: int, generation: int) -> None:
        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None or session.generation != generation:
                return
            await self._skip_locked(session)

    # --- button handlers -------------------------------------------------

    async def _control_session(
        self, interaction: discord.Interaction, guild_id: int
    ) -> Session | None:
        session = self._sessions.get(guild_id)
        if session is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return None
        if session.stopped:
            await interaction.response.send_message(
                "Playback is stopped. Use `/play` to continue or `/clearqueue` to discard.",
                ephemeral=True,
            )
            return None
        rejection = self._control_rejection(interaction, session)
        if rejection is not None:
            await interaction.response.send_message(rejection, ephemeral=True)
            return None
        return session

    async def _join_or_resume(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        channel: discord.abc.Connectable,
    ) -> None:
        """Handle /play without a query: resume the parked session or wait silently."""
        guild_id = guild.id

        session = self._sessions.get(guild_id)
        if session is not None and not session.stopped:
            rejection = self._play_rejection(session, channel.id, interaction.channel_id)
            if rejection is not None:
                await interaction.response.send_message(rejection, ephemeral=True)
                return

        # The deferred reply must be ephemeral to match the ephemeral status we
        # edit it into; a public "thinking" placeholder with an ephemeral first
        # follow-up is what leaves the interaction stuck on "thinking".
        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)

            if session is not None and session.stopped:
                try:
                    await self._connect(guild, channel)
                except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                    log.warning("Voice connect failed: %s", exc)
                    await interaction.edit_original_response(
                        content=f"Couldn't join {channel.mention}: {exc}"
                    )
                    return

                session.channel_id = channel.id
                session.stopped = False
                await self._attach_player_message(session)

                current = session.state.current
                if current is not None:
                    current.stream_url = None
                    if not await self._play_track(session, current, offset=session.offset):
                        await self._skip_locked(session)
                    if self._sessions.get(guild_id) is session:
                        await interaction.edit_original_response(
                            content=f"Resumed in <#{session.channel_id}>."
                        )
                        await self._publish_player_message(session, interaction)
                        await self._save_state()
                    else:
                        await interaction.edit_original_response(
                            content="Couldn't resume that track. Check the logs for details."
                        )
                    return

                session.state.stay_after_end = True
                await interaction.edit_original_response(
                    content=f"Waiting for tracks in <#{channel.id}>."
                )
                await self._publish_player_message(session, interaction)
                await self._save_state()
                return

            if session is not None:
                voice_client = guild.voice_client
                connected = voice_client is not None and voice_client.is_connected()

                if connected:
                    rejection = self._play_rejection(session, channel.id, interaction.channel_id)
                    if rejection is not None:
                        await interaction.edit_original_response(content=rejection)
                        return
                    if session.state.current is not None:
                        await interaction.edit_original_response(
                            content=(
                                f"Already playing in <#{session.channel_id}>. "
                                "Run `/play <link or search>` to add a track."
                            )
                        )
                        return

                if not connected:
                    try:
                        await self._connect(guild, channel)
                    except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                        log.warning("Voice connect failed: %s", exc)
                        await interaction.edit_original_response(
                            content=f"Couldn't join {channel.mention}: {exc}"
                        )
                        return
                    session.channel_id = channel.id
                elif session.channel_id != channel.id:
                    with contextlib.suppress(discord.HTTPException):
                        await voice_client.move_to(channel)
                        session.channel_id = channel.id

                session.state.stay_after_end = True
                await interaction.edit_original_response(
                    content=f"Waiting for tracks in <#{channel.id}>."
                )
                await self._publish_player_message(session, interaction)
                await self._save_state()
                return

            try:
                await self._connect(guild, channel)
            except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                log.warning("Voice connect failed: %s", exc)
                await interaction.edit_original_response(
                    content=f"Couldn't join {channel.mention}: {exc}"
                )
                return

            session = Session(
                guild_id=guild_id,
                channel_id=channel.id,
                text_channel_id=interaction.channel_id,
                view=PlayerView(self, guild_id),
            )
            session.state.stay_after_end = True
            self._sessions[guild_id] = session
            await interaction.edit_original_response(
                content=f"Waiting for tracks in <#{channel.id}>."
            )
            await self._publish_player_message(session, interaction)
            await self._save_state()

    async def _skip_locked(self, session: Session) -> None:
        """Advance to the next track. The caller holds the guild lock."""
        voice_client = self._voice_client(session.guild_id)
        session.generation += 1
        if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()

        # Unplayable playlist entries are skipped; keep going until something starts.
        while True:
            next_track = session.state.start_next()
            if next_track is None:
                break
            if await self._play_track(session, next_track):
                return

        session.offset = 0.0
        session.started_at = None
        session.resume_on_start = False
        if session.state.stay_after_end:
            await self._update_player_message(session)
            await self._save_state()
        else:
            await self._teardown(session, "Queue finished.")

    async def handle_pause(self, interaction: discord.Interaction, guild_id: int) -> None:
        session = await self._control_session(interaction, guild_id)
        if session is None:
            return

        voice_client = self._voice_client(guild_id)
        if voice_client is None or not voice_client.is_connected():
            await interaction.response.send_message("I'm not connected to voice.", ephemeral=True)
            return

        if voice_client.is_playing():
            session.offset = self._position(session)
            session.started_at = None
            session.restore_paused = True
            voice_client.pause()
        elif voice_client.is_paused():
            session.started_at = time.monotonic()
            session.restore_paused = False
            voice_client.resume()

        await interaction.response.defer()
        await self._update_player_message(session)
        await self._save_state()

    async def handle_skip(self, interaction: discord.Interaction, guild_id: int) -> None:
        session = await self._control_session(interaction, guild_id)
        if session is None:
            return
        await interaction.response.defer()

        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None:
                return
            await self._skip_locked(session)

    async def handle_previous(self, interaction: discord.Interaction, guild_id: int) -> None:
        session = await self._control_session(interaction, guild_id)
        if session is None:
            return
        await interaction.response.defer()

        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None or not session.state.history:
                await interaction.followup.send("There's nothing to go back to.", ephemeral=True)
                return

            voice_client = self._voice_client(guild_id)
            session.generation += 1
            if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
                voice_client.stop()

            previous = session.state.previous()
            if previous is not None and not await self._play_track(session, previous):
                # Extremely rare: the previous entry cannot be resolved anymore.
                await self._skip_locked(session)

    # --- commands --------------------------------------------------------

    @app_commands.command(
        name="play",
        description="Play, queue, or resume YouTube audio in your voice channel.",
    )
    @app_commands.describe(
        query="YouTube URL, playlist link, or search terms (omit to resume or wait)",
        stay_after_end="Stay in the voice channel after the queue ends",
    )
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        stay_after_end: bool = False,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        state = getattr(interaction.user, "voice", None)
        channel = state.channel if state else None
        if channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then run `/play`.", ephemeral=True
            )
            return

        # Bare /play resumes a /stop-parked session, or joins and waits silently
        # when there is nothing to continue.
        if query is None or not query.strip():
            await self._join_or_resume(interaction, guild, channel)
            return

        # Reject early (before the slow yt-dlp lookup) when this session belongs
        # to another voice channel or another text channel.
        voice_client = guild.voice_client
        session = self._sessions.get(guild.id)
        rejection = None
        if voice_client is not None and voice_client.is_connected():
            rejection = self._play_rejection(session, channel.id, interaction.channel_id)
        if rejection is not None:
            await interaction.response.send_message(rejection, ephemeral=True)
            return

        # Ephemeral defer: the first response below is ephemeral too, and
        # Discord cannot turn a public "thinking" placeholder into one.
        await interaction.response.defer(thinking=True, ephemeral=True)

        playlist = is_playlist_url(query)
        try:
            if playlist:
                entries = await asyncio.to_thread(extract_playlist, query)
            else:
                entries = [await asyncio.to_thread(extract, query)]
        except Exception as exc:
            log.warning("yt-dlp failed for %r: %s", query, exc)
            await interaction.edit_original_response(
                content="Couldn't load that video. Check the link, or try another one."
            )
            return

        if not ensure_opus():
            await interaction.edit_original_response(
                content="Audio isn't available on this host (libopus missing)."
            )
            return

        requester_id = interaction.user.id
        requester_name = getattr(interaction.user, "display_name", interaction.user.name)
        tracks = [
            Track(
                title=entry["title"],
                page_url=entry["page_url"],
                stream_url=entry.get("url"),  # playlist entries resolve lazily
                duration=entry.get("duration"),
                requester_id=requester_id,
                requester_name=requester_name,
            )
            for entry in entries
        ]

        guild_id = guild.id
        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)

            if session is not None and session.stopped:
                # /stop parked this session: rejoin the caller's channel and
                # continue the saved track from its timestamp.
                try:
                    await self._connect(guild, channel)
                except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                    log.warning("Voice connect failed: %s", exc)
                    await interaction.edit_original_response(
                        content=f"Couldn't join {channel.mention}: {exc}"
                    )
                    return

                session.channel_id = channel.id
                session.stopped = False
                await self._attach_player_message(session)

                current = session.state.current
                if current is not None:
                    for track in tracks:
                        session.state.enqueue(track, stay_after_end=stay_after_end)
                    current.stream_url = None  # the saved direct URL is probably stale
                    if not await self._play_track(session, current, offset=session.offset):
                        await self._skip_locked(session)
                    if self._sessions.get(guild_id) is session:
                        await interaction.edit_original_response(
                            content=f"Resumed in <#{session.channel_id}>."
                        )
                        await self._publish_player_message(session, interaction)
                        await self._save_state()
                    else:
                        await interaction.edit_original_response(
                            content="Couldn't resume that track. Check the logs for details."
                        )
                    return
            else:
                voice_client = guild.voice_client
                connected = voice_client is not None and voice_client.is_connected()

                # Re-check: another /play may have started a session while we
                # were resolving the query.
                if session is not None and connected:
                    rejection = self._play_rejection(session, channel.id, interaction.channel_id)
                    if rejection is not None:
                        await interaction.edit_original_response(content=rejection)
                        return

                if session is None or not connected:
                    try:
                        voice_client = await self._connect(guild, channel)
                    except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                        log.warning("Voice connect failed: %s", exc)
                        await interaction.edit_original_response(
                            content=f"Couldn't join {channel.mention}: {exc}"
                        )
                        return

                    session = Session(
                        guild_id=guild_id,
                        channel_id=channel.id,
                        text_channel_id=interaction.channel_id,
                        view=PlayerView(self, guild_id),
                    )
                    self._sessions[guild_id] = session
                elif session.state.current is None and not session.state.queue:
                    # Idle (e.g. kept alive by stay_after_end): follow the caller.
                    if session.channel_id != channel.id and voice_client is not None:
                        with contextlib.suppress(discord.HTTPException):
                            await voice_client.move_to(channel)
                            session.channel_id = channel.id

            if session.state.current is None:
                for track in tracks:
                    session.state.enqueue(track, stay_after_end=stay_after_end)

                started = False
                while True:
                    next_track = session.state.start_next()
                    if next_track is None:
                        break
                    if await self._play_track(session, next_track):
                        started = True
                        break

                if guild_id not in self._sessions:
                    # _play_track tore the session down (connect/play failure).
                    await interaction.edit_original_response(
                        content="Couldn't start playback. Check the logs for details."
                    )
                    return

                if not started:
                    await interaction.edit_original_response(
                        content="Couldn't load that video. Check the link, or try another one."
                    )
                    if not session.state.stay_after_end:
                        await self._teardown(session, "Nothing could be played.")
                    else:
                        await self._publish_player_message(session, interaction)
                    return

                await interaction.edit_original_response(content=f"Playing in <#{channel.id}>.")
                await self._publish_player_message(session, interaction)
                await self._save_state()
                return

            first_position = len(session.state.queue) + 1
            for track in tracks:
                session.state.enqueue(track, stay_after_end=stay_after_end)
            last_position = first_position + len(tracks) - 1

            if len(tracks) == 1:
                note = f"Added **{tracks[0].title}** to the queue (position {first_position})."
            else:
                note = (
                    f"Added **{len(tracks)}** tracks from the playlist "
                    f"(positions {first_position}-{last_position})."
                )
            if session.channel_id != channel.id:
                note += f"\nI'm playing in <#{session.channel_id}>; join it to hear the queue."
            await interaction.edit_original_response(content=note)
            await self._update_player_message(session)
            await self._save_state()

    @app_commands.command(
        name="skip",
        description="Skip the current video and play the next one.",
    )
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = await self._control_session(interaction, interaction.guild.id)
        if session is None:
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self._lock(interaction.guild.id):
            session = self._sessions.get(interaction.guild.id)
            if session is None:
                await interaction.edit_original_response(content="Nothing is playing right now.")
                return
            await self._skip_locked(session)

        if self._sessions.get(interaction.guild.id) is None:
            await interaction.edit_original_response(content="Skipped; the queue ended.")
        else:
            await interaction.edit_original_response(content="Skipped.")

    @app_commands.command(
        name="queue",
        description="Show the current track and what is coming up next.",
    )
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self._sessions.get(interaction.guild.id)

        if session is None or (session.state.current is None and not session.state.queue):
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        await interaction.response.send_message(
            describe_queue(
                session.state,
                stopped=session.stopped,
                position=session.offset,
            )
        )

    @app_commands.command(
        name="clearqueue",
        description="Remove every upcoming track, keeping the current one playing.",
    )
    @app_commands.guild_only()
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id

        # A /stop-parked session has no voice connection to check against.
        # Clearing it discards the saved track, position, and queue entirely.
        session = self._sessions.get(guild_id)
        if session is not None and session.stopped:
            await interaction.response.defer(thinking=True, ephemeral=True)
            async with self._lock(guild_id):
                session = self._sessions.get(guild_id)
                if session is None or not session.stopped:
                    await interaction.edit_original_response(
                        content="Nothing is playing right now."
                    )
                    return
                session.state.clear()
                session.offset = 0.0
                session.started_at = None
                self._sessions.pop(guild_id, None)
                if session.message is not None:
                    with contextlib.suppress(discord.HTTPException):
                        await session.message.edit(content="⏹️ Queue cleared.", view=None)
                await self._save_state()
            await interaction.edit_original_response(content="Discarded the saved queue.")
            return

        session = await self._control_session(interaction, guild_id)
        if session is None:
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None:
                await interaction.edit_original_response(content="Nothing is playing right now.")
                return
            removed = len(session.state.queue)
            session.state.queue.clear()
            await self._update_player_message(session)
            await self._save_state()

        if removed == 0:
            await interaction.edit_original_response(content="The queue is already empty.")
        else:
            await interaction.edit_original_response(
                content=f"Cleared **{removed}** track(s) from the queue."
            )

    @app_commands.command(
        name="jump",
        description="Jump to a track in the upcoming queue.",
    )
    @app_commands.describe(index="Queue position to jump to (1 = the next track)")
    @app_commands.guild_only()
    async def jump(self, interaction: discord.Interaction, index: int) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id

        session = await self._control_session(interaction, guild_id)
        if session is None:
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None:
                await interaction.edit_original_response(content="Nothing is playing right now.")
                return

            queued = len(session.state.queue)
            if queued == 0:
                await interaction.edit_original_response(content="The queue is empty.")
                return
            if index < 1 or index > queued:
                await interaction.edit_original_response(
                    content=f"Pick a position between 1 and {queued}."
                )
                return

            voice_client = self._voice_client(guild_id)
            session.generation += 1
            if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
                voice_client.stop()

            target = session.state.jump(index)
            if target is not None and not await self._play_track(session, target):
                await self._skip_locked(session)

        session = self._sessions.get(guild_id)
        playing = session.state.current if session is not None else None
        if playing is None:
            await interaction.edit_original_response(content="Couldn't play that track.")
        else:
            await interaction.edit_original_response(content=f"Jumped to **{playing.title}**.")

    @app_commands.command(
        name="stop",
        description="Stop playback and leave, keeping the queue and position saved.",
    )
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id

        session = self._sessions.get(guild_id)
        state = getattr(interaction.user, "voice", None)
        channel = state.channel if state else None

        if session is not None and session.stopped:
            if session.resume_on_start:
                # After a crash/voice drop the session is parked but still marked
                # for auto-resume; /stop is the way to cancel that safely.
                session.resume_on_start = False
                await self._save_state()
                await interaction.response.send_message(
                    "Auto-resume cancelled. The queue and position are still saved; "
                    "use `/play` to continue or `/clearqueue` to discard.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Already stopped. Use `/play` to continue or `/clearqueue` to discard.",
                ephemeral=True,
            )
            return

        # /stop follows the same channel rules as /play.
        if session is not None:
            rejection = self._channel_rejection(
                session, channel.id if channel else None, interaction.channel_id
            )
            if rejection is not None:
                await interaction.response.send_message(rejection, ephemeral=True)
                return

        if session is None:
            voice_client = self._voice_client(guild_id)
            if voice_client is None:
                await interaction.response.send_message(
                    "I'm not in a voice channel.", ephemeral=True
                )
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            self._expected_leave.add(guild_id)
            try:
                with contextlib.suppress(discord.HTTPException):
                    await voice_client.disconnect(force=False)
            finally:
                self._expected_leave.discard(guild_id)
            await interaction.edit_original_response(content="Stopped and left the voice channel.")
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        async with self._lock(guild_id):
            session = self._sessions.get(guild_id)
            if session is None:
                await interaction.edit_original_response(content="Nothing is playing right now.")
                return
            if session.stopped:
                await interaction.edit_original_response(
                    content="Already stopped. Use `/play` to continue or `/clearqueue` to discard."
                )
                return
            await self._stop_locked(session)
        await interaction.edit_original_response(
            content="Stopped. The queue and position are saved; run `/play` to continue."
        )

    @app_commands.command(
        name="voice",
        description="Show voice connection diagnostics and optionally play a test tone.",
    )
    @app_commands.describe(test_tone="Play a 3 second 440 Hz tone instead of diagnostics only")
    @app_commands.guild_only()
    async def voice(self, interaction: discord.Interaction, test_tone: bool = False) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        voice_client = guild.voice_client

        if test_tone:
            state = getattr(interaction.user, "voice", None)
            channel = state.channel if state else None
            if channel is None:
                await interaction.followup.send(
                    "Join a voice channel first, then run `/voice test_tone:true`.",
                    ephemeral=True,
                )
                return
            if not ensure_opus():
                await interaction.followup.send("libopus is missing on this host.", ephemeral=True)
                return
            session = self._sessions.get(guild.id)
            if session is not None and not session.stopped:
                await interaction.followup.send(
                    "Stop playback first with `/stop`, then run the tone test.", ephemeral=True
                )
                return
            try:
                voice_client = await self._connect(guild, channel)
            except (TimeoutError, discord.ClientException, discord.Forbidden) as exc:
                await interaction.followup.send(f"Couldn't join: {exc}", ephemeral=True)
                return

        if voice_client is None or not voice_client.is_connected():
            await interaction.followup.send(
                "I'm not connected to voice. Run `/play` or `/voice test_tone:true`.",
                ephemeral=True,
            )
            return

        if test_tone:
            await self._wait_for_dave(voice_client)
            self._log_encryption_state(voice_client)
            voice_client.play(discord.PCMAudio(tone_pcm()))
            await interaction.followup.send(
                "🔊 Playing a 3 second test tone. If you can't hear it, the problem is "
                "the voice transport, not YouTube.",
                ephemeral=True,
            )
            return

        lines = _voice_diagnostics(voice_client)
        session = self._sessions.get(guild.id)
        if session is not None:
            lines += [
                f"queue: {len(session.state.queue)}",
                f"history: {len(session.state.history)}",
                f"stay after end: {session.state.stay_after_end}",
            ]
        if guild.id in self._last_error:
            lines.append(f"last playback error: {self._last_error[guild.id]}")

        await interaction.followup.send(
            "**Voice diagnostics**\n```\n" + "\n".join(lines) + "\n```", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # Being kicked/disconnected shows up as leaving the channel.
        if self.bot.user is None or member.id != self.bot.user.id:
            return

        guild_id = member.guild.id
        if before.channel is not None and after.channel is None:
            session = self._sessions.get(guild_id)
            if session is not None and session.stopped:
                # /stop disconnects on purpose; keep the saved queue alive.
                log.info("Parked voice session in guild %s", guild_id)
                return

            if guild_id in self._expected_leave:
                # _teardown/_stop already updated the session; never discard a
                # queue here just because the leave was intentional.
                log.info("Left voice in guild %s", guild_id)
                return

            if session is None:
                log.info("Voice connection lost in guild %s (no session)", guild_id)
                return

            # An unexpected drop must not throw away the queue: park it, save the
            # position, and try to rejoin before falling back to manual /play.
            voice_client = self._voice_client(guild_id)
            paused = session.restore_paused or bool(
                voice_client is not None and voice_client.is_paused()
            )
            session.offset = self._position(session)
            session.started_at = None
            session.stopped = True
            session.restore_paused = paused
            session.generation += 1
            log.warning(
                "Voice connection lost in guild %s; keeping %d queued track(s)",
                guild_id,
                len(session.state.queue),
            )
            if session.message is not None:
                with contextlib.suppress(discord.HTTPException):
                    await session.message.edit(content="⏹️ Disconnected — reconnecting…", view=None)
            await self._save_state()
            self._spawn(self._reconnect(session, paused=paused))
        elif after.channel is not None and before.channel != after.channel:
            session = self._sessions.get(guild_id)
            if session is not None:
                session.channel_id = after.channel.id
                log.info("Moved to voice channel %s in guild %s", after.channel.id, guild_id)
                await self._save_state()


async def setup(bot: AllAroundBot) -> None:
    await bot.add_cog(Voice(bot))
