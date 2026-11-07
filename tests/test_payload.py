"""Tests for the shared body helpers (pure module): bounded reads and safe JSON decoding.

The depth check must not depend on the interpreter: a too deeply nested body is refused
by the linear scan before ``json.loads`` runs, which the tests prove by replacing
``json.loads`` with a function that fails when called.  Neither may the digit bounds:
the tests that convert digits run under several process-wide ``int_max_str_digits``
settings (the ``int_max_str_digits`` fixture of ``conftest.py``).  Secret-like values
carry the ``private-`` marker so that leaks into exception messages are caught.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.socialschools import payload as payload_module
from custom_components.socialschools.const import MAX_JSON_DEPTH, MAX_JSON_INT_DIGITS
from custom_components.socialschools.errors import ApiError
from custom_components.socialschools.payload import (
    async_read_limited,
    decode_json,
    json_nesting_depth_exceeds,
)

WHAT = "GET ^communityposts$"
LIMIT = 1024


def nested_arrays(depth: int, inner: str = "") -> str:
    """Return ``depth`` nested arrays around ``inner``."""
    return "[" * depth + inner + "]" * depth


def nested_objects(depth: int, inner: str = "1") -> str:
    """Return ``depth`` nested objects (``{"k": ...}``) around ``inner``."""
    return '{"k":' * depth + inner + "}" * depth


def nested_mixed(depth: int) -> str:
    """Return ``depth`` levels alternating arrays and objects."""
    opening = "".join("[" if level % 2 == 0 else '{"k":' for level in range(depth))
    closing = "".join("]" if level % 2 == 0 else "}" for level in reversed(range(depth)))
    return opening + "0" + closing


def refuse_json_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any json.loads call from the payload module fail the test."""

    def loads(_text: str, **_kwargs: Any) -> Any:
        raise AssertionError("json.loads must not run for this body")

    monkeypatch.setattr(payload_module, "json", SimpleNamespace(loads=loads))


def assert_clean(err: ApiError, *, message: str, status: int | None) -> None:
    """The error carries only the fixed message and the status; no body, no chained error."""
    assert str(err) == f"{WHAT}: {message}"
    assert err.status == status
    assert err.__cause__ is None
    assert err.__context__ is None
    assert not getattr(err, "__notes__", None)
    assert "private-" not in repr(vars(err))
    assert all("private-" not in str(arg) for arg in err.args)


# --- json_nesting_depth_exceeds -------------------------------------------------------------


@pytest.mark.parametrize("build", [nested_arrays, nested_objects, nested_mixed])
def test_depth_of_exactly_the_limit_passes_and_one_more_is_refused(build: Any) -> None:
    """Depth MAX_JSON_DEPTH is accepted, MAX_JSON_DEPTH + 1 is not, for every container mix."""
    assert MAX_JSON_DEPTH == 64
    at_limit = build(MAX_JSON_DEPTH)
    assert json.loads(at_limit) is not None  # the fixtures are valid JSON
    assert not json_nesting_depth_exceeds(at_limit, MAX_JSON_DEPTH)
    assert json_nesting_depth_exceeds(build(MAX_JSON_DEPTH + 1), MAX_JSON_DEPTH)


@pytest.mark.parametrize(
    "text",
    [
        '["' + "[" * 1000 + '"]',  # brackets inside a string
        '{"' + "{" * 1000 + '": 1}',  # braces inside a key
        r'["a\"' + "[" * 1000 + '"]',  # an escaped quote does not end the string
        '["\\u005b' * 1 + "[" * 1000 + '"]',  # a unicode escape is plain string content
        '["é' + "[{" * 500 + '"]',  # non-ASCII content
        '["\U0001f600' + "[" * 1000 + '"]',  # a character outside the BMP
    ],
    ids=["array-string", "object-key", "escaped-quote", "unicode-escape", "non-ascii", "emoji"],
)
def test_brackets_inside_strings_do_not_count(text: str) -> None:
    """Only structural brackets count; string content of any kind is skipped."""
    assert json.loads(text) is not None
    assert not json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)


