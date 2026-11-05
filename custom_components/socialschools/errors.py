"""Exception hierarchy for the Social Schools integration (HA-free).

Messages must never contain secrets (passwords, tokens, cookies, codes) or
response bodies; they are safe to log and to show in the UI.
"""

from __future__ import annotations


class SocialSchoolsError(Exception):
    """Base class for all Social Schools errors."""


class CannotConnect(SocialSchoolsError):
    """Network error, timeout, DNS failure or HTTP 5xx: retry later."""


class InvalidAuth(SocialSchoolsError):
    """Credentials or tokens are not accepted: a new login is required."""


class UnsupportedLogin(SocialSchoolsError):
    """The login flow returned an unknown page, redirect or extra step."""


class ApiError(SocialSchoolsError):
    """Unexpected HTTP status (non-auth 4xx) or an invalid JSON body."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Initialise with a sanitised message and the HTTP status if known."""
        super().__init__(message)
        self.status = status


class RequestNotAllowed(SocialSchoolsError):
    """A request outside the read-only whitelist was attempted (programming error)."""
