"""Data update coordinator for the Social Schools integration.

One coordinator per config entry polls the feed of every school of the
account, refreshes each school calendar once an hour and detects new posts
with a small bookkeeping record per school (``high_water`` and ``seen_ids``)
that is persisted in a private :class:`Store`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import SocialSchoolsClient
from .const import (
    CALENDAR_LOOKBACK_DAYS,
    CALENDAR_REFRESH_INTERVAL,
    DEFAULT_CALENDAR_DAYS,
    DEFAULT_POSTS_INTERVAL,
    DOMAIN,
    FETCH_POST_DETAILS,
    MAX_CALENDAR_DAYS,
    MAX_DETAIL_FETCHES_PER_UPDATE,
    MAX_POSTS_IN_LIST,
    MIN_CALENDAR_DAYS,
    MIN_POSTS_INTERVAL,
    NEW_POST_GRACE,
    OPT_CALENDAR_DAYS,
    OPT_POSTS_INTERVAL,
    SCHOOL_TIMEZONE,
    SEEN_IDS_LIMIT,
)
from .errors import ApiError, CannotConnect, InvalidAuth
from .models import Account, Post, School, SchoolEvent, parse_events, parse_post, parse_posts_page

if TYPE_CHECKING:
    from . import SocialSchoolsConfigEntry

_LOGGER = logging.getLogger(__name__)

_STORE_SCHOOLS = "schools"
_STORE_HIGH_WATER = "high_water"
_STORE_SEEN_IDS = "seen_ids"


@dataclass
class SchoolData:
    """Everything the entities of one school show."""

    school: School
    posts: list[Post]  # newest first, at most FEED_LIMIT
    events: list[SchoolEvent]  # sorted by start
    events_fetched_at: datetime | None  # UTC
    new_posts: list[Post]  # detected as new in this update, oldest first; else []


@dataclass
class SocialSchoolsData:
    """Coordinator data: the account and the data of each of its schools."""

    account: Account
    schools: dict[int, SchoolData]  # keyed by school id


@dataclass
class _SchoolTracker:
    """In-memory bookkeeping of one school; ``high_water``/``seen_ids`` mirror the store."""

    seeded: bool = False
    high_water: datetime | None = None  # UTC
    seen_ids: list[int] = field(default_factory=list)  # insertion order, oldest first
    events_horizon_days: int | None = None  # calendar_days used for the cached events
    events_failing: bool = False  # a warning was logged for the ongoing calendar failure


def _chronological(post: Post) -> tuple[datetime, int]:
    """Sort key: oldest first, ties by id."""
    return (post.created, post.id)


def _parse_stored_datetime(value: Any) -> datetime | None:
    """Return the aware UTC datetime of a stored ISO string, or None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SocialSchoolsCoordinator(DataUpdateCoordinator[SocialSchoolsData]):
    """Poll the feeds and calendars of all schools of one account."""

    config_entry: SocialSchoolsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SocialSchoolsConfigEntry,
        client: SocialSchoolsClient,
        account: Account,
        store: Store[dict[str, Any]],
    ) -> None:
        """Initialise with the poll interval from the entry options (clamped to the minimum)."""
        minutes = max(
            MIN_POSTS_INTERVAL, int(entry.options.get(OPT_POSTS_INTERVAL, DEFAULT_POSTS_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # The entry id, not the title: the title is the parent's display name and the
            # coordinator writes its name into every error and recovery log line.
            name=f"{DOMAIN} {entry.entry_id}",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.account = account
        self.last_success_utc: datetime | None = None
        self.consecutive_failures = 0
        self._store = store
        self._trackers = {school.id: _SchoolTracker() for school in account.schools}
        self._detail_cache: dict[int, str] = {}
        self._tz = ZoneInfo(SCHOOL_TIMEZONE)

    async def async_load_store(self) -> None:
        """Load the bookkeeping of an earlier run; called once before the first refresh."""
        stored = await self._store.async_load()
        schools = stored.get(_STORE_SCHOOLS) if isinstance(stored, dict) else None
        if not isinstance(schools, dict):
            return
        for school_id, tracker in self._trackers.items():
            record = schools.get(str(school_id))
            if not isinstance(record, dict):
                continue
            tracker.high_water = _parse_stored_datetime(record.get(_STORE_HIGH_WATER))
            seen_ids = record.get(_STORE_SEEN_IDS)
            if isinstance(seen_ids, list):
                tracker.seen_ids = [value for value in seen_ids if isinstance(value, int)]

    async def _async_update_data(self) -> SocialSchoolsData:
        """Fetch every feed, refresh the calendars that are due and detect new posts."""
        now = dt_util.utcnow()
        calendar_days = self._calendar_days()
        fetched: dict[int, tuple[list[Post], list[SchoolEvent], datetime | None]] = {}
        try:
            for school in self.account.schools:
                posts = parse_posts_page(await self.client.async_get_posts(school.id))
                if FETCH_POST_DETAILS:
                    posts = await self._async_add_details(school, posts)
                events, events_fetched_at = await self._async_events(school, now, calendar_days)
                fetched[school.id] = (posts, events, events_fetched_at)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(
                "The stored Social Schools login is no longer valid"
            ) from err
        except (CannotConnect, ApiError) as err:
            raise UpdateFailed(f"Fetching Social Schools data failed: {err}") from err

        schools: dict[int, SchoolData] = {}
        store_changed = False
        for school in self.account.schools:
            posts, events, events_fetched_at = fetched[school.id]
            new_posts, changed = self._detect_new_posts(school.id, posts)
            store_changed = store_changed or changed
            schools[school.id] = SchoolData(
                school=school,
                posts=posts,
                events=events,
                events_fetched_at=events_fetched_at,
                new_posts=new_posts,
            )
        if store_changed:
            await self._store.async_save(self._store_payload())
        if FETCH_POST_DETAILS:
            current_ids = {post.id for data in schools.values() for post in data.posts}
            self._detail_cache = {
                post_id: text
                for post_id, text in self._detail_cache.items()
                if post_id in current_ids
            }
        return SocialSchoolsData(account=self.account, schools=schools)

    @callback
    def _async_refresh_finished(self) -> None:
        """Track the outcome of every refresh for diagnostics."""
        if self.last_update_success:
            self.consecutive_failures = 0
            self.last_success_utc = dt_util.utcnow()
        else:
            self.consecutive_failures += 1

    def _calendar_days(self) -> int:
        """Return the calendar horizon option, clamped to its allowed range."""
        value = int(self.config_entry.options.get(OPT_CALENDAR_DAYS, DEFAULT_CALENDAR_DAYS))
        return max(MIN_CALENDAR_DAYS, min(MAX_CALENDAR_DAYS, value))

    async def _async_add_details(self, school: School, posts: list[Post]) -> list[Post]:
        """Replace the feed text of the newest posts by their (cached) detail text."""
        fetches = 0
        result: list[Post] = []
        for index, post in enumerate(posts):
            text = self._detail_cache.get(post.id)
            if (
                text is None
                and index <= MAX_POSTS_IN_LIST
                and fetches < MAX_DETAIL_FETCHES_PER_UPDATE
            ):
                fetches += 1
                try:
                    text = parse_post(await self.client.async_get_post(school.id, post.id)).text
                except (CannotConnect, ApiError, ValueError) as err:
                    _LOGGER.debug(
                        "School %s: detail of post %s unavailable, using the feed text (%s)",
                        school.id,
                        post.id,
                        err,
                    )
                else:
                    self._detail_cache[post.id] = text
            result.append(post if text is None else replace(post, text=text))
        return result

    async def _async_events(
        self, school: School, now: datetime, calendar_days: int
    ) -> tuple[list[SchoolEvent], datetime | None]:
        """Return the school calendar: refetched when due, otherwise the cached list.

        A failed refetch keeps the previous events (warning once, then debug);
        the next poll tries again.  Authentication errors propagate.
        """
        tracker = self._trackers[school.id]
        previous = self.data.schools.get(school.id) if self.data else None
        cached_events = previous.events if previous else []
        cached_at = previous.events_fetched_at if previous else None
        if (
            cached_at is not None
            and now - cached_at < CALENDAR_REFRESH_INTERVAL
            and tracker.events_horizon_days == calendar_days
        ):
            return cached_events, cached_at
        start = now - timedelta(days=CALENDAR_LOOKBACK_DAYS)
        end = now + timedelta(days=calendar_days)
        try:
            raw = await self.client.async_get_events(school.id, start, end)
        except (CannotConnect, ApiError) as err:
            if tracker.events_failing:
                _LOGGER.debug("School %s: calendar still unavailable (%s)", school.id, err)
            else:
                tracker.events_failing = True
                _LOGGER.warning(
                    "School %s: calendar could not be refreshed, keeping the previous events (%s)",
                    school.id,
                    err,
                )
            return cached_events, cached_at
        if tracker.events_failing:
            tracker.events_failing = False
            _LOGGER.info("School %s: calendar refresh recovered", school.id)
        tracker.events_horizon_days = calendar_days
        return parse_events(raw, tz=self._tz), now

    def _detect_new_posts(self, school_id: int, posts: list[Post]) -> tuple[list[Post], bool]:
        """Update the bookkeeping of a school; return (new posts oldest first, store changed).

        The first fetch of this process only seeds the bookkeeping and reports
        nothing.  Afterwards a post is new when its id was never seen and it
        was created after ``high_water - NEW_POST_GRACE``; edited posts keep
        their id and are therefore never new.
        """
        tracker = self._trackers[school_id]
        if tracker.seeded:
            threshold = None if tracker.high_water is None else tracker.high_water - NEW_POST_GRACE
            recorded = [
                post
                for post in posts
                if post.id not in tracker.seen_ids
                and (threshold is None or post.created > threshold)
            ]
        else:
            recorded = list(posts)
        recorded.sort(key=_chronological)
        changed = False
        for post in recorded:
            if post.id not in tracker.seen_ids:
                tracker.seen_ids.append(post.id)
                changed = True
            if tracker.high_water is None or post.created > tracker.high_water:
                tracker.high_water = post.created
                changed = True
        if len(tracker.seen_ids) > SEEN_IDS_LIMIT:
            del tracker.seen_ids[: len(tracker.seen_ids) - SEEN_IDS_LIMIT]
            changed = True
        new_posts = recorded if tracker.seeded else []
        tracker.seeded = True
        return new_posts, changed

    def _store_payload(self) -> dict[str, Any]:
        """Serialise the bookkeeping of all schools (JSON types only)."""
        return {
            _STORE_SCHOOLS: {
                str(school_id): {
                    _STORE_HIGH_WATER: (
                        None if tracker.high_water is None else tracker.high_water.isoformat()
                    ),
                    _STORE_SEEN_IDS: list(tracker.seen_ids),
                }
                for school_id, tracker in self._trackers.items()
            }
        }
