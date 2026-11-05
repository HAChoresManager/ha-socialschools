"""Native-app login for Social Schools: PKCE authorization code via the ``/Account/Login`` form.

HA-free: only the standard library, ``aiohttp`` and ``yarl``. The caller supplies a
dedicated ``aiohttp.ClientSession`` with its own cookie jar; this module never creates
sessions, never follows redirects automatically and never executes page scripts.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import logging
import re
import secrets
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .const import (
    LOGIN_FORM_PATH,
    LOOPBACK_HOSTS,
    MAX_LOGIN_PAGE_BYTES,
    MAX_LOGIN_STEPS,
    OIDC_AUTHORIZE_PATH,
    OIDC_CALLBACK_PATH,
    OIDC_CLIENT_ID,
    OIDC_ISSUER,
    OIDC_REDIRECT_URI,
    OIDC_SCOPES,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .errors import CannotConnect, InvalidAuth, UnsupportedLogin

_LOGGER = logging.getLogger(__name__)

_FIELD_USERNAME = "Username"
_FIELD_PASSWORD = "Password"  # noqa: S105 - a form field name, not a secret
_FIELD_TOKEN = "__RequestVerificationToken"  # noqa: S105 - a form field name, not a secret
_FIELD_RETURN_URL = "ReturnUrl"
_FIELD_BUTTON = "button"
_LOGIN_FIELDS = frozenset({_FIELD_USERNAME, _FIELD_PASSWORD, _FIELD_TOKEN, _FIELD_RETURN_URL})
"""The inputs the login reads; a duplicate of one of them makes the form unusable."""

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REPLAYING_STATUSES = frozenset({307, 308})
_AUTH_FAILURE_STATUSES = frozenset({400, 401, 403})
_META_REFRESH_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of a successful login: the authorization code and the PKCE verifier it belongs to."""

    code: str = field(repr=False)
    code_verifier: str = field(repr=False)


def _base64url(raw: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _random_token() -> str:
    """Return 32 random bytes as base64url (used for state, nonce and the PKCE verifier)."""
    return _base64url(secrets.token_bytes(32))


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)``: 32 random bytes base64url, S256 challenge."""
    verifier = _random_token()
    return verifier, _base64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class _Form:
    """A ``<form>`` of the login page: method, action and ``name -> value`` of its inputs.

    A repeated input name keeps its first value and is listed in ``duplicates``.  Only the
    method is shown by ``repr()``: the action and the inputs may carry secrets.
    """

    method: str
    action: str = field(repr=False)
    inputs: dict[str, str] = field(default_factory=dict, repr=False)
    duplicates: set[str] = field(default_factory=set, repr=False)


def _parse_meta_refresh(content: str) -> str:
    """Extract the URL from a ``N;url=...`` refresh value (optional quotes) or raise UnsupportedLogin."""
    match = _META_REFRESH_RE.match(content)
    if match is None:
        raise UnsupportedLogin("login page has an unsupported meta refresh")
    target = match.group(1)
    if len(target) >= 2 and target[0] == target[-1] and target[0] in "\"'":
        target = target[1:-1].strip()
    if not target:
        raise UnsupportedLogin("login page has a meta refresh without a url")
    return target


class _LoginPageParser(HTMLParser):
    """Collect the forms and the first meta refresh of a page.

    Script and style contents and comments never reach the tag handlers; nothing is executed.
    """

    def __init__(self) -> None:
        """Start with no forms and no meta refresh."""
        super().__init__()
        self.forms: list[_Form] = []
        self.meta_refresh: str | None = None
        self._form: _Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record form, input and meta refresh tags; repeated attributes and inputs keep the first value."""
        if tag not in ("form", "input", "meta"):
            return
        attributes: dict[str, str] = {}
        for name, value in attrs:
            attributes.setdefault(name, value or "")
        if tag == "form":
            self._form = _Form(
                attributes.get("method", "").strip().lower(), attributes.get("action", "")
            )
            self.forms.append(self._form)
        elif tag == "input":
            name = attributes.get("name")
            if not name or self._form is None:
                return
            if name in self._form.inputs:
                self._form.duplicates.add(name)
            else:
                self._form.inputs[name] = attributes.get("value", "")
        elif (
            attributes.get("http-equiv", "").strip().lower() == "refresh"
            and self.meta_refresh is None
        ):
            self.meta_refresh = _parse_meta_refresh(attributes.get("content", ""))

    def handle_endtag(self, tag: str) -> None:
        """Close the current form."""
        if tag == "form":
            self._form = None


