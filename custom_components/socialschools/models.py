"""Data models and JSON parsing for the Social Schools integration (HA-free).

The frozen dataclasses are the only shapes the Home Assistant layer works
with.  The parse functions turn API JSON into them, tolerate missing optional
fields, skip unusable items and never put response content into exception
messages or log lines.  Every text they produce can be encoded as UTF-8.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from .const import ROLE_TYPE_PARENT, SCHOOL_TIMEZONE
from .errors import ApiError
from .textutil import collapse_whitespace, html_to_text

_LOGGER = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)
_ONE_HOUR = timedelta(hours=1)
_MIDNIGHT = time(0)
_ISO_DATE_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
"""Start of an ISO 8601 date *and* time; a bare date or week date is not a timestamp."""
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
"""A UTF-16 surrogate code point.  ``json.loads`` joins an escaped surrogate pair into one
character, so a surrogate left in a decoded string is unpaired (``"\\ud83d"``, half of an
emoji the server cut off) and cannot be encoded as UTF-8."""
_REPLACEMENT_CHARACTER = "\ufffd"
_MIN_YEAR = 1900
_MAX_YEAR = 9000
"""Years outside this window are rejected: no school date lives there, and near the edges of
the datetime range a timezone conversion or a day of arithmetic raises OverflowError."""


@dataclass(frozen=True)
class School:
    """A school at which the account holds a parent role."""

    id: int
    name: str  # school.displayName or school.name or str(id)
    role_id: int


@dataclass(frozen=True)
class Account:
    """The signed-in account with its parent-role schools in API order."""

    user_profile_id: int
    display_name: str
    schools: tuple[School, ...]


@dataclass(frozen=True)
class Post:
    """One feed post with its body as plain text."""

    id: int
    title: str  # whitespace-collapsed, may be ""
    text: str  # full plain text of the body, untruncated
    created: datetime  # aware, UTC
    groups: tuple[str, ...]  # communities[].name, in order, de-duplicated
    author: str  # author.displayName or ""
    comments_count: int
    attachments: tuple[str, ...]  # media[].fileName only
    community_ids: tuple[int, ...] = ()  # communities[].id, in order


@dataclass(frozen=True)
class SchoolEvent:
    """One calendar event; all-day events use dates with an exclusive end."""

    id: int
    title: str
    description: str  # plain text
    start: datetime | date  # date when all_day, else aware datetime in the school timezone
    end: datetime | date  # exclusive date when all_day
    all_day: bool
    location: str
    groups: tuple[str, ...]


def parse_datetime(value: Any, *, assume_tz: tzinfo) -> datetime:
    """Parse an ISO 8601 date-time ('Z', an offset, fractions or no offset) into an aware datetime.

    A value without offset is taken to be in ``assume_tz``.  A bare date, a
    non-string, a year outside 1900-9000 or anything else raises ValueError;
    the message never repeats the value.
    """
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    text = value.strip()
    if not _ISO_DATE_TIME_RE.match(text):
        raise ValueError("timestamp is not an ISO 8601 date and time")
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        raise ValueError("timestamp is not ISO 8601") from None
    if not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        raise ValueError("timestamp year is out of range")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=assume_tz)
    return parsed


def _as_int(value: Any) -> int | None:
    """Return ``value`` when it is an int (bools excluded), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _without_surrogates(value: str) -> str:
    """Return ``value`` with every unpaired surrogate replaced by U+FFFD.

    Home Assistant serialises states, attributes, device names and config entries as
    UTF-8 JSON, which fails on such a character.
    """
    return _SURROGATE_RE.sub(_REPLACEMENT_CHARACTER, value)


def _as_text(value: Any) -> str:
    """Return ``value`` whitespace-collapsed and UTF-8 safe when it is a string, else ``""``."""
    return collapse_whitespace(_without_surrogates(value)) if isinstance(value, str) else ""


def _as_plain_text(value: Any) -> str:
    """Return ``value`` as UTF-8 safe plain text (from HTML) when it is a string, else ``""``."""
    return html_to_text(_without_surrogates(value)) if isinstance(value, str) else ""


def _names(value: Any, key: str, *, unique: bool) -> tuple[str, ...]:
    """Collect the non-empty ``key`` strings of a list of objects, in order."""
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            name = _as_text(item.get(key)) if isinstance(item, Mapping) else ""
            if name and not (unique and name in names):
                names.append(name)
    return tuple(names)


def _community_ids(value: Any) -> tuple[int, ...]:
    """Collect the integer ``id`` of every community object, in order, de-duplicated."""
    ids: list[int] = []
    if isinstance(value, list):
        for item in value:
            community_id = _as_int(item.get("id")) if isinstance(item, Mapping) else None
            if community_id is not None and community_id not in ids:
                ids.append(community_id)
    return tuple(ids)


def parse_account(data: Any) -> Account:
    """Build an Account from ``useraccounts/current``; raises ApiError on an unusable profile.

    Schools come from the roles of type ROLE_TYPE_PARENT that carry a school id,
    de-duplicated by school id in API order.
    """
    if not isinstance(data, Mapping):
        raise ApiError("account: unexpected JSON shape")
    user_profile_id = _as_int(data.get("userProfileId"))
    if user_profile_id is None:
        raise ApiError("account: missing or invalid userProfileId")
    roles = data.get("roles")
    if not isinstance(roles, list):
        raise ApiError("account: missing or invalid roles")
    schools: dict[int, School] = {}
    for role in roles:
        if not isinstance(role, Mapping) or _as_int(role.get("type")) != ROLE_TYPE_PARENT:
            continue
        school = role.get("school")
        if not isinstance(school, Mapping):
            continue
        school_id = _as_int(school.get("id"))
        if school_id is None or school_id in schools:
            continue
        name = _as_text(school.get("displayName")) or _as_text(school.get("name")) or str(school_id)
        schools[school_id] = School(id=school_id, name=name, role_id=_as_int(role.get("id")) or 0)
    return Account(
        user_profile_id=user_profile_id,
        display_name=_as_text(data.get("displayName")),
        schools=tuple(schools.values()),
    )


