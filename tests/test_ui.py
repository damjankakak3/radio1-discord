import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from radio1_bot.commands import RadioCommands
from radio1_bot.errors import BotError
from radio1_bot.models import Account, Device, RadioSnapshot, RadioSong, Track
from radio1_bot.playback import Session
from radio1_bot.ui import ActionButton, ActionView, Message, RadioUI


@pytest.fixture
def ui():
    store, radio, spotify, engine = (MagicMock() for _ in range(4))
    store.account = AsyncMock(
        return_value=Account(
            42,
            "spotify-test",
            "Listener",
            "access-test",
            "refresh-test",
            int(time.time()) + 3600,
        )
    )
    radio.snapshot = AsyncMock(
        return_value=RadioSnapshot(
            (RadioSong("Artist", "Title", datetime.now(UTC), "10"),),
            datetime.now(UTC),
        )
    )
    engine.sessions, engine.notices = {}, {}
    engine.stop = AsyncMock(return_value="Radio playback stopped.")
    store.unlink = AsyncMock(return_value=True)
    spotify.devices = AsyncMock(
        return_value=[Device("desktop", "Desktop", "Computer", active=True)]
    )
    return RadioUI(store, radio, spotify, engine)


def interaction(user_id=42):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=MagicMock())),
        edit_original_response=AsyncMock(return_value=MagicMock()),
    )


async def test_personal_controls_reject_other_users(ui):
    panel = await ui.status(42)
    stranger = interaction(99)
    assert not await panel.view.interaction_check(stranger)
    stranger.response.send_message.assert_awaited_once()
    assert stranger.response.send_message.call_args.kwargs["ephemeral"]
    assert await panel.view.interaction_check(interaction(42))


async def test_public_song_card_opens_private_panel_without_disabling_shared_controls(ui):
    card = await ui.now()
    ui.action = AsyncMock(
        return_value=Message(content="Player", view=ActionView(ui, 42, [("stop", "Stop")]))
    )
    button = next(
        child
        for child in card.view.children
        if isinstance(child, ActionButton) and child.action == "play"
    )
    click = interaction()
    await button.callback(click)
    click.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert click.followup.send.call_args.kwargs["ephemeral"] is True
    click.edit_original_response.assert_not_awaited()
    assert not card.view.is_finished()


async def test_personal_buttons_edit_existing_panel(ui):
    panel = await ui.status(42)
    replacement = Message(content="Refreshed", view=ActionView(ui, 42, [("stop", "Stop")]))
    ui.action = AsyncMock(return_value=replacement)
    button = panel.view.children[0]
    click = interaction()
    await button.callback(click)
    click.edit_original_response.assert_awaited_once()
    click.followup.send.assert_not_awaited()
    assert panel.view.is_finished()
    assert replacement.view.message is click.edit_original_response.return_value


async def test_expired_panel_disables_controls(ui):
    panel = await ui.status(42)
    panel.view.message = MagicMock(edit=AsyncMock())
    await panel.view.on_timeout()
    assert all(child.disabled for child in panel.view.children)
    panel.view.message.edit.assert_awaited_once_with(view=panel.view)


async def test_disconnect_requires_confirmation_and_deletes_only_after_confirm(ui):
    await ui.action(42, "unlink")
    ui.engine.stop.assert_not_awaited()
    ui.store.unlink.assert_not_awaited()
    await ui.action(42, "unlink_confirm")
    ui.engine.stop.assert_awaited_once_with(42, pause=False)
    ui.store.unlink.assert_awaited_once_with(42)


async def test_queue_pages_respect_discord_limits_and_show_next_first(ui):
    session = Session(42, "desktop", "Desktop", "buffered")
    session.queue.extend(
        RadioSong("Artist @everyone", str(i) + "x" * 600, source_id=str(i)) for i in range(40)
    )
    ui.engine.sessions[42] = session
    first = ui.queue(42, 0)
    last = ui.queue(42, 999)
    assert first.embed.description.startswith("**1.**")
    assert "**10.**" in first.embed.description and "**11.**" not in first.embed.description
    assert len(first.embed.description) < 4096
    assert "**31.**" in last.embed.description and "Page 4/4" in last.embed.footer.text
    assert "@everyone" not in first.embed.description


async def test_device_select_menu_caps_options_and_excludes_restricted_devices(ui):
    ui.spotify.devices.return_value = [Device(str(i), f"Device {i}", "Computer") for i in range(30)]
    ui.spotify.devices.return_value.append(
        Device("restricted", "Restricted", "Speaker", restricted=True)
    )
    panel = await ui.devices(42)
    select = next(item for item in panel.view.children if isinstance(item, discord.ui.Select))
    assert len(select.options) == 25
    assert all(option.value != "restricted" for option in select.options)
    assert select.options[0].value == "auto"


async def test_now_card_needs_no_spotify_account_and_never_contains_tokens(ui):
    ui.store.account.return_value = None
    card = await ui.now()
    assert card.embed.title == "Title"
    ui.spotify.devices.assert_not_awaited()
    assert "access-test" not in str(card.embed.to_dict())
    assert "refresh-test" not in str(card.embed.to_dict())


async def test_player_card_uses_thumbnail_and_compact_controls(ui):
    session = Session(42, "desktop", "Desktop", "buffered")
    session.current = Track(
        "spotify:track:abc",
        "Title",
        ("Artist",),
        180000,
        image_url="https://i.scdn.co/image/test",
        url="https://open.spotify.com/track/abc",
    )
    ui.engine.sessions[42] = session
    panel = await ui.status(42)
    assert panel.embed.title == "Title"
    assert panel.embed.thumbnail.url == "https://i.scdn.co/image/test"
    assert len(panel.embed.fields) == 3
    assert len(panel.view.children) <= 5
    assert panel.view.owner_id == 42


async def test_private_prefix_error_stays_in_dms(ui):
    ui.action = AsyncMock(side_effect=BotError("Your private device is offline."))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=42, send=AsyncMock()),
        guild=MagicMock(),
        interaction=None,
        defer=AsyncMock(),
        send=AsyncMock(),
    )
    await RadioCommands(ui).run(ctx, "play")
    assert ctx.author.send.call_args.kwargs["content"] == "Your private device is offline."
    assert "private device" not in str(ctx.send.call_args)
