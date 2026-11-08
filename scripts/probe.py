#!/usr/bin/env python3
"""Live protocol check for the Social Schools integration, without Home Assistant.

Run it from the repository root.  The documented way is a throw-away container with
nothing but ``aiohttp`` installed::

    docker run --rm -it --network host -v "$PWD":/w -w /w python:3.12-slim \\
        sh -c "pip install -q aiohttp && python scripts/probe.py"

The script asks for the e-mail address and the password at the prompt (never as
arguments or environment variables), signs in like the native app, exchanges the
authorization code, refreshes the tokens once and then performs the four read-only
API calls the integration relies on.  It prints only yes/no answers, counts, text
lengths and field names: never tokens, names, titles, bodies or URLs.

``--check`` only imports the pure modules through the stand-in package and prints
``ok``; the test suite uses it.

Two opt-in investigations add a section after the normal output:

* ``--agenda-vorm`` prints, for the calendar items of the coming 60 days, only
  ``allDay``, the raw ``start`` and ``end`` strings and their time zone designator
  (``Z``, an offset or none). No titles, descriptions or locations.
* ``--kinderen-vorm`` prints the field names of the parent role and of its
  ``connections[]`` (the children) in ``useraccounts/current``, and whether those
  connections carry community ids that also occur on posts.  It then describes the
  ``groups`` of the children: the type (and for an object the field names and types)
  of the first group, whether a ``groupId`` (or ``id``) of a feed community occurs in
  the groups of a child and for how many posts of the feed, whether a group name of a
  child equals a community name, and whether ``yearClass`` equals a community name
  (exactly, and ignoring case and spacing).  Only types, key names, yes/no answers and
  counts: no names of children or groups, no ids.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import getpass
import importlib
from pathlib import Path
import re
import sys
import time
import types
from typing import Any

import aiohttp

PACKAGE_NAME = "ss_probe"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "socialschools"
MODULE_NAMES = ("const", "errors", "payload", "textutil", "models", "login", "tokens", "client")
MAX_MESSAGE_LENGTH = 200

_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\S+@\S+")
_LONG_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?")
_TZ_DESIGNATOR_RE = re.compile(r"(Z|[+-][0-9]{2}:?[0-9]{2})$")
_COMMUNITY_KEY_RE = re.compile(r"communit|group|klas|class", re.IGNORECASE)
_DIGITS_RE = re.compile(r"[0-9]{1,20}")
AGENDA_WINDOW = timedelta(days=60)
MAX_FIELD_DEPTH = 3


def load_modules() -> dict[str, types.ModuleType]:
    """Import the pure integration modules under the stand-in package ``ss_probe``.

    The stand-in package carries only ``__path__``, so the integration's own
    ``__init__`` (which needs Home Assistant) is never executed, and no bytecode
    is written into the mounted repository.
    """
    sys.dont_write_bytecode = True
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_DIR)]
        sys.modules[PACKAGE_NAME] = package
    return {name: importlib.import_module(f"{PACKAGE_NAME}.{name}") for name in MODULE_NAMES}


def ja_nee(value: bool) -> str:
    """Return the Dutch yes/no word for ``value``."""
    return "ja" if value else "nee"


def field_names(value: Any) -> str:
    """Return the sorted key names of a JSON object on one line (keys are schema, not content)."""
    if not isinstance(value, Mapping):
        return "(geen object)"
    return ", ".join(sorted(str(key) for key in value)) or "(leeg)"


def first_item(value: Any) -> Any:
    """Return the first element of a non-empty list, else None."""
    return value[0] if isinstance(value, list) and value else None


def text_length(value: Any) -> int:
    """Return the length of a string value; anything else counts as empty."""
    return len(value) if isinstance(value, str) else 0


def sanitise(message: str) -> str:
    """Make an exception message safe to show: no URLs, e-mail addresses or long opaque words."""
    text = " ".join(message.split())
    text = _URL_RE.sub("<url>", text)
    text = _EMAIL_RE.sub("<e-mail>", text)
    text = _LONG_WORD_RE.sub("<weggelaten>", text)
    return text[:MAX_MESSAGE_LENGTH]


def describe_post(post: Mapping[str, Any], textutil: types.ModuleType) -> None:
    """Print the field names of a feed post and of its nested objects, plus the body type."""
    print(f"Velden van een bericht: {field_names(post)}")
    nested = (
        ("communities[0]", first_item(post.get("communities"))),
        ("author", post.get("author")),
        ("media[0]", first_item(post.get("media"))),
    )
    for label, value in nested:
        if isinstance(value, Mapping):
            print(f"Velden van {label}: {field_names(value)}")
    body = post.get("body")
    is_html = isinstance(body, str) and textutil.looks_like_html(body)
    print(f"Body is HTML: {ja_nee(is_html)}")


def raw_timestamp(value: Any) -> str:
    """Return ``value`` when it is an ISO-like timestamp string; otherwise only its type."""
    if isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value.strip()):
        return value.strip()
    return f"<{type(value).__name__}>"


def tz_designator(value: Any) -> str:
    """Return the time zone designator of a timestamp string: ``Z``, an offset or ``geen``."""
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value.strip()):
        return "onbekend"
    match = _TZ_DESIGNATOR_RE.search(value.strip())
    return match.group(1) if match else "geen"


def describe_agenda(items: list[Any]) -> None:
    """Print allDay and the raw start/end of every calendar item; never titles or other text."""
    print(f"Agenda-vorm: {len(items)} items in de komende {AGENDA_WINDOW.days} dagen")
    for item in items:
        if not isinstance(item, Mapping):
            print("  (geen object)")
            continue
        all_day = item.get("allDay")
        all_day_text = (
            ja_nee(all_day) if isinstance(all_day, bool) else f"<{type(all_day).__name__}>"
        )
        start, end = item.get("start"), item.get("end")
        print(
            f"  allDay={all_day_text}"
            f"  start={raw_timestamp(start)} (tijdzone: {tz_designator(start)})"
            f"  end={raw_timestamp(end)} (tijdzone: {tz_designator(end)})"
        )


def type_name(value: Any) -> str:
    """Return a short JSON type name for a value (never the value itself)."""
    if isinstance(value, bool):
        return "bool"
    return {dict: "object", list: "list", str: "str", int: "int", float: "float"}.get(
        type(value), "null" if value is None else type(value).__name__
    )


def print_field_tree(value: Any, label: str, depth: int = 0) -> None:
    """Print the key names and value types of an object and its nested objects (no values)."""
    if isinstance(value, list):
        value = first_item(value)
        label = f"{label}[0]"
    if not isinstance(value, Mapping) or depth > MAX_FIELD_DEPTH:
        return
    fields = ", ".join(f"{key} ({type_name(item)})" for key, item in sorted(value.items()))
    print(f"Velden van {label}: {fields or '(leeg)'}")
    for key, item in sorted(value.items()):
        if isinstance(item, Mapping) or (
            isinstance(item, list) and isinstance(first_item(item), Mapping)
        ):
            print_field_tree(item, f"{label}.{key}", depth + 1)


def community_ids(value: Any, *, inside: bool = False, path: str = "") -> tuple[set[int], set[str]]:
    """Collect integer ids found under community/group-like keys, with the key paths."""
    ids: set[int] = set()
    paths: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            matched = inside or bool(_COMMUNITY_KEY_RE.search(str(key)))
            child = f"{path}.{key}" if path else str(key)
            if matched and isinstance(item, int) and not isinstance(item, bool):
                if inside and key != "id":
                    continue
                ids.add(item)
                paths.add(child)
            elif isinstance(item, Mapping | list):
                found, where = community_ids(item, inside=matched, path=child)
                ids |= found
                paths |= where
    elif isinstance(value, list):
        for item in value:
            if inside and isinstance(item, int) and not isinstance(item, bool):
                ids.add(item)
                paths.add(f"{path}[]")
            else:
                found, where = community_ids(item, inside=inside, path=f"{path}[]")
                ids |= found
                paths |= where
    return ids, paths


def describe_children(account: Mapping[str, Any], posts: list[Any], parent_role: int) -> None:
    """Print the shape of the parent role and its connections, and how they relate to the feed."""
    roles = [
        role
        for role in as_list(account.get("roles"))
        if isinstance(role, Mapping) and role.get("type") == parent_role
    ]
    print(f"Kinderen-vorm: {len(roles)} ouderrol(len)")
    if not roles:
        return
    counts = [len(as_list(role.get("connections"))) for role in roles]
    print(f"Aantal connections per ouderrol: {', '.join(str(count) for count in counts)}")
    print_field_tree(roles[0], "roles[0]")
    connections = [item for role in roles for item in as_list(role.get("connections"))]
    ids, paths = community_ids(connections)
    print(f"Community-id's in connections: {ja_nee(bool(ids))}")
    if paths:
        print(f"Gevonden onder: {', '.join(sorted(paths))}")
    if ids:
        post_ids = set().union(*(community_ids_of(post, "id") for post in posts))
        post_group_ids = set().union(*(community_ids_of(post, "groupId") for post in posts))
        print(f"Overlap van die id's met communities[].id: {ja_nee(bool(ids & post_ids))}")
        print(
            f"Overlap van die id's met communities[].groupId: {ja_nee(bool(ids & post_group_ids))}"
        )
    describe_child_groups(connections, roles, posts)


def as_list(value: Any) -> list[Any]:
    """Return ``value`` when it is a list, else an empty list."""
    return value if isinstance(value, list) else []


def id_value(value: Any) -> int | None:
    """Return an id-like value as int: an int (not a bool) or a short string of digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def normalise_name(value: str) -> str:
    """Return a group name for comparison: whitespace collapsed, case folded (as the option)."""
    return " ".join(value.split()).casefold()


