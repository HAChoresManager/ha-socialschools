"""Tests for the local read bookkeeping per person: seeding, migration, actions and sensors.

"Read" never leaves Home Assistant: the tests also check that the integration only
sends whitelisted GET requests to the API while all of this happens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.client import ALLOWED_PATHS
from custom_components.socialschools.const import (
    DOMAIN,
    OPT_CHILDREN,
    OPT_READERS,
    SERVICE_MARK_ALL_READ,
    SERVICE_MARK_READ,
    SERVICE_OPEN_POST,
    STORAGE_VERSION,
)
from tests.conftest import STORAGE_KEY, ApiState, advance, make_post, setup_integration

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
UNREAD_ID = "sensor.social_schools_private_school_ongelezen"
POSTS_ID = "sensor.social_schools_private_school_berichten"
ONE = "person.ouder_een"
TWO = "person.ouder_twee"
GUEST = "person.gast"
READERS = {OPT_READERS: [ONE, TWO]}

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


async def _persons(hass: HomeAssistant) -> dict[str, str]:
    """Create users and person states: two readers with a user, a guest and a kiosk user."""
    users: dict[str, str] = {}
    for entity_id, name in ((ONE, "Ouder Een"), (TWO, "Ouder Twee"), (GUEST, "Gast")):
        user = await hass.auth.async_create_user(name)
        users[entity_id] = user.id
        hass.states.async_set(entity_id, "home", {"friendly_name": name, "user_id": user.id})
    users["kiosk"] = (await hass.auth.async_create_user("Kiosk")).id
    return users


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Create the persons first (their names appear in attributes), then set the entry up."""
    users = await _persons(hass)
    await setup_integration(hass, entry)
    return users


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], user_id: str | None = None
) -> None:
    """Call an action of the integration as ``user_id`` and wait for it."""
    await hass.services.async_call(
        DOMAIN, service, data, blocking=True, context=Context(user_id=user_id)
    )


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Return the attributes of an entity."""
    state = hass.states.get(entity_id)
    assert state is not None
    return dict(state.attributes)


def _list_item(hass: HomeAssistant, post_id: int) -> dict[str, Any]:
    """Return the ``berichten`` item of a post."""
    return next(
        item for item in _attributes(hass, POSTS_ID)["berichten"] if item["post_id"] == post_id
    )


async def _new_post(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, api_state: ApiState
) -> None:
    """Publish post 104 and poll."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z"))
    await advance(hass, freezer, POLL)


