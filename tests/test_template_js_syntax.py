"""Guard against unterminated string literals in template <script> blocks.

Shipped live: a settings.html handler was written with real newlines inside a
double-quoted confirm() string:

    ? "Re-check every book's series?

    This revisits books that already have a series..."

A quoted JS string cannot span lines, so that is a SyntaxError — and one
SyntaxError kills the *entire* script block, silently undefining every function
in it. Both new series buttons did nothing when clicked, no request reached the
server, and the failure was invisible without opening the browser console.

Template JS is never imported by the test suite, so nothing else catches this.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL)


# A "/" starts a regex literal (rather than division) when the previous
# significant character cannot end an expression.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>")


def unterminated_string_lines(js: str) -> list:
    """Return 1-based line numbers where a quoted string runs past end-of-line.

    Understands line/block comments, template literals (which legally span
    lines) and regex literals -- templates/next/_stats_scripts.html contains ``/\\"/g``,
    whose quote is not a string delimiter -- so only real ``'`` and ``"``
    strings are reported.
    """
    findings = []
    i, n, line = 0, len(js), 1
    prev = None
    while i < n:
        ch = js[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                if js[i] == "\n":
                    line += 1
                i += 1
            i += 2
        elif ch == "/" and (prev is None or prev in _REGEX_PRECEDERS):
            # Regex literal: run to the unescaped closing slash, honouring a
            # character class, which may itself contain a slash.
            i += 1
            in_class = False
            while i < n and js[i] != "\n":
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == "[":
                    in_class = True
                elif js[i] == "]":
                    in_class = False
                elif js[i] == "/" and not in_class:
                    break
                i += 1
            i += 1
        elif ch == "`":
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == "`":
                    break
                if js[i] == "\n":
                    line += 1
                i += 1
            i += 1
        elif ch in ("'", '"'):
            quote, start = ch, line
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == "\n":
                    findings.append(start)
                    break
                if js[i] == quote:
                    break
                i += 1
            i += 1
        else:
            i += 1
        if not ch.isspace():
            prev = ch
    return findings


def script_blocks(path: Path) -> list:
    return SCRIPT_BLOCK.findall(path.read_text(encoding="utf-8"))


class TestScannerItself(unittest.TestCase):
    """The scanner has to actually catch the shipped bug, and stay quiet otherwise."""

    def test_catches_a_double_quoted_string_broken_across_lines(self):
        js = '\n'.join([
            "function f() {",
            '    const q = "Re-check every book\'s series?',
            "",
            '    This revisits books.";',
            "}",
        ])
        # A break cascades into following lines; the reported start is what matters.
        self.assertIn(2, unterminated_string_lines(js))

    def test_template_literals_may_span_lines(self):
        js = "const s = `line one\nline two`;\n"
        self.assertEqual(unterminated_string_lines(js), [])

    def test_escaped_newline_sequences_are_fine(self):
        js = 'const s = "line one\\n\\nline two";\n'
        self.assertEqual(unterminated_string_lines(js), [])

    def test_apostrophe_inside_double_quotes_is_fine(self):
        js = 'const s = "every book\'s series";\n'
        self.assertEqual(unterminated_string_lines(js), [])

    def test_escaped_quote_does_not_end_the_string(self):
        js = 'const s = "she said \\"hi\\" loudly";\n'
        self.assertEqual(unterminated_string_lines(js), [])

    def test_regex_literal_containing_a_quote_is_not_a_string(self):
        """templates/stats.html does exactly this in escapeHtml()."""
        js = 'String(v).replace(/\\"/g, "&quot;");\n'
        self.assertEqual(unterminated_string_lines(js), [])

    def test_regex_literal_containing_an_apostrophe_is_not_a_string(self):
        js = "String(v).replace(/'/g, '&#39;');\n"
        self.assertEqual(unterminated_string_lines(js), [])

    def test_regex_character_class_may_contain_a_slash(self):
        js = 'const m = s.match(/[a-z/]+/g);\n'
        self.assertEqual(unterminated_string_lines(js), [])

    def test_comments_are_not_scanned(self):
        js = "// it's fine\n/* and \"this\" too */\nconst a = 1;\n"
        self.assertEqual(unterminated_string_lines(js), [])


class TestShippedTemplatesParse(unittest.TestCase):

    def test_every_template_script_block_has_terminated_strings(self):
        checked = 0
        paths = [
            *sorted(TEMPLATE_DIR.glob("*.html")),
            TEMPLATE_DIR / "next" / "_stats_scripts.html",
            TEMPLATE_DIR / "next" / "_logs_scripts.html",
            TEMPLATE_DIR / "next" / "_settings_scripts.html",
            TEMPLATE_DIR / "next" / "_account_scripts.html",
            TEMPLATE_DIR / "next" / "_account_integrations_scripts.html",
            TEMPLATE_DIR / "next" / "_admin_user_integrations_scripts.html",
        ]
        for path in paths:
            for index, block in enumerate(script_blocks(path)):
                checked += 1
                bad = unterminated_string_lines(block)
                self.assertEqual(
                    bad, [],
                    f"{path.name} script block {index}: unterminated string literal "
                    f"at block line(s) {bad} — this breaks the whole block",
                )
        self.assertGreater(checked, 0, "no script blocks found to check")

    def test_settings_series_handlers_are_defined(self):
        """The two series buttons must reference functions that exist."""
        source = (TEMPLATE_DIR / "settings.html").read_text(encoding="utf-8")
        script = (TEMPLATE_DIR / "next" / "_settings_scripts.html").read_text(encoding="utf-8")
        for handler in ("backfillSeriesMetadata", "refreshSeriesMetadata"):
            self.assertIn(f'onclick="{handler}(this)"', source)
            self.assertIn(f"async function {handler}(", script)


if __name__ == "__main__":
    unittest.main()
