# Transcription diagnostics and recovery

Local Whisper options are in **Settings → AI → Transcription**. Settings saved in
the dashboard override compose environment values on an existing install.

- **Whisper Language** (`WHISPER_LANGUAGE`): blank detects each chunk's language;
  a code such as `ru` requests Russian. It does not enable translation.
- **Filter Non-Speech Audio** (`WHISPER_VAD_FILTER`): off by default. Enable it to
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
