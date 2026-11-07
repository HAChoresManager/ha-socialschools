"""Tests for strings.json and the translations: structure, keys in use and plain-text values."""

from __future__ import annotations

import ast
from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.socialschools.const import (
    EVENT_TYPE_NEW_POST,
    OPT_CALENDAR_DAYS,
    OPT_CHILDREN,
    OPT_POSTS_IN_LIST,
    OPT_POSTS_INTERVAL,
    OPT_READERS,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "socialschools"
STRINGS_PATH = PACKAGE_DIR / "strings.json"
EN_PATH = PACKAGE_DIR / "translations" / "en.json"
NL_PATH = PACKAGE_DIR / "translations" / "nl.json"
PLATFORMS = ("sensor", "event", "calendar")

# hassfest's rule for translation keys (entity keys, state keys, attribute keys).
TRANSLATION_KEY_RE = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
PLACEHOLDER_RE = re.compile(r"\{([^}]*)\}")
# The names of DESIGN.md §10, per platform and translation key.
ENTITY_NAMES = {
    "sensor": {
        "latest_post": ("Latest post", "Laatste bericht"),
        "post_count": ("Posts", "Berichten"),
        "unread": ("Unread", "Ongelezen"),
        "open_post": ("Opened post", "Geopend bericht"),
    },
    "event": {"new_post": ("New post", "Nieuw bericht")},
    "calendar": {"calendar": ("Calendar", "Agenda")},
}


def _load(path: Path) -> dict[str, Any]:
    """Return the parsed JSON of a translation file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _key_paths(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return every key path of a nested mapping."""
    paths: set[tuple[str, ...]] = set()
    for key, value in data.items():
        path = (*prefix, key)
        paths.add(path)
        if isinstance(value, dict):
            paths |= _key_paths(value, path)
    return paths


def _values(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    """Yield (dotted key path, value) for every string value of a nested mapping."""
    for key, value in data.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            yield from _values(value, path)
        else:
            yield ".".join(path), value


@pytest.fixture(name="strings")
def strings_fixture() -> dict[str, Any]:
    """Return strings.json."""
    return _load(STRINGS_PATH)


@pytest.fixture(name="dutch")
def dutch_fixture() -> dict[str, Any]:
    """Return translations/nl.json."""
    return _load(NL_PATH)


def test_english_translation_is_a_copy_of_strings(strings: dict[str, Any]) -> None:
    """translations/en.json must be a literal copy of strings.json (no key references)."""
    assert _load(EN_PATH) == strings
    assert STRINGS_PATH.read_text(encoding="utf-8") == EN_PATH.read_text(encoding="utf-8")


def test_dutch_translation_has_the_same_structure(
    strings: dict[str, Any], dutch: dict[str, Any]
) -> None:
    """nl.json carries exactly the keys of strings.json, with the same placeholders."""
    assert _key_paths(dutch) == _key_paths(strings)
    english_placeholders = {path: PLACEHOLDER_RE.findall(value) for path, value in _values(strings)}
    dutch_placeholders = {path: PLACEHOLDER_RE.findall(value) for path, value in _values(dutch)}
    assert dutch_placeholders == english_placeholders


@pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, NL_PATH], ids=lambda path: path.name)
def test_values_are_plain_text(path: Path) -> None:
    """Values are non-empty strings without URLs, HTML, references or stray whitespace."""
    for key_path, value in _values(_load(path)):
        assert isinstance(value, str), key_path
        assert value, key_path
        assert value == value.strip(), key_path
        assert "http" not in value, key_path
        assert "<" not in value, key_path
        assert "[%" not in value, key_path
        for placeholder in PLACEHOLDER_RE.findall(value):
            assert placeholder.isidentifier(), key_path


def test_reauth_description_names_the_account(
    strings: dict[str, Any], dutch: dict[str, Any]
) -> None:
    """The reauth form fills in the username of the entry."""
    for data in (strings, dutch):
        step = data["config"]["step"]["reauth_confirm"]
        assert "{username}" in step["description"]
        assert list(step["data"]) == ["password"]


def test_config_flow_error_keys(strings: dict[str, Any]) -> None:
    """Every error key the config flow sets is translated, and no unused key is declared."""
    source = (PACKAGE_DIR / "config_flow.py").read_text(encoding="utf-8")
    used = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', source))
    assert used == {"invalid_auth", "cannot_connect", "unsupported", "no_parent_role", "unknown"}
    assert set(strings["config"]["error"]) == used


def test_config_flow_abort_reasons(strings: dict[str, Any]) -> None:
    """The abort reasons the flows can end with are translated."""
    assert set(strings["config"]["abort"]) == {
        "already_configured",
        "unique_id_mismatch",
        "reauth_successful",
    }


@pytest.mark.parametrize("section", ["config", "options"])
def test_data_descriptions_match_data(strings: dict[str, Any], section: str) -> None:
    """Every data_description key exists in the data of the same step (hassfest rule)."""
    for step_id, step in strings[section]["step"].items():
        assert set(step.get("data_description", {})) <= set(step.get("data", {})), step_id


def test_user_step_fields(strings: dict[str, Any]) -> None:
    """The user step asks for the username and the password, both described."""
    step = strings["config"]["step"]["user"]
    assert list(step["data"]) == ["username", "password"]
    assert set(step["data_description"]) == {"username", "password"}


