"""Bounded body reads and interpreter-independent JSON decoding (HA-free).

Shared by the three modules that talk HTTP.  ``login.py`` reads its HTML pages
with :func:`async_read_limited`; ``tokens.py`` and ``client.py`` read their
bodies with it as well and decode them with :func:`decode_json`.

``json.loads`` recurses once per nesting level, and whether a deeply nested
document ends in ``RecursionError`` depends on the interpreter version, its
recursion limit and the size of the C stack.  :func:`decode_json` therefore
bounds the body size and checks the nesting depth itself, in one linear pass
without recursion, before ``json.loads`` runs: the outcome is the same on every
Python build.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import MAX_JSON_DEPTH
from .errors import ApiError

_INVALID = object()
"""Marker for a body that is not usable JSON; no exception that holds the body survives."""


async def async_read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """Return the body when it is at most ``limit`` bytes, else None without buffering the rest.

    A ``Content-Length`` over the limit is refused before reading; otherwise at most
    ``limit + 1`` bytes are read from the (decompressed) stream.
    """
    try:
        declared = int(response.headers.get("Content-Length", ""))
    except ValueError:  # absent, malformed or absurdly long
        declared = None
    if declared is not None and declared > limit:
        return None
    stream = response.content
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = await stream.read(limit + 1 - size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    return None


def json_nesting_depth_exceeds(text: str, limit: int) -> bool:
    """Return True when ``text`` nests arrays and objects more than ``limit`` levels deep.

    One pass over the characters, without recursion.  Brackets and braces inside
    strings do not count; inside a string a backslash escapes the next character,
    so an escaped quote does not end it.  The depth never drops below zero, so
    stray closing brackets cannot hide nesting that follows them.  The scan does
    not validate JSON: malformed text is left to ``json.loads``.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}" and depth:
            depth -= 1
    return False


def decode_json(
    body: bytes,
    *,
    what: str,
    status: int | None,
    max_bytes: int,
    max_depth: int = MAX_JSON_DEPTH,
) -> Any:
    """Return the decoded JSON value of a UTF-8 ``body``, or raise ApiError.

    The body must be at most ``max_bytes`` long (else ``"<what>: response body
    too large"``), UTF-8 encoded (a byte order mark is tolerated) and nest at most
    ``max_depth`` levels; anything else, including what ``json.loads`` rejects
    with ValueError or RecursionError, is ``"<what>: invalid JSON"``.  The error
    carries ``status`` and is raised outside every ``except`` block, so neither
    ``__cause__`` nor ``__context__`` keeps a decode error whose ``doc`` or
    ``object`` would hold the body.
    """
    if len(body) > max_bytes:
        raise ApiError(f"{what}: response body too large", status)
    payload = _decode(body, max_depth)
    if payload is _INVALID:
        raise ApiError(f"{what}: invalid JSON", status)
    return payload


def _decode(body: bytes, max_depth: int) -> Any:
    """Decode ``body`` or return ``_INVALID``; every caught error is dropped with its frame."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _INVALID
    if json_nesting_depth_exceeds(text, max_depth):
        return _INVALID
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _INVALID
