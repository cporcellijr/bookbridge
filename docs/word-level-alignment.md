# Trying word-level alignment (#426)

Whisper word timestamps now survive provider responses, audio-part offsets,
transcript caching and alignment. Storyteller re-anchoring keeps its original
word timing while matching text into BookBridge's EPUB character coordinates.
Segment-only transcripts still use the existing timing estimate. Incomplete,
invalid or out-of-order word records fall back to the segment estimate.

This is the Whisper/lexical pipeline, not a native CTC forced aligner. The matcher
uses overlapping unique 12-word phrases: matching passages can already have an
anchor at nearly every word. Word timestamps improve their timing; they do not
fill every transcription mismatch, repeated passage or edition difference.

## Providers

- **Existing HTTP server:** keep the Whisper.cpp/server provider, URL and model
  settings. Word and segment timestamps are requested together in `verbose_json`.
  Both nested segment words and top-level word arrays are supported. Servers that
  return only segment timestamps retain their previous behavior.
- **Built-in:** select Local Whisper in Settings. `faster-whisper` is already in
  BookBridge's dependencies; the selected model downloads on first use. CPU works
  in the regular image. Local CUDA needs the CUDA image/build and GPU access.

The current local install already uses the server at
`http://host.docker.internal:8000/v1/audio/transcriptions` with model
`distil-large-v3`. No setting change or additional install is needed for that path.
Whisper's supported word-timestamp option is documented in the
[faster-whisper repository](https://github.com/SYSTRAN/faster-whisper#word-level-timestamps).

## Compare an excerpt without replacing a saved alignment

`scripts/compare_word_alignment.py` extracts an excerpt with FFmpeg, runs the real
transcription/cache pipeline, and builds measured and estimated maps in a temporary
SQLite database. It parses the actual EPUB and verifies locator resolution. It
does not connect to readers or open the live database. Temporary audio and maps
are cleaned up; the JSON report contains timestamps and offsets for inspection.

For this local Docker install, from PowerShell in the repository:

```powershell
docker cp scripts/compare_word_alignment.py abs_kosync_enhanced:/tmp/compare_word_alignment.py
docker exec -e TRANSCRIPTION_PROVIDER=whispercpp -e WHISPER_CPP_URL=http://host.docker.internal:8000/v1/audio/transcriptions -e WHISPER_MODEL=distil-large-v3 abs_kosync_enhanced python /tmp/compare_word_alignment.py --audio '/audiobooks/01 Heretic Spellblade/Heretic Spellblade - Book 1.m4b' --epub '/books/Heretic Spellblade - K.D. Robertson/Heretic Spellblade - K.D. Robertson.epub' --start 300 --seconds 90 --output /tmp/word-comparison.json
docker cp abs_kosync_enhanced:/tmp/word-comparison.json ./word-comparison.json
docker exec abs_kosync_enhanced rm -f /tmp/compare_word_alignment.py /tmp/word-comparison.json
```

Change `--start`, `--seconds` and the two paths to compare another passage.
For a built-in comparison, use `-e TRANSCRIPTION_PROVIDER=local`,
`-e WHISPER_MODEL=base`, `-e WHISPER_DEVICE=cpu` and
`-e WHISPER_COMPUTE_TYPE=int8` instead of the server variables. These command-local
environment overrides do not alter saved settings.

Look for `Word timing: N measured, M estimated tokens` in the log. The JSON's
median/max character changes show how much the two maps differ at matched word
times. They are **not measured accuracy**: listen at the reported audio times and
check the corresponding book words. Excerpt boundary anchors are excluded from
the comparison; the whole-book endpoints are artificial for a short excerpt.

## Existing books and deployment

Restart BookBridge to load the source changes. No schema migration or new Python
dependency is required; prebuilt-image installs need an image containing the changes.
Existing alignment maps and completed `_progress.json` caches stay valid and are
not automatically replaced. Re-aligning an old segment-only transcript cannot
recover word timestamps: that book needs fresh transcription first. Storyteller
maps can be rebuilt from existing wordTimeline data without running Whisper again.
Use the isolated excerpt comparison first; do not clear the whole library's cache
just to evaluate this change.

## Optional CTC backend

Build with `--build-arg INSTALL_CTC=true` to include torch and torchaudio, enable
**Use CTC forced alignment** in Settings, and use **Remap alignment** on the book's
reset menu. `CTC_DEVICE=auto` selects CUDA when available. Both model emissions and
target tokens remain on that device through `forced_align`; only the finished path
and scores move to CPU for word-span reduction. This follows the device requirements
in [torchaudio's CUDA implementation](https://github.com/pytorch/audio/blob/main/src/libtorchaudio/forced_align/gpu/compute.cu).
Logs distinguish audio decoding, emission progress and the final alignment device.

For an existing lexical map with matching EPUB length and transcript matches spanning
at least 90% of its recorded audio duration, remapping uses the EPUB chapters containing
those matches. Synthetic head/tail anchors do not count as narration evidence. This
keeps an unnarrated bonus excerpt out of the CTC target without changing canonical EPUB
offsets or the stored full-text length. The final anchor stays at the narrated section's
end. Without this evidence, the backend uses the complete text; automatic matching of
different editions or interior omissions remains outside this boundary selection.

Long books still require substantial host memory even with CUDA. On CPU, maps exceeding
the signed 32-bit back-pointer index limit fall back to lexical alignment before calling
the native implementation. Listen at representative mapped positions before claiming
an accuracy improvement; successful GPU execution alone does not measure accuracy.

Deploy these source fixes with a restart if the running image already includes the CTC
dependencies. Otherwise rebuild with the CTC build argument first. Keep `TORCH_HOME`
under a persisted model-cache directory to reuse the downloaded MMS model.
