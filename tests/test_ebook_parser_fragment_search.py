import unittest
from bs4 import BeautifulSoup, Tag
from src.utils.ebook_utils import EbookParser


class TestEbookFragmentSearch(unittest.TestCase):
    def setUp(self):
        self.parser = EbookParser(books_dir=".")

    def test_element_has_id(self):
        html_content = "<html><body><div class='chapter'><img src='x.jpg'/><p id='par1'><span id='sentence1'>First sentence.</span><span id='sentence2'>Second sentence.</span></p></div></body></html>"
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find_all("span")[1]

        fragment_id = self.parser.get_fragment_for_tag(target_tag)
        self.assertEqual(fragment_id, "sentence2")

    def test_element_no_id_but_parent_has_id(self):
        html_content = "<html><body><div class='chapter'><img src='x.jpg'/><p id='par1'><span>First sentence.</span><span>Second sentence.</span></p></div></body></html>"
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find_all("span")[1]

        fragment_id = self.parser.get_fragment_for_tag(target_tag)
        self.assertEqual(fragment_id, "par1")

    def test_element_no_id_but_top_div_has_id(self):
        html_content = "<html><body><div id='chapter1'><img src='x.jpg'/><p><span>First sentence.</span><span>Second sentence.</span></p></div></body></html>"
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find_all("span")[1]

        fragment_id = self.parser.get_fragment_for_tag(target_tag)
        self.assertEqual(fragment_id, "chapter1")

    def test_element_no_id_no_parent_id(self):
        html_content = "<html><body><div class='chapter'><img src='x.jpg'/><p><span>First sentence.</span><span>Second sentence.</span></p></div></body></html>"
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find_all("span")[1]

        fragment_id = self.parser.get_fragment_for_tag(target_tag)
        self.assertIsNone(fragment_id)

    def test_prefers_media_overlay_id_over_innermost(self):
        # Storyteller nesting: outer -sN span is in the SMIL, inner -sentenceN is not.
        html_content = (
            "<html><body><p id='p1'>"
            "<span id='ch1.html-s116'><span id='ch1.html-sentence116'>Why not?</span></span>"
            "</p></body></html>"
        )
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find(id='ch1.html-sentence116')

        fragment_id = self.parser.get_fragment_for_tag(target_tag, valid_ids={'ch1.html-s116'})
        self.assertEqual(fragment_id, "ch1.html-s116")

    def test_innermost_id_when_no_ancestor_in_valid_set(self):
        html_content = (
            "<html><body><p id='p1'>"
            "<span id='outer'><span id='inner'>Text.</span></span>"
            "</p></body></html>"
        )
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find(id='inner')

        fragment_id = self.parser.get_fragment_for_tag(target_tag, valid_ids={'unrelated-id'})
        self.assertEqual(fragment_id, "inner")

    def test_innermost_id_when_valid_set_empty(self):
        html_content = (
            "<html><body><p id='p1'>"
            "<span id='outer'><span id='inner'>Text.</span></span>"
            "</p></body></html>"
        )
        soup = BeautifulSoup(html_content, 'html.parser')
        target_tag = soup.find(id='inner')

        fragment_id = self.parser.get_fragment_for_tag(target_tag, valid_ids=set())
        self.assertEqual(fragment_id, "inner")


if __name__ == "__main__":
    unittest.main()
