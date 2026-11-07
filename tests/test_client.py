"""Tests for the read-only API client (pure module).

A local aiohttp application plays the Social Schools API and records every
request it receives.  All values are synthetic; secret-like values carry the
``private-`` marker so that leaks into exception messages or logs are caught.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
import json
import logging
import re
import socket
from types import SimpleNamespace
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient
from multidict import CIMultiDict
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.socialschools import client as client_module, payload as payload_module
from custom_components.socialschools.client import (
    ALLOWED_PATHS,
    RESPONSE_SHAPES,
    SocialSchoolsClient,
)
from custom_components.socialschools.const import (
    FEED_FILTER_TYPE,
    FEED_LIMIT,
    MAX_API_BODY_BYTES,
    MAX_JSON_DEPTH,
    ROLE_TYPE_PARENT,
    USER_AGENT,
    VERSION,
)
from custom_components.socialschools.errors import (
    ApiError,
    CannotConnect,
    InvalidAuth,
    RequestNotAllowed,
    SocialSchoolsError,
)

SCHOOL_ID = 7


@dataclass
class Reply:
    """One canned answer of the fake API."""

    status: int = 200
    json: Any = field(default_factory=dict)
    raw: bytes | None = None  # sent verbatim instead of ``json`` when set
    chunked: bool = False  # send ``raw`` chunked, without a Content-Length header
    content_type: str = "application/json"
    headers: dict[str, str] | None = None
    hold: asyncio.Event | None = None  # the handler waits for this before answering


@dataclass
class Recorded:
    """What the fake API saw of one request."""

    method: str
    host: str
    path: str
    query: dict[str, str]
    headers: CIMultiDict[str]


@dataclass
class FakeApi:
    """Fake API under ``/api/v1/`` that records every request."""

    replies: list[Reply] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)

    def reply(self, status: int = 200, json: Any = None, **kwargs: Any) -> Reply:
        """Queue a reply; the last queued reply repeats for every further request."""
        reply = Reply(status=status, json={} if json is None else json, **kwargs)
        self.replies.append(reply)
        return reply

    async def handle(self, request: web.Request) -> web.Response:
        """Record the request and answer with the next queued reply (default: 200 ``{}``)."""
        self.requests.append(
            Recorded(
                method=request.method,
                host=request.host,
                path=request.path,
                query=dict(request.query),
                headers=CIMultiDict(request.headers),
            )
        )
        if not self.replies:
            reply = Reply()
        else:
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if reply.hold is not None:
            await reply.hold.wait()
        if reply.raw is not None:
            response = web.Response(
                status=reply.status,
                body=reply.raw,
                content_type=reply.content_type,
                headers=reply.headers,
            )
            if reply.chunked:
                response.enable_chunked_encoding()
            return response
        return web.json_response(reply.json, status=reply.status, headers=reply.headers)


class FakeTokenManager:
    """Stand-in for TokenManager: hands out queued tokens and records every call."""

    def __init__(self, *tokens: str) -> None:
        """Start with ``tokens``; each refresh moves on to the next one."""
        self._tokens = list(tokens)
        self.calls: list[tuple[bool, str | None]] = []
        self.refreshes = 0

    @property
    def current(self) -> str:
        """Return the token currently handed out."""
        return self._tokens[0]

    async def async_get_access_token(
        self, force_refresh: bool = False, stale: str | None = None
    ) -> str:
        """Mirror TokenManager: a forced refresh is skipped when ``stale`` is already replaced."""
        self.calls.append((force_refresh, stale))
        if force_refresh and (stale is None or stale == self.current):
            await self.async_refresh()
        return self.current

    async def async_refresh(self) -> None:
        """Rotate to the next queued token."""
        self.refreshes += 1
        if len(self._tokens) > 1:
            self._tokens.pop(0)


def assert_sanitised(exc: BaseException) -> None:
    """Exception messages must never carry secrets, bodies or query strings."""
    message = str(exc)
    assert "private-" not in message
    assert "?" not in message


def assert_no_body_in_chain(exc: BaseException) -> None:
    """Neither the message nor any chained exception (e.g. JSONDecodeError.doc) carries the body."""
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert "private-" not in repr(vars(exc))


@pytest.fixture
def api() -> FakeApi:
    """Return the state of the fake API."""
    return FakeApi()


@pytest.fixture
async def api_server(
    aiohttp_client: ClientSessionGenerator, api: FakeApi, socket_enabled: None
) -> TestClient:
    """Serve the fake API on 127.0.0.1 (pytest-socket allows only that host)."""
    app = web.Application()
    app.router.add_route("*", "/api/v1/{tail:.*}", api.handle)
    return await aiohttp_client(app)


@pytest.fixture
def base_url(api_server: TestClient) -> str:
    """Return the local API base URL."""
    return str(api_server.make_url("/api/v1/"))


@pytest.fixture
def token_manager() -> FakeTokenManager:
    """Return a fake token manager with a current token and one spare."""
    return FakeTokenManager("private-token-1", "private-token-2")


@pytest.fixture
def client(
    api_server: TestClient, base_url: str, token_manager: FakeTokenManager
) -> SocialSchoolsClient:
    """Return a client against the fake API."""
    return SocialSchoolsClient(api_server.session, token_manager, base_url=base_url, timeout=5)


def test_allowed_paths_are_exactly_the_read_endpoints() -> None:
    """The whitelist holds the four read endpoints, ASCII-only."""
    assert [pattern.pattern for pattern in ALLOWED_PATHS] == [
        r"^useraccounts/current$",
        r"^communityposts$",
        r"^communityposts/\d+$",
        r"^events$",
    ]
    assert all(pattern.flags & re.ASCII for pattern in ALLOWED_PATHS)


async def test_get_current_account(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """The profile call carries the bearer token but no school headers."""
    api.reply(json={"userProfileId": 42, "roles": []})
    assert await client.async_get_current_account() == {"userProfileId": 42, "roles": []}
    (request,) = api.requests
    assert request.method == "GET"
    assert request.path == "/api/v1/useraccounts/current"
    assert request.query == {}
    assert request.headers["Authorization"] == "Bearer private-token-1"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == f"HomeAssistant-SocialSchools/{VERSION}" == USER_AGENT
    assert "Schoolid" not in request.headers
    assert "Roletypeid" not in request.headers
    assert token_manager.calls == [(False, None)]


async def test_get_posts(client: SocialSchoolsClient, api: FakeApi) -> None:
    """The feed call carries the school headers and the fixed query."""
    api.reply(json={"values": [], "totalCount": 0, "timeStamp": "2026-11-04T10:00:00Z"})
    result = await client.async_get_posts(SCHOOL_ID)
    assert result == {"values": [], "totalCount": 0, "timeStamp": "2026-11-04T10:00:00Z"}
    (request,) = api.requests
    assert request.method == "GET"
    assert request.path == "/api/v1/communityposts"
    assert request.query == {
        "offset": "0",
        "limit": str(FEED_LIMIT),
        "filterType": str(FEED_FILTER_TYPE),
    }
    assert request.headers["Authorization"] == "Bearer private-token-1"
    assert request.headers["Schoolid"] == str(SCHOOL_ID)
    assert request.headers["Roletypeid"] == str(ROLE_TYPE_PARENT)
    assert request.headers["User-Agent"] == USER_AGENT


async def test_get_posts_paging(client: SocialSchoolsClient, api: FakeApi) -> None:
    """Offset and limit are sent as strings."""
    await client.async_get_posts(SCHOOL_ID, offset=40, limit=5)
    assert api.requests[0].query == {"offset": "40", "limit": "5", "filterType": "0"}


async def test_get_post(client: SocialSchoolsClient, api: FakeApi) -> None:
    """The detail call targets ``communityposts/{id}`` with school headers and no query."""
    api.reply(json={"id": 12})
    assert await client.async_get_post(SCHOOL_ID, 12) == {"id": 12}
    (request,) = api.requests
    assert request.path == "/api/v1/communityposts/12"
    assert request.query == {}
    assert request.headers["Schoolid"] == "7"
    assert request.headers["Roletypeid"] == "1"


async def test_get_post_rejects_non_numeric_id(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """A post id that does not form a whitelisted path is refused before any access."""
    with pytest.raises(RequestNotAllowed) as exc_info:
        await client.async_get_post(SCHOOL_ID, -1)
    assert_sanitised(exc_info.value)
    assert token_manager.calls == []
    assert api.requests == []


async def test_custom_role_type_and_user_agent(
    api_server: TestClient, base_url: str, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """Role type and user agent are configurable."""
    client = SocialSchoolsClient(
        api_server.session, token_manager, base_url=base_url, role_type=2, user_agent="Probe/1.0"
    )
    await client.async_get_posts(SCHOOL_ID)
    (request,) = api.requests
    assert request.headers["Roletypeid"] == "2"
    assert request.headers["User-Agent"] == "Probe/1.0"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
            datetime(2026, 11, 5, 23, 59, 59, tzinfo=UTC),
            {"from": "2026-11-04T10:00:00Z", "until": "2026-11-05T23:59:59Z"},
        ),
        (
            datetime(2026, 11, 4, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            datetime(2026, 11, 4, 5, 30, 15, tzinfo=timezone(timedelta(hours=-5))),
            {"from": "2026-11-04T10:00:00Z", "until": "2026-11-04T10:30:15Z"},
        ),
    ],
)
async def test_get_events_formats_utc(
    client: SocialSchoolsClient,
    api: FakeApi,
    start: datetime,
    end: datetime,
    expected: dict[str, str],
) -> None:
    """Range boundaries are converted to UTC and formatted with a ``Z`` suffix."""
    api.reply(json=[{"id": 1}])
    assert await client.async_get_events(SCHOOL_ID, start, end) == [{"id": 1}]
    (request,) = api.requests
    assert request.method == "GET"
    assert request.path == "/api/v1/events"
    assert request.query == expected
    assert request.headers["Schoolid"] == str(SCHOOL_ID)
    assert request.headers["Roletypeid"] == str(ROLE_TYPE_PARENT)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 11, 4, 10, 0), datetime(2026, 11, 5, 10, 0, tzinfo=UTC)),
        (datetime(2026, 11, 4, 10, 0, tzinfo=UTC), datetime(2026, 11, 5, 10, 0)),
    ],
)
async def test_get_events_rejects_naive_datetimes(
    client: SocialSchoolsClient,
    api: FakeApi,
    token_manager: FakeTokenManager,
    start: datetime,
    end: datetime,
) -> None:
    """A naive boundary raises ValueError before any token or network access."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await client.async_get_events(SCHOOL_ID, start, end)
    assert token_manager.calls == []
    assert api.requests == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "communityposts"),
        ("PUT", "events"),
        ("DELETE", "communityposts/12"),
        ("HEAD", "events"),
        ("get", "events"),
        ("GET", "inboxes"),
        ("GET", "communityposts/12/comments"),
        ("GET", "statistics/PostsSeenBy"),
        ("GET", "https://api.socialschools.eu/api/v1/events"),
        ("GET", "//api.socialschools.eu/api/v1/events"),
        ("GET", "/events"),
        ("GET", "../events"),
        ("GET", "communityposts/../events"),
        ("GET", "events?x=1"),
        ("GET", "events?token=private-token"),
        ("GET", "events#fragment"),
        ("GET", "events/"),
        ("GET", "events\n"),
        ("GET", "Events"),
        ("GET", ""),
        ("GET", "communityposts/"),
        ("GET", "communityposts/12a"),
        ("GET", "communityposts/-12"),
        ("GET", "communityposts/١٢"),
        ("GET", "useraccounts"),
        ("GET", "useraccounts/current/"),
        ("GET", "private-path"),
    ],
)
async def test_request_not_allowed(
    client: SocialSchoolsClient,
    api: FakeApi,
    token_manager: FakeTokenManager,
    method: str,
    path: str,
) -> None:
    """Anything outside the GET whitelist is refused before any token or network access."""
    with pytest.raises(RequestNotAllowed) as exc_info:
        await client.async_request(method, path, school_id=SCHOOL_ID)
    assert isinstance(exc_info.value, SocialSchoolsError)
    assert_sanitised(exc_info.value)
    assert "x=1" not in str(exc_info.value)
    assert token_manager.calls == []
    assert api.requests == []


