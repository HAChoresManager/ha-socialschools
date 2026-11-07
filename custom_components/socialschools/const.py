"""Constants for the Social Schools integration.

This module is HA-free: it must not import anything from ``homeassistant`` so
that the protocol modules (and ``scripts/probe.py``) can use it on their own.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "socialschools"
VERSION: Final = "0.1.0"  # keep equal to manifest.json "version" (checked by a test)
USER_AGENT: Final = f"HomeAssistant-SocialSchools/{VERSION}"

# --- OpenID Connect / native app flow -------------------------------------------------------
OIDC_ISSUER: Final = "https://login.socialschools.eu"
OIDC_CLIENT_ID: Final = "eu.socialschools.ios"
OIDC_REDIRECT_URI: Final = "eu.socialschools.ios:/oauthredirect"
# Live-verified scope set. Narrowing it later is a one-line change here.
OIDC_SCOPES: Final = "openid profile SocsWebApi email offline_access SocsNotificationApi"
OIDC_AUTHORIZE_PATH: Final = "/connect/authorize"
OIDC_CALLBACK_PATH: Final = "/connect/authorize/callback"
OIDC_TOKEN_PATH: Final = "/connect/token"  # noqa: S105 - a URL path, not a secret
LOGIN_FORM_PATH: Final = "/Account/Login"
MAX_LOGIN_STEPS: Final = 10

# --- API ------------------------------------------------------------------------------------
API_BASE_URL: Final = "https://api.socialschools.eu/api/v1/"
ROLE_TYPE_PARENT: Final = 1
FEED_LIMIT: Final = 20
FEED_FILTER_TYPE: Final = 0
REQUEST_TIMEOUT: Final = 30  # seconds, for every HTTP call
TOKEN_REFRESH_LEEWAY: Final = 60  # seconds before expiry at which the access token is renewed
DEFAULT_EXPIRES_IN: Final = 3600
MAX_EXPIRES_IN: Final = 10 * 365 * 86400  # seconds; a larger expires_in is clamped to this

# --- Transport limits -----------------------------------------------------------------------
# Response bodies are never buffered beyond these sizes: Content-Length is checked first,
# then at most limit + 1 bytes are read.  Over the limit: UnsupportedLogin / ApiError.
MAX_LOGIN_PAGE_BYTES: Final = 1_048_576  # 1 MiB, pages of the login flow
MAX_TOKEN_BODY_BYTES: Final = 1_048_576  # 1 MiB, token endpoint responses
MAX_API_BODY_BYTES: Final = 8 * 1_048_576  # 8 MiB, API responses
# Deepest array/object nesting accepted in a JSON body, checked before json.loads so the
# outcome never depends on the interpreter's recursion limit or C stack size.  Real
# responses nest about four levels deep.
MAX_JSON_DEPTH: Final = 64
# Plain http is accepted only for these hosts (local test servers); any other host needs https.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

# --- Config entry data ----------------------------------------------------------------------
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"  # noqa: S105 - a key name; only used inside the flows, never stored
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 - a key name
CONF_CLIENT_ID: Final = "client_id"
CONF_USER_PROFILE_ID: Final = "user_profile_id"

# --- Options --------------------------------------------------------------------------------
OPT_POSTS_INTERVAL: Final = "posts_interval"  # minutes
OPT_CALENDAR_DAYS: Final = "calendar_days"  # days ahead
OPT_POSTS_IN_LIST: Final = "posts_in_list"  # entries in the ``berichten`` attribute
DEFAULT_POSTS_INTERVAL: Final = 15
MIN_POSTS_INTERVAL: Final = 10
MAX_POSTS_INTERVAL: Final = 1440
DEFAULT_CALENDAR_DAYS: Final = 90
MIN_CALENDAR_DAYS: Final = 7
MAX_CALENDAR_DAYS: Final = 365
DEFAULT_POSTS_IN_LIST: Final = 10
MIN_POSTS_IN_LIST: Final = 1
MAX_POSTS_IN_LIST: Final = 20

# --- Polling --------------------------------------------------------------------------------
CALENDAR_LOOKBACK_DAYS: Final = 7
CALENDAR_REFRESH_INTERVAL: Final = timedelta(hours=1)
POST_COUNT_WINDOW: Final = timedelta(days=7)
SCHOOL_TIMEZONE: Final = "Europe/Amsterdam"

# --- Entity limits --------------------------------------------------------------------------
MAX_TITLE_LENGTH: Final = 255
MAX_TEXT_LENGTH: Final = 2000
MAX_SHORT_TEXT_LENGTH: Final = 300
MAX_LIST_TITLE_LENGTH: Final = 120
MAX_LIST_GROUP_LENGTH: Final = 120
MAX_GROUP_LENGTH: Final = 255  # ``groep`` of the latest-post sensor
MAX_AUTHOR_LENGTH: Final = 120  # ``auteur`` everywhere
MAX_ATTACHMENTS: Final = 20  # entries of ``bijlagen``
MAX_ATTACHMENT_NAME_LENGTH: Final = 120  # characters per entry of ``bijlagen``
ATTRIBUTES_BYTE_BUDGET: Final = 12_000  # JSON bytes of all attributes of one entity

# --- Events / storage -----------------------------------------------------------------------
EVENT_TYPE_NEW_POST: Final = "nieuw_bericht"
STORAGE_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = "socialschools.{entry_id}"
SEEN_IDS_LIMIT: Final = 500
NEW_POST_GRACE: Final = timedelta(days=7)
# Set to True only if scripts/probe.py reports that the feed body is shorter than the
# post detail body; the coordinator then fetches ``communityposts/{id}`` for new posts.
FETCH_POST_DETAILS: Final = False
MAX_DETAIL_FETCHES_PER_UPDATE: Final = 20

# --- Attribute keys (Dutch, fixed, never translated) ----------------------------------------
ATTR_POST_ID: Final = "post_id"
ATTR_TITLE: Final = "titel"
ATTR_GROUP: Final = "groep"
ATTR_AUTHOR: Final = "auteur"
ATTR_POSTED: Final = "geplaatst"
ATTR_TEXT: Final = "tekst"
ATTR_SHORT_TEXT: Final = "tekst_kort"
ATTR_TRUNCATED: Final = "afgekapt"
ATTR_ATTACHMENTS: Final = "bijlagen"
ATTR_COMMENTS: Final = "reacties"
ATTR_POSTS: Final = "berichten"
