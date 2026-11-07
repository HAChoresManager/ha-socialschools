"""Base entity for the Social Schools integration: one service device per school."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .children import Child, children_for, is_schoolwide
from .const import (
    ATTR_AUTHOR,
    ATTR_CHILDREN,
    ATTR_GROUP,
    ATTR_NEW,
    ATTR_POST_ID,
    ATTR_POSTED,
    ATTR_READ_BY,
    ATTR_SCHOOLWIDE,
    ATTR_SHORT_TEXT,
    ATTR_TITLE,
    ATTR_TO_READ,
    ATTR_URL,
    DOMAIN,
    MAX_AUTHOR_LENGTH,
    MAX_LIST_GROUP_LENGTH,
    MAX_LIST_TITLE_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
    POST_URL_TEMPLATE,
    SCHOOL_TIMEZONE,
)
from .coordinator import SchoolData, SocialSchoolsCoordinator
from .models import Post, School
from .textutil import truncate

CONFIGURATION_URL = "https://app.socialschools.eu/"
SCHOOL_TZ = ZoneInfo(SCHOOL_TIMEZONE)


def posted_at(post: Post) -> str:
    """Return when ``post`` was created, as ISO 8601 in the school's time zone."""
    return post.created.astimezone(SCHOOL_TZ).isoformat()


def post_url(post: Post) -> str:
    """Return the deep link to the post in the Social Schools web app."""
    return POST_URL_TEMPLATE.format(post_id=post.id)


def child_fields(post: Post, children: tuple[Child, ...]) -> dict[str, Any]:
    """Return ``kinderen`` (names of the children the post is for) and ``schoolbreed``."""
    names = children_for(post.groups, children)
    return {ATTR_CHILDREN: list(names), ATTR_SCHOOLWIDE: is_schoolwide(post.groups, names)}


def post_summary(post: Post, children: tuple[Child, ...]) -> dict[str, Any]:
    """Return the bounded summary of ``post`` shared by the post list and the new-post event."""
    return {
        ATTR_POST_ID: post.id,
        ATTR_TITLE: truncate(post.title, MAX_LIST_TITLE_LENGTH)[0],
        ATTR_GROUP: truncate(", ".join(post.groups), MAX_LIST_GROUP_LENGTH)[0],
        **child_fields(post, children),
        ATTR_AUTHOR: truncate(post.author, MAX_AUTHOR_LENGTH)[0],
        ATTR_POSTED: posted_at(post),
        ATTR_SHORT_TEXT: truncate(post.text, MAX_SHORT_TEXT_LENGTH)[0],
        ATTR_URL: post_url(post),
    }


def person_name(hass: HomeAssistant, person: str) -> str:
    """Return the friendly name of a person entity, or its entity id when it is unknown."""
    state = hass.states.get(person)
    return state.name if state is not None else person


class SocialSchoolsEntity(CoordinatorEntity[SocialSchoolsCoordinator]):
    """Entity of one school; available as long as the coordinator holds data for that school."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SocialSchoolsCoordinator, school: School, key: str) -> None:
        """Bind to a school; ``key`` is the entity's part of the unique id and its translation key."""
        super().__init__(coordinator)
        self._school_id = school.id
        profile_id = coordinator.account.user_profile_id
        self._attr_unique_id = f"{profile_id}_{school.id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{profile_id}_{school.id}")},
            name=f"Social Schools · {school.name}",
            manufacturer="Social Schools",
            model="School",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=CONFIGURATION_URL,
        )

    @property
    def school_data(self) -> SchoolData | None:
        """Return the current data of this school, if the coordinator has any."""
        data = self.coordinator.data
        return None if data is None else data.schools.get(self._school_id)

    @property
    def available(self) -> bool:
        """Stay available on stale data: only a school missing from the data is unavailable."""
        return self.school_data is not None

    def read_fields(self, post: Post) -> dict[str, Any]:
        """Return ``gelezen_door``, ``nog_lezen`` (person names) and ``nieuw`` for a post.

        Only readers from the ``lezers`` option count; ``nieuw`` is True while at
        least one of them has not read the post.
        """
        coordinator = self.coordinator
        readers = coordinator.readers
        read_by = coordinator.read_state.read_by(self._school_id, post.id)
        to_read = coordinator.read_state.unread_by(self._school_id, post.id, readers)
        return {
            ATTR_READ_BY: [person_name(self.hass, p) for p in readers if p in read_by],
            ATTR_TO_READ: [person_name(self.hass, p) for p in to_read],
            ATTR_NEW: bool(to_read),
        }


class LocalUpdatesEntity(SocialSchoolsEntity):
    """Entity that also updates when read marks or the opened post change (no API request)."""

    async def async_added_to_hass(self) -> None:
        """Listen to the coordinator and to the entry's local-update signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.local_update_signal, self.async_write_ha_state
            )
        )
