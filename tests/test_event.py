"""Tests for the new-post event entity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import (
    ATTR_POST_ID,
    ATTR_SHORT_TEXT,
    EVENT_TYPE_NEW_POST,
)
from custom_components.socialschools.event import SocialSchoolsNewPostEvent
from tests.conftest import ApiState, advance, make_post, setup_integration

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
EVENT_ID = "event.social_schools_private_school_nieuw_bericht"
STATIC_ATTRIBUTES = {"friendly_name", "event_types"}

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


def _state(hass: HomeAssistant) -> State:
    """Return the state of the event entity, which must exist."""
    state = hass.states.get(EVENT_ID)
    assert state is not None
    return state


def _event_attributes(state: State) -> dict[str, Any]:
    """Return the attributes of the last event, without the static entity attributes."""
    return {key: value for key, value in state.attributes.items() if key not in STATIC_ATTRIBUTES}


def _changes(events: list[Event]) -> list[State]:
    """Return the new states of the event entity among captured ``state_changed`` events."""
    return [event.data["new_state"] for event in events if event.data["entity_id"] == EVENT_ID]


async def test_nothing_fires_on_seeding(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The seeding refresh fires nothing: the entity starts unknown."""
    state = _state(hass)
    assert state.state == "unknown"
    assert state.attributes == {
        "event_types": [EVENT_TYPE_NEW_POST],
        "event_type": None,
        "friendly_name": "Social Schools · Private School Nieuw bericht",
    }


async def test_short_text_is_not_recorded(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """``tekst_kort`` stays out of the recorder, like the text attributes of the sensors."""
    assert SocialSchoolsNewPostEvent._unrecorded_attributes == {ATTR_SHORT_TEXT}
    info = _state(hass).state_info
    assert info is not None
    assert {ATTR_SHORT_TEXT, "event_types"} <= info["unrecorded_attributes"]


async def test_new_post_fires_one_event(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A new post sets the timestamp state and exactly the documented event attributes."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    state = _state(hass)
    assert state.state == "2026-11-04T10:15:00.000+00:00"
    assert _event_attributes(state) == {
        "event_type": "nieuw_bericht",
        "post_id": 104,
        "titel": "Bericht 104",
        "groep": "Groep 3",
        "auteur": "Private Teacher",
        "geplaatst": "2026-11-04T10:00:00+01:00",
        "tekst_kort": "Hallo wereld",
    }


async def test_two_new_posts_fire_in_chronological_order(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """Two new posts in one poll give two state changes, oldest post first."""
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    api_state.posts[0:0] = [
        make_post(105, "2026-11-04T09:30:00Z"),
        make_post(104, "2026-11-04T09:00:00Z"),
    ]
    await advance(hass, freezer, POLL)
    states = _changes(changes)
    assert [state.attributes[ATTR_POST_ID] for state in states] == [104, 105]
    assert [state.state for state in states] == [
        "2026-11-04T10:15:00.000+00:00",
        "2026-11-04T10:15:00.001+00:00",
    ]
    assert _state(hass).attributes[ATTR_POST_ID] == 105


async def test_known_post_does_not_fire_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A post that stays in the feed is reported once."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    fired = _state(hass)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    await advance(hass, freezer, POLL)
    assert _changes(changes) == []
    state = _state(hass)
    assert state.state == fired.state
    assert state.attributes == fired.attributes


async def test_failed_poll_does_not_fire_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A failed refresh re-notifies the entity with the previous data; the event is not repeated."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    fired = _state(hass)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    api_state.posts_status = 500
    await advance(hass, freezer, POLL)
    assert _changes(changes) == []
    state = _state(hass)
    assert state.state == fired.state
    assert state.attributes == fired.attributes


async def test_restart_restores_the_last_event_and_fires_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """After unload and setup the last event is back and a post that appeared meanwhile is silent."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)
    fired = _state(hass)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert _state(hass).state == "unavailable"
    api_state.posts.insert(0, make_post(105, "2026-11-04T10:20:00Z"))
    await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    restored = _state(hass)
    assert restored.state == fired.state
    assert restored.attributes == fired.attributes
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    await advance(hass, freezer, POLL)
    assert _changes(changes) == []
    assert _state(hass).attributes[ATTR_POST_ID] == 104


async def test_last_event_is_restored_from_storage(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """After a Home Assistant restart the last event comes back from the restore cache."""
    attributes = {
        "post_id": 103,
        "titel": "Bericht 103",
        "groep": "Groep 3",
        "auteur": "Private Teacher",
        "geplaatst": "2026-11-03T09:00:00+01:00",
        "tekst_kort": "Hallo wereld",
    }
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    EVENT_ID,
                    "2026-11-03T08:05:00.000+00:00",
                    {"event_type": EVENT_TYPE_NEW_POST, **attributes},
                ),
                {"last_event_type": EVENT_TYPE_NEW_POST, "last_event_attributes": attributes},
            )
        ],
    )
    await setup_integration(hass, mock_config_entry)
    state = _state(hass)
    assert state.state == "2026-11-03T08:05:00.000+00:00"
    assert _event_attributes(state) == {"event_type": EVENT_TYPE_NEW_POST, **attributes}
