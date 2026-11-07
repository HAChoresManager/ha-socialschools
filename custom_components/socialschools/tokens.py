"""Token endpoint access and token lifecycle for the Social Schools integration (HA-free).

Only two calls exist: the one-off authorization code exchange and the refresh
grant, both ``POST {issuer}/connect/token``.  :class:`TokenManager` keeps the
current token pair, renews the access token shortly before it expires and
reports a rotated refresh token to its owner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import inspect
import logging
import time
from typing import Any

import aiohttp

from .const import (
    DEFAULT_EXPIRES_IN,
    MAX_EXPIRES_IN,
    MAX_TOKEN_BODY_BYTES,
    OIDC_CLIENT_ID,
    OIDC_ISSUER,
    OIDC_REDIRECT_URI,
    OIDC_TOKEN_PATH,
    REQUEST_TIMEOUT,
    TOKEN_REFRESH_LEEWAY,
    USER_AGENT,
)
from .errors import ApiError, CannotConnect, InvalidAuth
from .payload import async_read_limited, decode_json

_LOGGER = logging.getLogger(__name__)

RefreshTokenCallback = Callable[[str], Awaitable[None] | None]
"""Called with a rotated refresh token; a returned awaitable is awaited."""

_EXCHANGE_AUTH_STATUSES = frozenset({400, 401, 403})
_REFRESH_AUTH_STATUSES = frozenset({400, 401})


@dataclass(frozen=True)
class TokenResponse:
    """Validated subset of a token endpoint response; ``repr()`` never shows the tokens."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int  # seconds; DEFAULT_EXPIRES_IN when missing or invalid, at most MAX_EXPIRES_IN


def _is_token_string(value: Any) -> bool:
    """Return True for a non-empty str without control or whitespace characters."""
    return (
        isinstance(value, str)
        and bool(value)
        and not any(ord(char) <= 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)
    )


def _parse_expires_in(value: Any) -> int:
    """Return ``value`` as seconds between 1 and MAX_EXPIRES_IN, else DEFAULT_EXPIRES_IN."""
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_EXPIRES_IN
    return min(seconds, MAX_EXPIRES_IN) if seconds > 0 else DEFAULT_EXPIRES_IN


def _mark_outcome_retrieved(task: asyncio.Task[Any]) -> None:
    """Retrieve the exception of a finished task so that the event loop never reports it.

    A refresh whose caller was cancelled has nobody left to await it; its failure is
    simply the next caller's problem (that caller starts a fresh refresh).
    """
    if not task.cancelled():
        task.exception()


def _parse_token_payload(payload: Any, *, what: str) -> TokenResponse:
    """Validate a decoded token endpoint body without echoing any of it."""
    if not isinstance(payload, dict):
        raise ApiError(f"{what}: unexpected JSON shape", 200)
    if not _is_token_string(payload.get("access_token")):
        raise ApiError(f"{what}: missing or invalid access token", 200)
    refresh_token = payload.get("refresh_token")
    if "refresh_token" in payload and not _is_token_string(refresh_token):
        raise ApiError(f"{what}: invalid refresh token", 200)
    return TokenResponse(
        access_token=payload["access_token"],
        refresh_token=refresh_token,
        expires_in=_parse_expires_in(payload.get("expires_in")),
    )


async def _async_token_request(
    session: aiohttp.ClientSession,
    form: dict[str, str],
    *,
    what: str,
    issuer: str,
    user_agent: str,
    request_timeout: float,
    auth_statuses: frozenset[int],
) -> TokenResponse:
    """POST ``form`` to the token endpoint and return the validated response."""
    url = f"{issuer.rstrip('/')}{OIDC_TOKEN_PATH}"
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    try:
        async with session.post(
            url,
            data=form,
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=request_timeout),
        ) as response:
            status = response.status
            body = (
                await async_read_limited(response, MAX_TOKEN_BODY_BYTES) if status == 200 else b""
            )
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CannotConnect(f"{what}: {type(err).__name__}") from err
    _LOGGER.debug("%s: HTTP %s", what, status)
    if status in auth_statuses:
        raise InvalidAuth(f"{what}: HTTP {status}")
    if status >= 500:
        raise CannotConnect(f"{what}: HTTP {status}")
    if status != 200:
        raise ApiError(f"{what}: HTTP {status}", status)
    if body is None:
        raise ApiError(f"{what}: response body too large", status)
    payload = decode_json(body, what=what, status=status, max_bytes=MAX_TOKEN_BODY_BYTES)
    return _parse_token_payload(payload, what=what)


