import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch
import sys
import os
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent))

from bs4.element import Tag

from src.utils.smil_extractor import SmilExtractor

class TestSmilExtractorRobustness(unittest.TestCase):
    def setUp(self):
        self.extractor = SmilExtractor()

    def test_ms_timestamps(self):
        """Verify ms timestamps are converted to seconds correctly."""
        # Test 500ms -> 0.5s
        res = self.extractor._parse_timestamp("500ms")
        self.assertEqual(res, 0.5)

        # Test 1s
        res = self.extractor._parse_timestamp("1s")
        self.assertEqual(res, 1.0)

        # Test 1000ms
        res = self.extractor._parse_timestamp("1000ms")
        self.assertEqual(res, 1.0)

        # Test mixed/invalid
        self.assertEqual(self.extractor._parse_timestamp(""), 0.0)
        self.assertEqual(self.extractor._parse_timestamp("invalid"), 0.0)

    def test_malformed_ms_timestamp_returns_zero(self):
        """Verify non-numeric ms values return 0.0 instead of raising."""
        self.assertEqual(self.extractor._parse_timestamp("abcms"), 0.0)
        self.assertEqual(self.extractor._parse_timestamp("ms"), 0.0)
        self.assertEqual(self.extractor._parse_timestamp("12.34.56ms"), 0.0)

    def test_strip_namespaces(self):
        """Verify namespaces and prefixes are stripped correctly."""
        # Default namespace
        xml = '<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body><par>text</par></body></smil>'
        stripped = self.extractor._strip_namespaces(xml)
        self.assertNotIn('xmlns=', stripped)
        self.assertIn('<smil', stripped)
        
        # Named namespace
        xml = '<smil xmlns:epub="http://www.idpf.org/2007/ops"><epub:text>content</epub:text></smil>'
        stripped = self.extractor._strip_namespaces(xml)
        self.assertNotIn('xmlns:epub=', stripped)
        self.assertNotIn('epub:', stripped) # Check prefix removal in tag
        self.assertIn('<text>', stripped) # Check tag preservation
        self.assertIn('</text>', stripped)

        # Complex mixed
        xml = '<seq epub:textref="foo.html" xmlns:epub="ops">Content</seq>'
        stripped = self.extractor._strip_namespaces(xml)
        self.assertIn('<seq', stripped)
        self.assertNotIn('xmlns:epub=', stripped)
        # Attribute prefixes SHOULD be stripped now to prevent unbound prefix errors
        self.assertNotIn('epub:textref', stripped)
        self.assertIn(' textref=', stripped)
        # That strips the TAG prefix. It does NOT strip attribute prefixes.
        # This is expected behavior for _strip_namespaces as implemented.
        # But we should verification that it handles the TAGS correctly.
        
    def test_url_encoded_filenames(self):
        """Verify URL encoded filenames in SMIL are decoded before reading."""
        # Mock ZipFile
        mock_zf = MagicMock()
        
        smil_content = """
        <smil xmlns="http://www.w3.org/ns/SMIL" version="3.0">
            <body>
                <par>
                    <text src="Chapter%201.html#p1"/>
                    <audio src="audio/ch1.mp3" clipBegin="0s" clipEnd="10s"/>
                </par>
            </body>
        </smil>
        """
        
        # Mock read behavior
        def side_effect(path):
            str_path = str(path).replace('\\', '/')
            if str_path.endswith('.smil'):
                return smil_content.encode('utf-8')
            if str_path == 'OEBPS/Chapter 1.html': # Decoded path
                 return b'<html><body id="p1">Hello World</body></html>'
            if 'Chapter%201.html' in str_path: # Encoded path (Should NOT happen if fix works)
                 return b'<html><body id="p1">FAIL_ENCODED</body></html>'
            raise KeyError(f"File not found: {str_path}")
            
        mock_zf.read.side_effect = side_effect
        mock_zf.getinfo.side_effect = lambda x: True
        
        # We need to ensure _get_text_content logic uses the decoded path.
        # Run _process_smil_absolute
        # Note: path passed to process is the SMIL path.
        segments = self.extractor._process_smil_absolute(mock_zf, "OEBPS/chapter1.smil")
        
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], 'Hello World')
        self.assertNotEqual(segments[0]['text'], 'FAIL_ENCODED')

    def test_text_lookups_search_each_chapter_once(self):
        """Verify span lookups don't search the whole chapter again for every span."""
        html = (
            b'<html><body><p id="p1">One.</p><p id="p2">Two <span id="p3">three</span>.</p>'
            b'<p id="p1">Duplicate.</p></body></html>'
        )
        mock_zf = MagicMock()
        mock_zf.read.side_effect = lambda path: html
        searches = []
        original_find, original_find_all = Tag.find, Tag.find_all

        def find(tag, *args, **kwargs):
            searches.append('find')
            return original_find(tag, *args, **kwargs)

        def find_all(tag, *args, **kwargs):
            searches.append('find_all')
            return original_find_all(tag, *args, **kwargs)

        with patch.object(Tag, 'find', find), patch.object(Tag, 'find_all', find_all):
            texts = [self.extractor._get_text_content(mock_zf, 'OEBPS', f'ch1.html#{fragment}')
                     for fragment in ('p1', 'p2', 'p3', 'missing')]

        self.assertEqual(texts, ['One.', 'Two three .', 'three', None])
        self.assertLessEqual(len(searches), 1)

    def test_xml_parsing_with_prefixes(self):
        """Verify stripped XML is valid and parseable by ElementTree."""
        # This string would cause "unbound prefix" if xmlns:epub is stripped but epub:textref remains
        xml = '<seq epub:textref="foo.html" xmlns:epub="ops">Content</seq>'
        stripped = self.extractor._strip_namespaces(xml)
        
        try:
            root = ET.fromstring(stripped)
            self.assertEqual(root.tag, 'seq')
            # attribute name should be just 'textref' now
            self.assertIn('textref', root.attrib)
            self.assertEqual(root.attrib['textref'], 'foo.html')
        except ET.ParseError as e:
            self.fail(f"Stripped XML failed to parse: {e}")

    def test_malicious_container_entity_is_rejected_without_crash(self):
        """Defused XML should reject entity expansion and return no OPF path."""
        mock_zf = MagicMock()
        mock_zf.read.return_value = b"""<?xml version="1.0"?>
        <!DOCTYPE root [<!ENTITY boom SYSTEM "file:///etc/passwd">]>
        <container><rootfiles><rootfile full-path="&boom;"/></rootfiles></container>
        """

        self.assertIsNone(self.extractor._find_opf_path(mock_zf))

if __name__ == '__main__':
    unittest.main()
