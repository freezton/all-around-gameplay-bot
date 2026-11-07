from __future__ import annotations

import time
import types
from collections import deque

import pytest

from all_around_bot.cogs.voice import (
    MAX_HISTORY,
    PlayerView,
    QueueState,
    Session,
    Track,
    Voice,
    VoiceStateStore,
    describe_queue,
    format_duration,
    is_playlist_url,
    tone_pcm,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "live"),
        (0, "live"),
        (5, "0:05"),
        (65, "1:05"),
        (213, "3:33"),
        (3725, "1:02:05"),
    ],
)
def test_format_duration(seconds, expected) -> None:
    assert format_duration(seconds) == expected


def test_tone_pcm_produces_audible_stereo() -> None:
    import array

    data = tone_pcm(seconds=0.1).read()
    assert len(data) == 4800 * 4  # 48kHz * 0.1s * 2 channels * 2 bytes

    samples = array.array("h")
    samples.frombytes(data)
    assert max(abs(sample) for sample in samples) > 1000


def _track(title: str) -> Track:
    return Track(
        title=title,
        page_url=f"https://youtu.be/{title}",
        stream_url=f"https://example.test/{title}.webm",
        duration=60,
        requester_id=1,
        requester_name="tester",
    )


def test_queue_plays_in_order() -> None:
    state = QueueState()
    assert state.enqueue(_track("a"), stay_after_end=False) == 1
    assert state.enqueue(_track("b"), stay_after_end=False) == 2

    assert state.start_next().title == "a"
    assert state.start_next().title == "b"
    assert state.current.title == "b"
    assert state.start_next() is None
    assert [track.title for track in state.history] == ["a", "b"]


def test_queue_empty_start_next_returns_none() -> None:
    state = QueueState()
    assert state.start_next() is None
    assert state.history == []


def test_previous_swaps_current_back_into_queue() -> None:
    state = QueueState()
    state.enqueue(_track("a"), stay_after_end=False)
    state.enqueue(_track("b"), stay_after_end=False)
    state.start_next()
    state.start_next()

    assert state.previous().title == "a"
    assert state.current.title == "a"
    # b was playing when we went back, so it must be next in line.
    assert [track.title for track in state.queue] == ["b"]
    assert state.start_next().title == "b"


def test_previous_without_history_returns_none() -> None:
    state = QueueState()
    state.start_next()
    assert state.previous() is None
    assert state.current is None


def test_enqueue_updates_stay_after_end_last_wins() -> None:
    state = QueueState()
    state.enqueue(_track("a"), stay_after_end=False)
    state.enqueue(_track("b"), stay_after_end=True)
    assert state.stay_after_end is True

    state.enqueue(_track("c"), stay_after_end=False)
    assert state.stay_after_end is False


def test_clear_drops_queue_history_and_current() -> None:
    state = QueueState()
    state.enqueue(_track("a"), stay_after_end=False)
    state.start_next()
    state.enqueue(_track("b"), stay_after_end=False)

    state.clear()

    assert state.current is None
    assert state.queue == deque()
    assert state.history == []


def test_history_is_bounded() -> None:
    state = QueueState()
    for index in range(MAX_HISTORY + 5):
        state.enqueue(_track(f"track-{index}"), stay_after_end=False)
        state.start_next()

    assert len(state.history) == MAX_HISTORY
    assert state.history[-1].title == f"track-{MAX_HISTORY + 3}"


def test_player_view_has_queue_controls() -> None:
    view = PlayerView(object(), guild_id=1)  # type: ignore[arg-type]

    assert [str(button.emoji) for button in view.children] == ["⏮️", "⏸️", "⏭️"]
    assert all(button.callback is not None for button in view.children)


def _fake_voice_client() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        is_playing=lambda: False,
        is_paused=lambda: False,
        stop=lambda: None,
    )


def _bare_cog(monkeypatch: pytest.MonkeyPatch) -> tuple[Voice, list[str], list[str]]:
    """A Voice cog with the Discord-dependent parts recorded instead of executed."""
    cog = Voice.__new__(Voice)  # skip __init__ (loads libopus)
    cog._sessions = {}
    cog._locks = {}
    cog._last_error = {}
    cog._expected_leave = set()

    played: list[str] = []
    finished: list[str] = []

    async def fake_play(session: Session, track: Track) -> bool:
        session.state.current = track
        played.append(track.title)
        return True

    async def fake_teardown(session: Session, note: str) -> None:
        finished.append(note)
        cog._sessions.pop(session.guild_id, None)

    async def fake_update(session: Session) -> None:
        return None

    monkeypatch.setattr(cog, "_voice_client", lambda guild_id: _fake_voice_client())
    monkeypatch.setattr(cog, "_play_track", fake_play)
    monkeypatch.setattr(cog, "_teardown", fake_teardown)
    monkeypatch.setattr(cog, "_update_player_message", fake_update)
    return cog, played, finished


