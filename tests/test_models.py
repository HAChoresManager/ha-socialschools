"""Tests for the data models and JSON parsing (pure module).

All JSON is synthetic.  Dates are fixed: November 2026 is CET (+01:00), June
2026 is CEST (+02:00) and the school timezone is Europe/Amsterdam.  Content
that must never reach exception messages or log lines carries the
``private-`` marker.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.socialschools import models as models_module
from custom_components.socialschools.errors import ApiError
from custom_components.socialschools.models import (
    Account,
    Post,
    School,
    SchoolEvent,
    parse_account,
    parse_datetime,
    parse_event,
    parse_events,
    parse_post,
    parse_posts_page,
)

TZ = ZoneInfo("Europe/Amsterdam")
CET = timezone(timedelta(hours=1))
NOV_4_10_UTC = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
JUN_10_10_UTC = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
TWO_HOURS = timedelta(hours=2)
NO_OFFSET = timedelta(0)


def assert_sanitised(exc: BaseException) -> None:
    """Exception messages never carry API content."""
    assert "private-" not in str(exc)


def event_json(**overrides: Any) -> dict[str, Any]:
    """Return a minimal timed event object with ``overrides`` applied."""
    data: dict[str, Any] = {"id": 1, "title": "Studiedag", "start": "2026-11-04T09:00:00+01:00"}
    data.update(overrides)
    return data


def post_json(**overrides: Any) -> dict[str, Any]:
    """Return a minimal post object with ``overrides`` applied."""
    data: dict[str, Any] = {"id": 1, "created": "2026-11-04T10:00:00Z"}
    data.update(overrides)
    return data


# --- parse_datetime -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected", "offset"),
    [
        pytest.param("2026-11-04T10:00:00Z", NOV_4_10_UTC, NO_OFFSET, id="zulu"),
        pytest.param("2026-11-04T11:00:00+01:00", NOV_4_10_UTC, ONE_HOUR, id="cet-offset"),
        pytest.param("2026-06-10T12:00:00+02:00", JUN_10_10_UTC, TWO_HOURS, id="cest-offset"),
        pytest.param(
            "2026-11-04T10:00:00.123Z",
            NOV_4_10_UTC.replace(microsecond=123000),
            NO_OFFSET,
            id="milliseconds",
        ),
        pytest.param(
            "2026-11-04T10:00:00.123456Z",
            NOV_4_10_UTC.replace(microsecond=123456),
            NO_OFFSET,
            id="microseconds",
        ),
        pytest.param(
            "2026-11-04T10:00:00.1234567Z",
            NOV_4_10_UTC.replace(microsecond=123456),
            NO_OFFSET,
            id="dotnet-seven-fraction-digits",
        ),
        pytest.param(
            "2026-11-04T11:00:00.5+01:00",
            NOV_4_10_UTC.replace(microsecond=500000),
            ONE_HOUR,
            id="fraction-with-offset",
        ),
        pytest.param(
            "  2026-11-04T10:00:00Z  ", NOV_4_10_UTC, NO_OFFSET, id="surrounding-whitespace"
        ),
    ],
)
def test_parse_datetime_aware(value: str, expected: datetime, offset: timedelta) -> None:
    """Aware values keep their own offset; ``assume_tz`` is not applied."""
    parsed = parse_datetime(value, assume_tz=TZ)
    assert parsed == expected
    assert parsed.utcoffset() == offset


@pytest.mark.parametrize(
    ("value", "assume_tz", "expected", "offset"),
    [
        pytest.param("2026-11-04T11:00:00", TZ, NOV_4_10_UTC, ONE_HOUR, id="winter-school-tz"),
        pytest.param("2026-06-10T12:00:00", TZ, JUN_10_10_UTC, TWO_HOURS, id="summer-school-tz"),
        pytest.param("2026-11-04T10:00:00", UTC, NOV_4_10_UTC, NO_OFFSET, id="utc"),
        pytest.param(
            "2026-11-04T11:00:00.250",
            CET,
            NOV_4_10_UTC.replace(microsecond=250000),
            ONE_HOUR,
            id="fixed-offset-with-fraction",
        ),
    ],
)
def test_parse_datetime_naive(
    value: str, assume_tz: timezone | ZoneInfo, expected: datetime, offset: timedelta
) -> None:
    """A value without offset is placed in ``assume_tz`` without shifting the wall clock."""
    parsed = parse_datetime(value, assume_tz=assume_tz)
    assert parsed.tzinfo is assume_tz
    assert parsed.utcoffset() == offset
    assert parsed == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-11-04", id="date-only"),
        pytest.param("2026-W45-3", id="week-date"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("private-garbage", id="garbage"),
        pytest.param("vandaag 10:00", id="words"),
        pytest.param("04-11-2026T10:00:00Z", id="day-first"),
        pytest.param("2026-13-01T00:00:00Z", id="month-13"),
        pytest.param("2026-11-04T25:00:00Z", id="hour-25"),
        pytest.param("1762250400", id="epoch-seconds"),
    ],
)
def test_parse_datetime_rejects_strings_without_date_and_time(value: str) -> None:
    """A bare date or garbage is a ValueError whose message does not repeat the value."""
    with pytest.raises(ValueError, match="timestamp") as exc_info:
        parse_datetime(value, assume_tz=TZ)
    assert_sanitised(exc_info.value)
    if value.strip():
        assert value.strip() not in str(exc_info.value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        1762250400,
        1.5,
        True,
        ["private-list"],
        {"private-key": 1},
        b"2026-11-04T10:00:00Z",
        NOV_4_10_UTC,
        date(2026, 11, 4),
    ],
)
def test_parse_datetime_rejects_non_strings(value: Any) -> None:
    """Only strings are timestamps."""
    with pytest.raises(ValueError, match="not a string") as exc_info:
        parse_datetime(value, assume_tz=TZ)
    assert_sanitised(exc_info.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("9999-12-31T23:59:59Z", id="max-datetime-sentinel"),
        pytest.param("0001-01-01T00:00:00+01:00", id="min-datetime-with-offset"),
        pytest.param("0001-01-01T00:00:00", id="min-datetime-naive"),
        pytest.param("1899-12-31T23:59:59Z", id="just-before-the-window"),
        pytest.param("9001-01-01T00:00:00Z", id="just-after-the-window"),
    ],
)
def test_parse_datetime_rejects_years_outside_the_window(value: str) -> None:
    """Edge-of-range timestamps are a ValueError (never an OverflowError) before any conversion."""
    with pytest.raises(ValueError, match="timestamp") as exc_info:
        parse_datetime(value, assume_tz=TZ)
    assert_sanitised(exc_info.value)
    assert value not in str(exc_info.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1900-01-01T00:00:00Z", id="window-start"),
        pytest.param("1900-01-01T00:00:00", id="window-start-naive"),
        pytest.param("9000-12-31T23:59:59+01:00", id="window-end"),
        pytest.param("9000-12-31T23:59:59", id="window-end-naive"),
    ],
)
def test_parse_datetime_accepts_the_window_edges(value: str) -> None:
    """The first and last year of the window parse and can still be converted and shifted."""
    parsed = parse_datetime(value, assume_tz=TZ)
    assert parsed.year in (1900, 9000)
    assert (parsed.astimezone(UTC) + timedelta(days=1)).tzinfo is UTC


# --- parse_account --------------------------------------------------------------------------


def test_parse_account_parent_roles_at_two_schools() -> None:
    """Each parent role yields one school in API order; names are whitespace-collapsed."""
    account = parse_account(
        {
            "userProfileId": 42,
            "displayName": "  Ouder   Test ",
            "roles": [
                {
                    "id": 11,
                    "type": 1,
                    "school": {
                        "id": 100,
                        "displayName": "Voorbeeldschool",
                        "name": "voorbeeldschool",
                    },
                },
                {"id": 12, "type": 1, "school": {"id": 200, "name": " Tweede  Voorbeeldschool "}},
            ],
        }
    )
    assert account == Account(
        user_profile_id=42,
        display_name="Ouder Test",
        schools=(
            School(id=100, name="Voorbeeldschool", role_id=11),
            School(id=200, name="Tweede Voorbeeldschool", role_id=12),
        ),
    )


def test_parse_account_duplicate_school_is_listed_once() -> None:
    """Two parent roles at the same school (two children) give one school; the first role wins."""
    account = parse_account(
        {
            "userProfileId": 42,
            "roles": [
                {"id": 11, "type": 1, "school": {"id": 100, "displayName": "Voorbeeldschool"}},
                {
                    "id": 13,
                    "type": 1,
                    "school": {"id": 200, "displayName": "Tweede Voorbeeldschool"},
                },
                {"id": 12, "type": 1, "school": {"id": 100, "displayName": "Voorbeeldschool (2)"}},
            ],
        }
    )
    assert account.schools == (
        School(id=100, name="Voorbeeldschool", role_id=11),
        School(id=200, name="Tweede Voorbeeldschool", role_id=13),
    )


@pytest.mark.parametrize(
    "role",
    [
        pytest.param(
            {"id": 2, "type": 2, "school": {"id": 300, "displayName": "Werk"}}, id="employee"
        ),
        pytest.param({"id": 2, "type": "1", "school": {"id": 300}}, id="string-type"),
        pytest.param({"id": 2, "type": True, "school": {"id": 300}}, id="bool-type"),
        pytest.param({"id": 2, "school": {"id": 300}}, id="missing-type"),
        pytest.param(
            {"id": 2, "type": 1, "school": {"id": "300", "displayName": "X"}}, id="string-school-id"
        ),
        pytest.param({"id": 2, "type": 1, "school": {"id": None}}, id="null-school-id"),
        pytest.param({"id": 2, "type": 1, "school": {"id": True}}, id="bool-school-id"),
        pytest.param({"id": 2, "type": 1, "school": {"id": 3.5}}, id="float-school-id"),
        pytest.param(
            {"id": 2, "type": 1, "school": {"displayName": "Zonder id"}}, id="no-school-id"
        ),
        pytest.param({"id": 2, "type": 1, "school": None}, id="null-school"),
        pytest.param({"id": 2, "type": 1, "school": "private-string"}, id="string-school"),
        pytest.param({"id": 2, "type": 1}, id="no-school"),
        pytest.param("private-role", id="role-not-an-object"),
        pytest.param(None, id="null-role"),
    ],
)
def test_parse_account_ignores_unusable_roles(role: Any) -> None:
    """Non-parent roles and roles without a usable school are skipped, the rest is kept."""
    account = parse_account(
        {
            "userProfileId": 42,
            "roles": [role, {"id": 11, "type": 1, "school": {"id": 100, "displayName": "Echt"}}],
        }
    )
    assert account.schools == (School(id=100, name="Echt", role_id=11),)


@pytest.mark.parametrize(
    ("school", "expected_name"),
    [
        pytest.param(
            {"id": 7, "displayName": "Weergave", "name": "naam"}, "Weergave", id="displayName"
        ),
        pytest.param({"id": 7, "displayName": "", "name": "naam"}, "naam", id="empty-displayName"),
        pytest.param(
            {"id": 7, "displayName": "   ", "name": "naam"}, "naam", id="blank-displayName"
        ),
        pytest.param(
            {"id": 7, "displayName": None, "name": " de  naam "}, "de naam", id="null-displayName"
        ),
        pytest.param({"id": 7, "name": "naam"}, "naam", id="name-only"),
        pytest.param({"id": 7}, "7", id="id-only"),
        pytest.param({"id": 7, "displayName": 5, "name": ["x"]}, "7", id="non-string-names"),
    ],
)
def test_parse_account_school_name_fallbacks(school: dict[str, Any], expected_name: str) -> None:
    """The school name is displayName, else name, else the id as text."""
    account = parse_account({"userProfileId": 1, "roles": [{"id": 3, "type": 1, "school": school}]})
    assert account.schools == (School(id=7, name=expected_name, role_id=3),)


def test_parse_account_optional_fields() -> None:
    """A missing display name is empty, a role without id gets role_id 0, no roles is fine."""
    account = parse_account({"userProfileId": 1, "roles": []})
    assert account == Account(user_profile_id=1, display_name="", schools=())
    account = parse_account(
        {"userProfileId": 1, "displayName": None, "roles": [{"type": 1, "school": {"id": 9}}]}
    )
    assert account.display_name == ""
    assert account.schools == (School(id=9, name="9", role_id=0),)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("private-string", id="string"),
        pytest.param(5, id="number"),
        pytest.param({}, id="empty-object"),
        pytest.param({"roles": []}, id="missing-userProfileId"),
        pytest.param({"userProfileId": None, "roles": []}, id="null-userProfileId"),
        pytest.param({"userProfileId": "42", "roles": []}, id="string-userProfileId"),
        pytest.param({"userProfileId": True, "roles": []}, id="bool-userProfileId"),
        pytest.param({"userProfileId": 4.2, "roles": []}, id="float-userProfileId"),
        pytest.param({"userProfileId": 42, "displayName": "private-name"}, id="missing-roles"),
        pytest.param({"userProfileId": 42, "roles": None}, id="null-roles"),
        pytest.param({"userProfileId": 42, "roles": {"id": 1}}, id="roles-object"),
        pytest.param({"userProfileId": 42, "roles": "private-roles"}, id="roles-string"),
    ],
)
def test_parse_account_rejects_unusable_profiles(data: Any) -> None:
    """Without an int userProfileId and a roles list the account is an ApiError without content."""
    with pytest.raises(ApiError, match="account") as exc_info:
        parse_account(data)
    assert exc_info.value.status is None
    assert_sanitised(exc_info.value)


# --- parse_post -----------------------------------------------------------------------------


def test_parse_post_full() -> None:
    """A full feed item maps every field; attachments carry file names only."""
    post = parse_post(
        {
            "id": 1001,
            "title": "  Schoolreisje \n groep 3  ",
            "body": "<p>Beste ouders,</p><p>Neem&nbsp;mee:</p><ul><li>laarzen</li><li>regenjas</li></ul>",
            "created": "2026-11-04T10:00:00Z",
            "communities": [
                {"id": 1, "name": " Groep  3 "},
                {"id": 2, "name": "Groep 3"},
                {"id": 3, "name": "Hele school"},
                {"id": 4, "name": ""},
                {"id": 5},
                "private-string",
                None,
            ],
            "author": {"id": 5, "displayName": "  Juf   Voorbeeld "},
            "commentsCount": 3,
            "media": [
                {
                    "id": 1,
                    "fileName": "foto.jpg",
                    "fullPath": "https://cdn.example.test/private-path/1",
                },
                {"id": 2, "fullPath": "https://cdn.example.test/private-path/2"},
                {"id": 3, "fileName": "  brief .pdf "},
                {
                    "id": 4,
                    "fileName": "foto.jpg",
                    "fullPath": "https://cdn.example.test/private-path/4",
                },
                {"id": 5, "fileName": ""},
                None,
                "private-string",
            ],
        }
    )
    assert post == Post(
        id=1001,
        title="Schoolreisje groep 3",
        text="Beste ouders,\nNeem mee:\n- laarzen\n- regenjas",
        created=NOV_4_10_UTC,
        groups=("Groep 3", "Hele school"),
        author="Juf Voorbeeld",
        comments_count=3,
        attachments=("foto.jpg", "brief .pdf", "foto.jpg"),
    )
    assert post.created.tzinfo is UTC
    assert "private-" not in repr(post)


def test_parse_post_minimal() -> None:
    """Only id and created are required; everything else has an empty default."""
    post = parse_post({"id": 7, "created": "2026-11-04T10:00:00Z"})
    assert post == Post(
        id=7,
        title="",
        text="",
        created=NOV_4_10_UTC,
        groups=(),
        author="",
        comments_count=0,
        attachments=(),
    )


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        pytest.param("2026-11-04T10:00:00Z", NOV_4_10_UTC, id="zulu"),
        pytest.param("2026-11-04T11:00:00+01:00", NOV_4_10_UTC, id="cet-offset-normalised"),
        pytest.param("2026-11-04T12:00:00+02:00", NOV_4_10_UTC, id="other-offset-normalised"),
        pytest.param("2026-11-04T11:00:00", NOV_4_10_UTC, id="naive-is-school-time-winter"),
        pytest.param("2026-06-10T12:00:00", JUN_10_10_UTC, id="naive-is-school-time-summer"),
        pytest.param(
            "2026-11-04T10:00:00.1234567Z", NOV_4_10_UTC.replace(microsecond=123456), id="fraction"
        ),
    ],
)
def test_parse_post_created_is_utc(created: str, expected: datetime) -> None:
    """``created`` is always an aware UTC datetime; naive values are Europe/Amsterdam."""
    post = parse_post(post_json(created=created))
    assert post.created == expected
    assert post.created.tzinfo is UTC
    assert post.created.utcoffset() == NO_OFFSET


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"title": None}, {"title": ""}, id="null-title"),
        pytest.param({"title": 5}, {"title": ""}, id="numeric-title"),
        pytest.param({"body": None}, {"text": ""}, id="null-body"),
        pytest.param({"body": ["private-list"]}, {"text": ""}, id="list-body"),
        pytest.param(
            {"body": "Regel een\n\n\n\nRegel   twee"},
            {"text": "Regel een\n\nRegel twee"},
            id="plain-body",
        ),
        pytest.param({"author": None}, {"author": ""}, id="null-author"),
        pytest.param({"author": "private-string"}, {"author": ""}, id="string-author"),
        pytest.param({"author": {"id": 1}}, {"author": ""}, id="author-without-name"),
        pytest.param({"commentsCount": None}, {"comments_count": 0}, id="null-comments"),
        pytest.param({"commentsCount": "3"}, {"comments_count": 0}, id="string-comments"),
        pytest.param({"commentsCount": True}, {"comments_count": 0}, id="bool-comments"),
        pytest.param({"communities": None}, {"groups": ()}, id="null-communities"),
        pytest.param({"communities": {"name": "x"}}, {"groups": ()}, id="communities-object"),
        pytest.param({"media": None}, {"attachments": ()}, id="null-media"),
        pytest.param({"media": "private-string"}, {"attachments": ()}, id="media-string"),
    ],
)
def test_parse_post_tolerates_optional_fields(
    overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Optional fields with an unexpected type fall back to their empty default."""
    post = parse_post(post_json(**overrides))
    for name, value in expected.items():
        assert getattr(post, name) == value


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"created": "2026-11-04T10:00:00Z"}, id="missing-id"),
        pytest.param({"id": None, "created": "2026-11-04T10:00:00Z"}, id="null-id"),
        pytest.param({"id": "private-id", "created": "2026-11-04T10:00:00Z"}, id="string-id"),
        pytest.param({"id": True, "created": "2026-11-04T10:00:00Z"}, id="bool-id"),
        pytest.param({"id": 1.0, "created": "2026-11-04T10:00:00Z"}, id="float-id"),
        pytest.param({"id": 1}, id="missing-created"),
        pytest.param({"id": 1, "created": None}, id="null-created"),
        pytest.param({"id": 1, "created": "private-garbage"}, id="garbage-created"),
        pytest.param({"id": 1, "created": "2026-11-04"}, id="date-only-created"),
        pytest.param({"id": 1, "created": 1762250400}, id="numeric-created"),
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("private-string", id="string"),
    ],
)
def test_parse_post_rejects_unusable_items(data: Any) -> None:
    """Without an int id and a parsable created timestamp the item is a ValueError."""
    with pytest.raises(ValueError, match="post") as exc_info:
        parse_post(data)
    assert_sanitised(exc_info.value)


