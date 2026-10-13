from __future__ import annotations

import logging
from contextlib import AsyncExitStack

import discord
import httpx
from aiohttp import web
from discord.ext import commands

from .commands import RadioCommands, install_legacy
from .config import Settings
from .errors import BotError
from .playback import PlaybackEngine
from .radio import Radio
from .spotify import Spotify
from .storage import Store
from .ui import RadioUI
from .web import create_app

log = logging.getLogger(__name__)


class RadioBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = settings.prefix_commands
        super().__init__(
            command_prefix=settings.prefix,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(type=discord.ActivityType.listening, name="Rádió 1 · /radio"),
        )
        self.settings = settings
        self.store = Store(settings.database_path, settings.encryption_key)
        self.external_http: httpx.AsyncClient | None = None
        self.engine: PlaybackEngine | None = None
        self.web_runner: web.AppRunner | None = None
        self._shutdown_started = False
        self.resources = AsyncExitStack()

    async def setup_hook(self) -> None:
        self.resources.push_async_callback(self.store.close)
        await self.store.open()
        self.external_http = httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )
        self.resources.push_async_callback(self.external_http.aclose)
        spotify = Spotify(self.settings, self.store, self.external_http)
        radio = Radio(self.external_http, self.settings.radio_poll)
        self.engine = PlaybackEngine(self.settings, radio, spotify)
        self.resources.push_async_callback(self.engine.close)
        cog = RadioCommands(RadioUI(self.store, radio, spotify, self.engine))
        await self.add_cog(cog)
        if self.settings.prefix_commands:
            install_legacy(self, cog)
        app = create_app(self.settings, self.store, spotify, self.engine, self.is_ready)
        # No callback URLs or authorization codes in HTTP access logs.
        self.web_runner = web.AppRunner(app, access_log=None)
        self.resources.push_async_callback(self.web_runner.cleanup)
        await self.web_runner.setup()
        await web.TCPSite(self.web_runner, self.settings.web_host, self.settings.web_port).start()
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info(
            "Registered %s command group; OAuth server listening on port %s.",
            len(synced),
            self.settings.web_port,
        )

    async def on_ready(self) -> None:
        log.info("Connected to Discord as %s in %s servers.", self.user, len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        if self.settings.prefix_commands:
            await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        cause = getattr(error, "original", error)
        if isinstance(cause, BotError):
            message = str(cause)
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"Try again in {max(1, int(error.retry_after))}s."
        elif isinstance(error, commands.UserInputError):
            message = "That option wasn't recognized. Use /radio help for the commands."
        else:
            log.error("Command failed: %s", type(cause).__name__, exc_info=cause)
            message = "Couldn't complete that command. Try again."
        await ctx.send(message, ephemeral=True)

    async def close(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        # Stop network tasks before closing their HTTP client or database.
        try:
            await self.resources.aclose()
        finally:
            await super().close()
