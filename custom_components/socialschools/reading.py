"""Local read bookkeeping per person (HA-free).

Which person has read which post is administration inside Home Assistant only:
nothing of it is ever sent to Social Schools (no read receipts, no statistics
calls, no extra API paths).  Persons are identified by their ``person``
entity ids; this module does not care what the strings are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .const import READ_MARKS_MARGIN

_READ = "read"
_READERS = "readers"


class ReadState:
    """Read marks per school and post, plus the readers that have been seeded."""

    def __init__(self) -> None:
        """Start empty: no marks and no seeded readers."""
        self._read: dict[int, dict[int, set[str]]] = {}
        self._seeded: dict[int, set[str]] = {}

    def load_school(self, school_id: int, record: Mapping[str, Any]) -> None:
        """Load the stored marks of one school; unusable entries are skipped."""
        marks: dict[int, set[str]] = {}
        stored = record.get(_READ)
        if isinstance(stored, Mapping):
            for key, persons in stored.items():
                try:
                    post_id = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(persons, list):
                    marks[post_id] = {person for person in persons if isinstance(person, str)}
        readers = record.get(_READERS)
        self._read[school_id] = marks
        self._seeded[school_id] = (
            {reader for reader in readers if isinstance(reader, str)}
            if isinstance(readers, list)
            else set()
        )

    def school_payload(self, school_id: int) -> dict[str, Any]:
        """Return the JSON-serialisable record of one school (sorted for stable files)."""
        marks = self._read.get(school_id, {})
        return {
            _READ: {str(post_id): sorted(persons) for post_id, persons in sorted(marks.items())},
            _READERS: sorted(self._seeded.get(school_id, set())),
        }

    def read_by(self, school_id: int, post_id: int) -> frozenset[str]:
        """Return the persons that have read a post."""
        return frozenset(self._read.get(school_id, {}).get(post_id, ()))

    def unread_by(self, school_id: int, post_id: int, readers: Sequence[str]) -> list[str]:
        """Return the readers (in the given order) that have not read a post yet."""
        done = self._read.get(school_id, {}).get(post_id, set())
        return [reader for reader in readers if reader not in done]

    def sync(self, school_id: int, readers: Sequence[str], feed_ids: Sequence[int]) -> bool:
        """Seed new readers, forget removed ones and prune; return True when anything changed.

        A reader that was not seeded before (first run after installing or upgrading,
        or a reader added later) gets every post currently in the feed as read, so
        nothing old shows up as new.  A post that arrives afterwards is unread for
        everyone.  Marks of persons that are no longer readers are dropped, and only
        the marks of posts in the feed plus READ_MARKS_MARGIN others are kept.
        """
        marks = self._read.setdefault(school_id, {})
        seeded = self._seeded.setdefault(school_id, set())
        wanted = set(readers)
        changed = False
        for reader in wanted - seeded:
            for post_id in feed_ids:
                marks.setdefault(post_id, set()).add(reader)
            seeded.add(reader)
            changed = True
        for gone in seeded - wanted:
            seeded.discard(gone)
            changed = True
        for post_id in list(marks):
            persons = marks[post_id]
            if persons - wanted:
                persons &= wanted
                changed = True
            if not persons:
                del marks[post_id]
                changed = True
        return self._prune(marks, feed_ids) or changed

    def mark(self, school_id: int, post_ids: Iterable[int], person: str, read: bool) -> bool:
        """Mark posts as read (or unread) by ``person``; return True when anything changed."""
        marks = self._read.setdefault(school_id, {})
        changed = False
        for post_id in post_ids:
            persons = marks.setdefault(post_id, set())
            if read and person not in persons:
                persons.add(person)
                changed = True
            elif not read and person in persons:
                persons.discard(person)
                changed = True
            if not persons:
                del marks[post_id]
        return changed

    def marked_post_count(self) -> int:
        """Return how many posts carry at least one mark, over all schools (for diagnostics)."""
        return sum(len(marks) for marks in self._read.values())

    @staticmethod
    def _prune(marks: dict[int, set[str]], feed_ids: Sequence[int]) -> bool:
        """Keep marks of feed posts plus the READ_MARKS_MARGIN newest other ids."""
        feed = set(feed_ids)
        others = sorted((post_id for post_id in marks if post_id not in feed), reverse=True)
        stale = others[READ_MARKS_MARGIN:]
        for post_id in stale:
            del marks[post_id]
        return bool(stale)
