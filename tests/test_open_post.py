"""Tests for ``open_bericht`` and the opened-post sensor.

A post in the current feed is shown as the feed has it, without any request.
Only a post that has dropped out of the feed, or a feed text that looks cut off,
is fetched with ``GET communityposts/{id}``, once, and then served from the cache.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
from tests.conftest import (
    STORAGE_KEY,
    ApiState,
    advance,
    make_post,
    setup_integration,
)

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
OPEN_ID = "sensor.social_schools_private_school_geopend_bericht"
UNREAD_ID = "sensor.social_schools_private_school_ongelezen"
DETAIL_PREFIX = "/api/v1/communityposts/"

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


async def _open(hass: HomeAssistant, post_id: int) -> None:
    """Call ``open_bericht`` and wait for it."""
    await hass.services.async_call(DOMAIN, SERVICE_OPEN_POST, {"post_id": post_id}, blocking=True)


def _detail_requests(api_state: ApiState, post_id: int | None = None) -> int:
    """Return how often the detail of a post (or of any post) was requested."""
    return sum(
        1
        for _method, url, _headers in api_state.request_log
        if url.path.startswith(DETAIL_PREFIX)
        and (post_id is None or url.path == f"{DETAIL_PREFIX}{post_id}")
    )


def _attributes(hass: HomeAssistant) -> dict[str, Any]:
    """Return the attributes of the opened-post sensor without the friendly name."""
    attributes = dict(hass.states.get(OPEN_ID).attributes)
    attributes.pop("friendly_name")
    return attributes


async def _drop_out_of_feed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, api_state: ApiState, post_id: int
) -> None:
    """Let ``post_id`` drop out of the feed and poll; its detail stays available in the API."""
    post = next(item for item in api_state.posts if item["id"] == post_id)
    api_state.post_details.setdefault(post_id, post)
    api_state.posts = [item for item in api_state.posts if item["id"] != post_id]
    await advance(hass, freezer, POLL)


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


async def test_opening_a_feed_post_sends_no_request(
    hass: HomeAssistant, init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """A post in the feed is shown from the feed: not a single request goes out.

    The detail in the fake API differs on purpose; the sensor must show the feed text.
    """
    api_state.post_details[101] = make_post(
        101, "2026-10-20T08:00:00Z", body="<p>Tekst uit het detail</p>"
    )
    requests_before = len(api_state.request_log)
    for post_id in (101, 102, 103, 101):
        await _open(hass, post_id)
    assert len(api_state.request_log) == requests_before
    assert _detail_requests(api_state) == 0
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _attributes(hass)["tekst"] == "Hallo wereld"
    assert _attributes(hass)["post_id"] == 101


async def test_post_outside_the_feed_is_fetched_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A post that dropped out of the feed costs one request; after that it comes from the cache."""
    api_state.post_details[101] = make_post(
        101, "2026-10-20T08:00:00Z", body="<p>Volledige tekst</p><p>Tweede alinea</p>"
    )
    await _drop_out_of_feed(hass, freezer, api_state, 101)
    await _open(hass, 101)
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _attributes(hass)["tekst"] == "Volledige tekst\nTweede alinea"
    assert _detail_requests(api_state, 101) == 1
    await _open(hass, 102)  # still in the feed: no request
    await _open(hass, 101)  # from the cache
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _detail_requests(api_state, 101) == 1
    assert _detail_requests(api_state) == 1


async def test_post_seen_before_a_restart_can_be_opened(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """The seen ids of the store survive a restart, so an older feed post can still be opened."""
    hass_storage[STORAGE_KEY] = {
        "version": 2,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "schools": {
                "7": {
                    "high_water": "2026-11-03T08:00:00+00:00",
                    "seen_ids": [90, 101, 102, 103],
                    "read": {},
                    "readers": [],
                }
            }
        },
    }
    api_state.post_details[90] = make_post(90, "2026-10-01T08:00:00Z", body="<p>Oud bericht</p>")
    await setup_integration(hass, mock_config_entry)
    await _open(hass, 90)
    assert _attributes(hass)["tekst"] == "Oud bericht"
    assert _detail_requests(api_state, 90) == 1


async def test_unknown_post_is_refused_without_a_request(
    hass: HomeAssistant, init_integration: MockConfigEntry, api_state: ApiState
) -> None:
    """A post id that was never in a feed is refused before anything is sent."""
    requests_before = len(api_state.request_log)
    with pytest.raises(ServiceValidationError) as excinfo:
        await _open(hass, 999)
    assert excinfo.value.translation_key == "unknown_post"
    assert excinfo.value.translation_placeholders == {"post_id": "999"}
    assert len(api_state.request_log) == requests_before
    assert hass.states.get(OPEN_ID).state == "Bericht 103"


