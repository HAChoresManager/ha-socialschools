"""Sensors of the Social Schools integration: latest post, post list, unread and opened post."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SocialSchoolsConfigEntry
from .const import (
    ATTR_ATTACHMENTS,
    ATTR_AUTHOR,
    ATTR_COMMENTS,
    ATTR_FIRST,
    ATTR_GROUP,
    ATTR_NEW,
    ATTR_PER_PERSON,
    ATTR_POST_ID,
    ATTR_POSTED,
    ATTR_POSTS,
    ATTR_SHORT_TEXT,
    ATTR_TEXT,
    ATTR_TO_READ,
    ATTR_TRUNCATED,
    ATTR_URL,
    ATTRIBUTES_BYTE_BUDGET,
    DEFAULT_POSTS_IN_LIST,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_ATTACHMENTS,
    MAX_AUTHOR_LENGTH,
    MAX_GROUP_LENGTH,
    MAX_OPEN_TEXT_LENGTH,
    MAX_POSTS_IN_LIST,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
    MIN_POSTS_IN_LIST,
    OPT_POSTS_IN_LIST,
    POST_COUNT_WINDOW,
)
from .entity import (
    LocalUpdatesEntity,
    SocialSchoolsEntity,
    child_fields,
    person_name,
    post_summary,
    post_url,
    posted_at,
)
from .models import Post
from .textutil import truncate

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SocialSchoolsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the latest-post, post-count, unread and opened-post sensors of every school."""
    coordinator = entry.runtime_data
    async_add_entities(
        sensor
        for school in coordinator.account.schools
        for sensor in (
            SocialSchoolsLatestPostSensor(coordinator, school, "latest_post"),
            SocialSchoolsPostCountSensor(coordinator, school, "post_count"),
            SocialSchoolsUnreadSensor(coordinator, school, "unread"),
            SocialSchoolsOpenPostSensor(coordinator, school, "open_post"),
        )
    )


def _attributes_size(attributes: dict[str, Any]) -> int:
    """Return the size of ``attributes`` as UTF-8 encoded JSON."""
    return len(json.dumps(attributes, ensure_ascii=False).encode())


def _fit_text(attributes: dict[str, Any], full_text: str, longest: int) -> None:
    """Cut ``tekst`` to the longest prefix (at most ``longest`` characters) within the budget.

    A binary search over the length, so multi-byte characters and JSON escapes are
    accounted for exactly; ``afgekapt`` becomes True.
    """

    def text_of(length: int) -> str:
        return truncate(full_text, length)[0] if length >= 1 else ""

    def fits(length: int) -> bool:
        return _attributes_size(attributes | {ATTR_TEXT: text_of(length)}) <= ATTRIBUTES_BYTE_BUDGET

    low, high = 0, longest  # fits(low) is assumed (or nothing fits); fits(high) is False
    while low < high - 1:
        middle = (low + high) // 2
        if fits(middle):
            low = middle
        else:
            high = middle
    attributes[ATTR_TEXT] = text_of(low)
    attributes[ATTR_TRUNCATED] = True


