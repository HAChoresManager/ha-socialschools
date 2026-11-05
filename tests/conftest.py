"""Shared pytest fixtures.

The Home Assistant layer is tested against a fake Social Schools API served
through ``aioclient_mock``: the real token and client code runs against the
mutable :class:`ApiState`, which tests change between polls.  All values are
synthetic; secret-like values carry the ``private-`` marker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.socialschools.const import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_PROFILE_ID,
    CONF_USERNAME,
    DOMAIN,
    OIDC_CLIENT_ID,
)

TOKEN_URL = "https://login.socialschools.eu/connect/token"
API_URL = "https://api.socialschools.eu/api/v1/"
ACCOUNT_URL = f"{API_URL}useraccounts/current"
POSTS_URL = f"{API_URL}communityposts"
EVENTS_URL = f"{API_URL}events"
POST_DETAIL_PATTERN = re.compile(r"^https://api\.socialschools\.eu/api/v1/communityposts/\d+")

TOKEN_PATH = "/connect/token"
ACCOUNT_PATH = "/api/v1/useraccounts/current"
POSTS_PATH = "/api/v1/communityposts"
EVENTS_PATH = "/api/v1/events"

ENTRY_ID = "01SOCIALSCHOOLSTEST"
STORAGE_KEY = f"socialschools.{ENTRY_ID}"
USER_PROFILE_ID = 4242
SCHOOL_ID = 7
SCHOOL_NAME = "Private School"

RequestRecord = tuple[str, URL, dict[str, str]]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""
    return


def make_account(
    user_profile_id: int = USER_PROFILE_ID,
    display_name: str = "Private Parent",
    schools: Sequence[tuple[int, str]] = ((SCHOOL_ID, SCHOOL_NAME),),
) -> dict[str, Any]:
    """Return a ``useraccounts/current`` body: a parent role per school plus an employee role.

    The employee role (type 2, school 99) must be ignored by the integration.  Every
    parent role lists a connected child, as the real profile does; that name must never
    reach a state, the store, the diagnostics or the log.
    """
    roles: list[dict[str, Any]] = [
        {
            "id": 500 + index,
            "type": 1,
            "school": {"id": school_id, "displayName": name},
            "connections": [
                {
                    "id": 600 + index,
                    "roleType": 3,
                    "uniqueId": 700 + index,
                    "displayName": "Private Child",
                }
            ],
        }
        for index, (school_id, name) in enumerate(schools)
    ]
    roles.append(
        {"id": 599, "type": 2, "school": {"id": 99, "displayName": "Private Employer School"}}
    )
    return {"userProfileId": user_profile_id, "displayName": display_name, "roles": roles}


def make_post(
    post_id: int,
    created: str,
    title: str | None = None,
    body: str = "<p>Hallo <b>wereld</b></p>",
    groups: Sequence[str] = ("Groep 3",),
    author: str = "Private Teacher",
    comments: int = 1,
    attachments: Sequence[str] = ("brief.pdf",),
) -> dict[str, Any]:
    """Return one feed item in the API shape; media carry a signed URL that must never leak."""
    return {
        "id": post_id,
        "title": f"Bericht {post_id}" if title is None else title,
        "body": body,
        "created": created,
        "communities": [{"id": 10 + index, "name": name} for index, name in enumerate(groups)],
        "author": {"id": 77, "displayName": author},
        "commentsCount": comments,
        "media": [
            {
                "id": 900 + index,
                "fileName": name,
                "fullPath": f"https://cdn.example.test/private-signed/{index}/{name}",
            }
            for index, name in enumerate(attachments)
        ],
    }


def make_event(
    event_id: int,
    start: str,
    end: str,
    title: str,
    all_day: bool = False,
    description: str = "<p>Studiedag</p>",
    location: str = "Aula",
    groups: Sequence[str] = ("Groep 3",),
) -> dict[str, Any]:
    """Return one calendar event in the API shape."""
    return {
        "id": event_id,
        "title": title,
        "description": description,
        "start": start,
        "end": end,
        "allDay": all_day,
        "location": location,
        "communities": [{"id": 10 + index, "name": name} for index, name in enumerate(groups)],
    }


@dataclass
class ApiState:
    """Mutable state of the fake API; tests change it between polls."""

    account: dict[str, Any]
    posts: list[dict[str, Any]]  # newest first
    post_details: dict[int, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    token_rotates_to: str | None = None
    posts_status: int = 200
    posts_status_queue: list[int] = field(default_factory=list)  # one-off statuses, used first
    events_status: int = 200
    account_status: int = 200
    account_status_queue: list[int] = field(default_factory=list)  # one-off statuses, used first
    token_status: int = 200
    token_exc: Exception | None = None  # raised by the token endpoint instead of answering
    token_hold: asyncio.Event | None = None  # the token endpoint waits for this before answering
    token_calls: int = 0
    request_log: list[RequestRecord] = field(default_factory=list)

    def requests_for(self, path: str) -> list[RequestRecord]:
        """Return the recorded requests whose URL path equals ``path``."""
        return [record for record in self.request_log if record[1].path == path]


@pytest.fixture
def api_state() -> ApiState:
    """Return the default fake API state: one school, three posts, two events."""
    return ApiState(
        account=make_account(),
        posts=[
            make_post(103, "2026-11-03T08:00:00Z"),
            make_post(102, "2026-11-01T08:00:00Z"),
            make_post(101, "2026-10-20T08:00:00Z"),
        ],
        events=[
            make_event(201, "2026-11-05T13:00:00Z", "2026-11-05T14:00:00Z", "Ouderavond"),
            make_event(
                202,
                "2026-11-10T00:00:00+01:00",
                "2026-11-11T00:00:00+01:00",
                "Studiedag",
                all_day=True,
            ),
        ],
    )


@pytest.fixture
def mock_api(aioclient_mock: AiohttpClientMocker, api_state: ApiState) -> AiohttpClientMocker:
    """Serve ``api_state`` on every endpoint the integration may call, logging each request."""

    def _log(method: str, url: URL) -> None:
        # The mocker appends (method, url, data, headers) right before awaiting a handler.
        headers = aioclient_mock.mock_calls[-1][3] or {}
        api_state.request_log.append((method, url, dict(headers)))

    def _reply(method: str, url: URL, status: int, payload: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method, url, status=status, json=payload if status == 200 else {}
        )

    async def _token(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _log(method, url)
        api_state.token_calls += 1
        if api_state.token_hold is not None:
            await api_state.token_hold.wait()
        if api_state.token_exc is not None:
            raise api_state.token_exc
        if api_state.token_status != 200:
            return _reply(method, url, api_state.token_status, None)
        refresh_token = api_state.token_rotates_to or data["refresh_token"]
        return _reply(
            method,
            url,
            200,
            {
                "access_token": f"private-access-{api_state.token_calls}",
                "refresh_token": refresh_token,
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    async def _account(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _log(method, url)
        status = (
            api_state.account_status_queue.pop(0)
            if api_state.account_status_queue
            else api_state.account_status
        )
        return _reply(method, url, status, api_state.account)

    async def _posts(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _log(method, url)
        status = (
            api_state.posts_status_queue.pop(0)
            if api_state.posts_status_queue
            else api_state.posts_status
        )
        page = {
            "values": api_state.posts,
            "totalCount": len(api_state.posts),
            "timeStamp": "2026-11-04T10:00:00Z",
        }
        return _reply(method, url, status, page)

    async def _post_detail(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _log(method, url)
        post_id = int(url.path.rsplit("/", 1)[1])
        detail = api_state.post_details.get(post_id) or next(
            (post for post in api_state.posts if post["id"] == post_id), None
        )
        return _reply(method, url, 200 if detail else 404, detail)

    async def _events(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _log(method, url)
        return _reply(method, url, api_state.events_status, api_state.events)

    aioclient_mock.post(TOKEN_URL, side_effect=_token)
    aioclient_mock.get(ACCOUNT_URL, side_effect=_account)
    aioclient_mock.get(POSTS_URL, side_effect=_posts)
    aioclient_mock.get(EVENTS_URL, side_effect=_events)
    aioclient_mock.get(POST_DETAIL_PATTERN, side_effect=_post_detail)
    return aioclient_mock


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options of the mock config entry; parametrize to test option handling."""
    return {}


@pytest.fixture
def mock_config_entry(entry_options: dict[str, Any]) -> MockConfigEntry:
    """Return a config entry for the synthetic parent account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Private Parent",
        unique_id=str(USER_PROFILE_ID),
        entry_id=ENTRY_ID,
        data={
            CONF_USERNAME: "private-user@example.test",
            CONF_REFRESH_TOKEN: "private-refresh",
            CONF_CLIENT_ID: OIDC_CLIENT_ID,
            CONF_USER_PROFILE_ID: USER_PROFILE_ID,
        },
        options=entry_options,
    )


async def setup_integration(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    language: str = "nl",
    time_zone: str = "Europe/Amsterdam",
) -> MockConfigEntry:
    """Add ``entry`` and set it up in ``language``, by default in the school's time zone."""
    hass.config.language = language
    await hass.config.async_set_time_zone(time_zone)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> MockConfigEntry:
    """Set the entry up on a Dutch Home Assistant against the fake API."""
    return await setup_integration(hass, mock_config_entry)


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Move the frozen clock forward, fire the timers that became due and let the work finish."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