@pytest.mark.parametrize("ending", ["…", "...", "…  "])
async def test_feed_text_that_looks_cut_is_fetched_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    ending: str,
) -> None:
    """A feed text ending in an ellipsis may be cut off: the detail is fetched, once."""
    api_state.posts[1] = make_post(
        102, "2026-11-01T08:00:00Z", body=f"<p>Begin van de tekst{ending}</p>"
    )
    api_state.post_details[102] = make_post(
        102, "2026-11-01T08:00:00Z", body="<p>Begin van de tekst en de rest</p>"
    )
    await setup_integration(hass, mock_config_entry)
    await _open(hass, 102)
    assert _attributes(hass)["tekst"] == "Begin van de tekst en de rest"
    await _open(hass, 103)
    await _open(hass, 102)
    assert _detail_requests(api_state, 102) == 1
    assert _detail_requests(api_state) == 1


async def test_with_fetch_post_details_the_polled_detail_text_is_reused(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """With FETCH_POST_DETAILS a plain feed text counts as cut, a polled detail text does not.

    During the first poll every detail fails, so opening a post fetches its detail. Once
    a poll has fetched the details, the feed posts carry the full text: opening costs nothing.
    """
    api_state.post_details[103] = make_post(103, "2026-11-03T08:00:00Z", body="<p>Detail 103</p>")
    api_state.post_details[102] = make_post(102, "2026-11-01T08:00:00Z", body="<p>Detail 102</p>")
    with patch("custom_components.socialschools.coordinator.FETCH_POST_DETAILS", True):
        api_state.post_detail_status = 500
        await setup_integration(hass, mock_config_entry)
        assert _detail_requests(api_state) == 3  # 103, 102 and 101 during the first poll
        api_state.post_detail_status = 200
        await _open(hass, 102)
        assert _attributes(hass)["tekst"] == "Detail 102"
        assert _detail_requests(api_state, 102) == 2
        await advance(hass, freezer, POLL)  # this poll fetches the details successfully
        assert _detail_requests(api_state) == 7
        requests_before = len(api_state.request_log)
        await _open(hass, 103)
        assert _attributes(hass)["tekst"] == "Detail 103"
        assert len(api_state.request_log) == requests_before


async def test_cache_keeps_the_most_recent_posts(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """The cache holds OPEN_POST_CACHE_SIZE details; the least recently opened one goes first."""
    count = OPEN_POST_CACHE_SIZE + 1
    api_state.posts = [make_post(200 + i, f"2026-11-0{1 + i % 3}T08:00:00Z") for i in range(count)]
    await setup_integration(hass, mock_config_entry)
    api_state.post_details = {post["id"]: post for post in api_state.posts}
    api_state.posts = [make_post(300, "2026-11-04T09:00:00Z")]  # all earlier posts dropped out
    await advance(hass, freezer, POLL)
    for i in range(count):
        await _open(hass, 200 + i)
    assert _detail_requests(api_state) == count
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
    await advance(hass, freezer, POLL)
    await _open(hass, 104)
    assert _attributes(hass)["nog_lezen"] == ["Ouder Een"]
    assert hass.states.get(UNREAD_ID).state == "1"


async def test_failure_keeps_the_previous_post(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """A failed fetch raises a translated error and the previously opened post stays.

    Nothing is cached for the failed post: the next attempt asks again.
    """
    await _open(hass, 102)
    await _drop_out_of_feed(hass, freezer, api_state, 101)
    api_state.post_detail_status = 500
    with pytest.raises(HomeAssistantError) as excinfo:
        await _open(hass, 101)
    assert excinfo.value.translation_key == "open_failed"
    assert excinfo.value.translation_placeholders == {"post_id": "101"}
    assert hass.states.get(OPEN_ID).state == "Bericht 102"
    assert _attributes(hass)["post_id"] == 102
    api_state.post_detail_status = 200
    await _open(hass, 101)
    assert hass.states.get(OPEN_ID).state == "Bericht 101"
    assert _detail_requests(api_state, 101) == 2


@pytest.mark.parametrize(
    ("character", "expect_full_length"), [("x", True), ("é", False), ("😀", False)]
)
async def test_long_text_stays_within_the_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    character: str,
    expect_full_length: bool,
) -> None:
    """Up to MAX_OPEN_TEXT_LENGTH characters, cut further when the attribute budget requires."""
    api_state.posts[2] = make_post(101, "2026-10-20T08:00:00Z", body=f"<p>{character * 9000}</p>")
    await setup_integration(hass, mock_config_entry)
    await _open(hass, 101)
    assert _detail_requests(api_state) == 0
    attributes = _attributes(hass)
    assert attributes["afgekapt"] is True
    assert len(attributes["tekst"]) <= MAX_OPEN_TEXT_LENGTH
    assert (len(attributes["tekst"]) == MAX_OPEN_TEXT_LENGTH) is expect_full_length
    size = len(json.dumps(attributes, ensure_ascii=False).encode())
    assert size <= ATTRIBUTES_BYTE_BUDGET
    if not expect_full_length:  # the budget is used, not wasted
        assert size > ATTRIBUTES_BYTE_BUDGET - len(character.encode()) - 4
