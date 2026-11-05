"""Tests for the latest-post and post-count sensors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import (
    ATTR_ATTACHMENTS,
    ATTR_AUTHOR,
    ATTR_GROUP,
    ATTR_POST_ID,
    ATTR_POSTS,
    ATTR_SHORT_TEXT,
    ATTR_TEXT,
    ATTR_TITLE,
    ATTR_TRUNCATED,
    ATTRIBUTES_BYTE_BUDGET,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_ATTACHMENTS,
    MAX_AUTHOR_LENGTH,
    MAX_GROUP_LENGTH,
    MAX_LIST_GROUP_LENGTH,
    MAX_LIST_TITLE_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
    OPT_POSTS_IN_LIST,
)
from custom_components.socialschools.sensor import (
    SocialSchoolsLatestPostSensor,
    SocialSchoolsPostCountSensor,
)
from tests.conftest import ApiState, advance, make_post, setup_integration

FROZEN_NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
POLL = timedelta(minutes=15)
LATEST_POST_ID = "sensor.social_schools_private_school_laatste_bericht"
POST_COUNT_ID = "sensor.social_schools_private_school_berichten"
SUMMARY_KEYS = {"post_id", "titel", "groep", "auteur", "geplaatst", "tekst_kort"}

pytestmark = pytest.mark.freeze_time(FROZEN_NOW)


def _state(hass: HomeAssistant, entity_id: str) -> State:
    """Return the state of ``entity_id``, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def _hourly_posts(count: int, **overrides: object) -> list[dict]:
    """Return ``count`` feed items, one per hour before the frozen clock, newest first."""
    return [
        make_post(300 - index, (FROZEN_NOW - timedelta(hours=index + 1)).isoformat(), **overrides)
        for index in range(count)
    ]