def _session_with(*titles: str) -> Session:
    session = Session(
        guild_id=1,
        channel_id=2,
        text_channel_id=3,
        view=object(),  # type: ignore[arg-type]
    )
    for title in titles:
        session.state.enqueue(_track(title), stay_after_end=False)
    session.state.start_next()
    return session


@pytest.mark.asyncio
async def test_skip_locked_plays_next_track(monkeypatch) -> None:
    cog, played, finished = _bare_cog(monkeypatch)
    session = _session_with("a")
    session.state.enqueue(_track("b"), stay_after_end=False)
    cog._sessions[1] = session

    await cog._skip_locked(session)

    assert played == ["b"]
    assert finished == []
    assert session.state.history[-1].title == "a"


@pytest.mark.asyncio
async def test_skip_locked_leaves_when_queue_empty(monkeypatch) -> None:
    cog, played, finished = _bare_cog(monkeypatch)
    session = _session_with("a")
    cog._sessions[1] = session

    await cog._skip_locked(session)

    assert played == []
    assert finished == ["Queue finished."]


@pytest.mark.asyncio
async def test_skip_locked_stays_when_asked(monkeypatch) -> None:
    cog, played, finished = _bare_cog(monkeypatch)
    session = _session_with("a")
    session.state.stay_after_end = True
    cog._sessions[1] = session

    await cog._skip_locked(session)

    assert played == []
    assert finished == []
    assert session.state.current is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("https://www.youtube.com/playlist?list=PL1234567890", True),
        ("https://music.youtube.com/playlist?list=OLAK5uy_abc", True),
        ("https://www.youtube.com/watch?v=abc123&list=PL1234567890", False),
        ("https://youtu.be/abc123", False),
        ("never gonna give you up", False),
    ],
)
def test_is_playlist_url(query: str, expected: bool) -> None:
    assert is_playlist_url(query) is expected


def test_describe_queue_shows_current_and_upcoming() -> None:
    state = QueueState()
    for title in ("a", "b", "c"):
        state.enqueue(_track(title), stay_after_end=True)
    state.start_next()

    text = describe_queue(state)

    assert "**Now playing:** a" in text
    assert "1. b" in text
    assert "2. c" in text
    assert "Staying in the channel" in text


def test_describe_queue_idle() -> None:
    state = QueueState()
    state.enqueue(_track("a"), stay_after_end=False)
    state.start_next()
    state.start_next()

    text = describe_queue(state)

    assert "**Now playing:**" not in text
    assert "Up next" not in text


def test_describe_queue_truncates_long_titles_and_lists() -> None:
    state = QueueState()
    state.enqueue(_track("x" * 200), stay_after_end=False)
    for index in range(20):
        state.enqueue(_track(f"track-{index}"), stay_after_end=False)
    state.start_next()

    text = describe_queue(state, limit=5)

    assert "x" * 100 not in text
    assert "…and 15 more" in text
    assert len(text) < 2000


def test_jump_discards_earlier_tracks() -> None:
    state = QueueState()
    for title in ("a", "b", "c", "d"):
        state.enqueue(_track(title), stay_after_end=False)
    state.start_next()  # a is playing

    assert state.jump(2).title == "c"
    assert state.current.title == "c"
    assert [track.title for track in state.queue] == ["d"]
    assert [track.title for track in state.history] == ["a"]


def test_jump_out_of_range_leaves_queue_alone() -> None:
    state = QueueState()
    for title in ("a", "b"):
        state.enqueue(_track(title), stay_after_end=False)
    state.start_next()

    assert state.jump(0) is None
    assert state.jump(5) is None
    assert state.current.title == "a"
    assert [track.title for track in state.queue] == ["b"]


def _bare_policy_cog() -> Voice:
    return Voice.__new__(Voice)


def _policy_session(*, playing: bool) -> Session:
    session = Session(
        guild_id=1,
        channel_id=2,
        text_channel_id=3,
        view=object(),  # type: ignore[arg-type]
    )
    if playing:
        session.state.enqueue(_track("a"), stay_after_end=False)
        session.state.start_next()
    return session


