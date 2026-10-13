from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from discord.http import HTTPClient

from radio1_bot.bot import RadioBot
from radio1_bot.config import Settings


def settings(tmp_path, **overrides):
    return replace(
        Settings(
            "discord-test",
            "client-test",
            "secret-test",
            "https://radio.example.com/callback",
            Fernet.generate_key().decode(),
            database_path=tmp_path / "accounts.db",
        ),
        **overrides,
    )


async def test_boot_registers_slash_group_preserves_discord_http_and_cleans_resources(tmp_path):
    bot = RadioBot(settings(tmp_path))
    site = MagicMock(start=AsyncMock())
    with (
        patch.object(bot.tree, "sync", AsyncMock(return_value=[])) as sync,
        patch("radio1_bot.bot.web.TCPSite", return_value=site) as site_factory,
    ):
        async with bot:
            await bot.setup_hook()
            assert isinstance(bot.http, HTTPClient)
            assert bot.external_http is not None
            assert not bot.intents.message_content
            group = bot.tree.get_command("radio")
            assert group is not None
            assert {command.name for command in group.commands} == {
                "help",
                "link",
                "unlink",
                "devices",
                "now",
                "play",
                "stop",
                "skip",
                "queue",
                "status",
            }
            mode = group.get_command("play").parameters[0]
            assert {choice.value for choice in mode.choices} == {"buffered", "follow"}
            await bot.on_ready()
            await bot.on_ready()
            sync.assert_awaited_once()
            site_factory.assert_called_once()
        assert bot.external_http.is_closed
        assert bot.store._db is None
        assert bot.engine._closed


async def test_prefix_commands_are_opt_in_and_forward_to_same_ui(tmp_path):
    bot = RadioBot(settings(tmp_path, prefix_commands=True, guild_id=123))
    site = MagicMock(start=AsyncMock())
    with (
        patch.object(bot.tree, "sync", AsyncMock(return_value=[])) as sync,
        patch("radio1_bot.bot.web.TCPSite", return_value=site),
    ):
        async with bot:
            await bot.setup_hook()
            assert bot.intents.message_content
            assert bot.get_command("linkspotify") is not None
            assert bot.get_command("play") is not None
            assert bot.get_command("help_radio") is not None
            assert sync.call_args.kwargs["guild"].id == 123


async def test_partial_startup_failure_still_closes_store_and_http(tmp_path):
    bot = RadioBot(settings(tmp_path))
    site = MagicMock(start=AsyncMock(side_effect=OSError("address already in use")))
    with patch("radio1_bot.bot.web.TCPSite", return_value=site):
        try:
            async with bot:
                await bot.setup_hook()
        except OSError:
            pass
    assert bot.store._db is None
    assert bot.external_http.is_closed


async def test_disabled_prefix_mode_does_not_process_messages(tmp_path):
    bot = RadioBot(settings(tmp_path))
    with patch.object(bot, "process_commands", AsyncMock()) as process:
        await bot.on_message(MagicMock())
        process.assert_not_awaited()
    await bot.close()
