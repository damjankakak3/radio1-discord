from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import discord

from .errors import BotError, ReconnectRequired
from .models import ContentKind, Track
from .playback import Mode, PlaybackEngine
from .radio import BUDAPEST, URL, Radio
from .spotify import Spotify
from .storage import Store

log = logging.getLogger(__name__)
GREEN = 0x4D9B71
AMBER = 0xB88645
MUTED = 0x79838D


def text(value: str, limit: int = 200) -> str:
    value = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    return value if len(value) <= limit else value[: limit - 1] + "…"


def track_card(track: Track, footer: str) -> discord.Embed:
    embed = discord.Embed(
        title=text(track.name, 256),
        description=text(", ".join(track.artists), 1000),
        color=GREEN,
        url=track.url,
    )
    if track.image_url:
        embed.set_thumbnail(url=track.image_url)
    embed.set_footer(text=footer)
    return embed


@dataclass(slots=True)
class Message:
    content: str | None = None
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None

    def kwargs(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "embed": self.embed,
            "view": self.view,
            "allowed_mentions": discord.AllowedMentions.none(),
        }


class RadioUI:
    def __init__(
        self, store: Store, radio: Radio, spotify: Spotify, engine: PlaybackEngine
    ) -> None:
        self.store, self.radio, self.spotify, self.engine = store, radio, spotify, engine

    async def action(
        self, user_id: int, action: str, *, mode: Mode = "buffered", page: int = 0
    ) -> Message:
        if action == "help":
            embed = discord.Embed(
                title="Rádió 1, on your Spotify",
                description="Connect your account, open Spotify on a device, then press Play.",
                color=GREEN,
            )
            embed.add_field(
                name="Get started",
                value="`/radio link` · `/radio devices` · `/radio play`",
                inline=False,
            )
            embed.add_field(
                name="Listening",
                value="`/radio now` · `/radio queue` · `/radio skip` · `/radio stop`",
                inline=False,
            )
            embed.add_field(
                name="Your account", value="`/radio status` · `/radio unlink`", inline=False
            )
            embed.set_footer(
                text="Personal Spotify playback · Premium required · No Discord voice audio"
            )
            return Message(embed=embed, view=ActionView(self, None, [("link", "Connect Spotify")]))
        if action == "now":
            return await self.now()
        if action == "link":
            url = self.spotify.auth_url(await self.store.new_state(user_id))
            embed = discord.Embed(
                title="Connect Spotify",
                color=GREEN,
                description=(
                    "Sign in to the account you want to listen with. "
                    "The bot controls playback on your Spotify devices."
                ),
            )
            embed.set_footer(text="This link expires in 10 minutes. Playback requires Premium.")
            view = discord.ui.View(timeout=600)
            view.add_item(discord.ui.Button(label="Continue to Spotify", url=url))
            return Message(embed=embed, view=view)
        if action == "unlink":
            return Message(
                embed=discord.Embed(
                    title="Disconnect Spotify?",
                    color=AMBER,
                    description=(
                        "This stops your radio session and deletes your saved account "
                        "and device preference."
                    ),
                ),
                view=ActionView(
                    self, user_id, [("unlink_confirm", "Disconnect"), ("status", "Cancel")]
                ),
            )
        if action == "unlink_confirm":
            await self.engine.stop(user_id, pause=False)
            deleted = await self.store.unlink(user_id)
            return Message(
                content="Spotify disconnected." if deleted else "No Spotify account is connected."
            )
        if action == "devices":
            return await self.devices(user_id)
        if action == "play":
            await self.engine.start(user_id, mode)
            return await self.status(user_id)
        if action == "skip":
            await self.engine.skip(user_id)
            return await self.status(user_id)
        if action == "stop":
            result = await self.engine.stop(user_id)
            return Message(content=result, view=ActionView(self, user_id, [("play", "Play again")]))
        if action == "queue":
            return self.queue(user_id, page)
        if action == "status":
            return await self.status(user_id)
        raise ValueError(f"Unknown UI action: {action}")

    async def now(self) -> Message:
        snapshot = await self.radio.snapshot()
        song = snapshot.current
        if song.kind == ContentKind.MUSIC:
            embed = discord.Embed(
                title=text(song.title, 256),
                description=text(song.artist, 1000),
                color=AMBER if snapshot.stale else GREEN,
                url="https://open.spotify.com/search/" + quote(f"{song.artist} {song.title}"),
            )
            label = "Last reported song" if snapshot.stale else "On Rádió 1"
        else:
            embed = discord.Embed(
                title="On Rádió 1", description=text(song.label, 1000), color=MUTED
            )
            label = "Radio show" if song.kind == ContentKind.SHOW else "Break"
        stamp = song.played_at.astimezone(BUDAPEST).strftime("%H:%M") if song.played_at else None
        embed.set_footer(text=f"{label}{' · ' + stamp + ' Budapest' if stamp else ''}")
        if snapshot.stale:
            embed.add_field(
                name="Playlist delayed",
                value="The station hasn't reported a fresh song yet.",
                inline=False,
            )
        view = ActionView(self, None, [("play", "Play on my Spotify"), ("now", "Refresh")])
        view.add_item(discord.ui.Button(label="Station playlist", url=URL))
        return Message(embed=embed, view=view)

    async def status(self, user_id: int) -> Message:
        account = await self.store.account(user_id)
        session = self.engine.sessions.get(user_id)
        if session and session.current:
            embed = track_card(session.current, f"Spotify · {session.state.capitalize()}")
        else:
            embed = discord.Embed(title="Your radio", color=GREEN)
        if account is None:
            embed.description = "Connect Spotify to start listening on your own device."
            return Message(
                embed=embed, view=ActionView(self, user_id, [("link", "Connect Spotify")])
            )
        if account.needs_reauth:
            raise ReconnectRequired()
        if session:
            embed.add_field(name="Device", value=text(session.device_name, 100), inline=True)
            embed.add_field(name="Queue", value=f"{len(session.queue)} songs", inline=True)
            embed.add_field(
                name="Mode",
                value="Buffered" if session.mode == "buffered" else "Follow",
                inline=True,
            )
            if session.current is None:
                embed.description = "Waiting for the station to report another song."
            if session.state == "paused":
                embed.add_field(
                    name="Paused in Spotify",
                    value="Resume there to keep following, or press Stop.",
                    inline=False,
                )
            if session.last_error:
                embed.add_field(
                    name="Waiting to retry", value=text(session.last_error, 500), inline=False
                )
            if session.skipped or session.dropped:
                embed.set_footer(
                    text=f"{session.skipped} unavailable · {session.dropped} old songs dropped"
                )
            view = ActionView(
                self,
                user_id,
                [("skip", "Skip"), ("queue", "Queue"), ("stop", "Stop"), ("status", "Refresh")],
            )
            if session.current and session.current.url:
                view.add_item(discord.ui.Button(label="Open Spotify", url=session.current.url))
        else:
            embed.description = f"Connected as **{text(account.display_name, 100)}**."
            embed.add_field(
                name="Device",
                value=text(account.device_name or "Active Spotify device", 100),
                inline=False,
            )
            notice = self.engine.notices.get(user_id)
            if notice:
                embed.add_field(name="Last session", value=text(notice, 500), inline=False)
            embed.set_footer(text="Buffered playback starts a few songs behind the station.")
            view = ActionView(
                self, user_id, [("play", "Play"), ("devices", "Devices"), ("unlink", "Disconnect")]
            )
        return Message(embed=embed, view=view)

    def queue(self, user_id: int, page: int = 0) -> Message:
        session = self.engine.sessions.get(user_id)
        if session is None:
            raise BotError("Start a session with /radio play first.")
        songs = tuple(session.queue)
        last_page = max(0, (len(songs) - 1) // 10)
        page = max(0, min(page, last_page))
        lines = [
            f"**{index + 1}.** {text(song.label, 250)}"
            for index, song in enumerate(songs[page * 10 : (page + 1) * 10], start=page * 10)
        ]
        embed = discord.Embed(
            title="Up next",
            description="\n".join(lines) or "Waiting for the station's next song.",
            color=GREEN,
        )
        embed.set_footer(
            text=f"{len(songs)} songs · Page {page + 1}/{last_page + 1} · Next song first"
        )
        view = ActionView(self, user_id, [("skip", "Skip"), ("status", "Player")])
        if last_page:
            view.add_item(
                ActionButton(self, "queue", "Previous", page=page - 1, disabled=page == 0)
            )
            view.add_item(
                ActionButton(self, "queue", "Next", page=page + 1, disabled=page == last_page)
            )
        return Message(embed=embed, view=view)

    async def devices(self, user_id: int) -> Message:
        devices = await self.spotify.devices(user_id)
        account = await self.store.account(user_id)
        available = [device for device in devices if not device.restricted]
        embed = discord.Embed(title="Choose a Spotify device", color=GREEN)
        if not available:
            embed.description = "Open Spotify on your phone or computer, then press Refresh."
            return Message(embed=embed, view=ActionView(self, user_id, [("devices", "Refresh")]))
        embed.description = "This device will be used the next time you press Play."
        embed.set_footer(text="Choosing a device stops the current radio session.")
        view = ActionView(self, user_id, [("devices", "Refresh"), ("status", "Player")])
        options = [
            discord.SelectOption(
                label="Use the active device",
                value="auto",
                default=not account or account.device_id is None,
            )
        ]
        # Discord permits 25 options. Put a saved device first so it cannot fall off.
        available.sort(
            key=lambda d: (not (account and account.device_id == d.id), not d.active, d.name)
        )
        options.extend(
            discord.SelectOption(
                label=device.name[:100],
                value=device.id,
                description=(f"{device.type}{' · Active' if device.active else ''}")[:100],
                default=bool(account and account.device_id == device.id),
            )
            for device in available[:24]
        )
        view.add_item(DeviceSelect(self, options))
        if len(available) > 24:
            embed.description += " Only the first 24 devices are shown."
        return Message(embed=embed, view=view)


class ActionView(discord.ui.View):
    def __init__(self, ui: RadioUI, owner_id: int | None, actions: list[tuple[str, str]]) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.message: discord.Message | discord.InteractionMessage | None = None
        for action, label in actions:
            self.add_item(ActionButton(ui, action, label))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "These are someone else's controls. Use /radio status.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        if isinstance(error, BotError):
            message = str(error)
        else:
            log.error("Discord control failed: %s", type(error).__name__, exc_info=error)
            message = "Couldn't complete that action. Try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class ActionButton(discord.ui.Button[ActionView]):
    def __init__(
        self, ui: RadioUI, action: str, label: str, *, page: int = 0, disabled: bool = False
    ) -> None:
        style = (
            discord.ButtonStyle.success
            if action in {"play", "link"}
            else discord.ButtonStyle.danger
            if action == "unlink_confirm"
            else discord.ButtonStyle.secondary
        )
        super().__init__(label=label, style=style, disabled=disabled)
        self.ui, self.action, self.page = ui, action, page

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        public_refresh = self.view.owner_id is None and self.action == "now"
        new_private = self.view.owner_id is None and not public_refresh
        await interaction.response.defer(ephemeral=new_private, thinking=new_private)
        message = await self.ui.action(interaction.user.id, self.action, page=self.page)
        sent: discord.Message | discord.InteractionMessage
        if new_private:
            sent = await interaction.followup.send(**message.kwargs(), ephemeral=True, wait=True)
        else:
            sent = await interaction.edit_original_response(**message.kwargs())
        if not new_private:
            self.view.stop()
        if isinstance(message.view, ActionView):
            message.view.message = sent


class DeviceSelect(discord.ui.Select[ActionView]):
    def __init__(self, ui: RadioUI, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="Select a device", options=options, row=1)
        self.ui = ui

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        selected = self.values[0]
        await self.ui.engine.stop(interaction.user.id, pause=False)
        if selected == "auto":
            await self.ui.store.set_device(interaction.user.id, None, None)
            name = "the active Spotify device"
        else:
            devices = await self.ui.spotify.devices(interaction.user.id)
            device = next(
                (item for item in devices if item.id == selected and not item.restricted), None
            )
            if device is None:
                raise BotError(
                    "That device is no longer available. Press Refresh and choose another."
                )
            await self.ui.store.set_device(interaction.user.id, device.id, device.name)
            name = device.name
        message = await self.ui.status(interaction.user.id)
        message.content = f"Selected {text(name, 100)}."
        sent = await interaction.edit_original_response(**message.kwargs())
        if self.view:
            self.view.stop()
        if isinstance(message.view, ActionView):
            message.view.message = sent
