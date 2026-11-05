"""Tests for the token endpoint helpers and the TokenManager (pure module).

A local aiohttp application plays the token endpoint; every value is synthetic
and secret-like values carry the ``private-`` marker so that leaks into
exception messages can be detected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient
from multidict import CIMultiDict
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.socialschools import tokens as tokens_module
from custom_components.socialschools.const import (
    DEFAULT_EXPIRES_IN,
    MAX_EXPIRES_IN,
    MAX_TOKEN_BODY_BYTES,
    OIDC_CLIENT_ID,
    OIDC_REDIRECT_URI,
    TOKEN_REFRESH_LEEWAY,
    USER_AGENT,
)
from custom_components.socialschools.errors import ApiError, CannotConnect, InvalidAuth
from custom_components.socialschools.tokens import (
    TokenManager,
    TokenResponse,
    async_exchange_code,
    async_refresh_tokens,
)

MISSING = object()


@dataclass
class Reply:
    """One canned answer of the fake token endpoint."""

    status: int = 200
    json: Any = None
    raw: bytes | None = None  # sent verbatim instead of ``json`` when set
    chunked: bool = False  # send ``raw`` chunked, without a Content-Length header
    headers: dict[str, str] | None = None
    hold: asyncio.Event | None = None  # the handler waits for this before answering


@dataclass
class TokenEndpoint:
    """Fake ``/connect/token`` that records every posted form and its headers."""

    replies: list[Reply] = field(default_factory=list)
    forms: list[dict[str, str]] = field(default_factory=list)
    headers: list[CIMultiDict[str]] = field(default_factory=list)

    def reply(self, status: int = 200, json: Any = None, **kwargs: Any) -> None:
        """Queue a reply; the last queued reply repeats for every further request."""
        self.replies.append(Reply(status=status, json=json, **kwargs))

    async def handle(self, request: web.Request) -> web.Response:
        """Record the request and answer with the next queued reply."""
        form = await request.post()
        self.forms.append({key: str(value) for key, value in form.items()})
        self.headers.append(CIMultiDict(request.headers))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if reply.hold is not None:
            await reply.hold.wait()
        if reply.raw is not None:
            response = web.Response(
                status=reply.status,
                body=reply.raw,
                content_type="application/json",
                headers=reply.headers,
            )
            if reply.chunked:
                response.enable_chunked_encoding()
            return response
        return web.json_response(reply.json, status=reply.status, headers=reply.headers)


class FakeClock:
    """A mutable monotonic clock."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start at ``now`` seconds."""
        self.now = now

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


def tokens(
    access: Any = "private-access-2",
    refresh: Any = "private-refresh-2",
    expires_in: Any = 3600,
) -> dict[str, Any]:
    """Build a token endpoint body; ``MISSING`` leaves a key out."""
    fields = {"access_token": access, "refresh_token": refresh, "expires_in": expires_in}
    return {key: value for key, value in fields.items() if value is not MISSING}


def assert_sanitised(exc: BaseException) -> None:
    """Exception messages must never carry secrets."""
    assert "private-" not in str(exc)


def assert_no_body_in_chain(exc: BaseException) -> None:
    """Neither the message nor any chained exception (e.g. JSONDecodeError.doc) carries the body."""
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert "private-" not in repr(vars(exc))


