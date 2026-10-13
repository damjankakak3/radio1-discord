from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from radio1_bot.migrate import migrate_legacy
from radio1_bot.models import Account
from radio1_bot.storage import Store

LegacyRow = tuple[int, str | None, str | None, str | None, int | None]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    value = Store(tmp_path / "new" / "radio1.db", Fernet.generate_key().decode())
    await value.open()
    try:
        yield value
    finally:
        await value.close()


def linked_row(
    cipher: Fernet, discord_id: int, *, expires: int | None = 2_000_000_000
) -> LegacyRow:
    return (
        discord_id,
        f"spotify-{discord_id}",
        cipher.encrypt(f"access-{discord_id}".encode()).decode(),
        cipher.encrypt(f"refresh-{discord_id}".encode()).decode(),
        expires,
    )


def write_legacy(path: Path, rows: list[LegacyRow]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                discord_id INTEGER PRIMARY KEY,
                spotify_id TEXT,
                access_token_encrypted TEXT,
                refresh_token_encrypted TEXT,
                token_expires_at INTEGER
            );
            CREATE TABLE auth_states (state TEXT PRIMARY KEY, discord_id INTEGER);
            INSERT INTO auth_states VALUES ('old-pending-link', 101);
            """
        )
        connection.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?)", rows)
    return path


async def test_import_preserves_account_identity_and_tokens_without_changing_source(
    store: Store, tmp_path: Path
) -> None:
    source = write_legacy(
        tmp_path / "old accounts.db",
        [linked_row(store.cipher, 101), linked_row(store.cipher, 202, expires=None)],
    )
    source.chmod(0o400)
    original_bytes = source.read_bytes()
    original_modified = source.stat().st_mtime_ns

    assert await migrate_legacy(source, store) == 2
    assert await store.count() == 2
    for discord_id, expires in [(101, 2_000_000_000), (202, 0)]:
        assert await store.account(discord_id) == Account(
            discord_id=discord_id,
            spotify_id=f"spotify-{discord_id}",
            display_name=f"spotify-{discord_id}",
            access_token=f"access-{discord_id}",
            refresh_token=f"refresh-{discord_id}",
            expires_at=expires,
        )
    async with store.db.execute("SELECT access_token, refresh_token FROM accounts") as cursor:
        encrypted_tokens = await cursor.fetchall()
    for row in encrypted_tokens:
        assert row["access_token"].startswith("gAAAA")
        assert row["refresh_token"].startswith("gAAAA")
    async with store.db.execute("SELECT count(*) FROM oauth_states") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert source.read_bytes() == original_bytes
    assert source.stat().st_mtime_ns == original_modified


@pytest.mark.parametrize("via_symlink", [False, True])
async def test_source_cannot_be_the_destination_even_through_an_alias(
    store: Store, tmp_path: Path, via_symlink: bool
) -> None:
    source = store.path
    if via_symlink:
        source = tmp_path / "legacy-alias.db"
        source.symlink_to(store.path)
    with pytest.raises(ValueError, match="must be different files"):
        await migrate_legacy(source, store)
    assert await store.count() == 0
    async with store.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
        assert {row[0] for row in await cursor.fetchall()} == {"accounts", "oauth_states"}


async def test_nonempty_destination_is_refused_without_overwriting_or_merging_accounts(
    store: Store, tmp_path: Path
) -> None:
    existing = Account(101, "current-id", "Current name", "current-access", "current-refresh", 99)
    await store.save_account(existing)
    state = await store.new_state(101)
    source = write_legacy(
        tmp_path / "users.db", [linked_row(store.cipher, 101), linked_row(store.cipher, 202)]
    )
    source_before = source.read_bytes()
    with pytest.raises(ValueError, match="destination already has accounts"):
        await migrate_legacy(source, store)
    assert await store.count() == 1
    assert await store.account(101) == existing
    assert await store.account(202) is None
    assert await store.state_owner(state) == 101
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("invalid_field", [2, 3])
async def test_wrong_key_in_later_row_prevents_a_partial_import(
    store: Store, tmp_path: Path, invalid_field: int
) -> None:
    first = linked_row(store.cipher, 101)
    second = list(linked_row(store.cipher, 202))
    second[invalid_field] = Fernet(Fernet.generate_key()).encrypt(b"wrong-key-token").decode()
    source = write_legacy(tmp_path / "users.db", [first, tuple(second)])
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError, match="Use the original key"):
        await migrate_legacy(source, store)
    assert await store.count() == 0
    assert await store.account(101) is None
    assert source.read_bytes() == original_bytes


async def test_unlinked_rows_are_skipped_without_trying_to_decrypt_their_tokens(
    store: Store, tmp_path: Path
) -> None:
    source = write_legacy(
        tmp_path / "users.db",
        [
            (1, None, "invalid-token", "invalid-token", None),
            (2, "", "invalid-token", "invalid-token", None),
            (3, "spotify-3", "invalid-token", None, None),
            (4, "spotify-4", "invalid-token", "", None),
            linked_row(store.cipher, 101),
        ],
    )
    assert await migrate_legacy(source, store) == 1
    assert await store.count() == 1
    for user_id in range(1, 5):
        assert await store.account(user_id) is None
    assert (await store.account(101)).spotify_id == "spotify-101"


async def test_missing_source_is_reported_without_creating_it(store: Store, tmp_path: Path) -> None:
    source = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="Legacy database does not exist"):
        await migrate_legacy(source, store)
    assert not source.exists()
    assert await store.count() == 0


async def test_database_without_legacy_users_table_is_refused(store: Store, tmp_path: Path) -> None:
    source = tmp_path / "unrelated.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep me')")
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError, match="legacy users table"):
        await migrate_legacy(source, store)
    assert await store.count() == 0
    assert source.read_bytes() == original_bytes


async def test_insert_failure_rolls_back_every_account_and_leaves_store_usable(
    store: Store, tmp_path: Path
) -> None:
    source = write_legacy(
        tmp_path / "users.db", [linked_row(store.cipher, 101), linked_row(store.cipher, 202)]
    )
    original_bytes = source.read_bytes()
    await store.db.execute(
        """CREATE TRIGGER fail_second_account BEFORE INSERT ON accounts
        WHEN NEW.discord_id = 202
        BEGIN SELECT RAISE(ABORT, 'simulated second-row failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated second-row failure"):
        await migrate_legacy(source, store)
    assert await store.count() == 0
    assert await store.account(101) is None
    assert source.read_bytes() == original_bytes

    await store.db.execute("DROP TRIGGER fail_second_account")
    assert await migrate_legacy(source, store) == 2
    assert await store.count() == 2


async def test_migration_with_only_unlinked_rows_is_a_successful_empty_import(
    store: Store, tmp_path: Path
) -> None:
    source = write_legacy(tmp_path / "users.db", [(101, None, None, None, None)])
    assert await migrate_legacy(source, store) == 0
    assert await store.count() == 0
