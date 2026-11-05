"""Tests for the native login flow against a local stand-in identity provider."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
import hashlib
import html
import logging
import re
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient
from multidict import CIMultiDictProxy, MultiDict
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.socialschools.const import (
    LOGIN_FORM_PATH,
    MAX_LOGIN_PAGE_BYTES,
    MAX_LOGIN_STEPS,
    OIDC_AUTHORIZE_PATH,
    OIDC_CALLBACK_PATH,
    OIDC_REDIRECT_URI,
)
from custom_components.socialschools.errors import CannotConnect, InvalidAuth, UnsupportedLogin
from custom_components.socialschools.login import (
    AuthorizationResult,
    _LoginPageParser,
    _page_step,
    async_native_login,
    generate_pkce,
)

USERNAME = "private-username"
PASSWORD = "private-password"
CSRF_TOKEN = "private-csrf&value"
CODE = "private-code"
CSRF_COOKIE = ("csrf", "private-cookie")
AUTH_COOKIE = ("auth", "private-auth")
CLIENT_ID = "test-client"
SCOPES = "openid offline_access"
USER_AGENT = "test-agent/1.0"
BASE64URL_32_BYTES = re.compile(r"[A-Za-z0-9_-]{43}")
INTERSTITIAL_PATH = "/Account/Redirecting"
MFA_PATH = "/Account/Mfa"
MFA_PAGE = (
    '<html><body><form method="post" action="/Account/Mfa"><input name="Code">'
    '<button name="button" value="verify">Verify</button></form></body></html>'
)
# ASP.NET Core renders a checkbox as the box plus a hidden twin with the same name.
REMEMBER_LOGIN_INPUTS = (
    '<input type="checkbox" name="RememberLogin" value="true">'
    '<input name="RememberLogin" type="hidden" value="false">'
)

LOGIN_PAGE = (
    "<!DOCTYPE html><html><head><title>Sign in</title></head><body>"
    '<form method="post" action="">'
    '<input name="ReturnUrl" type="hidden" value="{return_url}">'
    '<input name="Username" type="text">'
    '<input name="Password" type="password">'
    '<input value="{token}" name="__RequestVerificationToken" type="hidden">'
    '<button name="button" value="login">Login</button>'
    "</form></body></html>"
)
FAKE_FORM = (
    '<form method="post" action="/elsewhere"><input name="Username"><input name="Password">'
    '<input name="__RequestVerificationToken" value="private-fake">'
    '<input name="ReturnUrl" value="/elsewhere"></form>'
    '<meta http-equiv="refresh" content="0;url=/elsewhere">'
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class Call:
    """One request received by a fake server."""

    method: str
    path: str
    query: MultiDict[str]
    headers: CIMultiDictProxy[str]
    cookies: dict[str, str]
    form: dict[str, str]


@dataclass
class ForeignHost:
    """A server on another port that must never receive a request."""

    url: str
    calls: list[str] = field(default_factory=list)


def redirect(
    location: str, *, status: int = 302, cookie: tuple[str, str] | None = None
) -> web.Response:
    """Build a redirect response, optionally setting a cookie."""
    response = web.Response(status=status, headers={"Location": location})
    if cookie is not None:
        response.set_cookie(*cookie, httponly=True)
    return response


def page(body: str, *, cookie: tuple[str, str] | None = None) -> web.Response:
    """Build an HTML page response, optionally setting a cookie."""
    response = web.Response(text=body, content_type="text/html")
    if cookie is not None:
        response.set_cookie(*cookie, httponly=True)
    return response


def meta_refresh(target: str, *, template: str = "0;url={url}") -> str:
    """Build a page whose only content is a meta refresh to ``target``."""
    content = html.escape(template.format(url=target))
    return f'<html><head><meta http-equiv="refresh" content="{content}"></head></html>'


def serve_page(
    body: Callable[[], bytes], *, content_type: str = "text/html", chunked: bool = False
) -> Handler:
    """Return a handler that serves ``body()`` verbatim under the given Content-Type header."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.Response(body=body(), headers={"Content-Type": content_type})
        if chunked:
            response.enable_chunked_encoding()
        return response

    return handler