# --- parse_posts_page -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("private-string", id="string"),
        pytest.param(5, id="number"),
        pytest.param({}, id="no-values"),
        pytest.param({"values": None}, id="null-values"),
        pytest.param({"values": "private-string"}, id="string-values"),
        pytest.param({"values": {"id": 1}}, id="values-object"),
        pytest.param({"values": []}, id="empty-values"),
    ],
)
def test_parse_posts_page_without_values_is_empty(data: Any) -> None:
    """Anything but an object with a values list yields no posts."""
    assert parse_posts_page(data) == []


def test_parse_posts_page_skips_invalid_items(caplog: pytest.LogCaptureFixture) -> None:
    """Unusable items are skipped with a debug line that names the index, not the content."""
    caplog.set_level(logging.DEBUG)
    posts = parse_posts_page(
        {
            "values": [
                {"id": "private-id", "created": "2026-11-04T10:00:00Z"},
                {"id": 2, "created": "private-created"},
                "private-string",
                {"id": 4, "created": "2026-11-04T10:00:00Z", "title": "Echt"},
                {"id": 5},
            ],
            "totalCount": 5,
        }
    )
    assert [post.id for post in posts] == [4]
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == parse_posts_page.__module__
    ]
    assert messages == [
        "posts page: skipping item 0 (post without a valid id)",
        "posts page: skipping item 1 (post without a valid created timestamp)",
        "posts page: skipping item 2 (post is not an object)",
        "posts page: skipping item 4 (post without a valid created timestamp)",
    ]
    assert "private-" not in "\n".join(messages)


