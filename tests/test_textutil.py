"""Tests for the plain-text helpers (pure module).

Every input is synthetic HTML or plain text.  ``html_to_text`` is checked rule
by rule against the design contract (DESIGN.md §7), ``looks_like_html`` on
tags, entities and plain-text comparisons, and ``collapse_whitespace`` and
``truncate`` on their boundaries.
"""

from __future__ import annotations

import pytest

from custom_components.socialschools.textutil import (
    collapse_whitespace,
    html_to_text,
    looks_cut,
    looks_like_html,
    truncate,
)

ELLIPSIS = "…"


# --- html_to_text ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param("<p>Eerste</p><p>Tweede</p>", "Eerste\nTweede", id="paragraphs"),
        pytest.param("<div>Een</div><div>Twee</div>", "Een\nTwee", id="divs"),
        pytest.param("<div><p>Genest</p></div>", "Genest", id="nested-blocks-no-blank-line"),
        pytest.param(
            "<p>Regel een<br>Regel twee</p>", "Regel een\nRegel twee", id="br-in-paragraph"
        ),
        pytest.param(
            "<p>Regel een<br/>Regel twee<br />Regel drie</p>",
            "Regel een\nRegel twee\nRegel drie",
            id="br-self-closing",
        ),
        pytest.param(
            "<h1>Kop</h1><p>Tekst</p><h2>Subkop</h2>Meer", "Kop\nTekst\nSubkop\nMeer", id="headings"
        ),
        pytest.param(
            "<h1>a</h1><h2>b</h2><h3>c</h3><h4>d</h4><h5>e</h5><h6>f</h6>",
            "a\nb\nc\nd\ne\nf",
            id="all-heading-levels",
        ),
        pytest.param(
            "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>",
            "A B\nC D",
            id="table-rows",
        ),
        pytest.param(
            "<table><tr><th>Dag</th><th>Tijd</th></tr><tr><td>Ma</td><td>9:00</td></tr></table>",
            "Dag Tijd\nMa 9:00",
            id="table-header-cells",
        ),
        pytest.param(
            "Voor<table><tr><td>cel</td></tr></table>Na", "Voor\ncel\nNa", id="table-on-own-lines"
        ),
        pytest.param("<p>Boven</p><hr><p>Onder</p>", "Boven\n\nOnder", id="hr-leaves-blank-line"),
        pytest.param("<p>Boven</p><hr/><p>Onder</p>", "Boven\n\nOnder", id="hr-self-closing"),
        pytest.param("<p>a</p><hr><hr><p>b</p>", "a\n\nb", id="double-hr-one-blank-line"),
        pytest.param("<blockquote>Citaat</blockquote>Na", "Citaat\nNa", id="blockquote"),
        pytest.param(
            "<section>S</section><article>A</article><header>H</header><footer>F</footer>",
            "S\nA\nH\nF",
            id="sectioning-elements",
        ),
        pytest.param("<pre>  voor\n  geformatteerd</pre>Na", "voor\ngeformatteerd\nNa", id="pre"),
        pytest.param("<P>Hoofd</P><DIV>x</DIV>", "Hoofd\nx", id="uppercase-tags"),
        pytest.param("Tekst<br>", "Tekst", id="trailing-br-stripped"),
        pytest.param("<p>Een   </p><p>Twee</p>", "Een\nTwee", id="trailing-spaces-before-break"),
    ],
)
def test_html_to_text_blocks(html: str, expected: str) -> None:
    """Block elements, ``<br>`` and ``<hr>`` start and end lines."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            "<ul><li>Appel</li><li>Peer<ul><li>Conference</li><li>Doyenne</li></ul></li>"
            "<li>Banaan</li></ul>",
            "- Appel\n- Peer\n- Conference\n- Doyenne\n- Banaan",
            id="nested-ul",
        ),
        pytest.param("<ol><li>Eerste</li><li>Tweede</li></ol>", "- Eerste\n- Tweede", id="ol"),
        pytest.param(
            "<ol><li>Buiten<ol><li>Binnen</li></ol>Na binnen</li></ol>",
            "- Buiten\n- Binnen\nNa binnen",
            id="nested-ol-with-trailing-text",
        ),
        pytest.param(
            "<ul><li>  <b>Vet</b> item</li></ul>", "- Vet item", id="inline-markup-in-item"
        ),
        pytest.param(
            "<ul><li><p>Para in item</p></li></ul>", "- Para in item", id="paragraph-in-item"
        ),
        pytest.param(
            "<p>Meenemen:</p><ul><li>laarzen</li><li>jas</li></ul><p>Tot morgen</p>",
            "Meenemen:\n- laarzen\n- jas\nTot morgen",
            id="list-between-paragraphs",
        ),
        pytest.param("<ul><li>a</li></ul><ul><li>b</li></ul>", "- a\n- b", id="two-lists"),
        pytest.param("<ul><li>Een<br>vervolg</li></ul>", "- Een\nvervolg", id="br-inside-item"),
        pytest.param("<ul><li>Een<li>Twee", "- Een\n- Twee", id="unclosed-items"),
        pytest.param("<li>Los</li>", "- Los", id="bare-item"),
        pytest.param("<ul><li></li><li>Twee</li></ul>", "- Twee", id="empty-item"),
    ],
)
def test_html_to_text_lists(html: str, expected: str) -> None:
    """Every ``<li>`` is prefixed with ``- `` on its own line, also when nested."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            'Zie <a href="https://example.test/info">de website</a> voor meer.',
            "Zie de website (https://example.test/info) voor meer.",
            id="https-text-differs",
        ),
        pytest.param(
            'Zie <a href="http://example.test/a">hier</a>.',
            "Zie hier (http://example.test/a).",
            id="http",
        ),
        pytest.param(
            'Zie <a href="https://example.test/info">https://example.test/info</a> voor meer.',
            "Zie https://example.test/info voor meer.",
            id="text-equals-href",
        ),
        pytest.param(
            'Zie <a href="https://example.test/info">   https://example.test/info\t</a> voor meer.',
            "Zie https://example.test/info voor meer.",
            id="text-equals-href-after-collapsing",
        ),
        pytest.param(
            'Link <a href="https://example.test/x"></a> leeg.',
            "Link https://example.test/x leeg.",
            id="empty-text-shows-url-once",
        ),
        pytest.param(
            'Mail <a href="mailto:info@example.test">de school</a>.',
            "Mail de school.",
            id="mailto-plain-text",
        ),
        pytest.param('Bel <a href="tel:+31000000000">de school</a>.', "Bel de school.", id="tel"),
        pytest.param(
            'Link <a href="/relative/path">relatief</a>.', "Link relatief.", id="relative"
        ),
        pytest.param('Link <a href="javascript:alert(1)">klik</a>.', "Link klik.", id="javascript"),
        pytest.param('Link <a href="ftp://example.test/f">ftp</a>.', "Link ftp.", id="ftp"),
        pytest.param("Link <a>zonder href</a>.", "Link zonder href.", id="no-href"),
        pytest.param('Link <a href="">leeg</a>.', "Link leeg.", id="empty-href"),
        pytest.param(
            'Link <a href="https://example.test/a b">spatie</a>.',
            "Link spatie.",
            id="href-with-space-is-not-a-url",
        ),
        pytest.param(
            "<a href='https://example.test/p?q=1&amp;r=2'>vraag</a>",
            "vraag (https://example.test/p?q=1&r=2)",
            id="entity-in-href",
        ),
        pytest.param(
            '<a href="HTTPS://EXAMPLE.TEST/UP">up</a>',
            "up (HTTPS://EXAMPLE.TEST/UP)",
            id="scheme-case-insensitive",
        ),
        pytest.param(
            '<a href="https://example.test/i"><b>vet</b> en <i>cursief</i></a>',
            "vet en cursief (https://example.test/i)",
            id="inline-markup-in-link",
        ),
        pytest.param(
            'Link <a href="https://example.test/a">een</a> <a href="https://example.test/b">twee</a>.',
            "Link een (https://example.test/a) twee (https://example.test/b).",
            id="two-links",
        ),
        pytest.param(
            '<a href="https://example.test/multi">Eerste<br>Tweede</a>',
            "Eerste\nTweede (https://example.test/multi)",
            id="link-text-with-br",
        ),
        pytest.param(
            'Zie <a href="https://example.test/u">hier',
            "Zie hier (https://example.test/u)",
            id="unclosed-link",
        ),
    ],
)
def test_html_to_text_links(html: str, expected: str) -> None:
    """Only http(s) links whose collapsed text differs from the URL become ``text (url)``."""
    assert html_to_text(html) == expected