def group_values(connections: list[Any]) -> tuple[dict[str, set[int]], dict[str, set[str]]]:
    """Collect the ids and the texts in the ``groups`` of the connections, keyed by their place.

    The place is ``groups[]`` for a group given as a bare value and ``groups[].<key>`` for a
    value under ``key`` of a group object.  Callers print only the places, never the values.
    """
    ids: dict[str, set[int]] = {}
    texts: dict[str, set[str]] = {}
    for connection in connections:
        groups = connection.get("groups") if isinstance(connection, Mapping) else None
        for group in as_list(groups):
            items = (
                [(f"groups[].{key}", item) for key, item in group.items()]
                if isinstance(group, Mapping)
                else [("groups[]", group)]
            )
            for place, item in items:
                if (number := id_value(item)) is not None:
                    ids.setdefault(place, set()).add(number)
                if isinstance(item, str) and item.strip():
                    texts.setdefault(place, set()).add(item)
    return ids, texts


def community_ids_of(post: Any, key: str) -> set[int]:
    """Return the id-like ``key`` values of the communities of one feed post."""
    communities = post.get("communities") if isinstance(post, Mapping) else None
    return {
        number
        for community in as_list(communities)
        if isinstance(community, Mapping) and (number := id_value(community.get(key))) is not None
    }


