# Choosing the ebook that belongs to an audiobook

For an Audiobookshelf audiobook, create **one mapping in BookBridge**:

1. Open **Discover → Search** (or review a pair suggestion).
2. Select the audiobook from **Audiobookshelf**.
3. Select the corresponding ebook from **BookOrbit** or **Grimmory**.
4. Select the book card. When it has multiple compatible files, **Choose an
   edition** opens in a modal. Pick the exact publication and confirm **Use this
   edition**. A single compatible file is selected directly, without a popup.
5. Add the pair and run the normal match workflow. A ready-made read-along can
   supply SMIL timings; it does not need another read-along generated inside it.

Search groups files into one card per provider book. The edition chooser is also
available on pair suggestions, without forcing a detail request for every book during a
full-library scan. Comic files remain ebook-only: they cannot be paired to audio.
Both Classic and New UI use the same accessible dialog, with theme-aware styling.
Filenames, available file sizes and the provider's primary-file marker distinguish editions.
An explicit media-overlay badge is based on provider metadata, not a filename
guess; Grimmory may not expose that metadata.

Keep the matching text/audio/read-along in one BookOrbit entry if using its native
read-aloud synchronization. Do not also create a competing BookOrbit-audio mapping
for the same ABS pairing, and do not use **Move Audiobooks to BookOrbit** unless
you actually intend to replace ABS as the mapping's audio provider.

## Identity and existing mappings

The persisted selection uses the provider book ID, `ebook_source_file_id` and
exact source filename (`original_ebook_filename`, or `ebook_filename` without an
original). A nullable-column migration leaves all existing mappings unpinned,
with their existing behavior. A selection is pinned only when the provider book
offers more than one ebook file; a book whose only ebook is the chosen file stays
unpinned and keeps following the provider's primary, so replacing that file does
not stop its sync. Rematching a mapping to a different file or provider book
without a new pick clears an old pin. Pinned selections are verified within the
book's freshly fetched metadata for downloads and BookOrbit progress. Missing,
replaced, or ambiguous files fail closed rather than
silently substituting whichever EPUB is now primary. This deliberately does not
claim that a file rename is the same document.

Downloads for hashing, matching, Forge, cached-library acquisition and device
delivery retain that selection. An explicitly different EPUB does not inherit a
manual hash override intended for the old document. Changing the publication may
require rebuilding its alignment: two EPUBs of the same text can have different
DOMs, CFIs and hashes. Existing mappings are not changed just by listing files.

## Grimmory progress limitation

Grimmory exposes alternate-file downloads, but the inspected API exposes merged
book-level EPUB progress, without identifying which EPUB produced its CFI.
BookBridge must not apply a primary EPUB's CFI to an alternate EPUB's DOM.
Therefore alternate Grimmory files can supply text/SMIL for ABS and KoSync, but
Grimmory's native progress reads/writes are skipped for them. For a newly pinned
primary EPUB with another EPUB in the same entry, native progress is also skipped:
the merged CFI could belong to either file. A primary-file change fails closed
until the mapping is reviewed. Full native alternate-
EPUB progress sync needs a file-specific read API in Grimmory.

BookOrbit exposes per-file progress and supports the selected file in both
directions. Its internal mirroring between ebook, read-along and audio remains
BookOrbit's responsibility.

## Other sources

ABS already enumerates its ebook files. CWA resolves a format-specific download;
Kavita's mapped source identity selects the downloadable edition/chapter. This
change does not invent an EPUB-file API for providers that do not expose one.
Local-file selection remains distinct from authenticated provider selection.