def test_parse_posts_page_skips_out_of_range_timestamps(caplog: pytest.LogCaptureFixture) -> None:
    """A post dated at the edge of the datetime range is skipped; the page is not lost."""
    caplog.set_level(logging.DEBUG)
    posts = parse_posts_page(
        {
            "values": [
                {"id": 1, "created": "0001-01-01T00:00:00+01:00"},
                {"id": 4, "created": "2026-11-04T10:00:00Z"},
                {"id": 2, "created": "9999-12-31T23:59:59Z"},
            ]
        }
    )
    assert [post.id for post in posts] == [4]
    messages = [
        record.getMessage() for record in caplog.records if record.name == models_module.__name__
    ]
    assert messages == [
        "posts page: skipping item 0 (post without a valid created timestamp)",
        "posts page: skipping item 2 (post without a valid created timestamp)",
    ]


def test_parse_posts_page_sorted_newest_first_then_id_desc() -> None:
    """Posts are ordered by created descending; equal instants by id descending."""
    posts = parse_posts_page(
        {
            "values": [
                {"id": 1, "created": "2026-11-04T09:00:00Z"},
                {"id": 2, "created": "2026-11-04T10:00:00Z"},
                {"id": 9, "created": "2026-11-04T08:00:00Z"},
                {"id": 3, "created": "2026-11-04T10:00:00Z"},
                {"id": 4, "created": "2026-11-04T11:00:00+01:00"},
                {"id": 8, "created": "2026-11-04T11:00:00"},
            ]
        }
    )
    assert [post.id for post in posts] == [8, 4, 3, 2, 1, 9]