class SocialSchoolsLatestPostSensor(SocialSchoolsEntity, SensorEntity):
    """The newest post of a school: its title as state, text and metadata as attributes."""

    _unrecorded_attributes = frozenset({ATTR_TEXT, ATTR_ATTACHMENTS})

    @property
    def _latest_post(self) -> Post | None:
        """Return the newest post, if the feed holds any."""
        data = self.school_data
        return data.posts[0] if data is not None and data.posts else None

    @property
    def native_value(self) -> str | None:
        """Return the title (or the first line of the text) of the newest post, cut to 255 chars."""
        if (post := self._latest_post) is None:
            return None
        headline = post.title or post.text.partition("\n")[0]
        return truncate(headline, MAX_TITLE_LENGTH)[0] or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the bounded metadata and text of the newest post.

        Every attribute has a length cap; attachments are dropped from the end
        should the attributes still exceed ATTRIBUTES_BYTE_BUDGET.
        """
        if (post := self._latest_post) is None:
            return None
        text, truncated = truncate(post.text, MAX_TEXT_LENGTH)
        attachments = [
            truncate(name, MAX_ATTACHMENT_NAME_LENGTH)[0]
            for name in post.attachments[:MAX_ATTACHMENTS]
        ]
        attributes = {
            ATTR_POST_ID: post.id,
            ATTR_GROUP: truncate(", ".join(post.groups), MAX_GROUP_LENGTH)[0],
            ATTR_AUTHOR: truncate(post.author, MAX_AUTHOR_LENGTH)[0],
            ATTR_POSTED: posted_at(post),
            ATTR_TEXT: text,
            ATTR_TRUNCATED: truncated,
            ATTR_ATTACHMENTS: attachments,
            ATTR_COMMENTS: post.comments_count,
        }
        while attachments and _attributes_size(attributes) > ATTRIBUTES_BYTE_BUDGET:
            attachments.pop()
        return attributes


class SocialSchoolsPostCountSensor(LocalUpdatesEntity, SensorEntity):
    """The number of posts of the past week, with a bounded list of the newest posts."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({ATTR_POSTS})

    @property
    def native_value(self) -> int | None:
        """Return how many fetched posts were created within POST_COUNT_WINDOW, as of this poll."""
        if (data := self.school_data) is None:
            return None
        since = dt_util.utcnow() - POST_COUNT_WINDOW
        return sum(1 for post in data.posts if post.created >= since)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the newest posts, limited by the option and then by ATTRIBUTES_BYTE_BUDGET."""
        if (data := self.school_data) is None:
            return None
        children = self.coordinator.children_for(self._school_id)
        posts = [
            post_summary(post, children) | self.read_fields(post)
            for post in data.posts[: self._posts_in_list]
        ]
        attributes = {ATTR_POSTS: posts}
        while posts and _attributes_size(attributes) > ATTRIBUTES_BYTE_BUDGET:
            posts.pop()
        return attributes

    @property
    def _posts_in_list(self) -> int:
        """Return the configured length of the post list, clamped to its allowed range."""
        options = self.coordinator.config_entry.options
        value = int(options.get(OPT_POSTS_IN_LIST, DEFAULT_POSTS_IN_LIST))
        return max(MIN_POSTS_IN_LIST, min(MAX_POSTS_IN_LIST, value))


class SocialSchoolsUnreadSensor(LocalUpdatesEntity, SensorEntity):
    """How many posts in the feed not every reader has read yet (local bookkeeping only)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the number of feed posts with at least one reader who has not read them."""
        if (data := self.school_data) is None:
            return None
        return sum(1 for post in data.posts if self.read_fields(post)[ATTR_NEW])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return ``eerste`` (the newest unread post or null) and ``per_persoon`` counts."""
        if (data := self.school_data) is None:
            return None
        coordinator = self.coordinator
        readers = coordinator.readers
        per_person = dict.fromkeys(readers, 0)
        first: dict[str, Any] | None = None
        for post in data.posts:  # newest first
            to_read = coordinator.read_state.unread_by(self._school_id, post.id, readers)
            for person in to_read:
                per_person[person] += 1
            if to_read and first is None:
                summary = post_summary(post, coordinator.children_for(self._school_id))
                summary.pop(ATTR_SHORT_TEXT)
                summary[ATTR_TO_READ] = [person_name(self.hass, person) for person in to_read]
                first = summary
        return {
            ATTR_FIRST: first,
            ATTR_PER_PERSON: {
                person_name(self.hass, person): count for person, count in per_person.items()
            },
        }


class SocialSchoolsOpenPostSensor(LocalUpdatesEntity, SensorEntity):
    """The post opened with ``open_bericht`` (else the newest post), with its full text.

    Opening a post marks nothing as read; the dashboard calls ``markeer_gelezen``.
    """

    _unrecorded_attributes = frozenset({ATTR_TEXT, ATTR_ATTACHMENTS})

    @property
    def _post(self) -> Post | None:
        """Return the opened post of this school, else the newest post in the feed."""
        if (opened := self.coordinator.opened.get(self._school_id)) is not None:
            return opened
        data = self.school_data
        return data.posts[0] if data is not None and data.posts else None

    @property
    def native_value(self) -> str | None:
        """Return the title (or the first line of the text), cut to 255 characters."""
        if (post := self._post) is None:
            return None
        headline = post.title or post.text.partition("\n")[0]
        return truncate(headline, MAX_TITLE_LENGTH)[0] or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the metadata and up to MAX_OPEN_TEXT_LENGTH characters of text, within budget.

        Should the attributes exceed ATTRIBUTES_BYTE_BUDGET, the text is cut further
        (``afgekapt`` becomes True) and then attachments are dropped from the end.
        """
        if (post := self._post) is None:
            return None
        text, truncated = truncate(post.text, MAX_OPEN_TEXT_LENGTH)
        attachments = [
            truncate(name, MAX_ATTACHMENT_NAME_LENGTH)[0]
            for name in post.attachments[:MAX_ATTACHMENTS]
        ]
        attributes: dict[str, Any] = {
            ATTR_POST_ID: post.id,
            ATTR_GROUP: truncate(", ".join(post.groups), MAX_GROUP_LENGTH)[0],
            **child_fields(post, self.coordinator.children_for(self._school_id)),
            ATTR_AUTHOR: truncate(post.author, MAX_AUTHOR_LENGTH)[0],
            ATTR_POSTED: posted_at(post),
            ATTR_TEXT: text,
            ATTR_TRUNCATED: truncated,
            ATTR_ATTACHMENTS: attachments,
            ATTR_COMMENTS: post.comments_count,
            **self.read_fields(post),
            ATTR_URL: post_url(post),
        }
        attributes.pop(ATTR_NEW)
        if _attributes_size(attributes) > ATTRIBUTES_BYTE_BUDGET:
            _fit_text(attributes, post.text, len(text))
        while attachments and _attributes_size(attributes) > ATTRIBUTES_BYTE_BUDGET:
            attachments.pop()
        return attributes