def test_escaped_backslash_before_a_quote_ends_the_string() -> None:
    """``"a\\\\"`` is a complete string, so the brackets after it are structural."""
    text = r'["a\\",' + nested_arrays(MAX_JSON_DEPTH) + "]"
    assert json.loads(text)[0] == "a\\"
    assert json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)


def test_whitespace_between_brackets_is_ignored() -> None:
    """Newlines and spaces between brackets do not hide nesting."""
    text = "[\n " * (MAX_JSON_DEPTH + 1) + "]\n" * (MAX_JSON_DEPTH + 1)
    assert json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)


def test_stray_closing_brackets_cannot_hide_deeper_nesting() -> None:
    """The depth never drops below zero, so leading closers do not offset later openers."""
    text = "]" * 1000 + "[" * (MAX_JSON_DEPTH + 1)
    assert json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)


@pytest.mark.parametrize("text", ["", "1", '"x"', "null", "[]", "{}", "[[], {}, [[]]]"])
def test_shallow_or_scalar_text_passes(text: str) -> None:
    """Scalars and shallow containers are well within the limit."""
    assert not json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)


def test_an_unterminated_string_hides_nothing_json_would_parse() -> None:
    """Brackets after an unterminated string are string content to the scan and to json."""
    text = '["' + "[" * 1000
    assert not json_nesting_depth_exceeds(text, MAX_JSON_DEPTH)
    with pytest.raises(ValueError):
        json.loads(text)


@pytest.mark.parametrize("build", [nested_arrays, nested_objects])
def test_extreme_nesting_stops_early(build: Any) -> None:
    """100 000 levels are refused as soon as the limit is passed."""
    assert json_nesting_depth_exceeds(build(100_000), MAX_JSON_DEPTH)


# --- decode_json ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [{"values": [{"id": 1, "title": "x"}], "totalCount": 1}, [1, "two", None], "text", 3.5, None],
)
def test_valid_json_is_returned_unchanged(value: Any) -> None:
    """Any valid, shallow JSON value comes back as json.loads would return it."""
    body = json.dumps(value).encode()
    assert decode_json(body, what=WHAT, status=200, max_bytes=LIMIT) == value


def test_depth_of_exactly_the_limit_is_decoded() -> None:
    """A body nested exactly MAX_JSON_DEPTH levels deep is decoded."""
    body = nested_arrays(MAX_JSON_DEPTH).encode()
    assert decode_json(body, what=WHAT, status=200, max_bytes=LIMIT) == json.loads(body)


@pytest.mark.parametrize("build", [nested_arrays, nested_objects, nested_mixed])
@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH + 1, 100_000])
def test_too_deep_json_is_refused_before_json_loads(
    monkeypatch: pytest.MonkeyPatch, build: Any, depth: int
) -> None:
    """Too deep nesting is an ApiError from the scan; json.loads never sees the body.

    This is what makes the outcome independent of the interpreter's recursion limit
    and C stack size.
    """
    refuse_json_loads(monkeypatch)
    body = build(depth).encode()
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=len(body))
    assert_clean(excinfo.value, message="invalid JSON", status=200)


@pytest.mark.parametrize(
    "body",
    [
        b'{"access_token": "private-at-trunc", "refresh_token": "private-rt-trunc"',
        b"{not json: private-body",
        b"<html>private-body</html>",
        b"",
        b'"private-unterminated',
        b"[1, 2] private-trailing",
    ],
    ids=["truncated", "not-json", "html", "empty", "unterminated-string", "trailing-data"],
)
def test_invalid_json_keeps_no_body(body: bytes) -> None:
    """What json.loads rejects is an ApiError without the body or the decode error attached."""
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=LIMIT)
    assert_clean(excinfo.value, message="invalid JSON", status=200)


@pytest.mark.parametrize(
    "body",
    [b'["private-\xff"]', '["private-x"]'.encode("utf-16"), '["private-x"]'.encode("utf-32")],
    ids=["invalid-utf8", "utf16", "utf32"],
)
def test_bodies_that_are_not_utf8_are_invalid(body: bytes) -> None:
    """Only UTF-8 is accepted; the UnicodeDecodeError (whose .object is the body) is dropped."""
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=LIMIT)
    assert_clean(excinfo.value, message="invalid JSON", status=200)


