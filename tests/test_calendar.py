"""Tests for the school calendar entity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant, State
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from tests.conftest import EVENTS_PATH, ApiState, advance, make_event, setup_integration

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
CALENDAR_ID = "calendar.social_schools_private_school_agenda"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
OUDERAVOND = {
    "start": "2026-11-05T14:00:00+01:00",
    "end": "2026-11-05T15:00:00+01:00",
    "summary": "Ouderavond",
    "description": "Studiedag",
    "location": "Aula",
}
STUDIEDAG = {
    "start": "2026-11-10",
    "end": "2026-11-11",
    "summary": "Studiedag",
    "description": "Studiedag",
    "location": "Aula",
}

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


def _state(hass: HomeAssistant) -> State:
    """Return the state of the calendar entity, which must exist."""
    state = hass.states.get(CALENDAR_ID)
    assert state is not None
    return state


async def _get_events(hass: HomeAssistant, start: str, end: str) -> list[dict[str, Any]]:
    """Call ``calendar.get_events`` for the range and return the events of the school calendar."""
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": CALENDAR_ID, "start_date_time": start, "end_date_time": end},
        blocking=True,
        return_response=True,
    )
    return response[CALENDAR_ID]["events"]


async def test_upcoming_event(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Before the first event the calendar is off and shows that event in local time."""
    state = _state(hass)
    assert state.state == "off"
    assert state.attributes == {
        "message": "Ouderavond",
        "all_day": False,
        "start_time": "2026-11-05 14:00:00",
        "end_time": "2026-11-05 15:00:00",
        "location": "Aula",
        "description": "Studiedag",
        "friendly_name": "Social Schools · Private School Agenda",
    }