def s256(verifier: str) -> str:
    """Return the PKCE S256 challenge of a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FakeIdp:
    """Stand-in identity provider: authorize redirect, login form, form post and callback."""

    client: TestClient

    def __init__(self) -> None:
        """Create a provider with the default (successful) behaviour."""
        self.issuer = ""
        self.calls: list[Call] = []
        self.authorize_query: MultiDict[str] = MultiDict()
        self.return_url = ""
        self.authorize_override: Handler | None = None
        self.login_page_override: Handler | None = None
        self.login_override: Handler | None = None
        self.form_mutator: Callable[[str], str] = lambda form: form
        self.callback_mutator: Callable[[URL], URL] = lambda url: url

    def app(self) -> web.Application:
        """Build the aiohttp application serving the provider."""

        @web.middleware
        async def record(request: web.Request, handler: Handler) -> web.StreamResponse:
            form = dict(await request.post()) if request.method == "POST" else {}
            self.calls.append(
                Call(
                    request.method,
                    request.path,
                    MultiDict(request.query),
                    request.headers,
                    dict(request.cookies),
                    form,
                )
            )
            return await handler(request)

        app = web.Application(middlewares=[record])
        app.router.add_get(OIDC_AUTHORIZE_PATH, self._authorize)
        app.router.add_get(LOGIN_FORM_PATH, self._login_page)
        app.router.add_post(LOGIN_FORM_PATH, self._login_post)
        app.router.add_get(OIDC_CALLBACK_PATH, self._callback)
        app.router.add_get(INTERSTITIAL_PATH, self._interstitial)
        app.router.add_get(MFA_PATH, self._second_step)
        return app

    def start(self, client: TestClient) -> None:
        """Remember the issuer URL and the client session once the server runs."""
        self.issuer = str(client.make_url("")).rstrip("/")
        self.client = client

    @property
    def requests(self) -> list[tuple[str, str]]:
        """Method and path of every request received so far."""
        return [(call.method, call.path) for call in self.calls]

    def login_page_html(self) -> str:
        """The login form page for the current authorization request."""
        return LOGIN_PAGE.format(
            return_url=html.escape(self.return_url), token=html.escape(CSRF_TOKEN)
        )

    def login_redirect(self) -> web.Response:
        """Redirect to the login page, as the provider does for an unauthenticated request."""
        return redirect(f"{LOGIN_FORM_PATH}?{urlencode({'ReturnUrl': self.return_url})}")

    async def _authorize(self, request: web.Request) -> web.StreamResponse:
        self.authorize_query = MultiDict(request.query)
        self.return_url = f"{OIDC_CALLBACK_PATH}?{request.query_string}"
        if self.authorize_override is not None:
            return await self.authorize_override(request)
        response = self.login_redirect()
        response.set_cookie(*CSRF_COOKIE, httponly=True)
        return response

    async def _login_page(self, request: web.Request) -> web.StreamResponse:
        if self.login_page_override is not None:
            return await self.login_page_override(request)
        return page(self.form_mutator(self.login_page_html()))

    async def _login_post(self, request: web.Request) -> web.StreamResponse:
        if self.login_override is not None:
            return await self.login_override(request)
        form = await request.post()
        accepted = (
            request.cookies.get(CSRF_COOKIE[0]) == CSRF_COOKIE[1]
            and form.get("__RequestVerificationToken") == CSRF_TOKEN
            and form.get("Username") == USERNAME
            and form.get("Password") == PASSWORD
        )
        if not accepted:
            return page(self.login_page_html())
        return page(meta_refresh(self.return_url), cookie=AUTH_COOKIE)

    async def _callback(self, request: web.Request) -> web.StreamResponse:
        if request.cookies.get(AUTH_COOKIE[0]) != AUTH_COOKIE[1]:
            return self.login_redirect()
        params = {"code": CODE, "state": request.query.get("state", ""), "iss": self.issuer}
        target = URL(f"{OIDC_REDIRECT_URI}?{urlencode(params)}")
        return redirect(str(self.callback_mutator(target)))

    async def _interstitial(self, request: web.Request) -> web.StreamResponse:
        """An intermediate issuer page that sends the browser on to the callback."""
        return redirect(self.return_url)

    async def _second_step(self, request: web.Request) -> web.StreamResponse:
        """A second sign-in step (a verification code form) after the password."""
        return page(MFA_PAGE)


@pytest.fixture
async def idp(aiohttp_client: ClientSessionGenerator, socket_enabled: None) -> FakeIdp:
    """A running fake identity provider."""
    provider = FakeIdp()
    provider.start(await aiohttp_client(provider.app()))
    return provider


@pytest.fixture
async def foreign(aiohttp_client: ClientSessionGenerator, socket_enabled: None) -> ForeignHost:
    """A running server on another port that records every request it receives."""
    host = ForeignHost(url="")

    async def record(request: web.Request) -> web.Response:
        host.calls.append(request.path)
        return web.Response(text="foreign")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", record)
    client = await aiohttp_client(app)
    host.url = str(client.make_url("")).rstrip("/")
    return host


@pytest.fixture(autouse=True)
def _secrets_never_logged(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Capture the login module at DEBUG and require that no secret marker was logged."""
    caplog.set_level(logging.DEBUG, logger="custom_components.socialschools.login")
    yield
    assert "private-" not in caplog.text


