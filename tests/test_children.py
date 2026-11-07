"""Tests for linking posts to children by group name (pure module)."""

from __future__ import annotations

import pytest

from custom_components.socialschools.children import (
    Child,
    children_for,
    is_schoolwide,
    parse_children,
)

LIVE_LIST = (
    "6A",
    "1/2D",
    "5/6B",
    "5A",
    "4A",
    "1/2C",
    "4B",
    "Neo 4/5/6",
    "3A",
    "1/2B",
    "2/3B",
    "1/2A",
)


def test_parse_one_line_per_child() -> None:
    """Lines become children in order; groups are normalised for comparison."""
    children = parse_children("Noud=6A\nNora = 1/2A,  1/2b \n\n")
    assert children == (
        Child(name="Noud", groups=frozenset({"6a"})),
        Child(name="Nora", groups=frozenset({"1/2a", "1/2b"})),
    )


def test_child_named_twice_gets_all_groups() -> None:
    """Two lines for the same child are merged."""
    assert parse_children("Noud=6A\nNoud=Neo 4/5/6") == (
        Child(name="Noud", groups=frozenset({"6a", "neo 4/5/6"})),
    )


@pytest.mark.parametrize("text", ["Noud", "=6A", "Noud=", "Noud= , ", "Noud=6A\nfoo"])
def test_unusable_lines_are_rejected(text: str) -> None:
    """Every non-empty line needs a name, an equals sign and at least one group."""
    with pytest.raises(ValueError, match="line"):
        parse_children(text)


def test_empty_option_means_no_children() -> None:
    """No text, no children."""
    assert parse_children("") == ()
    assert parse_children("  \n ") == ()


def test_exact_group_match_case_insensitive() -> None:
    """6A matches 6a, but Neo 4/5/6 never counts as 6A and 6 does not match 6A."""
    children = parse_children("Noud=6A\nNora=1/2A")
    assert children_for(["6a"], children) == ("Noud",)
    assert children_for(["Neo 4/5/6"], children) == ()
    assert children_for(["6"], children) == ()
    assert children_for(["Neo  4/5/6", "5A"], parse_children("Noud=6A")) == ()
    assert children_for(LIVE_LIST, children) == ("Noud", "Nora")


def test_neo_group_can_be_configured_explicitly() -> None:
    """A child in Neo 4/5/6 is matched by exactly that name."""
    children = parse_children("Noud=6A, Neo 4/5/6")
    assert children_for(["neo 4/5/6"], children) == ("Noud",)


@pytest.mark.parametrize(
    ("groups", "children", "expected"),
    [
        (["6A"], ("Noud",), False),
        (["Schoolnieuws"], (), True),
        (["6A", "5A"], ("Noud",), False),
        (["6A", "5A", "4A"], ("Noud",), False),
        (["6A", "5A", "4A", "3A"], ("Noud",), True),
        (list(LIVE_LIST), ("Noud",), True),
        ([], (), True),
    ],
)
def test_schoolwide(groups: list[str], children: tuple[str, ...], expected: bool) -> None:
    """School-wide: no group of a child (such as Schoolnieuws) or more than three groups."""
    assert is_schoolwide(groups, children) is expected
