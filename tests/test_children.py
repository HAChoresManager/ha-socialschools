"""Tests for linking posts to children by group name (pure module)."""

from __future__ import annotations

import pytest

from custom_components.socialschools.children import (
    Child,
    child_from_account,
    children_for,
    is_schoolwide,
    merge_children,
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


# --- children of the account and the option ---------------------------------------------------


def test_child_from_account_normalises_its_groups() -> None:
    """Group names are compared like those of the option; the name is whitespace-collapsed."""
    child = child_from_account("  Kind   Een ", ["6A", " 6a ", "Neo  4/5/6"])
    assert child == Child(name="Kind Een", groups=frozenset({"6a", "neo 4/5/6"}))
    assert children_for(["6A"], (child,)) == ("Kind Een",)


@pytest.mark.parametrize(
    ("name", "groups"),
    [("Kind", []), ("Kind", ["", "  "]), ("", ["6A"]), ("   ", ["6A"])],
)
def test_child_from_account_needs_a_name_and_a_group(name: str, groups: list[str]) -> None:
    """A child without a group can never match a post, and one without a name cannot be shown."""
    assert child_from_account(name, groups) is None


ACCOUNT = (
    Child(name="Kind Een", groups=frozenset({"6a"})),
    Child(name="Kind Twee", groups=frozenset({"1/2a"})),
)


def test_merge_without_option_keeps_the_account() -> None:
    """Without the option the children of the account are used as they are."""
    assert merge_children(ACCOUNT, ()) == ACCOUNT


def test_merge_option_replaces_the_groups_of_an_account_child() -> None:
    """A line for a child of the account (any case or spacing) replaces its groups and name."""
    option = parse_children("kind  een=Neo 4/5/6")
    merged = merge_children(ACCOUNT, option)
    assert merged == (
        Child(name="kind een", groups=frozenset({"neo 4/5/6"})),
        Child(name="Kind Twee", groups=frozenset({"1/2a"})),
    )
    assert children_for(["6A"], merged) == ()
    assert children_for(["Neo 4/5/6"], merged) == ("kind een",)


def test_merge_option_adds_other_children_after_the_account() -> None:
    """A line for another child adds it, after the account children, in option order."""
    option = parse_children("Kind Drie=3A\nKind Vier=4A")
    assert [child.name for child in merge_children(ACCOUNT, option)] == [
        "Kind Een",
        "Kind Twee",
        "Kind Drie",
        "Kind Vier",
    ]


def test_merge_without_account_is_the_option() -> None:
    """Without children in the account the option works as before."""
    option = parse_children("Noud=6A\nNora=1/2A")
    assert merge_children((), option) == option
