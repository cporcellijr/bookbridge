# Plan: Goodreads as a write-only tracker

Status: **Phase 1 complete** (2026-09-29). Branch: `local/goodreads-tracker`, local only.
Bring changes from local `dev` into this branch as `dev` advances; do not push this
branch. This integration is for personal use and does not need a public release.

## 1. Goal

Add Goodreads beside StoryGraph and Hardcover as a third write-only tracker. When a
linked book moves, BookBridge keeps its Goodreads shelf (want to read, currently
reading, read) in step and posts a percentage progress update. Goodreads never
leads a sync. Fable is out of scope.

The approach comes from [Lyfts/ShelfSync](https://github.com/Lyfts/ShelfSync) (MIT),
a KOReader plugin that already syncs Goodreads. Its Lua is a reference for endpoints
and quirks; the Python here is written fresh.

## 2. How Goodreads works (from ShelfSync, to be confirmed live)

Goodreads has no public API. ShelfSync, like BookBridge's StoryGraph client, replays a
logged-in browser session against the classic Rails pages.

| Need | Request | Notes |
|---|---|---|
| Session check, CSRF token, user id | `GET /` | Only the classic homepage has `<meta name="csrf-token">` and a `/user/show/<id>` nav link. One GET primes every write. |
| Search | `GET /search?q=<text>` | The live 2026-09-29 Next.js page has one `Book` card per result, with `BookCard__stretchedLink`, `book-item-title`, and `book-item-contributors` in the same card. An exact single match (e.g. ISBN) can redirect to `/book/show/<id>`, even as a `200` with a `Location` header when signed in (ShelfSync behavior, still to confirm here). |
| Book metadata | `GET /book/show/<id>` | `<script type="application/ld+json">` holds a schema.org `Book`: name, image, author, and `aggregateRating` with `ratingValue`, `ratingCount`, and `reviewCount` (confirmed live). |
| Viewer's shelf | same page | Apollo cache: `"viewerShelving":{"__ref":"Shelving:..."}`, then find the dereferenced object for `shelf.name`. Only `to-read`, `currently-reading` and `read` can be read back. |
| Page count | same page | `"details":{"__typename":"BookDetails",...,"numPages":N}` |
| Set shelf | `POST /shelf/add_to_shelf` | Form fields `book_id`, `name=<shelf>`, `a=`. Headers: `X-CSRF-Token`, `X-Requested-With: XMLHttpRequest`, `Origin`, `Sec-Fetch-Mode: cors`. |
| Post progress | `POST /user_status.json` | Fields `user_status[book_id]`, `user_status[percent]` (or `[page]`), `user_status[body]`. Same XHR headers. |
| Remove from shelves | `POST /review/destroy/<book_id>` | Form with `authenticity_token`. Not used by sync; listed for completeness. |

Quirks that must be carried over:

- **Cookie bundle.** A session is about 13 cookies across goodreads.com and Amazon.
  Store and replay the whole raw `Cookie` header as one opaque string.
- **Browser-shaped headers.** GETs need `Referer`, `Sec-Fetch-*` and
  `Upgrade-Insecure-Requests` like a real navigation. Without them Goodreads loops on
  self-redirects.
- **Session bootstrap redirect.** A redirect can carry `Set-Cookie` for
  `_session_id2`, and the next hop must send that cookie back or the redirect repeats
  forever. Follow redirects by hand for GETs, merging `Set-Cookie` into the jar each hop.
- **AWS WAF.** A `x-amzn-waf-action` response header means the bot challenge rejected
  the session. Retrying with the same cookie never helps (see §3).
- **Sign-in redirect.** `401`, or a `Location` containing `sign_in`/`signin`, means the
  session is dead.

## 3. Cookie refresher

Goodreads' WAF periodically rejects a saved cookie, so the cookie refresher is part of
the design from the start, not a later add-on.

**What it is.** [Lyfts/goodreads-cookie-refresher](https://github.com/Lyfts/goodreads-cookie-refresher)
runs two containers: `selenium/standalone-chrome`, with a noVNC view on port 7900 for
a one-time interactive login (Amazon 2FA works), and a small Flask service that keeps
that Chrome session alive, revisits goodreads.com every 20 minutes, and serves the
current cookie:

- `GET /cookie` returns the last captured `Cookie` header as plain text.
- `POST /refresh` forces a fresh browser visit and returns the new cookie.
- `GET /health` returns `{"logged_in", "cookie_count", "last_refresh_seconds_ago"}`.
- With `AUTH_TOKEN` set, every call needs `X-Auth-Token` (or `?token=`).

**Licence.** That repo has no licence file, so its code is not vendored or copied into
BookBridge. BookBridge only calls its HTTP API, and users run the published image
`ghcr.io/lyfts/goodreads-cookie-refresher` themselves. The docs link to it; they do not
repackage it.

**How BookBridge uses it** (per user; each refresher holds one Goodreads login):

1. **Bootstrap.** No cookie saved but a refresher URL is set: `GET /cookie`, use it,
   and save it to that user's credentials.
2. **WAF hit.** On `x-amzn-waf-action`, call `POST /refresh` once per request, retry
   the request once with the new cookie, and save it. A second WAF hit stops and logs.
3. **Sign-in redirect.** Try `GET /cookie` once in case the refresher has a newer
   login. Otherwise trip the auth breaker (§5.1) and log once.
4. **Save-back.** Per-user clients call
   `database_service.set_user_credential(user_id, "GOODREADS_SESSION_COOKIE", cookie)`.
   The global/admin client writes `os.environ` plus `set_setting`, the same pattern as
   `BookFusionClient._persist_access_token`. The in-memory creds dict is updated too.
5. **Failure hints** in logs: HTTP 401 from the refresher means the auth token doesn't
   match `REFRESHER_AUTH_TOKEN`; anything else means the refresher is logged out, so
   open its noVNC view.

**This host.** Run it as its own stack at `C:\Server\stacks\goodreads-cookie-refresher`
on `blackcat-net`, so BookBridge reaches it at `http://<refresher-service>:8080` and
port 5080 never needs publishing. Set a real `VNC_PASSWORD` and an `AUTH_TOKEN`.
Publish 7900 (noVNC) to the LAN only. Chrome needs `shm_size: 2gb`.

## 4. Design decisions

- **Internal key `Goodreads`**, state name `goodreads`, display name "Goodreads".
- **Write-only follower.** `can_be_leader()` is False and `get_service_state()` returns
  None, exactly like StoryGraph.
- **Cooldown-driven.** Driven by `_handle_tracker_cooldown` with its own
  `GOODREADS_UPDATE_COOLDOWN_MINS` (default 60), not the dispatch loop. Finishing a
  book posts immediately.
- **Shelf mapping** (same thresholds as StoryGraph): pct > 0.99 → `read`,
  pct > 0.02 → `currently-reading`, otherwise `to-read`.
- **Shelf writes only on change.** Each `add_to_shelf` can create a feed item, so the
  client keeps an in-memory last-known shelf per Goodreads book id (per-user client
  instance), seeded by reading `viewerShelving`. It posts only when the target differs.
  No DB column, because the details table is shared across users.
- **Progress** is sent as a whole percent through `user_status[percent]`, with an empty
  body. Page mode is out of scope for v1 (Goodreads accepts it, but it would be an
  estimate, as with StoryGraph #458).
- **Never un-shelve automatically.** Unlinking in BookBridge deletes only the local link.
- **Visible side effect.** Every progress post is a public status update in the user's
  Goodreads feed. The cooldown keeps these to about one per reading session. Docs must
  say this plainly.

## 5. Implementation

### 5.1 Client: `src/api/goodreads_client.py` (new)

`GoodreadsClient(credentials: dict | None = None, database_service=None, user_id=None)`,
modelled on `StorygraphClient`:

- `is_configured()`: `GOODREADS_ENABLED` not false, and either a session cookie or a
  refresher URL is set. Read settings per call through `resolve_setting`.
- `_request(path, method, data, extra_headers)`: a `requests` call with browser-shaped
  headers, manual redirect following for GET (max 5 hops, merging `Set-Cookie`), the
  `200` + `Location` quirk, WAF detection and the refresher retry from §3, and
  sign-in detection. **Never log the cookie or POST bodies.** Log cookie length only.
- Auth circuit breaker keyed by cookie fingerprint, copied from
  `StorygraphClient._note_auth_expired`. It warns once per dead cookie, then
  short-circuits writes until the cookie changes.
- `check_connection()`: `GET /`, require a `/user/show/<id>` link.
- `_session_meta()`: `GET /` → `(csrf, user_id)`.
- `search_books(query)`: parses results and the redirect-to-book case. Returns
  `[{book_id, title, author, cover_url, url}]`.
- `get_book(book_id)`: ld+json title/author/image/rating/review count, `numPages`,
  viewer shelf.
- `resolve_book(title, author, isbn)`: ISBN search first, then title+author, with the
  same author gate as `StorygraphClient.resolve_book` (`author_match_floor()`).
- `resolve_book_from_input(text)`: a URL, bare id, `goodreads:<id>`, or search text.
- `set_shelf(book_id, shelf) -> bool`, `update_progress(book_id, fraction) -> bool`,
  `book_url(book_id)`.
- Refresher calls `_fetch_refresher_cookie(force: bool)` with a 15 s timeout and the
  `X-Auth-Token` header.

### 5.2 Data: `GoodreadsDetails` + migration

A new table `goodreads_details` keyed by `abs_id` (FK to books, cascade), mirroring
`storygraph_details`: `goodreads_book_id`, `goodreads_url`, `goodreads_pages`,
`goodreads_rating`, `goodreads_review_count`, `goodreads_rating_updated_at`, `isbn`,
`asin`, `matched_by`, timestamps. Additive Alembic revision on the current head.

- `Book.goodreads_details` relationship in `src/db/models.py`.
- `DatabaseService`: `get_/save_/delete_/get_all_goodreads_details`, and the
  abs_id-migration cleanup next to the StoryGraph one (~line 1192).

### 5.3 Sync client: `src/sync_clients/goodreads_sync_client.py` (new)

Mirrors `StorygraphSyncClient`: `update_progress()` runs automatch if unlinked, maps
the shelf, and posts progress. The automatch strategy order is:

1. **Grimmory `goodreadsId`** from the cached Grimmory metadata for the mapping's
   ebook. It is exact, needs no search, and 398 of 400 cached books on this host have
   one.
2. ISBN, then ASIN, through `resolve_book`.
3. Title+author (judge off), or the LLM craft+judge path (`tracker_match_enabled()`)
   exactly as StoryGraph does it.

Automatch does **not** set an initial shelf. The first cooldown post does that.

### 5.4 Wiring (each item mirrors an existing StoryGraph line)

- `src/utils/di_container.py`: `goodreads_client` and `goodreads_sync_client`
  Singletons, and `Goodreads=` in the three `sync_clients` dicts.
- `src/services/user_client_registry.py`: per-user `GoodreadsClient(credentials,
  database_service, user_id)`, a sync client entry, and a `UserClients` field.
- `src/sync_manager.py`:
  - `_COMPLETION_PROPAGATION_EXCLUDED_CLIENTS`
  - `_goodreads_cooldown` store and lock
  - `_handle_goodreads_cooldown`, called beside the StoryGraph and Hardcover calls
    (~4866)
  - the dispatch-loop skip tuple (~5229)
- `src/services/reading_position_preview.py`: `_WRITE_ONLY_CLIENTS`.
- Deferred automatch hooks: `web_server.py` (~688),
  `src/services/book_mapping_service.py`, `src/services/forge_service.py`.
- `web_server.py`:
  - integration display map (~1370) and `_TEST_CONNECTION_FIELDS` (~1388)
  - dashboard integration flag (~1813)
  - boolean key list (~4104)
  - test-connection dispatch (~11944) plus a new `_test_goodreads`
  - dashboard mapping fields (~5415) and `get_all_goodreads_details()` at both call
    sites (~5845, ~10842)
- `diagnostics_receiver/app.py` `_KNOWN_SERVICE_KEYS` and `reports_site/app.py` labels.
  Both deploy separately, so note that in the deploy step.

### 5.5 Settings (use the `/add-setting` skill for every key)

| Key | Type | Scope | Default |
|---|---|---|---|
| `GOODREADS_ENABLED` | bool | global gate + per user | `false` |
| `GOODREADS_SESSION_COOKIE` | secret | per user | empty |
| `GOODREADS_COOKIE_REFRESH_URL` | text | per user | empty |
| `GOODREADS_COOKIE_REFRESH_TOKEN` | secret | per user | empty |
| `GOODREADS_UPDATE_COOLDOWN_MINS` | int | global | `60` |

Touchpoints: `ALL_SETTINGS`, `DEFAULT_CONFIG`, `bool_keys`, `PER_USER_CREDENTIAL_KEYS`,
the user integration field list and `SERVICE_ENABLE_KEYS` in `user_config.py`, a
Goodreads card in `templates/settings.html` (copy the StoryGraph card), and the maps in
`account_integrations.html`, `admin_user_integrations.html` and the `settings.html` JS
test-field list (~3319). Credentials are encrypted at rest like the other secrets.

### 5.6 Linking UI

- `src/api/goodreads_routes.py` (new blueprint): `GET /api/goodreads/resolve` and
  `POST /link-goodreads/<abs_id>`, with the same ownership checks as the StoryGraph
  routes. Goodreads has no edition switching, so the modal is simpler.
- `static/js/goodreads-modal.js` plus a modal block and a service row in
  `templates/index.html` (copy the StoryGraph row and modal and drop the edition list).
- `static/goodreads.svg`: a plain neutral glyph, not Goodreads' logo.

### 5.7 Ratings

When linked, rating and review count come from the book page's ld+json and are stored
on `goodreads_details`. The dashboard prefers them and falls back to the cached
Grimmory `goodreadsRating`, so existing badges don't regress. A backfill thread (like
`storygraph_rating_backfill`) is optional. It is skipped unless ratings are missing in
practice, since Grimmory already covers 390 of 400 books here.

## 6. Tests

New files: `test_goodreads_client.py`, `test_goodreads_sync_client.py`,
`test_goodreads_cooldown.py`, `test_goodreads_routes.py`, plus settings and migration
cases in the existing suites. HTML fixtures are trimmed from live captures with names,
ids and cookies scrubbed. Each case listed below must fail with its change reverted.

- **Parsing:** search results; ISBN redirect, including the `200` + `Location` case;
  ld+json; `viewerShelving` → shelf; `numPages`; CSRF and user id from the homepage.
- **Redirects:** `Set-Cookie` is merged across a bootstrap redirect.
- **WAF:** WAF → refresher `/refresh` → retry succeeds, and the new cookie is saved
  for a per-user client (`set_user_credential`) and for the global client
  (`set_setting` + env). A second WAF hit does not loop.
- **Refresher:** no cookie + refresher → bootstrap from `/cookie`. A refresher 401
  logs the token hint.
- **Auth breaker:** a sign-in redirect warns once and blocks writes until the cookie
  changes.
- **Shelves:** shelf posted only when it changes; thresholds 0.02 and 0.99.
- **Sync:** cooldown holds while progressing and posts at idle and at completion.
  Goodreads is excluded from dispatch and from completion propagation.
- **Automatch:** order is Grimmory id → ISBN → title/author, and the author gate
  rejects a same-title/different-author book.
- **Multi-user:** a regular user never falls back to admin cookies, and one user's
  refreshed cookie never lands in another's credentials.
- **Settings:** booleans accept `'true'` and `'on'`; keys grep in every touchpoint.
- **Migration:** base → head on fresh SQLite.
- Full suite passes in order and random order. Record new counts.

## 7. Live verification (needs the user)

1. Bring up the refresher stack. The user logs in through noVNC once.
   `GET /health` shows `logged_in: true`.
2. Deploy is **migration + restart**. Confirm the migration applied, settings loaded,
   and `check_connection()` found the user id.
3. Read-only: resolve three real books (Grimmory id, ISBN, title) and read each shelf.
4. Writes happen **only with the user's go-ahead, on a book they are actually reading**,
   because progress posts appear publicly in their feed and can only be deleted by hand.
   Record the book's shelf and progress before the write. Then do one shelf check and
   one progress post, read both back, and restore the shelf if it changed.
5. Force a WAF path if one occurs naturally, or simulate it by pointing at a stale
   cookie, and capture the refresh-and-retry log lines.

## 8. Phases and commits

- [x] **Phase 0 — refresher and fixtures.** Refresher stack up on `blackcat-net`;
      user logged in through noVNC; scrubbed home, search, and book HTML fixtures
      saved under `tests/fixtures/` and validated.
      Stack is running at `C:\Server\stacks\goodreads-cookie-refresher` with noVNC
      bound to `192.168.50.65:7900` and its API internal to `blackcat-net`.
      The signed-in profile-menu link and homepage CSRF token were verified in
      Chrome; the refresher holds 22 cookies after login. The upstream `/health`
      reports `logged_in: true` from any captured cookies (four before login here),
      so verify the profile-menu link instead. A direct cookie replay hit an AWS
      WAF challenge before login and timed out after login; Phase 1 must test and
      handle that path. No Goodreads shelf or progress writes were made.
- [x] **Phase 1 — client and settings.** `goodreads_client.py`, all five settings,
      client tests. The full suite passed (4965 passed, 17 skipped, 357 subtests).
      Authentication and parser behavior is fixture-tested; live read-only connection
      and all Goodreads writes remain for Phase 5. Commit
      `[Feature] Goodreads client with cookie-refresher support`.
- [ ] **Phase 2 — data and sync.** Model, migration, `DatabaseService` methods, sync
      client, automatch, `sync_manager` and DI wiring, cooldown tests. Commit.
- [ ] **Phase 3 — UI.** Routes, modal, dashboard row, integration pages, connection
      test. Commit.
- [ ] **Phase 4 — ratings.** Goodreads-sourced rating with Grimmory fallback. Commit.
- [ ] **Phase 5 — local notes and live.** Record the working auth and refresher
      setup in this plan, update `CHANGELOG.md` for the user-visible behavior,
      run live verification from §7, and update `BRANCH_STATUS.md`.
      **Deploy: migration + restart.**

## 9. Risks and open questions

- **Scraping breaks.** Goodreads' Next.js markup can change without notice. Parsers are
  isolated in small functions with fixtures so a break is quick to find and fix.
- **Feed noise.** Covered by the cooldown and shelf-on-change. A future setting could
  turn progress posts off and keep shelf-only sync if the user wants it.
- **"Read" shelf date.** It is not confirmed whether `add_to_shelf name=read` sets the
  finish date. Check during live verification.
- **Progress on a book not yet shelved.** Whether `/user_status.json` needs
  `currently-reading` first is unconfirmed. The client shelves first either way.
- **Terms of service.** This automates the user's own account through unofficial
  endpoints, as the StoryGraph client already does. The docs carry the same disclaimer.
- **Refresher cost.** A persistent Chrome uses about 1–2 GB of RAM. It is optional: a
  manually pasted cookie still works without it.