# --- parse_event ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "2026-11-05T00:00:00+01:00",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="end-next-day-midnight-is-exclusive",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "2026-11-04T00:00:00+01:00",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="end-same-day-midnight",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "2026-11-04T23:59:59+01:00",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="end-23-59-59-same-day",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "2026-11-06T23:59:59+01:00",
            date(2026, 11, 4),
            date(2026, 11, 7),
            id="multi-day-inclusive-end",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "2026-11-07T00:00:00+01:00",
            date(2026, 11, 4),
            date(2026, 11, 7),
            id="multi-day-exclusive-end",
        ),
        pytest.param(
            "2026-11-04T23:00:00Z",
            "2026-11-05T23:00:00Z",
            date(2026, 11, 5),
            date(2026, 11, 6),
            id="utc-crossing-local-midnight",
        ),
        pytest.param(
            "2026-11-03T23:00:00Z",
            "2026-11-04T22:59:59Z",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="utc-inclusive-end-crossing-local-midnight",
        ),
        pytest.param(
            "2026-06-10T22:00:00Z",
            "2026-06-11T22:00:00Z",
            date(2026, 6, 11),
            date(2026, 6, 12),
            id="utc-in-summer-time",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "2026-11-04T09:00:00+01:00",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="start-equals-end",
        ),
        pytest.param(
            "2026-11-06T00:00:00+01:00",
            "2026-11-04T00:00:00+01:00",
            date(2026, 11, 6),
            date(2026, 11, 7),
            id="end-before-start",
        ),
        pytest.param(
            "2026-11-04T00:00:00",
            "2026-11-05T00:00:00",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="naive-local",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            None,
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="missing-end",
        ),
        pytest.param(
            "2026-11-04T00:00:00+01:00",
            "private-garbage",
            date(2026, 11, 4),
            date(2026, 11, 5),
            id="invalid-end",
        ),
    ],
)
def test_parse_event_all_day(
    start: str, end: str | None, expected_start: date, expected_end: date
) -> None:
    """All-day events become local dates with an exclusive end after the start."""
    data = event_json(allDay=True, start=start)
    if end is not None:
        data["end"] = end
    event = parse_event(data, tz=TZ)
    assert event.all_day is True
    assert event.start == expected_start
    assert event.end == expected_end
    assert type(event.start) is date
    assert type(event.end) is date
    assert event.end > event.start


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end", "offset"),
    [
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "2026-11-04T10:30:00+01:00",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 30, tzinfo=TZ),
            ONE_HOUR,
            id="cet",
        ),
        pytest.param(
            "2026-11-04T08:00:00Z",
            "2026-11-04T09:30:00Z",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 30, tzinfo=TZ),
            ONE_HOUR,
            id="utc-to-cet",
        ),
        pytest.param(
            "2026-06-10T09:00:00+02:00",
            "2026-06-10T10:00:00+02:00",
            datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
            datetime(2026, 6, 10, 10, 0, tzinfo=TZ),
            TWO_HOURS,
            id="cest",
        ),
        pytest.param(
            "2026-06-10T07:00:00Z",
            "2026-06-10T08:00:00Z",
            datetime(2026, 6, 10, 9, 0, tzinfo=TZ),
            datetime(2026, 6, 10, 10, 0, tzinfo=TZ),
            TWO_HOURS,
            id="utc-to-cest",
        ),
        pytest.param(
            "2026-11-04T09:00:00",
            "2026-11-04T10:00:00",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="naive-local",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            None,
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="missing-end-lasts-one-hour",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "2026-11-04T08:00:00+01:00",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="end-before-start-lasts-one-hour",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "2026-11-04T09:00:00+01:00",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="end-equals-start-lasts-one-hour",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "private-garbage",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="invalid-end-lasts-one-hour",
        ),
        pytest.param(
            "2026-11-04T09:00:00+01:00",
            "2026-11-04",
            datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
            datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
            ONE_HOUR,
            id="date-only-end-lasts-one-hour",
        ),
    ],
)
def test_parse_event_timed(
    start: str,
    end: str | None,
    expected_start: datetime,
    expected_end: datetime,
    offset: timedelta,
) -> None:
    """Timed events are aware datetimes in the school timezone; a bad end means one hour."""
    data = event_json(start=start)
    if end is not None:
        data["end"] = end
    event = parse_event(data, tz=TZ)
    assert event.all_day is False
    assert isinstance(event.start, datetime)
    assert isinstance(event.end, datetime)
    assert event.start.tzinfo is TZ
    assert event.end.tzinfo is TZ
    assert event.start == expected_start
    assert event.end == expected_end
    assert event.start.utcoffset() == offset
    assert event.end.utcoffset() == offset


