from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from .errors import ServiceUnavailable
from .models import ContentKind, RadioSnapshot, RadioSong, normalize

URL = "https://myonlineradio.hu/radio-1/most-szol"
BUDAPEST = ZoneInfo("Europe/Budapest")
log = logging.getLogger(__name__)

BREAK_LABELS = {
    "reklam",
    "reklam blokk",
    "hirdetes",
    "ad break",
    "commercial",
    "hirek",
    "idojaras",
    "kozlekedes",
    "jingle",
    "szignal",
    "station id",
}
SHOW_PATTERN = re.compile(
    r"\b(world is mine|radio show|radio 1 live mix|daily mix|disco s hit|cooky weekend|"
    r"kivansagmusor|balazsek|top 500|top 50)\b"
)


def classify(artist: str, title: str) -> ContentKind:
    artist_key, title_key = normalize(artist), normalize(title)
    if SHOW_PATTERN.search(f"{artist_key} {title_key}"):
        return ContentKind.SHOW
    if artist_key in {"radio 1", "radio1", "r1"} or title_key in BREAK_LABELS:
        return ContentKind.BREAK
    if artist_key in BREAK_LABELS:
        return ContentKind.BREAK
    return ContentKind.MUSIC


def parse_time(element: Tag | None, now: datetime) -> datetime | None:
    if element is None:
        return None
    content = element.get("content")
    if isinstance(content, str):
        try:
            stamp = datetime.fromisoformat(content)
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=BUDAPEST)
        except ValueError:
            pass
    text = element.get_text(" ", strip=True)
    match = re.search(r"(?:(\d{1,2})\.(\d{1,2})\.\s*)?(\d{1,2}):(\d{2})", text)
    if match is None:
        return None
    month, day, hour, minute = match.groups()
    local = now.astimezone(BUDAPEST)
    try:
        result = local.replace(
            month=int(month or local.month),
            day=int(day or local.day),
            hour=int(hour),
            minute=int(minute),
            second=0,
            microsecond=0,
        )
        if result > local + timedelta(minutes=5):
            result = result.replace(year=local.year - 1) if month else result - timedelta(days=1)
        return result
    except ValueError:
        return None


def parse_playlist(html: str, now: datetime | None = None) -> RadioSnapshot:
    now = now or datetime.now(UTC)
    soup = BeautifulSoup(html, "html.parser")
    entries: list[RadioSong] = []
    live: RadioSong | None = None
    for row in soup.select("div.yt-row")[:60]:
        artist_element = row.select_one('[itemprop="byArtist"]')
        title_element = row.select_one('[itemprop="name"]')
        if not artist_element or not title_element:
            continue
        artist = artist_element.get_text(" ", strip=True)
        title = title_element.get_text(" ", strip=True)
        if not artist or not title:
            continue
        item = row.select_one("[data-id]")
        source_id = str(item["data-id"]) if item else None
        stamp = parse_time(
            row.select_one('[itemprop="startDate"]') or row.select_one(".txt2.mcolumn"), now
        )
        song = RadioSong(artist, title, stamp, source_id, classify(artist, title))
        entries.append(song)
        if "live-link" in row.get("class", []):
            live = song
    if not entries:
        raise ServiceUnavailable("Rádió 1's playlist")
    if live is not None:
        entries.remove(live)
        entries.insert(0, live)
    current = entries[0]
    # Long shows are valid metadata; an old music row cannot prove an ad break.
    stale = bool(
        current.kind == ContentKind.MUSIC
        and current.played_at
        and now - current.played_at > timedelta(minutes=12)
    )
    return RadioSnapshot(tuple(entries), now, stale)


class Radio:
    def __init__(self, http: httpx.AsyncClient, ttl: float = 15) -> None:
        self.http = http
        self.ttl = ttl
        self._snapshot: RadioSnapshot | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> RadioSnapshot | None:
        return self._snapshot

    async def snapshot(self) -> RadioSnapshot:
        async with self._lock:
            if time.monotonic() < self._expires:
                if self._snapshot is not None:
                    return self._snapshot
                raise ServiceUnavailable("Rádió 1's playlist")
            try:
                response = await self.http.get(
                    URL,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; Radio1Discord/2.0)",
                        "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
                    },
                )
                response.raise_for_status()
                self._snapshot = await asyncio.to_thread(parse_playlist, response.text)
            except (httpx.HTTPError, ServiceUnavailable):
                log.warning("Radio playlist fetch failed; retrying after the cache interval.")
                self._expires = time.monotonic() + self.ttl
                if self._snapshot and datetime.now(UTC) - self._snapshot.fetched_at < timedelta(
                    minutes=2
                ):
                    self._snapshot = replace(self._snapshot, stale=True)
                    return self._snapshot
                self._snapshot = None
                raise ServiceUnavailable("Rádió 1's playlist") from None
            self._expires = time.monotonic() + self.ttl
            return self._snapshot
