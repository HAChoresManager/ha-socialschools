# Design contract: Social Schools for Home Assistant

This document is the binding contract between the modules of the
`socialschools` custom integration. Implementers must follow the names,
signatures and behaviours below. Where this document and the Home Assistant
source disagree, the installed Home Assistant source wins and this document
must be updated.

Everything is **read-only**: the integration never performs a write against
Social Schools and never calls an endpoint with side effects (no inboxes,
conversations, messages, statistics, read-registration endpoints).

## 1. Layout

```
custom_components/socialschools/
  __init__.py          HA: async_setup_entry / async_unload_entry / async_remove_entry
  manifest.json
  const.py             PURE: domain, protocol constants, option keys, limits
  errors.py            PURE: exception hierarchy
  payload.py           PURE: bounded body reads + JSON decoding with a depth limit (shared)
  login.py             PURE: native app login (PKCE + /Account/Login form) and code exchange
  tokens.py            PURE: TokenManager (refresh, expiry, rotation callback)
  client.py            PURE: SocialSchoolsClient (GET whitelist, headers, 401 retry)
  models.py            PURE: dataclasses + JSON parsing + date helpers
  textutil.py          PURE: html_to_text, looks_like_html, truncate
  coordinator.py       HA: SocialSchoolsCoordinator + new-post detection + Store
  entity.py            HA: SocialSchoolsEntity base (device per school, availability)
  config_flow.py       HA: user / reauth / options flows
  sensor.py            HA: latest_post + post_count
  event.py             HA: new_post event entity
  calendar.py          HA: school calendar
  diagnostics.py       HA: redacted diagnostics
  strings.json
  translations/nl.json
  translations/en.json
scripts/probe.py       live protocol check, reuses the PURE modules only
scripts/release.py     CI helper: missing tags and GitHub releases (§17), standard library only
tests/                 pytest (pytest-homeassistant-custom-component)
.github/workflows/     validate.yml (hassfest, HACS, tests) and release.yml (§17)
```

### PURE modules

`const.py`, `errors.py`, `payload.py`, `login.py`, `tokens.py`, `client.py`,
`models.py` and `textutil.py`:

* import nothing from `homeassistant` (directly or indirectly), only the
  standard library, `aiohttp`, `yarl` and `multidict`;
* use only syntax that runs on Python 3.12 (no PEP 695 `type` statements or
  generic syntax, no `except*` tricks); `from __future__ import annotations`
  is fine;
* import each other only with explicit relative imports (`from .const import
  ...`) so that `scripts/probe.py` can load them under a stand-in package
  name without executing `__init__.py` (see §13).

A test (`tests/test_pure_modules.py`) imports them in a subprocess with a
meta-path finder that raises on any `homeassistant` import.

## 2. const.py

```python
DOMAIN = "socialschools"
VERSION = "0.2.2"                       # must equal manifest.json "version" (tested)
USER_AGENT = f"HomeAssistant-SocialSchools/{VERSION}"

OIDC_ISSUER = "https://login.socialschools.eu"
OIDC_CLIENT_ID = "eu.socialschools.ios"
OIDC_REDIRECT_URI = "eu.socialschools.ios:/oauthredirect"
OIDC_SCOPES = "openid profile SocsWebApi email offline_access SocsNotificationApi"
OIDC_AUTHORIZE_PATH = "/connect/authorize"
OIDC_CALLBACK_PATH = "/connect/authorize/callback"
OIDC_TOKEN_PATH = "/connect/token"
LOGIN_FORM_PATH = "/Account/Login"
MAX_LOGIN_STEPS = 10

API_BASE_URL = "https://api.socialschools.eu/api/v1/"
ROLE_TYPE_PARENT = 1
FEED_LIMIT = 20
FEED_FILTER_TYPE = 0
REQUEST_TIMEOUT = 30                     # seconds, every HTTP call
TOKEN_REFRESH_LEEWAY = 60                # seconds before expiry at which we refresh
DEFAULT_EXPIRES_IN = 3600
MAX_EXPIRES_IN = 10 * 365 * 86400        # seconds; a larger expires_in is clamped to this
MAX_LOGIN_PAGE_BYTES = 1_048_576         # 1 MiB; a larger login page → UnsupportedLogin
MAX_TOKEN_BODY_BYTES = 1_048_576         # 1 MiB; a larger token response → ApiError
MAX_API_BODY_BYTES = 8 * 1_048_576       # 8 MiB; a larger API response → ApiError
MAX_JSON_DEPTH = 64                      # deepest array/object nesting decode_json accepts
MAX_JSON_INT_DIGITS = 640                # longest integer literal (digits, no sign) decode_json accepts
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})  # plain http only for these hosts

CONF_USERNAME = "username"
CONF_PASSWORD = "password"               # only used inside flows, never stored
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CLIENT_ID = "client_id"
CONF_USER_PROFILE_ID = "user_profile_id"

OPT_POSTS_INTERVAL = "posts_interval"    # minutes
OPT_CALENDAR_DAYS = "calendar_days"      # days ahead
OPT_POSTS_IN_LIST = "posts_in_list"      # entries in the `berichten` attribute
DEFAULT_POSTS_INTERVAL = 15
MIN_POSTS_INTERVAL = 10
MAX_POSTS_INTERVAL = 1440
DEFAULT_CALENDAR_DAYS = 90
MIN_CALENDAR_DAYS = 7
MAX_CALENDAR_DAYS = 365
DEFAULT_POSTS_IN_LIST = 10
MIN_POSTS_IN_LIST = 1
MAX_POSTS_IN_LIST = 20

CALENDAR_LOOKBACK_DAYS = 7
CALENDAR_REFRESH_INTERVAL = timedelta(hours=1)
ACCOUNT_REFRESH_INTERVAL = timedelta(days=1)   # children and their groups (0.2.2, §18)
ACCOUNT_RETRY_INTERVAL = timedelta(hours=1)    # after a failed account fetch
POST_COUNT_WINDOW = timedelta(days=7)
SCHOOL_TIMEZONE = "Europe/Amsterdam"

MAX_TITLE_LENGTH = 255
MAX_TEXT_LENGTH = 2000
MAX_SHORT_TEXT_LENGTH = 300
MAX_LIST_TITLE_LENGTH = 120
MAX_LIST_GROUP_LENGTH = 120
MAX_GROUP_LENGTH = 255                   # `groep` of the latest-post sensor
MAX_AUTHOR_LENGTH = 120                  # `auteur` everywhere
MAX_ATTACHMENTS = 20                     # entries of `bijlagen`
MAX_ATTACHMENT_NAME_LENGTH = 120         # characters per entry of `bijlagen`
ATTRIBUTES_BYTE_BUDGET = 12_000          # JSON bytes of all attributes of one entity

EVENT_TYPE_NEW_POST = "nieuw_bericht"
STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "socialschools.{entry_id}"
SEEN_IDS_LIMIT = 500
NEW_POST_GRACE = timedelta(days=7)
FETCH_POST_DETAILS = False               # flip to True if the probe shows the feed body is truncated
MAX_DETAIL_FETCHES_PER_UPDATE = 20
```

Attribute keys (Dutch, fixed, never translated):

