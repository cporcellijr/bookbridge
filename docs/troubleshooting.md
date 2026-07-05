# Troubleshooting

## Common Issues

### Books are not showing up

- Make sure your `/books` volume is mounted correctly in `docker-compose.yml`.
- Check file permissions for the user running the container.
- If the missing books should come from Grimmory, run **Settings -> Refresh Grimmory Cache**.
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
- In Settings, make sure **KOReader -> Highlight Sync** is enabled.
- For Grimmory web-reader annotations, enable **Highlight Sync** in that reader's Grimmory / BookLore Integrations.
- For BookOrbit web-reader highlights, fill in the BookOrbit KOReader sync username/password in that reader's Integrations and make sure the owner matches the BookOrbit user.

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
