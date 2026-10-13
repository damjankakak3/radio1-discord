from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import stat
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from radio1_bot.errors import ReconnectRequired
from radio1_bot.migrate import migrate_legacy
from radio1_bot.models import Account
from radio1_bot.storage import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    value = Store(tmp_path / "private" / "radio1.db", Fernet.generate_key().decode())
    await value.open()
    try:
        yield value
    finally:
        await value.close()


def account(discord_id: int = 42) -> Account:
    return Account(
        discord_id,
        "spotify-user",
        "A listener",
        "access-token-very-secret",
        "refresh-token-very-secret",
        2_000_000_000,
        "device-1",
        "Desk",
    )


async def test_tokens_roundtrip_encrypted_and_do_not_leak_in_repr(store: Store) -> None:
    original = account()
    await store.save_account(original)
    assert await store.account(42) == original
    async with store.db.execute("SELECT * FROM accounts WHERE discord_id=42") as cursor:
        row = await cursor.fetchone()
    assert row["access_token"] != original.access_token
    assert row["refresh_token"] != original.refresh_token
    assert store.cipher.decrypt(row["access_token"].encode()).decode() == original.access_token
    assert store.cipher.decrypt(row["refresh_token"].encode()).decode() == original.refresh_token
    assert original.access_token not in repr(original)
    assert original.refresh_token not in repr(original)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    await store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert original.access_token.encode() not in store.path.read_bytes()
    assert original.refresh_token.encode() not in store.path.read_bytes()


async def test_account_updates_preserve_one_row_and_support_sql_punctuation(store: Store) -> None:
    await store.save_account(account())
    updated = replace(
        account(),
        display_name="O'Brien'); DROP TABLE accounts; --",
        access_token="new access",
        refresh_token="new refresh",
        needs_reauth=True,
    )
    await store.save_account(updated)
    assert await store.account(42) == updated
    assert await store.count() == 1
    assert await store.account(999) is None


async def test_database_reopen_preserves_accounts_and_states_without_schema_reset(
    store: Store,
) -> None:
    await store.save_account(account())
    state = await store.new_state(42)
    await store.close()
    await store.open()
    assert await store.account(42) == account()
    assert await store.consume_state(state) == 42
    async with store.db.execute("PRAGMA user_version") as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_wrong_key_and_corrupt_tokens_require_reconnection(store: Store) -> None:
    await store.save_account(account())
    wrong_key = Store(store.path, Fernet.generate_key().decode())
    await wrong_key.open()
    try:
        with pytest.raises(ReconnectRequired):
            await wrong_key.account(42)
    finally:
        await wrong_key.close()
    await store.db.execute("UPDATE accounts SET access_token='corrupt' WHERE discord_id=42")
    with pytest.raises(ReconnectRequired):
        await store.account(42)


async def test_oauth_state_is_hashed_at_rest_and_single_use(store: Store) -> None:
    state = await store.new_state(42)
    async with store.db.execute("SELECT * FROM oauth_states") as cursor:
        row = await cursor.fetchone()
    assert row["state_hash"] == hashlib.sha256(state.encode()).hexdigest()
    assert row["state_hash"] != state
    assert await store.state_owner(state) == 42
    assert await store.state_owner("forged-state") is None
    assert await store.consume_state(state) == 42
    assert await store.state_owner(state) is None
    assert await store.consume_state(state) is None


async def test_concurrent_callbacks_can_consume_state_only_once(store: Store) -> None:
    state = await store.new_state(42)
    results = await asyncio.gather(*(store.consume_state(state) for _ in range(20)))
    assert results.count(42) == 1
    assert results.count(None) == 19


async def test_state_consumption_is_atomic_across_database_connections(store: Store) -> None:
    state = await store.new_state(42)
    second = Store(store.path, Fernet.generate_key().decode())
    await second.open()
    try:
        results = await asyncio.gather(store.consume_state(state), second.consume_state(state))
        assert results.count(42) == 1
        assert results.count(None) == 1
    finally:
        await second.close()


async def test_new_link_invalidates_previous_link_for_same_user_only(store: Store) -> None:
    old = await store.new_state(42)
    other = await store.new_state(43)
    new = await store.new_state(42)
    assert new != old
    assert await store.state_owner(old) is None
    assert await store.consume_state(old) is None
    assert await store.state_owner(other) == 43
    assert await store.consume_state(new) == 42


async def test_unlink_removes_credentials_and_pending_authorization(store: Store) -> None:
    await store.save_account(account())
    state = await store.new_state(42)
    assert await store.unlink(42)
    assert await store.account(42) is None
    assert await store.consume_state(state) is None
    assert not await store.unlink(42)
    pending_for_unlinked_user = await store.new_state(99)
    assert not await store.unlink(99)
    assert await store.consume_state(pending_for_unlinked_user) is None


