"""Tests for the config entry lifecycle of the Social Schools integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.socialschools.const import (
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
    VERSION,
)
from tests.conftest import (
    ACCOUNT_PATH,
    POSTS_PATH,
    STORAGE_KEY,
    TOKEN_PATH,
    ApiState,
    make_account,
    setup_integration,
)

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)

DUTCH_ENTITY_IDS = {
    "sensor.social_schools_private_school_laatste_bericht": "4242_7_latest_post",
    "sensor.social_schools_private_school_berichten": "4242_7_post_count",
    "sensor.social_schools_private_school_ongelezen": "4242_7_unread",
    "sensor.social_schools_private_school_geopend_bericht": "4242_7_open_post",
    "event.social_schools_private_school_nieuw_bericht": "4242_7_new_post",
    "calendar.social_schools_private_school_agenda": "4242_7_calendar",
}
ENGLISH_ENTITY_IDS = (
    "sensor.social_schools_private_school_latest_post",
    "sensor.social_schools_private_school_posts",
    "sensor.social_schools_private_school_unread",
    "sensor.social_schools_private_school_opened_post",
    "event.social_schools_private_school_new_post",
    "calendar.social_schools_private_school_calendar",
)


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    """Return the reauth flows of the integration that are in progress."""
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"]["source"] == SOURCE_REAUTH
    ]


async def _run_loop(turns: int = 20) -> None:
    """Let the event loop run ``turns`` iterations."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def _wait_until(condition: Callable[[], bool], *, turns: int = 50) -> None:
    """Let the event loop run until ``condition`` holds."""
    for _ in range(turns):
        if condition():
            return
        await asyncio.sleep(0)
    assert condition(), "the condition did not come true"


async def test_setup_creates_device_and_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """One service device per school with the four entities, named in Dutch."""
    assert init_integration.state is ConfigEntryState.LOADED
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, "4242_7"), init_integration.entry_id
    )
    assert device is not None
    assert device.name == "Social Schools · Private School"
    assert device.manufacturer == "Social Schools"
    assert device.model == "School"
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.configuration_url == "https://app.socialschools.eu/"
    for entity_id, unique_id in DUTCH_ENTITY_IDS.items():
        entry = entity_registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.unique_id == unique_id
        assert entry.device_id == device.id
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state != "unavailable"
    assert len(er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)) == 6


