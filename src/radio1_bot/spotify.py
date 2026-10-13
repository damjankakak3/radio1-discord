from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .errors import (
    NoDevice,
    NotLinked,
    RateLimited,
    ReconnectRequired,
    ServiceUnavailable,
    SpotifyForbidden,
)
from .models import Account, Device, Playback, RadioSong, Track, normalize
from .storage import Store

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-read-private user-read-playback-state user-modify-playback-state"


class Unauthorized(Exception):
    pass


def clean_title(title: str) -> str:
    # Keep named remixes: erasing them can select an entirely different recording.
    title = re.sub(
        r"\s*[([](?:radio edit|original mix|extended mix|official (?:video|audio)|lyrics)[)\]]",
        "",
        title,
        flags=re.I,
    )
    return re.sub(r"\s+-\s+(?:radio edit|extended mix)$", "", title, flags=re.I).strip()


def main_artist(artist: str) -> str:
    return re.split(
        r"\s*(?:/|\sfeat\.?\s|\sft\.?\s|\sfeaturing\s|\s&\s|\sx\s)\s*",
        artist,
        maxsplit=1,
        flags=re.I,
    )[0].strip()


def remix_name(title: str) -> str | None:
    match = re.search(r"[([]([^\])]*remix[^\])]*)[)\]]|\s-\s(.*remix.*)$", title, re.I)
    if match is None:
        return None
    name = normalize(match.group(1) or match.group(2))
    return re.sub(r"\b(?:extended|radio|edit)\b", "", name).strip()


def match_track(items: list[dict[str, Any]], artist: str, title: str) -> Track | None:
    target_artist = normalize(main_artist(artist))
    target_title = normalize(clean_title(title))
    if not target_artist or not target_title:
        return None
    best: tuple[float, Track] | None = None
    target_versions = set(
        re.findall(r"\b(?:remix|acoustic|live|instrumental|karaoke|cover)\b", normalize(title))
    )
    for item in items:
        if item.get("is_playable") is False or not item.get("uri", "").startswith("spotify:track:"):
            continue
        track = Track.from_api(item)
        artist_score = max(
            (
                SequenceMatcher(None, target_artist, normalize(name)).ratio()
                for name in track.artists
            ),
            default=0.0,
        )
        title_score = SequenceMatcher(
            None, target_title, normalize(clean_title(track.name))
        ).ratio()
        versions = set(
            re.findall(
                r"\b(?:remix|acoustic|live|instrumental|karaoke|cover)\b", normalize(track.name)
            )
        )
        if (
            artist_score < 0.8
            or title_score < 0.9
            or versions != target_versions
            or remix_name(title) != remix_name(track.name)
        ):
            continue
        score = artist_score * 0.4 + title_score * 0.6
        if best is None or score > best[0]:
            best = (score, track)
    return best[1] if best else None