def community_names_of(post: Any) -> set[str]:
    """Return the community names of one feed post."""
    communities = post.get("communities") if isinstance(post, Mapping) else None
    return {
        community["name"]
        for community in as_list(communities)
        if isinstance(community, Mapping) and isinstance(community.get("name"), str)
    }


def matching_posts(
    per_post: list[set[Any]], by_place: Mapping[str, set[Any]]
) -> tuple[int, list[str]]:
    """Return how many posts share a value with ``by_place``, and through which places."""
    places = sorted(
        place for place, values in by_place.items() if any(values & p for p in per_post)
    )
    wanted: set[Any] = set().union(*(by_place[place] for place in places))
    return sum(1 for values in per_post if values & wanted), places


def answer(count: int, total: int, places: list[str] | None = None) -> str:
    """Return ``nee`` or ``ja, bij N van M berichten`` plus the places; never a value."""
    if not count:
        return "nee"
    via = f" (via {', '.join(places)})" if places else ""
    return f"ja, bij {count} van {total} berichten{via}"


def describe_first_group(roles: list[Any]) -> None:
    """Print the type, and for an object its field names and types, of the first group of a child."""
    for role_index, role in enumerate(roles):
        for connection_index, connection in enumerate(as_list(role.get("connections"))):
            groups = as_list(connection.get("groups") if isinstance(connection, Mapping) else None)
            if not groups:
                continue
            label = f"roles[{role_index}].connections[{connection_index}].groups[0]"
            group = groups[0]
            if isinstance(group, Mapping):
                fields = ", ".join(
                    f"{key} ({type_name(item)})" for key, item in sorted(group.items())
                )
                print(f"Vorm van {label}: object; velden: {fields or '(leeg)'}")
            else:
                print(f"Vorm van {label}: {type_name(group)}")
            return
    print("Vorm van connections[].groups[0]: geen groepen (lijst leeg of ontbreekt)")