```python
ATTR_POST_ID = "post_id"; ATTR_GROUP = "groep"; ATTR_AUTHOR = "auteur"
ATTR_POSTED = "geplaatst"; ATTR_TEXT = "tekst"; ATTR_TRUNCATED = "afgekapt"
ATTR_ATTACHMENTS = "bijlagen"; ATTR_COMMENTS = "reacties"; ATTR_TITLE = "titel"
ATTR_SHORT_TEXT = "tekst_kort"; ATTR_POSTS = "berichten"
```

## 3. errors.py

```python
class SocialSchoolsError(Exception): """Base. Messages never contain secrets or response bodies."""
class CannotConnect(SocialSchoolsError): """Network error, timeout, DNS failure or HTTP 5xx."""
class InvalidAuth(SocialSchoolsError): """Wrong credentials, login 400/401/403, refresh 400/401, or a 401 that persists after a forced refresh."""
class UnsupportedLogin(SocialSchoolsError): """Unknown page, unexpected redirect, extra sign-in step, callback validation failure."""
class ApiError(SocialSchoolsError):
    """Unexpected HTTP status (4xx other than auth) or invalid JSON. Has `status: int | None`."""
class RequestNotAllowed(SocialSchoolsError): """Whitelist violation (method, path, host)."""
```

Mapping in the HA layer:

| pure exception | config flow error key | setup / update |
|---|---|---|
| `InvalidAuth` | `invalid_auth` | `ConfigEntryAuthFailed` (→ reauth) |
| `UnsupportedLogin` | `unsupported` | n/a (login only happens in flows) |
| `CannotConnect` | `cannot_connect` | `ConfigEntryNotReady` at setup, `UpdateFailed` in the coordinator |
| `ApiError` | `cannot_connect` | `ConfigEntryNotReady` at setup, `UpdateFailed` in the coordinator |
| `RequestNotAllowed` | programming error, never caught | |
| no parent role | `no_parent_role` | `ConfigEntryError` (translation key `no_parent_role`) |

A transient failure (`CannotConnect`, `ApiError`) must never start a reauth
flow and never stop polling.

## 4. login.py (PURE)

```python
@dataclass(frozen=True)
class AuthorizationResult:
    code: str
    code_verifier: str

def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge): 32 random bytes, base64url without padding; S256 challenge."""

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
    timeout: float = REQUEST_TIMEOUT,
) -> AuthorizationResult: ...

```

`login.py` imports only `const` and `errors` (not `tokens`). The code exchange
lives in `tokens.py` (§5).

`async_native_login` algorithm (the caller passes a **dedicated** session with
its own `aiohttp.CookieJar()`; the function never creates sessions):

1. `issuer` must be an `https` URL unless its host is in `LOOPBACK_HOSTS`
   (local test servers); anything else raises `ValueError` before any request.
   Build the authorize URL `{issuer}/connect/authorize` with query
   `client_id, redirect_uri, response_type=code, scope, state, nonce,
   code_challenge, code_challenge_method=S256`. `state` and `nonce` are 32
   random bytes base64url each.
2. Loop at most `MAX_LOGIN_STEPS` requests. Every request: `allow_redirects=False`,
   headers `Accept: text/html`, `User-Agent`. Before each request the target URL
   must satisfy `_check_login_url`: scheme/host/port equal to the issuer's,
   no userinfo, no fragment → otherwise `UnsupportedLogin`.
3. Status handling:
   * 301/302/303: follow `Location` (resolved against the current URL) with GET.
     A `Location` whose scheme equals the scheme of `redirect_uri`
     (`eu.socialschools.ios`) is the final callback (step 6).
     Missing `Location` → `UnsupportedLogin`.
   * 307/308 after the POST → `UnsupportedLogin` (never replay the password);
     307/308 after a GET: follow like 302.
   * 200: read the body up to `MAX_LOGIN_PAGE_BYTES` (`Content-Length` is
     checked first, then at most limit + 1 bytes are read); a larger page →
     `UnsupportedLogin`. Decode it with the charset of `Content-Type` (UTF-8
     when absent, unknown or not a text encoding; undecodable bytes are
     replaced), then parse it (step 4/5).
   * 400/401/403 → `InvalidAuth`.
   * 5xx → `CannotConnect`. Anything else → `UnsupportedLogin`.
   * `aiohttp.ClientError`, `asyncio.TimeoutError` → `CannotConnect`.
4. A 200 page is parsed with `html.parser.HTMLParser` (`_LoginPageParser`):
   contents of `script`/`style` and comments are ignored, nothing is executed.
   It records `<meta http-equiv="refresh" content="N;url=...">` (case-insensitive,
   optional quotes around the url) and every `<form>` with its `method`,
   `action` and `<input name value>` pairs; a repeated input name keeps its
   first value and is recorded as a duplicate.
   * If a meta refresh is present: its URL must pass `_check_login_url`; the
     next step is a GET to it, whatever its path, and the next iteration
     validates whatever comes back (page, redirect or callback).
   * Else, if the form has already been submitted: the login form page again
     (path `LOGIN_FORM_PATH`) → `InvalidAuth` (wrong password); any other
     page → `UnsupportedLogin` (an additional sign-in step such as a
     verification code).
   * Else the page path must be `LOGIN_FORM_PATH`, otherwise `UnsupportedLogin`.
     Find the first POST form whose inputs contain `Username`, `Password`,
     a non-empty `__RequestVerificationToken` and a non-empty `ReturnUrl`.
     A duplicate of one of these four inputs inside that form →
     `UnsupportedLogin`; other duplicated names (e.g. a checkbox with its
     hidden twin) are ignored. Form `action` (empty = current URL) must
     resolve to the issuer with path `LOGIN_FORM_PATH`. `ReturnUrl` must
     resolve to the issuer with path `OIDC_CALLBACK_PATH` and contain each of
     the authorize query parameters exactly once with an identical value;
     additional parameters are ignored. Anything else → `UnsupportedLogin`.
5. POST the form (`application/x-www-form-urlencoded`) with
   `Username, Password, ReturnUrl, __RequestVerificationToken, button=login`,
   header `Origin: {issuer}`. Mark submitted.
6. Final callback validation (`yarl.URL(location)`): scheme and path must
   equal `redirect_uri` (`eu.socialschools.ios:/oauthredirect`), no host,
   no userinfo, no fragment, no `error`, exactly one `code`, `state`, `iss`,
   `state` equal to ours, `iss` equal to `issuer`, `code` non-empty →
   `AuthorizationResult`. Otherwise `UnsupportedLogin`.
7. Loop exhausted → `UnsupportedLogin`.

Never log or include in exception messages: username, password, cookies,
tokens, code, verifier, state, URLs with query strings, or page bodies. The
internal `_Step` and `_Form` objects hide their URL/action and inputs from
`repr()` for the same reason.

## 5. tokens.py (PURE)

