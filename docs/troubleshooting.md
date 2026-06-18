# Troubleshooting

## Common Issues

### Books are not showing up

- Make sure your `/books` volume is mounted correctly in `docker-compose.yml`.
- Check file permissions for the user running the container.
- If the missing books should come from Grimmory, run **Settings -> Refresh Grimmory Cache**.

### Suggestions look stale or new imports are missing

- A normal **Scan Library** run reuses cached results on purpose.
- Use **Full Refresh** after large imports, removals, or metadata cleanup.
- If Grimmory titles are missing from Suggestions, refresh the Grimmory cache first.

### Grimmory audiobook links are not syncing

- Confirm **Grimmory** is enabled and the audiobook still exists in the selected Grimmory library.
- Run **Refresh Grimmory Cache** after moving, rescanning, or replacing Grimmory items.
- If the old Grimmory item was deleted and recreated with a new ID, rematch that book.

### BookOrbit books are not matching or syncing

- Confirm **BookOrbit** is enabled and configured in Settings, and use the **Test** button to check the connection.
- BookOrbit behaves like Grimmory: if a book exists in BookOrbit but won't link, make sure its library has been scanned so filenames resolve.
- If you switched from Grimmory to BookOrbit, you do not need to rematch — run `scripts/migrate_grimmory_to_bookorbit.py` (see the [Configuration Guide](configuration.md#bookorbit)).

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
- Grimmory cache refreshes
- Background job failures

---

## GPU Acceleration

See the **[Configuration Guide](configuration.md#gpu-support-optional)** for NVIDIA GPU setup instructions.
