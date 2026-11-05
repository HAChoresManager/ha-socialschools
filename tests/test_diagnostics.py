"""Tests for the redacted diagnostics of the Social Schools integration."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.socialschools.const import OIDC_CLIENT_ID, OPT_POSTS_INTERVAL
from custom_components.socialschools.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import ApiState, advance, make_account, setup_integration

pytestmark = pytest.mark.freeze_time("2026-11-04T10:00:00+00:00")

FROZEN_ISO = "2026-11-04T10:00:00+00:00"
# Fragments of the synthetic data that must never show up: names, secrets, titles,
# bodies, attachments, groups, locations and the username's domain.
CONTENT_FRAGMENTS = (
    "Private",
    "Child",
    "private-",
    "example.test",
    "Bericht",
    "Hallo",
    "wereld",
    "brief.pdf",
    "Groep 3",
    "Ouderavond",
    "Studiedag",
    "Aula",
    "cdn.",
)


def _assert_plain_and_anonymous(diagnostics: dict[str, Any]) -> None:
    """Assert the payload is plain JSON and carries no names, content or secrets."""
    dump = json.dumps(diagnostics, ensure_ascii=False)
    for fragment in CONTENT_FRAGMENTS:
        assert fragment not in dump, fragment


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """The download redacts the identifying entry fields and reports counts and timestamps."""
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert diagnostics == {
        "entry": {
            "title": REDACTED,
            "data": {
                "username": REDACTED,
                "refresh_token": REDACTED,
                "client_id": OIDC_CLIENT_ID,
                "user_profile_id": REDACTED,
            },
            "options": {},
        },
        "coordinator": {
            "last_update_success": True,
            "last_success_utc": FROZEN_ISO,
            "consecutive_failures": 0,
            "update_interval_seconds": 900,
            "schools": [
                {
                    "school_id": 7,
                    "post_count": 3,
                    "event_count": 2,
                    "events_fetched_at": FROZEN_ISO,
                    "newest_post_created": "2026-11-03T08:00:00+00:00",
                }
            ],
        },
    }
    _assert_plain_and_anonymous(diagnostics)


@pytest.mark.parametrize("entry_options", [{OPT_POSTS_INTERVAL: 20}])
async def test_diagnostics_after_a_failed_poll(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A failed poll shows up as a failure count while the stale school data stays listed."""
    api_state.posts_status = 503
    await advance(hass, freezer, timedelta(minutes=20))
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diagnostics["entry"]["options"] == {OPT_POSTS_INTERVAL: 20}
    coordinator = diagnostics["coordinator"]
    assert coordinator["last_update_success"] is False
    assert coordinator["consecutive_failures"] == 1
    assert coordinator["last_success_utc"] == FROZEN_ISO
    assert coordinator["update_interval_seconds"] == 1200
    assert [school["school_id"] for school in coordinator["schools"]] == [7]
    _assert_plain_and_anonymous(diagnostics)


async def test_diagnostics_with_two_schools_and_no_posts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Every school is listed by id only; a school without posts has no newest post."""
    api_state.account = make_account(schools=[(7, "Private School"), (8, "Second School")])
    api_state.posts = []
    api_state.events = []
    await setup_integration(hass, mock_config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["coordinator"]["schools"] == [
        {
            "school_id": school_id,
            "post_count": 0,
            "event_count": 0,
            "events_fetched_at": FROZEN_ISO,
            "newest_post_created": None,
        }
        for school_id in (7, 8)
    ]
    _assert_plain_and_anonymous(diagnostics)
    assert "Second" not in json.dumps(diagnostics)