```python
@dataclass(frozen=True)
class TokenResponse:
    access_token: str = field(repr=False)          # never shown by repr()/str()
    refresh_token: str | None = field(repr=False)  # never shown by repr()/str()
    expires_in: int              # seconds; DEFAULT_EXPIRES_IN when missing or invalid, at most MAX_EXPIRES_IN
                                 # (a string longer than MAX_JSON_INT_DIGITS characters is invalid)

RefreshTokenCallback = Callable[[str], Awaitable[None] | None]

async def async_exchange_code(
    session, code: str, code_verifier: str, *,
    issuer=OIDC_ISSUER, client_id=OIDC_CLIENT_ID, redirect_uri=OIDC_REDIRECT_URI,
    user_agent=USER_AGENT, timeout=REQUEST_TIMEOUT,
) -> TokenResponse:
    """POST {issuer}/connect/token form: grant_type=authorization_code, code, code_verifier,
    redirect_uri, client_id. Raises InvalidAuth on 400/401/403, CannotConnect on network/timeout/5xx,
    ApiError on other statuses, invalid JSON (payload.decode_json) or a top-level value that is not an
    object ("unexpected JSON shape"). A response without a non-empty refresh_token raises
    ApiError (message: no refresh token). access_token must be a non-empty string without control
    characters, else ApiError."""

async def async_refresh_tokens(session, refresh_token: str, *, issuer=OIDC_ISSUER,
                               client_id=OIDC_CLIENT_ID, user_agent=USER_AGENT,
                               timeout=REQUEST_TIMEOUT) -> TokenResponse:
    """POST {issuer}/connect/token form grant_type=refresh_token, refresh_token, client_id.
    400/401 → InvalidAuth; network/timeout/5xx → CannotConnect; other → ApiError."""

class TokenManager:
    def __init__(self, session, *, client_id: str, refresh_token: str,
                 access_token: str | None = None, expires_at: float | None = None,
                 issuer: str = OIDC_ISSUER, user_agent: str = USER_AGENT,
                 timeout: float = REQUEST_TIMEOUT,
                 on_refresh_token_update: RefreshTokenCallback | None = None,
                 clock: Callable[[], float] | None = None) -> None: ...
        # without clock: time.monotonic() looked up at every use (a patched clock is honoured)
    @property
    def refresh_token(self) -> str: ...
    @property
    def expires_at(self) -> float | None: ...
    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token. Refresh when there is none, when it expires within
        TOKEN_REFRESH_LEEWAY seconds, or when force_refresh. Serialised with an asyncio.Lock;
        a forced refresh that finds a token newer than the one the caller saw does not refresh again
        (pass the token the caller used: see async_get_access_token(force_refresh=True, stale=<token>))."""
    async def async_refresh(self) -> TokenResponse:
        """Unconditional refresh. Updates access token and expiry (clock() + expires_in). If the
        response carries a different non-empty refresh_token, store it and call
        on_refresh_token_update(new_token) (await it if it returns an awaitable). A response without
        refresh_token keeps the old one."""
    async def async_wait_for_refresh(self) -> None:
        """Wait until a refresh that is in flight has finished; its outcome is not raised here."""
```

Token endpoint bodies are read up to `MAX_TOKEN_BODY_BYTES` (`Content-Length`
first, then at most limit + 1 bytes; a larger 200 body → `ApiError`). The body
is decoded with `payload.decode_json` (§6a): a body that is not UTF-8, nests
deeper than `MAX_JSON_DEPTH`, holds an integer literal of more than
`MAX_JSON_INT_DIGITS` digits or is not JSON → `ApiError` "invalid JSON"; no
decode error (whose `.doc` holds the body) is chained as
`__cause__`/`__context__`. A string `expires_in` is converted only when it has
at most `MAX_JSON_INT_DIGITS` characters, so its result does not depend on the
interpreter's `int_max_str_digits` setting either. `async_refresh` computes the
new expiry before storing the access token, so the pair changes together.

The refresh request runs in its own task (`asyncio.create_task`) that the
caller awaits with `asyncio.wait`, so a caller that is cancelled mid-request
(the coordinator is cancelled on every reload and shutdown) leaves the
request running: it completes, stores the tokens and reports a rotation. A
later caller joins a refresh that is still running; a finished one (failed
or not) is never reused. The task's exception is marked retrieved, so the
event loop never reports it.

The exact `stale` mechanism: `async_get_access_token(force_refresh=False, stale: str | None = None)`;
with `force_refresh=True` and `stale` equal to the current token, refresh; if the current
token already differs from `stale`, return it without refreshing.

The HA layer passes a sync `@callback` that writes the rotated refresh token
to the config entry with `hass.config_entries.async_update_entry` (ignoring
`UnknownEntry`: the entry may have been removed while the refresh ran), and
`async_unload_entry` awaits `async_wait_for_refresh()` before unloading the
platforms, so that a refresh token rotated by a refresh that was still
running is stored before a reload starts over from the stored one.

## 6. client.py (PURE)

```python
ALLOWED_PATHS: tuple[re.Pattern, ...]  # ^useraccounts/current$, ^communityposts$, ^communityposts/\d+$, ^events$
RESPONSE_SHAPES: Mapping[str, tuple[type, ...]]  # accepted top-level JSON types, keyed by pattern source:
    # useraccounts/current, communityposts, communityposts/{id} → (dict,); events → (dict, list)

class SocialSchoolsClient:
    def __init__(self, session, token_manager: TokenManager, *, base_url: str = API_BASE_URL,
                 role_type: int = ROLE_TYPE_PARENT, user_agent: str = USER_AGENT,
                 timeout: float = REQUEST_TIMEOUT) -> None:
        """base_url must be absolute, end with '/' and use https unless its host is in LOOPBACK_HOSTS
        (local test servers); otherwise ValueError."""
    async def async_request(self, method: str, path: str, *, school_id: int | None = None,
                            params: Mapping[str, str] | None = None) -> Any:
        """The only place that talks to the API. Enforces: method == "GET"; path relative, no leading
        slash, no '?', no '..', matches ALLOWED_PATHS; the final URL has scheme https and host/port of
        base_url. Otherwise raises RequestNotAllowed BEFORE any network or token access.
        Headers: Authorization: Bearer <token>, Accept: application/json, User-Agent; when school_id
        is given also Schoolid: <id> and Roletypeid: <role_type>. allow_redirects=False.
        On 401: once, get a token with force_refresh=True (stale=<token used>) and retry; a second 401
        → InvalidAuth. 400/403/404/409/... → ApiError(status). 5xx, ClientError, TimeoutError,
        3xx → CannotConnect (3xx counts as connectivity problem; never followed). A 2xx body larger than
        MAX_API_BODY_BYTES (Content-Length first, then at most limit + 1 bytes read) → ApiError. The body
        is decoded with payload.decode_json (invalid, non-UTF-8, deeper than MAX_JSON_DEPTH or with an
        integer literal over MAX_JSON_INT_DIGITS digits → ApiError "invalid JSON" without the body
        chained as __cause__/__context__); a top-level value whose type is not in RESPONSE_SHAPES for
        the path → ApiError "unexpected JSON shape". A whitelisted path without a RESPONSE_SHAPES
        entry → RequestNotAllowed, checked after the URL guard and before any token or network
        access."""
    @property
    def token_manager(self) -> TokenManager: ...                       # the manager given to __init__
    async def async_get_current_account(self) -> dict[str, Any]       # GET useraccounts/current (no school headers)
    async def async_get_posts(self, school_id: int, *, offset: int = 0, limit: int = FEED_LIMIT) -> dict[str, Any]
        # GET communityposts?offset=&limit=&filterType=0  → {"values": [...], "totalCount": n, "timeStamp": ...}
    async def async_get_post(self, school_id: int, post_id: int) -> dict[str, Any]   # GET communityposts/{id}
    async def async_get_events(self, school_id: int, start: datetime, end: datetime) -> list[Any] | dict[str, Any]
        # GET events?from=<utc Z>&until=<utc Z>; naive datetimes raise ValueError; format "%Y-%m-%dT%H:%M:%SZ"
```

