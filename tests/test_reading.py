"""Tests for the local read bookkeeping per person (pure module)."""

from __future__ import annotations

from custom_components.socialschools.const import READ_MARKS_MARGIN
from custom_components.socialschools.reading import ReadState

SCHOOL = 7
MARTIJN = "person.ouder_een"
LAURA = "person.ouder_twee"
READERS = [MARTIJN, LAURA]


def test_first_sync_seeds_every_feed_post_for_every_reader() -> None:
    """Installing or upgrading marks what is in the feed as read: no flood of 'new'."""
    state = ReadState()
    assert state.sync(SCHOOL, READERS, [103, 102, 101])
    for post_id in (101, 102, 103):
        assert state.read_by(SCHOOL, post_id) == {MARTIJN, LAURA}
        assert state.unread_by(SCHOOL, post_id, READERS) == []


def test_post_arriving_after_seeding_is_unread_for_everyone() -> None:
    """A later post is unread by both readers, in the configured order."""
    state = ReadState()
    state.sync(SCHOOL, READERS, [102, 101])
    assert not state.sync(SCHOOL, READERS, [103, 102, 101])
    assert state.unread_by(SCHOOL, 103, READERS) == [MARTIJN, LAURA]
    assert state.unread_by(SCHOOL, 103, [LAURA, MARTIJN]) == [LAURA, MARTIJN]


def test_reader_added_later_gets_existing_posts_as_read() -> None:
    """A reader added later is seeded with the current feed; earlier readers keep their state."""
    state = ReadState()
    state.sync(SCHOOL, [MARTIJN], [102, 101])
    state.sync(SCHOOL, [MARTIJN], [103, 102, 101])
    assert state.sync(SCHOOL, READERS, [103, 102, 101])
    assert state.unread_by(SCHOOL, 103, READERS) == [MARTIJN]


def test_removed_reader_is_forgotten_and_seeded_again_when_added_back() -> None:
    """Removing a reader drops their marks; adding them back seeds them anew."""
    state = ReadState()
    state.sync(SCHOOL, READERS, [101])
    assert state.sync(SCHOOL, [MARTIJN], [102, 101])
    assert state.read_by(SCHOOL, 101) == {MARTIJN}
    state.sync(SCHOOL, READERS, [102, 101])
    assert state.unread_by(SCHOOL, 102, READERS) == [MARTIJN]


def test_mark_and_undo() -> None:
    """Marking adds a person, unmarking removes them; repeating changes nothing."""
    state = ReadState()
    state.sync(SCHOOL, READERS, [])
    assert state.mark(SCHOOL, [103], LAURA, True)
    assert not state.mark(SCHOOL, [103], LAURA, True)
    assert state.unread_by(SCHOOL, 103, READERS) == [MARTIJN]
    assert state.mark(SCHOOL, [103], LAURA, False)
    assert not state.mark(SCHOOL, [103], LAURA, False)
    assert state.read_by(SCHOOL, 103) == frozenset()


def test_marks_are_pruned_to_the_feed_plus_a_margin() -> None:
    """Marks of posts that left the feed survive only for the newest READ_MARKS_MARGIN ids."""
    state = ReadState()
    old_ids = list(range(1, READ_MARKS_MARGIN + 11))
    state.sync(SCHOOL, READERS, old_ids)
    assert state.sync(SCHOOL, READERS, [1000])
    kept = [post_id for post_id in old_ids if state.read_by(SCHOOL, post_id)]
    assert kept == old_ids[-READ_MARKS_MARGIN:]
    assert state.read_by(SCHOOL, 1000) == frozenset()


def test_payload_round_trip_and_robust_loading() -> None:
    """The stored record is JSON-friendly and survives a round trip; junk is skipped."""
    state = ReadState()
    state.sync(SCHOOL, READERS, [101])
    state.mark(SCHOOL, [102], LAURA, True)
    payload = state.school_payload(SCHOOL)
    assert payload == {
        "read": {"101": sorted(READERS), "102": [LAURA]},
        "readers": sorted(READERS),
    }
    restored = ReadState()
    restored.load_school(SCHOOL, payload)
    assert restored.school_payload(SCHOOL) == payload
    junk = ReadState()
    junk.load_school(SCHOOL, {"read": {"x": [MARTIJN], "5": "nope", "6": [1, LAURA]}, "readers": 3})
    assert junk.school_payload(SCHOOL) == {"read": {"6": [LAURA]}, "readers": []}


def test_schools_are_independent() -> None:
    """Marks of one school do not affect another school with the same post id."""
    state = ReadState()
    state.sync(SCHOOL, READERS, [101])
    state.sync(8, READERS, [])
    assert state.unread_by(8, 101, READERS) == READERS
    assert state.marked_post_count() == 1