def describe_child_groups(connections: list[Any], roles: list[Any], posts: list[Any]) -> None:
    """Print how the groups and yearClass of the children relate to the communities of the feed.

    Only types, key names, yes/no answers and counts; never names, group names or ids.
    """
    counts = [
        len(as_list(connection.get("groups"))) if isinstance(connection, Mapping) else 0
        for connection in connections
    ]
    print(f"Groepen per connection: {', '.join(map(str, counts)) or 'geen connections'}")
    describe_first_group(roles)
    total = len(posts)
    ids_by_place, texts_by_place = group_values(connections)
    for key in ("groupId", "id"):
        per_post = [community_ids_of(post, key) for post in posts]
        label = f"{key} uit de feed in de groepen van een kind"
        if not any(per_post):
            print(f"{label}: onbekend (geen communities[].{key} in de feed)")
            continue
        count, places = matching_posts(per_post, ids_by_place)
        print(f"{label}: {answer(count, total, places)}")
    names_per_post = [community_names_of(post) for post in posts]
    if not any(names_per_post):
        print("Communitynamen in de feed: geen, dus niets te vergelijken")
        return
    count, places = matching_posts(names_per_post, texts_by_place)
    print(
        "Groepsnaam van een kind gelijk aan een communitynaam in de feed: "
        f"{answer(count, total, places)}"
    )
    year_classes = {
        connection["yearClass"]
        for connection in connections
        if isinstance(connection, Mapping)
        and isinstance(connection.get("yearClass"), str)
        and connection["yearClass"].strip()
    }
    if not year_classes:
        print("yearClass gelijk aan een communitynaam in de feed: onbekend (geen yearClass)")
        return
    exact, _ = matching_posts(names_per_post, {"yearClass": year_classes})
    print(f"yearClass exact gelijk aan een communitynaam in de feed: {answer(exact, total)}")
    loose, _ = matching_posts(
        [{normalise_name(name) for name in names} for names in names_per_post],
        {"yearClass": {normalise_name(value) for value in year_classes}},
    )
    print(f"yearClass gelijk zonder verschil in hoofdletters en spaties: {answer(loose, total)}")


async def async_compare_detail(api: Any, school_id: int, post: Mapping[str, Any]) -> None:
    """Fetch the detail of ``post`` and compare the body lengths (lengths only, never content)."""
    post_id = post.get("id")
    if not isinstance(post_id, int) or isinstance(post_id, bool):
        print("Feedbody even lang als detail: onbekend (bericht zonder geldig id)")
        return
    detail = await api.async_get_post(school_id, post_id)
    feed_length = text_length(post.get("body"))
    detail_length = text_length(detail.get("body")) if isinstance(detail, Mapping) else 0
    same = ja_nee(feed_length == detail_length)
    print(f"Feedbody even lang als detail: {same} ({feed_length}/{detail_length})")


async def async_probe_api(
    api: Any,
    *,
    models: types.ModuleType,
    textutil: types.ModuleType,
    lookback: timedelta,
    horizon: timedelta,
    parent_role: int,
    agenda_vorm: bool = False,
    kinderen_vorm: bool = False,
) -> None:
    """Run the four whitelisted GET calls for the first parent school and describe the shapes."""
    raw_account = await api.async_get_current_account()
    account = models.parse_account(raw_account)
    print(f"Ouderrollen/scholen: {len(account.schools)}")
    if not account.schools:
        return
    school_id = account.schools[0].id
    feed = await api.async_get_posts(school_id)
    post = first_item(feed.get("values")) if isinstance(feed, Mapping) else None
    if post is None:
        print("Velden van een bericht: geen berichten in de feed")
    else:
        describe_post(post, textutil)
        await async_compare_detail(api, school_id, post)
    now = datetime.now(UTC)
    events = await api.async_get_events(school_id, now - lookback, now + horizon)
    items = events.get("values") if isinstance(events, Mapping) else events
    if not isinstance(items, list):
        items = []
    event = first_item(items)
    event_fields = field_names(event) if event is not None else "geen events in venster"
    print(f"Velden van een event: {event_fields}")
    print(f"Aantal events in venster: {len(items)}")
    if agenda_vorm:
        upcoming = await api.async_get_events(school_id, now, now + AGENDA_WINDOW)
        upcoming_items = upcoming.get("values") if isinstance(upcoming, Mapping) else upcoming
        describe_agenda(upcoming_items if isinstance(upcoming_items, list) else [])
    if kinderen_vorm:
        posts = feed.get("values") if isinstance(feed, Mapping) else None
        describe_children(raw_account, posts if isinstance(posts, list) else [], parent_role)