async def async_exchange_code(
    session: aiohttp.ClientSession,
    code: str,
    code_verifier: str,
    *,
    issuer: str = OIDC_ISSUER,
    client_id: str = OIDC_CLIENT_ID,
    redirect_uri: str = OIDC_REDIRECT_URI,
    user_agent: str = USER_AGENT,
    timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109 - name fixed by the design contract
) -> TokenResponse:
    """Exchange a PKCE authorization code for tokens; a refresh token is required.

    Raises InvalidAuth on 400/401/403, CannotConnect on network errors, timeouts
    and 5xx, ApiError on other statuses, invalid JSON or a missing refresh token.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    response = await _async_token_request(
        session,
        form,
        what="code exchange",
        issuer=issuer,
        user_agent=user_agent,
        request_timeout=timeout,
        auth_statuses=_EXCHANGE_AUTH_STATUSES,
    )
    if response.refresh_token is None:
        raise ApiError("code exchange: no refresh token in the response", 200)
    return response


async def async_refresh_tokens(
    session: aiohttp.ClientSession,
    refresh_token: str,
    *,
    issuer: str = OIDC_ISSUER,
    client_id: str = OIDC_CLIENT_ID,
    user_agent: str = USER_AGENT,
    timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109 - name fixed by the design contract
) -> TokenResponse:
    """Use the refresh grant to obtain a new access token (and maybe a rotated refresh token).

    Raises InvalidAuth on 400/401, CannotConnect on network errors, timeouts and
    5xx, ApiError on other statuses or invalid JSON.
    """
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    return await _async_token_request(
        session,
        form,
        what="token refresh",
        issuer=issuer,
        user_agent=user_agent,
        request_timeout=timeout,
        auth_statuses=_REFRESH_AUTH_STATUSES,
    )


class TokenManager:
    """Hold the token pair of one account and renew the access token on demand."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        client_id: str,
        refresh_token: str,
        access_token: str | None = None,
        expires_at: float | None = None,
        issuer: str = OIDC_ISSUER,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT,
        on_refresh_token_update: RefreshTokenCallback | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Store the token pair; ``expires_at`` is on the ``clock`` time scale.

        Without ``clock`` the manager reads ``time.monotonic()`` at every use (looked up
        on each call, so a patched clock is honoured).
        """
        self._session = session
        self._client_id = client_id
        self._refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = expires_at
        self._issuer = issuer
        self._user_agent = user_agent
        self._timeout = timeout
        self._on_refresh_token_update = on_refresh_token_update
        self._clock = clock
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[TokenResponse] | None = None

    @property
    def refresh_token(self) -> str:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def expires_at(self) -> float | None:
        """Return when the access token expires (``clock`` scale), if known."""
        return self._expires_at

    async def async_get_access_token(
        self, force_refresh: bool = False, stale: str | None = None
    ) -> str:
        """Return a usable access token, refreshing it first when needed.

        Without ``force_refresh`` the stored token is reused unless it is missing
        or expires within TOKEN_REFRESH_LEEWAY seconds.  With ``force_refresh``
        the token is renewed, except when ``stale`` (the token the caller used)
        differs from the current one: then another caller already renewed it and
        the current token is returned as is.
        """
        async with self._lock:
            current = self._access_token
            if current is not None and self._can_reuse(current, force_refresh, stale):
                return current
            response = await self._async_refresh_locked()
            return response.access_token

    async def async_refresh(self) -> TokenResponse:
        """Refresh unconditionally and store the result."""
        async with self._lock:
            return await self._async_refresh_locked()

    async def async_wait_for_refresh(self) -> None:
        """Wait until a refresh that is in flight has finished; its outcome is not raised here.

        The owner calls this before it unloads, so that a refresh token rotated by a
        refresh that was still running is stored before the owner starts over from the
        stored one.
        """
        task = self._refresh_task
        if task is not None and not task.done():
            await asyncio.wait({task})

    def _now(self) -> float:
        """Return the current time on the configured clock (``time.monotonic`` by default)."""
        return time.monotonic() if self._clock is None else self._clock()

    def _can_reuse(self, current: str, force_refresh: bool, stale: str | None) -> bool:
        """Return True when ``current`` satisfies the caller without a refresh."""
        if force_refresh:
            return stale is not None and current != stale
        if self._expires_at is None:
            return True
        return self._expires_at - self._now() >= TOKEN_REFRESH_LEEWAY

    async def _async_refresh_locked(self) -> TokenResponse:
        """Refresh while holding the lock; the request outlives a cancelled caller.

        A caller cancelled mid-request (its owner is reloaded or shut down) would otherwise
        abort a refresh the identity provider may already have completed, and with a
        one-time-use refresh token the rotated one would be lost.  The request therefore
        runs in its own task, awaited with ``asyncio.wait`` so that a cancelled caller
        leaves it running: it finishes, stores the tokens and reports a rotation whatever
        happens to the caller.  A later caller joins a refresh that is still running
        instead of starting a second one.
        """
        task = self._refresh_task
        if task is None or task.done():
            task = self._refresh_task = asyncio.create_task(
                self._async_refresh_now(), name="socialschools token refresh"
            )
            task.add_done_callback(_mark_outcome_retrieved)
        await asyncio.wait({task})
        return task.result()

    async def _async_refresh_now(self) -> TokenResponse:
        """Refresh, store the tokens (expiry computed first), then report a rotation."""
        issued_at = self._now()
        response = await async_refresh_tokens(
            self._session,
            self._refresh_token,
            issuer=self._issuer,
            client_id=self._client_id,
            user_agent=self._user_agent,
            timeout=self._timeout,
        )
        expires_at = issued_at + response.expires_in  # computed first: both change together
        self._access_token = response.access_token
        self._expires_at = expires_at
        rotated = response.refresh_token
        if rotated is None or rotated == self._refresh_token:
            return response
        self._refresh_token = rotated
        _LOGGER.debug("token refresh: refresh token rotated")
        if self._on_refresh_token_update is not None:
            result = self._on_refresh_token_update(rotated)
            if inspect.isawaitable(result):
                await result
        return response
