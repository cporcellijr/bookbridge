# Release Notes - 7.14.0

**BookBridge can now get the half of a book you're missing and pair it when it arrives,
spread alignment work over other machines, and track progress on Goodreads.** The New
interface gets its own Display page, a hero card for your current book and one
continuous Library grid.

This release also fixes finished books being pulled back below 100% in Grimmory,
KOReader and BookOrbit, and makes BridgeSync refuse to delete books from your e-reader
when the server briefly can't see them. Update BridgeSync to 0.9.12 on every device.

## What's New

- **Get the missing ebook or audiobook (#585).** When a search on Discover's Search tab
  finds only one half of a book, **Get the ebook**, **Get the audiobook** or **Get both**
  files a request with BookOrbit or finds a release in Shelfmark, with your own login.
  When the file arrives, even days later, BookBridge pairs it with exactly that book;
  anything uncertain goes to Discover's review. **My requests** and Processing follow
  each request to the end. Admins can always use it; other readers need **Let readers
  get missing books** switched on.

- **Alignment workers.** Run the same image with `BOOKBRIDGE_MODE=worker` on another
  machine to work through the alignment queue. This instance still checks and stores
  every map, and workers never see reading positions or credentials. Contributed by
  [@eschatus](https://github.com/eschatus).

- **Pick the ebook edition** when a BookOrbit or Grimmory book holds more than one file.
  Discover shows an edition chooser, and the chosen file is used everywhere from then
  on. Contributed by [@monkeCode](https://github.com/monkeCode).

- **Goodreads progress tracking**, off by default. Like Hardcover and StoryGraph, it posts
  progress, page count and finished books, through your own signed-in Goodreads session.

- **BookOrbit API keys.** Connect with a BookOrbit 3.8 API key instead of a password,
  which makes SSO-only BookOrbit installs work (#482).

- **New interface: Display page, hero card and one Library grid.** Display (in the
  account menu) holds the theme (now following your device by default), Library style
  (Cards, Face-out shelves, Spine shelves or List, with a shelf finish), cover size and
  what each cover shows, all with a live preview of your own books. A hero card above
  the Library shows the book you're on, where it last moved and how far each app has
  got. The Library is one grid sorted by reading status, with lighter covers and fewer
  background requests.

- **Smaller additions:** remove several books at once in Classic, and CTC alignment
  jobs show progress during the model pass (contributed by
  [@hnrydvd](https://github.com/hnrydvd)).

## Fixed

- **Finished books stay finished (#609, #610).** BookBridge no longer writes the
  audiobook's end back as about 98% of the ebook, and *Propagate Completion*'s 100% is
  no longer undone minutes later.
- **Old positions can't pull a book back.** A device left open at an old page, or an
  older KOReader copy, no longer rewinds your other apps (#592, contributed by
  [@chemicalsno](https://github.com/chemicalsno)); an approved rewind survives further
  reading on any copy (#599, contributed by [@jucor](https://github.com/jucor)).
- **KOReader:** short sessions in long books sync again; Calibre/CWA renames no longer
  lose the file (#589); catalog downloads find their progress in filename mode (#590);
  malformed hashes and positions are rejected cleanly (#591). Contributed by
  [@chemicalsno](https://github.com/chemicalsno).
- **BridgeSync 0.9.12:** books are no longer deleted from your device when the server
  briefly can't find them, your own collections are left alone, and reading stats use
  KOReader's real statistics database (#603, contributed by
  [@jeffhammett](https://github.com/jeffhammett)).
- **BookOrbit:** multi-file audiobooks with unmeasured files no longer jump to the last
  file (#601), audio writes recover after a library scan (#602), and regenerating a
  read-along works while the old copy is attached.
- **CWA** no longer re-downloads every book on every poll (#593).
- **Sync reliability:** a failed write to one app is retried, a re-aligned map is used
  straight away, and an audiobook linked to an existing ebook-only book no longer breaks
  later (#597; affected books are repaired on upgrade).
- **Alignment:** unread endnotes no longer force the Whisper path (#604), short cached
  audio is downloaded again, thinking models such as qwen3 work for AI checks (#605,
  contributed by [@eschatus](https://github.com/eschatus)), and large read-aloud EPUBs
  no longer exhaust memory during cover extraction (#595, contributed by
  [@monkeCode](https://github.com/monkeCode)).
- **Readium positions** with URL-encoded chapter names resolve correctly (#594,
  contributed by [@josephyancey](https://github.com/josephyancey)).
- **Discover and Suggestions:** BookOrbit ebooks with a different file name are found
  (#586), and "Clear stale suggestions" keeps your "Never suggest" list.
- **Security:** your Audiobookshelf API key no longer reaches the logs, one person's
  wrong passwords no longer lock others out behind a reverse proxy, changing a password
  signs the account out elsewhere, and a last Classic script-injection path is closed.
- **Also:** Telegram alerts containing `<` arrive again, the New interface's integration
  Test button uses your saved account (#584), and Forge & Match uses the reader's own
  Audiobookshelf collection.

## Upgrading

Back up your `/data` directory, then pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

- **16 database migrations** apply automatically on start (book requests and their
  arrivals, alignment worker leases, chosen ebook editions, Goodreads matches, BookOrbit
  statistics receipts, the synopsis cache and a repair for #597).
- **Everyone signs in once** after updating, because of the password-session change.
- **Update BridgeSync to 0.9.12 on every device.** It's the version that refuses to
  delete books when the server's list is incomplete.
- The image's health check changed for worker mode. If you build the image yourself,
  rebuild it.

## Operational Notes

- **Getting missing books is admin-only until you switch it on.** Readers need **Let
  readers get missing books**, and each adds their own Shelfmark login.
- **Alignment workers, Goodreads and BookOrbit API keys are all optional.** Nothing
  changes until you set them up.
- **Cache Cleanup only previews now.** **Review Inactive Cache** reports what could be
  removed; deleting from it stays off for now.
- **The docs site is redesigned** at
  [cporcellijr.github.io/bookbridge](https://cporcellijr.github.io/bookbridge/).
- **Known issue (since 7.12.0):** a targeted sync of a book nobody has started, whose
  audiobook narrates its parts in a different order from the EPUB (for example an
  anthology), can move the ebook to the middle of the book. It can be triggered by a
  KoSync update at 0%, **Sync Now**, or a reader app's sync request.

Thanks to [@chemicalsno](https://github.com/chemicalsno),
[@eschatus](https://github.com/eschatus), [@hnrydvd](https://github.com/hnrydvd),
[@jeffhammett](https://github.com/jeffhammett),
[@josephyancey](https://github.com/josephyancey), [@jucor](https://github.com/jucor),
[@monkeCode](https://github.com/monkeCode), and everyone who reported issues for this
release.
