from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", value))


class ContentKind(StrEnum):
    MUSIC = "music"
    BREAK = "break"
    SHOW = "show"


@dataclass(frozen=True, slots=True)
class RadioSong:
    artist: str
    title: str
    played_at: datetime | None = None
    source_id: str | None = None
    kind: ContentKind = ContentKind.MUSIC

    @property
    def key(self) -> str:
        if self.source_id:
            return f"id:{self.source_id}"
        stamp = self.played_at.isoformat() if self.played_at else ""
        return f"{stamp}:{normalize(self.artist)}:{normalize(self.title)}"

    @property
    def label(self) -> str:
        return f"{self.artist} · {self.title}"


@dataclass(frozen=True, slots=True)
class RadioSnapshot:
    entries: tuple[RadioSong, ...]  # Newest first, including shows and breaks.
    fetched_at: datetime
    stale: bool = False

    @property
    def current(self) -> RadioSong:
        return self.entries[0]

    @property
    def music(self) -> tuple[RadioSong, ...]:
        return tuple(song for song in self.entries if song.kind == ContentKind.MUSIC)


@dataclass(frozen=True, slots=True)
class Track:
    uri: str
    name: str
    artists: tuple[str, ...]
    duration_ms: int
    album: str = ""
    image_url: str | None = None
    url: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Track:
        album = data.get("album") or {}
        images = album.get("images") or []
        return cls(
            uri=data["uri"],
            name=data["name"],
            artists=tuple(artist["name"] for artist in data.get("artists", [])),
            duration_ms=int(data.get("duration_ms", 0)),
            album=album.get("name", ""),
            image_url=images[-1].get("url") if images else None,
            url=(data.get("external_urls") or {}).get("spotify"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str
    type: str
    active: bool = False
    restricted: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        return cls(
            id=data["id"],
            name=data.get("name", "Spotify"),
            type=data.get("type", "Device"),
            active=data.get("is_active", False),
            restricted=data.get("is_restricted", False),
        )


@dataclass(frozen=True, slots=True)
class Playback:
    playing: bool
    track: Track | None
    progress_ms: int
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class Account:
    discord_id: int
    spotify_id: str
    display_name: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: int
    device_id: str | None = None
    device_name: str | None = None
    needs_reauth: bool = False
