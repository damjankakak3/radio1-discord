from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from cryptography.fernet import InvalidToken

from .models import Account
from .storage import Store


def read_legacy(source: Path, store: Store) -> list[Account]:
    if source.resolve() == store.path.resolve():
        raise ValueError("Source and destination databases must be different files.")
    if not source.is_file():
        raise ValueError("Legacy database does not exist.")
    # Validate every row before writing anything, and never modify the source.
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        try:
            rows = legacy.execute("SELECT * FROM users").fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("Source does not contain the legacy users table.") from exc
    accounts: list[Account] = []
    for row in rows:
        if not row["spotify_id"] or not row["refresh_token_encrypted"]:
            continue
        try:
            access = store.cipher.decrypt((row["access_token_encrypted"] or "").encode()).decode()
            refresh = store.cipher.decrypt(row["refresh_token_encrypted"].encode()).decode()
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError(
                "ENCRYPTION_KEY does not decrypt the legacy tokens. Use the original key."
            ) from exc
        accounts.append(
            Account(
                discord_id=row["discord_id"],
                spotify_id=row["spotify_id"],
                display_name=row["spotify_id"],
                access_token=access,
                refresh_token=refresh,
                expires_at=row["token_expires_at"] or 0,
            )
        )
    return accounts


async def migrate_legacy(source: Path, store: Store) -> int:
    if await store.count():
        raise ValueError("The destination already has accounts. Migration won't overwrite them.")
    accounts = await asyncio.to_thread(read_legacy, source, store)
    # One transaction means an interrupted migration never leaves half an import.
    async with store._write_lock:
        await store.db.execute("BEGIN IMMEDIATE")
        try:
            for account in accounts:
                await store.db.execute(
                    """INSERT INTO accounts
                    (discord_id, spotify_id, display_name, access_token, refresh_token, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        account.discord_id,
                        account.spotify_id,
                        account.display_name,
                        store.cipher.encrypt(account.access_token.encode()).decode(),
                        store.cipher.encrypt(account.refresh_token.encode()).decode(),
                        account.expires_at,
                    ),
                )
            await store.db.execute("COMMIT")
        except BaseException:
            await store.db.execute("ROLLBACK")
            raise
    return len(accounts)
