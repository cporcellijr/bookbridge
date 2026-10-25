import io
import json
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import MagicMock, patch
import sys
import os
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent))

from bs4 import BeautifulSoup
from bs4.element import Tag

from src.utils.smil_extractor import AUDIO_DURATIONS_ENTRY, SmilExtractor

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

    def test_nested_overlay_spans_keep_only_their_own_words(self):
        """Verify an overlay span nested inside another isn't repeated at the outer span's time."""
        smil = """
        <smil xmlns="http://www.w3.org/ns/SMIL" version="3.0">
            <body>
                <par><text src="ch1.html#w1"/><audio src="a.mp3" clipBegin="10s" clipEnd="12s"/></par>
                <par><text src="ch1.html#w2"/><audio src="a.mp3" clipBegin="12s" clipEnd="13s"/></par>
                <par><text src="ch1.html#w3"/><audio src="a.mp3" clipBegin="13s" clipEnd="15s"/></par>
            </body>
        </smil>
        """
        html = (
            '<html><body><p><span id="s1">'
            '<span id="w3"><span id="w2"><span id="w1"><em>The storm door </em></span>'
            '<em>had this </em></span><em>lock</em><a id="n1">*</a>.</span>'
            '</span></p></body></html>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('OEBPS/ch1.smil', smil)
            zf.writestr('OEBPS/ch1.html', html)
        with zipfile.ZipFile(buffer) as zf:
            segments = self.extractor._process_smil_absolute(zf, 'OEBPS/ch1.smil')

        self.assertEqual(
            [(s['start'], s['text']) for s in segments],
            [(10.0, 'The storm door'), (12.0, 'had this'), (13.0, 'lock * .')],
        )

    def test_nested_overlay_spans_do_not_depend_on_overlay_file_order(self):
        """Verify a nested span is left out of its outer span whichever overlay file is read first."""
        outer = ('<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body>'
                 '<par><text src="ch1.html#w2"/><audio src="a.mp3" clipBegin="12s" clipEnd="13s"/></par>'
                 '</body></smil>')
        nested = ('<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body>'
                  '<par><text src="ch1.html#w1"/><audio src="a.mp3" clipBegin="10s" clipEnd="12s"/></par>'
                  '</body></smil>')
        html = '<html><body><p><span id="w2"><span id="w1">The storm </span>door</span></p></body></html>'
        opf = ('<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
               '<item id="o1" href="1.smil" media-type="application/smil+xml"/>'
               '<item id="o2" href="2.smil" media-type="application/smil+xml"/>'
               '</manifest><spine/></package>')
        container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                     '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')

        for first, second in ((outer, nested), (nested, outer)):
            with tempfile.TemporaryDirectory() as tmp:
                epub = os.path.join(tmp, 'book.epub')
                with zipfile.ZipFile(epub, 'w') as zf:
                    zf.writestr('META-INF/container.xml', container)
                    zf.writestr('OEBPS/content.opf', opf)
                    zf.writestr('OEBPS/1.smil', first)
                    zf.writestr('OEBPS/2.smil', second)
                    zf.writestr('OEBPS/ch1.html', html)
                with patch.object(self.extractor, '_detect_timestamp_mode', return_value='absolute'):
                    segments = self.extractor.extract_transcript(epub)

            self.assertEqual(
                [(s['start'], s['text']) for s in segments],
                [(10.0, 'The storm'), (12.0, 'door')],
            )

    def test_own_strings_accepts_a_single_string_type(self):
        """Verify an element whose string types are one class, as bs4 4.12 gives <rt>, still yields its text."""
        element = BeautifulSoup('<ruby>漢<rt id="r1">kan</rt></ruby>', 'html.parser').find(id='r1')
        element.interesting_string_types = type(element.contents[0])

        self.assertEqual(self.extractor._own_strings(element, set()), ['kan'])

    def test_clip_times_are_placed_by_their_audio_file_chapter(self):
        """Verify each audio file's clip times start at that file's audiobook chapter."""
        opf = """<?xml version="1.0"?>
        <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
            <manifest>
                <item id="c1" href="ch1.html" media-type="application/xhtml+xml" media-overlay="m1"/>
                <item id="c2" href="ch2.html" media-type="application/xhtml+xml" media-overlay="m2"/>
                <item id="m1" href="ch1.smil" media-type="application/smil+xml"/>
                <item id="m2" href="ch2.smil" media-type="application/smil+xml"/>
            </manifest>
            <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
        </package>"""

        def smil(chapter, clips):
            pars = ''.join(
                f'<par><text src="ch{chapter}.html#p{n}"/>'
                f'<audio src="Audio/{audio}" clipBegin="{begin}s" clipEnd="{end}s"/></par>'
                for n, audio, begin, end in clips
            )
            return f'<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body>{pars}</body></smil>'

        def html(numbers):
            return '<html><body>' + ''.join(f'<p id="p{n}">Sentence {n}.</p>' for n in numbers) + '</body></html>'

        # The second text chapter starts in the first audio file and ends in the second.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('META-INF/container.xml',
                        '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            zf.writestr('OEBPS/content.opf', opf)
            zf.writestr('OEBPS/ch1.smil', smil(1, [(1, '00000-00001.m4a', 0, 100), (2, '00000-00001.m4a', 100, 200)]))
            zf.writestr('OEBPS/ch2.smil', smil(2, [(3, '00000-00001.m4a', 200, 300),
                                                   (4, '00000-00002.m4a', 0, 50),
                                                   (5, '00000-00002.m4a', 50, 120)]))
            zf.writestr('OEBPS/ch1.html', html([1, 2]))
            zf.writestr('OEBPS/ch2.html', html([3, 4, 5]))
        chapters = [{'start': 0.0, 'end': 305.0}, {'start': 305.0, 'end': 430.0}]

        transcript = self.extractor.extract_transcript(buffer, chapters)

        self.assertEqual(
            [(s['start'], s['text']) for s in transcript],
            [(0.0, 'Sentence 1.'), (100.0, 'Sentence 2.'), (200.0, 'Sentence 3.'),
             (305.0, 'Sentence 4.'), (355.0, 'Sentence 5.')],
        )

    def test_audio_file_offsets_need_one_file_per_chapter(self):
        """Verify per-file offsets are used only when the audio files and chapters line up."""
        def epub_with(audio_files, archived_audio=()):
            pars = ''.join(
                f'<par><text src="ch1.html#p{n}"/><audio src="{name}" clipBegin="0s" clipEnd="1s"/></par>'
                for n, name in enumerate(audio_files)
            )
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as zf:
                zf.writestr('ch1.smil', f'<smil xmlns="http://www.w3.org/ns/SMIL"><body>{pars}</body></smil>')
                for name in archived_audio:
                    zf.writestr(name, b'')
            return zipfile.ZipFile(buffer)

        two_chapters = [{'start': 0.0, 'end': 10.0}, {'start': 10.0, 'end': 20.0}]
        three_chapters = two_chapters + [{'start': 20.0, 'end': 30.0}]

        self.assertEqual(self._offsets_by_name(epub_with(['a.mp3', 'b.mp3']), ['ch1.smil'], two_chapters),
                         {'a.mp3': 0.0, 'b.mp3': 10.0})
        self.assertEqual(self._offsets_by_name(epub_with(['a.mp3', 'b.mp3']), ['ch1.smil'], three_chapters), {})
        self.assertEqual(self._offsets_by_name(epub_with(['a.mp3', 'a.mp3']), ['ch1.smil'], two_chapters), {})
        self.assertEqual(self._offsets_by_name(epub_with(['a.mp3', 'b.mp3']), ['ch1.smil'], None), {})
        # An audio file the aligner left without clips still takes up its chapter.
        self.assertEqual(
            self._offsets_by_name(
                epub_with(['Audio/a.mp3', 'Audio/c.mp3'], ['Audio/a.mp3', 'Audio/b.mp3', 'Audio/c.mp3']),
                ['ch1.smil'], three_chapters),
            {'a.mp3': 0.0, 'b.mp3': 10.0, 'c.mp3': 20.0})

    def _offsets_by_name(self, *args):
        """Placed offsets keyed by file name, for readable expectations."""
        return {Path(path).name: start for path, start in self.extractor._audio_file_offsets(*args).items()}

    def test_audio_files_sharing_a_name_in_different_folders_are_placed_apart(self):
        """part1/01.mp3 and part2/01.mp3 are different files: keyed by name alone, the second
        part's clips landed on the first part's times and the timings ended at 200s of 400s."""
        files = ['Audio/part1/01.mp3', 'Audio/part1/02.mp3', 'Audio/part2/01.mp3', 'Audio/part2/02.mp3']
        pars = ''.join(f'<par><text src="ch1.html#p{n}"/><audio src="{name}" clipBegin="0s" clipEnd="99s"/></par>'
                       for n, name in enumerate(files))
        html = ''.join(f'<p id="p{n}">Sentence number {n}.</p>' for n in range(len(files)))
        opf = """<?xml version="1.0"?>
        <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
            <manifest>
                <item id="ch1" href="ch1.html" media-type="application/xhtml+xml" media-overlay="ch1_overlay"/>
                <item id="ch1_overlay" href="ch1.smil" media-type="application/smil+xml"/>
            </manifest>
            <spine><itemref idref="ch1"/></spine>
        </package>
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'parts.epub'
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('META-INF/container.xml',
                            '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
                zf.writestr('OEBPS/content.opf', opf)
                zf.writestr('OEBPS/ch1.smil', f'<smil xmlns="http://www.w3.org/ns/SMIL"><body>{pars}</body></smil>')
                zf.writestr('OEBPS/ch1.html', f'<html><body>{html}</body></html>')
                for name in files:
                    zf.writestr(f'OEBPS/{name}', b'')
                zf.writestr(AUDIO_DURATIONS_ENTRY, json.dumps({f'OEBPS/{name}': 100.0 for name in files}))

            segments = SmilExtractor().extract_transcript(str(path))

        self.assertEqual([round(segment['start']) for segment in segments], [0, 100, 200, 300])
        self.assertEqual(round(segments[-1]['end']), 399)

    @staticmethod
    def _numbered_audio_epub(lengths, clip_ends):
        """A read-along with three numbered audio files, each timed up to its clip end."""
        names = ['00000-00001.m4a', '00000-00002.m4a', '00000-00003.m4a']
        pars = ''.join(
            f'<par><text src="ch1.html#p{n}"/><audio src="Audio/{name}" clipBegin="0s" clipEnd="{end}s"/></par>'
            for n, (name, end) in enumerate(zip(names, clip_ends)) if end is not None
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('OEBPS/ch1.smil', f'<smil xmlns="http://www.w3.org/ns/SMIL"><body>{pars}</body></smil>')
            for name in names:
                zf.writestr(f'OEBPS/Audio/{name}', b'')
            if lengths:
                zf.writestr(AUDIO_DURATIONS_ENTRY, json.dumps(
                    {f'OEBPS/Audio/{name}': length for name, length in zip(names, lengths) if length is not None}))
        return zipfile.ZipFile(buffer)

    def test_stored_audio_lengths_place_each_file_whatever_the_chapters(self):
        """Verify recorded audio lengths set each file's start, even when the chapters don't line up."""
        two_chapters = [{'start': 0.0, 'end': 150.0}, {'start': 150.0, 'end': 240.0}]
        three_chapters = [{'start': 0.0, 'end': 121.0}, {'start': 121.0, 'end': 202.0}, {'start': 202.0, 'end': 243.0}]
        placed = {'00000-00001.m4a': 0.0, '00000-00002.m4a': 120.0, '00000-00003.m4a': 200.0}
        clip_ends = [119.9, 79.9, 39.9]

        def offsets(lengths, chapters):
            return self._offsets_by_name(
                self._numbered_audio_epub(lengths, clip_ends), ['OEBPS/ch1.smil'], chapters)

        self.assertEqual(offsets([120.0, 80.0, 40.0], two_chapters), placed)
        self.assertEqual(offsets([120.0, 80.0, 40.0], None), placed)
        # A file with no recorded length falls back to one file per chapter.
        self.assertEqual(offsets([120.0, 80.0, None], three_chapters),
                         {'00000-00001.m4a': 0.0, '00000-00002.m4a': 121.0, '00000-00003.m4a': 202.0})

    def test_a_placement_that_contradicts_the_clip_times_is_not_used(self):
        """Verify each file is placed only when its clips end shortly before the length it is given."""
        chapters = [{'start': 0.0, 'end': 120.0}, {'start': 120.0, 'end': 200.0}, {'start': 200.0, 'end': 240.0}]
        clip_ends = [119.9, 79.9, 39.9]

        def offsets(lengths, chapters, clip_ends=clip_ends):
            return self._offsets_by_name(
                self._numbered_audio_epub(lengths, clip_ends), ['OEBPS/ch1.smil'], chapters)

        by_chapter = {'00000-00001.m4a': 0.0, '00000-00002.m4a': 120.0, '00000-00003.m4a': 200.0}
        # A recorded length 200 s too long, such as from a wrong MP3 header, gives way to the chapters.
        self.assertEqual(offsets([320.0, 80.0, 40.0], chapters), by_chapter)
        # With no chapters to fall back on, nothing is placed.
        self.assertEqual(offsets([320.0, 80.0, 40.0], None), {})
        # Chapters that leave a gap between them aren't one file each.
        gapped = [{'start': 0.0, 'end': 120.0}, {'start': 125.0, 'end': 205.0}, {'start': 205.0, 'end': 245.0}]
        self.assertEqual(offsets(None, gapped), {})
        # Counts that match while the boundaries don't: the second file's clips run past its chapter.
        misaligned = [{'start': 0.0, 'end': 150.0}, {'start': 150.0, 'end': 210.0}, {'start': 210.0, 'end': 240.0}]
        self.assertEqual(offsets(None, misaligned), {})
        # A file whose clips end more than a minute before its chapter does.
        self.assertEqual(offsets(None, chapters, clip_ends=[119.9, 15.0, 39.9]), {})
        # A file with no clips at all can't contradict its chapter.
        self.assertEqual(offsets(None, chapters, clip_ends=[119.9, None, 39.9]), by_chapter)

    def test_recorded_lengths_must_end_with_the_audiobook(self):
        """Verify recorded lengths are used only when they add up to the audiobook's length."""
        two_chapters = [{'start': 0.0, 'end': 200.0}, {'start': 200.0, 'end': 240.0}]
        one_per_file = [{'start': 0.0, 'end': 120.0}, {'start': 120.0, 'end': 200.0}, {'start': 200.0, 'end': 240.0}]

        def offsets(lengths, chapters, clip_ends=(119.9, 79.9, 39.9)):
            return self._offsets_by_name(
                self._numbered_audio_epub(lengths, list(clip_ends)), ['OEBPS/ch1.smil'], chapters)

        # A file with no clips, such as the credits, recorded 10 s short would start every later file early.
        self.assertEqual(offsets([120.0, 70.0, 40.0], two_chapters, clip_ends=(119.9, None, 39.9)), {})
        # Each re-encoded file runs a little long; that adds up and is allowed.
        self.assertEqual(offsets([120.2, 80.2, 40.2], two_chapters),
                         {'00000-00001.m4a': 0.0, '00000-00002.m4a': 120.2, '00000-00003.m4a': 200.4})
        # Running 5 s long over three files is more than that.
        self.assertEqual(offsets([121.7, 81.7, 41.6], two_chapters), {})
        # When the files are one per chapter, the chapter starts are used: they're on the audiobook's own clock.
        by_chapter = {'00000-00001.m4a': 0.0, '00000-00002.m4a': 120.0, '00000-00003.m4a': 200.0}
        self.assertEqual(offsets([120.2, 80.2, 40.2], one_per_file), by_chapter)
        # The last chapter ends at the audiobook's recorded length, which can fall short of the audio in the
        # last file; clips past it can't move any other file. A middle file running that far over still fails.
        self.assertEqual(offsets(None, one_per_file, clip_ends=(119.9, 79.9, 42.6)), by_chapter)
        self.assertEqual(offsets(None, one_per_file, clip_ends=(119.9, 82.6, 39.9)), {})

    def test_chapters_that_disagree_with_recorded_lengths_give_way(self):
        """Verify chapter marks set away from the file boundaries don't override recorded lengths."""
        def offsets(lengths, chapters, clip_ends=(119.9, 79.9, None)):
            return self._offsets_by_name(
                self._numbered_audio_epub(lengths, list(clip_ends)), ['OEBPS/ch1.smil'], chapters)

        # Every mark 8 s late: the first file gains an untimed tail, and the last file, the credits, has no clips.
        shifted = [{'start': 0.0, 'end': 128.0}, {'start': 128.0, 'end': 208.0}, {'start': 208.0, 'end': 240.0}]
        self.assertEqual(offsets([120.0, 80.0, 40.0], shifted),
                         {'00000-00001.m4a': 0.0, '00000-00002.m4a': 120.0, '00000-00003.m4a': 200.0})
        # Chapters that don't start at the beginning of the audiobook aren't one file each.
        late_start = [{'start': 5.0, 'end': 125.0}, {'start': 125.0, 'end': 205.0}, {'start': 205.0, 'end': 245.0}]
        self.assertEqual(offsets(None, late_start, clip_ends=(119.9, 79.9, 39.9)), {})

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