def parse_post(data: Mapping[str, Any]) -> Post:
    """Build a Post from a feed item; raises ValueError when ``id`` or ``created`` is unusable.

    A ``created`` without offset is taken to be in SCHOOL_TIMEZONE; the result is UTC.
    """
    if not isinstance(data, Mapping):
        raise ValueError("post is not an object")
    post_id = _as_int(data.get("id"))
    if post_id is None:
        raise ValueError("post without a valid id")
    try:
        created = parse_datetime(
            data.get("created"), assume_tz=ZoneInfo(SCHOOL_TIMEZONE)
        ).astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("post without a valid created timestamp") from None
    author = data.get("author")
    return Post(
        id=post_id,
        title=_as_text(data.get("title")),
        text=_as_plain_text(data.get("body")),
        created=created,
        groups=_names(data.get("communities"), "name", unique=True),
        author=_as_text(author.get("displayName")) if isinstance(author, Mapping) else "",
        comments_count=_as_int(data.get("commentsCount")) or 0,
        attachments=_names(data.get("media"), "fileName", unique=False),
        community_ids=_community_ids(data.get("communities")),
    )


def parse_posts_page(data: Any) -> list[Post]:
    """Parse a ``communityposts`` page: unusable items are skipped, newest first.

    Anything that is not an object with a ``values`` list yields an empty list.
    """
    values = data.get("values") if isinstance(data, Mapping) else None
    if not isinstance(values, list):
        return []
    posts: list[Post] = []
    for index, item in enumerate(values):
        try:
            posts.append(parse_post(item))
        except (ValueError, OverflowError) as err:
            _LOGGER.debug("posts page: skipping item %d (%s)", index, err)
    posts.sort(key=lambda post: (post.created, post.id), reverse=True)
    return posts


def _all_day_end(start: datetime, end: datetime | None) -> date:
    """Return the exclusive end date of an all-day event from its local start and end.

    An end at local midnight after the start is already exclusive; any other
    end is inclusive and moves to the next day.  The result is after the start date.
    """
    start_date = start.date()
    if end is None:
        return start_date + _ONE_DAY
    if end.time() == _MIDNIGHT and end.astimezone(UTC) > start.astimezone(UTC):
        end_date = end.date()
    else:
        end_date = end.date() + _ONE_DAY
    return max(end_date, start_date + _ONE_DAY)


def _timed_end(start: datetime, end: datetime | None, tz: tzinfo) -> datetime:
    """Return the end of a timed event: one hour after the start unless a later end is given."""
    start_utc = start.astimezone(UTC)
    if end is None or end.astimezone(UTC) <= start_utc:
        return (start_utc + _ONE_HOUR).astimezone(tz)
    return end


def parse_event(data: Mapping[str, Any], *, tz: tzinfo) -> SchoolEvent:
    """Build a SchoolEvent in ``tz``; raises ValueError when ``id`` or ``start`` is unusable.

    All-day events become dates with an exclusive end; timed events get aware
    datetimes in ``tz`` and last one hour when ``end`` is missing or not after
    the start.  Timestamps without offset are taken to be in ``tz``.
    """
    if not isinstance(data, Mapping):
        raise ValueError("event is not an object")
    event_id = _as_int(data.get("id"))
    if event_id is None:
        raise ValueError("event without a valid id")
    try:
        start = parse_datetime(data.get("start"), assume_tz=tz).astimezone(tz)
    except (ValueError, OverflowError):
        raise ValueError("event without a valid start") from None
    try:
        end: datetime | None = parse_datetime(data.get("end"), assume_tz=tz).astimezone(tz)
    except (ValueError, OverflowError):
        end = None
    all_day = bool(data.get("allDay"))
    try:
        start_value: datetime | date = start.date() if all_day else start
        end_value: datetime | date = (
            _all_day_end(start, end) if all_day else _timed_end(start, end, tz)
        )
    except OverflowError:
        raise ValueError("event with a start or end outside the supported range") from None
    return SchoolEvent(
        id=event_id,
        title=_as_text(data.get("title")),
        description=_as_plain_text(data.get("description")),
        start=start_value,
        end=end_value,
        all_day=all_day,
        location=_as_text(data.get("location")),
        groups=_names(data.get("communities"), "name", unique=True),
    )


def _start_instant(start: datetime | date, tz: tzinfo) -> datetime:
    """Return a UTC datetime for sorting; an all-day date counts as local midnight."""
    if isinstance(start, datetime):
        return start.astimezone(UTC)
    return datetime.combine(start, _MIDNIGHT, tzinfo=tz).astimezone(UTC)


def parse_events(data: Any, *, tz: tzinfo) -> list[SchoolEvent]:
    """Parse the ``events`` response (a list, or an object with ``values``), sorted by start.

    Unusable items are skipped with a debug log; anything else yields an empty list.
    """
    items = data.get("values") if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        return []
    events: list[SchoolEvent] = []
    for index, item in enumerate(items):
        try:
            events.append(parse_event(item, tz=tz))
        except (ValueError, OverflowError) as err:
            _LOGGER.debug("events: skipping item %d (%s)", index, err)
    events.sort(key=lambda event: (_start_instant(event.start, tz), event.id))
    return events
