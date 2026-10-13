from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from .errors import ReconnectRequired
from .models import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    discord_id INTEGER PRIMARY KEY,
    spotify_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    device_id TEXT,
    device_name TEXT,
    needs_reauth INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    discord_id INTEGER NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL
);
PRAGMA user_version = 1;
"""


class Store:
    def __init__(self, path: Path, encryption_key: str) -> None:
        self.path = path
        self.cipher = Fernet(encryption_key.encode())
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._account_locks: dict[int, asyncio.Lock] = {}

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not open.")
        return self._db

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(SCHEMA)
        self.path.chmod(0o600)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def account_lock(self, discord_id: int) -> AsyncIterator[None]:
        # Refresh, reconnect, and unlink share this lock so stale requests cannot
        # overwrite freshly linked credentials or resurrect a deleted account.
        lock = self._account_locks.setdefault(discord_id, asyncio.Lock())
        async with lock:
            yield

    async def account(self, discord_id: int) -> Account | None:
        async with self.db.execute(
            "SELECT * FROM accounts WHERE discord_id=?", (discord_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return Account(
                discord_id=row["discord_id"],
                spotify_id=row["spotify_id"],
                display_name=row["display_name"],
                access_token=self.cipher.decrypt(row["access_token"].encode()).decode(),
                refresh_token=self.cipher.decrypt(row["refresh_token"].encode()).decode(),
                expires_at=row["expires_at"],
                device_id=row["device_id"],
                device_name=row["device_name"],
                needs_reauth=bool(row["needs_reauth"]),
            )
        except InvalidToken as exc:
            raise ReconnectRequired() from exc

    async def save_account(self, account: Account) -> None:
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO accounts
                (discord_id, spotify_id, display_name, access_token, refresh_token, expires_at,
                 device_id, device_name, needs_reauth) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                spotify_id=excluded.spotify_id, display_name=excluded.display_name,
                access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at, device_id=excluded.device_id,
                device_name=excluded.device_name, needs_reauth=excluded.needs_reauth""",
                (
                    account.discord_id,
                    account.spotify_id,
                    account.display_name,
                    self.cipher.encrypt(account.access_token.encode()).decode(),
                    self.cipher.encrypt(account.refresh_token.encode()).decode(),
                    account.expires_at,
                    account.device_id,
                    account.device_name,
                    int(account.needs_reauth),
                ),
            )

    async def mark_reauth(self, discord_id: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "UPDATE accounts SET needs_reauth=1 WHERE discord_id=?", (discord_id,)
            )

    async def set_device(self, discord_id: int, device_id: str | None, name: str | None) -> None:
        async with self.account_lock(discord_id), self._write_lock:
            await self.db.execute(
                "UPDATE accounts SET device_id=?, device_name=? WHERE discord_id=?",
                (device_id, name, discord_id),
            )

    async def unlink(self, discord_id: int) -> bool:
        async with self.account_lock(discord_id), self._write_lock:
            async with self.db.execute(
                "DELETE FROM accounts WHERE discord_id=?", (discord_id,)
            ) as cursor:
                deleted = cursor.rowcount > 0
            await self.db.execute("DELETE FROM oauth_states WHERE discord_id=?", (discord_id,))
        return deleted

    async def new_state(self, discord_id: int) -> str:
        state = secrets.token_urlsafe(32)
        digest = hashlib.sha256(state.encode()).hexdigest()
        async with self.account_lock(discord_id), self._write_lock:
            await self.db.execute(
                "DELETE FROM oauth_states WHERE expires_at<=?", (int(time.time()),)
            )
            await self.db.execute(
                "INSERT OR REPLACE INTO oauth_states VALUES (?, ?, ?)",
                (digest, discord_id, int(time.time()) + 600),
            )
        return state

    async def state_owner(self, state: str) -> int | None:
        digest = hashlib.sha256(state.encode()).hexdigest()
        async with self.db.execute(
            "SELECT discord_id FROM oauth_states WHERE state_hash=? AND expires_at>?",
            (digest, int(time.time())),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def consume_state(self, state: str) -> int | None:
        # DELETE RETURNING is one atomic statement, even across multiple callbacks.
        digest = hashlib.sha256(state.encode()).hexdigest()
        async with self._write_lock:
            async with self.db.execute(
                "DELETE FROM oauth_states WHERE state_hash=? RETURNING discord_id, expires_at",
                (digest,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or row["expires_at"] <= int(time.time()):
            return None
        return int(row["discord_id"])

    async def count(self) -> int:
        async with self.db.execute("SELECT count(*) FROM accounts") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