@pytest.mark.parametrize("entry_options", [READERS])
async def test_install_seeds_existing_posts_as_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """On the first run every post already in the feed counts as read by every reader."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(UNREAD_ID).state == "0"
    assert _attributes(hass, UNREAD_ID) == {
        "state_class": "measurement",
        "eerste": None,
        "per_persoon": {"Ouder Een": 0, "Ouder Twee": 0},
        "friendly_name": "Social Schools · Private School Ongelezen",
    }
    stored = hass_storage[STORAGE_KEY]
    assert stored["version"] == STORAGE_VERSION == 2
    school = stored["data"]["schools"]["7"]
    assert school["readers"] == sorted([ONE, TWO])
    assert school["read"] == {str(post_id): sorted([ONE, TWO]) for post_id in (101, 102, 103)}
    item = _list_item(hass, 103)
    assert (item["gelezen_door"], item["nog_lezen"], item["nieuw"]) == (
        ["Ouder Een", "Ouder Twee"],
        [],
        False,
    )


@pytest.mark.parametrize("entry_options", [READERS])
async def test_new_post_stays_new_until_both_have_read_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """A new post is unread for both; marking with and without ``persoon`` clears it locally."""
    users = await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    assert hass.states.get(UNREAD_ID).state == "1"
    attributes = _attributes(hass, UNREAD_ID)
    assert attributes["eerste"] == {
        "post_id": 104,
        "titel": "Bericht 104",
        "groep": "Groep 3",
        "kinderen": [],
        "schoolbreed": True,
        "auteur": "Private Teacher",
        "geplaatst": "2026-11-04T10:00:00+01:00",
        "url": "https://app.socialschools.eu/communityposts/104",
        "nog_lezen": ["Ouder Een", "Ouder Twee"],
    }
    assert attributes["per_persoon"] == {"Ouder Een": 1, "Ouder Twee": 1}
    assert _list_item(hass, 104)["nieuw"] is True

    requests_before = len(api_state.request_log)
    # Tablet: the kiosk user picks who read it.
    await _call(hass, SERVICE_MARK_READ, {"post_id": 104, "persoon": TWO}, users["kiosk"])
    item = _list_item(hass, 104)
    assert (item["gelezen_door"], item["nog_lezen"], item["nieuw"]) == (
        ["Ouder Twee"],
        ["Ouder Een"],
        True,
    )
    assert hass.states.get(UNREAD_ID).state == "1"
    assert _attributes(hass, UNREAD_ID)["per_persoon"] == {"Ouder Een": 1, "Ouder Twee": 0}
    # Phone: the person follows from the user who calls the action.
    await _call(hass, SERVICE_MARK_READ, {"post_id": 104}, users[ONE])
    assert hass.states.get(UNREAD_ID).state == "0"
    assert _attributes(hass, UNREAD_ID)["eerste"] is None
    assert _list_item(hass, 104)["nieuw"] is False
    assert len(api_state.request_log) == requests_before  # nothing was sent anywhere
    assert hass_storage[STORAGE_KEY]["data"]["schools"]["7"]["read"]["104"] == sorted([ONE, TWO])


@pytest.mark.parametrize("entry_options", [READERS])
async def test_undo_a_wrong_tap(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """``gelezen: false`` removes the mark again."""
    users = await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    await _call(hass, SERVICE_MARK_READ, {"post_id": 104, "persoon": ONE}, users["kiosk"])
    await _call(
        hass, SERVICE_MARK_READ, {"post_id": 104, "persoon": ONE, "gelezen": False}, users["kiosk"]
    )
    assert _list_item(hass, 104)["nog_lezen"] == ["Ouder Een", "Ouder Twee"]
    # Undo also works on a seeded post.
    await _call(hass, SERVICE_MARK_READ, {"post_id": 103, "gelezen": False}, users[TWO])
    assert _list_item(hass, 103)["nog_lezen"] == ["Ouder Twee"]
    assert hass.states.get(UNREAD_ID).state == "2"


@pytest.mark.parametrize("entry_options", [READERS])
@pytest.mark.parametrize("caller", ["kiosk", None], ids=["kiosk-user", "no-user"])
async def test_without_person_the_caller_must_be_a_person(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    caller: str | None,
) -> None:
    """A user without a person (the tablet's kiosk user) has to choose who read it."""
    users = await _setup(hass, mock_config_entry)
    for service, data in ((SERVICE_MARK_READ, {"post_id": 103}), (SERVICE_MARK_ALL_READ, {})):
        with pytest.raises(ServiceValidationError) as excinfo:
            await _call(hass, service, data, users[caller] if caller else None)
        assert excinfo.value.translation_key == "choose_person"
        assert excinfo.value.translation_domain == DOMAIN


@pytest.mark.parametrize("entry_options", [READERS])
async def test_person_outside_the_readers_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A person who is not a reader, given or derived from the user, is refused."""
    users = await _setup(hass, mock_config_entry)
    for data, user in (
        ({"post_id": 103, "persoon": GUEST}, users["kiosk"]),
        ({"post_id": 103}, users[GUEST]),
    ):
        with pytest.raises(ServiceValidationError) as excinfo:
            await _call(hass, SERVICE_MARK_READ, data, user)
        assert excinfo.value.translation_key == "not_a_reader"
        assert excinfo.value.translation_placeholders == {"person": "Gast"}


@pytest.mark.parametrize("entry_options", [READERS])
async def test_unknown_post_id_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Only posts in the current feed can be marked or opened."""
    users = await _setup(hass, mock_config_entry)
    for service in (SERVICE_MARK_READ, SERVICE_OPEN_POST):
        with pytest.raises(ServiceValidationError) as excinfo:
            await _call(hass, service, {"post_id": 999}, users[ONE])
        assert excinfo.value.translation_key == "unknown_post"
        assert excinfo.value.translation_placeholders == {"post_id": "999"}


@pytest.mark.parametrize("entry_options", [READERS])
async def test_mark_all_read(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """``markeer_alles_gelezen`` marks every feed post for one person only."""
    users = await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    await _call(hass, SERVICE_MARK_ALL_READ, {}, users[ONE])
    assert _attributes(hass, UNREAD_ID)["per_persoon"] == {"Ouder Een": 0, "Ouder Twee": 1}
    await _call(hass, SERVICE_MARK_ALL_READ, {"persoon": TWO}, users["kiosk"])
    assert hass.states.get(UNREAD_ID).state == "0"


@pytest.mark.parametrize("entry_options", [READERS])
async def test_upgrade_from_version_1_seeds_and_migrates(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
    hass_storage: dict[str, Any],
) -> None:
    """A 0.1 store is migrated; the first refresh marks the existing feed as read for all."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "schools": {
                "7": {"high_water": "2026-11-03T08:00:00+00:00", "seen_ids": [101, 102, 103]}
            }
        },
    }
    await _setup(hass, mock_config_entry)
    stored = hass_storage[STORAGE_KEY]
    assert stored["version"] == 2
    school = stored["data"]["schools"]["7"]
    assert school["seen_ids"] == [101, 102, 103]
    assert school["read"] == {str(post_id): sorted([ONE, TWO]) for post_id in (101, 102, 103)}
    assert hass.states.get(UNREAD_ID).state == "0"
    await _new_post(hass, freezer, api_state)
    assert hass.states.get(UNREAD_ID).state == "1"


@pytest.mark.parametrize("entry_options", [{OPT_READERS: [ONE]}])
async def test_reader_added_later_gets_existing_posts_as_read(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Adding a reader seeds only that reader; the others keep their unread posts."""
    await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    hass.config_entries.async_update_entry(mock_config_entry, options=READERS)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _list_item(hass, 104)["nog_lezen"] == ["Ouder Een"]
    assert _attributes(hass, UNREAD_ID)["per_persoon"] == {"Ouder Een": 1, "Ouder Twee": 0}


@pytest.mark.parametrize("entry_options", [READERS])
async def test_marks_survive_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """After a reload nothing is seeded again: unread stays unread, read stays read."""
    users = await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    await _call(hass, SERVICE_MARK_READ, {"post_id": 104}, users[ONE])
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _list_item(hass, 104)["nog_lezen"] == ["Ouder Twee"]
    assert hass.states.get(UNREAD_ID).state == "1"


async def test_without_readers_nothing_is_new(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Without the ``lezers`` option nothing is unread and nobody can mark posts."""
    users = await _setup(hass, mock_config_entry)
    assert hass.states.get(UNREAD_ID).state == "0"
    assert _attributes(hass, UNREAD_ID)["per_persoon"] == {}
    assert _list_item(hass, 103)["nieuw"] is False
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call(hass, SERVICE_MARK_READ, {"post_id": 103}, users[ONE])
    assert excinfo.value.translation_key == "not_a_reader"


@pytest.mark.parametrize(
    "entry_options", [{**READERS, OPT_CHILDREN: "Kind Een=6A\nKind Twee=1/2A, 1/2B"}]
)
async def test_children_per_post(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Posts are linked to children by exact group name; Neo 4/5/6 does not count as 6A."""
    live_list = [
        "6A",
        "1/2D",
        "5/6B",
        "5A",
        "4A",
        "1/2C",
        "4B",
        "Neo 4/5/6",
        "3A",
        "1/2B",
        "2/3B",
        "1/2A",
    ]
    api_state.posts = [
        make_post(105, "2026-11-03T12:00:00Z", groups=["6a"]),
        make_post(104, "2026-11-03T11:00:00Z", groups=["Schoolnieuws"]),
        make_post(103, "2026-11-03T10:00:00Z", groups=live_list),
        make_post(102, "2026-11-03T09:00:00Z", groups=["Neo 4/5/6"]),
        make_post(101, "2026-11-03T08:00:00Z", groups=["6A", "1/2B"]),
    ]
    await _setup(hass, mock_config_entry)
    expected = {
        105: (["Kind Een"], False),
        104: ([], True),
        103: (["Kind Een", "Kind Twee"], True),
        102: ([], True),
        101: (["Kind Een", "Kind Twee"], False),
    }
    for post_id, (children, schoolwide) in expected.items():
        item = _list_item(hass, post_id)
        assert (item["kinderen"], item["schoolbreed"]) == (children, schoolwide), post_id


@pytest.mark.parametrize("entry_options", [READERS])
async def test_only_whitelisted_get_requests_are_sent(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Marking, opening and polling send nothing but GETs on the four whitelisted API paths."""
    users = await _setup(hass, mock_config_entry)
    await _new_post(hass, freezer, api_state)
    await _call(hass, SERVICE_MARK_READ, {"post_id": 104}, users[ONE])
    await _call(hass, SERVICE_MARK_ALL_READ, {"persoon": TWO}, users["kiosk"])
    await _call(hass, SERVICE_OPEN_POST, {"post_id": 102}, users["kiosk"])
    for method, url, _headers in api_state.request_log:
        if url.host == "login.socialschools.eu":
            assert (method, url.path) == ("POST", "/connect/token")
            continue
        assert url.host == "api.socialschools.eu"
        assert method == "GET"
        relative = url.path.removeprefix("/api/v1/")
        assert any(pattern.fullmatch(relative) for pattern in ALLOWED_PATHS), relative
    assert not any(
        "statistics" in str(url) or "seen" in str(url).lower()
        for _m, url, _h in api_state.request_log
    )
    assert all(re.fullmatch(r"[a-z/0-9]+", url.path) for _m, url, _h in api_state.request_log)
