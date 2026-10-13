import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from radio1_bot.config import Settings
from radio1_bot.errors import ServiceUnavailable
from radio1_bot.models import Account
from radio1_bot.storage import Store
from radio1_bot.web import create_app


@pytest.fixture
async def oauth(tmp_path, aiohttp_client):
    key = Fernet.generate_key().decode()
    settings = Settings(
        "discord-test",
        "client-test",
        "secret-test",
        "https://radio.example.com/oauth/spotify",
        key,
        database_path=tmp_path / "accounts.db",
    )
    store = Store(settings.database_path, key)
    await store.open()
    spotify = MagicMock()
    spotify.exchange = AsyncMock(
        return_value=Account(
            42,
            "spotify-test",
            'Listener <script>alert("hello")</script>',
            "access-test",
            "refresh-test",
            int(time.time()) + 3600,
        )
    )
    engine = MagicMock()
    engine.stop = AsyncMock()
    ready = MagicMock(return_value=True)
    client = await aiohttp_client(create_app(settings, store, spotify, engine, ready))
    yield store, spotify, engine, client, ready
    await store.close()


async def test_callback_uses_configured_path_and_stores_encrypted_account(oauth):
    store, spotify, engine, client, _ = oauth
    state = await store.new_state(42)
    response = await client.get("/oauth/spotify", params={"state": state, "code": "code-test"})
    assert response.status == 200
    body = await response.text()
    assert "Spotify connected" in body
    assert "&lt;script&gt;" in body and "<script>alert" not in body
    assert "access-test" not in body and "refresh-test" not in body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    account = await store.account(42)
    assert account and account.spotify_id == "spotify-test"
    spotify.exchange.assert_awaited_once_with(42, "code-test")
    engine.stop.assert_awaited_once_with(42, pause=False)
    assert (await client.get("/callback")).status == 404
    replay = await client.get("/oauth/spotify", params={"state": state, "code": "code-test"})
    assert replay.status == 400
    assert spotify.exchange.await_count == 1


@pytest.mark.parametrize(
    "params", [{}, {"state": "unknown", "code": "code"}, {"state": "x" * 257, "code": "code"}]
)
async def test_missing_or_unknown_state_cannot_exchange_credentials(oauth, params):
    _, spotify, _, client, _ = oauth
    response = await client.get("/oauth/spotify", params=params)
    assert response.status == 400
    spotify.exchange.assert_not_awaited()


async def test_denied_login_consumes_link_without_saving_account(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    response = await client.get("/oauth/spotify", params={"state": state, "error": "access_denied"})
    assert response.status == 400
    assert await store.state_owner(state) is None
    assert await store.account(42) is None
    spotify.exchange.assert_not_awaited()


async def test_failed_exchange_does_not_save_or_leak_internal_error(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    spotify.exchange.side_effect = RuntimeError("secret-provider-body")
    response = await client.get("/oauth/spotify", params={"state": state, "code": "code-test"})
    assert response.status == 500
    assert "secret-provider-body" not in await response.text()
    assert await store.count() == 0
    assert await store.state_owner(state) is None


async def test_expected_provider_failure_is_actionable(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    spotify.exchange.side_effect = ServiceUnavailable()
    response = await client.get("/oauth/spotify", params={"state": state, "code": "code-test"})
    assert response.status == 400
    assert "Spotify is unavailable" in await response.text()


async def test_simultaneous_callbacks_exchange_only_once(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    responses = await asyncio.gather(
        *[
            client.get("/oauth/spotify", params={"state": state, "code": "same-code"})
            for _ in range(4)
        ]
    )
    assert sorted(response.status for response in responses) == [200, 400, 400, 400]
    assert spotify.exchange.await_count == 1


async def test_unlink_waits_for_inflight_callback_then_deletes_account(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    started, finish = asyncio.Event(), asyncio.Event()
    account = spotify.exchange.return_value

    async def exchange(*args):
        started.set()
        await finish.wait()
        return account

    spotify.exchange.side_effect = exchange
    callback = asyncio.create_task(
        client.get("/oauth/spotify", params={"state": state, "code": "code"})
    )
    await started.wait()
    unlink = asyncio.create_task(store.unlink(42))
    await asyncio.sleep(0)
    assert not unlink.done()
    finish.set()
    assert (await callback).status == 200
    await unlink
    assert await store.account(42) is None


async def test_unlink_invalidates_pending_callback(oauth):
    store, spotify, _, client, _ = oauth
    state = await store.new_state(42)
    await store.unlink(42)
    response = await client.get("/oauth/spotify", params={"state": state, "code": "code"})
    assert response.status == 400
    spotify.exchange.assert_not_awaited()


async def test_readiness_liveness_and_styles_do_not_expose_account_data(oauth):
    _, _, _, client, ready = oauth
    assert (await client.get("/health")).status == 200
    ready.return_value = False
    for path in ("/health", "/status"):
        response = await client.get(path)
        assert response.status == 503
        assert await response.json() == {"status": "starting"}
    assert (await client.get("/live")).status == 200
    css = await client.get("/callback.css")
    assert css.status == 200 and css.content_type == "text/css"
