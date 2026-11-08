"""Data update coordinator for the Social Schools integration.

One coordinator per config entry polls the feed of every school of the
account, refreshes each school calendar once an hour and detects new posts
with a small bookkeeping record per school (``high_water`` and ``seen_ids``)
that is persisted in a private :class:`Store`.  The same store keeps the local
read marks per person (``read``/``readers``, storage version 2); those are
bookkeeping inside Home Assistant only and are never sent to Social Schools.
Once a day it fetches the account again for the children and their groups,
which link posts to children together with the ``kinderen`` option.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .children import Child, child_from_account, merge_children, parse_children
from .client import SocialSchoolsClient
from .const import (
    ACCOUNT_REFRESH_INTERVAL,
    ACCOUNT_RETRY_INTERVAL,
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
    OPEN_POST_CACHE_SIZE,
    OPT_CALENDAR_DAYS,
    OPT_CHILDREN,
    OPT_POSTS_INTERVAL,
    OPT_READERS,
    SCHOOL_TIMEZONE,
    SEEN_IDS_LIMIT,
    SIGNAL_LOCAL_UPDATE,
)
from .errors import ApiError, CannotConnect, InvalidAuth
from .models import (
    Account,
    ConnectedChild,
    Post,
    School,
    SchoolEvent,
    parse_connected_children,
    parse_events,
    parse_post,
    parse_posts_page,
)
from .reading import ReadState
from .textutil import looks_cut

if TYPE_CHECKING:
    from . import SocialSchoolsConfigEntry

_LOGGER = logging.getLogger(__name__)

_STORE_SCHOOLS = "schools"
_STORE_HIGH_WATER = "high_water"
_STORE_SEEN_IDS = "seen_ids"


class SocialSchoolsStore(Store[dict[str, Any]]):
    """The private store of one entry; migrates version 1 (no read marks) to 2."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        """Version 1 had only the new-post bookkeeping: add empty read marks per school.

        No reader is recorded as seeded, so the first refresh after the upgrade marks
        every post in the feed as read by every configured reader.
        """
        if old_major_version != 1:
            raise NotImplementedError
        data = old_data if isinstance(old_data, dict) else {}
        schools = data.get(_STORE_SCHOOLS)
        if isinstance(schools, dict):
            for record in schools.values():
                if isinstance(record, dict):
                    record.setdefault("read", {})
                    record.setdefault("readers", [])
        return data


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
        connected: Mapping[int, Sequence[ConnectedChild]] | None = None,
    ) -> None:
        """Initialise with the poll interval from the entry options (clamped to the minimum).

        ``connected`` holds the children of the account per school, as fetched at setup.
        """
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
        self.read_state = ReadState()
        self.option_children: tuple[Child, ...] = _children_option(
            entry.options.get(OPT_CHILDREN, "")
        )
        self._account_children = _account_children(connected or {})
        self._account_refresh_at = dt_util.utcnow() + ACCOUNT_REFRESH_INTERVAL
        self._account_failing = False
        self.opened: dict[int, Post] = {}  # per school: the post shown by the opened-post sensor
        self._open_cache: OrderedDict[tuple[int, int], Post] = OrderedDict()

    def children_for(self, school_id: int) -> tuple[Child, ...]:
        """Return the children of a school: those of the account, completed by the option."""
        return merge_children(self._account_children.get(school_id, ()), self.option_children)

    @property
    def account_child_count(self) -> int:
        """Return how many children with groups the account has, over all schools."""
        return sum(len(children) for children in self._account_children.values())

    @property
    def readers(self) -> list[str]:
        """Return the person entity ids that must have read a post, in option order."""
        value = self.config_entry.options.get(OPT_READERS, [])
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(item for item in value if isinstance(item, str)))

    @property
    def local_update_signal(self) -> str:
        """Return the dispatcher signal sent when read marks or the opened post change."""
        return SIGNAL_LOCAL_UPDATE.format(entry_id=self.config_entry.entry_id)

    def find_post(self, post_id: int) -> tuple[School, Post] | None:
        """Return the school and the feed post with ``post_id``, if the current feed has it."""
        if self.data is None:
            return None
        for school_data in self.data.schools.values():
            for post in school_data.posts:
                if post.id == post_id:
                    return school_data.school, post
        return None

    def find_earlier_post(self, post_id: int) -> School | None:
        """Return the school whose feed held ``post_id`` before, if this entry knows it.

        That is a post that has dropped out of the feed since: its id is among the
        seen ids of the new-post bookkeeping (the newest SEEN_IDS_LIMIT per school,
        kept across restarts) or its detail is still cached.
        """
        for school in self.account.schools:
            seen_ids = self._trackers[school.id].seen_ids
            if post_id in seen_ids or (school.id, post_id) in self._open_cache:
                return school
        return None

    async def async_mark_read(
        self, school_id: int, post_ids: list[int], person: str, read: bool
    ) -> None:
        """Change local read marks, persist them and update the entities (no API request)."""
        if self.read_state.mark(school_id, post_ids, person, read):
            await self._store.async_save(self._store_payload())
        async_dispatcher_send(self.hass, self.local_update_signal)

    async def async_open_post(self, school: School, post_id: int) -> Post:
        """Show the full post ``post_id`` in the opened-post sensor and return it.

        A post in the current feed is shown as the feed has it, without a request: the
        feed carries the whole body (the probe found feed and detail bodies equally
        long).  Only a post that has dropped out of the feed, or a feed text with a sign
        of being cut off (see ``_feed_text_may_be_cut``), is fetched with the whitelisted
        ``GET communityposts/{id}``; fetched posts stay in a small in-memory cache, so
        opening one again costs no request either.  Whether Social Schools counts such
        a fetch as a view is unknown, so it is kept to these cases.

        Raises CannotConnect, ApiError, InvalidAuth or ValueError; the previously
        opened post then stays.
        """
        feed_post = self._feed_post(school.id, post_id)
        if feed_post is not None and not self._feed_text_may_be_cut(feed_post):
            post = feed_post
        else:
            post = await self._async_fetch_for_opening(school, post_id)
        self.opened[school.id] = post
        async_dispatcher_send(self.hass, self.local_update_signal)
        return post

    def _feed_post(self, school_id: int, post_id: int) -> Post | None:
        """Return the post ``post_id`` from the current feed of a school, if it is there."""
        school_data = self.data.schools.get(school_id) if self.data is not None else None
        if school_data is None:
            return None
        return next((post for post in school_data.posts if post.id == post_id), None)

    def _feed_text_may_be_cut(self, post: Post) -> bool:
        """Return True when there is a sign that the feed text of ``post`` is not complete.

        With FETCH_POST_DETAILS (set when the probe finds shorter feed bodies) every feed
        text counts as cut, unless the coordinator already replaced it by the detail text.
        Otherwise a text that ends in an ellipsis (``…`` or ``...``) counts as cut.
        """
        if post.id in self._detail_cache:
            return False
        return FETCH_POST_DETAILS or looks_cut(post.text)

    async def _async_fetch_for_opening(self, school: School, post_id: int) -> Post:
        """Return the detail of a post from the cache, else fetch it once and cache it."""
        key = (school.id, post_id)
        post = self._open_cache.get(key)
        if post is not None:
            self._open_cache.move_to_end(key)
            return post
        post = parse_post(await self.client.async_get_post(school.id, post_id))
        if post.id != post_id:
            raise ValueError("the detail belongs to another post")
        self._open_cache[key] = post
        while len(self._open_cache) > OPEN_POST_CACHE_SIZE:
            self._open_cache.popitem(last=False)
        return post

    @property
    def open_cache_size(self) -> int:
        """Return how many post details are cached (for diagnostics)."""
        return len(self._open_cache)

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
            self.read_state.load_school(school_id, record)

    async def _async_update_data(self) -> SocialSchoolsData:
        """Fetch every feed, refresh the calendars that are due and detect new posts."""
        now = dt_util.utcnow()
        calendar_days = self._calendar_days()
        fetched: dict[int, tuple[list[Post], list[SchoolEvent], datetime | None]] = {}
        try:
            await self._async_refresh_children(now)
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
            read_changed = self.read_state.sync(
                school.id, self.readers, [post.id for post in posts]
            )
            store_changed = store_changed or changed or read_changed
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

    async def _async_refresh_children(self, now: datetime) -> None:
        """Fetch the children of the account and their groups again, once a day.

        Groups change with the school year and a child that starts school appears in the
        account; this picks both up without a restart.  A failure keeps the children of
        the previous fetch and is retried after ACCOUNT_RETRY_INTERVAL (warning once, then
        debug); authentication errors propagate.  Only counts are logged, never names.
        """
        if now < self._account_refresh_at:
            return
        try:
            raw = await self.client.async_get_current_account()
        except (CannotConnect, ApiError) as err:
            self._account_refresh_at = now + ACCOUNT_RETRY_INTERVAL
            if self._account_failing:
                _LOGGER.debug("The children of the account are still unavailable (%s)", err)
            else:
                self._account_failing = True
                _LOGGER.warning(
                    "The children of the account could not be refreshed, keeping the previous "
                    "ones (%s)",
                    err,
                )
            return
        self._account_failing = False
        self._account_children = _account_children(parse_connected_children(raw))
        self._account_refresh_at = now + ACCOUNT_REFRESH_INTERVAL
        _LOGGER.debug(
            "Entry %s: %d child(ren) with groups in the account",
            self.config_entry.entry_id,
            self.account_child_count,
        )

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
                    **self.read_state.school_payload(school_id),
                }
                for school_id, tracker in self._trackers.items()
            }
        }


def _account_children(
    connected: Mapping[int, Sequence[ConnectedChild]],
) -> dict[int, tuple[Child, ...]]:
    """Return the children of the account per school; a child without a group is left out."""
    return {
        school_id: tuple(
            child
            for connection in children
            if (child := child_from_account(connection.name, connection.groups)) is not None
        )
        for school_id, children in connected.items()
    }


def _children_option(value: Any) -> tuple[Child, ...]:
    """Parse the ``kinderen`` option; an unusable value (validated in the flow) counts as empty."""
    if not isinstance(value, str):
        return ()
    try:
        return parse_children(value)
    except ValueError:
        _LOGGER.warning("The children option is not valid and is ignored")
        return ()
