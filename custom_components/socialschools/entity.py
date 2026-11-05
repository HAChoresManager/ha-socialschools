"""Base entity for the Social Schools integration: one service device per school."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_AUTHOR,
    ATTR_GROUP,
    ATTR_POST_ID,
    ATTR_POSTED,
    ATTR_SHORT_TEXT,
    ATTR_TITLE,
    DOMAIN,
    MAX_AUTHOR_LENGTH,
    MAX_LIST_GROUP_LENGTH,
    MAX_LIST_TITLE_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
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


def post_summary(post: Post) -> dict[str, Any]:
    """Return the bounded summary of ``post`` shared by the post list and the new-post event."""
    return {
        ATTR_POST_ID: post.id,
        ATTR_TITLE: truncate(post.title, MAX_LIST_TITLE_LENGTH)[0],
        ATTR_GROUP: truncate(", ".join(post.groups), MAX_LIST_GROUP_LENGTH)[0],
        ATTR_AUTHOR: truncate(post.author, MAX_AUTHOR_LENGTH)[0],
        ATTR_POSTED: posted_at(post),
        ATTR_SHORT_TEXT: truncate(post.text, MAX_SHORT_TEXT_LENGTH)[0],
    }


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
