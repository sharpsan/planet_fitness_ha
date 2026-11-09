"""Exceptions raised by the Planet Fitness API client."""

from __future__ import annotations


class PlanetFitnessError(Exception):
    """Base error; also used for unexpected HTTP statuses."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PlanetFitnessConnectionError(PlanetFitnessError):
    """Network error or timeout."""


class PlanetFitnessAuthError(PlanetFitnessError):
    """Credentials were rejected; the user must sign in again."""


class PlanetFitnessRateLimitError(PlanetFitnessError):
    """HTTP 429, or a request held back by the pause after one (`sent` False)."""

    def __init__(self, retry_after: float | None = None, *, sent: bool = True) -> None:
        super().__init__("Rate limited by Planet Fitness", status=429)
        self.retry_after = retry_after
        self.sent = sent


class LoginError(PlanetFitnessError):
    """Sign-in failed: the email or code was rejected, or the sign-in page changed."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason  # translation key for the config flow error