def test_play_rejection_allows_when_idle() -> None:
    cog = _bare_policy_cog()
    assert cog._play_rejection(_policy_session(playing=False), 2, 3) is None
    assert cog._play_rejection(None, 2, 3) is None


def test_play_rejection_blocks_other_voice_channel() -> None:
    cog = _bare_policy_cog()
    rejection = cog._play_rejection(_policy_session(playing=True), 9, 3)

    assert rejection is not None
    assert "<#2>" in rejection


def test_play_rejection_blocks_missing_voice_channel() -> None:
    cog = _bare_policy_cog()
    rejection = cog._play_rejection(_policy_session(playing=True), None, 3)

    assert rejection is not None
    assert "<#2>" in rejection


def test_play_rejection_blocks_other_text_channel() -> None:
    cog = _bare_policy_cog()
    rejection = cog._play_rejection(_policy_session(playing=True), 2, 9)

    assert rejection is not None
    assert "<#3>" in rejection


def test_play_rejection_allows_original_channels() -> None:
    cog = _bare_policy_cog()
    assert cog._play_rejection(_policy_session(playing=True), 2, 3) is None


def test_channel_rejection_requires_voice_and_text_channel() -> None:
    cog = _bare_policy_cog()
    session = _policy_session(playing=True)

    assert cog._channel_rejection(session, 2, 3) is None
    assert cog._channel_rejection(session, 9, 3) is not None
    assert cog._channel_rejection(session, 2, 9) is not None
    assert cog._channel_rejection(session, None, 3) is not None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (9, "0:09"), (65, "1:05"), (3725, "1:02:05"), (-3, "0:00")],
)
def test_format_position(seconds, expected) -> None:
    from all_around_bot.cogs.voice import format_position

    assert format_position(seconds) == expected


def test_describe_queue_stopped_shows_timestamp() -> None:
    state = QueueState()
    state.enqueue(_track("a"), stay_after_end=False)
    state.start_next()
    state.enqueue(_track("b"), stay_after_end=False)

    text = describe_queue(state, stopped=True, position=83)

    assert "**Stopped at** `1:23`" in text
    assert "**Now playing:**" not in text


def test_position_uses_saved_offset_and_elapsed_time(monkeypatch) -> None:
    cog = _bare_policy_cog()
    session = _policy_session(playing=True)

    monkeypatch.setattr("all_around_bot.cogs.voice.time.monotonic", lambda: 100.0)
    session.offset = 5.0
    session.started_at = 90.0

    assert cog._position(session) == 15.0

    session.started_at = None
    assert cog._position(session) == 5.0


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str | None, object]] = []

    async def edit(self, *, content: str | None = None, view: object = None) -> None:
        self.edits.append((content, view))


