"""Regression tests for _resolve_spine_item_for_position.

Verifies that the spine-position resolver correctly handles:
- Exact matches inside spine items
- Synthetic separator gaps between spine items (snaps to following item)
- Trailing positions past the last real character (clamps to last item)
- Empty spine maps
- Edge cases at item boundaries

This is a pure-logic test using synthetic spine_map data — no EPUB file needed.
"""

import unittest
from src.utils.ebook_utils import EbookParser


def _spine_item(start, end, spine_index=0, href="spine.html"):
    """Build a minimal spine map entry dict matching extract_text_and_map output."""
    return {
        "start": start,
        "end": end,
        "char_len": end - start,
        "spine_index": spine_index,
        "href": href,
        "content": f"<html><body><p>Item {spine_index} content</p></body></html>",
    }


class TestResolveSpineItemForPosition(unittest.TestCase):
    """Test _resolve_spine_item_for_position with synthetic spine maps."""

    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def test_empty_spine_map(self):
        """Empty spine map returns (None, None)."""
        item, clamped = self.parser._resolve_spine_item_for_position([], 0)
        self.assertIsNone(item)
        self.assertIsNone(clamped)

    def test_exact_match_inside_item(self):
        """Position inside a spine item's [start, end) range returns that item."""
        spine_map = [
            _spine_item(0, 100, spine_index=0, href="chap1.html"),
            _spine_item(101, 300, spine_index=1, href="chap2.html"),
        ]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 150)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 150)

    def test_exact_match_first_item(self):
        """Position inside the first spine item."""
        spine_map = [_spine_item(0, 100, spine_index=0)]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 50)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 0)
        self.assertEqual(clamped, 50)

    def test_position_at_start_of_item(self):
        """Position exactly at item['start'] matches that item."""
        spine_map = [
            _spine_item(0, 50, spine_index=0),
            _spine_item(51, 100, spine_index=1),
        ]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 51)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)

    def test_gap_between_items_snaps_to_following(self):
        """Position in the separator gap between spine items snaps forward."""
        # Suppose item 0 is [0, 100), item 1 is [101, 200).
        # Position 100 is the synthetic separator — should snap to item 1.
        spine_map = [
            _spine_item(0, 100, spine_index=0, href="chap1.html"),
            _spine_item(101, 200, spine_index=1, href="chap2.html"),
        ]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 100)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 101)  # snaps to following item's start

    def test_gap_snaps_to_second_of_three(self):
        """Gap snaps to the correct following item, not the last one."""
        spine_map = [
            _spine_item(0, 100, spine_index=0, href="chap1.html"),
            _spine_item(101, 200, spine_index=1, href="chap2.html"),
            _spine_item(201, 300, spine_index=2, href="chap3.html"),
        ]
        # Position 100 is between item 0 and item 1 — snaps to item 1
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 100)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 101)

    def test_trailing_position_past_last_item(self):
        """Position past the last spine item clamps to the last real character."""
        spine_map = [
            _spine_item(0, 50, spine_index=0),
            _spine_item(51, 100, spine_index=1),
        ]
        # Past end of last item
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 999)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 99)  # last['end'] - 1

    def test_trailing_position_exact_end_clamps(self):
        """Position exactly at the last item's end clamps one before."""
        spine_map = [_spine_item(0, 100, spine_index=0)]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 100)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 0)
        self.assertEqual(clamped, 99)  # end - 1

    def test_negative_position_clamps_to_start(self):
        """Negative position snaps to first item's start."""
        spine_map = [_spine_item(10, 100, spine_index=0)]
        # Negative doesn't match [10, 100) and is not past the last item,
        # so it hits the gap-snap path — positive infinity in practice.
        # Actually: target_index < item['start'] for item 0 → snaps to item 0 start
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 5)
        self.assertIsNotNone(item)
        # 5 < item['start'] (10) → gap snap to first item
        self.assertEqual(item["spine_index"], 0)
        self.assertEqual(clamped, 10)

    def test_zero_length_spine_item(self):
        """Zero-length spine items don't match any position but don't crash."""
        spine_map = [
            _spine_item(0, 0, spine_index=0),  # zero-length
            _spine_item(0, 100, spine_index=1),
        ]
        # This scenario is unusual but shouldn't crash
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 50)
        self.assertIsNotNone(item)

    def test_gap_skips_zero_length_spine_item(self):
        """Separator gaps snap to the following item containing real text."""
        spine_map = [
            _spine_item(0, 100, spine_index=0),
            _spine_item(101, 101, spine_index=1),
            _spine_item(102, 200, spine_index=2),
        ]

        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 100)

        self.assertEqual(item["spine_index"], 2)
        self.assertEqual(clamped, 102)

    def test_trailing_empty_spine_item_clamps_to_last_real_character(self):
        """Trailing empty documents never become the end-of-book locator target."""
        spine_map = [
            _spine_item(0, 100, spine_index=0),
            _spine_item(101, 101, spine_index=1),
        ]

        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 101)

        self.assertEqual(item["spine_index"], 0)
        self.assertEqual(clamped, 99)

    def test_only_item_gap_position(self):
        """Single-item spine map with gap-like position snaps to that item's start."""
        spine_map = [_spine_item(10, 100, spine_index=0)]
        # Position 5 is before item start → gap snap to item's start
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 5)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 0)
        self.assertEqual(clamped, 10)

    def test_last_cover_spine_item_never_selected_by_default(self):
        """A trailing position past all items clamps to the last real character,
        not blindly to the last spine item. This specifically protects against
        the original bug: spine_map[-1] fallback for synthetic separators."""
        spine_map = [
            _spine_item(0, 200, spine_index=0, href="content.html"),
            _spine_item(201, 250, spine_index=1, href="cover.html"),
        ]
        # Position at the separator after item 0 (position 200) — EXPECT item 1
        # (the following non-empty item), NOT item 0 just because spine_map[-1] is cover.
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 200)
        self.assertIsNotNone(item)
        # 200 is between item 0 ends (200) and item 1 starts (201) → snaps to item 1
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 201)