def test_utf8_byte_order_mark_is_tolerated() -> None:
    """A UTF-8 BOM is skipped, as json.loads on bytes would."""
    assert decode_json(b'\xef\xbb\xbf{"a": 1}', what=WHAT, status=200, max_bytes=LIMIT) == {"a": 1}


def test_recursion_error_from_json_loads_is_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should json.loads still recurse too deep on some interpreter, that is invalid JSON too."""

    def loads(_text: str, **_kwargs: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded private-body")

    monkeypatch.setattr(payload_module, "json", SimpleNamespace(loads=loads))
    with pytest.raises(ApiError) as excinfo:
        decode_json(b"[1]", what=WHAT, status=200, max_bytes=LIMIT)
    assert_clean(excinfo.value, message="invalid JSON", status=200)


def test_digit_bound_is_below_every_interpreter_limit() -> None:
    """``int`` consults int_max_str_digits only above this threshold, the lowest non-zero value.

    So ``int`` never refuses a literal of at most MAX_JSON_INT_DIGITS digits, on any build.
    """
    assert sys.int_info.str_digits_check_threshold >= MAX_JSON_INT_DIGITS


@pytest.mark.parametrize(
    "literal",
    ["9" * MAX_JSON_INT_DIGITS, "-" + "9" * MAX_JSON_INT_DIGITS],
    ids=["positive", "negative"],
)
def test_integer_of_exactly_the_digit_bound_is_decoded(
    int_max_str_digits: int, literal: str
) -> None:
    """MAX_JSON_INT_DIGITS digits (a minus sign not counted) decode under every setting."""
    body = f'{{"values": [{literal}]}}'.encode()
    assert decode_json(body, what=WHAT, status=200, max_bytes=LIMIT) == {"values": [int(literal)]}


@pytest.mark.parametrize(
    "body",
    [
        b"9" * (MAX_JSON_INT_DIGITS + 1),
        b"[-" + b"9" * (MAX_JSON_INT_DIGITS + 1) + b"]",
        b'{"id": ' + b"1" * 5000 + b"}",
    ],
    ids=["one-over", "negative-one-over", "5000-digits"],
)
def test_integer_with_too_many_digits_is_invalid_json(int_max_str_digits: int, body: bytes) -> None:
    """More than MAX_JSON_INT_DIGITS digits is invalid JSON, whatever int_max_str_digits says.

    Left to the setting, the answer would vary: 641 digits pass at the default 4300 but not
    at 640, and at 0 any length passes, after a conversion whose cost grows quadratically.
    """
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=len(body))
    assert_clean(excinfo.value, message="invalid JSON", status=200)


def test_huge_integer_is_refused_before_it_is_converted(
    monkeypatch: pytest.MonkeyPatch, int_max_str_digits: int
) -> None:
    """A million digits never reach ``int``, whose cost grows quadratically when unlimited."""

    def refuse_int(*_args: Any) -> int:
        raise AssertionError("int() must not run for this literal")

    monkeypatch.setattr(payload_module, "int", refuse_int, raising=False)
    body = b"[" + b"9" * 1_000_000 + b"]"
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=len(body))
    assert_clean(excinfo.value, message="invalid JSON", status=200)


def test_body_over_max_bytes_is_refused_without_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size bound comes first, so the scan and json.loads only ever see bounded input."""
    refuse_json_loads(monkeypatch)
    body = b'["' + b"x" * LIMIT + b'"]'
    with pytest.raises(ApiError) as excinfo:
        decode_json(body, what=WHAT, status=200, max_bytes=LIMIT)
    assert_clean(excinfo.value, message="response body too large", status=200)


def test_body_of_exactly_max_bytes_is_decoded() -> None:
    """The bound is inclusive."""
    body = b'"' + b"x" * (LIMIT - 2) + b'"'
    assert len(body) == LIMIT
    assert decode_json(body, what=WHAT, status=200, max_bytes=LIMIT) == "x" * (LIMIT - 2)


