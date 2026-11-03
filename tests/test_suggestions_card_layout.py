"""Issue #381 - a long title hid the source badge on the candidate cards.

The reporter had the same book in ABS, BookOrbit and CWA and could not tell the
candidate cards apart, because the badge naming the source was clipped away. Sort
order is not stable either, so position was no guide.

Mechanism (on the retired Add Book page): `.resource-card` is a centred flex
column, and `aspect-ratio: 1/1` pinned its height to its width. Content taller
than that square was centred and sliced at *both* edges - the icon off the top,
the source badge off the bottom. The fix: nothing pins a card's height (a plain
`min-height` floor keeps a short-title card square), the badges sit in their own
row at the top of the card so the source is in the same place on every candidate
- which is what the reporter asked for - and the selection tick sits in the
bottom corner so it cannot cover them.

The Suggestions page (Pair Suggestions review and the Search tab, which replaced
Add Book) builds the same cards in JS, so these guard its CSS and card builders.
CSS cannot be executed here, so these are structural assertions.
"""

import re
import unittest
from pathlib import Path

_SUGGESTIONS = Path(__file__).resolve().parents[1] / "templates" / "suggestions.html"


def _source() -> str:
    return _SUGGESTIONS.read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Return the declaration block for a top-level CSS rule, comments stripped."""
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", _source()
    )
    if not match:
        raise AssertionError(f"CSS rule {selector} not found in suggestions.html")
    return re.sub(r"/\*.*?\*/", "", match.group("body"), flags=re.S)


def _function_body(name: str) -> str:
    source = _source()
    start = source.index(f"function {name}(")
    return source[start:source.index("\n    }\n", start)]


class CandidateCardLayoutTests(unittest.TestCase):
    def test_card_height_is_never_pinned_to_its_width(self):
        """`aspect-ratio: 1/1` is what sliced the badge off a tall card (#381)."""
        for selector in (".ebook-card", ".resource-card"):
            with self.subTest(selector=selector):
                body = _rule(selector)
                self.assertNotIn("aspect-ratio", body, "a pinned aspect ratio clips the badge on a long title (#381)")
                self.assertRegex(body, r"min-height:\s*\d+px", "a pixel floor is what keeps a short-title card square")

    def test_title_is_line_clamped(self):
        for selector in (".ebook-title", ".resource-title"):
            with self.subTest(selector=selector):
                body = _rule(selector)
                self.assertRegex(body, r"-webkit-line-clamp:\s*\d+")
                self.assertIn("-webkit-box-orient: vertical", body)
                self.assertRegex(body, r"flex-shrink:\s*0")

    def test_subtitle_is_clamped_to_one_line(self):
        for selector in (".ebook-subtitle", ".resource-subtitle"):
            with self.subTest(selector=selector):
                body = _rule(selector)
                self.assertIn("white-space: nowrap", body)
                self.assertIn("text-overflow: ellipsis", body)
                self.assertRegex(body, r"flex-shrink:\s*0")

    def test_source_badge_never_shrinks(self):
        """The badge is the whole point of the card for a multi-source library."""
        self.assertRegex(_rule(".source-badge"), r"flex-shrink:\s*0")

    def test_badge_row_never_shrinks_and_can_wrap(self):
        """A card can carry both a source badge and the 'Linked' badge."""
        body = _rule(".card-badges")
        self.assertIn("flex-wrap: wrap", body)
        self.assertRegex(body, r"flex-shrink:\s*0")

    def test_selection_tick_stays_clear_of_the_badge_row(self):
        """The tick is absolutely positioned and paints over whatever is beneath
        it, so it must not sit in the corner the badges occupy."""
        for selector in (".ebook-card.selected::after", ".resource-card.selected::after"):
            with self.subTest(selector=selector):
                body = _rule(selector)
                self.assertIn("position: absolute", body)
                self.assertRegex(body, r"bottom:\s*\d+px")
                self.assertNotRegex(body, r"(^|;)\s*top:", "a top-anchored tick covers the source badge (#381)")

    def test_every_ebook_card_renders_its_source_in_the_badge_row(self):
        """Guard the markup as well as the CSS - a badge that is not emitted
        cannot be un-clipped."""
        body = _function_body("buildEbookCard")
        self.assertIn("badges.className = 'card-badges';", body)
        self.assertIn("if (source) addBadge(sourceBadgeClass(source), ebookSourceLabel(source));", body)

    def test_badge_row_comes_before_the_title_on_every_card(self):
        """The reporter's ask: the source is in the same place on every candidate,
        not trailing a title whose length varies."""
        ebook = _function_body("buildEbookCard")
        self.assertLess(ebook.index("card.appendChild(badges)"), ebook.index("card.appendChild(icon)"))
        storyteller = _function_body("appendStorytellerCards")
        self.assertIn("card.prepend(badges);", storyteller)
        audio_only = _function_body("selectSearchResult")
        self.assertIn("card.innerHTML = '<div class=\"card-badges\">", audio_only)

    def test_full_title_stays_available_on_hover(self):
        """Clamping is only acceptable because the untruncated title is kept."""
        self.assertIn("title.title = displayName;", _function_body("buildEbookCard"))
        self.assertIn("title.title = fullTitle;", _function_body("appendStorytellerCards"))


if __name__ == "__main__":
    unittest.main()
