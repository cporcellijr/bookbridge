---
hide:
  - toc
description: "When a transcript is in the wrong language or picks up music, or an alignment is refused: what to check, and how to retry without losing work."
---

# Transcription problems

Most books never need transcription: BookBridge lines the audio up with the ebook directly. A book falls back to Whisper transcription when it is not in English, its audio does not follow the text closely enough, or its chapters are read out of order. This page covers what to do when that transcript, or the alignment built from it, goes wrong.

## 1. Check the transcription settings

**Settings → Alignment → Transcription**

- **Whisper Language:** leave it blank to detect the language of each part, or enter a code such as `ru` to ask for Russian. It does not translate.
- **Skip music and silence:** off by default. Turn it on when music or silence comes out as words, then check that quiet narration is still transcribed.

Both apply to audio transcribed from now on; a transcript that is already saved is reused. The log shows the language asked for and the language detected for each part. For Russian and other Cyrillic languages, mostly Latin output logs a warning. Names and quotes can be Latin, so treat it as a hint, not proof.

## 2. When an alignment is refused

Expand **Alignment needs attention** on the book. It shows how much of the ebook and the transcript match. BookBridge refuses a map that would put you in the wrong place; a different edition, partial narration, transcription errors and a different language all lower the match.

- Check that the ebook is the same edition as the audiobook, and fix the pairing if it is not.
- Then use **Remap alignment**. The finished transcript is kept, so a remap does not run Whisper again when the transcript has usable word timings.
- Keep the book in BookBridge while you fix it: removing a book also deletes its saved transcript.

Don't lower the content-match threshold to force a map through. An ebook of seven books paired with an audiobook of three needs an ebook of the narrated books instead.

## 3. When the ebook is refused

- A file that is not an EPUB, or an EPUB with no readable text, stops processing before transcription. The error names the file. Choose the right ebook or replace the unusable copy, then retry or use **Remap alignment**.
- PDF, MOBI and AZW3 attachments in Audiobookshelf are skipped. A Calibre-Web Automated ebook you chose wins over an Audiobookshelf attachment.
- Automatic Calibre-Web Automated lookup needs exactly one book matching the title and author. When several match, choose the book yourself.

## 4. Retry the transcription itself

When the transcript itself is wrong:

1. Change the settings in step 1 first.
2. Copy the book's transcript, `<DATA_DIR>/audio_cache/<book-id>/_progress.json`, to somewhere outside that folder.
3. Delete only the original `_progress.json`, then retry the book or use **Remap alignment**.

With forced alignment (CTC) switched off, this runs a full transcription, which takes as long as the first one did. Keep your copy until the new transcript works; a transcript that is already deleted cannot be brought back. An interrupted run picks up from the parts already finished, as long as their files are still there.

??? info "Automatic repair of older Calibre-Web Automated mistakes"
    BookBridge repairs some older Calibre-Web Automated pairing failures by itself, but only when it can prove the
    pairing: the book has a numeric CWA ID, the original EPUB is still cached, and a complete word-timed transcript
    matches its text. It works from cached files only and keeps reading progress, history and alignment backups.
    The check is tied to that audiobook and ebook; if either one changes, the book needs a manual review instead.

    A book that cannot be proven shows a manual-review error and stays out of sync. Check the CWA ID, the file
    names, the cached EPUB and the transcript, fix the pairing, then retry or use **Remap alignment**.

??? info "KoSync hash"
    A device's KoSync hash is never replaced just because it differs from the cached EPUB. If you have confirmed
    the hash is wrong, set it from the **KoSync** entry in the book's apps; **Remap alignment** alone does not reset
    it. The old document link is kept, so check it under **Reader Documents** on Discover's Search tab. Clearing
    reading progress does not repair an input file.
