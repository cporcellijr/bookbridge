# Release Notes — 6.8.0

The headline additions are a second ebook library manager (**BookOrbit**) and an optional local-LLM assistant (**Ollama**), alongside a batch of sync fixes. Existing setups upgrade in place — database migrations and new settings apply automatically.

## Added
- **BookOrbit** — a second ebook/audiobook library manager that works like Grimmory. Optional "Up Next" collection watch auto-matches books you drop onto a shelf. Includes a Grimmory→BookOrbit migration script (no rematching needed).
- **Optional Ollama (local LLM)** — smarter match suggestions and audio↔text alignment rescue. Off by default; falls back to normal behavior if Ollama is unreachable, so it never blocks a sync.
- **Link Storyteller from any dashboard card** — not just ebook-only mappings.
- **Combined KOReader reading stats across devices.**
- **Expanded stats page** with more reading-activity views.

## Changed
- Storyteller-led syncs now count as listening time in Audiobookshelf.

## Fixed
- Safer SQLite journal mode on filesystems where WAL is unreliable (9p, some NFS, certain VM shares).
- Storyteller read-along no longer snaps back in books that navigate by SMIL fragment IDs.
- Fewer false rollbacks from stale or out-of-order KoSync updates.
- More accurate dashboard "out of sync" warnings.

## Cleaned up (for public release)
- Removed a stale debug script, pinned dependencies, dropped a bogus `ffmpeg` pip dependency, fixed license/readme metadata, and neutralized localhost defaults.

## Migration
None required. Database migrations and new settings apply automatically on startup. Switching Grimmory→BookOrbit is optional and does not require rematching — run `scripts/migrate_grimmory_to_bookorbit.py` (dry-run by default; add `--apply`).

## Known limitations
- BookOrbit is newer than Grimmory and less battle-tested; the docs cover it lightly for now.
- Ollama is advanced and opt-in: you supply your own server and models, and match quality depends on the model you choose.