async def login(
    idp: FakeIdp, *, password: str = PASSWORD, timeout: float = 5
) -> AuthorizationResult:
    """Run the login under test against the fake provider."""
    return await async_native_login(
        idp.client.session,
        USERNAME,
        password,
        issuer=idp.issuer,
        client_id=CLIENT_ID,
        redirect_uri=OIDC_REDIRECT_URI,
        scopes=SCOPES,
        user_agent=USER_AGENT,
        timeout=timeout,
    )


async def assert_login_fails(
    idp: FakeIdp, error: type[Exception], *, password: str = PASSWORD, timeout: float = 5
) -> None:
    """Require the login to fail with ``error`` and a message free of secrets."""
    with pytest.raises(error) as excinfo:
        await login(idp, password=password, timeout=timeout)
    assert "private-" not in str(excinfo.value)


def fill(template: str, idp: FakeIdp, foreign: ForeignHost) -> str:
    """Fill the URL placeholders of a template with the running servers' addresses."""
    return template.format(
        issuer=idp.issuer,
        https_issuer=idp.issuer.replace("http://", "https://"),
        userinfo=idp.issuer.replace("://", "://user:private-password@"),
        foreign=foreign.url,
        foreign_hostport=foreign.url.removeprefix("http://"),
    )


def without(url: URL, key: str) -> URL:
    """Drop one query parameter from a URL."""
    return url.with_query(MultiDict((k, v) for k, v in url.query.items() if k != key))


def with_extra_code(url: URL) -> URL:
    """Repeat the ``code`` parameter."""
    query = MultiDict(url.query)
    query.add("code", "private-extra")
    return url.with_query(query)


def test_generate_pkce() -> None:
    """The verifier is 32 random bytes base64url and the challenge its S256 hash."""
    verifier, challenge = generate_pkce()
    assert BASE64URL_32_BYTES.fullmatch(verifier)
    assert challenge == s256(verifier)
    other_verifier, other_challenge = generate_pkce()
    assert other_verifier != verifier
    assert other_challenge != challenge


async def test_happy_path(idp: FakeIdp, caplog: pytest.LogCaptureFixture) -> None:
    """The whole flow: authorize, form, post with cookie and token, meta refresh, callback."""
    result = await login(idp)

    assert result.code == CODE
    assert s256(result.code_verifier) == idp.authorize_query["code_challenge"]
    assert "private-" not in repr(result)
    assert idp.requests == [
        ("GET", OIDC_AUTHORIZE_PATH),
        ("GET", LOGIN_FORM_PATH),
        ("POST", LOGIN_FORM_PATH),
        ("GET", OIDC_CALLBACK_PATH),
    ]
    for call in idp.calls:
        assert call.headers["Accept"] == "text/html"
        assert call.headers["User-Agent"] == USER_AGENT

    query = idp.authorize_query
    assert len(query) == 8
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == OIDC_REDIRECT_URI
    assert query["response_type"] == "code"
    assert query["scope"] == SCOPES
    assert query["code_challenge_method"] == "S256"
    assert BASE64URL_32_BYTES.fullmatch(query["state"])
    assert BASE64URL_32_BYTES.fullmatch(query["nonce"])
    assert BASE64URL_32_BYTES.fullmatch(query["code_challenge"])

    post = idp.calls[2]
    assert post.cookies == dict([CSRF_COOKIE])
    assert post.headers["Origin"] == idp.issuer
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert post.form == {
        "Username": USERNAME,
        "Password": PASSWORD,
        "ReturnUrl": idp.return_url,
        "__RequestVerificationToken": CSRF_TOKEN,
        "button": "login",
    }

    callback = idp.calls[3]
    assert callback.cookies == dict([CSRF_COOKIE, AUTH_COOKIE])
    assert callback.query == idp.authorize_query
    assert "login completed after 4 steps" in caplog.text


