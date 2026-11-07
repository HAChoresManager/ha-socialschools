"""Tests for ``open_bericht`` and the opened-post sensor (full text of any feed post)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import (
    ATTRIBUTES_BYTE_BUDGET,
    DOMAIN,
    MAX_OPEN_TEXT_LENGTH,
    OPEN_POST_CACHE_SIZE,
    OPT_READERS,
    SERVICE_OPEN_POST,
)
from custom_components.socialschools.sensor import SocialSchoolsOpenPostSensor
from tests.conftest import ApiState, advance, make_post, setup_integration

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
OPEN_ID = "sensor.social_schools_private_school_geopend_bericht"
UNREAD_ID = "sensor.social_schools_private_school_ongelezen"

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


async def _open(hass: HomeAssistant, post_id: int) -> None:
    """Call ``open_bericht`` and wait for it."""
    await hass.services.async_call(DOMAIN, SERVICE_OPEN_POST, {"post_id": post_id}, blocking=True)


def _detail_requests(api_state: ApiState, post_id: int) -> int:
    """Return how often the detail of a post was requested."""
    return len(api_state.requests_for(f"/api/v1/communityposts/{post_id}"))


def _attributes(hass: HomeAssistant) -> dict[str, Any]:
    """Return the attributes of the opened-post sensor without the friendly name."""
    attributes = dict(hass.states.get(OPEN_ID).attributes)
    attributes.pop("friendly_name")
    return attributes


async def test_newest_post_is_shown_until_one_is_opened(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without an opened post the sensor shows the newest post; text and files are unrecorded."""
    assert hass.states.get(OPEN_ID).state == "Bericht 103"
    assert _attributes(hass) == {
        "post_id": 103,
        "groep": "Groep 3",
        "kinderen": [],
        "schoolbreed": True,
        "auteur": "Private Teacher",
        "geplaatst": "2026-11-03T09:00:00+01:00",
        "tekst": "Hallo wereld",
        "afgekapt": False,
        "bijlagen": ["brief.pdf"],
        "reacties": 1,
        "gelezen_door": [],
        "nog_lezen": [],
        "url": "https://app.socialschools.eu/communityposts/103",
    }
    assert {"tekst", "bijlagen"} <= SocialSchoolsOpenPostSensor._unrecorded_attributes


async def test_open_fetches_the_detail_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """Opening fetches ``communityposts/{id}``; opening it again comes from the cache."""
    api_state.post_details[101] = make_post(
        101, "2026-10-20T08:00:00Z", body="<p>Volledige tekst</p><p>Tweede alinea</p>"
    )
    await _open(hass, 101)
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _attributes(hass)["tekst"] == "Volledige tekst\nTweede alinea"
    assert _detail_requests(api_state, 101) == 1
    await _open(hass, 102)
    await _open(hass, 101)
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _detail_requests(api_state, 101) == 1
    assert _detail_requests(api_state, 102) == 1


async def test_cache_keeps_the_most_recent_posts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """The cache holds OPEN_POST_CACHE_SIZE details; the least recently opened one goes first."""
    count = OPEN_POST_CACHE_SIZE + 1
    api_state.posts = [make_post(200 + i, f"2026-11-0{1 + i % 3}T08:00:00Z") for i in range(count)]
    await setup_integration(hass, mock_config_entry)
    for i in range(count):
        await _open(hass, 200 + i)
    await _open(hass, 200 + count - 1)  # most recent: still cached
    assert _detail_requests(api_state, 200 + count - 1) == 1
    await _open(hass, 200)  # the oldest was evicted
    assert _detail_requests(api_state, 200) == 2


@pytest.mark.parametrize("entry_options", [{OPT_READERS: ["person.ouder_een"]}])
async def test_opening_marks_nothing_as_read(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Opening only shows the post; the dashboard marks it read with ``markeer_gelezen``."""
    hass.states.async_set("person.ouder_een", "home", {"friendly_name": "Ouder Een"})
    await setup_integration(hass, mock_config_entry)
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, timedelta(minutes=15))
    await _open(hass, 104)
    assert _attributes(hass)["nog_lezen"] == ["Ouder Een"]
    assert hass.states.get(UNREAD_ID).state == "1"


async def test_failure_keeps_the_previous_post(
    hass: HomeAssistant, init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """A failed fetch raises a translated error and the previously opened post stays."""
    await _open(hass, 102)
    api_state.post_detail_status = 500
    with pytest.raises(HomeAssistantError) as excinfo:
        await _open(hass, 101)
    assert excinfo.value.translation_key == "open_failed"
    assert excinfo.value.translation_placeholders == {"post_id": "101"}
    assert hass.states.get(OPEN_ID).state == "Bericht 102"


@pytest.mark.parametrize(
    ("character", "expect_full_length"), [("x", True), ("é", False), ("😀", False)]
)
async def test_long_text_stays_within_the_budget(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    api_state: ApiState,
    character: str,
    expect_full_length: bool,
) -> None:
    """Up to MAX_OPEN_TEXT_LENGTH characters, cut further when the attribute budget requires."""
    api_state.post_details[101] = make_post(
        101, "2026-10-20T08:00:00Z", body=f"<p>{character * 9000}</p>"
    )
    await _open(hass, 101)
    attributes = _attributes(hass)
    assert attributes["afgekapt"] is True
    assert len(attributes["tekst"]) <= MAX_OPEN_TEXT_LENGTH
    assert (len(attributes["tekst"]) == MAX_OPEN_TEXT_LENGTH) is expect_full_length
    size = len(json.dumps(attributes, ensure_ascii=False).encode())
    assert size <= ATTRIBUTES_BYTE_BUDGET
    if not expect_full_length:  # the budget is used, not wasted
        assert size > ATTRIBUTES_BYTE_BUDGET - len(character.encode()) - 4
