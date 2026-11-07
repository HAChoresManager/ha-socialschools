"""Actions of the Social Schools integration: local read marks and the opened post.

``markeer_gelezen`` and ``markeer_alles_gelezen`` only change bookkeeping inside
Home Assistant; nothing is sent to Social Schools.  ``open_bericht`` fetches one
post with the whitelisted ``GET communityposts/{id}``.  Without a ``persoon``
the person is the one whose ``user_id`` matches the user that called the action.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    DOMAIN,
    FIELD_PERSON,
    FIELD_POST_ID,
    FIELD_READ,
    SERVICE_MARK_ALL_READ,
    SERVICE_MARK_READ,
    SERVICE_OPEN_POST,
)
from .errors import SocialSchoolsError

if TYPE_CHECKING:
    from .coordinator import SocialSchoolsCoordinator
    from .models import Post, School

PERSON_DOMAIN = "person"

SCHEMA_MARK_READ = vol.Schema(
    {
        vol.Required(FIELD_POST_ID): cv.positive_int,
        vol.Optional(FIELD_PERSON): cv.entity_domain(PERSON_DOMAIN),
        vol.Optional(FIELD_READ, default=True): cv.boolean,
    }
)
SCHEMA_MARK_ALL_READ = vol.Schema({vol.Optional(FIELD_PERSON): cv.entity_domain(PERSON_DOMAIN)})
SCHEMA_OPEN_POST = vol.Schema({vol.Required(FIELD_POST_ID): cv.positive_int})


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    """Return a translated validation error of this integration."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _coordinators(hass: HomeAssistant) -> list[SocialSchoolsCoordinator]:
    """Return the coordinators of all loaded entries, or raise when there are none."""
    coordinators = [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
    if not coordinators:
        raise _error("not_loaded")
    return coordinators


def _find_post(hass: HomeAssistant, post_id: int) -> tuple[SocialSchoolsCoordinator, School, Post]:
    """Return where ``post_id`` is in a current feed, or raise ``unknown_post``."""
    for coordinator in _coordinators(hass):
        if (found := coordinator.find_post(post_id)) is not None:
            return coordinator, *found
    raise _error("unknown_post", post_id=str(post_id))


def _person_name(hass: HomeAssistant, person: str) -> str:
    """Return the friendly name of a person entity, or its entity id."""
    state = hass.states.get(person)
    return state.name if state is not None else person


def _caller_person(hass: HomeAssistant, call: ServiceCall) -> str | None:
    """Return the person entity whose ``user_id`` is the user who called the action."""
    user_id = call.context.user_id
    if user_id is None:
        return None
    for state in hass.states.async_all(PERSON_DOMAIN):
        if state.attributes.get("user_id") == user_id:
            return state.entity_id
    return None


def _person(hass: HomeAssistant, call: ServiceCall) -> str:
    """Return the given ``persoon`` or the caller's person, or raise ``choose_person``."""
    person = call.data.get(FIELD_PERSON) or _caller_person(hass, call)
    if person is None:
        raise _error("choose_person")
    return person


def _require_reader(
    hass: HomeAssistant, coordinator: SocialSchoolsCoordinator, person: str
) -> None:
    """Raise ``not_a_reader`` when ``person`` is not one of the entry's readers."""
    if person not in coordinator.readers:
        raise _error("not_a_reader", person=_person_name(hass, person))


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the three actions of the integration."""

    async def mark_read(call: ServiceCall) -> None:
        coordinator, school, post = _find_post(hass, call.data[FIELD_POST_ID])
        person = _person(hass, call)
        _require_reader(hass, coordinator, person)
        await coordinator.async_mark_read(school.id, [post.id], person, call.data[FIELD_READ])

    async def mark_all_read(call: ServiceCall) -> None:
        person = _person(hass, call)
        targets = [c for c in _coordinators(hass) if person in c.readers]
        if not targets:
            raise _error("not_a_reader", person=_person_name(hass, person))
        for coordinator in targets:
            for school_data in (coordinator.data.schools if coordinator.data else {}).values():
                post_ids = [post.id for post in school_data.posts]
                await coordinator.async_mark_read(school_data.school.id, post_ids, person, True)

    async def open_post(call: ServiceCall) -> None:
        coordinator, school, post = _find_post(hass, call.data[FIELD_POST_ID])
        try:
            await coordinator.async_open_post(school, post.id)
        except (SocialSchoolsError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_failed",
                translation_placeholders={"post_id": str(post.id)},
            ) from err

    hass.services.async_register(DOMAIN, SERVICE_MARK_READ, mark_read, schema=SCHEMA_MARK_READ)
    hass.services.async_register(
        DOMAIN, SERVICE_MARK_ALL_READ, mark_all_read, schema=SCHEMA_MARK_ALL_READ
    )
    hass.services.async_register(DOMAIN, SERVICE_OPEN_POST, open_post, schema=SCHEMA_OPEN_POST)