### 6a. payload.py (PURE, shared by login.py, tokens.py and client.py)

```python
async def async_read_limited(response, limit: int) -> bytes | None
    # Content-Length over the limit → None before reading; else at most limit + 1 bytes are read,
    # None when the body is longer. An absent or malformed Content-Length, or one longer than 640
    # characters, is ignored. login.py (HTML pages), tokens.py and client.py all use it.
def json_nesting_depth_exceeds(text: str, limit: int) -> bool
    # one linear pass, no recursion: '[' / '{' outside strings +1, ']' / '}' -1 (never below 0);
    # inside a string a backslash escapes the next character. Does not validate JSON.
def decode_json(body: bytes, *, what: str, status: int | None, max_bytes: int,
                max_depth: int = MAX_JSON_DEPTH) -> Any
    # len(body) > max_bytes → ApiError("<what>: response body too large", status);
    # not UTF-8 (a BOM is tolerated), deeper than max_depth, an integer literal of more than
    # MAX_JSON_INT_DIGITS digits (a minus sign not counted), or rejected by json.loads (ValueError,
    # RecursionError) → ApiError("<what>: invalid JSON", status), raised outside every except block
    # so no JSONDecodeError (.doc) or UnicodeDecodeError (.object) holding the body stays attached.
```

`json.loads` recurses once per nesting level, and whether 100 000 levels end in
`RecursionError` depends on the interpreter version, its recursion limit and the
C stack size. The depth scan runs before `json.loads`, so the outcome is the same
on every build. The scan costs about 2 ms for a 50 KB feed and stays linear up to
the body cap (about 0.4 s for a hostile 8 MiB body). Only UTF-8 bodies are
accepted (RFC 8259 §8.1): the body is decoded once and the scan and `json.loads`
read the same text, whereas `json.loads` on bytes would also guess UTF-16/32.

Integer literals go through a `parse_int` hook that refuses more than
`MAX_JSON_INT_DIGITS` digits before converting. On its own, `json.loads` leaves
that bound to the process-wide `int_max_str_digits` setting (4300 by default;
`PYTHONINTMAXSTRDIGITS`, `-X int_max_str_digits` or any library calling
`sys.set_int_max_str_digits` can change it, and at 0 there is no bound and the
conversion cost grows quadratically: about 10 s for an 8 MiB literal). A non-zero
setting is never below 640 (`sys.int_info.str_digits_check_threshold`), so `int`
never refuses a literal the hook accepts: the outcome is the same under every
setting. The hook costs about 0.2–0.3 s on a hostile 8 MiB body of small
integers and nothing measurable on a real feed. Floats are not affected
(their conversion is linear).

## 7. models.py and textutil.py (PURE)

```python
@dataclass(frozen=True)
class School:
    id: int
    name: str            # school.displayName or school.name or str(id)
    role_id: int

@dataclass(frozen=True)
class Account:
    user_profile_id: int
    display_name: str
    schools: tuple[School, ...]    # one per role with type == ROLE_TYPE_PARENT, de-duplicated by school id, in API order

@dataclass(frozen=True)
class Post:
    id: int
    title: str                # whitespace-collapsed, may be ""
    text: str                 # full plain text from body (html_to_text), untruncated
    created: datetime         # aware, UTC
    groups: tuple[str, ...]   # communities[].name, in order, de-duplicated
    author: str               # author.displayName or ""
    comments_count: int
    attachments: tuple[str, ...]   # media[].fileName only (never fullPath)

@dataclass(frozen=True)
class SchoolEvent:
    id: int
    title: str
    description: str          # plain text
    start: datetime | date    # date when all_day, else aware datetime in SCHOOL_TIMEZONE
    end: datetime | date      # date exclusive when all_day
    all_day: bool
    location: str
    groups: tuple[str, ...]

def parse_datetime(value: Any, *, assume_tz: tzinfo) -> datetime  # ISO 8601 incl. 'Z'; naive → assume_tz; result aware; year outside 1900..9000 → ValueError
def parse_account(data: Any) -> Account                            # raises ApiError on missing userProfileId / roles
def parse_post(data: Mapping) -> Post                              # raises ValueError on missing id/created
def parse_posts_page(data: Any) -> list[Post]                      # values[], invalid items skipped, sorted by created desc then id desc
def parse_event(data: Mapping, *, tz: tzinfo) -> SchoolEvent
def parse_events(data: Any, *, tz: tzinfo) -> list[SchoolEvent]    # sorted by start
```

All-day rule in `parse_event` (0.2.1): a whole day is a calendar date, not an
instant, so `start` and `end` are **never converted** to another zone; the date and
the time are read as they stand in the text (`Z`, an offset such as `+01:00`, or no
zone alike). `start_date = start.date()`; if the time of `end` is 00:00 →
`end_date = end.date()` (already exclusive), else `end_date = end.date() + 1 day`;
a missing/invalid `end` → `start_date + 1 day`; always ensure
`end_date >= start_date + 1 day`. The live API sends whole days as UTC midnight with
an exclusive end (`2026-11-26T00:00:00Z` … `2026-11-27T00:00:00Z`, probe of
8 November 2026); 0.2.0 converted that end to 01:00 Europe/Amsterdam, took it for an
inclusive end and added a day. Timed events: aware datetimes converted to `tz`, naive
ones taken as `tz`; a missing/invalid `end` → `start + 1 hour`; `end <= start` →
`start + 1 hour`.
`parse_datetime` rejects years outside 1900–9000, and `parse_datetime`,
`parse_post` and `parse_event` treat an `OverflowError` from a datetime
conversion like `ValueError`, so `parse_posts_page` and `parse_events` skip
such items instead of failing the whole page.

Every text field the parse functions produce (names, titles, texts, locations,
groups, attachment names) has its unpaired UTF-16 surrogates replaced by U+FFFD.
A JSON escape such as `"\ud83d"` (half an emoji, for example after the server
cut a text) decodes to a lone surrogate, which cannot be encoded as UTF-8:
Home Assistant's JSON serialisation of states, attributes, device names and
config entries, and the attribute byte budget of §10, would fail on it.
`json.loads` joins an escaped pair into one character, which stays as it is.

`textutil.py`:

```python
def looks_like_html(value: str) -> bool            # a tag like <p>, <br/>, </div> or an entity like &amp; / &#39;
def looks_cut(value: str) -> bool                  # ends in "…" or "..." (trailing whitespace ignored): may be cut off (§17)
def html_to_text(value: str) -> str                # see rules
def collapse_whitespace(value: str) -> str         # single line: all whitespace runs → one space, stripped
def truncate(value: str, limit: int) -> tuple[str, bool]   # keeps len <= limit; appends "…" when cut; (text, truncated)
```

`html_to_text` rules: `p`, `div`, `br`, `li`, `h1`–`h6`, `tr`, `table`,
`ul`, `ol`, `blockquote`, `section`, `article`, `header`, `footer`, `pre`,
`hr` start/end a line; `li` content is prefixed with `- `; `a href` with an
`http(s)` URL that differs from the link text becomes `text (url)`;
`script`/`style`/`head`/`template` contents dropped; entities decoded
(`convert_charrefs=True`); `&nbsp;` becomes a space; runs of spaces/tabs
collapse to one space; lines are stripped; three or more consecutive
newlines collapse to two; result stripped. Non-HTML input (plain text) comes
out with the same whitespace normalisation.