class Spotify:
    def __init__(self, settings: Settings, store: Store, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.store = store
        self.http = http
        self._blocked_until = 0.0
        self._search_cache: OrderedDict[tuple[int, str, str, str], tuple[float, Track | None]] = (
            OrderedDict()
        )
        self._search_lock = asyncio.Lock()

    def auth_url(self, state: str) -> str:
        return "https://accounts.spotify.com/authorize?" + urlencode(
            {
                "client_id": self.settings.spotify_client_id,
                "response_type": "code",
                "redirect_uri": self.settings.spotify_redirect_uri,
                "scope": SCOPES,
                "state": state,
                "show_dialog": "true",
            }
        )

    def _check_backoff(self) -> None:
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise RateLimited(remaining)

    def _check_response(self, response: httpx.Response, *, oauth: bool = False) -> None:
        status = response.status_code
        if status == 429:
            try:
                delay = max(1.0, float(response.headers.get("Retry-After", "30")))
            except ValueError:
                delay = 30.0
            self._blocked_until = max(self._blocked_until, time.monotonic() + delay)
            raise RateLimited(delay)
        if oauth and status in {400, 401}:
            raise ReconnectRequired()
        if status == 401:
            raise Unauthorized()
        if status == 403:
            raise SpotifyForbidden()
        if status == 404:
            raise NoDevice()
        if status >= 400:
            # Do not copy response bodies into logs or Discord: OAuth responses
            # can contain credentials, and upstream error text is not UI copy.
            raise ServiceUnavailable()

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except (ValueError, UnicodeError) as exc:
            raise ServiceUnavailable() from exc
        if not isinstance(data, dict):
            raise ServiceUnavailable()
        return data

    async def _grant(self, data: dict[str, str]) -> dict[str, Any]:
        self._check_backoff()
        try:
            response = await self.http.post(
                TOKEN_URL,
                data=data,
                auth=httpx.BasicAuth(
                    self.settings.spotify_client_id, self.settings.spotify_client_secret
                ),
            )
        except httpx.RequestError as exc:
            raise ServiceUnavailable() from exc
        self._check_response(response, oauth=True)
        result = self._response_object(response)
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token")
        try:
            expires_in = int(result["expires_in"])
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise ServiceUnavailable() from exc
        if (
            not isinstance(access_token, str)
            or not access_token
            or expires_in <= 0
            or (refresh_token is not None and not isinstance(refresh_token, str))
            or (data["grant_type"] == "authorization_code" and not refresh_token)
        ):
            raise ServiceUnavailable()
        return result

    async def exchange(self, discord_id: int, code: str) -> Account:
        data = await self._grant(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.spotify_redirect_uri,
            }
        )
        profile = await self._raw("GET", "/me", data["access_token"])
        return Account(
            discord_id=discord_id,
            spotify_id=profile["id"],
            display_name=profile.get("display_name") or profile["id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(time.time()) + int(data["expires_in"]),
        )

    async def _token(self, user_id: int, rejected_token: str | None = None) -> str:
        async with self.store.account_lock(user_id):
            account = await self.store.account(user_id)
            if account is None:
                raise NotLinked()
            if account.needs_reauth:
                raise ReconnectRequired()
            must_refresh = account.expires_at <= time.time() + 60
            must_refresh |= rejected_token is not None and account.access_token == rejected_token
            if not must_refresh:
                return account.access_token
            try:
                data = await self._grant(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": account.refresh_token,
                    }
                )
            except ReconnectRequired:
                await self.store.mark_reauth(user_id)
                raise
            updated = replace(
                account,
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token") or account.refresh_token,
                expires_at=int(time.time()) + int(data["expires_in"]),
            )
            await self.store.save_account(updated)
            return updated.access_token

    async def _raw(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        self._check_backoff()
        # Retry reads only. Replaying a timed-out playback mutation can restart a song.
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method,
                    API + path,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.3)
                    continue
                raise ServiceUnavailable() from exc
            if response.status_code >= 500 and attempt + 1 < attempts:
                await asyncio.sleep(0.3)
                continue
            self._check_response(response)
            return None if response.status_code == 204 else self._response_object(response)
        raise ServiceUnavailable()

    async def request(self, user_id: int, method: str, path: str, **kwargs: Any) -> Any:
        token = await self._token(user_id)
        try:
            return await self._raw(method, path, token, **kwargs)
        except Unauthorized:
            token = await self._token(user_id, rejected_token=token)
        try:
            return await self._raw(method, path, token, **kwargs)
        except Unauthorized as exc:
            async with self.store.account_lock(user_id):
                current = await self.store.account(user_id)
                if current is not None and current.access_token == token:
                    await self.store.mark_reauth(user_id)
            raise ReconnectRequired() from exc

    async def devices(self, user_id: int) -> list[Device]:
        data = await self.request(user_id, "GET", "/me/player/devices")
        return [Device.from_api(item) for item in data.get("devices", []) if item.get("id")]

    async def playback(self, user_id: int) -> Playback | None:
        data = await self.request(user_id, "GET", "/me/player")
        if data is None:
            return None
        item = data.get("item")
        track = Track.from_api(item) if item and item.get("type", "track") == "track" else None
        return Playback(
            playing=data.get("is_playing", False),
            track=track,
            progress_ms=int(data.get("progress_ms") or 0),
            device_id=(data.get("device") or {}).get("id"),
        )

    async def resolve_device(self, user_id: int) -> Device:
        account = await self.store.account(user_id)
        if account is None:
            raise NotLinked()
        devices = [device for device in await self.devices(user_id) if not device.restricted]
        if account.device_id:
            selected = next((device for device in devices if device.id == account.device_id), None)
            if selected is None:
                raise BotDeviceMissing(account.device_name or "Your selected device")
            return selected
        active = next((device for device in devices if device.active), None)
        if active:
            return active
        if len(devices) == 1:
            return devices[0]
        raise NoDevice()

    async def play(self, user_id: int, track: Track, device_id: str) -> None:
        await self.request(
            user_id,
            "PUT",
            "/me/player/play",
            params={"device_id": device_id},
            json={"uris": [track.uri]},
        )

    async def pause(self, user_id: int, device_id: str | None) -> None:
        await self.request(
            user_id, "PUT", "/me/player/pause", params={"device_id": device_id} if device_id else {}
        )

    async def search(self, user_id: int, song: RadioSong) -> Track | None:
        await self._token(user_id)
        account = await self.store.account(user_id)
        if account is None:
            raise NotLinked()
        key = (user_id, account.spotify_id, normalize(song.artist), normalize(song.title))
        # Keep caches per listener: catalog availability can vary by account.
        async with self._search_lock:
            cached = self._search_cache.get(key)
            if cached and cached[0] > time.monotonic():
                self._search_cache.move_to_end(key)
                return cached[1]
            artist = main_artist(song.artist).replace('"', "")
            title = clean_title(song.title).replace('"', "")
            queries = [f'artist:"{artist}" track:"{title}"', f"{song.artist} {song.title}"]
            found = None
            for query in queries:
                data = await self.request(
                    user_id, "GET", "/search", params={"q": query, "type": "track", "limit": 10}
                )
                found = match_track(
                    data.get("tracks", {}).get("items", []), song.artist, song.title
                )
                if found:
                    break
            self._search_cache[key] = (time.monotonic() + (3600 if found else 120), found)
            self._search_cache.move_to_end(key)
            while len(self._search_cache) > 256:
                self._search_cache.popitem(last=False)
            return found


class BotDeviceMissing(NoDevice):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.args = (
            f"{name} is offline. Open Spotify there or pick another device with /radio devices.",
        )