async def test_request_always_hits_base_host(
    client: SocialSchoolsClient, api: FakeApi, api_server: TestClient
) -> None:
    """A whitelisted path always lands under the base URL on the base host."""
    await client.async_request("GET", "communityposts", school_id=SCHOOL_ID)
    (request,) = api.requests
    assert request.host == f"{api_server.host}:{api_server.port}"
    assert request.path == "/api/v1/communityposts"


@pytest.mark.parametrize(
    "path",
    ["//other.example/api/v1/events", "https://other.example/api/v1/events", "/elsewhere/events"],
)
async def test_url_guard_keeps_requests_on_base(
    client: SocialSchoolsClient,
    api: FakeApi,
    token_manager: FakeTokenManager,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Even with the whitelist loosened, a URL off the API base is refused."""
    monkeypatch.setattr(client_module, "ALLOWED_PATHS", (re.compile(r"^.*$", re.DOTALL),))
    with pytest.raises(RequestNotAllowed) as exc_info:
        await client.async_request("GET", path)
    assert_sanitised(exc_info.value)
    assert token_manager.calls == []
    assert api.requests == []


@pytest.mark.parametrize("base_url", ["api/v1/", "/api/v1/", "https://api.example/api/v1"])
async def test_base_url_must_be_absolute_with_trailing_slash(
    api_server: TestClient, token_manager: FakeTokenManager, base_url: str
) -> None:
    """A base URL that cannot be joined safely is rejected at construction."""
    with pytest.raises(ValueError, match="base_url"):
        SocialSchoolsClient(api_server.session, token_manager, base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.socialschools.eu/api/v1/",
        "http://api.example/api/v1/",
        "http://10.0.0.1/api/v1/",
        "http://127.0.0.2/api/v1/",
    ],
)
async def test_base_url_must_be_https_unless_loopback(
    api_server: TestClient, token_manager: FakeTokenManager, base_url: str
) -> None:
    """Plain http would send the bearer token in clear: refused for any host but loopback."""
    with pytest.raises(ValueError, match="https"):
        SocialSchoolsClient(api_server.session, token_manager, base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.socialschools.eu/api/v1/",
        "http://127.0.0.1:1/api/v1/",
        "http://localhost:1/api/v1/",
        "http://[::1]:1/api/v1/",
    ],
)
async def test_https_or_loopback_base_url_is_accepted(
    api_server: TestClient, token_manager: FakeTokenManager, base_url: str
) -> None:
    """https anywhere, and plain http on a loopback host (local test servers), construct fine."""
    SocialSchoolsClient(api_server.session, token_manager, base_url=base_url)


async def test_401_then_200_refreshes_once(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """A 401 triggers one forced refresh with the stale token and a retry."""
    api.reply(status=401, json={"error": "private-body"})
    api.reply(json={"values": [1]})
    assert await client.async_get_posts(SCHOOL_ID) == {"values": [1]}
    assert token_manager.calls == [(False, None), (True, "private-token-1")]
    assert token_manager.refreshes == 1
    first, second = api.requests
    assert first.headers["Authorization"] == "Bearer private-token-1"
    assert second.headers["Authorization"] == "Bearer private-token-2"
    assert second.path == first.path == "/api/v1/communityposts"
    assert second.query == first.query


async def test_401_twice_is_invalid_auth(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """A 401 that survives the refresh means the session is really gone."""
    api.reply(status=401, json={"error": "private-body"})
    with pytest.raises(InvalidAuth) as exc_info:
        await client.async_get_current_account()
    assert_sanitised(exc_info.value)
    assert token_manager.calls == [(False, None), (True, "private-token-1")]
    assert len(api.requests) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 409, 418, 429])
async def test_other_4xx_is_api_error(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager, status: int
) -> None:
    """Non-auth client errors become ApiError with the status, body never surfaced."""
    api.reply(status=status, json={"message": "private-body"})
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_post(SCHOOL_ID, 12)
    assert exc_info.value.status == status
    assert str(status) in str(exc_info.value)
    assert_sanitised(exc_info.value)
    assert token_manager.refreshes == 0
    assert len(api.requests) == 1


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_is_cannot_connect(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager, status: int
) -> None:
    """Server errors are transient: CannotConnect, no refresh, no retry."""
    api.reply(status=status, raw=b"<html>private-body</html>", content_type="text/html")
    with pytest.raises(CannotConnect) as exc_info:
        await client.async_get_posts(SCHOOL_ID)
    assert_sanitised(exc_info.value)
    assert token_manager.refreshes == 0
    assert len(api.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirect_is_cannot_connect_and_not_followed(
    client: SocialSchoolsClient, api: FakeApi, status: int
) -> None:
    """A redirect is never followed and counts as a connectivity problem."""
    api.reply(status=status, raw=b"", headers={"Location": "/api/v1/elsewhere"})
    with pytest.raises(CannotConnect) as exc_info:
        await client.async_get_current_account()
    assert_sanitised(exc_info.value)
    assert [request.path for request in api.requests] == ["/api/v1/useraccounts/current"]


async def test_timeout_is_cannot_connect(
    api_server: TestClient, base_url: str, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """A response that does not arrive within the timeout raises CannotConnect."""
    reply = api.reply(json={}, hold=asyncio.Event())
    client = SocialSchoolsClient(api_server.session, token_manager, base_url=base_url, timeout=0.05)
    with pytest.raises(CannotConnect) as exc_info:
        await client.async_get_current_account()
    assert reply.hold is not None
    reply.hold.set()
    assert_sanitised(exc_info.value)


async def test_connection_error_is_cannot_connect(
    token_manager: FakeTokenManager, socket_enabled: None
) -> None:
    """A refused connection raises CannotConnect."""
    # Bound but never listening: the kernel refuses connections to the port, and keeping the
    # socket open means nothing else can take the port while the request is made.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        async with aiohttp.ClientSession() as session:
            client = SocialSchoolsClient(
                session, token_manager, base_url=f"http://127.0.0.1:{port}/api/v1/", timeout=5
            )
            with pytest.raises(CannotConnect) as exc_info:
                await client.async_get_current_account()
    assert_sanitised(exc_info.value)


@pytest.mark.parametrize(
    ("status", "raw", "content_type"),
    [
        (200, b"<html>private-body</html>", "text/html"),
        (200, b"{not json: private-body", "application/json"),
        (200, b'{"values": [{"title": "private-body"', "application/json"),
        (200, b"[" * 100_000 + b"]" * 100_000, "application/json"),
        (200, b"", "application/json"),
        (204, b"", "application/json"),
    ],
    ids=["html", "not-json", "truncated", "deeply-nested", "empty", "no-content"],
)
async def test_invalid_json_is_api_error(
    client: SocialSchoolsClient, api: FakeApi, status: int, raw: bytes, content_type: str
) -> None:
    """A successful status without a usable JSON body is an ApiError with the status, not the body.

    The body must not survive on a chained JSONDecodeError (``.doc``), and JSON nested beyond
    MAX_JSON_DEPTH is an ApiError like any other invalid body, on every interpreter.
    """
    api.reply(status=status, raw=raw, content_type=content_type)
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_posts(SCHOOL_ID)
    assert exc_info.value.status == status
    assert_sanitised(exc_info.value)
    assert_no_body_in_chain(exc_info.value)


def test_every_whitelisted_path_has_a_response_shape() -> None:
    """The shape table and the whitelist describe exactly the same endpoints."""
    assert set(RESPONSE_SHAPES) == {pattern.pattern for pattern in ALLOWED_PATHS}
    assert RESPONSE_SHAPES[r"^events$"] == (dict, list)
    for pattern in (r"^useraccounts/current$", r"^communityposts$", r"^communityposts/\d+$"):
        assert RESPONSE_SHAPES[pattern] == (dict,)


async def test_whitelisted_path_without_a_shape_is_refused(
    client: SocialSchoolsClient,
    api: FakeApi,
    token_manager: FakeTokenManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitelisted path without a declared shape is refused before any token or network use."""
    monkeypatch.setattr(client_module, "RESPONSE_SHAPES", {})
    with pytest.raises(RequestNotAllowed) as exc_info:
        await client.async_get_current_account()
    assert_sanitised(exc_info.value)
    assert token_manager.calls == []
    assert api.requests == []


OBJECT_ENDPOINTS = [
    pytest.param(lambda c: c.async_get_current_account(), r"^useraccounts/current$", id="account"),
    pytest.param(lambda c: c.async_get_posts(SCHOOL_ID), r"^communityposts$", id="posts"),
    pytest.param(lambda c: c.async_get_post(SCHOOL_ID, 12), r"^communityposts/\d+$", id="post"),
]
SHAPE_START = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
SHAPE_END = SHAPE_START + timedelta(days=30)


@pytest.mark.parametrize(("call", "pattern"), OBJECT_ENDPOINTS)
@pytest.mark.parametrize(
    "value",
    [[], [{"id": 1, "title": "private-title"}], "private-text", 3, None, True],
    ids=["empty-list", "list", "string", "number", "null", "bool"],
)
async def test_object_endpoints_refuse_other_top_level_values(
    client: SocialSchoolsClient, api: FakeApi, call: Any, pattern: str, value: Any
) -> None:
    """Valid JSON that is not an object (a list included) is refused with the status only."""
    api.reply(raw=json.dumps(value).encode())
    with pytest.raises(ApiError) as exc_info:
        await call(client)
    assert str(exc_info.value) == f"GET {pattern}: unexpected JSON shape"
    assert exc_info.value.status == 200
    assert_sanitised(exc_info.value)
    assert_no_body_in_chain(exc_info.value)


@pytest.mark.parametrize(("call", "pattern"), OBJECT_ENDPOINTS)
async def test_object_endpoints_accept_an_object(
    client: SocialSchoolsClient, api: FakeApi, call: Any, pattern: str
) -> None:
    """A JSON object passes the shape check unchanged."""
    api.reply(json={"values": [], "id": 12})
    assert await call(client) == {"values": [], "id": 12}


@pytest.mark.parametrize(
    "value", ["private-text", 3, None, True], ids=["string", "number", "null", "bool"]
)
async def test_events_refuse_scalar_top_level_values(
    client: SocialSchoolsClient, api: FakeApi, value: Any
) -> None:
    """The calendar must answer with a list or an object."""
    api.reply(raw=json.dumps(value).encode())
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_events(SCHOOL_ID, SHAPE_START, SHAPE_END)
    assert str(exc_info.value) == "GET ^events$: unexpected JSON shape"
    assert exc_info.value.status == 200
    assert_sanitised(exc_info.value)


@pytest.mark.parametrize(
    "value",
    [[], [{"id": 1, "title": "x"}], {"values": [{"id": 1}]}],
    ids=["empty-list", "list", "values-object"],
)
async def test_events_accept_a_list_or_an_object(
    client: SocialSchoolsClient, api: FakeApi, value: Any
) -> None:
    """A list of events (the API's answer) and a ``{"values": [...]}`` object both pass."""
    api.reply(raw=json.dumps(value).encode())
    assert await client.async_get_events(SCHOOL_ID, SHAPE_START, SHAPE_END) == value


async def test_shape_is_checked_on_the_retried_response(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager
) -> None:
    """After the 401 retry the second body still has to have the declared shape."""
    api.reply(status=401)
    api.reply(raw=b"[]")
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_current_account()
    assert str(exc_info.value) == "GET ^useraccounts/current$: unexpected JSON shape"
    assert token_manager.refreshes == 1
    assert len(api.requests) == 2


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH, 100_000], ids=["one-over", "extreme"])
async def test_too_deep_json_is_refused_without_json_loads(
    client: SocialSchoolsClient, api: FakeApi, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    """Nesting beyond MAX_JSON_DEPTH is refused by the scan: json.loads never sees the body.

    The object around the arrays adds one level, so ``depth`` arrays nest one too deep.
    """

    def loads(_text: str, **_kwargs: Any) -> Any:
        raise AssertionError("json.loads must not run for this body")

    monkeypatch.setattr(payload_module, "json", SimpleNamespace(loads=loads))
    api.reply(raw=b'{"values":' + b"[" * depth + b"]" * depth + b"}")
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_posts(SCHOOL_ID)
    assert str(exc_info.value) == "GET ^communityposts$: invalid JSON"
    assert exc_info.value.status == 200
    assert_no_body_in_chain(exc_info.value)


async def test_json_at_the_depth_limit_is_accepted(
    client: SocialSchoolsClient, api: FakeApi
) -> None:
    """An object holding MAX_JSON_DEPTH - 1 nested arrays is exactly at the limit."""
    inner = b"[" * (MAX_JSON_DEPTH - 1) + b"]" * (MAX_JSON_DEPTH - 1)
    api.reply(raw=b'{"values":' + inner + b"}")
    result = await client.async_get_posts(SCHOOL_ID)
    assert isinstance(result, dict)
    assert isinstance(result["values"], list)


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_oversized_body_is_api_error(
    client: SocialSchoolsClient, api: FakeApi, token_manager: FakeTokenManager, chunked: bool
) -> None:
    """A 2xx body over MAX_API_BODY_BYTES is refused (before or while reading) as an ApiError."""
    api.reply(raw=b'{"values": ["' + b"x" * MAX_API_BODY_BYTES + b'"]}', chunked=chunked)
    with pytest.raises(ApiError) as exc_info:
        await client.async_get_posts(SCHOOL_ID)
    assert exc_info.value.status == 200
    assert "too large" in str(exc_info.value)
    assert_sanitised(exc_info.value)
    assert token_manager.refreshes == 0
    assert len(api.requests) == 1


async def test_body_of_exactly_the_limit_is_accepted(
    client: SocialSchoolsClient, api: FakeApi
) -> None:
    """The size limit is inclusive."""
    head, tail = b'{"values": ["', b'"]}'
    raw = head + b"x" * (MAX_API_BODY_BYTES - len(head) - len(tail)) + tail
    assert len(raw) == MAX_API_BODY_BYTES
    api.reply(raw=raw)
    result = await client.async_get_posts(SCHOOL_ID)
    assert len(result["values"][0]) == MAX_API_BODY_BYTES - len(head) - len(tail)


async def test_logs_never_contain_secrets(
    client: SocialSchoolsClient, api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging reports only neutral facts."""
    caplog.set_level(logging.DEBUG)
    api.reply(status=401, json={"error": "private-body"})
    api.reply(json={"values": []})
    await client.async_get_posts(SCHOOL_ID)
    messages = [
        record.getMessage() for record in caplog.records if record.name == client_module.__name__
    ]
    assert messages == [
        r"GET ^communityposts$: HTTP 401, retrying once with a renewed access token",
        r"GET ^communityposts$: HTTP 200",
    ]
    logged = "\n".join(messages)
    assert "private-" not in logged
    assert "Bearer" not in logged
    assert "offset=" not in logged
