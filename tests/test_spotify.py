from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

import radio1_bot.spotify as spotify_module
from radio1_bot.config import Settings
from radio1_bot.errors import NoDevice, RateLimited, ReconnectRequired, ServiceUnavailable
from radio1_bot.models import Account, RadioSong
from radio1_bot.spotify import BotDeviceMissing, Spotify, match_track
from radio1_bot.storage import Store


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[tuple[Settings, Store]]:
    settings = Settings(
        discord_token="discord-secret",
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_redirect_uri="https://bot.example.com/oauth/callback",
        encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "radio1.db",
    )
    store = Store(settings.database_path, settings.encryption_key)
    await store.open()
    try:
        yield settings, store
    finally:
        await store.close()


def account(user_id: int = 1, *, expired: bool = False) -> Account:
    return Account(
        discord_id=user_id,
        spotify_id=f"spotify-user-{user_id}",
        display_name=f"Listener {user_id}",
        access_token=f"access-{user_id}",
        refresh_token=f"refresh-{user_id}",
        expires_at=int(time.time()) + (-100 if expired else 3600),
    )


def api_track(
    *,
    name: str = "Levitating",
    artist: str = "Dua Lipa",
    identity: str = "original",
) -> dict:
    return {
        "uri": f"spotify:track:{identity}",
        "name": name,
        "artists": [{"name": artist}],
        "duration_ms": 203000,
        "is_playable": True,
        "album": {"name": "Future Nostalgia", "images": []},
    }


@pytest.mark.parametrize("body", ["upstream returned invalid JSON", "null", "[]", '"text"'])
@pytest.mark.parametrize("expired", [False, True])
async def test_malformed_provider_response_is_retryable_without_changing_account(
    database, body: str, expired: bool
) -> None:
    settings, store = database
    original = account(expired=expired)
    await store.save_account(original)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as http:
        spotify = Spotify(settings, store, http)
        with pytest.raises(ServiceUnavailable):
            await spotify.request(1, "GET", "/me")
    assert await store.account(1) == original


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "fresh"},
        {"access_token": None, "expires_in": 3600},
        {"access_token": "fresh", "expires_in": "invalid"},
        {"access_token": "fresh", "expires_in": 0},
        {"access_token": "fresh", "expires_in": 3600, "refresh_token": []},
    ],
)
async def test_invalid_token_grant_preserves_saved_credentials(database, payload: dict) -> None:
    settings, store = database
    original = account(expired=True)
    await store.save_account(original)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        spotify = Spotify(settings, store, http)
        with pytest.raises(ServiceUnavailable):
            await spotify.request(1, "GET", "/me")
    assert await store.account(1) == original