def test_parse_event_default_duration_spans_the_dst_switch() -> None:
    """One hour after 02:30 CEST on 25 October 2026 is 02:30 CET: the hour is counted in instants."""
    event = parse_event(event_json(start="2026-10-25T02:30:00+02:00"), tz=TZ)
    assert isinstance(event.start, datetime)
    assert isinstance(event.end, datetime)
    assert event.start.utcoffset() == TWO_HOURS
    assert event.end.utcoffset() == ONE_HOUR
    assert event.end.astimezone(UTC) - event.start.astimezone(UTC) == ONE_HOUR


def test_parse_event_fields() -> None:
    """Title and location are collapsed, the description is plain text, groups are unique."""
    event = parse_event(
        {
            "id": 55,
            "title": "  Ouder\navond ",
            "description": "<p>Neem&nbsp;mee:</p><ul><li>pen</li><li>papier</li></ul>",
            "location": "  Gymzaal \n  A ",
            "start": "2026-11-04T19:00:00+01:00",
            "end": "2026-11-04T20:30:00+01:00",
            "allDay": False,
            "communities": [
                {"id": 1, "name": " Groep  3 "},
                {"id": 2, "name": "Groep 3"},
                {"id": 3, "name": "Hele school"},
                {"id": 4, "name": ""},
                {"id": 5},
                None,
                "private-string",
            ],
        },
        tz=TZ,
    )
    assert event == SchoolEvent(
        id=55,
        title="Ouder avond",
        description="Neem mee:\n- pen\n- papier",
        start=datetime(2026, 11, 4, 19, 0, tzinfo=TZ),
        end=datetime(2026, 11, 4, 20, 30, tzinfo=TZ),
        all_day=False,
        location="Gymzaal A",
        groups=("Groep 3", "Hele school"),
    )


