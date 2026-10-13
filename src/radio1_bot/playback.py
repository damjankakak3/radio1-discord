from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .config import Settings
from .errors import (
    BotError,
    NoDevice,
    NotLinked,
    RateLimited,
    ReconnectRequired,
    ServiceUnavailable,
    SpotifyForbidden,
)
from .models import ContentKind, RadioSnapshot, RadioSong, Track
from .radio import Radio
from .spotify import Spotify

log = logging.getLogger(__name__)
Mode = Literal["follow", "buffered"]


@dataclass(slots=True)
class Session:
    user_id: int
    device_id: str
    device_name: str
    mode: Mode
    queue: deque[RadioSong] = field(default_factory=deque)
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    newest_id: int = 0
    newest_time: datetime | None = None
    current: Track | None = None
    last_owned_uri: str | None = None
    state: str = "waiting"
    last_error: str | None = None
    skipped: int = 0
    dropped: int = 0
    started_at: float = 0.0
    verified_end: float | None = None
    paused_by_user: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def remember(self, song: RadioSong) -> None:
        self.seen[song.key] = None
        self.seen.move_to_end(song.key)
        while len(self.seen) > 500:
            self.seen.popitem(last=False)
        if song.source_id and song.source_id.isdecimal():
            self.newest_id = max(self.newest_id, int(song.source_id))
        if song.played_at and (self.newest_time is None or song.played_at > self.newest_time):
            self.newest_time = song.played_at

    def ingest(self, snapshot: RadioSnapshot, cap: int) -> None:
        if snapshot.stale:
            return
        # Compare every entry to the previous snapshot's watermark, not to the
        # preceding row, so all new songs from one poll enter in broadcast order.
        previous_id, previous_time = self.newest_id, self.newest_time
        for song in reversed(snapshot.entries):
            known = song.key in self.seen
            old_id = bool(
                song.source_id and song.source_id.isdecimal() and int(song.source_id) <= previous_id
            )
            newer_id = bool(
                song.source_id and song.source_id.isdecimal() and int(song.source_id) > previous_id
            )
            # The station's timestamps may have only minute precision. A newer
            # source ID distinguishes separate entries within that same minute.
            old_time = bool(
                song.played_at
                and previous_time
                and (
                    song.played_at < previous_time
                    or (song.played_at == previous_time and not newer_id)
                )
            )
            self.remember(song)
            if known or old_id or old_time or song.kind != ContentKind.MUSIC:
                continue
            self.queue.append(song)
        while len(self.queue) > cap:
            self.queue.popleft()
            self.dropped += 1


