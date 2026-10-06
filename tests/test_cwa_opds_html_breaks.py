"""Regression coverage for Calibre-Web's malformed custom review XML (#477)."""

import unittest
from unittest.mock import Mock, patch

from defusedxml import ElementTree as ET
from defusedxml.common import DTDForbidden

from src.api.cwa_client import CWAClient


FEED = '''<feed xmlns="http://www.w3.org/2005/Atom">
<link rel="search" type="application/atom+xml" href="/opds/search/{searchTerms}"/>
<entry><title>Rogue Protocol</title><author><name>Martha Wells</name></author>
<id>urn:uuid:530d7037-9862-4e51-ad81-05c7811cc317</id>
<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
Review: a custom review.<br><br><b>Story</b><br><br>More text.
</div></content>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
href="/opds/download/24962/epub/"/></entry>
<entry><title>Other Book</title>
<id>urn:uuid:11111111-2222-3333-4444-555555555555</id>
<link type="application/epub+zip" href="/opds/download/99/epub/"/></entry>
</feed>'''


class TestCWAOPDSHTMLBreaks(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {
            'CWA_ENABLED': 'true', 'CWA_SERVER': 'http://cwa:8083',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = CWAClient()
        self.client._make_request = Mock(return_value=Mock(status_code=200, text=FEED))

    def test_search_discovery_and_uuid_recover_malformed_review(self):
        with self.assertRaisesRegex(ET.ParseError, 'mismatched tag'):
            ET.fromstring(FEED)
        with self.assertLogs('src.api.cwa_client', level='WARNING') as logs:
            results = self.client.search_ebooks('Rogue Protocol')
            uuid = self.client.get_book_uuid('24962', search_hints=['Rogue Protocol'])
        self.assertEqual([r['id'] for r in results], ['24962', '99'])
        self.assertEqual(results[0]['author'], 'Martha Wells')
        self.assertEqual(results[0]['download_url'], 'http://cwa:8083/opds/download/24962/epub/')
        self.assertEqual(uuid, '530d7037-9862-4e51-ad81-05c7811cc317')
        self.assertTrue(any('Repaired bare HTML breaks' in line for line in logs.output))
        self.assertIsNone(self.client.get_book_uuid('123', search_hints=['Rogue Protocol']))

    def test_valid_xml_is_preserved(self):
        valid = FEED.replace('<br>', '<br/>')
        root = self.client._parse_opds_xml(valid)
        self.assertEqual(len(root.findall('.//{http://www.w3.org/1999/xhtml}br')), 4)

    def test_non_content_errors_remain_rejected(self):
        for feed in (
            FEED.replace('<title>Rogue Protocol</title>', '<title>Broken<br></title>'),
            FEED.replace('type="xhtml"', 'type="text"'),
            FEED.replace('</div></content>', '</div>'),
            FEED.replace('<b>Story</b>', '<b>Story'),
        ):
            with self.subTest(feed=feed), self.assertRaises(ET.ParseError):
                self.client._parse_opds_xml(feed)

    def test_dtd_and_entities_remain_rejected(self):
        hostile = '<!DOCTYPE feed [<!ENTITY unsafe SYSTEM "file:///etc/passwd">]>' + FEED
        with self.assertRaises(DTDForbidden):
            self.client._parse_opds_xml(hostile)

    def test_single_quote_xhtml_attribute(self):
        self.assertEqual(len(self.client._parse_opds(FEED.replace('type="xhtml"', "type='xhtml'"))), 2)
