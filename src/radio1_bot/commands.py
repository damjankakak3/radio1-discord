from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, cast

import discord
from discord.ext import commands

from .errors import BotError
from .playback import Mode
from .ui import ActionView, Message, RadioUI

log = logging.getLogger(__name__)
# discord.py's bound Cog decorator generics currently infer Never with mypy.
# Keep the dynamic decorator boundary untyped; command bodies remain checked.
hybrid_group = cast(Callable[..., Any], commands.hybrid_group)


class RadioCommands(commands.Cog):
    def __init__(self, ui: RadioUI) -> None:
        self.ui = ui

    async def run(self, ctx: commands.Context, action: str, *, mode: Mode = "buffered") -> None:
        public = action in {"now", "help"}
        await ctx.defer(ephemeral=not public)
        try:
            message = await self.ui.action(ctx.author.id, action, mode=mode)
        except BotError as exc:
            message = Message(content=str(exc))
        if ctx.interaction or public or ctx.guild is None:
            sent = await ctx.send(**message.kwargs(), ephemeral=not public)
        else:
            try:
                sent = await ctx.author.send(**message.kwargs())
                await ctx.send("Check your DMs for the controls. You can also use /radio here.")
            except discord.Forbidden:
                await ctx.send("I couldn't send a DM. Use /radio for private controls here.")
                return
        if isinstance(message.view, ActionView):
            message.view.message = sent

    @hybrid_group(
        name="radio",
        fallback="help",
        invoke_without_command=True,
        description="Follow Rádió 1 on your own Spotify device.",
    )
    async def radio(self, ctx: commands.Context) -> None:
        await self.run(ctx, "help")

    @radio.command(name="link", description="Connect or reconnect your Spotify account.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def link(self, ctx: commands.Context) -> None:
        await self.run(ctx, "link")

    @radio.command(name="unlink", description="Disconnect Spotify and delete your saved account.")
    async def unlink(self, ctx: commands.Context) -> None:
        await self.run(ctx, "unlink")

    @radio.command(name="devices", description="Choose which Spotify device to use.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def devices(self, ctx: commands.Context) -> None:
        await self.run(ctx, "devices")

    @radio.command(name="now", description="See the station's current song or show.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def now(self, ctx: commands.Context) -> None:
        await self.run(ctx, "now")

    @radio.command(name="play", description="Start following the station on Spotify.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def play(
        self, ctx: commands.Context, mode: Literal["buffered", "follow"] = "buffered"
    ) -> None:
        """Buffered starts a few songs back; follow starts at the latest song."""
        await self.run(ctx, "play", mode=mode)

    @radio.command(name="stop", description="Stop your radio session and pause its song.")
    async def stop(self, ctx: commands.Context) -> None:
        await self.run(ctx, "stop")

    @radio.command(name="skip", description="Play the next song in your radio queue.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def skip(self, ctx: commands.Context) -> None:
        await self.run(ctx, "skip")

    @radio.command(name="queue", description="Show upcoming songs, next song first.")
    async def queue(self, ctx: commands.Context) -> None:
        await self.run(ctx, "queue")

    @radio.command(name="status", description="Open your player and account controls.")
    async def status(self, ctx: commands.Context) -> None:
        await self.run(ctx, "status")


def install_legacy(bot: commands.Bot, cog: RadioCommands) -> None:
    mapping = {
        "linkspotify": "link",
        "link": "link",
        "connect": "link",
        "unlink": "unlink",
        "devices": "devices",
        "now": "now",
        "play": "play",
        "p": "play",
        "start": "play",
        "stop": "stop",
        "pause": "stop",
        "queue": "queue",
        "q": "queue",
        "skip": "skip",
        "status": "status",
        "help": "help",
        "help_radio": "help",
    }

    async def legacy(ctx: commands.Context) -> None:
        assert ctx.command is not None
        await cog.run(ctx, mapping[ctx.command.name])

    for name in mapping:
        bot.add_command(commands.Command(legacy, name=name))
