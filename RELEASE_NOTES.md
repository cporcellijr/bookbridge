# Release Notes — 7.0.0

The headline change is **user accounts**: the bridge now supports more than one reader, each with their own sign-in, their own service logins, and their own reading progress. This is a bigger release than usual — existing setups upgrade in place, but there is a one-time sign-in step the first time you open the dashboard (see Upgrading below).

## Added
- **Multiple readers** — create separate accounts for different people, for example everyone in a household. Each person signs in to their own dashboard, sees only the books they are reading, and keeps their own progress, even when two people read the same book.
- **Personal logins for each service** — every reader enters their own Audiobookshelf, KOSync, Grimmory or BookOrbit, Storyteller, and tracker logins, so each person syncs against their own accounts and their own shelves.
- **A sign-in screen** — the dashboard is now protected by a login. The first person to open it sets up the main account and can add more readers from a new Users area in Settings.
- **Same-folder matching** — audiobook and ebook files in the same library folder are recognized as likely pairs in Suggestions, treated as an exact match only when the titles also agree.
- **Bulk suggestion review** — select multiple suggestions with checkboxes or add every exact (100%) match at once, queue them without the page reloading, and run Forge & Match All straight from the Suggestions page.

## Changed
- The shared engine settings — how often it syncs, library scans, and shelf watching — stay in one place for the main account to manage, while the per-person service logins move into each reader's own settings.

## Fixed
- **CWA progress appears sooner** — books synced through Calibre-Web-Automated's Kobo sync now show their CWA row on the dashboard right away, instead of only after the first position comes in.

## Upgrading
Database migrations apply automatically on startup, so you only need to update and restart. The first time you open the dashboard after upgrading, you will be asked to create your main login — just pick a username and password. As soon as you do, your existing library, your matches, and every service login you had already entered are moved onto that account automatically, so there is nothing to set up again. Your KOReader devices keep syncing exactly as before. From there you can add accounts for other readers whenever you like.

## Known limitations
- The whole dashboard now requires a sign-in. If you previously left it open on your network without a login, you and anyone who used it will now need an account.
