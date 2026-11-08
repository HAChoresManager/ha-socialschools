"""Which child a post is about (HA-free).

The children and their groups come from the account (the parent role's
``connections[].groups``, see ``models.parse_connected_children``) and from the
``kinderen`` option, one line per child, ``Naam=groep, groep``, which completes
and overrides the account (``merge_children``).  A post belongs to a child when
one of the post's group names equals one of the child's groups exactly
(whitespace collapsed, case ignored), so ``Neo 4/5/6`` never counts as ``6A``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .const import SCHOOLWIDE_GROUP_THRESHOLD


@dataclass(frozen=True)
class Child:
    """A child with the normalised names of its groups."""

    name: str
    groups: frozenset[str]


def normalise_group(name: str) -> str:
    """Return a group name for comparison: whitespace collapsed, case folded."""
    return " ".join(name.split()).casefold()


def parse_children(text: str) -> tuple[Child, ...]:
    """Parse the option text; raises ValueError naming the first unusable line number.

    Empty lines are ignored.  A child named twice gets the groups of both lines.
    """
    merged: dict[str, set[str]] = {}
    order: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        name, separator, groups_text = line.partition("=")
        name = " ".join(name.split())
        groups = {normalise_group(group) for group in groups_text.split(",") if group.strip()}
        if not separator or not name or not groups:
            raise ValueError(f"line {number} is not 'Naam=groep, groep'")
        if name not in merged:
            merged[name] = set()
            order.append(name)
        merged[name] |= groups
    return tuple(Child(name=name, groups=frozenset(merged[name])) for name in order)


def child_from_account(name: str, groups: Iterable[str]) -> Child | None:
    """Return the Child for a child of the account, or None without a name or a group."""
    normalised = frozenset(normalise_group(group) for group in groups if group.strip())
    name = " ".join(name.split())
    return Child(name=name, groups=normalised) if name and normalised else None


def merge_children(account: Sequence[Child], option: Sequence[Child]) -> tuple[Child, ...]:
    """Return the children of the account with the ``kinderen`` option applied.

    The option completes and overrides the account: a line for a child of the account
    (the same name, compared without case and extra spaces) replaces that child's groups
    and spelling; any other line adds a child.  Account children keep their order and
    added children follow in option order.
    """
    merged = list(account)
    for child in option:
        key = normalise_group(child.name)
        index = next(
            (i for i, existing in enumerate(merged) if normalise_group(existing.name) == key), None
        )
        if index is None:
            merged.append(child)
        else:
            merged[index] = child
    return tuple(merged)


def children_for(groups: Sequence[str], children: Sequence[Child]) -> tuple[str, ...]:
    """Return the names of the children (in option order) that are in one of ``groups``."""
    post_groups = {normalise_group(group) for group in groups}
    return tuple(child.name for child in children if child.groups & post_groups)


def is_schoolwide(groups: Sequence[str], children: Sequence[str]) -> bool:
    """Return True for a post meant for (much of) the school.

    That is a post in more than SCHOOLWIDE_GROUP_THRESHOLD groups, or one whose
    groups belong to none of the children (such as ``Schoolnieuws``).
    """
    return len(groups) > SCHOOLWIDE_GROUP_THRESHOLD or not children
