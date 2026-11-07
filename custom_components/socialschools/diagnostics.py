"""Diagnostics for the Social Schools integration: counts and timestamps only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SocialSchoolsConfigEntry
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_USER_PROFILE_ID,
    CONF_USERNAME,
    OPT_CHILDREN,
    OPT_READERS,
)

# Readers and children are names of family members: redacted, only their counts are shown.
TO_REDACT = {
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    CONF_USER_PROFILE_ID,
    "title",
    OPT_READERS,
    OPT_CHILDREN,
}


def _isoformat(value: datetime | None) -> str | None:
    """Return the ISO 8601 form of a datetime, or None."""
    return None if value is None else value.isoformat()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SocialSchoolsConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics without names, post content or event content."""
    coordinator = entry.runtime_data
    interval = coordinator.update_interval
    return {
        "entry": async_redact_data(
            {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_success_utc": _isoformat(coordinator.last_success_utc),
            "consecutive_failures": coordinator.consecutive_failures,
            "update_interval_seconds": None if interval is None else int(interval.total_seconds()),
            "reader_count": len(coordinator.readers),
            "child_count": len(coordinator.children),
            "posts_with_read_marks": coordinator.read_state.marked_post_count(),
            "open_post_cache_size": coordinator.open_cache_size,
            "schools_with_opened_post": len(coordinator.opened),
            "schools": [
                {
                    "school_id": school_id,
                    "post_count": len(school.posts),
                    "unread_post_count": sum(
                        1
                        for post in school.posts
                        if coordinator.read_state.unread_by(school_id, post.id, coordinator.readers)
                    ),
                    "event_count": len(school.events),
                    "events_fetched_at": _isoformat(school.events_fetched_at),
                    "newest_post_created": (
                        _isoformat(school.posts[0].created) if school.posts else None
                    ),
                }
                for school_id, school in coordinator.data.schools.items()
            ],
        },
    }
