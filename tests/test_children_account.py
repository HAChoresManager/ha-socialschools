"""Tests for linking posts to children from the account (``connections[].groups``).

The live account lists each child of the parent role with ``groups``: the names of its
groups, which equal community names of the posts.  The ``kinderen`` option completes
and overrides that.  Names appear only in the ``kinderen`` attributes, never in the
store, the diagnostics or the log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import OPT_CHILDREN
from custom_components.socialschools.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import (
    ACCOUNT_PATH,
    ApiState,
    advance,
    make_account,
    make_post,
    setup_integration,
)

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
POSTS_ID = "sensor.social_schools_private_school_berichten"
OPEN_ID = "sensor.social_schools_private_school_geopend_bericht"
EVENT_ID = "event.social_schools_private_school_nieuw_bericht"
SECOND_POSTS_ID = "sensor.social_schools_second_school_berichten"
LIVE_LIST = ["6A", "1/2D", "5/6B", "5A", "4A", "1/2C", "4B", "Neo 4/5/6", "3A", "1/2B"]

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the warnings this integration logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.socialschools")
    ]


def _child(first_name: str, groups: list[Any], **extra: Any) -> dict[str, Any]:
    """Return a connection in the live shape: ``groups`` holds group names."""
    return {
        "alias": None,
        "avatar": None,
        "code": "private-code",
        "displayName": f"{first_name} Achternaam",
        "firstName": first_name,
        "groups": groups,
        "hasMissingConsentItemPreferences": False,
        "id": 600,
        "lastName": "Achternaam",
        "middleInitials": "",
        "roleType": 3,
        "yearClass": "6",
        **extra,
    }


def _with_children(api_state: ApiState, *connections: dict[str, Any]) -> None:
    """Give the parent role of school 7 these connections (children)."""
    api_state.account["roles"][0]["connections"] = list(connections)


def _posts(api_state: ApiState) -> None:
    """Posts in the child's group, in a look-alike group, school-wide and in no child's group."""
    api_state.posts = [
        make_post(105, "2026-11-03T12:00:00Z", groups=["6a"]),
        make_post(104, "2026-11-03T11:00:00Z", groups=["Schoolnieuws"]),
        make_post(103, "2026-11-03T10:00:00Z", groups=LIVE_LIST),
        make_post(102, "2026-11-03T09:00:00Z", groups=["Neo 4/5/6"]),
        make_post(101, "2026-11-03T08:00:00Z", groups=["6A", "1/2B"]),
    ]


def _linked(hass: HomeAssistant, entity_id: str = POSTS_ID) -> dict[int, tuple[list[str], bool]]:
    """Return ``{post_id: (kinderen, schoolbreed)}`` of the post list."""
    state = hass.states.get(entity_id)
    assert state is not None
    return {
        item["post_id"]: (item["kinderen"], item["schoolbreed"])
        for item in state.attributes["berichten"]
    }


async def test_posts_are_linked_to_the_children_of_the_account(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Without the option, the groups of the account link posts exactly; Neo 4/5/6 is not 6A."""
    _with_children(api_state, _child("Kind Een", ["6A"]), _child("Kind Twee", ["1/2B"]))
    _posts(api_state)
    await setup_integration(hass, mock_config_entry)
    assert _linked(hass) == {
        105: (["Kind Een"], False),
        104: ([], True),
        103: (["Kind Een", "Kind Twee"], True),
        102: ([], True),
        101: (["Kind Een", "Kind Twee"], False),
    }
    open_post = hass.states.get(OPEN_ID)
    assert open_post is not None
    assert open_post.attributes["kinderen"] == ["Kind Een"]  # the newest post, 105