@dataclass(frozen=True)
class _Step:
    """The next request of the flow: a GET, or a POST of ``form`` when it is given.

    Neither field is shown by ``repr()``: the URL carries the authorize query, the form the password.
    """

    url: URL = field(repr=False)
    form: Mapping[str, str] | None = field(default=None, repr=False)


def _check_login_url(url: URL, issuer: URL) -> URL:
    """Return ``url`` when it lives on the issuer (scheme, host, port) without userinfo or fragment."""
    if (
        (url.scheme, url.host, url.port) != (issuer.scheme, issuer.host, issuer.port)
        or url.user is not None
        or url.password is not None
        or url.fragment
    ):
        raise UnsupportedLogin("login flow left the identity provider")
    return url


def _resolve(base: URL, value: str) -> URL:
    """Resolve a Location, action, ReturnUrl or meta refresh value against the current URL."""
    try:
        return base.join(URL(value))
    except ValueError:
        raise UnsupportedLogin("login flow produced a malformed url") from None


def _check_return_url(return_url: URL, authorize: Mapping[str, str]) -> None:
    """Require each authorize parameter exactly once with its value; extra parameters are ignored."""
    query = return_url.query
    if any(query.getall(key, []) != [value] for key, value in authorize.items()):
        raise UnsupportedLogin("login form does not return to our authorization request")


def _is_login_form(form: _Form) -> bool:
    """Whether a form is a POST form with the inputs the login needs."""
    return (
        form.method == "post"
        and _FIELD_USERNAME in form.inputs
        and _FIELD_PASSWORD in form.inputs
        and bool(form.inputs.get(_FIELD_TOKEN))
        and bool(form.inputs.get(_FIELD_RETURN_URL))
    )


def _page_step(
    page: str,
    current: URL,
    *,
    issuer: URL,
    authorize: Mapping[str, str],
    submitted: bool,
    username: str,
    password: str,
) -> _Step:
    """Decide the next request after a 200 page: follow its meta refresh or fill in the login form."""
    parser = _LoginPageParser()
    parser.feed(page)
    parser.close()
    if parser.meta_refresh is not None:
        # Any page on the issuer may follow; the next iteration validates what it returns.
        return _Step(_check_login_url(_resolve(current, parser.meta_refresh), issuer))
    if submitted:
        # The login form again means rejected credentials; any other page is a further step.
        if current.path == LOGIN_FORM_PATH:
            raise InvalidAuth("credentials rejected")
        raise UnsupportedLogin("an additional sign-in step is required")
    if current.path != LOGIN_FORM_PATH:
        raise UnsupportedLogin("unexpected page instead of the login form")
    form = next((candidate for candidate in parser.forms if _is_login_form(candidate)), None)
    if form is None:
        raise UnsupportedLogin("login form not found")
    if form.duplicates & _LOGIN_FIELDS:
        raise UnsupportedLogin("login form has a duplicate input")
    action = _check_login_url(_resolve(current, form.action), issuer)
    if action.path != LOGIN_FORM_PATH:
        raise UnsupportedLogin("login form posts to an unexpected page")
    return_url = _check_login_url(_resolve(current, form.inputs[_FIELD_RETURN_URL]), issuer)
    if return_url.path != OIDC_CALLBACK_PATH:
        raise UnsupportedLogin("login form returns to an unexpected page")
    _check_return_url(return_url, authorize)
    return _Step(
        action,
        {
            _FIELD_USERNAME: username,
            _FIELD_PASSWORD: password,
            _FIELD_RETURN_URL: form.inputs[_FIELD_RETURN_URL],
            _FIELD_TOKEN: form.inputs[_FIELD_TOKEN],
            _FIELD_BUTTON: "login",
        },
    )