async def test_two_logins_use_fresh_state_nonce_and_challenge(idp: FakeIdp) -> None:
    """Every login generates new random state, nonce and PKCE values."""
    first = await login(idp)
    first_query = idp.authorize_query
    second = await login(idp)
    second_query = idp.authorize_query

    assert first.code_verifier != second.code_verifier
    for key in ("state", "nonce", "code_challenge"):
        assert first_query[key] != second_query[key]


async def test_post_answered_by_redirect(idp: FakeIdp) -> None:
    """An HTTP redirect after the form post is followed like the meta refresh."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return redirect(idp.return_url, cookie=AUTH_COOKIE)

    idp.login_override = login_override
    result = await login(idp)

    assert result.code == CODE
    assert idp.requests[2:] == [("POST", LOGIN_FORM_PATH), ("GET", OIDC_CALLBACK_PATH)]


@pytest.mark.parametrize("status", [301, 303, 307, 308])
async def test_redirect_statuses_after_get_are_followed(idp: FakeIdp, status: int) -> None:
    """Any redirect status after a GET leads to the next page."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        response = idp.login_redirect()
        response.set_status(status)
        response.set_cookie(*CSRF_COOKIE)
        return response

    idp.authorize_override = authorize_override
    result = await login(idp)

    assert result.code == CODE


async def test_meta_refresh_before_the_form(idp: FakeIdp) -> None:
    """A meta refresh to the callback before any form post is followed."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        return page(meta_refresh(idp.return_url), cookie=CSRF_COOKIE)

    idp.authorize_override = authorize_override
    result = await login(idp)

    assert result.code == CODE
    assert idp.requests == [
        ("GET", OIDC_AUTHORIZE_PATH),
        ("GET", OIDC_CALLBACK_PATH),
        ("GET", LOGIN_FORM_PATH),
        ("POST", LOGIN_FORM_PATH),
        ("GET", OIDC_CALLBACK_PATH),
    ]


@pytest.mark.parametrize(
    "template", ["0;url={url}", "0; URL='{url}'", '5 ; Url = "{url}"', "0.5;url={url}"]
)
async def test_meta_refresh_formats(idp: FakeIdp, template: str) -> None:
    """Meta refresh values are matched case-insensitively, with optional quotes and spaces."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return page(meta_refresh(idp.return_url, template=template), cookie=AUTH_COOKIE)

    idp.login_override = login_override
    result = await login(idp)

    assert result.code == CODE


async def test_wrong_password_is_invalid_auth(idp: FakeIdp) -> None:
    """A login page after the post means rejected credentials; the password is sent once."""
    await assert_login_fails(idp, InvalidAuth, password="private-wrong")

    assert idp.requests == [
        ("GET", OIDC_AUTHORIZE_PATH),
        ("GET", LOGIN_FORM_PATH),
        ("POST", LOGIN_FORM_PATH),
    ]
    assert idp.calls[2].form["Password"] == "private-wrong"


async def test_redirect_back_to_login_after_post_is_invalid_auth(idp: FakeIdp) -> None:
    """A redirect to the login page after the post is an extra step: InvalidAuth, no second post."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return idp.login_redirect()

    idp.login_override = login_override
    await assert_login_fails(idp, InvalidAuth)

    assert idp.requests[2:] == [("POST", LOGIN_FORM_PATH), ("GET", LOGIN_FORM_PATH)]


async def test_additional_step_after_the_password_is_unsupported(idp: FakeIdp) -> None:
    """A page other than the login form after the post is an extra step: UnsupportedLogin.

    The password was accepted but a verification code is asked for; that is not a wrong
    password, so the user must not be told to retry it.
    """

    async def login_override(request: web.Request) -> web.StreamResponse:
        return redirect(MFA_PATH, cookie=AUTH_COOKIE)

    idp.login_override = login_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests[2:] == [("POST", LOGIN_FORM_PATH), ("GET", MFA_PATH)]
    assert "Code" not in idp.calls[-1].form


@pytest.mark.parametrize(
    "body",
    [
        lambda idp: "<html><body><p>Maintenance</p></body></html>",
        lambda idp: idp.login_page_html(),
    ],
    ids=["no-form", "form-on-wrong-path"],
)
async def test_page_on_unexpected_path_is_unsupported(
    idp: FakeIdp, body: Callable[[FakeIdp], str]
) -> None:
    """A 200 page outside the login form path is never filled in."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        return page(body(idp))

    idp.authorize_override = authorize_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH)]