def test_status_is_carried_as_given() -> None:
    """The caller's status (or None) ends up on the error."""
    with pytest.raises(ApiError) as excinfo:
        decode_json(b"nope", what=WHAT, status=None, max_bytes=LIMIT)
    assert_clean(excinfo.value, message="invalid JSON", status=None)


# --- async_read_limited ---------------------------------------------------------------------


@pytest.fixture
async def body_server(aiohttp_client: ClientSessionGenerator, socket_enabled: None) -> TestClient:
    """Serve bodies of a given size, with a Content-Length header or chunked."""

    async def handle(request: web.Request) -> web.StreamResponse:
        size = int(request.query["size"])
        body = b"x" * size
        if request.query.get("mode") == "chunked":
            response = web.StreamResponse()
            response.enable_chunked_encoding()
            await response.prepare(request)
            for start in range(0, size, 100):
                await response.write(body[start : start + 100])
            await response.write_eof()
            return response
        return web.Response(body=body)

    app = web.Application()
    app.router.add_get("/body", handle)
    return await aiohttp_client(app)


async def read(session: ClientSession, server: TestClient, size: int, mode: str) -> bytes | None:
    """Fetch a body of ``size`` bytes and read it through async_read_limited."""
    url = server.make_url("/body").with_query(size=str(size), mode=mode)
    async with session.get(url) as response:
        return await async_read_limited(response, LIMIT)


@pytest.mark.parametrize("mode", ["content-length", "chunked"])
async def test_read_up_to_the_limit(body_server: TestClient, mode: str) -> None:
    """A body of exactly the limit is returned whole, with or without Content-Length."""
    assert await read(body_server.session, body_server, LIMIT, mode) == b"x" * LIMIT


@pytest.mark.parametrize("mode", ["content-length", "chunked"])
async def test_read_over_the_limit_is_refused(body_server: TestClient, mode: str) -> None:
    """One byte over the limit returns None, whether declared up front or found while reading."""
    assert await read(body_server.session, body_server, LIMIT + 1, mode) is None


async def test_read_empty_body(body_server: TestClient) -> None:
    """An empty body is returned as empty bytes."""
    assert await read(body_server.session, body_server, 0, "content-length") == b""


class _FakeStream:
    """Stream that hands out at most ``step`` bytes per read and counts what was read."""

    def __init__(self, data: bytes, step: int) -> None:
        self._data = data
        self._step = step
        self.read_total = 0

    async def read(self, n: int) -> bytes:
        chunk = self._data[: min(n, self._step)]
        self._data = self._data[len(chunk) :]
        self.read_total += len(chunk)
        return chunk


@pytest.mark.parametrize("header", ["", "garbage", "9" * 5000], ids=["absent", "garbage", "huge"])
async def test_unusable_content_length_falls_back_to_reading(
    header: str, int_max_str_digits: int
) -> None:
    """A missing or unparsable Content-Length is ignored; the stream is still bounded.

    More than 640 characters counts as malformed under every int_max_str_digits setting
    (at 0, ``int`` would otherwise parse 5000 digits and refuse before reading).
    """
    stream = _FakeStream(b"x" * (10 * LIMIT), step=300)
    response = SimpleNamespace(headers={"Content-Length": header} if header else {}, content=stream)
    assert await async_read_limited(response, LIMIT) is None  # type: ignore[arg-type]
    assert stream.read_total == LIMIT + 1


@pytest.mark.parametrize("declared", [LIMIT + 1, 10**400], ids=["one-over", "absurd"])
async def test_declared_length_over_the_limit_is_refused_without_reading(
    declared: int, int_max_str_digits: int
) -> None:
    """A Content-Length over the limit is refused before a single byte is read."""
    stream = _FakeStream(b"x" * (LIMIT + 1), step=300)
    response = SimpleNamespace(headers={"Content-Length": str(declared)}, content=stream)
    assert await async_read_limited(response, LIMIT) is None  # type: ignore[arg-type]
    assert stream.read_total == 0
