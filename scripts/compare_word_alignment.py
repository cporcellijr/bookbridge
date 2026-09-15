"""Compare word timing with segment estimates on an excerpt, using temporary data.

Run from the repository with PYTHONPATH=. and the usual TRANSCRIPTION_PROVIDER,
WHISPER_MODEL and WHISPER_CPP_URL environment variables. No live database is opened.
"""

import argparse
import json
import logging
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.ebook_utils import EbookParser
from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber

logger = logging.getLogger(__name__)


def main() -> None:
    """Transcribe an excerpt and compare maps without changing saved book progress."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--epub', type=Path, required=True)
    parser.add_argument('--start', type=float, default=300)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.seconds <= 0:
        parser.error('start must be nonnegative and seconds must be positive')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    with tempfile.TemporaryDirectory(prefix='bookbridge-word-comparison-') as temporary:
        root = Path(temporary)
        excerpt = root / 'excerpt.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(args.start), '-i', str(args.audio),
                        '-t', str(args.seconds), '-ac', '1', '-ar', '16000', str(excerpt)], check=True)
        polisher = Polisher()
        started = time.perf_counter()
        transcriber = AudioTranscriber(root, None, polisher)
        segments = transcriber.process_audio('comparison', [{'local_path': str(excerpt), 'ext': '.wav'}],
                                            expected_duration=args.seconds)
        transcription_seconds = time.perf_counter() - started
        ebook = EbookParser(args.epub.parent, epub_cache_dir=root / 'epubs')
        full_text, _ = ebook.extract_text_and_map(args.epub)
        if not full_text:
            raise ValueError('EPUB has no usable text')
        db = DatabaseService(str(root / 'comparison.db'))
        try:
            alignment = AlignmentService(db, polisher)
            legacy = [{k: v for k, v in seg.items() if k != 'words'} for seg in segments]
            for name, transcript in [('measured', segments), ('estimated', legacy)]:
                if not alignment.align_and_store(name, transcript, full_text):
                    raise ValueError(f'{name} alignment failed')
            points = alignment._get_alignment('measured')
            measured_anchors = [p for p in points if 't_idx' in p]
            estimated_anchors = [p for p in alignment._get_alignment('estimated') if 't_idx' in p]
            anchors = []
            if measured_anchors and estimated_anchors:
                # Outside either map's real anchors, interpolation goes to the
                # whole-book endpoints despite having only an audio excerpt.
                lower = max(measured_anchors[0]['ts'], estimated_anchors[0]['ts'])
                upper = min(measured_anchors[-1]['ts'], estimated_anchors[-1]['ts'])
                anchors = [p for p in measured_anchors if lower < p['ts'] < upper]
            accepted_words = sum(len(alignment._timed_segment_tokens(seg)) for seg in segments)
            samples = []
            for point in anchors:
                old_char = alignment.get_char_for_time('estimated', point['ts'])
                samples.append({'audio_seconds': round(args.start + point['ts'], 3),
                                'word_char': point['char'], 'segment_char': old_char,
                                'difference_chars': old_char - point['char']})
            shifts = [abs(s['difference_chars']) for s in samples]
            roundtrip_errors = [abs(alignment.get_time_for_text('measured', '', char_offset_hint=p['char'])
                                    - p['ts']) for p in anchors]
            locator = ebook.get_locator_from_char_offset(args.epub.name, anchors[len(anchors) // 2]['char']) if anchors else None
            report = {
                'audio': str(args.audio), 'epub': str(args.epub),
                'start_seconds': args.start, 'excerpt_seconds': args.seconds,
                'provider': os.getenv('TRANSCRIPTION_PROVIDER', 'local'),
                'model': os.getenv('WHISPER_MODEL', 'base'),
                'transcription_seconds': round(transcription_seconds, 2),
                'segments': len(segments), 'accepted_timed_words': accepted_words,
                'lexical_anchors': len(measured_anchors), 'compared_anchors': len(anchors),
                'canonical_chars': len(full_text),
                'locator_resolved': locator is not None,
                'max_time_roundtrip_error_seconds': max(roundtrip_errors) if roundtrip_errors else None,
                'median_change_chars': statistics.median(shifts) if shifts else None,
                'max_change_chars': max(shifts) if shifts else None,
                'note': 'Differences measure timing changes, not accuracy against human ground truth. '
                        'Excerpt endpoints and unmatched passages are not accuracy measurements.',
                'samples': samples,
            }
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
            logger.info('Comparison saved to %s: %s timed words, %s anchors; median/max shift %s/%s chars',
                        args.output, accepted_words, len(anchors), report['median_change_chars'], report['max_change_chars'])
            if not accepted_words or not anchors:
                raise RuntimeError('No usable word timings or lexical anchors; see comparison report')
            if locator is None:
                raise RuntimeError('EPUB locator resolution failed; see comparison report')
        finally:
            db.db_manager.close()


if __name__ == '__main__':
    main()