@pytest.mark.parametrize(
    "location",
    [
        "{foreign}/Account/Login",
        "//{foreign_hostport}/Account/Login",
        "{userinfo}/Account/Login",
        "{issuer}/Account/Login#private-fragment",
        "{https_issuer}/Account/Login",
    ],
    ids=["foreign-host", "scheme-relative-foreign", "userinfo", "fragment", "scheme-change"],
)
async def test_unsafe_authorize_redirect(idp: FakeIdp, foreign: ForeignHost, location: str) -> None:
    """A redirect off the issuer stops the flow before any further request."""
    target = fill(location, idp, foreign)

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        return redirect(target, cookie=CSRF_COOKIE)

    idp.authorize_override = authorize_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH)]
    assert foreign.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda url: url.update_query(state="private-wrong"),
        lambda url: without(url, "state"),
        lambda url: url.update_query(iss="https://login.example.test"),
        lambda url: without(url, "iss"),
        with_extra_code,
        lambda url: url.update_query(code=""),
        lambda url: without(url, "code"),
        lambda url: url.update_query(error="private-error"),
        lambda url: url.with_fragment("private-fragment"),
        lambda url: URL(str(url).replace("/oauthredirect", "/other")),
        lambda url: URL(str(url).replace(":/oauthredirect", "://host/oauthredirect")),
    ],
    ids=[
        "state-mismatch",
        "state-missing",
        "iss-mismatch",
        "iss-missing",
        "code-duplicate",
        "code-empty",
        "code-missing",
        "error",
        "fragment",
        "wrong-path",
        "host",
    ],
)
async def test_invalid_callback_is_rejected(idp: FakeIdp, mutate: Callable[[URL], URL]) -> None:
    """Callback validation failures end the flow without further requests."""
    idp.callback_mutator = mutate
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests[-1] == ("GET", OIDC_CALLBACK_PATH)
    assert len(idp.calls) == 4


@pytest.mark.parametrize(
    ("pattern", "replacement"),
    [
        (r"state=[^&]+", "state=private-wrong"),
        (r"nonce=[^&]+&amp;", ""),
        ("&amp;state=", "&amp;state=private-dup&amp;state="),
        (r'value="/connect/authorize/callback\?', 'value="/connect/other?'),
        (r'value="/connect/authorize/callback\?', 'value="{foreign}/connect/authorize/callback?'),
        (r'value="/connect/authorize/callback\?', 'value="{userinfo}/connect/authorize/callback?'),
        (r'value="/connect[^"]*"', 'value=""'),
        ('method="post"', 'method="get"'),
        ('name="Username"', 'name="user"'),
        ('name="__RequestVerificationToken"', 'name="unused"'),
        ('value="private-csrf&amp;value"', 'value=""'),
        ('action=""', 'action="/elsewhere"'),
        ('action=""', 'action="{issuer}/somewhere"'),
        ('action=""', 'action="{foreign}/Account/Login"'),
        ('action=""', 'action="{userinfo}/Account/Login"'),
        ('action=""', 'action="/Account/Login#private-fragment"'),
        ("</form>", '<input name="ReturnUrl" value="private-dup"></form>'),
        (r"<form.*</form>", "<p>no form</p>"),
    ],
    ids=[
        "returnurl-changed",
        "returnurl-missing-param",
        "returnurl-duplicate-param",
        "returnurl-other-path",
        "returnurl-foreign-host",
        "returnurl-userinfo",
        "returnurl-empty",
        "get-form",
        "no-username",
        "csrf-missing",
        "csrf-empty",
        "action-other-path",
        "action-other-absolute-path",
        "action-foreign-host",
        "action-userinfo",
        "action-fragment",
        "duplicate-input",
        "no-form",
    ],
)
async def test_unusable_form_is_never_posted(
    idp: FakeIdp, foreign: ForeignHost, pattern: str, replacement: str
) -> None:
    """A form that does not match our authorization request never receives the password."""
    idp.form_mutator = lambda form: re.sub(pattern, fill(replacement, idp, foreign), form)
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH), ("GET", LOGIN_FORM_PATH)]
    assert foreign.calls == []