@pytest.mark.freeze_time("2026-11-05T13:30:00+00:00")
async def test_state_on_during_event(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """While an event is going on the calendar is on."""
    state = _state(hass)
    assert state.state == "on"
    assert state.attributes["message"] == "Ouderavond"


@pytest.mark.parametrize(
    "expected_state",
    [
        pytest.param("off", marks=pytest.mark.freeze_time("2026-11-09T10:00:00+00:00")),
        pytest.param("on", marks=pytest.mark.freeze_time("2026-11-10T12:00:00+00:00")),
    ],
)
async def test_all_day_event(
    hass: HomeAssistant, init_integration: MockConfigEntry, expected_state: str
) -> None:
    """An all-day event spans local midnight to the next local midnight."""
    state = _state(hass)
    assert state.state == expected_state
    assert state.attributes["message"] == "Studiedag"
    assert state.attributes["all_day"] is True
    assert state.attributes["start_time"] == "2026-11-10 00:00:00"
    assert state.attributes["end_time"] == "2026-11-11 00:00:00"


@pytest.mark.freeze_time("2026-11-11T03:00:00+00:00")
async def test_all_day_event_follows_the_home_assistant_time_zone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """All-day boundaries are days of Home Assistant's own zone, as its state logic sees them.

    At 03:00 UTC the study day of 10 November is over in Amsterdam but still going on in
    Los Angeles: the entity must agree with Home Assistant, which evaluates the dates
    in its configured zone, instead of dropping the event hours early.
    """
    await setup_integration(hass, mock_config_entry, time_zone="America/Los_Angeles")
    state = _state(hass)
    assert state.state == "on"
    assert state.attributes["message"] == "Studiedag"
    assert state.attributes["all_day"] is True
    assert state.attributes["start_time"] == "2026-11-10 00:00:00"
    assert state.attributes["end_time"] == "2026-11-11 00:00:00"
    assert await _get_events(hass, "2026-11-10T16:00:00-08:00", "2026-11-11T00:00:00-08:00") == [
        STUDIEDAG
    ]
    assert await _get_events(hass, "2026-11-11T00:00:00-08:00", "2026-11-12T00:00:00-08:00") == []


async def test_without_events(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """An empty agenda is off without event attributes and answers with no events."""
    api_state.events = []
    await setup_integration(hass, mock_config_entry)
    state = _state(hass)
    assert state.state == "off"
    assert set(state.attributes) == {"friendly_name"}
    assert await _get_events(hass, "2026-11-04T00:00:00+01:00", "2027-01-01T00:00:00+01:00") == []


async def test_get_events_service(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The service returns timed events as local ISO strings and all-day events as dates."""
    events = await _get_events(hass, "2026-11-04T00:00:00+01:00", "2026-11-12T00:00:00+01:00")
    assert events == [OUDERAVOND, STUDIEDAG]


async def test_get_events_with_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A duration counts from now."""
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": CALENDAR_ID, "duration": {"days": 2}},
        blocking=True,
        return_response=True,
    )
    assert response == {CALENDAR_ID: {"events": [OUDERAVOND]}}


async def test_events_outside_the_range_are_excluded(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Only events overlapping the half-open range are returned."""
    assert await _get_events(hass, "2026-11-06T00:00:00+01:00", "2026-11-10T00:00:00+01:00") == []
    assert await _get_events(hass, "2026-11-10T00:00:00+01:00", "2026-11-11T00:00:00+01:00") == [
        STUDIEDAG
    ]
    assert await _get_events(hass, "2026-11-05T14:30:00+01:00", "2026-11-05T14:45:00+01:00") == [
        OUDERAVOND
    ]
    assert await _get_events(hass, "2026-11-05T15:00:00+01:00", "2026-11-06T00:00:00+01:00") == []


async def test_all_day_event_ending_before_midnight(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """An all-day event that ends at 23:59:59 on its day gets the next day as exclusive end."""
    api_state.events.append(
        make_event(
            203, "2026-11-12T00:00:00+01:00", "2026-11-12T23:59:59+01:00", "Sportdag", all_day=True
        )
    )
    await setup_integration(hass, mock_config_entry)
    events = await _get_events(hass, "2026-11-12T00:00:00+01:00", "2026-11-14T00:00:00+01:00")
    assert events == [
        {
            "start": "2026-11-12",
            "end": "2026-11-13",
            "summary": "Sportdag",
            "description": "Studiedag",
            "location": "Aula",
        }
    ]


async def test_untitled_event(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """An event without title gets a placeholder summary; empty description and location are omitted."""
    api_state.events = [
        make_event(
            204, "2026-11-06T09:00:00Z", "2026-11-06T10:00:00Z", "", description="", location=""
        )
    ]
    await setup_integration(hass, mock_config_entry)
    state = _state(hass)
    assert state.attributes["message"] == "(geen titel)"
    assert state.attributes["description"] == ""
    assert state.attributes["location"] == ""
    assert await _get_events(hass, "2026-11-06T00:00:00+01:00", "2026-11-07T00:00:00+01:00") == [
        {
            "start": "2026-11-06T10:00:00+01:00",
            "end": "2026-11-06T11:00:00+01:00",
            "summary": "(geen titel)",
        }
    ]


async def test_stale_events_stay_available(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A failing hourly refresh keeps the previous agenda; the entity keeps answering."""
    api_state.events_status = 500
    for _ in range(4):
        await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 2
    state = _state(hass)
    assert state.state == "off"
    assert state.attributes["message"] == "Ouderavond"
    events = await _get_events(hass, "2026-11-04T00:00:00+01:00", "2026-11-12T00:00:00+01:00")
    assert events == [OUDERAVOND, STUDIEDAG]


async def test_uids_are_stable_across_polls(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """The entity's events carry ``socialschools-<id>`` uids that survive a refetch."""
    entity = hass.data[DATA_COMPONENT].get_entity(CALENDAR_ID)
    assert entity is not None
    start = datetime(2026, 11, 4, tzinfo=AMSTERDAM)
    end = datetime(2026, 11, 12, tzinfo=AMSTERDAM)
    before = await entity.async_get_events(hass, start, end)
    assert [event.uid for event in before] == ["socialschools-201", "socialschools-202"]
    assert entity.event is not None
    assert entity.event.uid == "socialschools-201"
    for _ in range(4):
        await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(EVENTS_PATH)) == 2
    after = await entity.async_get_events(hass, start, end)
    assert after == before