async def test_expired_token_refreshes_once_for_concurrent_requests(database):
    settings, store = database
    await store.save_account(account(expired=True))
    requests: Counter[str] = Counter()

    async def handle(request: httpx.Request) -> httpx.Response:
        requests[request.url.path] += 1
        if request.url.path == "/api/token":
            assert parse_qs(request.content.decode())["refresh_token"] == ["refresh-1"]
            assert request.headers["Authorization"].startswith("Basic ")
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh"
        return httpx.Response(200, json={"id": "spotify-user-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        results = await asyncio.gather(*(spotify.request(1, "GET", "/me") for _ in range(12)))
    assert len(results) == 12
    assert requests == {"/api/token": 1, "/v1/me": 12}
    saved = await store.account(1)
    assert saved is not None and saved.access_token == "fresh"


async def test_concurrent_401_reuses_refreshed_token_and_retries_once(database):
    settings, store = database
    await store.save_account(account())
    requests: Counter[str] = Counter()
    old_requests_arrived = asyncio.Event()
    concurrency = 6

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            requests["refresh"] += 1
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        token = request.headers["Authorization"]
        requests[token] += 1
        if token == "Bearer access-1":
            if requests[token] == concurrency:
                old_requests_arrived.set()
            await asyncio.wait_for(old_requests_arrived.wait(), timeout=3)
            return httpx.Response(401, json={"error": "expired"})
        assert token == "Bearer fresh"
        return httpx.Response(200, json={"id": "spotify-user-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        results = await asyncio.gather(
            *(spotify.request(1, "GET", "/me") for _ in range(concurrency))
        )
    assert all(result["id"] == "spotify-user-1" for result in results)
    assert requests == {"refresh": 1, "Bearer access-1": concurrency, "Bearer fresh": concurrency}


async def test_second_401_stops_retrying_and_requires_reconnect(database):
    settings, store = database
    await store.save_account(account())
    requests: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        requests[request.url.path] += 1
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        return httpx.Response(401, json={"error": "invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        with pytest.raises(ReconnectRequired):
            await spotify.request(1, "GET", "/me")
        with pytest.raises(ReconnectRequired):
            await spotify.request(1, "GET", "/me")
    saved = await store.account(1)
    assert saved is not None and saved.needs_reauth
    assert requests == {"/api/token": 1, "/v1/me": 2}


async def test_delayed_second_401_cannot_invalidate_newly_relinked_account(database):
    settings, store = database
    await store.save_account(account())
    retry_arrived = asyncio.Event()
    release_retry = asyncio.Event()
    requests: Counter[str] = Counter()

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            requests["refresh"] += 1
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        token = request.headers["Authorization"]
        requests[token] += 1
        if token == "Bearer access-1":
            return httpx.Response(401)
        if token == "Bearer fresh":
            retry_arrived.set()
            await asyncio.wait_for(release_retry.wait(), timeout=3)
            return httpx.Response(401)
        assert token == "Bearer newly-linked"
        return httpx.Response(200, json={"id": "new-spotify-account"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        pending = asyncio.create_task(spotify.request(1, "GET", "/me"))
        await asyncio.wait_for(retry_arrived.wait(), timeout=3)
        async with store.account_lock(1):
            await store.save_account(
                replace(
                    account(),
                    access_token="newly-linked",
                    refresh_token="newly-linked-refresh",
                    spotify_id="new-spotify-account",
                )
            )
        release_retry.set()
        with pytest.raises(ReconnectRequired):
            await pending
        saved = await store.account(1)
        assert saved is not None and not saved.needs_reauth
        assert await spotify.request(1, "GET", "/me") == {"id": "new-spotify-account"}
    assert requests == {
        "refresh": 1,
        "Bearer access-1": 1,
        "Bearer fresh": 1,
        "Bearer newly-linked": 1,
    }


@pytest.mark.parametrize("rotated", ["rotated-refresh", None, ""])
async def test_refresh_rotates_or_preserves_refresh_token_and_device(database, rotated):
    settings, store = database
    previous = replace(account(expired=True), device_id="desktop", device_name="Study")
    await store.save_account(previous)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            response = {"access_token": "fresh", "expires_in": 3600}
            if rotated is not None:
                response["refresh_token"] = rotated
            return httpx.Response(200, json=response)
        return httpx.Response(200, json={"id": "spotify-user-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await Spotify(settings, store, http).request(1, "GET", "/me")
    saved = await store.account(1)
    assert saved is not None
    assert saved.refresh_token == (rotated or previous.refresh_token)
    assert saved.device_id == "desktop" and saved.device_name == "Study"
    assert saved.spotify_id == previous.spotify_id
    assert saved.expires_at > int(time.time()) + 3500


async def test_revoked_refresh_marks_reauth_and_suppresses_future_calls(database):
    settings, store = database
    await store.save_account(account(expired=True))
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": "invalid_grant", "secret": "private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        for _ in range(2):
            with pytest.raises(ReconnectRequired) as error:
                await spotify.request(1, "GET", "/me")
            assert "private" not in str(error.value)
    saved = await store.account(1)
    assert saved is not None and saved.needs_reauth
    assert calls == ["/api/token"]


async def test_refresh_credentials_are_independent_for_each_listener(database):
    settings, store = database
    for user_id in (1, 2):
        await store.save_account(account(user_id, expired=True))
    refreshed = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            refresh = parse_qs(request.content.decode())["refresh_token"][0]
            refreshed.append(refresh)
            return httpx.Response(
                200,
                json={
                    "access_token": f"fresh-{refresh[-1]}",
                    "refresh_token": f"rotated-{refresh[-1]}",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(200, json={"token": request.headers["Authorization"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        results = await asyncio.gather(*(spotify.request(uid, "GET", "/me") for uid in (1, 2)))
    assert results == [{"token": "Bearer fresh-1"}, {"token": "Bearer fresh-2"}]
    assert set(refreshed) == {"refresh-1", "refresh-2"}
    for user_id in (1, 2):
        saved = await store.account(user_id)
        assert saved is not None and saved.refresh_token == f"rotated-{user_id}"


async def test_search_cache_and_catalog_are_independent_for_each_listener(database):
    settings, store = database
    for user_id in (1, 2):
        await store.save_account(account(user_id))
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"]
        calls[token] += 1
        return httpx.Response(200, json={"tracks": {"items": [api_track(identity=token[-1])]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        song = RadioSong("Dua Lipa", "Levitating")
        first = await spotify.search(1, song)
        second = await spotify.search(2, song)
        assert await spotify.search(1, RadioSong("DUA LIPA", "levitating")) == first
        assert await spotify.search(2, song) == second
    assert first is not None and first.uri == "spotify:track:1"
    assert second is not None and second.uri == "spotify:track:2"
    assert calls == {"Bearer access-1": 1, "Bearer access-2": 1}


async def test_relink_to_different_spotify_account_cannot_reuse_old_catalog_cache(database):
    settings, store = database
    await store.save_account(account())
    tokens = []

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"]
        tokens.append(token)
        identity = "original-account" if token == "Bearer access-1" else "new-account"
        return httpx.Response(200, json={"tracks": {"items": [api_track(identity=identity)]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        song = RadioSong("Dua Lipa", "Levitating")
        original = await spotify.search(1, song)
        async with store.account_lock(1):
            await store.save_account(
                replace(
                    account(),
                    spotify_id="different-account",
                    access_token="different-access",
                    refresh_token="different-refresh",
                )
            )
        changed = await spotify.search(1, song)
    assert original is not None and original.uri == "spotify:track:original-account"
    assert changed is not None and changed.uri == "spotify:track:new-account"
    assert tokens == ["Bearer access-1", "Bearer different-access"]


@pytest.mark.parametrize("retry_after, expected", [("17", 17), ("invalid", 30), ("0", 1)])
async def test_429_backoff_suppresses_requests_globally(
    database,
    monkeypatch,
    retry_after,
    expected,
):
    settings, store = database
    await store.save_account(account())
    await store.save_account(account(2, expired=True))
    now = [100.0]
    monkeypatch.setattr(
        spotify_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time)
    )
    paths = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"id": "spotify-user-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        with pytest.raises(RateLimited) as error:
            await spotify.request(1, "GET", "/me")
        assert error.value.retry_after == expected
        for uid in (1, 2):
            with pytest.raises(RateLimited):
                await spotify.request(uid, "GET", "/me")
        assert paths == ["/v1/me"]
        now[0] += expected + 0.01
        assert await spotify.request(1, "GET", "/me") == {"id": "spotify-user-1"}
    assert paths == ["/v1/me", "/v1/me"]


@pytest.mark.parametrize("failure", ["transport", "server"])
async def test_reads_retry_once_on_temporary_failure(database, failure):
    settings, store = database
    await store.save_account(account())
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            if failure == "transport":
                raise httpx.ReadTimeout("network timeout", request=request)
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"devices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert await Spotify(settings, store, http).devices(1) == []
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["transport", "server"])
async def test_playback_mutations_are_not_replayed_on_temporary_failure(database, failure):
    settings, store = database
    await store.save_account(account())
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("network timeout", request=request)
        return httpx.Response(503, json={"error": "temporary"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ServiceUnavailable):
            await Spotify(settings, store, http).pause(1, "desktop")
    assert len(calls) == 1
    assert calls[0].method == "PUT"
    assert calls[0].url.params["device_id"] == "desktop"


async def test_null_and_restricted_devices_cannot_be_selected(database):
    settings, store = database
    await store.save_account(account())
    devices = [
        {"id": None, "name": "Missing ID", "is_active": True},
        {"id": "restricted", "name": "Speaker", "is_active": True, "is_restricted": True},
        {"id": "desktop", "name": "Desktop", "type": "Computer"},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"devices": devices})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        assert [device.id for device in await spotify.devices(1)] == ["restricted", "desktop"]
        assert (await spotify.resolve_device(1)).id == "desktop"
        await store.set_device(1, "restricted", "Speaker")
        with pytest.raises(BotDeviceMissing, match="Speaker is offline"):
            await spotify.resolve_device(1)
        await store.set_device(1, None, None)
        devices.pop()
        with pytest.raises(NoDevice):
            await spotify.resolve_device(1)


async def test_automatic_device_prefers_active_and_does_not_guess_among_inactive(database):
    settings, store = database
    await store.save_account(account())
    devices = [
        {"id": "phone", "name": "Phone"},
        {"id": "desktop", "name": "Desktop", "is_active": True},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"devices": devices})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        spotify = Spotify(settings, store, http)
        assert (await spotify.resolve_device(1)).id == "desktop"
        devices[1]["is_active"] = False
        with pytest.raises(NoDevice):
            await spotify.resolve_device(1)
        await store.set_device(1, "phone", "Phone")
        assert (await spotify.resolve_device(1)).id == "phone"


@pytest.mark.parametrize(
    "status, data",
    [
        (204, None),
        (200, {"item": None, "device": None, "progress_ms": None, "is_playing": False}),
        (200, {"item": {"type": "episode"}, "device": None, "is_playing": True}),
    ],
)
async def test_playback_handles_no_playback_null_device_and_podcasts(database, status, data):
    settings, store = database
    await store.save_account(account())

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=data) if data is not None else httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        playback = await Spotify(settings, store, http).playback(1)
    if status == 204:
        assert playback is None
    else:
        assert playback is not None
        assert playback.track is None and playback.device_id is None and playback.progress_ms == 0


@pytest.mark.parametrize(
    "candidate",
    [
        api_track(name="Don't Stop Now"),
        api_track(name="Don't Start Now", artist="Dua Cover Band"),
        api_track(name="Don't Start Now (Cover)"),
        api_track(name="Don't Start Now (Live)"),
        api_track(name="Don't Start Now (Acoustic)"),
        api_track(name="Don't Start Now (Karaoke)"),
        api_track(name="Don't Start Now - Remix"),
    ],
)
def test_matching_refuses_wrong_titles_artists_and_recordings(candidate):
    assert match_track([candidate], "Dua Lipa", "Don't Start Now") is None


def test_matching_refuses_different_artist_with_similar_name():
    assert (
        match_track(
            [api_track(name="Love Me Again", artist="John Lennon")], "John Newman", "Love Me Again"
        )
        is None
    )


def test_matching_refuses_different_named_remix():
    candidate = api_track(name="Levitating - Alesso Remix")
    assert match_track([candidate], "Dua Lipa", "Levitating - Tiesto Remix") is None


def test_matching_accepts_exact_recording_and_radio_edit_but_skips_unplayable():
    unplayable = {**api_track(identity="unavailable"), "is_playable": False}
    episode = {**api_track(identity="episode"), "uri": "spotify:episode:123"}
    playable = api_track(name="Levitating (Radio Edit)", identity="radio-edit")
    found = match_track([unplayable, episode, playable], "Dua Lipa feat. DaBaby", "Levitating")
    assert found is not None and found.uri == "spotify:track:radio-edit"


def test_matching_preserves_named_remix_and_accents():
    candidate = api_track(name="Été - Tiësto Remix", artist="Beyoncé", identity="remix")
    found = match_track([candidate], "Beyonce", "Ete - Tiesto Remix")
    assert found is not None and found.uri == "spotify:track:remix"