## 8. coordinator.py (HA)

```python
@dataclass
class SchoolData:
    school: School
    posts: list[Post]                      # newest first, max FEED_LIMIT
    events: list[SchoolEvent]              # sorted by start
    events_fetched_at: datetime | None     # UTC
    new_posts: list[Post]                  # posts detected as new in THIS update (chronological), else []

@dataclass
class SocialSchoolsData:
    account: Account
    schools: dict[int, SchoolData]         # key school id

class SocialSchoolsCoordinator(DataUpdateCoordinator[SocialSchoolsData]):
    config_entry: SocialSchoolsConfigEntry
    def __init__(self, hass, entry, client: SocialSchoolsClient, account: Account, store: Store[dict]) -> None
        # update_interval = timedelta(minutes=entry.options.get(OPT_POSTS_INTERVAL, DEFAULT_POSTS_INTERVAL)) (clamped to MIN)
        # name = f"{DOMAIN} {entry.entry_id}"  (never the title: it is the parent's display
        #                                        name and the coordinator logs its name)
    async def async_load_store(self) -> None        # called once before first refresh
    async def _async_update_data(self) -> SocialSchoolsData
```

Update algorithm, per school in `account.schools`:

1. `posts = parse_posts_page(await client.async_get_posts(school.id))`.
2. If `FETCH_POST_DETAILS`: for the first `MAX_POSTS_IN_LIST + 1` posts whose
   id is not in the in-memory detail cache, `async_get_post` (max
   `MAX_DETAIL_FETCHES_PER_UPDATE` per update); replace `text` with the
   detail body text; cache by post id. Detail failures fall back to the feed
   text and are logged at debug.
3. Events: if `events_fetched_at is None` or `now - events_fetched_at >=
   CALENDAR_REFRESH_INTERVAL` (or the horizon option changed): fetch
   `events?from=now-7d&until=now+calendar_days` and replace; otherwise keep
   the previous list unchanged.
4. New-post detection (§9).

Errors: `InvalidAuth` → `ConfigEntryAuthFailed`; `CannotConnect`/`ApiError`
→ `UpdateFailed` (coordinator keeps the previous `data`). A failure in the
events fetch while posts succeeded is logged (warning once, then debug) and
the previous events are kept; the update still succeeds.

The coordinator exposes `last_success_utc: datetime | None` and
`consecutive_failures: int` for diagnostics.

## 9. New-post detection and Store

`Store[dict](hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id))`
with content:

```json
{"schools": {"<school_id>": {"high_water": "<ISO UTC>", "seen_ids": [123, 124]}}}
```

Rules:

* `seeded` is an in-memory flag per school, False when the coordinator is
  created (first run or restart).
* On the first successful posts fetch for a school in this process: add all
  ids to `seen_ids`, raise `high_water` to the max `created`, save the store,
  set `seeded = True`, **fire nothing** (`new_posts = []`).
* Afterwards a post is new iff `id not in seen_ids` and
  `created > high_water - NEW_POST_GRACE`. New posts are returned in
  `new_posts` in chronological order (oldest first); their ids are added
  to `seen_ids`; `high_water = max(high_water, created)`. Edited posts keep
  their id and are therefore never new. `seen_ids` is trimmed to the newest
  `SEEN_IDS_LIMIT` entries (list order = insertion order, trim from the front).
* The store is saved (`async_save`) only when something changed.
* `async_remove_entry` removes the store file.

## 10. Entities

Base (`entity.py`):

```python
class SocialSchoolsEntity(CoordinatorEntity[SocialSchoolsCoordinator]):
    _attr_has_entity_name = True
    def __init__(self, coordinator, school: School, key: str) -> None:
        # unique_id = f"{account.user_profile_id}_{school.id}_{key}"
        # device_info = DeviceInfo(identifiers={(DOMAIN, f"{account.user_profile_id}_{school.id}")},
        #     name=f"Social Schools · {school.name}", manufacturer="Social Schools", model="School",
        #     entry_type=DeviceEntryType.SERVICE, configuration_url="https://app.socialschools.eu/")
    @property
    def school_data(self) -> SchoolData | None
    @property
    def available(self) -> bool:   # True as long as coordinator.data has this school (stale data stays available)
```

| platform | translation_key | key | NL name | EN name |
|---|---|---|---|---|
| sensor | `latest_post` | `latest_post` | Laatste bericht | Latest post |
| sensor | `post_count` | `post_count` | Berichten | Posts |
| event | `new_post` | `new_post` | Nieuw bericht | New post |
| calendar | `calendar` | `calendar` | Agenda | Calendar |

Resulting entity ids for a device named `Social Schools · Voorbeeldschool` on a
Dutch Home Assistant: `sensor.social_schools_voorbeeldschool_laatste_bericht`,
`sensor.social_schools_voorbeeldschool_berichten`,
`event.social_schools_voorbeeldschool_nieuw_bericht`,
`calendar.social_schools_voorbeeldschool_agenda`.

**sensor latest_post**: state = `truncate(title or first line of text, MAX_TITLE_LENGTH)`,
`None` when there are no posts. Attributes: `post_id` (int), `groep`
(truncate(", ".join(groups), MAX_GROUP_LENGTH)), `auteur` (truncate to
MAX_AUTHOR_LENGTH), `geplaatst` (ISO 8601 of `created` in SCHOOL_TIMEZONE),
`tekst` (truncate(text, MAX_TEXT_LENGTH)), `afgekapt` (bool: text was cut),
`bijlagen` (the first MAX_ATTACHMENTS file names, each truncated to
MAX_ATTACHMENT_NAME_LENGTH; duplicates are kept, distinct files may share a
name), `reacties` (int). After building, drop `bijlagen` entries from the
end while `len(json.dumps(attrs, ensure_ascii=False).encode()) >
ATTRIBUTES_BYTE_BUDGET`. `_unrecorded_attributes = frozenset({"tekst", "bijlagen"})`.

**sensor post_count**: state = number of posts with `created >= now - POST_COUNT_WINDOW`
(within the fetched feed), attribute `berichten`: list of the newest
`posts_in_list` posts, each `{"post_id", "titel" (≤120), "groep" (≤120),
"auteur" (≤120), "geplaatst", "tekst_kort" (truncate 300)}`. After building, if
`len(json.dumps(attrs, ensure_ascii=False).encode()) > ATTRIBUTES_BYTE_BUDGET`
drop entries from the end until it fits. `_unrecorded_attributes = frozenset({"berichten"})`.

**event new_post**: `_attr_event_types = [EVENT_TYPE_NEW_POST]`. In
`_handle_coordinator_update`, for each post in `school_data.new_posts`
(chronological) call `_trigger_event(EVENT_TYPE_NEW_POST, {post_id, titel,
groep, auteur, geplaatst, tekst_kort})` then `async_write_ha_state()`.
Nothing on seeding runs. `_unrecorded_attributes = frozenset({"tekst_kort"})`:
the post text stays out of the recorder like `tekst` and `berichten` of the
sensors; automations receive it from the state change and the restore cache
keeps the last event with all its attributes.

