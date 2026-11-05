"""Sensors of the Social Schools integration: the latest post and the weekly post count."""

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
    ATTR_GROUP,
    ATTR_POST_ID,
    ATTR_POSTED,
    ATTR_POSTS,
    ATTR_TEXT,
    ATTR_TRUNCATED,
    ATTRIBUTES_BYTE_BUDGET,
    DEFAULT_POSTS_IN_LIST,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_ATTACHMENTS,
    MAX_AUTHOR_LENGTH,
    MAX_GROUP_LENGTH,
    MAX_POSTS_IN_LIST,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
    MIN_POSTS_IN_LIST,
    OPT_POSTS_IN_LIST,
    POST_COUNT_WINDOW,
)
from .entity import SocialSchoolsEntity, post_summary, posted_at
from .models import Post
from .textutil import truncate

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SocialSchoolsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the latest-post and post-count sensors of every school."""
    coordinator = entry.runtime_data
    async_add_entities(
        sensor
        for school in coordinator.account.schools
        for sensor in (
            SocialSchoolsLatestPostSensor(coordinator, school, "latest_post"),
            SocialSchoolsPostCountSensor(coordinator, school, "post_count"),
        )
    )


def _attributes_size(attributes: dict[str, Any]) -> int:
    """Return the size of ``attributes`` as UTF-8 encoded JSON."""
    return len(json.dumps(attributes, ensure_ascii=False).encode())


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


class SocialSchoolsPostCountSensor(SocialSchoolsEntity, SensorEntity):
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
        posts = [post_summary(post) for post in data.posts[: self._posts_in_list]]
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
