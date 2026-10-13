# Setup

Run all commands from the repository root. `uv sync --python 3.12 --locked`
installs the application and its development tools from `uv.lock`. Python 3.12
is the minimum version.

## Discord application

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy its bot token into `DISCORD_TOKEN` in `.env`.
3. Invite the application with OAuth scopes `bot` and `applications.commands`.
4. Grant View Channels, Send Messages, Embed Links and Read Message History
   in the channels where you use it.

Slash commands need no privileged gateway intents. Leave Message Content Intent
off unless you enable prefix commands. The bot registers its command tree when
it starts.

During development, set `DISCORD_GUILD_ID` to one server ID so the commands
show up there immediately. Leave it blank for global commands, which can take a
while to appear in Discord. Restart the bot after changing this setting. To
restrict who can use the commands, use Discord's own server permissions.

## Spotify application

Create a Web API application in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`, and register the
same redirect URI in the dashboard and `SPOTIFY_REDIRECT_URI`.

Listeners need Premium for playback. While the Spotify app is in development
mode, its owner needs Premium too, and you have to add each listener under the
app's user management settings. Development mode allows up to five
authenticated users, and a listener who is not on that list can log in but
still gets no API access. See
[Spotify's quota mode documentation](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).

Since Spotify's quota update of July 23, 2026, a developer can have up to 25
development Client IDs, and all of that developer's apps share one API quota.
Creating another app does not give you a second quota budget. See the
[quota update post](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates).

The bot requests `user-read-private`, `user-read-playback-state` and
`user-modify-playback-state`. It uses the authorization code flow and refreshes
tokens when needed. Spotify sometimes asks a listener to connect again, and
`/radio link` creates a fresh connection.

### Callback address

For a browser running on the same computer as the bot:

```env
SPOTIFY_REDIRECT_URI=http://127.0.0.1:5000/callback
WEB_PORT=5000
```

`127.0.0.1` is whatever computer or phone runs the browser. A listener who
opens that link on another device cannot reach the bot's loopback server. For
remote listeners, use a public HTTPS address behind a reverse proxy:

```env
SPOTIFY_REDIRECT_URI=https://radio.example.com/callback
WEB_HOST=127.0.0.1
WEB_PORT=5000
```

Forward that HTTPS route to the callback server on port 5000, and register the
same public address in Spotify. The bot serves whatever path is in
`SPOTIFY_REDIRECT_URI`, so a custom path such as `/spotify/callback` works as
long as the proxy preserves it. Forward `/callback.css` as well, because the
result page loads its stylesheet from there. `WEB_HOST` is a bind address, not
a public hostname. Inside Docker, use `0.0.0.0` so the published port can reach
the container.

Spotify requires HTTPS for everything except explicit loopback IP addresses. It
does not accept `localhost`. See [redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).

## Settings

Copy `.env.example` to `.env` for a fresh install. Environment variables that
are already set take precedence over `.env`. The bot loads `.env` only from its
current directory and does not search parent folders for one.

To use another configuration file, pass the option before the subcommand:

```bash
uv run radio1 --env-file .env.staging check
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Required | Discord bot credentials. |
| `SPOTIFY_CLIENT_ID` | Required | Spotify application ID. |
| `SPOTIFY_CLIENT_SECRET` | Required | Spotify application secret. |
| `SPOTIFY_REDIRECT_URI` | Required | Registered callback URL; the example uses `http://127.0.0.1:5000/callback`. |
| `ENCRYPTION_KEY` | Required | Fernet key for saved Spotify tokens. Preserve it when moving or restoring a database. |
| `DATABASE_PATH` | `data/radio1.db` | SQLite file; relative paths are relative to the working directory. |
| `WEB_HOST` | `0.0.0.0` | Callback server bind address. |
| `WEB_PORT` | `5000` | Callback server port, 1 to 65535. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `DISCORD_GUILD_ID` | Empty | Optional server ID for command registration. |
| `ENABLE_PREFIX_COMMANDS` | `false` | Enable `!` message commands. |
| `BOT_PREFIX` | `!` | Prefix used in prefix mode, 1 to 10 characters. |
| `RADIO_POLL_SECONDS` | `15` | Shared radio metadata cache interval, 5 to 300 seconds. |
| `SPOTIFY_POLL_SECONDS` | `10` | Playback check interval per active session, 2 to 60 seconds. |
| `BUFFER_TRACKS` | `4` | Music entries seeded into buffered sessions. |
| `MAX_QUEUE_SIZE` | `40` | Maximum pending songs per listener. |

The queue settings must satisfy `1 <= BUFFER_TRACKS <= MAX_QUEUE_SIZE <= 100`.
When a queue fills up, the bot drops the oldest pending songs so the listener
stays reasonably close to the station. A larger buffer means a longer delay
behind the radio. A lower polling interval means more Spotify API calls for
every active listener.

Generate a key for a fresh database with:

```bash
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store the output in `.env` as `ENCRYPTION_KEY`. Keep `.env` private and back it
up somewhere other than the repository. Never generate a new key just to make
a configuration error go away, because only the existing key can decrypt the
saved tokens.

## Optional prefix commands

Set `ENABLE_PREFIX_COMMANDS=true`, enable Message Content Intent in the Discord
Developer Portal, and restart. The aliases are
`!linkspotify`, `!link`, `!connect`, `!unlink`, `!devices`, `!now`, `!play`,
`!p`, `!start`, `!stop`, `!pause`, `!queue`, `!q`, `!skip`, `!status`,
`!help`, and `!help_radio`. `!radio` exposes the command group.

When someone uses a prefix command in a server, the bot sends account and
player responses by DM. A listener who has DMs turned off should use slash
commands, which answer privately in the channel. The short aliases such as
`!play` always start buffered mode. To pick follow mode, use `/radio play` or
`!radio play follow`.
