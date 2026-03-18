# BRANCH STATUS: feature/stalin — stalign CLI Integration

## 1. BRANCH CONTEXT

This branch integrates Storyteller's new `stalign` CLI tool as the transcription/alignment backend, replacing the previous direct faster-whisper approach. stalign is Storyteller's standalone alignment pipeline that takes an EPUB + audiobook and produces a readaloud EPUB with Media Overlays.

### 1.1 Architecture

- **Transcription**: `stalign` external binary (replaces in-process faster-whisper)
- **Waterfall**: Storyteller JSON → SMIL extraction → stalign readaloud fallback
- **Engines**: whisper.cpp, openai-cloud, deepgram, whisper-server, google-cloud, microsoft-azure, amazon-transcribe
- **Configuration**: 16 `STALIGN_*` settings in config_loader.py

### 1.2 Key Components

- `src/utils/transcriber.py` — `transcribe_with_stalign()`, `_build_stalign_command()`, `_sanitize_stalign_command()`
- `src/services/forge_service.py` — `_build_auto_forge_transcript()` uses stalign
- `src/sync_manager.py` — Falls back to stalign in transcript waterfall
- `src/utils/config_loader.py` — STALIGN_* settings and legacy migration
- `templates/settings.html` — Engine selection UI with 7 engine branches

## 2. CURRENT OBJECTIVE

- [x] Merge dev branch improvements into feature/stalin
- [x] Main Goal: Fix Booklore Auto-Forge download truncation (55KB issue)
- [x] Context: Audiobooks were downloading as teaser headers, crashing FFmpeg.

## 3. CRITICAL FILE MAP

- `src/api/booklore_client.py`
- `src/services/forge_service.py`
- `BRANCH_STATUS.md`
- `src/utils/transcriber.py` — Core stalign integration
- `src/utils/config_loader.py` — STALIGN_* settings
- `src/services/alignment_service.py` — Transcript source storage + 4-layout Storyteller support
- `src/sync_manager.py` — Transcript waterfall with stalign fallback
- `src/web_server.py` — Settings UI, match routes, restart UX, Booklore cache invalidation
- `templates/settings.html` — stalign engine configuration UI
- `tests/test_stalign_integration.py` — stalign-specific tests

## 4. CHANGE LOG (Newest Top)

- **[2026-03-18 08:41]**: Antigravity - Investigated Grimmory API compatibility. Confirmed `abs-kosync-bridge` endpoints are fully compatible. Found `BookLoreSync-plugin` uses deprecated endpoints (`/by-hash`, `/batch`), but decided to hold off on workarounds until Grimmory's official release.
- **[2026-03-17 17:33]**: Fixed Booklore forge audio 55KB bug. `_copy_booklore_audio_files` for single M4B files (chapter_markers_single_stream mode) was calling `download_audiobook_track(book_id, 0)` which served M4B container stream 0 (cover art/mjpeg, 54KiB) instead of the audio stream. Fixed by routing to new `download_book_to_path` streaming method on `booklore_client` (/books/{id}/download endpoint). Multi-track audiobooks unchanged. 20 tests pass.
- **[2026-03-06]**: Merged dev branch into feature/stalin. 281 tests pass, 4 skipped.

## 5. AI EXECUTION CONSTRAINTS

**CRITICAL RULES ENFORCED BY USER:**

1. **PLANNING PRE-APPROVAL:** The AI is STRICTLY PROHIBITED from executing any commands, making any code changes, or running anything while in PLAN MODE or PLANNING phase until the user explicitly says so and approves the plan.
2. **NO UNAPPROVED COMMITS/PUSHES:** The AI is STRICTLY PROHIBITED from executing `git commit` or `git push` until the user explicitly says so.
3. **WINDOWS COMMAND CHAINING:** The AI is strictly running on Windows PowerShell and MUST use `;` to chain commands instead of `&&`.
4. **PYTHON SCRIPTING:** The AI MUST NOT run multi-line Python scripts using `python -c` in PowerShell as it will hang the process indefinitely. Instead, the AI MUST use `write_to_file` to save a local `.py` script and then execute it via `python script.py`.
5. **TEST EXECUTION:** Always execute pytest through the module flag as `python -m pytest` instead of calling `pytest` directly to ensure the environment `sys.path` is correct.

## 6. EXTERNAL INTEGRATION REFERENCES

- **Repositories:**
  - `https://github.com/WorldTeacher/BookLoreSync-plugin`
  - `https://github.com/grimmory-tools/grimmory`
  - `https://gitlab.com/storyteller-platform/storyteller`
  - `https://github.com/crocodilestick/Calibre-Web-Automated`
  - `https://github.com/calibrain/shelfmark`
  - `https://github.com/advplyr/audiobookshelf`
  - `https://github.com/Kareadita/Kavita`