class PlaybackEngine:
    def __init__(self, settings: Settings, radio: Radio, spotify: Spotify) -> None:
        self.settings = settings
        self.radio = radio
        self.spotify = spotify
        self.sessions: dict[int, Session] = {}
        self.notices: OrderedDict[int, str] = OrderedDict()
        self._locks: dict[int, asyncio.Lock] = {}
        self._closed = False

    def lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def notice(self, user_id: int, message: str) -> None:
        self.notices[user_id] = message
        self.notices.move_to_end(user_id)
        while len(self.notices) > 500:
            self.notices.popitem(last=False)

    async def start(self, user_id: int, mode: Mode = "buffered") -> Session:
        async with self.lock(user_id):
            if self._closed:
                raise ServiceUnavailable("The bot")
            if user_id in self.sessions:
                return self.sessions[user_id]
            snapshot = await self.radio.snapshot()
            if snapshot.stale:
                raise BotError("The station's playlist is out of date. Try again in a few minutes.")
            if mode == "follow" and snapshot.current.kind != ContentKind.MUSIC:
                raise BotError("The station is broadcasting a show or break. Try buffered mode.")
            device = await self.spotify.resolve_device(user_id)
            seed = snapshot.music[: self.settings.buffer_tracks if mode == "buffered" else 1]
            if not seed:
                raise BotError("The station has no recent songs to play. Try again later.")
            session = Session(user_id, device.id, device.name, mode, deque(reversed(seed)))
            for song in snapshot.entries:
                session.remember(song)
            if not await self._advance(session):
                raise BotError(
                    "Couldn't find these songs on Spotify. Try again when the playlist changes."
                )
            self.sessions[user_id] = session
            self.notices.pop(user_id, None)
            session.task = asyncio.create_task(self._run(session), name=f"radio1-session-{user_id}")
            return session

    async def _advance(self, session: Session, *, preserve_current: bool = False) -> bool:
        while session.queue:
            song = session.queue[0]
            track = await self.spotify.search(session.user_id, song)
            if track is None:
                session.queue.popleft()
                session.skipped += 1
                continue
            # Remove only after Spotify accepts playback. A 429 or network failure
            # must not silently discard the next song.
            await self.spotify.play(session.user_id, track, session.device_id)
            session.queue.popleft()
            session.current = track
            session.last_owned_uri = track.uri
            session.started_at = time.monotonic()
            session.verified_end = None
            session.paused_by_user = False
            session.state = "playing"
            session.last_error = None
            return True
        if preserve_current:
            return False
        if session.current:
            session.last_owned_uri = session.current.uri
        session.current = None
        session.verified_end = None
        session.state = "waiting"
        return False

    async def tick(self, session: Session) -> float:
        interval = self.settings.spotify_poll
        try:
            snapshot = await self.radio.snapshot()
        except ServiceUnavailable as exc:
            session.last_error = str(exc)
        else:
            session.ingest(snapshot, self.settings.max_queue)
            session.last_error = (
                "The station's playlist is out of date." if snapshot.stale else None
            )
        playback = await self.spotify.playback(session.user_id)
        now = time.monotonic()
        if playback and playback.device_id and playback.device_id != session.device_id:
            self._release(session, "You switched Spotify devices. Radio playback stopped.")
            return interval
        if session.current is None:
            if playback is not None and (
                playback.playing
                or (playback.track is not None and playback.track.uri != session.last_owned_uri)
            ):
                self._release(session, "You took over Spotify. Radio playback stopped.")
                return interval
            await self._advance(session)
            return interval
        if now - session.started_at < 3:
            return interval  # Spotify may take a moment to report a newly started track.
        if playback is None:
            # No playback is not proof that a song finished. Advance only after
            # a verified playing sample reached its predicted end, with no pause.
            if (
                not session.paused_by_user
                and session.verified_end is not None
                and now >= session.verified_end
            ):
                await self._advance(session)
            else:
                session.state = "paused"
                session.paused_by_user = True
            return interval
        if playback.track is None or playback.track.uri != session.current.uri:
            self._release(session, "You took over Spotify. Radio playback stopped.")
            return interval
        remaining = max(0, session.current.duration_ms - playback.progress_ms) / 1000
        if not playback.playing:
            if remaining <= 0 and not session.paused_by_user:
                await self._advance(session)
            else:
                session.state = "paused"
                session.paused_by_user = True
                session.verified_end = None
            return interval
        session.paused_by_user = False
        session.state = "playing"
        session.verified_end = now + remaining
        # Let the full track finish; use a shorter poll at the boundary instead of
        # cutting the last four seconds like the old bot.
        if remaining <= 0:
            await self._advance(session)
            return interval
        return max(1.0, min(interval, remaining + 0.2))

    def _release(self, session: Session, message: str) -> None:
        if self.sessions.get(session.user_id) is session:
            del self.sessions[session.user_id]
        session.state = "stopped"
        self.notice(session.user_id, message)

    async def _run(self, session: Session) -> None:
        delay = self.settings.spotify_poll
        failures = 0
        try:
            while not self._closed and self.sessions.get(session.user_id) is session:
                try:
                    await asyncio.wait_for(session.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                session.wake.clear()
                async with self.lock(session.user_id):
                    if self.sessions.get(session.user_id) is not session:
                        return
                    try:
                        delay = await self.tick(session)
                        failures = 0
                    except RateLimited as exc:
                        session.last_error = str(exc)
                        delay = exc.retry_after + 0.5
                    except (NotLinked, ReconnectRequired, NoDevice, SpotifyForbidden) as exc:
                        self._release(session, str(exc))
                    except ServiceUnavailable as exc:
                        failures += 1
                        session.last_error = str(exc)
                        delay = min(60.0, self.settings.spotify_poll * 2 ** min(failures, 3))
                        if failures >= 5:
                            self._release(
                                session, "Spotify stayed unavailable. Restart with /radio play."
                            )
                    except Exception:
                        log.exception("Playback session failed for user %s", session.user_id)
                        self._release(
                            session, "Playback stopped after an error. Try /radio play again."
                        )
        except asyncio.CancelledError:
            raise
        finally:
            if self.sessions.get(session.user_id) is session:
                self.sessions.pop(session.user_id, None)

    async def skip(self, user_id: int) -> Session:
        async with self.lock(user_id):
            session = self.sessions.get(user_id)
            if session is None:
                raise BotError("Start a session with /radio play first.")
            session.ingest(await self.radio.snapshot(), self.settings.max_queue)
            if not session.queue:
                raise BotError(
                    "There are no songs queued yet. Waiting for the station's next song."
                )
            if not await self._advance(session, preserve_current=True):
                raise BotError(
                    "None of the queued songs matched on Spotify. Waiting for new songs."
                )
            session.wake.set()
            return session

    async def stop(self, user_id: int, *, pause: bool = True) -> str:
        async with self.lock(user_id):
            session = self.sessions.pop(user_id, None)
            if session and session.task:
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            self.notices.pop(user_id, None)
            if not pause:
                return "Radio playback stopped."
            if session is None:
                return "No radio session is running."
            try:
                current = await self.spotify.playback(user_id)
                # Stop our own track only, even if the user took over between polls.
                if (
                    current
                    and current.track
                    and session.current
                    and current.track.uri == session.current.uri
                    and current.device_id in {None, session.device_id}
                ):
                    await self.spotify.pause(user_id, session.device_id)
            except BotError:
                return "Radio playback stopped. Spotify couldn't be paused; pause it in the app."
            return "Radio playback stopped."

    async def close(self) -> None:
        self._closed = True
        tasks = [session.task for session in self.sessions.values() if session.task]
        self.sessions.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