class _ParkableVoiceClient:
    def __init__(self) -> None:
        self.stopped = False
        self.disconnected = False

    def is_playing(self) -> bool:
        return True

    def is_paused(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True

    def is_connected(self) -> bool:
        return True

    async def disconnect(self, force: bool = False) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_stop_locked_parks_session_instead_of_clearing(monkeypatch) -> None:
    import time as time_module

    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._locks = {}
    cog._last_error = {}
    cog._expected_leave = set()

    session = _session_with("a")
    session.state.enqueue(_track("b"), stay_after_end=False)
    session.offset = 5.0
    session.started_at = time_module.monotonic() - 30
    session.message = _FakeMessage()  # type: ignore[assignment]
    cog._sessions[1] = session

    voice_client = _ParkableVoiceClient()
    monkeypatch.setattr(cog, "_voice_client", lambda guild_id: voice_client)

    await cog._stop_locked(session)

    assert session.stopped is True
    assert session.started_at is None
    assert 34.0 <= session.offset <= 36.0
    assert session.state.current.title == "a"
    assert [track.title for track in session.state.queue] == ["b"]
    assert cog._sessions.get(1) is session
    assert voice_client.stopped is True
    assert voice_client.disconnected is True
    last_content, last_view = session.message.edits[-1]  # type: ignore[union-attr]
    assert "Stopped at" in last_content
    assert last_view is None


def test_play_rejection_ignores_stopped_session() -> None:
    cog = _bare_policy_cog()
    session = _policy_session(playing=True)
    session.stopped = True

    assert cog._play_rejection(session, 9, 9) is None


def test_play_query_is_optional() -> None:
    parameters = {parameter.name: parameter for parameter in Voice.play.parameters}

    assert parameters["query"].required is False
    assert parameters["query"].default is None


def test_player_text_waits_while_connected(monkeypatch) -> None:
    cog = _bare_policy_cog()
    session = _policy_session(playing=False)

    connected = types.SimpleNamespace(is_connected=lambda: True)
    monkeypatch.setattr(cog, "_voice_client", lambda guild_id: connected)
    assert "Waiting for tracks" in cog._player_text(session)

    monkeypatch.setattr(cog, "_voice_client", lambda guild_id: None)
    assert "Queue finished" in cog._player_text(session)


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.ephemeral = False

    async def defer(self, thinking: bool = False, ephemeral: bool = False) -> None:
        self.deferred = True
        self.ephemeral = ephemeral

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        return None


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.channel_id = 3
        self.user = types.SimpleNamespace(id=1)
        self.original_response: str | None = None

    async def edit_original_response(
        self, *, content: str | None = None, **_kwargs: object
    ) -> None:
        self.original_response = content


class _FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.voice_client = None


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 2
        self.mention = "<#2>"


@pytest.mark.asyncio
async def test_join_or_resume_waits_when_nothing_is_saved(monkeypatch) -> None:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._locks = {}

    connected: list[tuple[int, int]] = []
    published: list[Session] = []

    async def fake_connect(guild, channel) -> None:
        connected.append((guild.id, channel.id))

    async def fake_publish(session: Session, interaction) -> None:
        published.append(session)

    monkeypatch.setattr(cog, "_connect", fake_connect)
    monkeypatch.setattr(cog, "_publish_player_message", fake_publish)

    interaction = _FakeInteraction()
    await cog._join_or_resume(interaction, _FakeGuild(), _FakeChannel())

    session = cog._sessions[1]
    assert session.state.current is None
    assert list(session.state.queue) == []
    assert session.state.stay_after_end is True
    assert session.channel_id == 2
    assert session.text_channel_id == 3
    assert connected == [(1, 2)]
    assert published == [session]
    assert interaction.response.deferred is True
    assert interaction.response.ephemeral is True
    assert interaction.original_response == "Waiting for tracks in <#2>."


@pytest.mark.asyncio
async def test_join_or_resume_resumes_parked_track(monkeypatch) -> None:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._locks = {}

    session = _session_with("a")
    session.stopped = True
    session.offset = 42.0
    session.channel_id = 9
    session.text_channel_id = 8
    cog._sessions[1] = session

    resumed: list[tuple[str, float]] = []
    published: list[Session] = []

    async def fake_connect(guild, channel) -> None:
        return None

    async def fake_play(session: Session, track: Track, *, offset: float = 0.0) -> bool:
        resumed.append((track.title, offset))
        session.state.current = track
        return True

    async def fake_publish(session: Session, interaction) -> None:
        published.append(session)

    monkeypatch.setattr(cog, "_connect", fake_connect)
    monkeypatch.setattr(cog, "_play_track", fake_play)
    monkeypatch.setattr(cog, "_publish_player_message", fake_publish)

    interaction = _FakeInteraction()
    await cog._join_or_resume(interaction, _FakeGuild(), _FakeChannel())

    assert session.stopped is False
    assert session.channel_id == 2
    assert resumed == [("a", 42.0)]
    assert published == [session]
    assert interaction.original_response == "Resumed in <#2>."


@pytest.mark.asyncio
async def test_join_or_resume_reports_already_playing(monkeypatch) -> None:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._locks = {}

    session = _session_with("a")
    cog._sessions[1] = session

    async def fake_connect(guild, channel) -> None:
        return None

    voice_client = types.SimpleNamespace(
        is_connected=lambda: True,
        is_paused=lambda: False,
    )
    guild = _FakeGuild()
    guild.voice_client = voice_client

    monkeypatch.setattr(cog, "_connect", fake_connect)
    monkeypatch.setattr(cog, "_voice_client", lambda guild_id: voice_client)

    interaction = _FakeInteraction()
    interaction.channel_id = session.text_channel_id
    await cog._join_or_resume(interaction, guild, _FakeChannel())

    assert interaction.original_response is not None
    assert "Already playing" in interaction.original_response
    assert interaction.response.ephemeral is True
    assert session.stopped is False


def test_voice_state_store_round_trip(tmp_path) -> None:
    store = VoiceStateStore(tmp_path / "voice_sessions.json")
    payload = {"version": 1, "sessions": [{"guild_id": 1, "channel_id": 2}]}

    store.save(payload)

    assert store.load() == payload
    assert not (tmp_path / "voice_sessions.json.tmp").exists()


def test_voice_state_store_missing_and_corrupt_files(tmp_path) -> None:
    path = tmp_path / "voice_sessions.json"
    store = VoiceStateStore(path)

    assert store.load() is None

    path.write_text("not json")
    assert store.load() is None


def _bare_storage_cog(store: VoiceStateStore, monkeypatch) -> Voice:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._store = store
    monkeypatch.setattr(
        cog,
        "_voice_client",
        lambda _guild_id: types.SimpleNamespace(
            is_connected=lambda: True,
            is_paused=lambda: False,
        ),
    )
    return cog


def test_snapshot_omits_expiring_stream_url_and_restores_queue(tmp_path, monkeypatch) -> None:
    store = VoiceStateStore(tmp_path / "voice_sessions.json")
    cog = _bare_storage_cog(store, monkeypatch)

    session = _session_with("now")
    session.state.enqueue(_track("next"), stay_after_end=False)
    session.offset = 42.5
    session.started_at = None
    session.resume_on_start = True
    session.message_id = 777
    cog._sessions[1] = session

    cog._save_now()

    raw = store.path.read_text()
    assert "example.test" not in raw
    assert "stream_url" not in raw

    restored_cog = Voice.__new__(Voice)
    restored_cog._sessions = {}
    restored_cog._store = store
    restored_cog._load_sessions()

    restored = restored_cog._sessions[1]
    assert restored.state.current is not None
    assert restored.state.current.title == "now"
    assert restored.state.current.stream_url is None
    assert [track.title for track in restored.state.queue] == ["next"]
    assert restored.offset == 42.5
    assert restored.stopped is True
    assert restored.resume_on_start is True
    assert restored.message_id == 777


def test_snapshot_saves_elapsed_position(monkeypatch) -> None:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    monkeypatch.setattr(cog, "_voice_client", lambda _guild_id: None)

    session = _session_with("a")
    session.offset = 5.0
    session.started_at = 90.0
    monkeypatch.setattr("all_around_bot.cogs.voice.time.monotonic", lambda: 100.0)

    payload = cog._session_payload(session)

    assert payload is not None
    assert payload["offset"] == 15.0


def test_stopped_session_is_not_marked_for_resume(tmp_path, monkeypatch) -> None:
    store = VoiceStateStore(tmp_path / "voice_sessions.json")
    cog = _bare_storage_cog(store, monkeypatch)

    session = _session_with("a")
    session.stopped = True
    session.resume_on_start = False
    cog._sessions[1] = session

    cog._save_now()
    restored_cog = Voice.__new__(Voice)
    restored_cog._sessions = {}
    restored_cog._store = store
    restored_cog._load_sessions()

    assert restored_cog._sessions[1].resume_on_start is False


def test_reconnect_is_cancelled_by_stop() -> None:
    cog = Voice.__new__(Voice)
    session = _session_with("a")
    session.stopped = True
    session.resume_on_start = True
    cog._sessions = {1: session}

    assert cog._should_reconnect(session) is True

    session.resume_on_start = False
    assert cog._should_reconnect(session) is False


@pytest.mark.asyncio
async def test_unexpected_disconnect_parks_and_keeps_queue(monkeypatch) -> None:
    cog = Voice.__new__(Voice)
    cog._sessions = {}
    cog._locks = {}
    cog._last_error = {}
    cog._expected_leave = set()
    cog._tasks = set()
    cog.bot = types.SimpleNamespace(user=types.SimpleNamespace(id=99))

    session = _session_with("a")
    session.state.enqueue(_track("b"), stay_after_end=False)
    session.offset = 5.0
    session.started_at = time.monotonic() - 30
    session.resume_on_start = True
    cog._sessions[1] = session

    spawned: list[object] = []
    saved: list[bool] = []

    async def fake_save() -> None:
        saved.append(True)

    def fake_spawn(coro) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(cog, "_voice_client", lambda _guild_id: None)
    monkeypatch.setattr(cog, "_save_state", fake_save)
    monkeypatch.setattr(cog, "_spawn", fake_spawn)

    member = types.SimpleNamespace(id=99, guild=types.SimpleNamespace(id=1))
    before = types.SimpleNamespace(channel=object())
    after = types.SimpleNamespace(channel=None)

    await cog.on_voice_state_update(member, before, after)

    assert cog._sessions.get(1) is session
    assert session.stopped is True
    assert 34.0 <= session.offset <= 36.0
    assert session.state.current is not None
    assert session.state.current.title == "a"
    assert [track.title for track in session.state.queue] == ["b"]
    assert session.resume_on_start is True
    assert len(spawned) == 1
    assert saved == [True]