def test_parse_event_minimal() -> None:
    """Only id and start are required."""
    event = parse_event({"id": 1, "start": "2026-11-04T09:00:00+01:00"}, tz=TZ)
    assert event == SchoolEvent(
        id=1,
        title="",
        description="",
        start=datetime(2026, 11, 4, 9, 0, tzinfo=TZ),
        end=datetime(2026, 11, 4, 10, 0, tzinfo=TZ),
        all_day=False,
        location="",
        groups=(),
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"title": None}, {"title": ""}, id="null-title"),
        pytest.param({"description": None}, {"description": ""}, id="null-description"),
        pytest.param({"description": 7}, {"description": ""}, id="numeric-description"),
        pytest.param(
            {"description": "Regel een\n\n\n\nRegel   twee"},
            {"description": "Regel een\n\nRegel twee"},
            id="plain-description",
        ),
        pytest.param({"location": None}, {"location": ""}, id="null-location"),
        pytest.param({"location": ["private-list"]}, {"location": ""}, id="list-location"),
        pytest.param({"communities": "private-string"}, {"groups": ()}, id="string-communities"),
        pytest.param({"allDay": None}, {"all_day": False}, id="null-allDay"),
        pytest.param({"allDay": 0}, {"all_day": False}, id="zero-allDay"),
        pytest.param({"allDay": True}, {"all_day": True}, id="true-allDay"),
    ],
)
def test_parse_event_tolerates_optional_fields(
    overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Optional fields with an unexpected type fall back to their empty default."""
    event = parse_event(event_json(**overrides), tz=TZ)
    for name, value in expected.items():
        assert getattr(event, name) == value


def test_parse_event_uses_the_given_timezone() -> None:
    """``tz`` decides both the assumed zone of naive values and the zone of the result."""
    naive = parse_event(event_json(start="2026-11-04T09:00:00"), tz=UTC)
    assert naive.start == datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    assert isinstance(naive.start, datetime)
    assert naive.start.tzinfo is UTC
    aware = parse_event(event_json(start="2026-11-04T10:00:00+01:00"), tz=UTC)
    assert aware.start == datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    assert isinstance(aware.start, datetime)
    assert aware.start.tzinfo is UTC
    all_day = parse_event(event_json(start="2026-11-04T23:30:00+01:00", allDay=True), tz=UTC)
    assert all_day.start == date(2026, 11, 4)
    assert all_day.end == date(2026, 11, 5)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"start": "2026-11-04T09:00:00+01:00"}, id="missing-id"),
        pytest.param({"id": None, "start": "2026-11-04T09:00:00+01:00"}, id="null-id"),
        pytest.param({"id": "private-id", "start": "2026-11-04T09:00:00+01:00"}, id="string-id"),
        pytest.param({"id": True, "start": "2026-11-04T09:00:00+01:00"}, id="bool-id"),
        pytest.param({"id": 1}, id="missing-start"),
        pytest.param({"id": 1, "start": None}, id="null-start"),
        pytest.param({"id": 1, "start": "private-garbage"}, id="garbage-start"),
        pytest.param({"id": 1, "start": "2026-11-04"}, id="date-only-start"),
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("private-string", id="string"),
    ],
)
def test_parse_event_rejects_unusable_items(data: Any) -> None:
    """Without an int id and a parsable start the event is a ValueError without content."""
    with pytest.raises(ValueError, match="event") as exc_info:
        parse_event(data, tz=TZ)
    assert_sanitised(exc_info.value)


# --- parse_events ---------------------------------------------------------------------------

EVENT_ITEMS: list[Any] = [
    {"id": 1, "start": "2026-11-04T09:00:00+01:00"},
    {
        "id": 2,
        "start": "2026-11-04T00:00:00+01:00",
        "allDay": True,
        "end": "2026-11-05T00:00:00+01:00",
    },
    {"id": 3, "start": "2026-11-03T23:30:00+01:00"},
    {"id": 5, "start": "2026-11-04T00:00:00+01:00", "allDay": True},
    {"id": 4, "start": "2026-11-04T00:00:00+01:00", "allDay": True},
    {"id": 6, "start": "2026-11-04T00:00:00+01:00"},
    {"id": 7, "start": "2026-11-03T23:00:00Z", "allDay": True},
]
EVENT_ORDER = [3, 2, 4, 5, 6, 7, 1]


def test_parse_events_sorted_by_start_then_id() -> None:
    """All-day events sort at local midnight, before the timed events of that day; ties by id."""
    assert [event.id for event in parse_events(EVENT_ITEMS, tz=TZ)] == EVENT_ORDER


def test_parse_events_accepts_values_wrapper() -> None:
    """A ``{"values": [...]}`` object is parsed like a bare list."""
    events = parse_events({"values": EVENT_ITEMS, "totalCount": len(EVENT_ITEMS)}, tz=TZ)
    assert [event.id for event in events] == EVENT_ORDER


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(None, id="null"),
        pytest.param("private-string", id="string"),
        pytest.param(5, id="number"),
        pytest.param({}, id="no-values"),
        pytest.param({"values": None}, id="null-values"),
        pytest.param({"values": "private-string"}, id="string-values"),
        pytest.param({"values": {"id": 1}}, id="values-object"),
        pytest.param([], id="empty-list"),
        pytest.param({"values": []}, id="empty-values"),
    ],
)
def test_parse_events_without_items_is_empty(data: Any) -> None:
    """Anything but a list (bare or under values) yields no events."""
    assert parse_events(data, tz=TZ) == []


def test_parse_events_skips_invalid_entries(caplog: pytest.LogCaptureFixture) -> None:
    """Unusable entries are skipped with a debug line that names the index, not the content."""
    caplog.set_level(logging.DEBUG)
    events = parse_events(
        [
            {"id": 1, "start": "2026-11-04T09:00:00+01:00"},
            None,
            "private-string",
            {"id": "private-id", "start": "2026-11-04T09:00:00+01:00"},
            {"id": 2},
            {"id": 3, "start": "private-garbage"},
            {"id": 4, "start": "2026-11-04T08:00:00+01:00", "title": "private-title"},
        ],
        tz=TZ,
    )
    assert [event.id for event in events] == [4, 1]
    messages = [
        record.getMessage() for record in caplog.records if record.name == parse_events.__module__
    ]
    assert messages == [
        "events: skipping item 1 (event is not an object)",
        "events: skipping item 2 (event is not an object)",
        "events: skipping item 3 (event without a valid id)",
        "events: skipping item 4 (event without a valid start)",
        "events: skipping item 5 (event without a valid start)",
    ]
    assert "private-" not in "\n".join(messages)


def test_parse_events_tolerates_out_of_range_timestamps(caplog: pytest.LogCaptureFixture) -> None:
    """An end at the datetime edge means a one-hour event; such a start skips only that event."""
    caplog.set_level(logging.DEBUG)
    events = parse_events(
        [
            {"id": 1, "start": "2026-11-04T09:00:00+01:00", "end": "9999-12-31T23:59:59Z"},
            {"id": 2, "start": "9999-12-31T23:59:59Z"},
            {"id": 3, "start": "0001-01-01T00:00:00+01:00", "allDay": True},
            {
                "id": 4,
                "start": "2026-11-04T00:00:00+01:00",
                "end": "9999-12-31T23:59:59Z",
                "allDay": True,
            },
        ],
        tz=TZ,
    )
    assert [(event.id, event.start, event.end) for event in events] == [
        (4, date(2026, 11, 4), date(2026, 11, 5)),
        (1, datetime(2026, 11, 4, 9, 0, tzinfo=TZ), datetime(2026, 11, 4, 10, 0, tzinfo=TZ)),
    ]
    messages = [
        record.getMessage() for record in caplog.records if record.name == models_module.__name__
    ]
    assert messages == [
        "events: skipping item 1 (event without a valid start)",
        "events: skipping item 2 (event without a valid start)",
    ]


@pytest.mark.parametrize(
    ("parsed", "parse", "parse_page", "message"),
    [
        pytest.param(
            datetime(1, 1, 1, 0, 0, tzinfo=CET),
            lambda: parse_post(post_json()),
            lambda: parse_posts_page({"values": [post_json()]}),
            "post without a valid created timestamp",
            id="post-created-to-utc",
        ),
        pytest.param(
            datetime(1, 1, 1, 0, 0, tzinfo=CET),
            lambda: parse_event(event_json(), tz=UTC),
            lambda: parse_events([event_json()], tz=UTC),
            "event without a valid start",
            id="event-start-to-tz",
        ),
        pytest.param(
            datetime(9999, 12, 31, 12, 0, tzinfo=UTC),
            lambda: parse_event(event_json(allDay=True), tz=UTC),
            lambda: parse_events([event_json(allDay=True)], tz=UTC),
            "event with a start or end outside the supported range",
            id="all-day-end-plus-one-day",
        ),
        pytest.param(
            datetime(9999, 12, 31, 23, 30, tzinfo=UTC),
            lambda: parse_event(event_json(), tz=UTC),
            lambda: parse_events([event_json()], tz=UTC),
            "event with a start or end outside the supported range",
            id="timed-end-plus-one-hour",
        ),
    ],
)
def test_overflowing_datetime_arithmetic_is_a_value_error(
    monkeypatch: pytest.MonkeyPatch, parsed: datetime, parse: Any, parse_page: Any, message: str
) -> None:
    """Even a datetime at the range edge (bypassing the year window) ends as a ValueError.

    ``parse_datetime`` is replaced so that the conversions and the one-day/one-hour arithmetic in
    ``parse_post``/``parse_event`` really overflow; the page parser then skips the item.
    """
    monkeypatch.setattr(models_module, "parse_datetime", lambda value, *, assume_tz: parsed)
    with pytest.raises(ValueError, match=message) as exc_info:
        parse()
    assert not isinstance(exc_info.value, OverflowError)
    assert exc_info.value.__cause__ is None
    assert parse_page() == []


# --- dataclasses ----------------------------------------------------------------------------


def test_models_are_frozen() -> None:
    """The HA layer can rely on the models never changing under its feet."""
    school = School(id=1, name="x", role_id=2)
    with pytest.raises(FrozenInstanceError):
        school.name = "y"  # type: ignore[misc]
    account = Account(user_profile_id=1, display_name="x", schools=(school,))
    with pytest.raises(FrozenInstanceError):
        account.schools = ()  # type: ignore[misc]
    post = parse_post(post_json())
    with pytest.raises(FrozenInstanceError):
        post.title = "y"  # type: ignore[misc]
    event = parse_event(event_json(), tz=TZ)
    with pytest.raises(FrozenInstanceError):
        event.title = "y"  # type: ignore[misc]