async def test_latest_post(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The newest post gives the state and exactly the documented attributes."""
    state = _state(hass, LATEST_POST_ID)
    assert state.state == "Bericht 103"
    assert state.attributes == {
        "post_id": 103,
        "groep": "Groep 3",
        "auteur": "Private Teacher",
        "geplaatst": "2026-11-03T09:00:00+01:00",
        "tekst": "Hallo wereld",
        "afgekapt": False,
        "bijlagen": ["brief.pdf"],
        "reacties": 1,
        "friendly_name": "Social Schools · Private School Laatste bericht",
    }


async def test_attachment_urls_never_reach_the_state_machine(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Attachments are exposed by file name only; the signed URLs appear in no state at all."""
    everything = json.dumps([state.as_dict() for state in hass.states.async_all()], default=str)
    assert "brief.pdf" in everything
    assert "fullPath" not in everything
    assert "private-signed" not in everything
    assert "cdn.example.test" not in everything


async def test_long_title_is_cut_to_the_state_limit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """A title longer than a state allows is cut to 255 characters ending in an ellipsis."""
    api_state.posts[0] = make_post(103, "2026-11-03T08:00:00Z", title="T" * 300)
    await setup_integration(hass, mock_config_entry)
    state = _state(hass, LATEST_POST_ID)
    assert len(state.state) == MAX_TITLE_LENGTH
    assert state.state == "T" * (MAX_TITLE_LENGTH - 1) + "…"


async def test_long_text_is_cut_and_flagged(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """The text attribute is bounded and ``afgekapt`` says so."""
    api_state.posts[0] = make_post(103, "2026-11-03T08:00:00Z", body="<p>" + "x" * 2500 + "</p>")
    await setup_integration(hass, mock_config_entry)
    attributes = _state(hass, LATEST_POST_ID).attributes
    assert len(attributes[ATTR_TEXT]) == MAX_TEXT_LENGTH
    assert attributes[ATTR_TEXT].endswith("…")
    assert attributes[ATTR_TRUNCATED] is True


async def test_state_falls_back_to_the_first_line_of_the_text(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Without a title the first line of the text becomes the state; the text keeps its lines."""
    api_state.posts[0] = make_post(
        103, "2026-11-03T08:00:00Z", title="", body="<p>Eerste regel</p><p>Tweede regel</p>"
    )
    await setup_integration(hass, mock_config_entry)
    state = _state(hass, LATEST_POST_ID)
    assert state.state == "Eerste regel"
    assert state.attributes[ATTR_TEXT] == "Eerste regel\nTweede regel"


async def test_latest_post_attributes_are_capped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Author, groups and attachments are bounded in length and number, in the list too."""
    api_state.posts[0] = make_post(
        103,
        "2026-11-03T08:00:00Z",
        author="A" * 300,
        groups=tuple(f"Groep {index:02d}" for index in range(60)),
        attachments=("x" * 300,) * 25,
    )
    await setup_integration(hass, mock_config_entry)
    attributes = _state(hass, LATEST_POST_ID).attributes
    assert len(attributes[ATTR_AUTHOR]) == MAX_AUTHOR_LENGTH
    assert attributes[ATTR_AUTHOR].endswith("…")
    assert len(attributes[ATTR_GROUP]) == MAX_GROUP_LENGTH
    assert attributes[ATTR_GROUP].startswith("Groep 00, Groep 01, ")
    assert attributes[ATTR_GROUP].endswith("…")
    attachments = attributes[ATTR_ATTACHMENTS]
    assert len(attachments) == MAX_ATTACHMENTS
    assert all(len(name) == MAX_ATTACHMENT_NAME_LENGTH for name in attachments)
    assert all(name.endswith("…") for name in attachments)
    listed = _state(hass, POST_COUNT_ID).attributes[ATTR_POSTS][0]
    assert len(listed[ATTR_AUTHOR]) == MAX_AUTHOR_LENGTH
    assert len(listed[ATTR_GROUP]) == MAX_LIST_GROUP_LENGTH


async def test_latest_post_respects_the_byte_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Attachments are dropped from the end until the attributes fit the JSON byte budget."""
    api_state.posts[0] = make_post(
        103,
        "2026-11-03T08:00:00Z",
        body="<p>" + "€" * 2500 + "</p>",
        attachments=("€" * 150,) * 25,
    )
    await setup_integration(hass, mock_config_entry)
    attributes = dict(_state(hass, LATEST_POST_ID).attributes)
    attributes.pop("friendly_name")
    attachments = attributes[ATTR_ATTACHMENTS]
    assert 0 < len(attachments) < MAX_ATTACHMENTS
    assert len(attributes[ATTR_TEXT]) == MAX_TEXT_LENGTH
    size = len(json.dumps(attributes, ensure_ascii=False).encode())
    assert size <= ATTRIBUTES_BYTE_BUDGET
    one_more = len(json.dumps(attachments[0], ensure_ascii=False).encode()) + len(", ")
    assert size + one_more > ATTRIBUTES_BYTE_BUDGET


async def test_without_posts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """An empty feed gives an unknown latest post without attributes and a count of zero."""
    api_state.posts = []
    await setup_integration(hass, mock_config_entry)
    latest = _state(hass, LATEST_POST_ID)
    assert latest.state == "unknown"
    assert set(latest.attributes) == {"friendly_name"}
    count = _state(hass, POST_COUNT_ID)
    assert count.state == "0"
    assert count.attributes[ATTR_POSTS] == []


async def test_recorder_exclusions(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The text, the attachments and the post list are kept out of the recorder."""
    assert SocialSchoolsLatestPostSensor._unrecorded_attributes == {ATTR_TEXT, ATTR_ATTACHMENTS}
    assert SocialSchoolsPostCountSensor._unrecorded_attributes == {ATTR_POSTS}
    latest_info = _state(hass, LATEST_POST_ID).state_info
    assert latest_info is not None
    assert {ATTR_TEXT, ATTR_ATTACHMENTS} <= latest_info["unrecorded_attributes"]
    count_info = _state(hass, POST_COUNT_ID).state_info
    assert count_info is not None
    assert ATTR_POSTS in count_info["unrecorded_attributes"]


async def test_post_count(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Posts of the past seven days are counted; the list holds every fetched post."""
    state = _state(hass, POST_COUNT_ID)
    assert state.state == "2"
    assert state.attributes == {
        "state_class": "measurement",
        "berichten": [
            {
                "post_id": 103,
                "titel": "Bericht 103",
                "groep": "Groep 3",
                "auteur": "Private Teacher",
                "geplaatst": "2026-11-03T09:00:00+01:00",
                "tekst_kort": "Hallo wereld",
            },
            {
                "post_id": 102,
                "titel": "Bericht 102",
                "groep": "Groep 3",
                "auteur": "Private Teacher",
                "geplaatst": "2026-11-01T09:00:00+01:00",
                "tekst_kort": "Hallo wereld",
            },
            {
                "post_id": 101,
                "titel": "Bericht 101",
                "groep": "Groep 3",
                "auteur": "Private Teacher",
                "geplaatst": "2026-10-20T10:00:00+02:00",
                "tekst_kort": "Hallo wereld",
            },
        ],
        "friendly_name": "Social Schools · Private School Berichten",
    }


async def test_post_count_window_moves_with_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, init_integration: MockConfigEntry
) -> None:
    """The seven-day window is evaluated at every poll, so old posts drop out of the count."""
    await advance(hass, freezer, timedelta(days=4))
    assert _state(hass, POST_COUNT_ID).state == "1"


@pytest.mark.parametrize("entry_options", [{OPT_POSTS_IN_LIST: 1}])
async def test_posts_in_list_option(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The posts_in_list option limits the list to the newest posts."""
    posts = _state(hass, POST_COUNT_ID).attributes[ATTR_POSTS]
    assert [post[ATTR_POST_ID] for post in posts] == [103]


async def test_post_list_default_cap(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """By default the list holds the ten newest posts with exactly the six documented keys."""
    api_state.posts = _hourly_posts(15)
    await setup_integration(hass, mock_config_entry)
    state = _state(hass, POST_COUNT_ID)
    assert state.state == "15"
    posts = state.attributes[ATTR_POSTS]
    assert [post[ATTR_POST_ID] for post in posts] == list(range(300, 290, -1))
    for post in posts:
        assert set(post) == SUMMARY_KEYS
        assert len(post[ATTR_SHORT_TEXT]) <= MAX_SHORT_TEXT_LENGTH


@pytest.mark.parametrize("entry_options", [{OPT_POSTS_IN_LIST: 20}])
async def test_post_list_respects_the_byte_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    api_state: ApiState,
) -> None:
    """Oldest entries are dropped until the attributes fit the JSON byte budget."""
    api_state.posts = _hourly_posts(
        20, title="T" * 150, body="<p>" + "x" * 400 + "</p>", groups=("G" * 150,)
    )
    await setup_integration(hass, mock_config_entry)
    posts = _state(hass, POST_COUNT_ID).attributes[ATTR_POSTS]
    assert 0 < len(posts) < 20
    assert [post[ATTR_POST_ID] for post in posts] == list(range(300, 300 - len(posts), -1))
    size = len(json.dumps({ATTR_POSTS: posts}, ensure_ascii=False).encode())
    assert size <= ATTRIBUTES_BYTE_BUDGET
    one_more = len(json.dumps(posts[0], ensure_ascii=False).encode()) + len(", ")
    assert size + one_more > ATTRIBUTES_BYTE_BUDGET
    for post in posts:
        assert set(post) == SUMMARY_KEYS
        assert len(post[ATTR_TITLE]) == MAX_LIST_TITLE_LENGTH
        assert post[ATTR_TITLE].endswith("…")
        assert len(post[ATTR_GROUP]) == MAX_LIST_GROUP_LENGTH
        assert post[ATTR_GROUP].endswith("…")
        assert len(post[ATTR_SHORT_TEXT]) == MAX_SHORT_TEXT_LENGTH
        assert post[ATTR_SHORT_TEXT].endswith("…")


async def test_states_follow_a_poll_with_a_new_post(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    init_integration: MockConfigEntry,
    api_state: ApiState,
) -> None:
    """Both sensors show the new post after the next poll."""
    api_state.posts.insert(0, make_post(104, "2026-11-04T09:00:00Z", title="Nieuw bericht"))
    await advance(hass, freezer, POLL)
    latest = _state(hass, LATEST_POST_ID)
    assert latest.state == "Nieuw bericht"
    assert latest.attributes[ATTR_POST_ID] == 104
    count = _state(hass, POST_COUNT_ID)
    assert count.state == "3"
    assert [post[ATTR_POST_ID] for post in count.attributes[ATTR_POSTS]] == [104, 103, 102, 101]
