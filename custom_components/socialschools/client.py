"""Read-only Social Schools API client (HA-free).

Every call goes through :meth:`SocialSchoolsClient.async_request`, the single
place that talks to the API.  It lets only whitelisted ``GET`` paths through,
attaches the bearer token and the school headers, retries once with a renewed
token on 401 and maps every outcome onto the integration's exception
hierarchy.  Redirects are never followed; response bodies, tokens and query
strings never end up in messages or logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from .const import (
    API_BASE_URL,
    FEED_FILTER_TYPE,
    FEED_LIMIT,
    LOOPBACK_HOSTS,
    MAX_API_BODY_BYTES,
    REQUEST_TIMEOUT,
    ROLE_TYPE_PARENT,
    USER_AGENT,
)
from .errors import ApiError, CannotConnect, InvalidAuth, RequestNotAllowed

if TYPE_CHECKING:
    from .tokens import TokenManager

_LOGGER = logging.getLogger(__name__)

ALLOWED_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^useraccounts/current$", re.ASCII),
    re.compile(r"^communityposts$", re.ASCII),
    re.compile(r"^communityposts/\d+$", re.ASCII),
    re.compile(r"^events$", re.ASCII),
)
"""Relative API paths this client may GET; a path must match one of them in full.

The anchored patterns leave no room for a leading slash, a scheme, a query, a
fragment or a ``..`` segment, so nothing else is ever requested.
"""

_EVENT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_INVALID = object()
"""Marker for a body that is not JSON; the decode error (which holds the body) is dropped."""


def _check_request(method: str, path: str) -> re.Pattern[str]:
    """Return the whitelist pattern ``path`` matches, or raise RequestNotAllowed.

    The message never echoes ``path``: it could carry a query string.
    """
    if method != "GET":
        raise RequestNotAllowed(f"{method!r} is not allowed: this client only sends GET requests")
    for pattern in ALLOWED_PATHS:
        if pattern.fullmatch(path):
            return pattern
    raise RequestNotAllowed("path is not a whitelisted relative API path")


def _decode_json(body: bytes) -> Any:
    """Return the decoded JSON body, or ``_INVALID`` when it is not JSON (nothing of it is kept)."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return _INVALID