class TestResolveSpineItemWithRealisticSpine(unittest.TestCase):
    """Test with a spine map shaped like a real book (multi-chapter)."""

    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def test_mid_book_position(self):
        """Position in the middle of a multi-chapter book resolves correctly."""
        spine_map = [
            _spine_item(0, 5000, spine_index=0, href="title.html"),
            _spine_item(5001, 15000, spine_index=1, href="chap1.html"),
            _spine_item(15001, 30000, spine_index=2, href="chap2.html"),
            _spine_item(30001, 45000, spine_index=3, href="chap3.html"),
        ]
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 20000)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 2)  # chap2
        self.assertEqual(clamped, 20000)

    def test_separator_between_chapters_snaps_forward(self):
        """The +1 separator character between chapters snaps to the next chapter."""
        spine_map = [
            _spine_item(0, 5000, spine_index=0, href="title.html"),
            _spine_item(5001, 15000, spine_index=1, href="chap1.html"),
            _spine_item(15001, 30000, spine_index=2, href="chap2.html"),
        ]
        # Position 5000 is the separator between title and chap1
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 5000)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)  # chap1, NOT title (last item)
        self.assertEqual(clamped, 5001)

    def test_last_chapter_separator(self):
        """The separator between the last content chapter and cover doesn't pick cover."""
        spine_map = [
            _spine_item(0, 30000, spine_index=0, href="content.html"),
            _spine_item(30001, 30100, spine_index=1, href="cover.html"),
        ]
        # Position 30000 is the separator between content and cover
        # This should snap to cover (the following item) — not the last item blindly.
        # Following item IS cover, that's fine.
        item, clamped = self.parser._resolve_spine_item_for_position(spine_map, 30000)
        self.assertIsNotNone(item)
        self.assertEqual(item["spine_index"], 1)
        self.assertEqual(clamped, 30001)
