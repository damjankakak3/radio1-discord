from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from radio1_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        discord_token="very-private-discord-token",
        spotify_client_id="private-client-id",
        spotify_client_secret="very-private-client-secret",
        spotify_redirect_uri="https://radio.example.com/oauth/callback",
        encryption_key=Fernet.generate_key().decode(),
    )


ENV_NAMES = (
    "DISCORD_TOKEN",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
    "ENCRYPTION_KEY",
    "DATABASE_PATH",
    "WEB_HOST",
    "WEB_PORT",
    "LOG_LEVEL",
    "DISCORD_GUILD_ID",
    "ENABLE_PREFIX_COMMANDS",
    "BOT_PREFIX",
    "RADIO_POLL_SECONDS",
    "SPOTIFY_POLL_SECONDS",
    "BUFFER_TRACKS",
    "MAX_QUEUE_SIZE",
)


@pytest.fixture
def clean_environment(monkeypatch):
    # Record every key before dotenv writes directly to os.environ, so values
    # loaded inside a test cannot leak into another test's environment.
    for name in ENV_NAMES:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def write_env(path: Path, settings: Settings, **values: str) -> None:
    config = {
        "DISCORD_TOKEN": settings.discord_token,
        "SPOTIFY_CLIENT_ID": settings.spotify_client_id,
        "SPOTIFY_CLIENT_SECRET": settings.spotify_client_secret,
        "SPOTIFY_REDIRECT_URI": settings.spotify_redirect_uri,
        "ENCRYPTION_KEY": settings.encryption_key,
        **values,
    }
    path.write_text("\n".join(f"{key}={value}" for key, value in config.items()) + "\n")


@pytest.mark.parametrize(
    "redirect",
    [
        "",
        "https://",
        "relative/callback",
        "ftp://radio.example.com/callback",
        "http://radio.example.com/callback",
        "http://localhost:5000/callback",
        "https://localhost/callback",
        "http://192.168.1.20:5000/callback",
        "https://radio.example.com",
        "https://radio.example.com/callback?state=abc",
        "https://radio.example.com/callback#fragment",
        "https://user:password@radio.example.com/callback",
        "https://radio.example.com:invalid/callback",
        "https://radio.example.com:99999/callback",
    ],
)
def test_redirect_rejects_unsafe_or_malformed_urls(settings, redirect):
    with pytest.raises(ValueError):
        replace(settings, spotify_redirect_uri=redirect)


@pytest.mark.parametrize(
    "redirect",
    [
        "https://radio.example.com/oauth/callback",
        "https://radio.example.com:8443/oauth/callback",
        "http://127.0.0.1:5000/oauth/callback",
        "http://[::1]:5000/oauth/callback",
    ],
)
def test_redirect_accepts_https_and_explicit_loopback(settings, redirect):
    configured = replace(settings, spotify_redirect_uri=redirect)
    assert configured.spotify_redirect_uri == redirect
    assert configured.callback_path == "/oauth/callback"


def test_settings_repr_never_discloses_credentials(settings):
    rendered = repr(settings)
    for secret in (
        settings.discord_token,
        settings.spotify_client_id,
        settings.spotify_client_secret,
        settings.encryption_key,
    ):
        assert secret not in rendered
    assert "spotify_redirect_uri=" in rendered


@pytest.mark.parametrize(
    "field",
    [
        "discord_token",
        "spotify_client_id",
        "spotify_client_secret",
        "encryption_key",
    ],
)
def test_missing_required_setting_names_are_actionable_without_values(settings, field):
    with pytest.raises(ValueError, match=field.upper()) as error:
        replace(settings, **{field: ""})
    assert settings.discord_token not in str(error.value)
    assert settings.spotify_client_secret not in str(error.value)


@pytest.mark.parametrize("key", ["not-a-fernet-key", "", "a" * 32])
def test_encryption_key_must_be_valid_fernet(settings, key):
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        replace(settings, encryption_key=key)


@pytest.mark.parametrize(
    "changes",
    [
        {"web_port": 0},
        {"web_port": 65536},
        {"radio_poll": 4},
        {"radio_poll": 301},
        {"spotify_poll": 1},
        {"spotify_poll": 61},
        {"buffer_tracks": 0},
        {"buffer_tracks": 41},
        {"max_queue": 101},
        {"prefix": ""},
        {"prefix": "!" * 11},
        {"log_level": "TRACE"},
    ],
)
def test_operational_settings_reject_invalid_ranges(settings, changes):
    with pytest.raises(ValueError):
        replace(settings, **changes)


def test_from_env_loads_only_the_requested_file_and_preserves_process_overrides(
    settings,
    clean_environment,
    monkeypatch,
    tmp_path,
):
    requested = tmp_path / "runtime.env"
    write_env(requested, settings, WEB_PORT="5050", LOG_LEVEL="debug", DATABASE_PATH="state/bot.db")
    write_env(tmp_path / ".env", settings, WEB_PORT="6060")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "deployment-secret")
    loaded = Settings.from_env(requested)
    assert loaded.spotify_client_secret == "deployment-secret"
    assert loaded.web_port == 5050
    assert loaded.log_level == "DEBUG"
    assert loaded.database_path == Path("state/bot.db")


def test_from_env_does_not_discover_credentials_in_parent_folder(
    settings,
    clean_environment,
    monkeypatch,
    tmp_path,
):
    write_env(tmp_path / ".env", settings)
    project = tmp_path / "rewrite"
    project.mkdir()
    monkeypatch.chdir(project)
    with pytest.raises(ValueError, match="Missing settings"):
        Settings.from_env()


def test_from_env_defaults_do_not_enable_legacy_prefix_commands(
    settings,
    clean_environment,
    tmp_path,
):
    path = tmp_path / ".env"
    write_env(path, settings)
    loaded = Settings.from_env(path)
    assert not loaded.prefix_commands
    assert loaded.prefix == "!"
    assert loaded.guild_id is None
    assert loaded.radio_poll == 15 and loaded.spotify_poll == 10
    assert loaded.buffer_tracks == 4 and loaded.max_queue == 40


def test_from_env_parses_explicit_optional_settings(settings, clean_environment, tmp_path):
    path = tmp_path / ".env"
    write_env(
        path,
        settings,
        ENABLE_PREFIX_COMMANDS="TrUe",
        BOT_PREFIX="r!",
        DISCORD_GUILD_ID="123456789012345678",
        RADIO_POLL_SECONDS="20.5",
        SPOTIFY_POLL_SECONDS="6",
        BUFFER_TRACKS="3",
        MAX_QUEUE_SIZE="25",
    )
    loaded = Settings.from_env(path)
    assert loaded.prefix_commands and loaded.prefix == "r!"
    assert loaded.guild_id == 123456789012345678
    assert loaded.radio_poll == 20.5 and loaded.spotify_poll == 6
    assert loaded.buffer_tracks == 3 and loaded.max_queue == 25


def test_invalid_numeric_environment_fails_before_startup(
    settings,
    clean_environment,
    tmp_path,
):
    path = tmp_path / ".env"
    write_env(path, settings, WEB_PORT="not-a-port")
    with pytest.raises(ValueError):
        Settings.from_env(path)
