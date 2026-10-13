from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from radio1_bot.config import Settings
from radio1_bot.errors import BotError, RateLimited, ServiceUnavailable
from radio1_bot.models import ContentKind, Device, Playback, RadioSnapshot, RadioSong, Track
from radio1_bot.playback import PlaybackEngine, Session

NOW = datetime(2026, 10, 4, 10, tzinfo=UTC)


def song(number: int, kind: ContentKind = ContentKind.MUSIC) -> RadioSong:
    return RadioSong("Artist", f"Song {number}", NOW + timedelta(minutes=number), str(number), kind)


def track(number: int) -> Track:
    return Track(f"spotify:track:{number}", f"Song {number}", ("Artist",), 180_000)


def snapshot(*songs: RadioSong, stale: bool = False) -> RadioSnapshot:
    return RadioSnapshot(songs or (song(1),), NOW, stale=stale)


def settings(**overrides: object) -> Settings:
    values = {
        "discord_token": "test-discord-token",
        "spotify_client_id": "test-client-id",
        "spotify_client_secret": "test-client-secret",
        "spotify_redirect_uri": "http://127.0.0.1:5000/callback",
        "encryption_key": Fernet.generate_key().decode(),
        "buffer_tracks": 3,
    }
    values.update(overrides)
    return Settings(**values)


class FakeRadio:
    def __init__(self, value: RadioSnapshot) -> None:
        self.value = value
        self.failure: Exception | None = None

    async def snapshot(self) -> RadioSnapshot:
        if self.failure is not None:
            raise self.failure
        return self.value


class FakeSpotify:
    def __init__(self) -> None:
        self.current: Playback | None = None
        self.plays: list[str] = []
        self.pauses: list[str] = []
        self.searches: list[str] = []
        self.missing: set[str] = set()
        self.play_error: Exception | None = None
        self.search_error: Exception | None = None
        self.playback_entered: asyncio.Event | None = None
        self.playback_release: asyncio.Event | None = None

    async def resolve_device(self, user_id: int) -> Device:
        return Device("device-1", "Desk", "Computer", active=True)

    async def search(self, user_id: int, value: RadioSong) -> Track | None:
        self.searches.append(value.key)
        if self.search_error is not None:
            raise self.search_error
        return None if value.key in self.missing else track(int(value.source_id or 1))

    async def play(self, user_id: int, value: Track, device_id: str) -> None:
        if self.play_error is not None:
            raise self.play_error
        self.plays.append(value.uri)
        self.current = Playback(True, value, 0, device_id)

    async def playback(self, user_id: int) -> Playback | None:
        if self.playback_entered is not None:
            self.playback_entered.set()
        if self.playback_release is not None:
            await self.playback_release.wait()
        return self.current

    async def pause(self, user_id: int, device_id: str) -> None:
        self.pauses.append(device_id)


def active_engine(monkeypatch: pytest.MonkeyPatch) -> tuple[PlaybackEngine, Session, FakeSpotify]:
    spotify = FakeSpotify()
    engine = PlaybackEngine(settings(), FakeRadio(snapshot(song(1))), spotify)
    session = Session(42, "device-1", "Desk", "buffered", deque([song(2)]))
    session.remember(song(1))
    session.current = track(1)
    session.last_owned_uri = track(1).uri
    session.started_at = 0
    session.state = "playing"
    engine.sessions[42] = session
    monkeypatch.setattr("radio1_bot.playback.time", SimpleNamespace(monotonic=lambda: 100.0))
    return engine, session, spotify


def test_ingest_orders_all_new_rows_chronologically_and_deduplicates() -> None:
    session = Session(42, "device-1", "Desk", "buffered")
    session.remember(song(10))
    newest = snapshot(song(14), song(13, ContentKind.BREAK), song(12), song(11), song(10), song(9))
    session.ingest(newest, 40)
    session.ingest(newest, 40)
    assert [entry.source_id for entry in session.queue] == ["11", "12", "14"]
    assert session.newest_id == 14