def test_html_to_text_nested_links_keep_both_urls() -> None:
    """A link opened inside another link closes the outer one; no URL is lost or repeated."""
    text = html_to_text(
        '<a href="https://example.test/a">buiten <a href="https://example.test/b">binnen</a> na</a>'
    )
    assert text.count("https://example.test/a") == 1
    assert text.count("https://example.test/b") == 1
    assert text.startswith("buiten")
    assert text.endswith("na")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param("Vier&nbsp;uur", "Vier uur", id="nbsp"),
        pytest.param("Tom &amp; Jerry", "Tom & Jerry", id="amp"),
        pytest.param("&#39;quotes&#39;", "'quotes'", id="decimal-charref"),
        pytest.param("caf&eacute;", "café", id="named-entity"),
        pytest.param("&#x27;x&#x27;", "'x'", id="hex-charref"),
        pytest.param("&euro;5 &hellip;", "€5 …", id="euro-and-hellip"),
        pytest.param("&lt;niet een tag&gt;", "<niet een tag>", id="escaped-angle-brackets"),
        pytest.param(
            "<p>Hallo&nbsp;&nbsp;&nbsp;wereld</p>", "Hallo wereld", id="nbsp-run-collapses"
        ),
        pytest.param("<p>Tekst&nbsp;</p>", "Tekst", id="trailing-nbsp-stripped"),
        pytest.param("&amp;amp;", "&amp;", id="decoded-once"),
        pytest.param("&unknown; blijft", "&unknown; blijft", id="unknown-entity-kept"),
    ],
)
def test_html_to_text_entities(html: str, expected: str) -> None:
    """Entities are decoded and ``&nbsp;`` becomes an ordinary space."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            "<script>var x = '<p>fake</p>';</script>Zichtbaar", "Zichtbaar", id="script-fake-tags"
        ),
        pytest.param("<script>if (a<b) {}</script><div>Na</div>", "Na", id="script-with-lt"),
        pytest.param(
            "<style>p { content: '<b>x</b>'; }</style>Zichtbaar", "Zichtbaar", id="style-fake-tags"
        ),
        pytest.param(
            '<style>p::before { content: "</p>"; }</style>Na', "Na", id="style-fake-end-tag"
        ),
        pytest.param("<SCRIPT>bad</SCRIPT>Ok", "Ok", id="uppercase-script"),
        pytest.param(
            "<head><title>Titel</title><script>a<b</script></head><body><p>Body</p></body>",
            "Body",
            id="head-dropped",
        ),
        pytest.param(
            "<html><head><meta charset='utf-8'><title>T</title></head><body><p>Body</p></body></html>",
            "Body",
            id="full-document",
        ),
        pytest.param("<html><head><title>T</title><p>Body</p>", "Body", id="unclosed-head-then-p"),
        pytest.param("<head><title>T</title><body>Body", "Body", id="unclosed-head-then-body"),
        pytest.param(
            "<head><template><p>in sjabloon</p></template></head><p>Body</p>",
            "Body",
            id="template-inside-head",
        ),
        pytest.param("<template><p>sjabloon</p></template>Echt", "Echt", id="template-fake-tags"),
        pytest.param(
            "<!-- <p>commentaar</p> -->Zichtbaar<!-- nog -->", "Zichtbaar", id="comments-fake-tags"
        ),
        pytest.param("<script>onafgesloten <p>x</p>", "", id="unclosed-script-drops-the-rest"),
    ],
)
def test_html_to_text_dropped_content(html: str, expected: str) -> None:
    """Script, style, head, template and comment contents vanish, fake tags included."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param("Tekst\tmet\t\ttabs  en   spaties", "Tekst met tabs en spaties", id="tabs"),
        pytest.param("Een\nTwee", "Een\nTwee", id="single-newline-kept"),
        pytest.param("Een\n\nTwee", "Een\n\nTwee", id="blank-line-kept"),
        pytest.param("Een\n\n\n\n\nTwee", "Een\n\nTwee", id="three-plus-newlines-collapse"),
        pytest.param("Een\r\nTwee", "Een\nTwee", id="crlf"),
        pytest.param(
            "  Gewone   tekst  \n  met   regels  ", "Gewone tekst\nmet regels", id="strip"
        ),
        pytest.param("<p>Een</p>\n\n\n\n<p>Twee</p>", "Een\nTwee", id="layout-whitespace-ignored"),
        pytest.param("<p>Een</p><p></p><p></p><p>Twee</p>", "Een\nTwee", id="empty-paragraphs"),
        pytest.param(
            "<div>\n\t<p>\n\t\tIngesprongen\n\t</p>\n</div>", "Ingesprongen", id="indented"
        ),
        pytest.param(
            "<p>Regel een<br>\n   Regel twee</p>", "Regel een\nRegel twee", id="br-indent"
        ),
        pytest.param("a<br/>  \n  b", "a\nb", id="br-then-blank"),
        pytest.param("a<br><br>b", "a\n\nb", id="double-br-blank-line"),
        pytest.param("a<br><br><br><br>b", "a\n\nb", id="many-br-one-blank-line"),
        pytest.param("<p>Een</p>\n\n\n\n\nTwee", "Een\nTwee", id="newlines-after-block-absorbed"),
    ],
)
def test_html_to_text_whitespace(html: str, expected: str) -> None:
    """Inline whitespace collapses, lines are stripped and blank runs shrink to one."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            "<span>Een</span> <b>twee</b> <em>drie</em> <custom-tag>vier</custom-tag>",
            "Een twee drie vier",
            id="inline-and-unknown-tags",
        ),
        pytest.param('<img src="x.png" alt="alt">Na img', "Na img", id="img"),
        pytest.param('<font color="red">rood</font>', "rood", id="font"),
        pytest.param("Gewone tekst", "Gewone tekst", id="plain"),
        pytest.param("3 < 4 en 5 > 2", "3 < 4 en 5 > 2", id="plain-comparison"),
        pytest.param("", "", id="empty"),
        pytest.param("   \n\t ", "", id="whitespace-only"),
    ],
)
def test_html_to_text_transparent_and_plain(html: str, expected: str) -> None:
    """Unknown tags are transparent and plain text only gets the whitespace normalisation."""
    assert html_to_text(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            "<p>Niet gesloten<p>Tweede", "Niet gesloten\nTweede", id="unclosed-paragraphs"
        ),
        pytest.param("<b>vet", "vet", id="unclosed-inline"),
        pytest.param("Een</p></div>Twee", "Een\nTwee", id="stray-end-tags"),
        pytest.param("a</li>b", "a\nb", id="stray-li-end"),
        pytest.param("<b><p>x</b></p>y", "x\ny", id="mismatched-nesting"),
        pytest.param("<p>Tekst <", "Tekst <", id="dangling-lt"),
        pytest.param("<div><ul><li>Open", "- Open", id="everything-unclosed"),
    ],
)
def test_html_to_text_malformed(html: str, expected: str) -> None:
    """Malformed markup never loses visible text."""
    assert html_to_text(html) == expected


def test_html_to_text_dangling_end_tag_keeps_text() -> None:
    """A cut-off end tag at the very end does not drop the text before it."""
    assert html_to_text("<p>Tekst</").startswith("Tekst")


def test_html_to_text_realistic_post() -> None:
    """A typical post body comes out as readable lines."""
    html = (
        '<div class="post"><p>Beste ouders,</p>'
        "<p>Morgen gaan we naar het bos.&nbsp;Denk aan:</p>"
        "<ul><li>laarzen</li><li>een <strong>warme</strong> jas</li></ul>"
        '<p>Meer info op <a href="https://example.test/bos">de site</a>.</p>'
        "<p>Groeten,<br>Juf Voorbeeld</p></div>"
    )
    assert html_to_text(html) == (
        "Beste ouders,\n"
        "Morgen gaan we naar het bos. Denk aan:\n"
        "- laarzen\n"
        "- een warme jas\n"
        "Meer info op de site (https://example.test/bos).\n"
        "Groeten,\n"
        "Juf Voorbeeld"
    )


# --- looks_like_html ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "<p>",
        "<p>Tekst</p>",
        "<br/>",
        "<br />",
        "</div>",
        "<P>",
        "<h1>",
        '<a href="https://example.test">x</a>',
        "<p class='x'>",
        "x<b>y",
        "&amp;",
        "&#39;",
        "&#x27;",
        "&eacute;",
        "&nbsp;",
        "Tom &amp; Jerry",
    ],
)
def test_looks_like_html_true(value: str) -> None:
    """A tag or a character entity marks HTML."""
    assert looks_like_html(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain tekst",
        "3 < 4",
        "3 < 4 en 5 > 2",
        "a <b",
        "x > y",
        "5 > 3 > 1",
        "2<3>1",
        "a<=b",
        "if a<b: pass",
        "a & b",
        "&amp",
        "&;",
        "&#;",
        "&notanentity;",
        "<1>",
        "< p>",
        "<p",
        "info@example.test",
    ],
)
def test_looks_like_html_false(value: str) -> None:
    """Comparisons, bare ampersands and unknown entities are plain text."""
    assert looks_like_html(value) is False


# --- looks_cut ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "De rest van de tekst…",
        "De rest van de tekst...",
        "De rest van de tekst… \n\t ",
        "Eerste regel\nTweede regel...",
        "…",
    ],
)
def test_looks_cut_true(value: str) -> None:
    """A text that ends in an ellipsis (trailing whitespace ignored) may have been cut off."""
    assert looks_cut(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Groetjes, juf",
        "Tot morgen.",
        "Wat neem je mee?",
        "Het begint… en het eindigt hier.",
        "Twee punten..",
        "[…] einde",
    ],
)
def test_looks_cut_false(value: str) -> None:
    """Any other ending, an ellipsis in the middle and an empty text are no sign of a cut."""
    assert looks_cut(value) is False


# --- collapse_whitespace --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  a \t b\n\nc  ", "a b c"),
        ("", ""),
        ("   ", ""),
        ("a", "a"),
        ("al een regel", "al een regel"),
        ("a" + chr(0xA0) + "b", "a b"),
        ("a\r\nb", "a b"),
    ],
)
def test_collapse_whitespace(value: str, expected: str) -> None:
    """Every whitespace run becomes one space and the ends are stripped."""
    assert collapse_whitespace(value) == expected


# --- truncate -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        pytest.param("abcde", 5, ("abcde", False), id="len-equals-limit"),
        pytest.param("abcdef", 5, ("abcd…", True), id="len-is-limit-plus-one"),
        pytest.param("abcd  f", 6, ("abcd…", True), id="rstrip-before-ellipsis"),
        pytest.param("ab    ", 3, ("ab…", True), id="trailing-spaces-cut"),
        pytest.param("abcdef", 1, ("…", True), id="limit-one-cut"),
        pytest.param("a", 1, ("a", False), id="limit-one-fits"),
        pytest.param("", 1, ("", False), id="empty"),
        pytest.param("ab", 2, ("ab", False), id="fits-exactly"),
        pytest.param("abc", 2, ("a…", True), id="cut-to-two"),
        pytest.param("a b", 2, ("a…", True), id="space-before-cut-removed"),
    ],
)
def test_truncate(value: str, limit: int, expected: tuple[str, bool]) -> None:
    """The result fits the limit and ends with one ellipsis only when cut."""
    result = truncate(value, limit)
    assert result == expected
    assert len(result[0]) <= limit


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_truncate_rejects_non_positive_limit(limit: int) -> None:
    """A limit below one cannot hold an ellipsis and is a programming error."""
    with pytest.raises(ValueError, match="limit"):
        truncate("abc", limit)


def test_truncate_never_exceeds_limit() -> None:
    """Across lengths and limits the result fits, keeps a prefix and marks the cut."""
    for length in range(25):
        value = ("ab cd " * 5)[:length]
        for limit in range(1, 13):
            text, cut = truncate(value, limit)
            assert len(text) <= limit
            assert cut is (len(value) > limit)
            if cut:
                assert text.endswith(ELLIPSIS)
                assert value.startswith(text[:-1])
                assert not text[:-1].endswith(" ")
            else:
                assert text == value