**calendar**: `event` property = the first event whose `end` is in the
future (ongoing or upcoming) as `CalendarEvent(start, end, summary=title,
description=description, location=location or None, uid=f"socialschools-{id}")`.
`async_get_events(hass, start_date, end_date)` returns cached events that
overlap the range, sorted by start. All-day events use `date` objects with
an exclusive end; timed events aware datetimes in SCHOOL_TIMEZONE. For the
`event` choice and the overlap test an all-day `date` counts as local
midnight in **Home Assistant's** time zone (`dt_util.start_of_local_day`),
the interpretation `CalendarEntity.state` and its alarms use; the dates
themselves are derived in SCHOOL_TIMEZONE by `parse_event`.

## 11. Config flow

* `ConfigFlow`, `VERSION = 1`, `MINOR_VERSION = 1`.
* `async_step_user`: schema `username` (TextSelector email), `password`
  (TextSelector password). On submit: create
  `async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())`,
  `async_native_login` → `async_exchange_code`, close the session in `finally`.
  Then with the shared session (`async_get_clientsession`) a `TokenManager`
  (access token from the exchange, no callback) and `SocialSchoolsClient`:
  `parse_account(await client.async_get_current_account())`. The refresh
  token that is stored is the manager's current one after that call (a 401
  there forces a refresh that may rotate it), not the exchange's.
  Errors → `invalid_auth`, `cannot_connect` (CannotConnect/ApiError),
  `unsupported`, `no_parent_role` (no schools), `unknown` (anything else,
  logged with `exception`). Unique id = `str(user_profile_id)`,
  `_abort_if_unique_id_configured()`. Entry: title = `display_name`
  (fallback username), data = `{username, refresh_token, client_id: OIDC_CLIENT_ID,
  user_profile_id}`.
* `async_step_reauth` → `async_step_reauth_confirm` (schema: password only,
  description placeholder `username`). Same login; the account's
  `user_profile_id` must match the entry's unique id
  (`_abort_if_unique_id_mismatch(reason="wrong_account")`); then
  `async_update_reload_and_abort(entry, data_updates={refresh_token, client_id, username})`.
* Options flow (`OptionsFlowWithReload` when available): `posts_interval`
  (NumberSelector box, min 10, max 1440, unit min), `calendar_days` (7–365,
  unit d), `posts_in_list` (1–20). Defaults from const.
* The password never leaves the flow handler; it is not logged or stored.

## 12. diagnostics.py

`async_get_config_entry_diagnostics` returns
`{"entry": {"data": redacted, "options": ..., "title": redacted}, "coordinator": {...}}`
redacting `refresh_token`, `username`, `user_profile_id`, `title`; the
coordinator part contains only counts, timestamps, school ids, `last_update_success`,
`consecutive_failures`, `update_interval`. No post or event content, no names.

## 13. scripts/probe.py

Runs with `python scripts/probe.py` from the repo root in `python:3.12-slim`
with only `aiohttp` installed. It must not import `homeassistant`. It loads the
pure modules through a stand-in package:

```python
pkg_dir = Path(__file__).resolve().parents[1] / "custom_components" / "socialschools"
pkg = types.ModuleType("ss_probe"); pkg.__path__ = [str(pkg_dir)]; sys.modules["ss_probe"] = pkg
login = importlib.import_module("ss_probe.login")  # etc.
```

It asks e-mail (`input`) and password (`getpass`), logs in, exchanges the
code, forces one refresh, lists the parent roles, fetches the feed, one post
detail, and the events of −7…+90 days, and prints only:

* whether a refresh token was received and `expires_in`;
* whether refresh works and whether the refresh token rotated;
* the number of parent roles / schools;
* the sorted **field names** of a post (top level plus nested `communities[0]`,
  `author`, `media[0]`) and of an event;
* whether `body` looks like HTML;
* whether the feed body length equals the detail body length (with both
  lengths);
* the number of events in the window.

Nothing else is printed or written; exceptions print only their class name
and the sanitised message.

Two opt-in investigations (0.2.0, extended in 0.2.1) add a section, again without
content:

* `--agenda-vorm`: for the calendar items of the coming 60 days only `allDay`, the raw
  `start`/`end` strings and their zone designator (`Z`, an offset or none).
* `--kinderen-vorm`: the field names and value types of the parent role and its
  `connections[]` (the children); whether ids under group-like keys of the connections
  occur in the feed's `communities[].id`/`groupId`; then the number of groups per
  connection, the type (for an object: field names and types) of the first
  `roles[i].connections[j].groups[0]`, whether a feed `communities[].groupId` and,
  separately, `communities[].id` occurs among the id-like values of the groups
  (`ja, bij N van M berichten (via groups[].<key>)` / `nee` / `onbekend`), whether a
  group name equals a community name, and whether `yearClass` equals a community name
  exactly and after collapsing whitespace and case folding. Never names, group names
  or ids.

## 14. Tests

pytest with `pytest-homeassistant-custom-component` (a Python 3.14 virtual
environment); `asyncio_mode = auto`; `tests/conftest.py` enables custom
integrations. Pure modules are tested against a local
`aiohttp.test_utils.TestServer` (real cookies, real redirects); HA layers
with `MockConfigEntry`, `aioclient_mock`, `hass_storage`, `freezer` and
`async_fire_time_changed`. Fixed dates (e.g. 2026-11-04T10:00:00+00:00).
Synthetic HTML and JSON only; never real names, tokens or school data.

## 15. HA-layer decisions verified against Home Assistant 2026.10.4

These were verified by reading the installed source (notes with file:line
citations taken during development).

* **Schemas**: `import voluptuous as vol` (HA aliases it to `probatio`; works
  on older versions too). Selectors from `homeassistant.helpers.selector`.
  `NumberSelectorConfig(min=…, max=…, step=1, mode=NumberSelectorMode.BOX,
  unit_of_measurement=…)`; values come back as `float` → cast to `int`.
* **Config flow login session**:
  `session = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())`
  and after the login (in `finally`) `session.detach()` — never
  `await session.close()` on an HA-created session (HA logs a warning; the
  connector is shared). The API/token calls of the integration use
  `async_get_clientsession(hass)` (no kwargs possible there). HA forces its
  own `User-Agent` at session level; our per-request `User-Agent` header
  overrides it (aiohttp request headers win).