def _parse_callback(url: URL, *, redirect: URL, state: str, issuer: str) -> str:
    """Validate the final native-app callback and return its authorization code."""
    query = url.query
    if (
        (url.scheme, url.path) != (redirect.scheme, redirect.path)
        or url.host is not None
        or url.user is not None
        or url.password is not None
        or url.fragment
        or "error" in query
    ):
        raise UnsupportedLogin("unexpected callback")
    values = {key: query.getall(key, []) for key in ("code", "state", "iss")}
    if any(len(found) != 1 for found in values.values()):
        raise UnsupportedLogin("callback parameters are missing or repeated")
    if values["state"][0] != state or values["iss"][0] != issuer or not values["code"][0]:
        raise UnsupportedLogin("callback validation failed")
    return values["code"][0]


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


def _decode_page(raw: bytes, charset: str | None) -> str:
    """Decode a page with its declared charset (UTF-8 by default), replacing undecodable bytes.

    A charset that is unknown or not a text encoding falls back to UTF-8 instead of raising.
    """
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", errors="replace")


async def async_native_login(
    session: aiohttp.ClientSession,
    username: str,
    password: str,
    *,
    issuer: str = OIDC_ISSUER,
    client_id: str = OIDC_CLIENT_ID,
    redirect_uri: str = OIDC_REDIRECT_URI,
    scopes: str = OIDC_SCOPES,
    user_agent: str = USER_AGENT,
    timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109 - signature fixed by the design contract
) -> AuthorizationResult:
    """Log in like the native app and return the authorization code with its PKCE verifier.

    ``session`` must be dedicated to this login (its own cookie jar); it is never closed here.
    ``issuer`` must be an https URL unless its host is a loopback address (ValueError otherwise).
    Raises InvalidAuth, UnsupportedLogin or CannotConnect; messages never carry secrets.
    """
    issuer_url = URL(issuer)
    if issuer_url.scheme != "https" and issuer_url.host not in LOOPBACK_HOSTS:
        raise ValueError("issuer must be an https URL (plain http only for loopback hosts)")
    redirect = URL(redirect_uri)
    code_verifier, code_challenge = generate_pkce()
    state = _random_token()
    authorize = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "nonce": _random_token(),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    step = _Step(issuer_url.with_path(OIDC_AUTHORIZE_PATH).with_query(authorize))
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    submitted = False
    for number in range(1, MAX_LOGIN_STEPS + 1):
        url = _check_login_url(step.url, issuer_url)
        method = "GET" if step.form is None else "POST"
        headers = {"Accept": "text/html", "User-Agent": user_agent}
        body: bytes | None = None
        if step.form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = issuer
            body = urlencode(step.form).encode()
            submitted = True
        try:
            async with session.request(
                method,
                url,
                data=body,
                headers=headers,
                allow_redirects=False,
                timeout=client_timeout,
            ) as response:
                status = response.status
                location = response.headers.get("Location")
                charset = response.charset
                raw = (
                    await _async_read_limited(response, MAX_LOGIN_PAGE_BYTES)
                    if status == 200
                    else b""
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("login step %d: %s", number, type(err).__name__)
            raise CannotConnect("could not reach the login service") from None
        _LOGGER.debug("login step %d: %s %s -> HTTP %d", number, method, url.path, status)
        if status in _REDIRECT_STATUSES:
            if status in _REPLAYING_STATUSES and step.form is not None:
                raise UnsupportedLogin("login service asked to resend the form")
            if location is None:
                raise UnsupportedLogin("login service redirected without a location")
            target = _resolve(url, location)
            if target.scheme == redirect.scheme:
                code = _parse_callback(target, redirect=redirect, state=state, issuer=issuer)
                _LOGGER.debug("login completed after %d steps", number)
                return AuthorizationResult(code=code, code_verifier=code_verifier)
            step = _Step(target)
        elif status == 200:
            if raw is None:
                raise UnsupportedLogin("login page is too large")
            step = _page_step(
                _decode_page(raw, charset),
                url,
                issuer=issuer_url,
                authorize=authorize,
                submitted=submitted,
                username=username,
                password=password,
            )
        elif status in _AUTH_FAILURE_STATUSES:
            raise InvalidAuth(f"login rejected with HTTP {status}")
        elif status >= 500:
            raise CannotConnect(f"login service failed with HTTP {status}")
        else:
            raise UnsupportedLogin(f"unexpected HTTP {status} during login")
    raise UnsupportedLogin("login did not complete within the step limit")