async def until(condition: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Let the loop run until ``condition`` holds (a request reached the fake endpoint)."""
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition(), "the condition did not come true in time"


@pytest.fixture
def endpoint() -> TokenEndpoint:
    """Return the state of the fake token endpoint."""
    return TokenEndpoint()


@pytest.fixture
async def token_client(
    aiohttp_client: ClientSessionGenerator, endpoint: TokenEndpoint, socket_enabled: None
) -> TestClient:
    """Serve the fake token endpoint on 127.0.0.1 (pytest-socket allows only that host)."""
    app = web.Application()
    app.router.add_post("/connect/token", endpoint.handle)
    return await aiohttp_client(app)


@pytest.fixture
def issuer(token_client: TestClient) -> str:
    """Return the local issuer URL (no trailing slash)."""
    return str(token_client.make_url("")).rstrip("/")


@pytest.fixture
def session(token_client: TestClient) -> ClientSession:
    """Return the real client session of the test client."""
    return token_client.session


def make_manager(session: ClientSession, issuer: str, **kwargs: Any) -> TokenManager:
    """Create a TokenManager against the local issuer with sensible defaults."""
    kwargs.setdefault("client_id", OIDC_CLIENT_ID)
    kwargs.setdefault("refresh_token", "private-refresh-1")
    return TokenManager(session, issuer=issuer, **kwargs)


# --- async_exchange_code -------------------------------------------------------------------


async def test_exchange_code_posts_pkce_form(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """The code exchange posts exactly the public-client PKCE form."""
    endpoint.reply(json=tokens("private-access-1", "private-refresh-1"))

    result = await async_exchange_code(session, "private-code", "private-verifier", issuer=issuer)

    assert result == TokenResponse("private-access-1", "private-refresh-1", 3600)
    assert endpoint.forms == [
        {
            "grant_type": "authorization_code",
            "code": "private-code",
            "code_verifier": "private-verifier",
            "redirect_uri": OIDC_REDIRECT_URI,
            "client_id": OIDC_CLIENT_ID,
        }
    ]
    assert "client_secret" not in endpoint.forms[0]
    headers = endpoint.headers[0]
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert "Authorization" not in headers


async def test_exchange_code_without_refresh_token(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A code exchange answer without a refresh token is unusable."""
    endpoint.reply(json=tokens("private-access-1", MISSING))

    with pytest.raises(ApiError) as excinfo:
        await async_exchange_code(session, "private-code", "private-verifier", issuer=issuer)

    assert "no refresh token" in str(excinfo.value)
    assert_sanitised(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_exchange_code_auth_failure(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, status: int
) -> None:
    """400/401/403 on the code exchange mean the code was not accepted."""
    endpoint.reply(status, {"error": "invalid_grant", "error_description": "private-detail"})

    with pytest.raises(InvalidAuth) as excinfo:
        await async_exchange_code(session, "private-code", "private-verifier", issuer=issuer)

    assert_sanitised(excinfo.value)


# --- async_refresh_tokens ------------------------------------------------------------------


async def test_refresh_tokens_posts_refresh_form(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """The refresh grant posts refresh_token and client_id only."""
    endpoint.reply(json=tokens(expires_in="900"))

    result = await async_refresh_tokens(
        session, "private-refresh-1", issuer=issuer, client_id="private-client"
    )

    assert result == TokenResponse("private-access-2", "private-refresh-2", 900)
    assert endpoint.forms == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "private-refresh-1",
            "client_id": "private-client",
        }
    ]
    assert endpoint.headers[0]["Accept"] == "application/json"
    assert endpoint.headers[0]["User-Agent"] == USER_AGENT


async def test_refresh_tokens_without_refresh_token(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A refresh answer may omit the refresh token."""
    endpoint.reply(json=tokens(refresh=MISSING))

    result = await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert result == TokenResponse("private-access-2", None, 3600)


@pytest.mark.parametrize("status", [400, 401])
async def test_refresh_tokens_auth_failure(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, status: int
) -> None:
    """400/401 on a refresh mean the refresh token is no longer valid."""
    endpoint.reply(status, {"error": "invalid_grant", "error_description": "private-detail"})

    with pytest.raises(InvalidAuth) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert_sanitised(excinfo.value)


@pytest.mark.parametrize("status", [403, 404, 429])
async def test_refresh_tokens_other_client_error(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, status: int
) -> None:
    """Other 4xx statuses are API errors that carry the status."""
    endpoint.reply(status, {"error": "private-detail"})

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert excinfo.value.status == status
    assert_sanitised(excinfo.value)


async def test_refresh_tokens_redirect_is_not_followed(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A 3xx is reported as an API error and never followed."""
    endpoint.reply(302, headers={"Location": f"{issuer}/connect/token"})

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert excinfo.value.status == 302
    assert len(endpoint.forms) == 1
    assert_sanitised(excinfo.value)


async def test_refresh_tokens_server_error(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A 5xx is a connectivity problem."""
    endpoint.reply(503, raw=b"<html>private-detail</html>")

    with pytest.raises(CannotConnect) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert_sanitised(excinfo.value)


async def test_refresh_tokens_timeout(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A request that exceeds the timeout is a connectivity problem."""
    hold = asyncio.Event()
    endpoint.reply(json=tokens(), hold=hold)

    try:
        with pytest.raises(CannotConnect) as excinfo:
            await async_refresh_tokens(session, "private-refresh-1", issuer=issuer, timeout=0.2)
    finally:
        hold.set()

    assert_sanitised(excinfo.value)


async def test_refresh_tokens_connection_error(session: ClientSession) -> None:
    """A refused connection is a connectivity problem."""
    with pytest.raises(CannotConnect) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer="http://127.0.0.1:9")

    assert_sanitised(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [b"not json {", b"", b"[]", b'"private-access"', b"\xff\xfe\x00"],
)
async def test_refresh_tokens_invalid_json(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, raw: bytes
) -> None:
    """Undecodable or wrongly shaped bodies are API errors."""
    endpoint.reply(raw=raw)

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert_sanitised(excinfo.value)
    assert_no_body_in_chain(excinfo.value)


async def test_refresh_tokens_truncated_json_keeps_no_body(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A truncated token body is not kept on the error: JSONDecodeError.doc would hold the tokens."""
    endpoint.reply(raw=b'{"access_token": "private-at-trunc", "refresh_token": "private-rt-trunc"')

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert "invalid JSON" in str(excinfo.value)
    assert excinfo.value.status == 200
    assert_no_body_in_chain(excinfo.value)


async def test_refresh_tokens_deeply_nested_json_is_api_error(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """JSON nested beyond the recursion limit (RecursionError) is an API error, not a crash."""
    endpoint.reply(raw=b"[" * 100_000 + b"]" * 100_000)

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert "invalid JSON" in str(excinfo.value)
    assert_no_body_in_chain(excinfo.value)


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_refresh_tokens_oversized_body_is_api_error(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, chunked: bool
) -> None:
    """A 200 body over MAX_TOKEN_BODY_BYTES is refused (before or while reading) as an API error."""
    endpoint.reply(
        raw=b'{"access_token": "private-big", "pad": "' + b"x" * MAX_TOKEN_BODY_BYTES + b'"}',
        chunked=chunked,
    )

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert "too large" in str(excinfo.value)
    assert excinfo.value.status == 200
    assert_sanitised(excinfo.value)


async def test_refresh_tokens_body_of_exactly_the_limit_is_accepted(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """The size limit is inclusive."""
    head = b'{"access_token": "private-access-2", "refresh_token": "private-refresh-2", "pad": "'
    tail = b'"}'
    raw = head + b"x" * (MAX_TOKEN_BODY_BYTES - len(head) - len(tail)) + tail
    assert len(raw) == MAX_TOKEN_BODY_BYTES
    endpoint.reply(raw=raw)

    result = await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert result == TokenResponse("private-access-2", "private-refresh-2", DEFAULT_EXPIRES_IN)


async def test_token_response_repr_hides_the_tokens(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """repr()/str() of a token response never show the access or refresh token."""
    endpoint.reply(json=tokens("private-access-1", "private-refresh-1"))
    exchanged = await async_exchange_code(
        session, "private-code", "private-verifier", issuer=issuer
    )
    endpoint.reply(json=tokens())
    refreshed = await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    for response in (exchanged, refreshed, TokenResponse("private-a", "private-r", 60)):
        assert response.access_token.startswith("private-")
        assert "private-" not in repr(response)
        assert "private-" not in str(response)
        assert "expires_in" in repr(response)


@pytest.mark.parametrize(
    "body",
    [
        tokens("private-access\n"),
        tokens("private-access\r\n2"),
        tokens("private access"),
        tokens(""),
        tokens(None),
        tokens(MISSING),
        tokens(12345),
        tokens(refresh="private-refresh\n"),
        tokens(refresh=""),
        tokens(refresh=None),
    ],
)
async def test_refresh_tokens_invalid_token_values(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, body: dict[str, Any]
) -> None:
    """Tokens must be non-empty strings without control characters."""
    endpoint.reply(json=body)

    with pytest.raises(ApiError) as excinfo:
        await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert_sanitised(excinfo.value)


@pytest.mark.parametrize(
    ("expires_in", "expected"),
    [
        (MISSING, DEFAULT_EXPIRES_IN),
        (None, DEFAULT_EXPIRES_IN),
        ("soon", DEFAULT_EXPIRES_IN),
        (0, DEFAULT_EXPIRES_IN),
        (-5, DEFAULT_EXPIRES_IN),
        ([], DEFAULT_EXPIRES_IN),
        ("900", 900),
        (1800.9, 1800),
        (120, 120),
        (MAX_EXPIRES_IN, MAX_EXPIRES_IN),
        (MAX_EXPIRES_IN + 1, MAX_EXPIRES_IN),
        (10**400, MAX_EXPIRES_IN),
        (float("inf"), DEFAULT_EXPIRES_IN),
        (float("nan"), DEFAULT_EXPIRES_IN),
    ],
)
async def test_refresh_tokens_expires_in(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, expires_in: Any, expected: int
) -> None:
    """expires_in falls back to the default unless it is a positive number; it is capped."""
    endpoint.reply(json=tokens(expires_in=expires_in))

    result = await async_refresh_tokens(session, "private-refresh-1", issuer=issuer)

    assert result.expires_in == expected


# --- TokenManager --------------------------------------------------------------------------


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_manager_refresh_rotation_invokes_callback(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, asynchronous: bool
) -> None:
    """A rotated refresh token is stored and reported exactly once."""
    calls: list[str] = []

    def sync_callback(token: str) -> None:
        calls.append(token)

    async def async_callback(token: str) -> None:
        calls.append(token)

    clock = FakeClock()
    manager = make_manager(
        session,
        issuer,
        client_id="private-client",
        on_refresh_token_update=async_callback if asynchronous else sync_callback,
        clock=clock,
    )
    endpoint.reply(json=tokens(expires_in=1200))

    response = await manager.async_refresh()

    assert response == TokenResponse("private-access-2", "private-refresh-2", 1200)
    assert calls == ["private-refresh-2"]
    assert manager.refresh_token == "private-refresh-2"
    assert manager.expires_at == clock.now + 1200
    assert endpoint.forms == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "private-refresh-1",
            "client_id": "private-client",
        }
    ]
    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 1


async def test_manager_refresh_without_refresh_token_keeps_old_one(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A refresh answer without refresh token keeps the old one and stays silent."""
    calls: list[str] = []
    manager = make_manager(session, issuer, on_refresh_token_update=calls.append)
    endpoint.reply(json=tokens(refresh=MISSING))

    assert await manager.async_get_access_token() == "private-access-2"

    assert manager.refresh_token == "private-refresh-1"
    assert calls == []
    assert len(endpoint.forms) == 1


async def test_manager_same_refresh_token_does_not_invoke_callback(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """Receiving the refresh token that is already stored is not a rotation."""
    calls: list[str] = []
    manager = make_manager(session, issuer, on_refresh_token_update=calls.append)
    endpoint.reply(json=tokens(refresh="private-refresh-1"))

    await manager.async_refresh()

    assert manager.refresh_token == "private-refresh-1"
    assert calls == []


async def test_manager_callback_error_propagates_after_storing_tokens(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A failing callback surfaces, but the new tokens are already stored."""

    def failing_callback(token: str) -> None:
        raise RuntimeError("callback failed")

    manager = make_manager(session, issuer, on_refresh_token_update=failing_callback)
    endpoint.reply(json=tokens())

    with pytest.raises(RuntimeError) as excinfo:
        await manager.async_refresh()

    assert_sanitised(excinfo.value)
    assert manager.refresh_token == "private-refresh-2"
    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 1


@pytest.mark.parametrize("status", [400, 401])
async def test_manager_auth_failure(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, status: int
) -> None:
    """A rejected refresh token surfaces as InvalidAuth and leaves the manager unchanged."""
    manager = make_manager(session, issuer)
    endpoint.reply(status, {"error": "invalid_grant"})

    with pytest.raises(InvalidAuth) as excinfo:
        await manager.async_get_access_token()

    assert_sanitised(excinfo.value)
    assert manager.refresh_token == "private-refresh-1"
    assert manager.expires_at is None


async def test_manager_server_error_releases_lock(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """After a failed refresh the next call tries again."""
    manager = make_manager(session, issuer)
    endpoint.reply(503, raw=b"private-detail")
    endpoint.reply(json=tokens())

    with pytest.raises(CannotConnect) as excinfo:
        await manager.async_get_access_token()
    assert_sanitised(excinfo.value)

    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 2


async def test_manager_proactive_refresh_before_expiry(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """The token is renewed once less than TOKEN_REFRESH_LEEWAY seconds remain."""
    clock = FakeClock()
    expires_at = clock.now + 3600
    manager = make_manager(
        session, issuer, access_token="private-access-1", expires_at=expires_at, clock=clock
    )
    endpoint.reply(json=tokens())

    clock.now = expires_at - TOKEN_REFRESH_LEEWAY - 1
    assert await manager.async_get_access_token() == "private-access-1"
    assert endpoint.forms == []

    clock.now = expires_at - TOKEN_REFRESH_LEEWAY + 1
    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 1
    assert manager.expires_at == clock.now + 3600

    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 1


async def test_manager_without_access_token_refreshes_first(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """Without a stored access token the first call refreshes."""
    clock = FakeClock()
    manager = make_manager(session, issuer, clock=clock)
    endpoint.reply(json=tokens(expires_in=600))

    assert manager.expires_at is None
    assert await manager.async_get_access_token() == "private-access-2"
    assert manager.expires_at == clock.now + 600
    assert len(endpoint.forms) == 1


async def test_manager_unknown_expiry_reuses_token(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A stored access token with unknown expiry is used until a forced refresh."""
    manager = make_manager(session, issuer, access_token="private-access-1")

    assert await manager.async_get_access_token() == "private-access-1"
    assert endpoint.forms == []


async def test_manager_concurrent_forced_refreshes_make_one_request(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """Callers that saw the same stale token share a single refresh."""
    clock = FakeClock()
    manager = make_manager(
        session, issuer, access_token="private-access-1", expires_at=clock.now + 3600, clock=clock
    )
    endpoint.reply(json=tokens())

    results = await asyncio.gather(
        *(
            manager.async_get_access_token(force_refresh=True, stale="private-access-1")
            for _ in range(5)
        )
    )

    assert results == ["private-access-2"] * 5
    assert len(endpoint.forms) == 1


async def test_manager_force_refresh_with_outdated_stale_token(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A forced refresh for a token that was already replaced returns the current one."""
    clock = FakeClock()
    manager = make_manager(
        session, issuer, access_token="private-access-2", expires_at=clock.now + 3600, clock=clock
    )

    assert (
        await manager.async_get_access_token(force_refresh=True, stale="private-access-1")
        == "private-access-2"
    )
    assert endpoint.forms == []


@pytest.mark.parametrize("stale", [None, "private-access-1"])
async def test_manager_force_refresh_renews_fresh_token(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, stale: str | None
) -> None:
    """A forced refresh without stale, or with the current token as stale, always refreshes."""
    clock = FakeClock()
    manager = make_manager(
        session, issuer, access_token="private-access-1", expires_at=clock.now + 3600, clock=clock
    )
    endpoint.reply(json=tokens())

    assert (
        await manager.async_get_access_token(force_refresh=True, stale=stale) == "private-access-2"
    )
    assert len(endpoint.forms) == 1


async def test_manager_force_refresh_without_access_token_refreshes(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A forced refresh with a stale token but no stored token still refreshes."""
    manager = make_manager(session, issuer)
    endpoint.reply(json=tokens())

    assert (
        await manager.async_get_access_token(force_refresh=True, stale="private-access-0")
        == "private-access-2"
    )
    assert len(endpoint.forms) == 1


async def test_manager_huge_expires_in_is_clamped(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """An absurd expires_in neither overflows the clock arithmetic nor leaves the manager half-updated."""
    clock = FakeClock()
    manager = make_manager(session, issuer, clock=clock)
    endpoint.reply(json=tokens(expires_in=10**400))

    assert await manager.async_get_access_token() == "private-access-2"

    assert manager.expires_at == clock.now + MAX_EXPIRES_IN
    assert manager.refresh_token == "private-refresh-2"


async def test_manager_stores_access_token_and_expiry_together(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When computing the expiry fails, the old access token (and its expiry) stay in place."""
    calls: list[str] = []

    async def unclamped_refresh(*args: Any, **kwargs: Any) -> TokenResponse:
        calls.append("refresh")
        return TokenResponse("private-access-2", "private-refresh-2", 10**400)

    monkeypatch.setattr(tokens_module, "async_refresh_tokens", unclamped_refresh)
    clock = FakeClock()
    manager = make_manager(
        session, issuer, access_token="private-access-1", expires_at=clock.now + 3600, clock=clock
    )

    with pytest.raises(OverflowError):
        await manager.async_refresh()

    assert manager.expires_at == clock.now + 3600
    assert manager.refresh_token == "private-refresh-1"
    assert await manager.async_get_access_token() == "private-access-1"
    assert calls == ["refresh"]


async def test_manager_refresh_survives_a_cancelled_caller(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A caller cancelled mid-request does not abort the refresh; the rotation is still stored.

    The owner's coordinator is cancelled on every reload and shutdown; a refresh the
    identity provider already completed must not lose the (one-time-use) rotated refresh
    token.  A caller that arrives meanwhile joins the running refresh instead of
    starting a second one.
    """
    calls: list[str] = []
    manager = make_manager(session, issuer, on_refresh_token_update=calls.append)
    hold = asyncio.Event()
    endpoint.reply(json=tokens(), hold=hold)

    first = asyncio.create_task(manager.async_get_access_token())
    await until(lambda: len(endpoint.forms) == 1)
    first.cancel()
    await asyncio.wait({first})
    assert first.cancelled()

    second = asyncio.create_task(manager.async_get_access_token())
    await asyncio.sleep(0.01)
    assert not second.done()
    hold.set()
    assert await second == "private-access-2"
    assert len(endpoint.forms) == 1
    assert manager.refresh_token == "private-refresh-2"
    assert calls == ["private-refresh-2"]


async def test_manager_wait_for_refresh(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """Waiting for a refresh returns at once when none runs and does not raise for a failed one."""
    manager = make_manager(session, issuer)
    await manager.async_wait_for_refresh()
    hold = asyncio.Event()
    endpoint.reply(503, raw=b"private-detail", hold=hold)

    caller = asyncio.create_task(manager.async_get_access_token())
    await until(lambda: len(endpoint.forms) == 1)
    waiter = asyncio.create_task(manager.async_wait_for_refresh())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    hold.set()
    await waiter
    with pytest.raises(CannotConnect) as excinfo:
        await caller
    assert_sanitised(excinfo.value)
    assert manager.refresh_token == "private-refresh-1"
    assert manager.expires_at is None


async def test_manager_failed_refresh_of_a_cancelled_caller_is_retried(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str
) -> None:
    """A refresh that failed after its caller was cancelled is not reused: the next caller retries."""
    manager = make_manager(session, issuer)
    hold = asyncio.Event()
    endpoint.reply(503, raw=b"private-detail", hold=hold)
    endpoint.reply(json=tokens())

    first = asyncio.create_task(manager.async_get_access_token())
    await until(lambda: len(endpoint.forms) == 1)
    first.cancel()
    await asyncio.wait({first})
    hold.set()
    await manager.async_wait_for_refresh()

    assert await manager.async_get_access_token() == "private-access-2"
    assert len(endpoint.forms) == 2


async def test_manager_default_clock_is_read_at_each_call(
    endpoint: TokenEndpoint, session: ClientSession, issuer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a clock the manager reads time.monotonic at every use, so a patched clock counts."""
    manager = make_manager(session, issuer, access_token="private-access-1", expires_at=5_000.0)
    endpoint.reply(json=tokens())

    monkeypatch.setattr(tokens_module, "time", SimpleNamespace(monotonic=lambda: 1_000.0))
    assert await manager.async_get_access_token() == "private-access-1"
    assert endpoint.forms == []

    monkeypatch.setattr(tokens_module, "time", SimpleNamespace(monotonic=lambda: 4_950.0))
    assert await manager.async_get_access_token() == "private-access-2"
    assert manager.expires_at == 4_950.0 + 3600
    assert len(endpoint.forms) == 1
