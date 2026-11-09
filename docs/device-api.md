# Device API for Reader Apps

Reader apps that pair an ebook with its audiobook can use three extra routes on the KOSync server: find a book, download its word map, and ask for a sync right away. The word map lets an app highlight each word as the narrator reads it, and jump from the audiobook to the same word in the ebook (and back) without waiting for the next sync.

The routes live beside KOReader's own device-sync routes, so they are served on the KOSync port in split-port mode and use the same sign-in: the `x-auth-user` and `x-auth-key` headers with the reader's KOSync username and password (or its MD5 hash, as KOReader sends it). A device only sees books its reader has matched. Any other book answers `404`, exactly like one that doesn't exist.

Every route is available with and without the `/koreader` prefix.

## Find a book

```
GET /device-sync/books/lookup?source=grimmory&id=110
GET /device-sync/books/lookup?source=bookorbit&id=42
```

Apps usually know their library's ids, not BookBridge's. Pass `source` (`grimmory` or `bookorbit`) and the id of either the ebook or the audiobook; an entry that holds both files matches on either. `?grimmory_id=110` is short for `?source=grimmory&id=110`. BookOrbit ids are looked up among the reader's own BookOrbit matches, since each reader can link a book to their own BookOrbit entries.

```json
{
  "books": [
    {
      "abs_id": "booklore:111",
      "title": "Technopoly",
      "sync_mode": "audiobook",
      "status": "active",
      "ebook_grimmory_id": "110",
      "audio_grimmory_id": "111",
      "ebook_bookorbit_id": null,
      "audio_bookorbit_id": null,
      "has_word_map": true
    }
  ]
}
```

## Download the word map

```
GET /device-sync/books/<abs_id>/word-map
```

| Response | Meaning |
| --- | --- |
| `200` | The map, below. Carries an `ETag`; send it back as `If-None-Match` to get `304` when nothing changed. |
| `202` | Matching hasn't finished. The body has `status: "not_ready"`, the book's `book_status`, and, when a job exists, its `progress` (0–1), `stage` and `last_error`. |
| `409` | The book has no ebook in BookBridge (`no_ebook`), it's an ebook-only book with no audiobook (`no_audio`), or its EPUB is missing, unreadable or its text couldn't be placed in it (`unsupported`). |

```json
{
  "format": 1,
  "abs_id": "booklore:111",
  "status": "ready",
  "map_version": "2026-11-02T16:05:11",
  "align_method": "lexical",
  "quality_score": 0.94,
  "duration": 31872.4,
  "epub": { "filename": "Technopoly.epub", "sha256": "3f1c…" },
  "chapters": [
    { "href": "OEBPS/ch04.xhtml", "spine_index": 4, "spine_step": 8 }
  ],
  "points": [
    [1843.52, 0, "/4/2/14/1:212"],
    [1843.91, 0, "/4/2/14/1:217"]
  ],
  "skipped": 3
}
```

Each point is `[seconds, chapter, path]`: a moment in the audiobook, an index into `chapters`, and the in-document part of an EPUB CFI. The full CFI is `epubcfi(/6/<spine_step>!<path>)`, which for the first point above is `epubcfi(/6/8!/4/2/14/1:212)`. Unlike the positions BookBridge writes to Grimmory, which point at the start of a paragraph, these point at the word itself. The character offset after `:` counts UTF-16 units, the same as a browser's DOM offsets, so it works directly in foliate-js, epub.js or Readium.

Points are in audio order, one per matched word. Seconds count from the start of the whole audiobook: an app that plays a multi-file audiobook (BookOrbit reports its position within the current track) adds the lengths of the earlier tracks. Between two points, the narrator is between those two words. `skipped` counts map points that had no place in the XHTML, such as a gap after a chapter's last word.

The CFIs are for the EPUB named in `epub`. If the app has a different copy of the book (compare `sha256` with the file it downloaded), the positions may not line up and the app should fall back to chapter-level positions.

The map is built the first time it's asked for after each match or re-match, then kept in memory, so that first request for a long book can take several seconds.

## Sync a book now

```
POST /device-sync/books/<abs_id>/sync-now
```

Runs one sync of this book for the device's reader in the background and answers `202` with `{"started": true, "abs_id": "…"}` straight away. An app that has just saved a position to Grimmory, for example when the reader closes the ebook, can call this so the audiobook position is caught up by the time the reader opens it, instead of at the next scheduled check.
