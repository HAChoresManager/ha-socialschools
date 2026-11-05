"""Tests for the coordinator: polling, new-post detection, the store and error handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import (
    DOMAIN,
    OPT_CALENDAR_DAYS,
    OPT_POSTS_INTERVAL,
    SEEN_IDS_LIMIT,
)
from custom_components.socialschools.coordinator import SocialSchoolsCoordinator
from tests.conftest import (
    EVENTS_PATH,
    POSTS_PATH,
    STORAGE_KEY,
    ApiState,
    advance,
    make_account,
    make_post,
    setup_integration,
)

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
SENSOR_ID = "sensor.social_schools_private_school_laatste_bericht"

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


def _coordinator(entry: MockConfigEntry) -> SocialSchoolsCoordinator:
    """Return the coordinator of a loaded entry."""
    return entry.runtime_data


def _new_ids(entry: MockConfigEntry, school_id: int = 7) -> list[int]:
    """Return the ids of the posts reported as new in the last update."""
    return [post.id for post in _coordinator(entry).data.schools[school_id].new_posts]


def _stored(hass_storage: dict[str, Any], school_id: int = 7) -> dict[str, Any]:
    """Return the stored bookkeeping of a school."""
    return hass_storage[STORAGE_KEY]["data"]["schools"][str(school_id)]


async def test_first_refresh_seeds_store(
    init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """The first refresh records every post and reports nothing as new."""
    coordinator = _coordinator(init_integration)
    assert coordinator.last_update_success
    assert coordinator.consecutive_failures == 0
    assert coordinator.last_success_utc == FROZEN_NOW
    assert coordinator.update_interval == POLL
    assert hass_storage[STORAGE_KEY]["data"] == {
        "schools": {"7": {"high_water": "2026-11-03T08:00:00+00:00", "seen_ids": [101, 102, 103]}}
    }
    school = coordinator.data.schools[7]
    assert school.school.name == "Private School"
    assert [post.id for post in school.posts] == [103, 102, 101]
    assert [event.id for event in school.events] == [201, 202]
    assert school.events_fetched_at == FROZEN_NOW
    assert school.new_posts == []


async def test_new_post_is_reported_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """A post with an unseen id is new in exactly one update and then recorded."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == [104]
    assert _stored(hass_storage) == {
        "high_water": "2026-11-04T09:00:00+00:00",
        "seen_ids": [101, 102, 103, 104],
    }
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == []
    assert [post.id for post in _coordinator(init_integration).data.schools[7].posts] == [
        104,
        103,
        102,
        101,
    ]