async def test_expired_states_are_rejected_and_removed_when_creating_a_link(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("radio1_bot.storage.time.time", lambda: 1_000)
    state = await store.new_state(42)
    cleanup_state = await store.new_state(43)
    monkeypatch.setattr("radio1_bot.storage.time.time", lambda: 1_599)
    assert await store.state_owner(state) == 42
    monkeypatch.setattr("radio1_bot.storage.time.time", lambda: 1_600)
    assert await store.state_owner(state) is None
    assert await store.consume_state(state) is None
    assert await store.state_owner(cleanup_state) is None
    await store.new_state(44)
    async with store.db.execute("SELECT discord_id FROM oauth_states") as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [44]


async def test_device_preference_and_reauthentication_flags_persist(store: Store) -> None:
    await store.save_account(account())
    await store.set_device(42, "device-2", "Phone")
    await store.mark_reauth(42)
    updated = await store.account(42)
    assert updated.device_id == "device-2"
    assert updated.device_name == "Phone"
    assert updated.needs_reauth
    await store.set_device(42, None, None)
    updated = await store.account(42)
    assert updated.device_id is None
    assert updated.device_name is None


async def test_account_lock_serializes_same_user_without_blocking_other_users(store: Store) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with store.account_lock(42):
            entered.set()
            await release.wait()
            order.append("first")

    async def second() -> None:
        async with store.account_lock(42):
            order.append("second")

    held = asyncio.create_task(first())
    await asyncio.wait_for(entered.wait(), 1)
    waiting = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not waiting.done()
    async with store.account_lock(43):
        order.append("other user")
    release.set()
    await asyncio.gather(held, waiting)
    assert order == ["other user", "first", "second"]


def test_database_access_requires_open_connection(tmp_path: Path) -> None:
    unopened = Store(tmp_path / "radio1.db", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="not open"):
        _ = unopened.db


def legacy_database(
    path: Path, rows: list[tuple[int, str | None, str | None, str | None, int | None]]
) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE users (discord_id INTEGER, spotify_id TEXT, "
            "access_token_encrypted TEXT, refresh_token_encrypted TEXT, token_expires_at INTEGER)"
        )
        db.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()


def encrypted_row(store: Store, discord_id: int = 42) -> tuple[int, str, str, str, int]:
    return (
        discord_id,
        "spotify-user",
        store.cipher.encrypt(b"old access").decode(),
        store.cipher.encrypt(b"old refresh").decode(),
        2_000_000_000,
    )


async def test_legacy_migration_imports_encrypted_accounts_without_modifying_source(
    store: Store, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    valid = encrypted_row(store)
    legacy_database(source, [valid, (43, None, None, None, None)])
    original = source.read_bytes()
    assert await migrate_legacy(source, store) == 1
    imported = await store.account(42)
    assert imported == Account(
        42, "spotify-user", "spotify-user", "old access", "old refresh", 2_000_000_000
    )
    assert source.read_bytes() == original
    assert await store.count() == 1
    assert await store.account(43) is None
    async with store.db.execute("SELECT access_token, refresh_token FROM accounts") as cursor:
        persisted = await cursor.fetchone()
    assert persisted["access_token"] != valid[2]
    assert persisted["refresh_token"] != valid[3]


async def test_legacy_migration_validates_every_token_before_any_write(
    store: Store, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    legacy_database(source, [encrypted_row(store), (43, "other-user", "bad", "bad", None)])
    with pytest.raises(ValueError, match="original key"):
        await migrate_legacy(source, store)
    assert await store.count() == 0


async def test_legacy_migration_rolls_back_the_whole_import_on_insert_failure(
    store: Store, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    legacy_database(source, [encrypted_row(store), encrypted_row(store)])
    with pytest.raises(sqlite3.IntegrityError):
        await migrate_legacy(source, store)
    assert await store.count() == 0
    await store.save_account(account())
    assert await store.count() == 1


async def test_legacy_migration_refuses_existing_accounts_and_identical_paths(
    store: Store, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    legacy_database(source, [encrypted_row(store)])
    with pytest.raises(ValueError, match="different files"):
        await migrate_legacy(store.path, store)
    await store.save_account(account())
    with pytest.raises(ValueError, match="won't overwrite"):
        await migrate_legacy(source, store)
    assert await store.account(42) == account()


async def test_legacy_migration_reports_missing_file_or_users_table(
    store: Store, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await migrate_legacy(tmp_path / "missing.db", store)
    invalid = tmp_path / "other.db"
    with closing(sqlite3.connect(invalid)) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="users table"):
        await migrate_legacy(invalid, store)


async def test_legacy_missing_expiry_imports_as_expired_for_refresh(
    store: Store, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    valid = encrypted_row(store)
    legacy_database(source, [(*valid[:4], None)])
    assert await migrate_legacy(source, store) == 1
    assert (await store.account(42)).expires_at == 0