async def test_return_url_with_extra_parameter_is_accepted(idp: FakeIdp) -> None:
    """A ReturnUrl that carries a parameter we did not send is fine as long as ours are intact.

    Like the reference app, only our own parameters are pinned (each exactly once, same value);
    the server's ReturnUrl is posted back verbatim.
    """
    idp.form_mutator = lambda form: form.replace("&amp;state=", "&amp;culture=nl-NL&amp;state=")
    result = await login(idp)

    assert result.code == CODE
    assert idp.calls[2].form["ReturnUrl"] == idp.return_url.replace(
        "&state=", "&culture=nl-NL&state="
    )
    assert idp.requests[-1] == ("GET", OIDC_CALLBACK_PATH)
    assert len(idp.calls) == 4


async def test_duplicate_input_outside_the_login_fields_is_ignored(idp: FakeIdp) -> None:
    """A repeated name such as a checkbox with its hidden twin does not make the form unusable."""
    idp.form_mutator = lambda form: (
        '<form method="post" action="/external"><input name="provider" value="a">'
        '<input name="provider" value="b"></form>'
        + form.replace("</form>", f"{REMEMBER_LOGIN_INPUTS}</form>")
    )
    result = await login(idp)

    assert result.code == CODE
    assert idp.calls[2].path == LOGIN_FORM_PATH
    assert "RememberLogin" not in idp.calls[2].form
    assert idp.calls[2].form["ReturnUrl"] == idp.return_url


@pytest.mark.parametrize("status", [307, 308])
async def test_redirect_after_post_is_not_replayed(idp: FakeIdp, status: int) -> None:
    """A 307/308 after the post would resend the password: refused, posted exactly once."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return redirect(LOGIN_FORM_PATH, status=status)

    idp.login_override = login_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [
        ("GET", OIDC_AUTHORIZE_PATH),
        ("GET", LOGIN_FORM_PATH),
        ("POST", LOGIN_FORM_PATH),
    ]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, InvalidAuth),
        (401, InvalidAuth),
        (403, InvalidAuth),
        (500, CannotConnect),
        (502, CannotConnect),
        (503, CannotConnect),
        (204, UnsupportedLogin),
        (404, UnsupportedLogin),
        (418, UnsupportedLogin),
        (302, UnsupportedLogin),
    ],
)
async def test_status_mapping(idp: FakeIdp, status: int, error: type[Exception]) -> None:
    """Auth failures, server errors and unexpected statuses (incl. a redirect without Location)."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        return web.Response(status=status)

    idp.authorize_override = authorize_override
    await assert_login_fails(idp, error)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH)]


async def test_timeout_is_cannot_connect(idp: FakeIdp) -> None:
    """A response that takes longer than the timeout is a connectivity failure."""
    release = asyncio.Event()

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        await release.wait()
        return web.Response()

    idp.authorize_override = authorize_override
    try:
        await assert_login_fails(idp, CannotConnect, timeout=0.2)
    finally:
        release.set()


async def test_dropped_connection_is_cannot_connect(idp: FakeIdp) -> None:
    """A connection closed by the server is a connectivity failure."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    idp.authorize_override = authorize_override
    await assert_login_fails(idp, CannotConnect)


async def test_redirect_loop_stops_at_step_limit(idp: FakeIdp) -> None:
    """An endless redirect stops after MAX_LOGIN_STEPS requests."""

    async def authorize_override(request: web.Request) -> web.StreamResponse:
        return redirect(OIDC_AUTHORIZE_PATH)

    idp.authorize_override = authorize_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert len(idp.calls) == MAX_LOGIN_STEPS


@pytest.mark.parametrize(
    "target",
    [
        "{foreign}/connect/authorize/callback?private-secret=1",
        "{userinfo}/connect/authorize/callback?x=1",
        "/connect/authorize/callback?x=1#private-fragment",
        "",
    ],
    ids=["foreign-host", "userinfo", "fragment", "empty"],
)
async def test_unsafe_meta_refresh_after_post(
    idp: FakeIdp, foreign: ForeignHost, target: str
) -> None:
    """A meta refresh off the issuer (or without a url) stops the flow after the post."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return page(meta_refresh(fill(target, idp, foreign)), cookie=AUTH_COOKIE)

    idp.login_override = login_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests[-1] == ("POST", LOGIN_FORM_PATH)
    assert len(idp.calls) == 3
    assert foreign.calls == []


