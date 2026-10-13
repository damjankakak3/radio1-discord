class BotError(Exception):
    """An expected failure with a message safe to show to a listener."""


class NotLinked(BotError):
    def __init__(self) -> None:
        super().__init__("Connect your Spotify account with /radio link first.")


class ReconnectRequired(BotError):
    def __init__(self) -> None:
        super().__init__("Your Spotify connection expired. Reconnect with /radio link.")


class NoDevice(BotError):
    def __init__(self) -> None:
        super().__init__("Open Spotify on your device, then choose it with /radio devices.")


class SpotifyForbidden(BotError):
    def __init__(self) -> None:
        super().__init__(
            "Spotify refused this action. Playback needs Premium. "
            "If this is a development app, your account must also be added to its user list."
        )


class ServiceUnavailable(BotError):
    def __init__(self, service: str = "Spotify") -> None:
        super().__init__(f"{service} is unavailable right now. Try again shortly.")


class RateLimited(BotError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Spotify is limiting requests. Try again in {max(1, int(retry_after))}s.")