def test_ingest_caps_queue_by_dropping_oldest_and_ignores_stale_data() -> None:
    session = Session(42, "device-1", "Desk", "buffered")
    session.remember(song(1))
    session.ingest(snapshot(song(5), song(4), song(3), song(2)), cap=2)
    assert [entry.source_id for entry in session.queue] == ["4", "5"]
    assert session.dropped == 2
    session.ingest(snapshot(song(6), stale=True), cap=2)
    assert session.newest_id == 5
    assert [entry.source_id for entry in session.queue] == ["4", "5"]


def test_ingest_uses_timestamps_when_source_has_no_ids() -> None:
    old = RadioSong("Artist", "Repeat", NOW)
    new = RadioSong("Artist", "Repeat", NOW + timedelta(minutes=4))
    older = RadioSong("Artist", "Previous", NOW - timedelta(minutes=4))
    session = Session(42, "device-1", "Desk", "buffered")
    session.remember(old)
    session.ingest(snapshot(new, old, older), cap=40)
    assert list(session.queue) == [new]


def test_ingest_accepts_distinct_new_ids_with_the_same_timestamp() -> None:
    previous = song(1)
    new = replace(song(2), played_at=previous.played_at)
    session = Session(42, "device-1", "Desk", "buffered")
    session.remember(previous)
    session.ingest(snapshot(new, previous), cap=40)
    session.ingest(snapshot(new, previous), cap=40)
    assert list(session.queue) == [new]


def test_ingest_rejects_older_timestamps_even_with_new_ids() -> None:
    session = Session(42, "device-1", "Desk", "buffered")
    session.remember(song(1))
    session.ingest(snapshot(replace(song(2), played_at=NOW)), cap=40)
    assert not session.queue


def test_seen_history_is_bounded_without_forgetting_watermark() -> None:
    session = Session(42, "device-1", "Desk", "buffered")
    for number in range(1, 602):
        session.remember(song(number))
    assert len(session.seen) == 500
    session.ingest(snapshot(song(1)), cap=40)
    assert not session.queue
    assert session.newest_id == 601


@pytest.mark.parametrize(("mode", "first", "queued"), [("buffered", 2, [3, 4]), ("follow", 4, [])])
async def test_start_seeds_buffer_in_broadcast_order_or_current_song_only(
    mode: str, first: int, queued: list[int]
) -> None:
    spotify = FakeSpotify()
    engine = PlaybackEngine(
        settings(), FakeRadio(snapshot(song(4), song(3), song(2), song(1))), spotify
    )
    try:
        session = await engine.start(42, mode)
        assert spotify.plays == [track(first).uri]
        assert [int(value.source_id) for value in session.queue] == queued
        assert await engine.start(42, mode) is session
        assert len(spotify.plays) == 1
    finally:
        await engine.close()


async def test_follow_refuses_shows_but_buffered_mode_uses_recent_music() -> None:
    radio = FakeRadio(snapshot(song(4, ContentKind.SHOW), song(3), song(2), song(1)))
    spotify = FakeSpotify()
    engine = PlaybackEngine(settings(), radio, spotify)
    try:
        with pytest.raises(BotError, match="show or break"):
            await engine.start(42, "follow")
        assert not spotify.plays
        session = await engine.start(42, "buffered")
        assert session.current == track(1)
        assert [value.source_id for value in session.queue] == ["2", "3"]
    finally:
        await engine.close()


async def test_start_rejects_stale_playlist_before_touching_spotify() -> None:
    spotify = FakeSpotify()
    engine = PlaybackEngine(settings(), FakeRadio(snapshot(song(1), stale=True)), spotify)
    with pytest.raises(BotError, match="out of date"):
        await engine.start(42)
    assert not spotify.plays
    assert not engine.sessions


async def test_paused_track_never_resumes_from_elapsed_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    session.verified_end = 101.0
    spotify.current = Playback(False, track(1), 90_000, "device-1")
    await engine.tick(session)
    assert session.state == "paused"
    assert session.verified_end is None
    spotify.current = None
    monkeypatch.setattr("radio1_bot.playback.time", SimpleNamespace(monotonic=lambda: 100_000.0))
    await engine.tick(session)
    assert not spotify.plays
    assert session.current == track(1)
    assert list(session.queue) == [song(2)]


