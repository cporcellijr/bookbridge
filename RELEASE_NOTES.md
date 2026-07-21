# Release Notes - 7.3.1

The headline change is **credential encryption at rest**. Every password, API token, sync key, and session cookie BookBridge stores for you — your own and every reader's — was previously written to `database.db` as the exact text you typed, so a copy of that file or of a backup exposed every account the bridge touches. Those values are now encrypted before they are stored and decrypted only in memory at the moment a credential is used. This release also fixes KOReader sync for readers who share a book file, adds HTTP Basic support for Calibre-Web Automated's KOSync endpoint, and tightens background transcription retries.

Upgrading is automatic — there is nothing to re-enter and no migration to run — but **please read the Operational Notes below about backups and rollback** before updating.

This release does **not** change the BridgeSync KOReader plugin (still 0.5.4), so no plugin re-download is required. Highlight and note sync continues to require the **BridgeSync plugin from 7.1.0 or newer**; standard KOReader/KOSync progress sync works without it.

## Security

- **Stored credentials are now encrypted at rest.** Both stores are covered: each
  reader's own service accounts under Account → My Integrations, and the
  server-wide credentials in Settings — including the Audiobookshelf API token,
  KOSync key, Hardcover token, StoryGraph session cookie, and BookBridge's own web
  session signing key. Values are wrapped with Fernet (AES-128-CBC + HMAC-SHA256)
  before they reach the database.

  On first boot after upgrading, BookBridge generates an encryption key at
  `DATA_DIR/secret.key` with `0600` permissions and rewrites every credential it
  finds still stored in the clear, logging `🔐 Encrypted N plaintext credential(s)
  at rest`. Usernames, server URLs, library IDs, and enable/disable toggles are
  deliberately left readable so an install remains inspectable and supportable.

  Credentials that cannot be decrypted are reported as "not configured" and logged
  by name, rather than being sent to a service as though they were the password.

  By default the key sits beside the database it protects, which defends a leaked
  database file or a copied backup but not a compromised host. Set
  `BOOKBRIDGE_SECRET_KEY` to any long random string to keep the key outside the
  data volume instead. It is intentionally not a Settings-page option and is never
  read from the database — a key stored inside the data it encrypts would defeat
  the purpose. (#336)

## Fixed

- **KOReader sync now recovers when two readers share one document hash.** A book
  already claimed by another reader still returns a privacy-preserving 404, but
  BookBridge now verifies the requesting reader's own EPUB in the background,
  creates that reader's book claim, and allows the next GET/PUT to sync instead of
  leaving them permanently stuck. (#335)
- **External KoSync relays can now use HTTP Basic authentication.** Choose
  **HTTP Basic (Calibre-Web Automated)** in a reader's KOReader / KoSync
  integration when targeting CWA's built-in `/kosync` endpoint; classic KOSync
  header authentication remains the default. (#334)
- **Background transcription retries remain bounded.** Retried jobs preserve their
  attempt count instead of resetting it, and an all-empty Whisper result now
  invalidates its completed cache and retries rather than being reused as a
  successful transcript.
- **A round of reliability fixes.** KOSync null progress is handled as an empty
  state, disabled Audiobookshelf cleanup no longer makes an invalid request, blank
  Grimmory shelf names fall back to `Kobo`, completed slow state fetches are
  retained, and expected missing Grimmory progress no longer emits warning noise.
- **Log and status text renders its intended symbols.** Scan status text and
  Grimmory, Hardcover, database, and ebook-resolution log lines no longer show
  mojibake; obsolete file-boundary banners and patch-history labels were removed
  without changing behavior.

## Operational Notes

No database migration is required for this release, and the BridgeSync KOReader
plugin is unchanged (0.5.4), so no plugin re-download is needed. Restart BookBridge
after updating. Three points specific to credential encryption:

- **Add `secret.key` to your backups.** It lives in the same `/data` volume as the
  database, so a whole-volume backup already captures it. If your routine copies
  `database.db` on its own, that backup is no longer self-sufficient — a database
  restored without its key leaves those credentials unreadable and prompts you to
  re-enter them.
- **Rolling back to an earlier version costs your credentials.** Older BookBridge
  builds do not recognise the encrypted form and will send it to your services as
  the password. If you need to downgrade, restore a pre-upgrade database backup or
  re-enter your credentials in the older version.
- **Building from source rather than pulling the published image?** This release
  adds `cryptography` to `requirements.txt`, so rebuild
  (`docker compose up -d --build`) or run `pip install -r requirements.txt`. A plain
  restart against the old dependencies logs `🔓 Credential encryption UNAVAILABLE`
  and keeps storing credentials in the clear. Users of the published `latest` /
  `dev` images simply pull the new image; the dependency is already in it.