def test_options_keys_match_const(strings: dict[str, Any]) -> None:
    """The options form translates exactly the option keys of const.py, each described."""
    step = strings["options"]["step"]["init"]
    expected = {OPT_POSTS_INTERVAL, OPT_CALENDAR_DAYS, OPT_POSTS_IN_LIST, OPT_READERS, OPT_CHILDREN}
    assert set(step["data"]) == expected
    assert set(step["data_description"]) == expected


def test_entity_names(strings: dict[str, Any], dutch: dict[str, Any]) -> None:
    """The entity section holds exactly the keys of DESIGN.md §10 with the agreed names."""
    assert set(strings["entity"]) == set(ENTITY_NAMES)
    for platform, names in ENTITY_NAMES.items():
        assert set(strings["entity"][platform]) == set(names)
        for key, (english, nederlands) in names.items():
            assert strings["entity"][platform][key]["name"] == english
            assert dutch["entity"][platform][key]["name"] == nederlands


def test_event_type_is_translated(strings: dict[str, Any], dutch: dict[str, Any]) -> None:
    """The new-post event type has a label in both languages."""
    for data, label in ((strings, "New post"), (dutch, "Nieuw bericht")):
        states = data["entity"]["event"]["new_post"]["state_attributes"]["event_type"]["state"]
        assert states == {EVENT_TYPE_NEW_POST: label}


def test_translation_keys_are_valid(strings: dict[str, Any]) -> None:
    """Entity, state and attribute keys follow hassfest's translation key rule."""
    for path in _key_paths(strings["entity"]):
        assert TRANSLATION_KEY_RE.match(path[-1]), path


EXCEPTION_KEYS = {
    "no_parent_role",
    "choose_person",
    "not_a_reader",
    "unknown_post",
    "open_failed",
    "not_loaded",
}


def test_exception_message(strings: dict[str, Any], dutch: dict[str, Any]) -> None:
    """Every error raised with a translation key has a message in both languages."""
    for data in (strings, dutch):
        assert set(data["exceptions"]) == EXCEPTION_KEYS
        for key in EXCEPTION_KEYS:
            assert set(data["exceptions"][key]) == {"message"}
    assert dutch["exceptions"]["choose_person"]["message"] == "Kies wie het bericht gelezen heeft"


def test_exception_keys_used_in_code_are_translated(strings: dict[str, Any]) -> None:
    """Every translation_key / _error key in the Python code exists under exceptions."""
    used: set[str] = set()
    for path in PACKAGE_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'translation_key="([a-z_]+)"', source))
        used |= set(re.findall(r'_error\("([a-z_]+)"', source))
    used -= set(ENTITY_NAMES["sensor"]) | {"calendar", "new_post"}
    assert used <= set(strings["exceptions"]), used - set(strings["exceptions"])


def test_services_are_translated(strings: dict[str, Any], dutch: dict[str, Any]) -> None:
    """Every action in services.yaml has a name, a description and named fields in both languages."""
    services = {
        "markeer_gelezen": {"post_id", "persoon", "gelezen"},
        "markeer_alles_gelezen": {"persoon"},
        "open_bericht": {"post_id"},
    }
    for data in (strings, dutch):
        assert set(data["services"]) == set(services)
        for service, fields in services.items():
            assert data["services"][service]["name"]
            assert data["services"][service]["description"]
            assert set(data["services"][service]["fields"]) == fields
            for field in fields:
                assert set(data["services"][service]["fields"][field]) == {"name", "description"}


def _entity_keys(platform: str) -> set[str]:
    """Return the key literals a platform module passes to its entity constructors.

    The platforms build ``SocialSchools...(coordinator, school, "<key>")`` and the base
    entity turns that third argument into the translation key.
    """
    tree = ast.parse((PACKAGE_DIR / f"{platform}.py").read_text(encoding="utf-8"))
    return {
        node.args[2].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("SocialSchools")
        and len(node.args) >= 3
        and isinstance(node.args[2], ast.Constant)
        and isinstance(node.args[2].value, str)
    }


def test_entity_key_is_the_translation_key() -> None:
    """The base entity uses the constructor key as the translation key."""
    source = (PACKAGE_DIR / "entity.py").read_text(encoding="utf-8")
    assert "self._attr_translation_key = key" in source


@pytest.mark.parametrize("platform", PLATFORMS)
def test_platform_translation_keys_exist(strings: dict[str, Any], platform: str) -> None:
    """The keys the platform module gives its entities are exactly the translated ones."""
    keys = _entity_keys(platform)
    assert keys == set(ENTITY_NAMES[platform])
    assert keys <= set(strings["entity"][platform]), keys - set(strings["entity"][platform])


async def test_registered_entities_are_translated(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    strings: dict[str, Any],
    dutch: dict[str, Any],
) -> None:
    """Every entity the platforms register has a name in both languages under its key."""
    entries = er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
    assert {(entry.domain, entry.translation_key) for entry in entries} == {
        (platform, key) for platform, names in ENTITY_NAMES.items() for key in names
    }
    for entry in entries:
        assert entry.translation_key is not None
        for data in (strings, dutch):
            assert data["entity"][entry.domain][entry.translation_key]["name"]