* **Coordinator**: `DataUpdateCoordinator.__init__(hass, _LOGGER, config_entry=entry,
  name=f"{DOMAIN} {entry.entry_id}", update_interval=timedelta(minutes=…))`.
  The name goes into the coordinator's own log lines through our logger
  (`Error fetching %s data: %s`, `Fetching %s data recovered`,
  helpers/update_coordinator.py:489/590), so it must not carry the title (the
  parent's display name); HA core itself logs the title in its setup messages
  (`Config entry '…' for socialschools integration not ready yet`).
  `async_config_entry_first_refresh()` only inside `async_setup_entry`.
  `UpdateFailed` in the first refresh becomes `ConfigEntryNotReady`
  automatically; `ConfigEntryAuthFailed` starts reauth (the flow has
  `async_step_reauth`). Track `last_success_utc` ourselves (plain coordinator
  has no `last_update_success_time`). Never await `async_refresh` from inside
  `_async_update_data`. The options flow is `OptionsFlowWithReload`; the
  integration registers **no** update listener (rotated refresh tokens are
  written with `async_update_entry`, which fires listeners but never reloads).
* **Availability**: all entities override `available` to
  `self.coordinator.data is not None and self._school_id in self.coordinator.data.schools`
  (deliberately ignoring `last_update_success`): stale data stays available,
  so `calendar.get_events` keeps answering during an outage. Diagnostics show
  `last_update_success`, `last_success_utc` and `consecutive_failures`.
* **Entity ids**: with `has_entity_name=True`, a device named
  `Social Schools · <school>` and translated entity names, HA builds
  `<domain>.<device slug>_<name slug>` using the translation of
  `hass.config.language` when that language has native entity ids (`nl`
  does). `slugify` turns `·`, spaces and punctuation into `_` and
  transliterates accents. Do **not** pass `suggested_area` in `DeviceInfo`.
  `DeviceInfo` keys: `identifiers`, `name`, `manufacturer`, `model`,
  `entry_type=DeviceEntryType.SERVICE`, `configuration_url`.
* **Sensors**: text sensors have no device_class/unit/state_class. A state
  longer than 255 characters is written as `unknown` with an error log, so
  truncate first. `_unrecorded_attributes` is a class-level `frozenset`.
* **Event entity**: `_attr_event_types = [EVENT_TYPE_NEW_POST]`; after
  `_trigger_event(...)` call `async_write_ha_state()`. The last event is
  restored by HA after a restart — never re-trigger on startup. Event
  attributes must be JSON-serialisable. Event-type ids must match
  `^[a-z0-9_-]+$` without doubled separators (translation key rules).
* **Calendar**: `CalendarEvent` needs tz-aware datetimes or plain dates
  (never mixed), `end >= start`. `event` returns the ongoing or next event.
  `async_get_events(hass, start_date, end_date)` gets local tz-aware
  datetimes, half-open range; return overlapping events. `state` and
  `state_attributes` are final. `calendar.get_events` silently skips
  unavailable entities. `state` and the start/end alarms use
  `CalendarEvent.start_datetime_local`/`end_datetime_local`, which turn an
  all-day `date` into `dt_util.start_of_local_day(date)` in HA's zone
  (components/calendar/helper.py:193-199, __init__.py:353-410), so the
  entity's own comparisons use the same conversion.
* **Unload order**: `ConfigEntry.async_unload` awaits the integration's
  `async_unload_entry` first and only then cancels the entry's background
  tasks (the coordinator's refresh) in `_async_process_on_unload`
  (config_entries.py:1130, 1333-1346); a refresh that must finish is awaited
  inside `async_unload_entry`.
* **Store**: `Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=…), private=True)`;
  `await store.async_load()` returns `None` on first run; `await store.async_save(data)`;
  `await store.async_remove()` in `async_remove_entry`. Only JSON types
  (datetimes as ISO strings, lists not sets).
* **Diagnostics**: return plain dicts only (no dataclasses/ConfigEntry
  objects); `async_redact_data(data, {"refresh_token", "username",
  "user_profile_id", "title"})` — pass a set; `None`/`""` are not redacted.
* **strings.json / translations**: `translations/en.json` must be a literal
  copy of `strings.json` (no `[%key:…%]` references); `translations/nl.json`
  the Dutch version. Needed keys: `config.step.user.data.{username,password}`,
  `config.step.reauth_confirm.{description,data.password}`,
  `config.error.{invalid_auth,cannot_connect,unsupported,no_parent_role,unknown}`,
  `config.abort.{already_configured,unique_id_mismatch,reauth_successful}`,
  `options.step.init.data.{posts_interval,calendar_days,posts_in_list}` plus
  `data_description` for each, `entity.sensor.{latest_post,post_count}.name`,
  `entity.event.new_post.name` and
  `entity.event.new_post.state_attributes.event_type.state.nieuw_bericht`,
  `entity.calendar.calendar.name`, `exceptions.no_parent_role.message`.
  No URLs or HTML inside translation values; placeholders only as `{name}`.
  Every `data_description` key must also exist under `data`.
* **Tests** (pytest-homeassistant-custom-component 0.13.375): the plugin's
  `hass` fixture runs with language `en`, time zone `US/Pacific`; set
  `hass.config.language = "nl"` **before** `hass.config_entries.async_setup`
  to get Dutch entity ids and `await hass.config.async_set_time_zone("Europe/Amsterdam")`
  for local-time assertions. `aioclient_mock` intercepts only sessions from
  `async_get_clientsession`/`async_create_clientsession` (kwargs ignored);
  matching ignores the port and query extras; `mock_calls` entries are
  `(method, URL, data, headers)`. For local servers request `socket_enabled`.
  Time: `freezer.tick(delta); async_fire_time_changed(hass); await hass.async_block_till_done(wait_background_tasks=True)`.
  The `TokenManager` reads `time.monotonic` at call time, so the freezer also
  drives the proactive refresh: an advance of one hour renews the access token
  before the poll (used for the token-failure-while-polling tests).
  Storage: seed `hass_storage["socialschools.<entry_id>"] = {"version": 1, "minor_version": 1, "key": ..., "data": {...}}`
  before setup. `MockConfigEntry(domain=DOMAIN, data=..., options=..., unique_id=..., title=..., entry_id=...)`,
  `entry.add_to_hass(hass)`, `await hass.config_entries.async_setup(entry.entry_id)`.
  Reauth: `await entry.start_reauth_flow(hass)`. Every test must leave no
  lingering timers (the coordinator's are cancelled on unload automatically).

## 16. Version 0.2: local read marks, opened post, children

New pure modules (HA-free, Python 3.12, listed in the purity tests and the CI
compile step):

* `reading.py` — `ReadState`: per school `{post_id: {person entity ids}}` plus the
  set of readers that have been seeded. `sync(school_id, readers, feed_ids)`
  seeds readers not seen before with every post in the feed, forgets removed
  readers (and their marks) and keeps marks only for feed posts plus the
  `READ_MARKS_MARGIN` (30) newest other ids. `mark(school_id, post_ids, person,
  read)`, `read_by`, `unread_by(school_id, post_id, readers)`.
* `children.py` — `parse_children(text)` reads the `kinderen` option (one line
  per child, `Naam=groep, groep`; ValueError with the line number otherwise),
  `children_for(groups, children)` matches group names exactly after collapsing
  whitespace and case folding, `is_schoolwide(groups, children)` is True for more
  than `SCHOOLWIDE_GROUP_THRESHOLD` (3) groups or when no child matches. Since
  0.2.2 the children also come from the account (§18): `child_from_account` and
  `merge_children`.

"Read" is local bookkeeping only. Nothing about it is sent to Social Schools:
no new whitelist paths, no statistics or read-receipt calls. The store goes to
version 2 (`SocialSchoolsStore._async_migrate_func` adds empty `read` and
`readers` per school); with no seeded readers after the migration, the first
refresh marks the feed as read for every reader.

Options: `lezers` (person entity ids, EntitySelector multiple) and `kinderen`
(multiline text, validated in the options flow: `invalid_children`).

Actions (registered in `async_setup`, `CONFIG_SCHEMA` is config-entry only):

| action | fields | behaviour |
|---|---|---|
| `markeer_gelezen` | `post_id` (int, required), `persoon` (person entity), `gelezen` (bool, default true) | marks one feed post for one person |
| `markeer_alles_gelezen` | `persoon` | marks every feed post of every entry where the person is a reader |
| `open_bericht` | `post_id` (int, required) | shown by the opened-post sensor; a feed post as the feed has it (no request), otherwise `GET communityposts/{id}` with an LRU cache of `OPEN_POST_CACHE_SIZE` = 20 per entry (§17) |

Without `persoon` the person is the `person` state whose `user_id` attribute is
`call.context.user_id`. Errors are `ServiceValidationError` with translation
keys `choose_person`, `not_a_reader` (`{person}`), `unknown_post` (`{post_id}`),
`not_loaded`; a failed fetch is `HomeAssistantError` `open_failed` and keeps the
previous post. Marking and opening send the dispatcher signal
`socialschools_local_update_{entry_id}`; `LocalUpdatesEntity` subclasses (post
list, unread, opened post) write their state on it, without an API request and
without touching the new-post event entity.

Entities: sensor `unread` (count; `eerste`, `per_persoon`) and sensor
`open_post` (title; `post_id`, `groep`, `kinderen`, `schoolbreed`, `auteur`,
`geplaatst`, `tekst` ≤ `MAX_OPEN_TEXT_LENGTH` (8000) and cut further by a
binary search to stay within `ATTRIBUTES_BYTE_BUDGET`, `afgekapt`, `bijlagen`,
`reacties`, `gelezen_door`, `nog_lezen`, `url`; `tekst` and `bijlagen`
unrecorded). `post_summary` adds `kinderen`, `schoolbreed` and `url`
(`POST_URL_TEMPLATE`, the web app route `/communityposts/:postId`); the post list
adds `gelezen_door`, `nog_lezen`, `nieuw`.

## 17. Version 0.2.1: all-day dates, opening from the feed, children probe, releases

* **All-day dates** are read as written, without any time zone conversion (§7).
  `calendar.py` is unchanged: for its `event` choice and the overlap test an all-day
  date still counts as local midnight in Home Assistant's zone (§10).
* **`open_bericht`** (`SocialSchoolsCoordinator.async_open_post`). The probe found the
  feed body as long as the detail body (494/494), and whether a detail request counts
  as a view at Social Schools (posts carry `readStatistics`) is unknown, so detail
  requests are kept to the cases that need one:
  * a post in the current feed of its school is shown as the feed has it: no request;
  * the detail is fetched only for a post that has dropped out of the feed, or when
    `_feed_text_may_be_cut` sees a sign that the feed text is not complete:
    `FETCH_POST_DETAILS` is set (unless the poll already replaced the text by the
    detail text, `_detail_cache`), or the text ends in an ellipsis
    (`textutil.looks_cut`: `…` or `...`, trailing whitespace ignored);
  * fetched posts go into the LRU cache (`OPEN_POST_CACHE_SIZE`), which is consulted
    before any fetch; a failed fetch raises (`open_failed`), caches nothing and keeps
    the previously opened post.

  The action looks the post up in every current feed first (`find_post`) and only
  then among the posts each entry saw before (`find_earlier_post`: the `seen_ids` of
  the new-post bookkeeping, the newest `SEEN_IDS_LIMIT` per school and persisted, or
  the open cache). An id found nowhere is `unknown_post`, before any request.
  `markeer_gelezen` still accepts feed posts only.
* **Children.** `scripts/probe.py --kinderen-vorm` describes the groups of the
  children against the feed (§13). Its live outcome led to the automatic link of
  0.2.2 (§18).
* **Releases.** `.github/workflows/release.yml` runs on every push to `main` (and on a
  manual run, on `main` only) with `permissions: contents: write`, only
  `actions/checkout` (pinned, `fetch-depth: 0`, `persist-credentials: false`) and the
  `gh` CLI with `GITHUB_TOKEN`; it never runs on pull requests, so it is not one of the
  required checks. It runs `python3 scripts/release.py`:

  ```python
  def manifest_version(text: str) -> str                    # "x.y.z", else ReleaseError
  def changelog_section(text: str, version: str) -> str     # body of "## [version] - date", up to
                                                            # the next "## " or a link definition;
                                                            # missing/empty → ReleaseError
  def first_parent_history(repo, ref="HEAD") -> list[tuple[str, str | None]]
      # (commit, manifest version) of the first-parent commits that changed the manifest, newest first
  def release_commit(history, version) -> str | None        # oldest commit of the newest run carrying
                                                            # version: for a merged PR the merge commit
  def run(repo, *, dry_run=False) -> int
  ```

  When the tag `v<version>` of the manifest version exists, nothing happens;
  otherwise `gh release create v<version> --target <commit> --title v<version>
  --notes-file <section> --latest=true`. A version without a commit on the branch or
  without its changelog section is not released and fails the run (`::error::`), as
  does a refused tag. The message names the two known causes:
  * GitHub refuses the `GITHUB_TOKEN` a tag on a commit whose `.github/workflows`
    differ from those of `main` (HTTP 403 "Resource not accessible by integration");
    the token cannot get that permission. This is why older versions are not tagged
    afterwards: 0.2.1 tried `v0.2.0` on `e81245d` and got exactly this, while `v0.2.1`
    on the merge commit (the tip of `main`) succeeded in the same run (0.2.2 dropped
    that backfill);
  * "Restrict creations" on a tag ruleset that covers `v*`; the `GITHUB_TOKEN` cannot
    be put on a bypass list. The live rulesets do not block it.

## 18. Version 0.2.2: children from the account

`scripts/probe.py --kinderen-vorm` on the live account (8 November 2026): one parent
role with one connection; `connections[0].groups[0]` is a string (a group name); a
group name of the child equals a `communities[].name` for 16 of the 20 feed posts;
`groupId`, community `id` and `yearClass` match nothing.

* **models.py** (pure):

  ```python
  @dataclass(frozen=True)
  class ConnectedChild:
      name: str                 # firstName, else displayName (whitespace-collapsed)
      groups: tuple[str, ...]   # connections[].groups names, collapsed, in order, de-duplicated
  def parse_connected_children(data: Any) -> dict[int, tuple[ConnectedChild, ...]]
      # per school id of every role of type ROLE_TYPE_PARENT; connections without a name are
      # skipped; a group is a string or an object with a string "name"; a child listed under two
      # parent roles of one school gets the groups of both; unusable input → {} (never raises)
  ```

* **children.py** (pure): `child_from_account(name, groups) -> Child | None` (groups
  normalised like the option; None without a name or a group) and
  `merge_children(account, option) -> tuple[Child, ...]`: a line of the option for a
  child of the account (names compared with `normalise_group`) replaces that child's
  groups and spelling, any other line adds a child; account children keep their
  order, added ones follow in option order.
* **Coordinator.** `async_setup_entry` passes `parse_connected_children(raw account)`
  as `connected`. `children_for(school_id)` is `merge_children(account children of the
  school, option_children)`; every entity (post list, unread `eerste`, opened post,
  new-post event) uses it, so `kinderen` and `schoolbreed` follow the account.
  `schoolbreed` keeps its rule. Once every `ACCOUNT_REFRESH_INTERVAL` the update
  fetches `useraccounts/current` again (whitelisted, GET) before the feeds and
  replaces the account children; `CannotConnect`/`ApiError` keep the previous
  children and retry after `ACCOUNT_RETRY_INTERVAL` (warning once, then debug);
  `InvalidAuth` propagates as for the other requests. Schools and entities are not
  changed by this refresh.
* **Privacy.** Names reach only the `kinderen` attributes, as names from the option
  do. The store holds no names; diagnostics show `child_count` (option lines) and
  `account_child_count` (children with groups); log lines carry counts and the entry
  id only.
