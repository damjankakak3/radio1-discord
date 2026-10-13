from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str = field(repr=False)
    spotify_client_id: str = field(repr=False)
    spotify_client_secret: str = field(repr=False)
    spotify_redirect_uri: str
    encryption_key: str = field(repr=False)
    database_path: Path = Path("data/radio1.db")
    web_host: str = "0.0.0.0"
    web_port: int = 5000
    log_level: str = "INFO"
    guild_id: int | None = None
    prefix_commands: bool = False
    prefix: str = "!"
    radio_poll: float = 15
    spotify_poll: float = 10
    buffer_tracks: int = 4
    max_queue: int = 40

    def __post_init__(self) -> None:
        missing = [
            name.upper()
            for name in (
                "discord_token",
                "spotify_client_id",
                "spotify_client_secret",
                "encryption_key",
            )
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"Missing settings: {', '.join(missing)}. See .env.example.")
        try:
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("ENCRYPTION_KEY must be a valid Fernet key.") from exc
        uri = urlsplit(self.spotify_redirect_uri)
        try:
            port = uri.port
        except ValueError as exc:
            raise ValueError("SPOTIFY_REDIRECT_URI contains an invalid port.") from exc
        try:
            loopback = ipaddress.ip_address(uri.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (
            not uri.hostname
            or uri.hostname == "localhost"
            or uri.scheme not in {"https", "http"}
            or (uri.scheme == "http" and not loopback)
            or uri.username
            or uri.password
            or uri.query
            or uri.fragment
            or not uri.path.startswith("/")
            or uri.path in {"/health", "/status", "/live", "/callback.css"}
            or port == 0
        ):
            raise ValueError(
                "SPOTIFY_REDIRECT_URI needs HTTPS, or HTTP with an explicit loopback IP."
            )
        if not 1 <= self.web_port <= 65535:
            raise ValueError("WEB_PORT must be between 1 and 65535.")
        if not 5 <= self.radio_poll <= 300 or not 2 <= self.spotify_poll <= 60:
            raise ValueError("Poll intervals must be 5-300s for radio and 2-60s for Spotify.")
        if not 1 <= self.buffer_tracks <= self.max_queue <= 100:
            raise ValueError("Require 1 <= BUFFER_TRACKS <= MAX_QUEUE_SIZE <= 100.")
        if not self.prefix or len(self.prefix) > 10:
            raise ValueError("BOT_PREFIX must contain 1 to 10 characters.")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

    @property
    def callback_path(self) -> str:
        return urlsplit(self.spotify_redirect_uri).path

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> Settings:
        # An explicit path avoids silently loading credentials from a parent project.
        load_dotenv(env_file, override=False)
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", ""),
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
            spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
            spotify_redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", ""),
            encryption_key=os.getenv("ENCRYPTION_KEY", ""),
            database_path=Path(os.getenv("DATABASE_PATH", "data/radio1.db")),
            web_host=os.getenv("WEB_HOST", "0.0.0.0"),
            web_port=int(os.getenv("WEB_PORT", "5000")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            guild_id=int(value) if (value := os.getenv("DISCORD_GUILD_ID")) else None,
            prefix_commands=os.getenv("ENABLE_PREFIX_COMMANDS", "false").lower() == "true",
            prefix=os.getenv("BOT_PREFIX", "!"),
            radio_poll=float(os.getenv("RADIO_POLL_SECONDS", "15")),
            spotify_poll=float(os.getenv("SPOTIFY_POLL_SECONDS", "10")),
            buffer_tracks=int(os.getenv("BUFFER_TRACKS", "4")),
            max_queue=int(os.getenv("MAX_QUEUE_SIZE", "40")),
        )