async def test_new_post_event_names_the_child(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """The ``nieuw_bericht`` event carries the child of the account too."""
    _with_children(api_state, _child("Kind Een", ["6A"]))
    await setup_integration(hass, mock_config_entry)
    api_state.posts.insert(0, make_post(106, "2026-11-04T09:00:00Z", groups=["6A"]))
    await advance(hass, freezer, POLL)
    event = hass.states.get(EVENT_ID)
    assert event is not None
    assert event.attributes["post_id"] == 106
    assert event.attributes["kinderen"] == ["Kind Een"]
    assert event.attributes["schoolbreed"] is False


@pytest.mark.parametrize(
    "entry_options",
    [{OPT_CHILDREN: "kind een=Neo 4/5/6\nKind Drie=Schoolnieuws"}],
)
async def test_option_overrides_and_completes_the_account(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A line for an account child replaces its groups (and spelling); another line adds one."""
    _with_children(api_state, _child("Kind Een", ["6A"]), _child("Kind Twee", ["1/2B"]))
    _posts(api_state)
    await setup_integration(hass, mock_config_entry)
    assert _linked(hass) == {
        105: ([], True),
        104: (["Kind Drie"], False),
        103: (["kind een", "Kind Twee"], True),
        102: (["kind een"], False),
        101: (["Kind Twee"], False),
    }


async def test_children_follow_the_account_every_day(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A new school year and a child that starts school are picked up within a day.

    The account is fetched once at setup and then once a day, not on every poll.
    """
    _with_children(api_state, _child("Kind Een", ["6A"]))
    _posts(api_state)
    await setup_integration(hass, mock_config_entry)
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 1
    _with_children(api_state, _child("Kind Een", ["7A"]), _child("Kind Twee", ["1/2B"]))
    api_state.posts.insert(0, make_post(106, "2026-11-04T09:00:00Z", groups=["7A"]))
    await advance(hass, freezer, timedelta(hours=23, minutes=45))
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 1
    assert _linked(hass)[106] == ([], True)
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 2
    linked = _linked(hass)
    assert linked[106] == (["Kind Een"], False)
    assert linked[105] == ([], True)  # 6A is no longer the child's group
    assert linked[101] == (["Kind Twee"], False)
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 2


async def test_failed_account_refresh_keeps_the_children(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed daily fetch keeps the previous children, warns once and retries hourly."""
    _with_children(api_state, _child("Kind Een", ["6A"]))
    _posts(api_state)
    await setup_integration(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG)
    api_state.account_status = 500
    await advance(hass, freezer, timedelta(days=1))
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 2
    assert mock_config_entry.runtime_data.last_update_success is True
    assert _linked(hass)[105] == (["Kind Een"], False)
    (warning,) = _own_warnings(caplog)
    assert warning.startswith(
        "The children of the account could not be refreshed, keeping the previous ones ("
    )
    assert warning.endswith("HTTP 500)")
    await advance(hass, freezer, timedelta(minutes=45))
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 2
    await advance(hass, freezer, POLL)
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 3  # retried after an hour
    assert len(_own_warnings(caplog)) == 1  # the retry fails quietly
    api_state.account_status = 200
    _with_children(api_state, _child("Kind Een", ["Neo 4/5/6"]))
    await advance(hass, freezer, timedelta(hours=1))
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 4
    assert _linked(hass)[102] == (["Kind Een"], False)


async def test_each_school_has_its_own_children(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A child of the role at one school is not linked to posts of another school."""
    api_state.account = make_account(schools=[(7, "Private School"), (8, "Second School")])
    api_state.account["roles"][0]["connections"] = [_child("Kind Een", ["6A"])]
    api_state.account["roles"][1]["connections"] = [_child("Kind Twee", ["6A"])]
    api_state.posts = [make_post(101, "2026-11-03T08:00:00Z", groups=["6A"])]
    await setup_integration(hass, mock_config_entry)
    assert _linked(hass) == {101: (["Kind Een"], False)}
    assert _linked(hass, SECOND_POSTS_ID) == {101: (["Kind Twee"], False)}
    assert mock_config_entry.runtime_data.account_child_count == 2


async def test_child_names_stay_out_of_store_diagnostics_and_log(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Names reach only the ``kinderen`` attributes; diagnostics show the count."""
    caplog.set_level(logging.DEBUG)
    _with_children(api_state, _child("Private Kind", ["Private Groep"]))
    api_state.posts.insert(0, make_post(104, "2026-11-03T12:00:00Z", groups=["Private Groep"]))
    await setup_integration(hass, mock_config_entry)
    await advance(hass, freezer, timedelta(days=1))  # includes the daily account fetch
    assert _linked(hass)[104] == (["Private Kind"], False)
    assert "Private Kind" not in json.dumps(hass_storage, default=str)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["coordinator"]["account_child_count"] == 1
    assert diagnostics["coordinator"]["child_count"] == 0
    dump = json.dumps(diagnostics)
    assert "Private Kind" not in dump
    assert "Private Groep" not in dump
    assert "Private Kind" not in caplog.text
    assert "Private Groep" not in caplog.text
