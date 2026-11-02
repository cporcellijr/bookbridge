# Transcription diagnostics and recovery

Local Whisper options are in **Settings → AI → Transcription**. Settings saved in
the dashboard override compose environment values on an existing install.

- **Whisper Language** (`WHISPER_LANGUAGE`): blank detects each chunk's language;
  a code such as `ru` requests Russian. It does not enable translation.
- **Skip music and silence** (`WHISPER_VAD_FILTER`): off by default. Enable it to
  test whether voice activity detection reduces output over music or silence.
  Check quiet narration first. Word timestamps remain on the original audio
  timeline; trailing music is not mistaken for truncated audio.

Options apply to newly transcribed chunks. A completed saved transcript is reused.
The logs report requested/detected language, confidence and VAD for each chunk.
For Russian and other supported Cyrillic languages, mostly Latin output produces
a warning with segment count and first/last times within that chunk. This is a
diagnostic warning: names and quoted passages can legitimately use Latin text.
VAD and a language pin cannot guarantee a particular model's output.

## When alignment is refused

Expand **Alignment needs attention** on the failed book's dashboard card. The
lexical guard reports ebook/transcript token counts, their ratio, exact overlap
in ebook quartiles, and the share of sampled sequences without an exact match.
These measurements do not prove which text was unnarrated. Different editions,
partial narration, transcription errors and language differences can all reduce
exact overlap. The guard still refuses to publish a misleading map.

Completed transcripts survive alignment refusal and exhausted retries. The cache
is `<DATA_DIR>/audio_cache/<book-id>/_progress.json`. Correct the ebook pairing
and use Remap to re-align without repeating Whisper when valid word timings are
available. Old untimed or invalid caches are re-transcribed. Keep the mapping:
deleting a book still intentionally removes its cached data.

## When ebook input is refused

Companion PDFs, MOBI and AZW3 files are skipped when acquiring an EPUB from ABS.
An explicitly mapped CWA ebook wins over an ABS attachment, including when its
cached copy must be downloaded again. Automatic CWA discovery retries simpler
title queries and requires a unique matching title and author; choose the book
manually when multiple records match.

A non-EPUB file or EPUB with no readable text stops processing before hashing or
transcription. The failed book's error names the file. Correct the selected ebook
or replace its unusable cache copy, then retry/Remap. Preserve any existing timed
transcript before replacing files; deleting the mapping also deletes cached data.

The job scheduler automatically repairs provable legacy CWA input failures. It
requires a numeric mapped CWA ID, the retained original EPUB, and a complete
word-timed transcript with matching text. It can replace a hash proven to belong
to that book's cached ABS companion PDF, or rebuild an actually zero-position map
whose hash already matches the retained EPUB. It uses cached files only, preserves
reader progress/history and existing alignment backups, and resumes sync after a
usable map is published. Only the proven wrong PDF document link is removed; its
document and reader progress rows remain.

Recovery approval records the inspected audiobook and ebook pairing. Retries and
restarts retain that approval; changing either source requires manual review
instead of applying the previous audiobook's cached timings. Older queued repairs
without recorded approval also require review. Word timings must cover the
normalized transcript text in order and stay within their segment bounds.
The map, first backup, activation and job completion are committed together only
while the inspected pairing, job, map version and document ownership still match.
A remap, replacement map or deletion during calculation leaves newer state intact.
Installations need the database migration and a bridge restart; prebuilt images
receive these changes in the next published image.

Suspicious mappings with incomplete proof show a manual-review error and stay out
of sync. Verify the CWA ID, current/original filenames, retained EPUB and transcript,
then correct the pairing and retry/Remap. A legitimate device hash is never replaced
merely because it differs from the cached EPUB. If manual verification proves a
wrong primary hash, use the existing hash-update action; Remap alone does not reset
it. Hash updates retain the old linked document, so review an erroneous link in
KoSync Documents separately. Clearing reader progress does not repair an input file.

## Deliberately retrying transcription

For a failed alignment whose transcript itself is wrong, configure the new
Whisper options first. Back up that book's `_progress.json` outside its audio
cache directory, then remove only the original `_progress.json` and retry the
book/Remap. With CTC disabled, this runs the normal full transcription pipeline.
This can take as long as the original run; retain the backup until the new run
succeeds. An already deleted transcript cannot be recovered by this fix.

There is no arbitrary-span repair or ebook-subset selector. Interrupted jobs can
resume their completed prefix when the chunk files remain. A seven-book ebook
paired with three narrated books needs an ebook matching the narrated material;
fixing language output does not supply the missing narration. Do not lower the
global content-match threshold to bypass that mismatch.