@pytest.mark.parametrize(
    ("target", "error", "tail"),
    [
        (INTERSTITIAL_PATH, None, [("GET", INTERSTITIAL_PATH), ("GET", OIDC_CALLBACK_PATH)]),
        (LOGIN_FORM_PATH, InvalidAuth, [("GET", LOGIN_FORM_PATH)]),
        ("/elsewhere", UnsupportedLogin, [("GET", "/elsewhere")]),
    ],
    ids=["interstitial-then-callback", "back-to-the-login-form", "unknown-page"],
)
async def test_meta_refresh_to_another_issuer_page_is_followed(
    idp: FakeIdp, target: str, error: type[Exception] | None, tail: list[tuple[str, str]]
) -> None:
    """A meta refresh may lead to any issuer page (like the reference app); the next step decides.

    An interstitial that redirects on to the callback completes the login, the login form
    again is a rejected sign-in, and a page the issuer does not serve (404) is unsupported.
    """

    async def login_override(request: web.Request) -> web.StreamResponse:
        return page(meta_refresh(target), cookie=AUTH_COOKIE)

    idp.login_override = login_override
    if error is None:
        assert (await login(idp)).code == CODE
    else:
        await assert_login_fails(idp, error)

    assert idp.requests[:3] == [
        ("GET", OIDC_AUTHORIZE_PATH),
        ("GET", LOGIN_FORM_PATH),
        ("POST", LOGIN_FORM_PATH),
    ]
    assert idp.requests[3:] == tail


async def test_unparsable_meta_refresh_is_unsupported(idp: FakeIdp) -> None:
    """A refresh value that is not ``N;url=...`` is an unknown page."""

    async def login_override(request: web.Request) -> web.StreamResponse:
        return page(meta_refresh(idp.return_url, template="private-garbage {url}"))

    idp.login_override = login_override
    await assert_login_fails(idp, UnsupportedLogin)

    assert len(idp.calls) == 3


async def test_forms_in_script_style_and_comments_are_ignored(idp: FakeIdp) -> None:
    """Only real markup counts: fake forms and refreshes in scripts, styles and comments are skipped."""
    noise = f"<script>var x = '{FAKE_FORM}';</script><style>/* {FAKE_FORM} */</style><!-- {FAKE_FORM} -->"
    idp.form_mutator = lambda form: form.replace("<body>", f"<body>{noise}")
    result = await login(idp)

    assert result.code == CODE
    assert idp.calls[2].path == LOGIN_FORM_PATH
    assert idp.calls[2].form["ReturnUrl"] == idp.return_url


async def test_only_fake_forms_is_unsupported(idp: FakeIdp) -> None:
    """A page whose only forms sit inside a script or a comment has no login form."""
    idp.form_mutator = lambda form: re.sub(
        r"<form.*</form>", f"<script>{FAKE_FORM}</script><!-- {FAKE_FORM} -->", form
    )
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH), ("GET", LOGIN_FORM_PATH)]


async def test_single_quotes_and_entities_are_decoded(idp: FakeIdp) -> None:
    """Attribute quoting styles, upper-case names and numeric entities are handled."""
    idp.form_mutator = lambda form: (
        form.replace('method="post"', "METHOD='POST'")
        .replace('action=""', "ACTION=''")
        .replace('name="Username"', "name='Username'")
        .replace("private-csrf&amp;value", "private-csrf&#38;value")
        .replace("&amp;", "&#x26;")
    )
    result = await login(idp)

    assert result.code == CODE
    assert idp.calls[2].form["__RequestVerificationToken"] == CSRF_TOKEN
    assert idp.calls[2].form["ReturnUrl"] == idp.return_url


