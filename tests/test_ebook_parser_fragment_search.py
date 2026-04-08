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


if __name__ == "__main__":
    unittest.main()
