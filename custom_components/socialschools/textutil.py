"""Plain-text helpers for post bodies and event descriptions (HA-free).

``html_to_text`` turns the HTML that Social Schools delivers into readable
plain text with only the standard library's ``html.parser``: block elements
become line breaks, list items get a ``- `` prefix, links keep their URL and
scripts, styles and comments disappear.  Plain text goes through the same
whitespace normalisation, so callers never need to know which one they got.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.entities import html5
from html.parser import HTMLParser
import re

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "table",
        "ul",
        "ol",
        "blockquote",
        "section",
        "article",
        "header",
        "footer",
        "pre",
    }
)
"""Elements whose start and end tags begin a new line (``br`` and ``hr`` are handled apart)."""

_LIST_TAGS = frozenset({"li", "ul", "ol"})
_CELL_TAGS = frozenset({"td", "th"})
_DROP_TAGS = frozenset({"script", "style", "head", "template"})
_HEAD_TAGS = frozenset({"base", "link", "meta", "noscript", "title"})
"""Elements that live in ``head``; any other start tag ends an unclosed ``head`` (as browsers do)."""
_LIST_PREFIX = "- "
_ELLIPSIS = "…"

_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_ENTITY_RE = re.compile(r"&(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
_LINK_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_INLINE_WS_RE = re.compile(r"[^\S\n]+")
_TRAILING_INLINE_WS_RE = re.compile(r"[^\S\n]+\Z")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def looks_like_html(value: str) -> bool:
    """Return True when ``value`` holds an HTML tag or a (known) character entity."""
    if _TAG_RE.search(value):
        return True
    return any(
        match.group(1).startswith("#") or f"{match.group(1)};" in html5
        for match in _ENTITY_RE.finditer(value)
    )


def collapse_whitespace(value: str) -> str:
    """Return ``value`` on a single line: whitespace runs become one space, ends stripped."""
    return " ".join(value.split())


def truncate(value: str, limit: int) -> tuple[str, bool]:
    """Return ``value`` cut to at most ``limit`` characters and whether it was cut.

    A cut text ends with a single ellipsis character and still fits ``limit``.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len(value) <= limit:
        return value, False
    return value[: limit - 1].rstrip() + _ELLIPSIS, True


@dataclass
class _Link:
    """An open ``<a>`` with an http(s) target and the text seen inside it so far."""

    href: str
    text: list[str] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    """Collect the visible text of a document with line breaks at block boundaries.

    Whitespace right after a line break is absorbed (it is layout, not content);
    newlines inside text are kept so that plain text keeps its line structure.
    """

    def __init__(self) -> None:
        """Start with an empty output."""
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._absorb = True  # drop leading whitespace until visible text arrives
        self._prefix = ""  # list marker waiting for the first text of an item
        self._dropping: list[str] = []  # open elements whose content is discarded
        self._link: _Link | None = None

    @property
    def text(self) -> str:
        """Return the raw collected text (not yet normalised)."""
        return "".join(self._parts)

    def close(self) -> None:
        """Flush the parser and finish an unclosed link."""
        super().close()
        self._end_link()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Begin a dropped element, a line, a list item, a table cell or a link."""
        if tag in _DROP_TAGS:
            self._dropping.append(tag)
            return
        if self._dropping:
            if self._dropping[-1] != "head" or tag in _HEAD_TAGS:
                return
            self._dropping.pop()  # an unclosed <head>: any other element starts the body
        if tag == "br":
            self._newline()
        elif tag == "hr":
            self._break_line()
            self._parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self._break_line()
            if tag == "li":
                self._prefix = _LIST_PREFIX
        elif tag in _CELL_TAGS:
            self._text(" ")
        elif tag == "a":
            self._start_link(attrs)

    def handle_endtag(self, tag: str) -> None:
        """End a dropped element, a line, a list, a table cell or a link."""
        if self._dropping:
            if tag in self._dropping:
                del self._dropping[self._dropping.index(tag) :]
            return
        if tag in _BLOCK_TAGS:
            self._break_line()
            if tag in _LIST_TAGS:
                self._prefix = ""
        elif tag in _CELL_TAGS:
            self._text(" ")
        elif tag == "a":
            self._end_link()

    def handle_data(self, data: str) -> None:
        """Add visible text; non-breaking spaces become ordinary spaces."""
        if not self._dropping:
            self._text(data.replace("\xa0", " "))

    def _text(self, data: str) -> None:
        """Append text, absorbing leading whitespace after a line break."""
        if self._absorb:
            data = data.lstrip()
            if not data:
                return
            self._absorb = False
            if self._prefix:
                self._parts.append(self._prefix)
                self._prefix = ""
        self._parts.append(data)
        if self._link is not None:
            self._link.text.append(data)

    def _trim_line_end(self) -> None:
        """Remove spaces and tabs at the end of the output."""
        while self._parts:
            trimmed = _TRAILING_INLINE_WS_RE.sub("", self._parts[-1])
            if trimmed:
                self._parts[-1] = trimmed
                return
            self._parts.pop()

    def _break_line(self) -> None:
        """Make sure the next text starts on a new line (no empty line is added)."""
        self._trim_line_end()
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")
        self._absorb = True
        if self._link is not None:
            self._link.text.append(" ")

    def _newline(self) -> None:
        """Force a line break (``<br>``)."""
        self._trim_line_end()
        self._parts.append("\n")
        self._absorb = True
        if self._link is not None:
            self._link.text.append(" ")

    def _start_link(self, attrs: list[tuple[str, str | None]]) -> None:
        """Track an ``<a>`` whose href is an http(s) URL; a nested one closes the open link."""
        self._end_link()
        href = next((value for name, value in attrs if name == "href" and value), "").strip()
        if _LINK_URL_RE.fullmatch(href):
            self._link = _Link(href)

    def _end_link(self) -> None:
        """Append the URL of the link that just ended unless its text already is the URL."""
        link = self._link
        if link is None:
            return
        self._link = None
        text = collapse_whitespace("".join(link.text))
        if not text:
            self._text(link.href)
        elif text != link.href:
            self._text(f" ({link.href})")


def html_to_text(value: str) -> str:
    """Convert HTML (or plain text) to normalised plain text.

    Block elements start and end lines, ``<br>`` breaks a line, ``<hr>`` leaves
    an empty line, list items get a ``- `` prefix, table cells are separated by
    a space, ``<a href="http(s)://...">text</a>`` becomes ``text (url)`` when
    the text differs from the URL, and the contents of ``script``, ``style``,
    ``head`` and ``template`` elements and comments are dropped.  Unknown tags
    are transparent.  Entities are decoded and ``&nbsp;`` becomes a space;
    runs of spaces and tabs collapse to one space, lines are stripped, three
    or more consecutive newlines collapse to two and the result is stripped.
    """
    extractor = _TextExtractor()
    extractor.feed(value)
    extractor.close()
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in extractor.text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
