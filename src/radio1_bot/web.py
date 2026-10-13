from __future__ import annotations

import html
import logging
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from .config import Settings
from .errors import BotError
from .playback import PlaybackEngine
from .spotify import Spotify
from .storage import Store

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("templates") / "callback.html"
STYLE = Path(__file__).with_name("templates") / "callback.css"


def page(title: str, message: str, *, success: bool = False, status: int = 200) -> web.Response:
    template = TEMPLATE.read_text(encoding="utf-8")
    template = template.replace("{{title}}", html.escape(title))
    template = template.replace("{{message}}", html.escape(message))
    template = template.replace("{{command}}", "/radio play" if success else "/radio link")
    return web.Response(
        text=template,
        content_type="text/html",
        status=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        },
    )


class OAuth:
    def __init__(self, store: Store, spotify: Spotify, engine: PlaybackEngine) -> None:
        self.store, self.spotify, self.engine = store, spotify, engine

    async def callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        if not state or len(state) > 256:
            return page("Link expired", "Request a fresh Spotify link in Discord.", status=400)
        user_id = await self.store.state_owner(state)
        if user_id is None:
            return page(
                "Link expired",
                "This link was used or expired. Request a fresh link in Discord.",
                status=400,
            )
        try:
            # Lock before consumption and hold it through the exchange. Unlink
            # deletes pending states under the same lock and cannot be undone by
            # a callback that was already waiting for its turn.
            async with self.store.account_lock(user_id):
                if await self.store.consume_state(state) != user_id:
                    return page(
                        "Link expired", "Request a fresh Spotify link in Discord.", status=400
                    )
                if request.query.get("error"):
                    return page(
                        "Spotify wasn't connected",
                        "You can try again whenever you're ready.",
                        status=400,
                    )
                code = request.query.get("code", "")
                if not code or len(code) > 4096:
                    return page(
                        "Spotify wasn't connected",
                        "The response was incomplete. Request a new link.",
                        status=400,
                    )
                account = await self.spotify.exchange(user_id, code)
                await self.store.save_account(account)
            await self.engine.stop(user_id, pause=False)
            return page(
                "Spotify connected",
                f"Signed in as {account.display_name}. "
                "Open Spotify on your device, then return to Discord to play.",
                success=True,
            )
        except BotError as exc:
            return page("Spotify wasn't connected", str(exc), status=400)
        except Exception:
            log.exception("OAuth callback failed without completing the connection.")
            return page(
                "Spotify wasn't connected",
                "Something went wrong. Request a new link and try again.",
                status=500,
            )


def create_app(
    settings: Settings,
    store: Store,
    spotify: Spotify,
    engine: PlaybackEngine,
    ready: Callable[[], bool],
) -> web.Application:
    app = web.Application(client_max_size=8192)
    oauth = OAuth(store, spotify, engine)

    async def health(request: web.Request) -> web.Response:
        connected = ready()
        return web.json_response(
            {"status": "ready" if connected else "starting"},
            status=200 if connected else 503,
            headers={"Cache-Control": "no-store"},
        )

    async def live(request: web.Request) -> web.Response:
        return web.json_response({"status": "alive"})

    async def css(request: web.Request) -> web.Response:
        return web.Response(
            text=STYLE.read_text(encoding="utf-8"),
            content_type="text/css",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app.router.add_get(settings.callback_path, oauth.callback)
    app.router.add_get("/health", health)
    app.router.add_get("/status", health)
    app.router.add_get("/live", live)
    app.router.add_get("/callback.css", css)
    return app