async def test_two_new_posts_are_chronological(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """Several new posts are reported oldest first."""
    api_state.posts[0:0] = [
        make_post(105, "2026-11-04T09:30:00Z"),
        make_post(104, "2026-11-04T09:00:00Z"),
    ]
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == [104, 105]


async def test_edited_post_is_not_new(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """A post that keeps its id is never new, whatever changed in it."""
    api_state.posts[0] = make_post(
        103, "2026-11-03T08:00:00Z", title="Gewijzigde titel", body="<p>Nieuwe tekst</p>"
    )
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == []
    assert _coordinator(init_integration).data.schools[7].posts[0].title == "Gewijzigde titel"
    assert _stored(hass_storage)["seen_ids"] == [101, 102, 103]


async def test_unseen_post_older_than_grace_is_not_new(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """Only unseen posts created after ``high_water - NEW_POST_GRACE`` count as new."""
    api_state.posts.append(make_post(95, "2026-10-30T08:00:00Z"))  # within the grace window
    api_state.posts.append(make_post(90, "2026-10-01T08:00:00Z"))  # long before it
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == [95]
    assert _stored(hass_storage) == {
        "high_water": "2026-11-03T08:00:00+00:00",
        "seen_ids": [101, 102, 103, 95],
    }
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == []


async def test_restart_seeds_again_without_reporting(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """After a restart the first refresh only seeds, even when the feed holds a new id."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    assert _new_ids(init_integration) == []
    assert _stored(hass_storage) == {
        "high_water": "2026-11-04T09:00:00+00:00",
        "seen_ids": [101, 102, 103, 104],
    }
    await advance(hass, freezer, POLL)
    assert _new_ids(init_integration) == []


async def test_seen_ids_are_trimmed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The stored id list keeps only the newest SEEN_IDS_LIMIT entries."""
    stored_ids = list(range(1000, 1000 + SEEN_IDS_LIMIT - 1))
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "schools": {"7": {"high_water": "2026-11-03T08:00:00+00:00", "seen_ids": stored_ids}}
        },
    }
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    seen_ids = _stored(hass_storage)["seen_ids"]
    assert len(seen_ids) == SEEN_IDS_LIMIT
    assert seen_ids == [*stored_ids[2:], 101, 102, 103]


async def test_two_schools_are_tracked_separately(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """Every school has its own bookkeeping and its own new posts."""
    api_state.account = make_account(schools=[(7, "Private School"), (8, "Second School")])
    await setup_integration(hass, mock_config_entry)
    assert set(hass_storage[STORAGE_KEY]["data"]["schools"]) == {"7", "8"}
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    assert _new_ids(mock_config_entry, 7) == [104]
    assert _new_ids(mock_config_entry, 8) == [104]
    assert _stored(hass_storage, 8)["seen_ids"] == [101, 102, 103, 104]


async def test_events_are_refreshed_hourly(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """The calendar is fetched at the first refresh and then at most once an hour."""
    ((_, url, _),) = api_state.requests_for(EVENTS_PATH)
    assert url.query["from"] == "2026-10-28T10:00:00Z"
    assert url.query["until"] == "2027-02-02T10:00:00Z"
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(POSTS_PATH)) == 2
    assert len(api_state.requests_for(EVENTS_PATH)) == 1
    for _ in range(3):
        await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(POSTS_PATH)) == 5
    events_requests = api_state.requests_for(EVENTS_PATH)
    assert len(events_requests) == 2
    assert events_requests[1][1].query["from"] == "2026-10-28T11:00:00Z"
    assert events_requests[1][1].query["until"] == "2027-02-02T11:00:00Z"
    school = _coordinator(init_integration).data.schools[7]
    assert school.events_fetched_at == FROZEN_NOW + timedelta(hours=1)
    assert [event.id for event in school.events] == [201, 202]


async def test_posts_failure_keeps_data_and_recovers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx on the feed marks the update failed but keeps the data and the entities.

    The failure and the recovery are logged under the coordinator's name, which must
    not carry the entry title (the parent's display name).
    """
    coordinator = _coordinator(init_integration)
    assert "Private Parent" not in coordinator.name
    data_before = coordinator.data
    state_before = hass.states.get(SENSOR_ID)
    assert state_before is not None
    api_state.posts_status = 500
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is False
    assert coordinator.consecutive_failures == 1
    assert coordinator.last_success_utc == FROZEN_NOW
    assert coordinator.data is data_before
    state = hass.states.get(SENSOR_ID)
    assert state is not None
    assert state.state == state_before.state
    assert state.state != "unavailable"
    await advance(hass, freezer, POLL)
    assert coordinator.consecutive_failures == 2
    assert not hass.config_entries.flow.async_progress()
    api_state.posts_status = 200
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is True
    assert coordinator.consecutive_failures == 0
    assert coordinator.last_success_utc == FROZEN_NOW + timedelta(minutes=45)
    assert _new_ids(init_integration) == [104]
    own_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.socialschools")
    ]
    assert any(line.startswith("Error fetching socialschools") for line in own_lines)
    assert any(line.startswith("Fetching socialschools") for line in own_lines)
    assert all("Private Parent" not in line for line in own_lines)
    assert all("private-" not in line for line in own_lines)


@pytest.mark.parametrize("failure", ["status", "timeout"])
async def test_token_failure_while_polling_is_transient(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    failure: str,
) -> None:
    """A token endpoint that fails when the access token expires is a failed update, no reauth."""
    coordinator = _coordinator(init_integration)
    data_before = coordinator.data
    if failure == "status":
        api_state.token_status = 503
    else:
        api_state.token_exc = TimeoutError()
    # The access token lives an hour: the poll at +60 min renews it before fetching.
    await advance(hass, freezer, timedelta(hours=1))
    assert api_state.token_calls == 2
    assert len(api_state.requests_for(POSTS_PATH)) == 1
    assert coordinator.last_update_success is False
    assert coordinator.consecutive_failures == 1
    assert coordinator.data is data_before
    assert hass.states.get(SENSOR_ID).state == "Bericht 103"
    assert not hass.config_entries.flow.async_progress()
    assert init_integration.state is ConfigEntryState.LOADED

    api_state.token_status = 200
    api_state.token_exc = None
    await advance(hass, freezer, POLL)
    assert api_state.token_calls == 3
    assert coordinator.last_update_success is True
    assert coordinator.consecutive_failures == 0
    (_, _, headers) = api_state.requests_for(POSTS_PATH)[-1]
    assert headers["Authorization"] == "Bearer private-access-3"


async def test_rejected_refresh_token_while_polling_starts_reauth(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A refresh rejected with 400 when the access token expires starts one reauth and stops polling."""
    coordinator = _coordinator(init_integration)
    api_state.token_status = 400
    await advance(hass, freezer, timedelta(hours=1))
    assert api_state.token_calls == 2
    assert len(api_state.requests_for(POSTS_PATH)) == 1
    assert coordinator.last_update_success is False
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id
    assert init_integration.state is ConfigEntryState.LOADED
    await advance(hass, freezer, POLL)
    assert api_state.token_calls == 2
    assert len(api_state.requests_for(POSTS_PATH)) == 1


async def test_events_failure_keeps_old_events(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing calendar refresh keeps the old events, warns once and is retried."""
    coordinator = _coordinator(init_integration)
    api_state.events_status = 500
    for _ in range(4):
        await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 2
    assert coordinator.last_update_success is True
    school = coordinator.data.schools[7]
    assert [event.id for event in school.events] == [201, 202]
    assert school.events_fetched_at == FROZEN_NOW
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 3
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "calendar" in record.getMessage()
    ]
    assert len(warnings) == 1
    api_state.events_status = 200
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 4
    school = coordinator.data.schools[7]
    assert school.events_fetched_at == FROZEN_NOW + timedelta(minutes=90)
    assert [event.id for event in school.events] == [201, 202]


async def test_persistent_401_starts_reauth_and_stops_polling(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A 401 that survives a token refresh starts a reauth flow and stops the polling."""
    coordinator = _coordinator(init_integration)
    api_state.posts_status = 401
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is False
    assert api_state.token_calls == 2
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id
    assert init_integration.state is ConfigEntryState.LOADED
    posts_requests = len(api_state.requests_for(POSTS_PATH))
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(POSTS_PATH)) == posts_requests


async def test_single_401_refreshes_the_token(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """One 401 is answered with a token refresh and a retry; the update succeeds."""
    api_state.posts_status_queue.append(401)
    await advance(hass, freezer, POLL)
    coordinator = _coordinator(init_integration)
    assert coordinator.last_update_success is True
    assert api_state.token_calls == 2
    assert [headers["Authorization"] for _, _, headers in api_state.requests_for(POSTS_PATH)] == [
        "Bearer private-access-1",
        "Bearer private-access-1",
        "Bearer private-access-2",
    ]
    assert not hass.config_entries.flow.async_progress()


@pytest.mark.parametrize("entry_options", [{OPT_CALENDAR_DAYS: 30}])
async def test_calendar_days_option_sets_the_horizon(
    init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """The calendar_days option changes the ``until`` boundary of the events window."""
    ((_, url, _),) = api_state.requests_for(EVENTS_PATH)
    assert url.query["from"] == "2026-10-28T10:00:00Z"
    assert url.query["until"] == "2026-12-04T10:00:00Z"


@pytest.mark.parametrize(
    ("entry_options", "minutes"),
    [({OPT_POSTS_INTERVAL: 10}, 10), ({OPT_POSTS_INTERVAL: 5}, 10), ({}, 15)],
)
async def test_posts_interval_option(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    minutes: int,
) -> None:
    """The posts interval option is used, clamped to the minimum."""
    assert _coordinator(init_integration).update_interval == timedelta(minutes=minutes)
    await advance(hass, freezer, timedelta(minutes=minutes - 1))
    assert len(api_state.requests_for(POSTS_PATH)) == 1
    await advance(hass, freezer, timedelta(minutes=1))
    assert len(api_state.requests_for(POSTS_PATH)) == 2


async def test_events_failure_at_setup_is_retried(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A calendar failure at setup does not block the entry; the next poll fetches it."""
    api_state.events_status = 500
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    school = _coordinator(mock_config_entry).data.schools[7]
    assert school.events == []
    assert school.events_fetched_at is None
    api_state.events_status = 200
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 2
    school = _coordinator(mock_config_entry).data.schools[7]
    assert [event.id for event in school.events] == [201, 202]
    assert school.events_fetched_at == FROZEN_NOW + POLL


async def test_post_details_are_fetched_when_enabled(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """With FETCH_POST_DETAILS the newest posts get their detail text, fetched once per id."""
    api_state.post_details[103] = make_post(
        103, "2026-11-03T08:00:00Z", body="<p>Volledige <i>tekst</i></p>"
    )

    def _detail_paths() -> list[str]:
        return [
            url.path for _, url, _ in api_state.request_log if url.path.startswith(f"{POSTS_PATH}/")
        ]

    with patch("custom_components.socialschools.coordinator.FETCH_POST_DETAILS", True):
        await setup_integration(hass, mock_config_entry)
        assert _detail_paths() == [f"{POSTS_PATH}/103", f"{POSTS_PATH}/102", f"{POSTS_PATH}/101"]
        posts = _coordinator(mock_config_entry).data.schools[7].posts
        assert posts[0].text == "Volledige tekst"
        assert posts[1].text == "Hallo wereld"
        api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
        await advance(hass, freezer, POLL)
        assert _detail_paths()[3:] == [f"{POSTS_PATH}/104"]
        assert _new_ids(mock_config_entry) == [104]