async def async_check_refresh(manager: Any, error_type: type[Exception]) -> None:
    """Refresh once and report whether that works and whether the refresh token rotated."""
    old_refresh_token = manager.refresh_token
    try:
        await manager.async_refresh()
    except error_type as err:
        print(f"Refresh werkt: nee ({type(err).__name__})")
        print("Refresh token roteert: onbekend")
        return
    rotated = manager.refresh_token != old_refresh_token
    print("Refresh werkt: ja")
    print(f"Refresh token roteert: {ja_nee(rotated)}")


async def async_probe(
    modules: Mapping[str, types.ModuleType],
    username: str,
    password: str,
    *,
    agenda_vorm: bool = False,
    kinderen_vorm: bool = False,
) -> None:
    """Sign in, exchange the code, refresh once and exercise the read-only API."""
    const = modules["const"]
    tokens = modules["tokens"]
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as login_session:
        authorization = await modules["login"].async_native_login(login_session, username, password)
        del password
        exchanged = await tokens.async_exchange_code(
            login_session, authorization.code, authorization.code_verifier
        )
    del authorization
    refresh_received = exchanged.refresh_token is not None
    expires_in = exchanged.expires_in
    print(f"Refresh token ontvangen: {ja_nee(refresh_received)}")
    print(f"expires_in: {expires_in}")
    async with aiohttp.ClientSession() as api_session:
        manager = tokens.TokenManager(
            api_session,
            client_id=const.OIDC_CLIENT_ID,
            refresh_token=exchanged.refresh_token,
            access_token=exchanged.access_token,
            expires_at=time.monotonic() + expires_in,
        )
        del exchanged
        await async_check_refresh(manager, modules["errors"].SocialSchoolsError)
        await async_probe_api(
            modules["client"].SocialSchoolsClient(api_session, manager),
            models=modules["models"],
            textutil=modules["textutil"],
            lookback=timedelta(days=const.CALENDAR_LOOKBACK_DAYS),
            horizon=timedelta(days=const.DEFAULT_CALENDAR_DAYS),
            parent_role=const.ROLE_TYPE_PARENT,
            agenda_vorm=agenda_vorm,
            kinderen_vorm=kinderen_vorm,
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line; credentials are always prompted for, never options."""
    parser = argparse.ArgumentParser(
        description=(
            "Live protocol check for Social Schools: asks for e-mail and password, "
            "prints only yes/no answers, counts, lengths and field names."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only import the pure modules through the stand-in package, print 'ok' and exit",
    )
    parser.add_argument(
        "--agenda-vorm",
        action="store_true",
        help="also print allDay and the raw start/end of the calendar items of the coming 60 days",
    )
    parser.add_argument(
        "--kinderen-vorm",
        action="store_true",
        help=(
            "also print the field names of the children (connections), the shape of their "
            "groups and whether those match the communities of the feed"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the probe and return the exit code (1 on any error, with class name and message)."""
    args = parse_args(argv)
    try:
        modules = load_modules()
        if args.check:
            print("ok")
            return 0
        asyncio.run(
            async_probe(
                modules,
                input("E-mailadres: ").strip(),
                getpass.getpass("Wachtwoord: "),
                agenda_vorm=args.agenda_vorm,
                kinderen_vorm=args.kinderen_vorm,
            )
        )
    except (Exception, KeyboardInterrupt) as err:
        message = sanitise(str(err))
        print(
            f"{type(err).__name__}: {message}" if message else type(err).__name__, file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
