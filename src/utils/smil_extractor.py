import json
import logging
import re
import zipfile
import urllib.parse
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

# Archive entry where an audio-stripped copy keeps each audio file's length in
# seconds, keyed by its path in the archive.
AUDIO_DURATIONS_ENTRY = "META-INF/bookbridge-audio-durations.json"

# A read-along's last clip in each audio file ends at, or shortly before, the end of
# that file: within 0.1 s in most StoryAlign builds, and 32 s at most in the ones
# measured. An audio file is placed with a length only when its clips end no more
# than UNTIMED_TAIL_SECONDS before that length and no more than OVERRUN_SECONDS past it.
UNTIMED_TAIL_SECONDS = 60.0
OVERRUN_SECONDS = 2.0
# Each audio file a read-along builder re-encodes comes out a little longer than the
# part of the audiobook it holds: 0 to 0.22 s per file in the books measured, 10 s
# over A Naked Singularity's 45 files. Recorded lengths may add up to that much past
# the audiobook's end, but never fall short of it.
PADDING_PER_FILE_SECONDS = 0.25

class SmilExtractor:
    """
    Extracts transcript data from EPUB3 media overlays.
    Matches Storyteller/Whisper JSON format.
    
    VERSION 4 - Simplified approach:
    - Detect if SMIL uses absolute or relative timestamps
    - If absolute: use timestamps directly (most common for professionally produced audiobooks)
    - If relative: stack chapters sequentially
    - Don't skip any SMIL files (front/back matter filtering was causing data loss)
    """
    
    def __init__(self):
        self._xhtml_cache = {}
        self._overlay_ids = {}

    def _strip_namespaces(self, xml_string: str) -> str:
        # Remove default xmlns
        xml_string = re.sub(r'\sxmlns="[^"]+"', '', xml_string)
        # Remove named namespaces (xmlns:foo="bar")
        xml_string = re.sub(r'\sxmlns:[a-zA-Z0-9-]+\s*=\s*"[^"]+"', '', xml_string)
        # Remove tag prefixes (<epub:text> -> <text>)
        xml_string = re.sub(r'<([a-zA-Z0-9-]+):', '<', xml_string)
        xml_string = re.sub(r'</([a-zA-Z0-9-]+):', '</', xml_string)
        # Remove attribute prefixes (epub:textref="foo" -> textref="foo")
        # Match whitespace, then prefix:name=
        xml_string = re.sub(r'(\s)[a-zA-Z0-9-]+:([a-zA-Z0-9-]+\s*=)', r'\1\2', xml_string)
        return xml_string

    def has_media_overlays(self, epub_path: str) -> bool:
        """Check if an EPUB has media overlay (SMIL) files."""
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                opf_path = self._find_opf_path(zf)
                if not opf_path: return False
                
                opf_content = zf.read(opf_path).decode('utf-8')
                root = ET.fromstring(opf_content)
                manifest = root.find('.//{http://www.idpf.org/2007/opf}manifest')
                if manifest is None: return False
                
                for item in manifest.findall('{http://www.idpf.org/2007/opf}item'):
                    if item.get('media-type') == 'application/smil+xml':
                        return True
                return False
        except Exception as e:
            logger.debug(f"Error checking media overlays: {e}")
            return False

    def extract_transcript(self, epub_path: str, abs_chapters: List[Dict] = None, audio_offset: float = 0.0) -> List[Dict]:
        """
        Extract transcript from EPUB SMIL files.
        
        Strategy:
        1. First, detect timestamp mode by sampling SMIL files
        2. If absolute timestamps: use them directly
        3. If relative timestamps: calculate offsets from ABS chapters or stack sequentially
        """
        transcript = []
        self._xhtml_cache = {}
        self._overlay_ids = {}
        
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                opf_path = self._find_opf_path(zf)
                if not opf_path:
                    logger.error(f"❌ Could not find OPF file in EPUB: '{epub_path}'")
                    return []
                
                opf_dir = str(Path(opf_path).parent)
                if opf_dir == '.': opf_dir = ''
                
                opf_content = zf.read(opf_path).decode('utf-8')
                smil_files = self._get_smil_files_in_order(opf_content, opf_dir, zf)
                
                if not smil_files:
                    logger.debug(f"No SMIL files found in EPUB: {epub_path}")
                    return []
                
                logger.info(f"📖 Found {len(smil_files)} SMIL files in EPUB")
                if abs_chapters:
                    logger.info(f"📖 Audio source has {len(abs_chapters)} chapters")

                # Another overlay file can time a span nested in this file's spans, so
                # every file's ids must be known before any text is read.
                self._register_all_overlay_ids(zf, smil_files)

                # Detect timestamp mode
                file_offsets = self._audio_file_offsets(zf, smil_files, abs_chapters)
                timestamp_mode = 'per_audio_file' if file_offsets else self._detect_timestamp_mode(zf, smil_files)
                logger.info(f"⚙️ Detected timestamp mode: {timestamp_mode}")
                
                if timestamp_mode == 'per_audio_file':
                    for smil_path in smil_files:
                        transcript.extend(self._process_smil_with_offset(zf, smil_path, 0.0, file_offsets))
                elif timestamp_mode == 'absolute':
                    # Process all SMIL files with absolute timestamps
                    for idx, smil_path in enumerate(smil_files):
                        segments = self._process_smil_absolute(zf, smil_path)
                        transcript.extend(segments)
                        
                        if logger.isEnabledFor(logging.DEBUG):
                            if idx < 3 or idx == len(smil_files) - 1:
                                if segments:
                                    logger.debug(f"   ✓ {Path(smil_path).name}: {len(segments)} segments ({segments[0]['start']:.1f}s - {segments[-1]['end']:.1f}s)")
                elif timestamp_mode == 'relative':
                    if abs_chapters:
                        logger.info(f"   Using Smart Duration Mapping (Files: {len(smil_files)}, Chapters: {len(abs_chapters)})")
                        transcript = self._process_relative_with_chapters(zf, smil_files, abs_chapters)
                    else:
                        logger.info(f"   Using Sequential Stacking (No audio chapters provided)")
                        transcript = self._process_relative_sequential(zf, smil_files, audio_offset)
                else:
                    # Auto/Smart mode
                    transcript = self._process_auto_sequence(zf, smil_files)
                
                # Sort and deduplicate
                transcript.sort(key=lambda x: (x['start'], x['end']))
                
                # Remove exact duplicates
                seen = set()
                unique_transcript = []
                for seg in transcript:
                    key = (seg['start'], seg['end'], seg['text'])
                    if key not in seen:
                        seen.add(key)
                        unique_transcript.append(seg)
                transcript = unique_transcript
                
                # Post-processing: Clamp to audiobook duration if known
                if abs_chapters:
                    abs_end = float(abs_chapters[-1].get('end', 0))
                    if abs_end > 0:
                        original_count = len(transcript)
                        transcript = [s for s in transcript if s['start'] < abs_end]
                        removed = original_count - len(transcript)
                        if removed > 0:
                            logger.debug(f"   Removed {removed} segments starting after audiobook end ({abs_end:.0f}s)")
                        
                        for s in transcript:
                            if s['end'] > abs_end:
                                s['end'] = min(s['end'], abs_end)
                
                total_segments = len(transcript)
                logger.info(f"📖 SMIL extraction complete: {total_segments} segments from {len(smil_files)} files")
                
                # Diagnostic: check for gaps
                if transcript:
                    self._log_gap_analysis(transcript, abs_chapters)
                
                return transcript
                
        except Exception as e:
            logger.error(f"❌ Error extracting SMIL transcript: {e}", exc_info=True)
            return []

    def _audio_file_offsets(self, zf: zipfile.ZipFile, smil_files: List[str],
                            abs_chapters: Optional[List[Dict]]) -> Dict[str, float]:
        """
        Map each audio file to its start in the audiobook.
        
        Clip times count from the start of their own audio file. A read-along made by
        splitting the audiobook at its chapters has one numbered file per chapter, and
        each file starts at its chapter, on the audiobook's own clock. Otherwise, when
        the archive records every file's length, each file starts where the files
        before it end. Files the aligner gave no clips still take up their place, so
        they're counted from the archive too. Either placement is used only when every
        file's clips fit the length it gives that file. Returns an empty dict when
        neither does.
        """
        names, folders, suffixes = set(), set(), set()
        clip_ends: Dict[str, float] = {}
        for smil_path in smil_files:
            try:
                root = ET.fromstring(self._strip_namespaces(zf.read(smil_path).decode('utf-8')))
            except (KeyError, UnicodeDecodeError, ET.ParseError, DefusedXmlException):
                continue
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            for audio_elem in root.iter('audio'):
                name = self._audio_file_path(smil_dir, audio_elem)
                if name:
                    names.add(name)
                    folders.add(str(Path(name).parent))
                    suffixes.add(Path(name).suffix.lower())
                    clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
                    clip_ends[name] = max(clip_ends.get(name, 0.0), clip_end)
        for entry in zf.namelist():
            if str(Path(entry).parent) in folders and Path(entry).suffix.lower() in suffixes:
                names.add(entry)
        audio_files = sorted(names, key=self._natural_sort_key)
        if len(audio_files) < 2:
            return {}
        audiobook_end = float(abs_chapters[-1].get('end', 0)) if abs_chapters else None

        placements = []
        if abs_chapters and len(audio_files) == len(abs_chapters):
            starts = [float(chapter.get('start', 0)) for chapter in abs_chapters]
            ends = [float(chapter.get('end', 0)) for chapter in abs_chapters]
            if abs(starts[0]) <= 1.0 and all(abs(end - start) <= 1.0 for end, start in zip(ends, starts[1:])):
                placements.append(("one file per chapter", dict(zip(audio_files, starts)),
                                   {name: end - start for name, start, end in zip(audio_files, starts, ends)}))
        recorded = self._recorded_audio_lengths(zf)
        if all(name in recorded for name in audio_files):
            offsets, start = {}, 0.0
            for name in audio_files:
                offsets[name] = start
                start += recorded[name]
            placements.append(("recorded lengths", offsets, {name: recorded[name] for name in audio_files}))

        fitting = []
        for method, offsets, lengths in placements:
            problems = self._placement_problems(audio_files, offsets, lengths, clip_ends, audiobook_end)
            if not problems:
                fitting.append((method, offsets, sum(lengths.values()) - sum(clip_ends.values())))
                continue
            logger.info(f"⚠️ Not placing audio files by {method}: {'; '.join(problems[:3])}"
                        + (f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""))
        if len(fitting) == 2:
            # Chapter marks can sit away from where the files really start, while recorded
            # lengths only drift by their padding, so chapters win only where they agree.
            drift = max(abs(fitting[0][1][name] - fitting[1][1][name]) for name in audio_files)
            if drift > OVERRUN_SECONDS + PADDING_PER_FILE_SECONDS * len(audio_files):
                logger.info(f"⚠️ Not placing audio files by {fitting[0][0]}: "
                            f"its starts are up to {drift:.0f}s from the recorded lengths'")
                fitting = fitting[1:]
        if not fitting:
            return {}
        method, offsets, untimed = fitting[0]
        logger.info(f"📖 Placed {len(audio_files)} audio files by {method} ({untimed:.0f}s of audio has no clips)")
        return offsets

    @staticmethod
    def _placement_problems(audio_files: List[str], offsets: Dict[str, float], lengths: Dict[str, float],
                            clip_ends: Dict[str, float], audiobook_end: Optional[float]) -> List[str]:
        """Why a placement contradicts the clip times; an empty list when it doesn't.

        Files sit back to back, so a file whose clips run past its length overlaps the
        next one, and a file whose clips end well before its length leaves audio that
        nothing times, as a wrong pairing of files and chapters does.
        """
        problems = []
        padding = OVERRUN_SECONDS + PADDING_PER_FILE_SECONDS * len(audio_files)
        last = audio_files[-1]
        for name in audio_files:
            if name not in clip_ends:
                continue
            spare = lengths[name] - clip_ends[name]
            # The last length can end at the audiobook's recorded length, which can fall
            # short of the decoded audio; clips past it can't move another file.
            if spare < -(padding if name == last else OVERRUN_SECONDS):
                problems.append(f"{name} has clips {-spare:.0f}s past its {lengths[name]:.0f}s")
            elif spare > UNTIMED_TAIL_SECONDS:
                problems.append(f"{name} ends {spare:.0f}s before its {lengths[name]:.0f}s")
        # Files with no clips can't be checked one by one, but a wrong length for one
        # of them still moves the files' end away from the audiobook's.
        if audiobook_end:
            past_end = offsets[last] + lengths[last] - audiobook_end
            if past_end < -OVERRUN_SECONDS:
                problems.append(f"the files end {-past_end:.0f}s before the {audiobook_end:.0f}s audiobook")
            elif past_end > padding:
                problems.append(f"the files run {past_end:.0f}s past the {audiobook_end:.0f}s audiobook")
        return problems

    def _recorded_audio_lengths(self, zf: zipfile.ZipFile) -> Dict[str, float]:
        """Audio file lengths an audio-stripped copy recorded, keyed by archive path."""
        try:
            recorded = json.loads(zf.read(AUDIO_DURATIONS_ENTRY))
        except (KeyError, ValueError):
            return {}
        if not isinstance(recorded, dict):
            return {}
        lengths = {}
        for path, seconds in recorded.items():
            try:
                lengths[path] = float(seconds)
            except (TypeError, ValueError):
                continue
        return lengths

    def _audio_file_path(self, smil_dir: str, audio_elem) -> str:
        """The archive path of a clip's audio file; two parts' "01.mp3" are different files."""
        src = urllib.parse.unquote(audio_elem.get('src', ''))
        return self._resolve_path(smil_dir, src) if src else ''

    def _detect_timestamp_mode(self, zf: zipfile.ZipFile, smil_files: List[str]) -> str:
        """
        Detect whether SMIL files use absolute or relative timestamps.
        
        Absolute: timestamps are positions in the full audiobook
        Relative: timestamps start near 0 for each chapter/file
        
        Strategy: Sample multiple SMIL files and look at their timestamp ranges.
        If files have non-overlapping, sequential timestamp ranges → absolute
        If files all start near 0 → relative
        """
        sample_ranges = []  # List of (min_ts, max_ts) for each file
        
        for smil_path in smil_files[:min(10, len(smil_files))]:  # Sample more files
            try:
                smil_content = zf.read(smil_path).decode('utf-8')
                smil_content = self._strip_namespaces(smil_content)
                
                root = ET.fromstring(smil_content)
                
                timestamps = []
                for par in root.iter('par'):
                    audio_elem = par.find('audio')
                    if audio_elem is not None:
                        clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
                        clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
                        timestamps.append(clip_begin)
                        timestamps.append(clip_end)
                
                if timestamps:
                    sample_ranges.append((min(timestamps), max(timestamps)))
            except Exception:
                continue
        
        if len(sample_ranges) < 2:
            return 'absolute'  # Default to absolute (safer - no offset applied)
        
        # Check how many files start near 0
        files_starting_near_zero = sum(1 for min_ts, _ in sample_ranges if min_ts < 30)
        
        # Check if timestamp ranges are sequential (non-overlapping)
        sorted_ranges = sorted(sample_ranges, key=lambda x: x[0])
        sequential_count = 0
        for i in range(1, len(sorted_ranges)):
            if sorted_ranges[i][0] >= sorted_ranges[i-1][1] - 10:  # 10s tolerance
                sequential_count += 1
        
        # If most files don't start near 0 OR ranges are mostly sequential → absolute
        # But if multiple files start near 0, assume auto/mixed to be safe.
        if files_starting_near_zero <= 1 and sequential_count >= len(sorted_ranges) - 2:
            return 'absolute'
        
        # If most files start near 0 → relative
        if files_starting_near_zero >= len(sample_ranges) * 0.7:
            return 'relative'
        
        # Mixed case - use auto/smart sequence
        logger.info(f"   Mixed timestamp patterns: {files_starting_near_zero}/{len(sample_ranges)} start near 0")
        return 'auto'

    def _get_raw_info(self, zf: zipfile.ZipFile, smil_path: str) -> Tuple[float, float, Optional[str]]:
        """Get the min start, max end, and audio src from a SMIL file."""
        try:
            smil_content = zf.read(smil_path).decode('utf-8')
            # Strip namespaces
            smil_content = self._strip_namespaces(smil_content)
            
            root = ET.fromstring(smil_content)
            starts = []
            ends = []
            audio_src = None
            
            for par in root.iter('par'):
                audio = par.find('audio')
                if audio is not None:
                    if audio_src is None:
                        audio_src = audio.get('src')
                    starts.append(self._parse_timestamp(audio.get('clipBegin', '0s')))
                    ends.append(self._parse_timestamp(audio.get('clipEnd', '0s')))
            
            if starts:
                return min(starts), max(ends), audio_src
        except Exception as e:
            logger.warning(f"⚠️ Error parsing raw info for '{smil_path}': {e}", exc_info=True)
            pass
        return 0.0, 0.0, None

    def _process_smil_absolute(self, zf: zipfile.ZipFile, smil_path: str) -> List[Dict]:
        """Process SMIL file using timestamps directly (absolute mode)."""
        segments = []
        try:
            smil_content = zf.read(smil_path).decode('utf-8')
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            
            smil_content = self._strip_namespaces(smil_content)
            
            root = ET.fromstring(smil_content)
            self._register_overlay_ids(root, smil_dir)
            
            for par in root.iter('par'):
                text_elem = par.find('text')
                audio_elem = par.find('audio')
                
                if text_elem is None or audio_elem is None:
                    continue
                
                clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
                clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
                
                text_src = urllib.parse.unquote(text_elem.get('src', ''))
                text_content = self._get_text_content(zf, smil_dir, text_src)
                
                if text_content:
                    segments.append({
                        'start': round(clip_begin, 3),
                        'end': round(clip_end, 3),
                        'text': text_content
                    })
                else:
                    logger.debug(f"       🔍 Text content empty for '{text_src}' (decoded)")

        except Exception as e:
            logger.warning(f"⚠️ Error processing SMIL '{smil_path}': {e}", exc_info=True)
            import traceback
            logger.debug(traceback.format_exc())
        
        return segments

    def _process_relative_with_chapters(self, zf: zipfile.ZipFile, smil_files: List[str], 
                                         abs_chapters: List[Dict]) -> List[Dict]:
        """Process SMIL files with relative timestamps using Smart Duration Mapping."""
        transcript = []
        
        # Skip obvious front matter
        content_smil_files = []
        for path in smil_files:
            filename = Path(path).stem.lower()
            if not self._is_front_matter(filename):
                content_smil_files.append(path)
        
        if len(content_smil_files) == 0:
            logger.warning(f"   ⚠️ ALL SMIL files filtered as front matter!")
            return []

        last_matched_abs_idx = -1
        current_sequential_offset = 0.0
        
        for idx, smil_path in enumerate(content_smil_files):
            # 1. Get SMIL duration
            start_raw, end_raw, _ = self._get_raw_info(zf, smil_path)
            smil_duration = end_raw - start_raw
            
            best_match_idx = -1
            best_offset = current_sequential_offset
            smallest_diff = float('inf')
            
            # 2. Search forward in ABS chapters for a duration match
            # Look ahead up to 6 chapters to account for skipped intro/prologue tracks
            search_start = max(0, last_matched_abs_idx)
            search_end = min(len(abs_chapters), search_start + 6)
            
            for abs_idx in range(search_start, search_end):
                ch = abs_chapters[abs_idx]
                ch_start = float(ch.get('start', 0))
                ch_end = float(ch.get('end', 0))
                ch_duration = ch_end - ch_start
                
                diff = abs(ch_duration - smil_duration)
                
                # If duration matches within 15 seconds, it's a solid hit
                if diff < 15.0 and diff < smallest_diff:
                    smallest_diff = diff
                    best_match_idx = abs_idx
                    best_offset = ch_start

            # 3. Apply the offset
            if best_match_idx != -1:
                if last_matched_abs_idx != -1 and best_match_idx > last_matched_abs_idx + 1:
                    logger.info(f"   ⏭️ Skipped {best_match_idx - last_matched_abs_idx - 1} ABS tracks to find match.")
                
                logger.debug(f"   🔗 Matched SMIL {Path(smil_path).name} ({smil_duration:.1f}s) to Audio Ch {best_match_idx} ({abs_chapters[best_match_idx].get('start', 0):.1f}s) - diff: {smallest_diff:.1f}s")
                last_matched_abs_idx = best_match_idx
                offset = best_offset
                current_sequential_offset = float(abs_chapters[best_match_idx].get('end', 0))
            else:
                logger.warning(f"   ⚠️ No duration match for {Path(smil_path).name} ({smil_duration:.1f}s). Falling back to sequential offset {current_sequential_offset:.1f}s")
                offset = current_sequential_offset
                current_sequential_offset += smil_duration

            segments = self._process_smil_with_offset(zf, smil_path, offset)
            transcript.extend(segments)
            
            if idx < 3 or idx == len(content_smil_files) - 1:
                if segments:
                    logger.debug(f"   ✓ {Path(smil_path).name}: offset={offset:.1f}s, {len(segments)} segments")
        
        return transcript

    def _process_relative_sequential(self, zf: zipfile.ZipFile, smil_files: List[str], 
                                      initial_offset: float) -> List[Dict]:
        """Process SMIL files with relative timestamps, stacking sequentially."""
        transcript = []
        current_offset = initial_offset
        
        for idx, smil_path in enumerate(smil_files):
            segments = self._process_smil_with_offset(zf, smil_path, current_offset)
            
            if segments:
                transcript.extend(segments)
                current_offset = max(s['end'] for s in segments)
            
            if idx < 3 or idx == len(smil_files) - 1:
                if segments:
                    logger.debug(f"   ✓ {Path(smil_path).name}: {len(segments)} segments")
        
        return transcript

    def _process_auto_sequence(self, zf: zipfile.ZipFile, smil_files: List[str]) -> List[Dict]:
        """
        Process SMIL files using "Smart Sequence" logic.
        
        Strategy:
        1. Group files by 'Audio Source' (e.g. part1.mp3, part2.mp3).
        2. Within the same audio source, timestamps are absolute (relative to that file).
        3. When audio source changes, we Stack/Reset the offset.
        """
        transcript = []
        global_offset = 0.0
        
        # State tracking
        current_audio_src = None
        part_offset = 0.0  # The offset for the current "Part" (Audio File)
        part_max_end = 0.0 # The furthest point reached in the current Part
        
        # We need to track the cumulative offset of all PREVIOUS parts
        cumulative_previous_duration = 0.0
        
        for idx, smil_path in enumerate(smil_files):
            # 1. Get structural info
            start_raw, end_raw, audio_src = self._get_raw_info(zf, smil_path)
            
            if audio_src:
                # Normalize audio src (handle minimal path differences)
                audio_src = Path(audio_src).name
            else:
                # If no audio source (e.g. text-only SMIL), ignore it for stacking logic
                # We don't want to break continuity of the current audio file
                pass
            
            # 2. Detect Context Switch (New Audio File)
            # Only trigger switch if we have a valid NEW audio source
            if audio_src:
                if idx == 0:
                    current_audio_src = audio_src
                elif current_audio_src is None:
                    current_audio_src = audio_src
                elif audio_src != current_audio_src:
                    # A new part was detected.
                    logger.info(f"   🔄 Audio source changed at {Path(smil_path).name} ({current_audio_src} -> {audio_src}). Stacking.")
                    
                    # Update cumulative duration with the length of the *previous* part
                    cumulative_previous_duration += part_max_end
                    
                    # Reset Part state
                    current_audio_src = audio_src
                    part_max_end = 0.0
            
            # 3. Calculate Global Offset
            # Global Offset = (All previous parts) + (0 for current part, since it's absolute within itself)
            # Wait, what if we have overlapping SMILs in the SAME part?
            # e.g. SMIL A (0-1000), SMIL B (0-500).
            # They stay as 0-1000 and 0-500.
            # We assume smil timestamps are correct relative to the audio file.
            
            current_offset = cumulative_previous_duration
            
            # 4. Extract Segments
            segments = self._process_smil_with_offset(zf, smil_path, current_offset)
            if segments:
                transcript.extend(segments)
            
            # 5. Update Part State
            # Track the max end timestamp seen IN THIS PART (raw time)
            if end_raw > part_max_end:
                part_max_end = end_raw
            
            if idx < 3 or idx == len(smil_files) - 1:
                seg_len = len(segments) if segments else 0
                logger.debug(f"   ✓ {Path(smil_path).name}: {seg_len} segs (src {audio_src}, raw {start_raw:.1f}-{end_raw:.1f} → abs {start_raw+current_offset:.1f}-{end_raw+current_offset:.1f})")
                
        return transcript

    def _process_smil_with_offset(self, zf: zipfile.ZipFile, smil_path: str, 
                                   offset: float, file_offsets: Optional[Dict[str, float]] = None) -> List[Dict]:
        """Process SMIL file adding an offset to all timestamps, or each audio file's own offset."""
        segments = []
        try:
            smil_content = zf.read(smil_path).decode('utf-8')
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            
            smil_content = self._strip_namespaces(smil_content)
            
            root = ET.fromstring(smil_content)
            self._register_overlay_ids(root, smil_dir)
            
            for par in root.iter('par'):
                text_elem = par.find('text')
                audio_elem = par.find('audio')
                
                if text_elem is None or audio_elem is None:
                    continue
                
                clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
                clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
                par_offset = file_offsets.get(self._audio_file_path(smil_dir, audio_elem), offset) if file_offsets else offset
                
                text_src = urllib.parse.unquote(text_elem.get('src', ''))
                text_content = self._get_text_content(zf, smil_dir, text_src)
                
                if text_content:
                    segments.append({
                        'start': round(clip_begin + par_offset, 3),
                        'end': round(clip_end + par_offset, 3),
                        'text': text_content
                    })
        
        except Exception as e:
            logger.warning(f"⚠️ Error processing SMIL '{smil_path}': {e}", exc_info=True)
            import traceback
            logger.debug(traceback.format_exc())
        
        return segments

    def _log_gap_analysis(self, transcript: List[Dict], abs_chapters: List[Dict] = None):
        """Log analysis of gaps in the transcript."""
        if not transcript:
            return
        
        # Find gaps > 100 seconds
        gaps = []
        for i in range(1, len(transcript)):
            gap = transcript[i]['start'] - transcript[i-1]['end']
            if gap > 100:
                gaps.append((transcript[i-1]['end'], transcript[i]['start'], gap))
        
        if gaps:
            logger.warning(f"⚠️ Found {len(gaps)} gaps > 100s in transcript")
            for end_t, start_t, gap in gaps[:5]:  # Show first 5
                logger.debug(f"   Gap: {end_t:.0f}s - {start_t:.0f}s ({gap:.0f}s)")
        
        # Check coverage
        if abs_chapters:
            abs_end = float(abs_chapters[-1].get('end', 0))
            transcript_end = transcript[-1]['end']
            coverage = (transcript_end / abs_end * 100) if abs_end > 0 else 0
            
            if coverage < 90:
                logger.warning(f"⚠️ Low transcript coverage: {coverage:.1f}% (ends at {transcript_end:.0f}s, audiobook ends at {abs_end:.0f}s)")
            elif coverage > 105:
                logger.warning(f"⚠️ Transcript exceeds audiobook: {coverage:.1f}% (ends at {transcript_end:.0f}s, audiobook ends at {abs_end:.0f}s)")

    def _is_front_matter(self, filename: str) -> bool:
        """Check if filename indicates front matter using word boundary matching."""
        # Use word boundaries to avoid matching 'toc' in 'TOCREF' etc.
        front_patterns = [
            r'\bcontents\b', r'\btoc\b', r'\bcopyright\b', r'\btitle\b', 
            r'\bcover\b', r'\bdedication\b', r'\backnowledgment\b', 
            r'\bpreface\b', r'\bforeword\b', r'\bfm0\b', r'\bfrontmatter\b'
        ]
        return any(re.search(p, filename, re.IGNORECASE) for p in front_patterns)

    def _find_opf_path(self, zf: zipfile.ZipFile) -> Optional[str]:
        try:
            container = zf.read('META-INF/container.xml').decode('utf-8')
            root = ET.fromstring(container)
            for rootfile in root.iter():
                if rootfile.tag.endswith('rootfile'):
                    return rootfile.get('full-path')
        except (KeyError, UnicodeDecodeError, ET.ParseError, DefusedXmlException) as e:
            logger.debug(f"Failed to read OPF path from container.xml: {e}")
        return None

    def _natural_sort_key(self, s):
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', s)]

    def _get_smil_files_in_order(self, opf_content: str, opf_dir: str, zf: zipfile.ZipFile) -> List[str]:
        root = ET.fromstring(opf_content)
        manifest = root.find('.//{http://www.idpf.org/2007/opf}manifest')
        spine = root.find('.//{http://www.idpf.org/2007/opf}spine')
        if manifest is None: return []
        
        smil_items = {}
        content_to_overlay = {}
        
        for item in manifest.findall('{http://www.idpf.org/2007/opf}item'):
            if item.get('media-type') == 'application/smil+xml':
                smil_items[item.get('id')] = item.get('href')
            if item.get('media-overlay'):
                content_to_overlay[item.get('id')] = item.get('media-overlay')
        
        smil_files = []
        seen_smil = set()
        
        if spine is not None:
            for itemref in spine.findall('{http://www.idpf.org/2007/opf}itemref'):
                idref = itemref.get('idref')
                if idref in content_to_overlay:
                    smil_id = content_to_overlay[idref]
                    if smil_id in smil_items and smil_id not in seen_smil:
                        smil_path = self._resolve_path(opf_dir, smil_items[smil_id])
                        smil_files.append(smil_path)
                        seen_smil.add(smil_id)
        
        if not smil_files and smil_items:
            logger.warning("⚠️ Spine media-overlay lookup failed, falling back to natural sort")
            all_smil = [self._resolve_path(opf_dir, href) for href in smil_items.values()]
            smil_files = sorted(all_smil, key=self._natural_sort_key)
        
        valid_files = []
        for smil_path in smil_files:
            for path_variant in [smil_path, smil_path.lstrip('/'), smil_path.replace('\\', '/')]:
                try:
                    zf.getinfo(path_variant)
                    valid_files.append(path_variant)
                    break
                except KeyError:
                    continue
        return valid_files
    
    def _resolve_path(self, base_dir: str, relative_path: str) -> str:
        if not base_dir: return relative_path
        full = str(Path(base_dir) / relative_path)
        parts = []
        for part in full.replace('\\', '/').split('/'):
            if part == '..':
                if parts: parts.pop()
            elif part and part != '.':
                parts.append(part)
        return '/'.join(parts)
    
    def _parse_timestamp(self, ts_str: str) -> float:
        if not ts_str: return 0.0
        ts_str = ts_str.strip()
        if ts_str.endswith('ms'):
            try: return float(ts_str.replace('ms', '')) / 1000.0
            except ValueError: return 0.0
        
        ts_str = ts_str.replace('s', '')
        if ':' in ts_str:
            parts = ts_str.split(':')
            return sum(float(p) * (60 ** i) for i, p in enumerate(reversed(parts)))
        try: return float(ts_str)
        except ValueError: return 0.0
    
    def _get_text_content(self, zf: zipfile.ZipFile, smil_dir: str, 
                          text_src: str) -> Optional[str]:
        if not text_src: return None
        if '#' in text_src: file_path, fragment_id = text_src.split('#', 1)
        else: file_path, fragment_id = text_src, None
        
        full_path = self._resolve_path(smil_dir, file_path)
        
        if full_path not in self._xhtml_cache:
            for variant in [full_path, full_path.lstrip('/'), full_path.replace('\\', '/')]:
                try:
                    content = zf.read(variant).decode('utf-8')
                    self._xhtml_cache[full_path] = self._index_ids(BeautifulSoup(content, 'html.parser'))
                    break
                except KeyError: continue
        
        elements_by_id = self._xhtml_cache.get(full_path)
        if not elements_by_id: return None
        
        if fragment_id:
            element = elements_by_id.get(fragment_id)
            if element:
                # Some aligners nest each word's span inside the next one's, so an
                # element's full text repeats words that other pars already time.
                overlay_ids = self._overlay_ids.get(full_path)
                if overlay_ids:
                    text = ' '.join(self._own_strings(element, overlay_ids))
                else:
                    text = element.get_text(separator=' ', strip=True)
                return re.sub(r'\s+', ' ', text).strip()
        
        return None

    def _index_ids(self, soup) -> Dict[str, object]:
        """Map each id to its first element, so lookups don't search the whole chapter."""
        elements_by_id = {}
        for element in soup.find_all(id=True):
            elements_by_id.setdefault(element['id'], element)
        return elements_by_id

    def _own_strings(self, element, nested_ids: set) -> List[str]:
        """Return the element's text pieces, leaving out elements with their own timing."""
        string_types = element.interesting_string_types or element.MAIN_CONTENT_STRING_TYPES
        # bs4 4.12 gives some elements, such as <rt>, a single class instead of a set.
        if isinstance(string_types, type):
            string_types = (string_types,)
        pieces = []
        stack = list(reversed(element.contents))
        while stack:
            node = stack.pop()
            if isinstance(node, NavigableString):
                if type(node) in string_types and node.strip():
                    pieces.append(node.strip())
            elif isinstance(node, Tag) and node.get('id') not in nested_ids:
                stack.extend(reversed(node.contents))
        return pieces

    def _register_overlay_ids(self, root, smil_dir: str) -> None:
        """Record which element ids in each text file have their own overlay timing."""
        for text_elem in root.iter('text'):
            text_src = urllib.parse.unquote(text_elem.get('src', ''))
            if '#' not in text_src:
                continue
            file_path, fragment_id = text_src.split('#', 1)
            full_path = self._resolve_path(smil_dir, file_path)
            self._overlay_ids.setdefault(full_path, set()).add(fragment_id)

    def _register_all_overlay_ids(self, zf: zipfile.ZipFile, smil_files: List[str]) -> None:
        """Record the timed element ids from every overlay file."""
        for smil_path in smil_files:
            try:
                root = ET.fromstring(self._strip_namespaces(zf.read(smil_path).decode('utf-8')))
            except (KeyError, UnicodeDecodeError, ET.ParseError, DefusedXmlException):
                continue
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            self._register_overlay_ids(root, smil_dir)


def extract_transcript_from_epub(epub_path: str, abs_chapters: List[Dict] = None, 
                               output_path: str = None) -> Optional[str]:
    extractor = SmilExtractor()
    if not extractor.has_media_overlays(epub_path): return None
    
    transcript = extractor.extract_transcript(epub_path, abs_chapters)
    if not transcript: return None
    
    if output_path is None:
        output_path = str(Path(epub_path).with_suffix('.transcript.json'))
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript, f, ensure_ascii=False)
    
    return output_path


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) < 2:
        print("Usage: python smil_extractor.py <epub_file>")
        sys.exit(1)
    extract_transcript_from_epub(sys.argv[1])