@pytest.mark.parametrize(
    ("charset", "encoding"),
    [
        ("utf-8", "utf-8"),
        ("UTF-16", "utf-16"),
        ("iso-8859-1", "iso-8859-1"),
        ("rot13", "utf-8"),
        ("idna", "utf-8"),
        ("hex", "utf-8"),
        ("x-unknown-charset", "utf-8"),
    ],
    ids=["utf-8", "utf-16", "latin-1", "rot13", "idna", "hex", "unknown"],
)
async def test_login_page_charset_is_honoured_or_falls_back(
    idp: FakeIdp, charset: str, encoding: str
) -> None:
    """The declared charset decodes the page; one that is unknown or not a text encoding means UTF-8.

    ``rot13``/``hex`` raise LookupError and ``idna`` UnicodeError from ``bytes.decode``; neither
    may escape the login as an unmapped exception.
    """
    idp.login_page_override = serve_page(
        lambda: f"<title>Caf\u00e9</title>{idp.login_page_html()}".encode(encoding),
        content_type=f"text/html; charset={charset}",
    )
    result = await login(idp)

    assert result.code == CODE
    assert idp.calls[2].form["__RequestVerificationToken"] == CSRF_TOKEN


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
async def test_oversized_login_page_is_unsupported(idp: FakeIdp, chunked: bool) -> None:
    """A page over MAX_LOGIN_PAGE_BYTES is refused (before or while reading) and never filled in."""
    idp.login_page_override = serve_page(
        lambda: idp.login_page_html().encode() + b"<!--" + b"x" * MAX_LOGIN_PAGE_BYTES + b"-->",
        chunked=chunked,
    )
    await assert_login_fails(idp, UnsupportedLogin)

    assert idp.requests == [("GET", OIDC_AUTHORIZE_PATH), ("GET", LOGIN_FORM_PATH)]


async def test_login_page_of_exactly_the_limit_is_accepted(idp: FakeIdp) -> None:
    """The size limit is inclusive: a page of MAX_LOGIN_PAGE_BYTES bytes still logs in."""

    def body() -> bytes:
        markup = idp.login_page_html().encode()
        return markup + b"<!--" + b"x" * (MAX_LOGIN_PAGE_BYTES - len(markup) - 7) + b"-->"

    idp.login_page_override = serve_page(body)
    result = await login(idp)

    assert result.code == CODE
    assert len(body()) == MAX_LOGIN_PAGE_BYTES


@pytest.mark.parametrize(
    "issuer", ["http://login.example.test", "http://10.0.0.1:8080", "http://127.0.0.2"]
)
async def test_plain_http_issuer_is_rejected_before_any_request(idp: FakeIdp, issuer: str) -> None:
    """Only https issuers are allowed, except loopback hosts for local test servers."""
    with pytest.raises(ValueError, match="https"):
        await async_native_login(idp.client.session, USERNAME, PASSWORD, issuer=issuer)

    assert idp.calls == []


def test_internal_step_and_form_hide_urls_and_inputs() -> None:
    """The parsed forms and the next step never print URLs with query strings or input values."""
    issuer = URL("https://idp.example.test")
    authorize = {"client_id": CLIENT_ID, "state": "private-state", "nonce": "private-nonce"}
    return_url = f"{OIDC_CALLBACK_PATH}?{urlencode(authorize)}"
    action = f"{LOGIN_FORM_PATH}?{urlencode({'ReturnUrl': return_url})}"
    markup = LOGIN_PAGE.format(
        return_url=html.escape(return_url), token=html.escape(CSRF_TOKEN)
    ).replace('action=""', f'action="{html.escape(action)}"')
    parser = _LoginPageParser()
    parser.feed(markup)
    parser.close()
    step = _page_step(
        markup,
        URL(f"{issuer}{action}"),
        issuer=issuer,
        authorize=authorize,
        submitted=False,
        username=USERNAME,
        password=PASSWORD,
    )

    assert step.url == URL(f"{issuer}{action}")
    assert step.form is not None
    assert step.form["Password"] == PASSWORD
    assert step.form["ReturnUrl"] == return_url
    for text in (repr(step), str(step), repr(parser.forms), str(parser.forms)):
        assert "private-" not in text
        assert "?" not in text
