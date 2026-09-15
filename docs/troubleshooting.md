# Troubleshooting

## Common Issues

### Do I need Storyteller?

No. Storyteller is an optional integration. The bridge does audio ↔ ebook alignment itself
using built-in Whisper transcription (or the EPUB's SMIL data), so cross-format sync works
with just an audio source and an ebook client. Add Storyteller only if you actually use the
Storyteller read-along app — the bridge will then sync its position and use its transcripts
as a bonus alignment source.

### An alignment is rejected, or opens at the wrong place

- Confirm that the selected ebook and audiobook are the same edition. The
  **Content-Match Guard** deliberately refuses maps with too little shared wording;
  leave it enabled unless you know the pairing is a rough but intended match.
- Open **Settings -> Sync -> Alignment Health**. A low-quality map can be rebuilt with
  **Remap alignment**, and **Restore previous** returns to the last saved map if the
  replacement is worse.
- If a collection's audiobook narrates chapters in a different order from the EPUB,
  enable experimental **Segmented Alignment Maps** in Settings, then remap that book.
  It is off by default and is not needed for normally ordered books.
- CTC forced alignment is optional and unavailable in published images. It requires a
  custom `INSTALL_CTC=true` build and is not the first troubleshooting step.

### A deliberate rewind does not stick

- Confirm **Honor a Deliberate Rewind** is enabled under **Settings -> Sync**.
- After going back, make some forward progress in the same app. One isolated backward
  report is intentionally held so a stale or echoed position cannot pull every reader
  backward.
- If you want the furthest position to win in every case, turn the setting off.

### Books are not showing up

- Make sure your `/books` volume is mounted correctly in `docker-compose.yml`.
- Check file permissions for the user running the container.
- If the missing books should come from Grimmory, run **Settings -> System -> Advanced -> Refresh Grimmory Cache**.
- If the missing books should come from BookOrbit, confirm BookOrbit is enabled, test the connection, and retry the matching flow.
- If the missing books should come from CWA, confirm CWA is enabled, test the connection, and make sure the book is visible in Calibre-Web Automated.

### Suggestions look stale or new imports are missing

- A normal **Scan Library** run reuses cached results on purpose.
- Use **Full Refresh** after large imports, removals, or metadata cleanup.
- If Grimmory titles are missing from Suggestions, refresh the Grimmory cache first.
- If BookOrbit titles are missing, confirm BookOrbit is enabled and reachable, then retry the scan.
- If CWA titles are missing, confirm CWA is enabled and reachable, then retry the scan.

### CWA progress is not syncing

- Confirm **CWA** is enabled and the CWA server URL is reachable from the bridge.
- Enable **Kobo Sync** in the CWA integration and enter the correct Kobo sync token for that reader.
- If using custom polling, check the CWA sync poll interval and make sure it is not set higher than expected.
- Confirm the book is linked to a CWA ebook entry; CWA progress only appears for books the bridge can resolve through CWA.

### BookFusion progress or highlights are not syncing

- Open **Account -> My Integrations** and confirm BookFusion is linked and enabled for that reader.
- If you entered a token manually, BookFusion API keys are available from the [BookFusion Calibre integration page](https://www.bookfusion.com/integrations/calibre).
- Confirm the book is linked to a BookFusion book ID. If the book only exists locally, the dashboard link flow can **Upload to BookFusion** — this needs the reader's Calibre API key in My Integrations.
- For highlights, also enable **BookFusion -> Highlight Sync** for that reader and keep the current **Bridge Sync** KOReader plugin installed on devices that should exchange annotations.
- If using custom polling, check the BookFusion poll interval and make sure it is not set higher than expected.

### Grimmory or BookOrbit audiobook links are not syncing

- Confirm the source is enabled and the audiobook still exists in the selected Grimmory or BookOrbit library.
- For Grimmory, run **Refresh Grimmory Cache** after moving, rescanning, or replacing items.
- For BookOrbit, confirm the item is still visible in BookOrbit and retry the matching or sync flow.
- If the old source item was deleted and recreated with a new ID, rematch that book.

### BookOrbit books are not matching or syncing

- Confirm **BookOrbit** is enabled and configured in Settings, and use the **Test** button to check the connection.
- If a book exists in BookOrbit but will not link, make sure it is visible in BookOrbit and that the bridge can reach the BookOrbit server.
- If you switched from Grimmory to BookOrbit, you do not need to rematch — run `scripts/migrate_grimmory_to_bookorbit.py` (see the [Configuration Guide](configuration.md#bookorbit)).

### Highlights or notes are not syncing

- Update the **Bridge Sync** KOReader plugin to the current release or newer on every KOReader device that should sync annotations.
- Plain KOReader/KOSync progress sync does not sync annotations. In KOReader, use **Tools -> Bridge Sync -> Sync Highlights** or **Sweep All Highlights**.
- Make sure **Highlight Sync** is enabled on the **Settings -> Integrations -> KOReader / KoSync** card.
- For Grimmory web-reader annotations, enable **Highlight Sync** in that reader's Grimmory integration.
- For BookOrbit web-reader highlights, fill in the BookOrbit KOReader sync username/password in that reader's Integrations and make sure the owner matches the BookOrbit user.
- For BookFusion, enable **BookFusion -> Highlight Sync** in that reader's Integrations and confirm the book is linked to BookFusion.
- For Readest, enable **Readest -> Highlight Sync** and enter that reader's Readest email/password.
- For Hardcover, enable **Hardcover -> Highlight Sync** and enter that reader's Hardcover API token.

### Bridge Sync uses too much memory or a device download fails

- Update the Bridge Sync plugin to **0.6.10** or newer, then restart KOReader.
- A failed replacement leaves the previous good book on the device. Run **Sync books**
  again after fixing network access; do not delete the existing copy first.

### Books are not appearing in Readest

- Uploading is off by default. Enable **Upload matched books to Readest** and/or **Upload
  books you are currently reading** in that reader's **Account -> My Integrations**.
- The currently-reading sweep only sends books with a reading position above 0% and below
  the completion threshold. A book you have not opened anywhere will not be sent by it —
  match it with **Upload matched books** enabled, or start reading it.
- Books already in that Readest account are skipped. Check the group named by **Group name
  for uploaded books** (default `BookBridge`).
- The sweep shares a timer with the annotation syncs. If every one of those intervals is
  `0`, set **Upload: sweep interval (minutes)** to a non-zero value so the timer runs.
- Only EPUB files are uploaded.
- If the Readest account is out of storage the log says so and uploading stops. Readest's
  free plan includes 500 MB; free space in Readest or lower **Upload: max books per run**.

### Storyteller transcripts are not found

- Verify the Docker volume is mounted as host Storyteller assets -> container `/storyteller/assets`.
- In Settings, set **Storyteller Assets Path** to `/storyteller`, not `/storyteller/assets`.
- Confirm the expected structure exists inside the container: `/storyteller/assets/{title}/transcriptions/*.json`.

### Storyteller transcripts are still rejected

- The bridge now accepts more Storyteller filename layouts, but the files still need real Storyteller timeline data.
- Each chapter JSON should contain `wordTimeline` or compatible Storyteller timeline data.
- If the filenames are right but the data format is wrong, the bridge will skip those files and fall back to SMIL or Whisper.

### KOSync split-port mode is not working

- If you set `KOSYNC_PORT`, you also need to map that same port in Docker.
- Example:

```yaml
ports:
  - "8080:5757"
  - "5758:5758"
```

### Transcription is taking too long

- Use a smaller local Whisper model such as `tiny`.
- If you have an NVIDIA GPU, enable GPU support as described in the [Configuration Guide](configuration.md#gpu-support-optional).
- If Storyteller transcript assets are available, configure them so the bridge can skip Whisper entirely for those books.

---

## Logs

You can inspect logs in the web UI or from the terminal:

```bash
docker compose logs -f
```

Useful places to look:

- Match and Suggestions actions
- Storyteller transcript ingest
- Library refreshes
- Background job failures

---

## GPU Acceleration

See the **[Configuration Guide](configuration.md#gpu-support-optional)** for NVIDIA GPU setup instructions.
