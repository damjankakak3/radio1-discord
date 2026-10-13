# Development

## Layout

The package uses Python 3.12+, `discord.py` for Discord, `aiohttp` for the small
OAuth callback server, `httpx` for outbound HTTP, and SQLite for accounts.
Runtime components share one process and one event loop.

| Module | Responsibility |
| --- | --- |
| `__main__.py` | Command-line entry points and offline diagnostics. |
| `config.py` | Explicit environment loading and validated settings. |
| `bot.py` | Dependency wiring, Discord lifecycle, command registration, and shutdown. |
| `commands.py` | Slash commands and the optional prefix aliases. |
| `ui.py` | Song cards, private panels, buttons, device selection, and user-visible errors. |
| `web.py` | Spotify callback validation and the minimal result page. |
| `radio.py` | Station page parsing, content classification, Budapest timestamps, and shared cache. |
| `spotify.py` | OAuth, token refresh, API error handling, device resolution, and catalog matching. |
| `storage.py` | Encrypted account storage, device preferences, and one-use OAuth states. |
| `playback.py` | Per-listener queues, playback ownership, and session tasks. |
| `models.py` | Typed account, song, snapshot, device, and playback records. |
| `errors.py` | Expected errors with actionable user-facing wording. |
| `migrate.py` | Account import from a pre-2.0 database. |

## Account linking

`/radio link` creates a random state and returns a private authorization URL.
Only the state hash is saved, with the Discord ID and a 10-minute expiry. The
callback consumes that state atomically, exchanges the code, reads the Spotify
profile, and stores encrypted tokens. Expired, reused, or replaced links cannot
save an account. The callback route follows the configured redirect path.

Refresh, reconnect and unlink share a per-account lock. Without it, an older
token refresh could overwrite a newly linked account or bring back one the
user had removed. If Spotify rejects the refresh token, the listener has to
connect again. A Spotify profile without a `product` field does not fail the
login, because the playback response is where Spotify really checks the
permission.

## Radio data and matching

The metadata source is [MyOnlineRadio's Rádió 1 playlist](https://myonlineradio.hu/radio-1/most-szol).
`Radio` shares a cached snapshot across listeners and locks concurrent fetches.
The parser reads structured playlist rows, preserves source IDs and broadcast
timestamps, and identifies recognized shows and breaks. Local station times
use `Europe/Budapest`, including date boundaries. When the newest music entry
is old, the bot reports stale data and does not guess that an ad break is on.

Search sends artist and title queries and ranks the playable candidates by
normalized artist and title similarity. A named variant such as a remix counts
as part of the title. Matches with low confidence or a conflicting version are
rejected. Successful and failed searches are cached for different lengths of
time. The cache is bounded and kept per listener, because catalog availability
differs between accounts.

Spotify reads are retried on transient errors. Playback writes are not replayed
after a timeout, because a replay could restart a song. On HTTP 429 the shared
Spotify client backs off for the `Retry-After` period.

## Playback sessions

Each listener has at most one session, protected by a per-listener lock. Start
resolves the device, seeds a short music buffer, and starts the first match.
Pressing Play again returns the current session. It does not switch modes or
restart the song.

A session remembers playlist row identities and a watermark of source ID and
time. Each poll adds every new entry in broadcast order. The pending queue and
the remembered rows are both bounded. A song that cannot be matched is removed,
but after a temporary Spotify API error the pending song stays in the queue for
another try.

The session checks the Spotify device, track, play/pause state, and reported
progress before advancing, and it waits until the full song has ended. It
leaves a manual pause alone. A different track or device releases control. Stop
pauses only the session's own song on its own device. Selecting a device or
unlinking stops the session without pausing whatever the listener is playing
in Spotify.

Accounts and device preferences are stored. Sessions, queues, recent session
notices, HTTP caches and UI views live in memory, so after a restart each
listener has to press Play again. Run a single instance, because nothing
coordinates playback ownership between processes.

## Tests and checks

```bash
uv sync --python 3.12 --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The tests need no Discord or Spotify credentials. They use temporary
databases, sample radio HTML, and mocked HTTP and playback. They cover parsing
and chronology, token encryption and refresh, OAuth expiry and replay, catalog
matching, queue order, user takeovers, pauses, retry paths and private
controls. When you add a regression test, test behavior a listener could
observe or a failure the bot has to survive.

For a targeted investigation:

```bash
uv run pytest -q -k oauth
uv run radio1 check
uv run radio1 radio-check
```

`radio-check` is a smoke test against the live playlist page. `check` validates
the local configuration and token decryption. Neither one controls a Spotify
device. After the automated checks pass, try real OAuth and playback with a
Premium test account that is on the Spotify app's user list.

CI installs from the committed lockfile and runs formatting, linting, type
checks and tests on Python 3.12 and 3.14. If you change dependencies, update
`uv.lock` with `uv lock`, rerun the checks, and commit `pyproject.toml` and the
lockfile together.

Keep Discord copy in English, short, and focused on what the listener can do
next. Use native controls with text labels and enforce ownership on personal
views. The reasoning is in [design.md](design.md) and [product.md](product.md).
