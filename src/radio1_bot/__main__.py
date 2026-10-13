from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx

from .bot import RadioBot
from .config import Settings
from .errors import BotError
from .migrate import migrate_legacy
from .radio import Radio
from .storage import Store


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "aiohttp.access", "discord"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def run(args: argparse.Namespace) -> None:
    if args.command == "radio-check":
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            snapshot = await Radio(http).snapshot()
            print(f"{snapshot.current.label} ({snapshot.current.kind})")
            print(f"{len(snapshot.music)} songs in history; stale={snapshot.stale}")
        return
    settings = Settings.from_env(args.env_file)
    configure_logging(settings.log_level)
    if args.command in {"check", "migrate"}:
        if args.command == "migrate" and args.source.resolve() == settings.database_path.resolve():
            raise ValueError("Source and destination databases must be different files.")
        store = Store(settings.database_path, settings.encryption_key)
        try:
            await store.open()
            if args.command == "migrate":
                count = await migrate_legacy(args.source, store)
                print(
                    f"Imported {count} account(s) into {store.path}. The source was left unchanged."
                )
            else:
                async with store.db.execute("SELECT discord_id FROM accounts") as cursor:
                    rows = list(await cursor.fetchall())
                for row in rows:
                    await store.account(row[0])
                print(
                    f"Configuration valid. Database ready; "
                    f"{len(rows)} account(s) decrypt correctly."
                )
                print(
                    "This is an offline check; "
                    "Discord and Spotify credentials haven't been verified."
                )
        finally:
            await store.close()
        return
    bot = RadioBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    # Restrict database journals and other runtime files as well as the main DB.
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Rádió 1 playlist follower for Discord and Spotify."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "check", help="Validate local settings and encrypted database without logging in."
    )
    migration = sub.add_parser("migrate", help="Import accounts from the original bot's database.")
    migration.add_argument("--from", dest="source", required=True, type=Path)
    sub.add_parser(
        "radio-check", help="Fetch and parse the real station playlist without credentials."
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, BotError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