async def _async_read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """Return the body when it is at most ``limit`` bytes, else None without buffering the rest.

    A ``Content-Length`` over the limit is refused before reading; otherwise at most
    ``limit + 1`` bytes are read from the (decompressed) stream.
    """
    try:
        declared = int(response.headers.get("Content-Length", ""))
    except ValueError:  # absent, malformed or absurdly long
        declared = None
    if declared is not None and declared > limit:
        return None
    stream = response.content
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = await stream.read(limit + 1 - size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    return None


def _format_utc(value: datetime) -> str:
    """Format an aware datetime as UTC ``YYYY-MM-DDTHH:MM:SSZ``; naive values are rejected."""
    if value.utcoffset() is None:
        raise ValueError("event range boundaries must be timezone-aware datetimes")
    return value.astimezone(UTC).strftime(_EVENT_TIME_FORMAT)


class SocialSchoolsClient:
    """Read-only access to the Social Schools API on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_manager: TokenManager,
        *,
        base_url: str = API_BASE_URL,
        role_type: int = ROLE_TYPE_PARENT,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Bind the client to a session, a token manager and an API base URL.

        ``base_url`` must be absolute, end with ``/`` and use https unless its host is a
        loopback address (local test servers); otherwise ValueError.
        """
        base = URL(base_url)
        if not base.absolute or not base.path.endswith("/"):
            raise ValueError("base_url must be an absolute URL whose path ends with '/'")
        if base.scheme != "https" and base.host not in LOOPBACK_HOSTS:
            raise ValueError("base_url must use https (plain http only for loopback hosts)")
        self._session = session
        self._token_manager = token_manager
        self._base_url = base
        self._role_type = role_type
        self._user_agent = user_agent
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def token_manager(self) -> TokenManager:
        """Return the token manager this client authenticates with."""
        return self._token_manager

    async def async_request(
        self,
        method: str,
        path: str,
        *,
        school_id: int | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """GET a whitelisted relative ``path`` and return its decoded JSON body.

        Validates ``method`` and ``path`` before any token or network access
        (RequestNotAllowed).  A 401 is retried once with a renewed access token;
        a second 401 raises InvalidAuth.  Redirects, 5xx, network errors and
        timeouts raise CannotConnect; other statuses, a body over
        MAX_API_BODY_BYTES and invalid JSON ApiError.
        """
        what = f"GET {_check_request(method, path).pattern}"
        url = self._build_url(path)
        token = await self._token_manager.async_get_access_token()
        status, body = await self._async_send(url, token, school_id, params, what)
        if status == 401:
            _LOGGER.debug("%s: HTTP 401, retrying once with a renewed access token", what)
            token = await self._token_manager.async_get_access_token(
                force_refresh=True, stale=token
            )
            status, body = await self._async_send(url, token, school_id, params, what)
        _LOGGER.debug("%s: HTTP %s", what, status)
        if status == 401:
            raise InvalidAuth(f"{what}: HTTP 401 after a token refresh")
        if 300 <= status < 400 or status >= 500:
            raise CannotConnect(f"{what}: HTTP {status}")
        if not 200 <= status < 300:
            raise ApiError(f"{what}: HTTP {status}", status)
        payload = _decode_json(body)
        if payload is _INVALID:
            raise ApiError(f"{what}: invalid JSON", status)
        return payload

    async def async_get_current_account(self) -> dict[str, Any]:
        """Return the account profile (``useraccounts/current``), without school headers."""
        return await self.async_request("GET", "useraccounts/current")

    async def async_get_posts(
        self, school_id: int, *, offset: int = 0, limit: int = FEED_LIMIT
    ) -> dict[str, Any]:
        """Return one page of the school feed (``values``, ``totalCount``, ``timeStamp``)."""
        params = {"offset": str(offset), "limit": str(limit), "filterType": str(FEED_FILTER_TYPE)}
        return await self.async_request("GET", "communityposts", school_id=school_id, params=params)

    async def async_get_post(self, school_id: int, post_id: int) -> dict[str, Any]:
        """Return the detail of one post."""
        return await self.async_request("GET", f"communityposts/{post_id}", school_id=school_id)

    async def async_get_events(
        self, school_id: int, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Return the calendar events between two aware datetimes (sent as UTC)."""
        params = {"from": _format_utc(start), "until": _format_utc(end)}
        return await self.async_request("GET", "events", school_id=school_id, params=params)

    def _build_url(self, path: str) -> URL:
        """Join a whitelisted path onto the base URL and verify it stayed on the API base."""
        base = self._base_url
        url = base.join(URL(path))
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port) or (
            not url.path.startswith(base.path)
        ):
            raise RequestNotAllowed("request URL would leave the API base")
        return url

    def _headers(self, token: str, school_id: int | None) -> dict[str, str]:
        """Build the request headers; school context only when a school is given."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if school_id is not None:
            headers["Schoolid"] = str(school_id)
            headers["Roletypeid"] = str(self._role_type)
        return headers

    async def _async_send(
        self,
        url: URL,
        token: str,
        school_id: int | None,
        params: Mapping[str, str] | None,
        what: str,
    ) -> tuple[int, bytes]:
        """Send one GET and return ``(status, body)``.

        The body is read only for 2xx and only up to MAX_API_BODY_BYTES; a larger
        body raises ApiError without being buffered.
        """
        try:
            async with self._session.get(
                url,
                headers=self._headers(token, school_id),
                params=params,
                allow_redirects=False,
                timeout=self._timeout,
            ) as response:
                status = response.status
                body = (
                    await _async_read_limited(response, MAX_API_BODY_BYTES)
                    if 200 <= status < 300
                    else b""
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"{what}: {type(err).__name__}") from err
        if body is None:
            raise ApiError(f"{what}: response body too large", status)
        return status, body
