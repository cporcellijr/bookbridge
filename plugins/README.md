# Optional KOReader Plugins

This directory holds optional KOReader plugins that are developed alongside the bridge.

Current plugins:

- `bridgesync.koplugin` - Mirrors bridge-managed books into a KOReader folder.

These plugins are not required to run the bridge itself.

## Download

Users should download prebuilt plugin zip files from the project's GitHub Releases page.

## Packaging

For local packaging or release prep, build plugin zips from this repo with:

```bash
python scripts/package_koreader_plugins.py
```

Output is written to `dist/plugins/`.

To package one plugin only:

```bash
python scripts/package_koreader_plugins.py bridgesync.koplugin
```
