---
description: "Which apps BookBridge works with and what it syncs with each: Audiobookshelf, KOReader, Calibre-Web Automated, BookOrbit, Grimmory, Storyteller, Kavita, BookFusion, Readest, Hardcover, StoryGraph, Goodreads and Fable."
---

# Supported apps

Every app is optional. Turn on the ones you use in **Settings**, and add more later. You need at least two places you read or listen for a sync to mean anything: an audiobook app and an ebook reader, or two ebook readers.

## At a glance

| App | What it is to BookBridge | Reading progress | Highlights and notes | Also |
| :--- | :--- | :--- | :--- | :--- |
| [**Audiobookshelf**](configuration.md#audiobookshelf) | Audiobooks, plus its built-in ebook reader | Two-way (ebook side opt-in) | No | Listening stats; files matched books into a collection |
| [**KOReader**](configuration.md#koreader-kosync) | Ebook reader on Kobo, PocketBook, Android or a jailbroken Kindle | Two-way | Two-way, with the Bridge Sync plugin | Reading status, History, reading stats, book delivery and collections, all through the plugin |
| [**Calibre-Web Automated**](configuration.md#calibre-web-automated-cwa) | Ebook library; progress for stock Kobo readers through Kobo sync | Two-way (opt-in) | No | Ebook source; reading status |
| [**BookOrbit**](configuration.md#bookorbit) | Audiobooks and ebooks | Two-way | Two-way (opt-in) | Reading sessions, watched shelf, read-along EPUBs |
| [**Grimmory**](configuration.md#grimmory) | Audiobooks and ebooks | Two-way | Two-way (opt-in) | Reading sessions, watched shelf, shelves as KOReader collections |
| [**Storyteller**](configuration.md#storyteller) | Read-along reader | Two-way | No | Better alignment for books you built there |
| [**Kavita**](configuration.md#kavita) | Ebook library and reader | Two-way (not comics) | No | Ebook source; watched collection (opt-in) |
| [**BookFusion**](configuration.md#bookfusion) | Ebook reader | Two-way | Two-way (opt-in) | Ebook source; uploads your EPUBs |
| [**Readest**](configuration.md#readest) | Ebook reader | Not synced | Two-way (opt-in) | Uploads books into a group |
| [**Hardcover**](configuration.md#hardcover) | Reading tracker | Sent one way, with status | Sent one way, as private notes (opt-in) | Lists as KOReader collections |
| [**StoryGraph**](configuration.md#storygraph) | Reading tracker | Sent one way, with status | No | Edition picking |
| [**Goodreads**](configuration.md#goodreads) | Reading tracker | Sent one way, with shelf moves | No | |
| [**Fable**](configuration.md#fable) | Reading tracker | Sent one way, with list moves | No | |

"Two-way" means the app can be the one you moved forward in, and BookBridge carries that position to the others. "Sent one way" means BookBridge updates the app but never reads a position back from it, so a tracker never decides where you are.

## Which side each app can play

- **Audiobook source:** Audiobookshelf, Grimmory or BookOrbit.
- **Ebook source:** Audiobookshelf ebooks, BookFusion, Grimmory, BookOrbit, Kavita, Calibre-Web Automated, or local files in your `/books` folder.
- **Ebook only:** link two or more readers with no audiobook at all, for example KOReader and Grimmory.

## Things worth knowing

!!! note "Highlights need the Bridge Sync plugin"
    Plain KOReader and KOSync clients sync your position only. Highlights, notes, reading status and History come with the [Bridge Sync plugin](getting-started.md#6-connect-a-koreader-device-optional), installed from the dashboard or the [Releases](https://github.com/cporcellijr/bookbridge/releases) page.

- **BookOrbit highlights** use BookOrbit's own KOReader sync login, so set those credentials in Settings as well.
- **Calibre-Web Automated** progress works through CWA's Kobo sync: BookBridge reads and writes the bookmark CWA keeps for your Kobo. It is off until you turn it on.
- **Watched shelves and collections** (Grimmory, BookOrbit, Kavita) link anything you drop into them. See [Automatic ways books get added](user-guide.md#automatic-ways-books-get-added).
- **Trackers** (Hardcover, StoryGraph, Goodreads, Fable) follow whichever app you read in. See [Progress trackers](configuration.md#progress-trackers) for each one's sign-in.
