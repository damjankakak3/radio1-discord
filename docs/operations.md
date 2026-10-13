# Operations

## Command-line checks

```bash
uv run radio1 check
uv run radio1 radio-check
uv run radio1
```

`check` validates the settings, opens the configured SQLite store, and tries to
decrypt the saved tokens. It does not log into Discord or call Spotify, so a
pass tells you the local configuration and storage are fine and nothing about
whether the external credentials or application permissions are valid. On a
fresh install it creates the database directory and schema.

`radio-check` fetches and parses the live station playlist and does nothing
else. Use it when you suspect the playlist page has changed. It needs network
access but no credentials, and it does not touch playback.

Running `radio1` without a subcommand starts the bot and its OAuth callback
server. Run one instance per database, because playback queues and session
ownership live in memory.

## Import accounts from a pre-2.0 database

Versions before 2.0 kept linked accounts in a `users` table, usually in a file
called `users.db`. If you ran one of those, you can carry the Spotify
connections over so listeners don't have to link again.

1. Stop the old bot. Back up its `.env` and database.
2. Create this bot's `.env` from `.env.example`. Reuse the old Discord and
   Spotify credentials, the redirect URI, and the old `ENCRYPTION_KEY`. Leave
   `DATABASE_PATH=data/radio1.db` alone. Do not point it at the old database.
3. Install dependencies, import the accounts, and validate the result:

```bash
uv sync --python 3.12 --locked
uv run radio1 migrate --from /path/to/users.db
uv run radio1 check
```

4. Start `uv run radio1`, look at `/radio status` and `/radio devices`, then
   start a fresh session with `/radio play`.

The import opens the old database read-only and validates every row before it
writes anything, all in one transaction. It refuses to run if the new store
already has accounts, so a second import cannot overwrite or merge them. Rows
without a linked Spotify account and refresh token are skipped. Display names
start out as the Spotify ID, and linking again fetches the current name. Device
preferences, pending login links, sessions and queues are not imported.

The import only works with the key that encrypted the old tokens. With any
other key it fails, and listeners have to use `/radio link` instead.

If you were used to a longer starting buffer, raise `BUFFER_TRACKS` and
restart. The default is four songs.

To go back, stop this bot and start the old one with its own configuration and
database. Accounts and device preferences changed here are not copied back.
Never run two bots against the same listening accounts at the same time.

## Docker

Fill in `.env` first. Keep `WEB_HOST=0.0.0.0` for Docker, use a public HTTPS
callback address if listeners are remote, and forward the callback through your
reverse proxy.

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

Compose stores the database in a named volume, `radio1-data`, mounted at
`/app/data`. It does not use the local `data/` directory. The Compose file
fixes the database path, bind host and port. The callback port is published on
`127.0.0.1:5000` of the Docker host, so a reverse proxy on that host can reach
it, while other containers need their own network setup. Keep the volume and
`.env` when you replace the container. To stop the bot:

```bash
docker compose down
```

Adding `-v` deletes the saved accounts, so leave it off unless that is what you
want. To import pre-2.0 accounts into the container's store, mount the old
database read-only for a one-off run:

```bash
docker compose run --rm --volume "/path/to/users.db:/legacy/users.db:ro" bot /app/.venv/bin/radio1 migrate --from /legacy/users.db
```

Do this before starting the service, with the old encryption key in `.env` and
no accounts in the Docker volume yet.

Replacing the container restarts the bot and clears active sessions. Listeners
can start again with `/radio play`. Their last Spotify song may keep playing,
because shutting the process down does not send a pause.

The callback server exposes `/live` for process liveness and `/health` (also
reachable as `/status`) for Discord readiness. Readiness returns HTTP 503 until
Discord is ready and HTTP 200 once connected. Neither endpoint tests Spotify or
the radio source. Compose uses `/health` for its health check.

For a rootless Podman host behind Traefik, see [deployment](deployment.md).

## Backups and privacy

The database stores Discord and Spotify IDs, a display name, encrypted access
and refresh tokens, and the saved device preference. OAuth states are hashed,
single use, and valid for 10 minutes. Generating a new link replaces the
listener's previous pending login. `/radio unlink` asks for confirmation, stops
the listener's session, and removes their account and pending states. It does
not revoke the authorization in Spotify's account settings.

Fernet encrypts the tokens. The rest of the account data is ordinary SQLite
data. Anyone who has both the database and `ENCRYPTION_KEY` can decrypt the
tokens, so keep backups and the key private. On Linux the store sets the
database file to owner-only permissions. Git ignores `.env`, databases, logs
and virtual environments.

Stop the bot before taking a plain file backup. SQLite runs in WAL mode, so
copying only the `.db` file while the bot is running can miss committed
changes. Back up the database together with its key, and restore them
together. Linked accounts survive restarts. Queues and sessions deliberately do
not, so a restart never takes over someone's Spotify without being asked.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing or invalid settings | Run `uv run radio1 check` from the repository root and compare your `.env` with `.env.example`. |
| Slash commands do not appear | Check that the invite included the `applications.commands` scope, and check the server's command permissions. Use `DISCORD_GUILD_ID` for a test server. Global registration takes time. |
| Prefix commands do nothing | You need both `ENABLE_PREFIX_COMMANDS=true` and Discord's Message Content Intent. |
| Spotify callback is unreachable | Check which device opened the browser. A loopback URL only reaches that device, and remote listeners need public HTTPS. Check the proxy path and port. |
| Spotify rejects the redirect | The URI in the dashboard and in `.env` must match exactly. Use an explicit loopback IP or HTTPS, not `localhost`. |
| Spotify refuses access | Check Premium, the app's development user list, the granted permissions, and which endpoints the app may use. Reconnect with `/radio link` if needed. |
| No available device | Open Spotify on the target device, play a song briefly, then refresh `/radio devices`. Restricted devices cannot be selected. |
| Selected device is offline | Open Spotify there or choose another device. The bot will not quietly move playback somewhere else. |
| A song cannot be matched | It may be missing from that listener's catalog or labeled differently. The bot skips uncertain matches and reports how many were unavailable. |
| Radio playlist is delayed | Run `uv run radio1 radio-check`. Stale data is labeled, and starting playback waits for fresh metadata. |
| Spotify is rate limited | Wait for the retry delay shown. Reduce API usage across all apps on the same Spotify developer account. |
| Controls have expired | Open `/radio status` again. Panels expire after 10 minutes, and old buttons stop working after a bot restart. |
| Spotify paused unexpectedly | Check whether you paused it in Spotify. Resume the same song there, or use Stop and start a new session. |
| Session stopped after a device or song change | This is intentional. Changing things in Spotify releases the session. Use `/radio play` when you want to follow again. |

Logs default to `INFO`. Set `LOG_LEVEL=DEBUG` while you investigate, then set
it back. Before you share logs, remove credentials, callback query strings,
personal IDs and account names. The automated tests cannot prove that Discord
login, OAuth or real Spotify playback work. For that you need a configured
application and a real listener.
