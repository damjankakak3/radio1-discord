# Podman and Traefik deployment

This is one way to run the bot on a small Linux server: rootless Podman with a
Quadlet unit, and Traefik in front of the callback server. It was written for
an SELinux host with Traefik 3.7 and its file provider. If you use Docker
Compose, the [Docker section](operations.md#docker) is shorter.

The examples use these placeholders. Replace them with your own values.

| Placeholder | Meaning |
| --- | --- |
| `radio.example.com` | Public hostname of the callback server. |
| `~/radio1` | Directory on the host that holds releases, `.env` and data. |
| `proxy.network` | Quadlet network unit that your Traefik container is attached to. For a plain Podman network, use its name without the `.network` suffix. |
| `~/traefik/dynamic` | Directory watched by Traefik's file provider. |
| `letsencrypt` | Name of your Traefik certificate resolver. |

The repository includes [the Quadlet unit](../deploy/radio1.container) and
[the Traefik route](../deploy/traefik-radio1.yml). Neither contains
credentials. The bot's port is not published on the host. Traefik reaches the
container over the shared network.

## Files on the host

| Path or setting | Purpose |
| --- | --- |
| `~/radio1/releases/<commit>` | Source checkout for each release. |
| `localhost/radio1:<commit>` | Local image for that release, built on the host. |
| `localhost/radio1:current` | Image the Quadlet unit runs. |
| `~/radio1/.env` | Private runtime settings, mode `0600`. |
| `~/radio1/data` | SQLite directory, writable by container UID and GID `10001`. |
| `~/.config/containers/systemd/radio1.container` | Quadlet source. It generates `radio1.service`. |
| `~/traefik/dynamic/radio1.yml` | Route loaded by Traefik's file watcher. |

`.env` needs `DISCORD_TOKEN`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
`ENCRYPTION_KEY` and `SPOTIFY_REDIRECT_URI=https://radio.example.com/callback`.
Register that exact URI in the Spotify application. [Setup](setup.md) covers
the Discord invite, Premium and Spotify's user list. Leave `DISCORD_GUILD_ID`
unset in production so the slash commands register globally.

The Quadlet unit sets `DATABASE_PATH=/app/data/radio1.db`, `WEB_HOST=0.0.0.0`
and `WEB_PORT=5000`. Environment values take precedence over `.env`, so those
three always win. The container needs outbound HTTPS and DNS to reach Discord,
Spotify and the station's playlist page.

## Build and validate a release

Clone the commit you want to run into its own directory and build it under
its own tag. Put the real commit SHA in place of `COMMIT_SHA`:

```bash
RADIO_RELEASE=COMMIT_SHA
git clone https://github.com/damjankakak3/radio1-discord.git ~/radio1/releases/$RADIO_RELEASE
cd ~/radio1/releases/$RADIO_RELEASE
git checkout "$RADIO_RELEASE"
podman build --file Dockerfile --tag "localhost/radio1:$RADIO_RELEASE" .
```

Prepare the data directory and lock down `.env`:

```bash
install -d -m 0700 ~/radio1/data
chmod 0600 ~/radio1/.env
podman unshare chown -R 10001:10001 ~/radio1/data
```

`podman unshare` applies the ownership inside Podman's user namespace, which is
where the container's UID 10001 lives. Leave `.env` owned by your own user,
because Podman reads that file on the host side. The data mount uses `:Z` to
give it a private SELinux label.

Validate the settings and the database before starting the service:

```bash
podman run --rm --network=none \
  --env-file ~/radio1/.env \
  --env DATABASE_PATH=/app/data/radio1.db \
  --env WEB_HOST=0.0.0.0 --env WEB_PORT=5000 \
  --volume ~/radio1/data:/app/data:Z \
  "localhost/radio1:$RADIO_RELEASE" /app/.venv/bin/radio1 check
```

This creates the schema if it is missing and checks token decryption, all
without network access. On later releases, stop the service before running
this command. Two containers cannot share a mount with a private SELinux label
at the same time.

## Route and start

The route sends the whole `radio.example.com` host to `http://radio1:5000`,
which keeps `/callback` and `/callback.css` intact. Edit the hostname, the
entry point and the certificate resolver in `deploy/traefik-radio1.yml` to
match your Traefik setup before you install it.

Two parts of that file exist for privacy. `observability.accessLogs: false`
keeps this route's OAuth query parameters out of Traefik's access log. Older
Traefik 3 releases do not have per-router observability options, so check the
[router reference](https://doc.traefik.io/traefik/reference/routing-configuration/http/routing/router/)
for your version. The bot also turns off access logging in its own callback
server.

The `radio1-privacy` middleware sets `Referrer-Policy: no-referrer` and
`X-Frame-Options: DENY`. The first one stops the browser from sending the
callback's query string as a referrer when it requests the stylesheet. If you
add your own header middlewares to the router, list `radio1-privacy` before
them. Traefik applies response headers in reverse order, so the first
middleware in the list has the final say. See the
[headers middleware reference](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/headers/).

Install the unit and the route, then start the service:

```bash
install -d ~/.config/containers/systemd
install -m 0644 deploy/radio1.container ~/.config/containers/systemd/radio1.container
install -m 0644 deploy/traefik-radio1.yml ~/traefik/dynamic/radio1.yml.new
chcon --reference="$HOME/traefik/dynamic" ~/traefik/dynamic/radio1.yml.new
mv ~/traefik/dynamic/radio1.yml.new ~/traefik/dynamic/radio1.yml
podman tag "localhost/radio1:$RADIO_RELEASE" localhost/radio1:current
systemctl --user daemon-reload
systemctl --user start radio1.service
```

The route goes in under a temporary name and is renamed into place, so Traefik
never reads a half-written file. `chcon --reference` copies the directory's
SELinux context onto the new file before the rename. Without it, a freshly
installed file can keep the `user_home_t` label, and Traefik cannot read it
when its dynamic directory is mounted with a private label. Skip that line on
hosts without SELinux.

The file provider picks up the new route without a Traefik restart. Quadlet
generates `radio1.service` during `daemon-reload`, and `WantedBy=default.target`
in the unit starts it at boot. Do not use `systemctl enable` or `disable` on
the generated unit. Change the Quadlet source instead. The service only keeps
running after you log out if lingering is enabled for your user. Check with
`loginctl show-user "$USER" --property=Linger` and turn it on with
`loginctl enable-linger`. See
[Podman's Quadlet documentation](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).

## Verify

```bash
systemctl --user is-active radio1.service
podman exec radio1 /app/.venv/bin/radio1 check
podman exec radio1 /app/.venv/bin/radio1 radio-check
curl --fail --silent --show-error https://radio.example.com/live
curl --fail --silent --show-error https://radio.example.com/health
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://radio.example.com/callback
curl --silent --show-error --head https://radio.example.com/callback
curl --fail --silent --show-error --output /dev/null https://radio.example.com/callback.css
```

`/live` answers as soon as the HTTP process is up. `/health` returns 503 while
Discord is still connecting and 200 once it is ready. A callback request
without a state must return 400, which shows the route is reachable and
rejects invalid logins. Its response headers should include
`Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. The stylesheet must
return 200.

None of this proves that the Spotify permissions are right. Finish with
`/radio link`, `/radio devices` and `/radio play` on a Premium test account
that is on the Spotify app's user list. Try Stop, and try taking over playback
in Spotify.

Read the service log with
`journalctl --user --unit=radio1.service --lines=50 --no-pager`. Do not paste
`.env`, the output of `podman inspect`, or real callback URLs into bug reports.

## Update or roll back

To update, build the new commit under its own tag and keep the previous tag.
Stop the service, back up the data directory together with its encryption key,
run the offline validation, and tag the new image as `current`. Then run
`systemctl --user daemon-reload` and `systemctl --user start radio1.service`.
If the Quadlet unit or the route changed, install those files again as well.
Retagging by itself does not replace a running container. Listeners start new
sessions after the restart.

To roll back, stop the service, tag the previous image as `current`, restore
the previous unit and route if they changed, and restore a database backup if
the newer release changed the schema. Reload the user manager and start the
service. Keep the old image tags, release checkouts and backups until you are
happy with the new release.

To take the bot offline for good:

```bash
systemctl --user stop radio1.service
mv ~/.config/containers/systemd/radio1.container ~/.config/containers/systemd/radio1.container.disabled
systemctl --user daemon-reload
rm ~/traefik/dynamic/radio1.yml
```

Removing the route matters, because otherwise Traefik keeps a router that
points at a stopped container. Your data, `.env`, images and backups stay
where they are.

One Discord application can only be served by one running bot. If you move to
a different implementation or host with the same Discord token, stop the old
instance before you start the new one.