async def test_missing_playback_without_a_verified_end_does_not_autoplay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    await engine.tick(session)
    assert session.state == "paused"
    assert session.paused_by_user
    assert not spotify.plays


@pytest.mark.parametrize("progress_ms", [179_500, 179_999])
async def test_pause_near_song_end_does_not_start_next_song(
    monkeypatch: pytest.MonkeyPatch, progress_ms: int
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.current = Playback(False, track(1), progress_ms, "device-1")
    await engine.tick(session)
    assert session.state == "paused"
    assert session.current == track(1)
    assert list(session.queue) == [song(2)]
    assert not spotify.plays


async def test_full_song_finishes_before_next_song_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.current = Playback(True, track(1), 176_000, "device-1")
    delay = await engine.tick(session)
    assert delay == pytest.approx(4.2)
    assert not spotify.plays
    assert session.verified_end == 104.0
    monkeypatch.setattr("radio1_bot.playback.time", SimpleNamespace(monotonic=lambda: 105.0))
    spotify.current = Playback(False, track(1), 180_000, "device-1")
    await engine.tick(session)
    assert spotify.plays == [track(2).uri]


async def test_verified_completion_can_advance_when_spotify_returns_no_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.current = Playback(True, track(1), 179_000, "device-1")
    await engine.tick(session)
    spotify.current = None
    monkeypatch.setattr("radio1_bot.playback.time", SimpleNamespace(monotonic=lambda: 102.0))
    await engine.tick(session)
    assert spotify.plays == [track(2).uri]


async def test_waiting_session_survives_retained_completed_track_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    session.queue.clear()
    spotify.current = Playback(False, track(1), 180_000, "device-1")
    await engine.tick(session)
    assert session.state == "waiting"
    await engine.tick(session)
    assert engine.sessions.get(42) is session
    assert session.state == "waiting"
    engine.radio.value = snapshot(song(2), song(1))
    await engine.tick(session)
    assert spotify.plays == [track(2).uri]


@pytest.mark.parametrize(
    ("playback", "message"),
    [
        (Playback(False, track(99), 0, "device-1"), "took over"),
        (Playback(True, track(1), 0, "device-1"), "took over"),
        (Playback(False, track(1), 180_000, "device-2"), "switched Spotify devices"),
    ],
)
async def test_waiting_session_releases_on_user_takeover_before_next_song(
    monkeypatch: pytest.MonkeyPatch, playback: Playback, message: str
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    session.queue.clear()
    spotify.current = Playback(False, track(1), 180_000, "device-1")
    await engine.tick(session)
    assert session.state == "waiting"
    spotify.current = playback
    engine.radio.value = snapshot(song(2), song(1))
    await engine.tick(session)
    assert 42 not in engine.sessions
    assert message in engine.notices[42]
    assert not spotify.plays


@pytest.mark.parametrize(
    ("playback", "message"),
    [
        (Playback(True, track(99), 50_000, "device-1"), "took over"),
        (Playback(True, track(1), 50_000, "device-2"), "switched Spotify devices"),
        (Playback(True, None, 50_000, "device-1"), "took over"),
    ],
)
async def test_user_takeover_releases_session_without_changing_spotify(
    monkeypatch: pytest.MonkeyPatch, playback: Playback, message: str
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.current = playback
    await engine.tick(session)
    assert 42 not in engine.sessions
    assert session.state == "stopped"
    assert message in engine.notices[42]
    assert not spotify.plays
    assert not spotify.pauses


@pytest.mark.parametrize("operation", ["search", "play"])
@pytest.mark.parametrize("error", [RateLimited(30), ServiceUnavailable()])
async def test_transient_spotify_failure_preserves_next_song(
    monkeypatch: pytest.MonkeyPatch, operation: str, error: Exception
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    setattr(spotify, f"{operation}_error", error)
    with pytest.raises(type(error)):
        await engine._advance(session)
    assert list(session.queue) == [song(2)]
    assert session.skipped == 0
    setattr(spotify, f"{operation}_error", None)
    await engine._advance(session)
    assert spotify.plays == [track(2).uri]
    assert not session.queue


async def test_unmatched_songs_are_skipped_once_and_next_match_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    session.queue.append(song(3))
    spotify.missing.add(song(2).key)
    assert await engine._advance(session)
    assert session.skipped == 1
    assert spotify.plays == [track(3).uri]
    assert not session.queue


async def test_skip_with_all_unmatched_songs_preserves_current_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.missing.add(song(2).key)
    with pytest.raises(BotError, match="matched"):
        await engine.skip(42)
    assert session.current == track(1)
    assert session.state == "playing"
    assert session.skipped == 1
    assert not spotify.plays


async def test_skip_with_no_queue_reports_waiting_without_changing_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    session.queue.clear()
    with pytest.raises(BotError, match="no songs queued"):
        await engine.skip(42)
    assert session.current == track(1)
    assert not spotify.plays


async def test_radio_failure_does_not_discard_buffer_or_interrupt_current_song(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    engine.radio.failure = ServiceUnavailable("Rádió 1")
    spotify.current = Playback(True, track(1), 30_000, "device-1")
    await engine.tick(session)
    assert list(session.queue) == [song(2)]
    assert session.state == "playing"
    assert "Rádió 1" in session.last_error
    assert not spotify.plays


async def test_stop_serializes_with_inflight_tick_and_cannot_restart_after_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.playback_entered = asyncio.Event()
    spotify.playback_release = asyncio.Event()
    spotify.current = Playback(False, track(1), 180_000, "device-1")
    session.task = asyncio.create_task(engine._run(session))
    session.wake.set()
    await asyncio.wait_for(spotify.playback_entered.wait(), 1)
    stopping = asyncio.create_task(engine.stop(42))
    await asyncio.sleep(0)
    assert not stopping.done()
    spotify.playback_release.set()
    assert await asyncio.wait_for(stopping, 1) == "Radio playback stopped."
    plays_when_stopped = spotify.plays.copy()
    session.wake.set()
    await asyncio.sleep(0)
    assert 42 not in engine.sessions
    assert session.task.done()
    assert spotify.plays == plays_when_stopped
    assert spotify.pauses == ["device-1"]


async def test_stop_waits_for_inflight_start_and_removes_new_session() -> None:
    spotify = FakeSpotify()
    engine = PlaybackEngine(settings(), FakeRadio(snapshot(song(1))), spotify)
    entered, release = asyncio.Event(), asyncio.Event()
    original_play = spotify.play

    async def slow_play(user_id: int, value: Track, device_id: str) -> None:
        entered.set()
        await release.wait()
        await original_play(user_id, value, device_id)

    spotify.play = slow_play
    starting = asyncio.create_task(engine.start(42))
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(engine.stop(42))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    session = await asyncio.wait_for(starting, 1)
    await asyncio.wait_for(stopping, 1)
    assert not engine.sessions
    assert session.task.done()
    assert spotify.plays == [track(1).uri]
    assert spotify.pauses == ["device-1"]


@pytest.mark.parametrize("changed_device", [False, True])
async def test_stop_does_not_pause_a_user_takeover(
    monkeypatch: pytest.MonkeyPatch, changed_device: bool
) -> None:
    engine, session, spotify = active_engine(monkeypatch)
    spotify.current = Playback(
        True,
        track(1) if changed_device else track(99),
        0,
        "device-2" if changed_device else "device-1",
    )
    await engine.stop(42)
    assert 42 not in engine.sessions
    assert not spotify.pauses


async def test_close_cancels_all_sessions_and_prevents_new_starts() -> None:
    engine = PlaybackEngine(settings(), FakeRadio(snapshot(song(1))), FakeSpotify())
    first = await engine.start(42)
    second = await engine.start(43)
    await engine.close()
    assert not engine.sessions
    assert first.task.done()
    assert second.task.done()
    with pytest.raises(ServiceUnavailable):
        await engine.start(42)
