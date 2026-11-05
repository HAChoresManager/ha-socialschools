"""Calendar of the Social Schools integration: the school agenda."""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SocialSchoolsConfigEntry
from .entity import SocialSchoolsEntity
from .models import SchoolEvent

PARALLEL_UPDATES = 0

UNTITLED_EVENT = "(geen titel)"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SocialSchoolsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the calendar of every school."""
    coordinator = entry.runtime_data
    async_add_entities(
        SocialSchoolsCalendar(coordinator, school, "calendar")
        for school in coordinator.account.schools
    )


def _to_calendar_event(event: SchoolEvent) -> CalendarEvent:
    """Convert a school event; all-day events keep their dates with the exclusive end."""
    return CalendarEvent(
        start=event.start,
        end=event.end,
        summary=event.title or UNTITLED_EVENT,
        description=event.description or None,
        location=event.location or None,
        uid=f"socialschools-{event.id}",
    )


def _as_datetime(value: datetime | date) -> datetime:
    """Return ``value`` as an aware datetime; a date becomes local midnight in Home Assistant's zone.

    Home Assistant evaluates all-day events in its own time zone (``CalendarEntity.state``
    and the start/end alarms use ``start_of_local_day``), so the choice of the current or
    next event and the overlap test follow the same interpretation.
    """
    if isinstance(value, datetime):
        return value
    return dt_util.start_of_local_day(value)


class SocialSchoolsCalendar(SocialSchoolsEntity, CalendarEntity):
    """The agenda of one school, answered from the coordinator's hourly refreshed events."""

    @property
    def event(self) -> CalendarEvent | None:
        """Return the ongoing or next event: the first (by start) that has not ended yet."""
        if (data := self.school_data) is None:
            return None
        now = dt_util.utcnow()
        return next(
            (_to_calendar_event(event) for event in data.events if _as_datetime(event.end) > now),
            None,
        )

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the cached events that overlap ``[start_date, end_date)``, sorted by start."""
        if (data := self.school_data) is None:
            return []
        return [
            _to_calendar_event(event)
            for event in data.events
            if _as_datetime(event.start) < end_date and _as_datetime(event.end) > start_date
        ]
