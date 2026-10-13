# Rádió 1 for Discord

A Discord bot that follows Rádió 1's playlist and plays the same songs on your
own Spotify device. You link a Spotify account, pick a device, and control
playback with slash commands and buttons. The interface is in English.

Every listener has their own account, device preference and session. The audio
comes from Spotify, so the bot never joins a voice channel, and you don't get
the presenters, the ads or the station's exact timing.

This is a hobby project. It is not affiliated with Rádió 1, MyOnlineRadio,
Spotify or Discord.

## Run it locally

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), Python
3.12 or newer, a Discord application and a Spotify application. Playback needs
[Spotify Premium](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback).
[Setup](docs/setup.md) covers the application permissions and the callback
address.

```bash
git clone https://github.com/damjankakak3/radio1-discord.git
cd radio1-discord
uv sync --python 3.12 --locked
cp .env.example .env
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Put the generated key into `.env` as `ENCRYPTION_KEY`, fill in the other
values, then run:

```bash
uv run radio1 check
uv run radio1
```

In Discord, use `/radio link`, open Spotify on the device you want, then use
`/radio devices` and `/radio play`.

If you already have accounts in a pre-2.0 database, keep its encryption key
instead of generating a new one and follow the
[import instructions](docs/operations.md#import-accounts-from-a-pre-20-database).

## Commands

| Command | What it does |
| --- | --- |
| `/radio help` | Show the quick start and controls. |
| `/radio link` | Open a private Spotify connection link, valid for 10 minutes. |
| `/radio unlink` | Confirm removal of your saved account and device preference. |
| `/radio devices` | Select a device or choose the active Spotify device. |
| `/radio now` | Share the station's current reported song, show, or break. |
| `/radio play mode:buffered` | Start with the latest four music entries, in broadcast order. This is the default. |
| `/radio play mode:follow` | Start with the latest reported song, then follow new entries. |
| `/radio stop` | End your session and pause its song if the bot still owns playback. |
| `/radio skip` | Play the next available song in your radio queue. |
| `/radio queue` | View upcoming songs, next song first, with page controls. |
| `/radio status` | Open your account and player controls. |

Help and station cards are public. Account, device and playback controls are
visible only to the listener who opened them. Those panels check their owner
and expire after 10 minutes. `/radio status` opens a fresh one.

If you prefer message commands such as `!play` and `!now`, there is an
[optional prefix mode](docs/setup.md#optional-prefix-commands).

## How playback behaves

Buffered mode runs a few songs behind the station so the next songs are already
known. It starts with up to `BUFFER_TRACKS` recent music entries, oldest first.
Follow mode starts the latest song from its beginning and does not seek to the
radio's position. To change mode, stop the session and start a new one.

New songs go into a bounded queue in broadcast order. The bot leaves out
recognized shows and breaks, ignores repeated playlist rows, and skips songs it
cannot find on Spotify. Matching compares both artist and title instead of
taking the first search result, and it rejects a live, acoustic, karaoke or
remix version when the station played a different one. The station's metadata
and Spotify's catalog still disagree sometimes.

If you pause in Spotify, the session stays paused, and resuming the same song
lets it continue. If you change the song or the device in Spotify, the session
ends and playback is yours again. The bot waits for each song to finish, backs
off when an API returns errors, and tells you when the station data is stale.
Sessions and queues are gone after a bot restart. Linked accounts and device
preferences are kept.

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The tests use temporary databases and mocked services. They never log into
Discord or touch a real Spotify device. CI runs the same checks on Python 3.12
and 3.14. `uv run radio1 radio-check` is a separate command that fetches the
live playlist page without connecting to Discord or Spotify.

## Documentation

- [Setup and settings](docs/setup.md)
- [Running, Docker, backups and troubleshooting](docs/operations.md)
- [Podman and Traefik deployment](docs/deployment.md)
- [Architecture and development](docs/development.md)
- [Product notes](docs/product.md) and [interface guidelines](docs/design.md)