async def test_english_entity_ids(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """An English Home Assistant gets English object ids."""
    await setup_integration(hass, mock_config_entry, language="en")
    assert mock_config_entry.state is ConfigEntryState.LOADED
    for entity_id in ENGLISH_ENTITY_IDS:
        assert entity_registry.async_get(entity_id) is not None, entity_id


async def test_two_schools(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Two parent roles give two devices and eight entities; the employee role is ignored."""
    api_state.account = make_account(schools=[(7, "Private School"), (8, "Second School")])
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(device_registry, mock_config_entry.entry_id)
    assert {device.name for device in devices} == {
        "Social Schools · Private School",
        "Social Schools · Second School",
    }
    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, "4242_99"), mock_config_entry.entry_id
        )
        is None
    )
    entities = er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)
    assert len(entities) == 12
    assert entity_registry.async_get("sensor.social_schools_second_school_berichten") is not None
    assert {headers["Schoolid"] for _, _, headers in api_state.requests_for(POSTS_PATH)} == {
        "7",
        "8",
    }


async def test_requests_carry_auth_and_school_headers(
    init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """Every API call carries the bearer token and user agent; school calls add the school headers."""
    assert api_state.token_calls == 1
    assert len(api_state.requests_for(TOKEN_PATH)) == 1
    ((_, _, account_headers),) = api_state.requests_for(ACCOUNT_PATH)
    assert account_headers["Authorization"] == "Bearer private-access-1"
    assert account_headers["User-Agent"] == f"HomeAssistant-SocialSchools/{VERSION}"
    assert "Schoolid" not in account_headers
    assert "Roletypeid" not in account_headers
    ((_, _, posts_headers),) = api_state.requests_for(POSTS_PATH)
    assert posts_headers["Authorization"] == "Bearer private-access-1"
    assert posts_headers["Schoolid"] == "7"
    assert posts_headers["Roletypeid"] == "1"
    assert posts_headers["User-Agent"] == f"HomeAssistant-SocialSchools/{VERSION}"


async def test_account_unreachable_retries_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A 5xx on the account call is transient: setup is retried, no reauth."""
    api_state.account_status = 503
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()
    assert not hasattr(mock_config_entry, "runtime_data")


async def test_rejected_refresh_token_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A rejected refresh token at setup fails the entry and starts a reauth flow."""
    api_state.token_status = 400
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert not api_state.requests_for(ACCOUNT_PATH)
    flows = _reauth_flows(hass)
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id


async def test_no_parent_role_fails_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """An account without a parent role cannot be set up and does not start a reauth."""
    api_state.account = make_account(schools=[])
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "no_parent_role"
    assert not hass.config_entries.flow.async_progress()
    assert not api_state.requests_for(POSTS_PATH)


async def test_unload_entry(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Unloading removes the entities and the runtime data."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(init_integration, "runtime_data")
    state = hass.states.get("sensor.social_schools_private_school_laatste_bericht")
    assert state is not None
    assert state.state == "unavailable"


async def test_remove_entry_deletes_store(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict
) -> None:
    """Removing the entry deletes its bookkeeping file."""
    assert STORAGE_KEY in hass_storage
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage


async def test_children_names_never_leave_the_account_payload(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connected child without groups matches no post, so its name reaches no state.

    No child name ever reaches the store, the diagnostics or a log line; a child with
    groups appears only in the ``kinderen`` attributes (tests/test_children_account.py).
    """
    assert "Private Child" in json.dumps(api_state.account)
    caplog.set_level(logging.DEBUG)
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    states = json.dumps([state.as_dict() for state in hass.states.async_all()], default=str)
    assert "Private Child" not in states
    assert "Private Child" not in json.dumps(hass_storage, default=str)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)
    assert "Private Child" not in json.dumps(diagnostics)
    assert "Private Child" not in caplog.text


@pytest.mark.freeze_time(FROZEN_NOW)
async def test_unload_lets_an_in_flight_token_refresh_finish(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """Unloading during a token refresh waits for it, so the rotated refresh token is stored.

    Home Assistant cancels the coordinator on unload; a refresh the identity provider
    already completed would otherwise be lost together with the only valid refresh token.
    """
    api_state.token_rotates_to = "private-rotated"
    api_state.token_hold = asyncio.Event()
    # The access token lives an hour: the poll at +60 min renews it before fetching.
    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await _wait_until(lambda: api_state.token_calls == 2)
    unload = hass.async_create_task(hass.config_entries.async_unload(init_integration.entry_id))
    await _run_loop()
    assert not unload.done()
    assert init_integration.data[CONF_REFRESH_TOKEN] == "private-refresh"

    api_state.token_hold.set()
    assert await unload
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert init_integration.data[CONF_REFRESH_TOKEN] == "private-rotated"


async def test_rotation_reported_after_removal_is_dropped(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rotation that reaches the entry after its removal is logged at debug, not raised."""
    caplog.set_level(logging.DEBUG, logger="custom_components.socialschools")
    manager = init_integration.runtime_data.client.token_manager
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage
    api_state.token_rotates_to = "private-rotated"
    await manager.async_refresh()  # the old manager still reports to the removed entry
    assert manager.refresh_token == "private-rotated"
    assert f"Entry {init_integration.entry_id} was removed during a token refresh" in caplog.text


async def test_rotated_refresh_token_is_stored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A rotated refresh token is written to the entry data; the other keys stay."""
    api_state.token_rotates_to = "private-rotated"
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "private-rotated"
    assert mock_config_entry.data[CONF_USERNAME] == "private-user@example.test"


async def test_unrotated_refresh_token_leaves_entry_untouched(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Without rotation the entry is not updated at all."""
    with patch.object(
        hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
    ) as update_entry:
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "private-refresh"
    update_entry.assert_not_called()
