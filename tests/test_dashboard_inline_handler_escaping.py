"""Dashboard: clicking Mark Complete did nothing for a title with an apostrophe.

Reported 2026-08-20. The button rendered as

    onclick="markComplete('ebook-1', '{{ mapping.abs_title | e }}')"

and Jinja's `e` filter escapes `'` to `&#39;`. The browser decodes HTML entities
in an attribute *before* compiling the handler, so a title like
"Wanderer's Resolve 6" produced

    markComplete('ebook-1', 'Wanderer's Resolve 6')

which is a JavaScript syntax error. The handler never ran and the click was
silently inert — no dialog, no request, nothing but a console error.

`tojson` is the correct filter for a value crossing into JS: it escapes the
quote as \\u0027, which survives HTML decoding intact. The same file already
used that idiom for openStorytellerModal.
"""

import html
import json
import re
import unittest
from pathlib import Path

from flask import Flask, render_template_string

_INDEX = Path(__file__).resolve().parents[1] / "templates" / "index.html"

# Titles that break naive quoting. The first two are real books in a reporting
# user's library.
_HOSTILE_TITLES = [
    "Wanderer's Resolve 6 - Jamie Rowe",
    "To Valor's Bid",
    'He said "hi" loudly',
    "Tom & Jerry",
    "<script>alert(1)</script>",
    "Both ' and \" together",
]


def _extract_onclick(handler_name: str) -> str:
    """Return the raw onclick attribute source for a handler in index.html."""
    source = _INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"onclick=(?P<quote>[\"'])(?P<body>" + handler_name + r"\(.*?)(?P=quote)",
        source,
        re.S,
    )
    if not match:
        raise AssertionError(f"no onclick found for {handler_name} in index.html")
    return match.group("quote"), match.group("body")


class MarkCompleteHandlerEscapingTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def _render(self, quote, body, title):
        template = f"<button onclick={quote}{body}{quote}></button>"
        with self.app.app_context():
            return render_template_string(
                template, mapping={"abs_id": "ebook-1", "abs_title": title}
            )

    def test_mark_complete_survives_hostile_titles(self):
        """The decoded handler must stay parseable, with the title intact."""
        quote, body = _extract_onclick("markComplete")

        for title in _HOSTILE_TITLES:
            with self.subTest(title=title):
                rendered = self._render(quote, body, title)

                attr = re.search(
                    r"onclick=(?P<q>[\"'])(?P<v>.*?)(?P=q)", rendered, re.S
                ).group("v")
                # The browser decodes entities before compiling the handler.
                as_javascript = html.unescape(attr)

                args = re.fullmatch(
                    r"markComplete\((?P<args>.*)\)", as_javascript.strip(), re.S
                )
                self.assertIsNotNone(
                    args, f"handler is not a single well-formed call: {as_javascript}"
                )
                # Both arguments must be valid JS string literals, and the second
                # must round-trip to exactly the title we passed in. json.loads
                # accepts the same string syntax the JS parser would.
                raw_args = args.group("args")
                parsed = json.loads(f"[{raw_args}]")
                self.assertEqual(parsed[0], "ebook-1")
                self.assertEqual(parsed[1], title)

    def test_mark_complete_uses_tojson_not_the_escape_filter(self):
        """Pin the fix: `| e` inside a quoted JS string is what caused this."""
        _quote, body = _extract_onclick("markComplete")

        self.assertIn("tojson", body)
        self.assertNotRegex(body, r"\|\s*e\s*\}\}")
        self.assertNotRegex(body, r"\|\s*escape\s*\}\}")

    def test_the_old_pattern_would_have_failed_this_test(self):
        """Guards the test itself: the pre-fix markup must not pass silently."""
        broken = "markComplete('{{ mapping.abs_id }}', '{{ mapping.abs_title | e }}')"

        rendered = self._render('"', broken, "Wanderer's Resolve 6")
        attr = re.search(r'onclick="(?P<v>.*?)"', rendered, re.S).group("v")
        as_javascript = html.unescape(attr)

        args = re.fullmatch(
            r"markComplete\((?P<args>.*)\)", as_javascript.strip(), re.S
        )
        with self.assertRaises(Exception):
            json.loads(f"[{args.group('args')}]")


class OtherInlineHandlersTests(unittest.TestCase):
    """Free text must never be quoted into an inline handler by hand.

    Scoped to title/author/name-bearing values on purpose. The other inline
    handlers in this template interpolate identifiers — `abs_id`, a regex-
    slugified `dom_id`, and a UUID/hash `source_id` — none of which can contain a
    quote, so they are not part of this bug class and are deliberately left alone.
    """

    _FREE_TEXT = ("title", "author", "name")

    def test_no_inline_handler_quotes_free_text_into_a_js_string(self):
        source = _INDEX.read_text(encoding="utf-8")

        offenders = [
            match
            for match in re.findall(r"onclick=\"[^\"]*'\{\{[^}]*\}\}'[^\"]*\"", source)
            if any(word in match.lower() for word in self._FREE_TEXT)
        ]

        self.assertEqual(
            [], offenders,
            "interpolate free text via |tojson instead of quoting it inside JS: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
