"""Event entity of the Social Schools integration: one event per new post."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SocialSchoolsConfigEntry
from .const import ATTR_SHORT_TEXT, EVENT_TYPE_NEW_POST
from .entity import SocialSchoolsEntity, post_summary

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SocialSchoolsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the new-post event entity of every school."""
    coordinator = entry.runtime_data
    async_add_entities(
        SocialSchoolsNewPostEvent(coordinator, school, "new_post")
        for school in coordinator.account.schools
    )


class SocialSchoolsNewPostEvent(SocialSchoolsEntity, EventEntity):
    """Fires ``nieuw_bericht`` for every post that appeared in the feed since the previous poll.

    A seeding refresh (first run or restart) reports no new posts, and the
    event platform restores the last event by itself, so nothing fires twice.
    """

    # Class attribute as in the core event platforms; the base class types it as an instance list.
    _attr_event_types = [EVENT_TYPE_NEW_POST]  # noqa: RUF012
    # The post text stays out of the recorder, like ``tekst`` and ``berichten`` of the sensors;
    # automations get it from the state change and the restore cache keeps the last event.
    _unrecorded_attributes = frozenset({ATTR_SHORT_TEXT})

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire one event per new post, oldest first, each written as its own state change.

        A failed refresh notifies the listeners with the previous data, whose
        new posts have been fired already, so only a successful one counts.
        """
        data = self.school_data
        succeeded = data is not None and self.coordinator.last_update_success
        new_posts = data.new_posts if succeeded else []
        for post in new_posts:
            self._trigger_event(EVENT_TYPE_NEW_POST, post_summary(post))
            self.async_write_ha_state()
        if not new_posts:
            super()._handle_coordinator_update()
