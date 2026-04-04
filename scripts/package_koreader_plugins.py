from __future__ import annotations

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
DIST_DIR = ROOT / "dist" / "plugins"


def find_plugins() -> list[Path]:
    return sorted(path for path in PLUGINS_DIR.iterdir() if path.is_dir() and path.name.endswith(".koplugin"))


def parse_version(plugin_dir: Path) -> str:
    meta_path = plugin_dir / "_meta.lua"
    if not meta_path.exists():
        return "unknown"

    text = meta_path.read_text(encoding="utf-8")
    match = re.search(r'version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "unknown"


def build_zip(plugin_dir: Path) -> Path:
    version = parse_version(plugin_dir)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / f"{plugin_dir.stem}-{version}.zip"

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_dir():
                continue
            arcname = Path(plugin_dir.name) / path.relative_to(plugin_dir)
            zf.write(path, arcname.as_posix())

    return zip_path


def resolve_targets(names: list[str]) -> list[Path]:
    available = {path.name: path for path in find_plugins()}
    if not names:
        return list(available.values())

    missing = [name for name in names if name not in available]
    if missing:
        raise SystemExit(f"Unknown plugin(s): {', '.join(missing)}")

    return [available[name] for name in names]


def main() -> int:
    parser = argparse.ArgumentParser(description="Package optional KOReader plugins from this repo.")
    parser.add_argument("plugins", nargs="*", help="Specific plugin folder names to package, e.g. bridgesync.koplugin")
    args = parser.parse_args()

    targets = resolve_targets(args.plugins)
    if not targets:
        raise SystemExit("No .koplugin directories found under plugins/")

    for plugin_dir in targets:
        zip_path = build_zip(plugin_dir)
        print(zip_path.relative_to(ROOT))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
