"""Write the "Every setting" index in docs/configuration.md from the settings registry.

The index lists every key in ALL_SETTINGS, grouped as the registry groups them, with
its default from DEFAULT_CONFIG, whether each reader has their own value or can override
the server default, whether
changing it restarts the app, and a link to the section of the page that describes it.

Usage (from the repo root, with the project venv):
    python scripts/docs_settings_index.py          rewrite the index
    python scripts/docs_settings_index.py --check  exit 1 if the index is out of date
"""
import re
import sys
from pathlib import Path

from markdown.extensions.toc import slugify

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.utils import config_loader as cl  # noqa: E402
from src.utils.display_settings import DISPLAY_SETTINGS  # noqa: E402
from src.utils.user_config import (  # noqa: E402
    PER_USER_CREDENTIAL_KEYS, PER_USER_PREFERENCE_KEYS, SERVICE_ENABLE_KEYS,
)

DOC = ROOT / "docs" / "configuration.md"
START, END = "<!-- settings-index:start -->", "<!-- settings-index:end -->"

# Registry comment -> heading on the page. Groups mapping to the same heading merge.
GROUPS = {
    "Required ABS": "Audiobookshelf",
    "Optional ABS": "Audiobookshelf",
    "KOSync": "KOReader / KoSync",
    "Storyteller": "Storyteller",
    "Grimmory": "Grimmory",
    "BookOrbit": "BookOrbit",
    "Read-along EPUB 3 generation": "BookOrbit",
    "Kavita": "Kavita",
    "BookFusion": "BookFusion",
    "CWA (Calibre-Web Automated)": "Calibre-Web Automated",
    "Readest annotation sync (account is per-user; these are global engine defaults)": "Readest",
    "Progress Tracker": "Hardcover",
    "Hardcover": "Hardcover",
    "StoryGraph": "StoryGraph",
    "Goodreads": "Goodreads",
    "LLM providers (Ollama, OpenAI, OpenAI-compatible local servers)": "AI",
    "Ollama (legacy/local LLM settings, still honored)": "AI",
    "One-time boot markers (no UI field; never in bool_keys)": "Set by BookBridge",
    "Diagnostics": "Diagnostics",
    "Telegram": "Telegram",
    "Shelfmark": "Getting missing books",
    "Acquisition": "Getting missing books",
    "Sync Behavior": "Sync, matching and alignment",
    "System": "Server, paths and interface",
}
# Keys the registry lists inside another app's block.
KEY_GROUPS = {
    "REMOTE_AUTH_ENABLED": "Sign-in", "REMOTE_AUTH_HEADER": "Sign-in", "REMOTE_AUTH_TRUSTED_PROXIES": "Sign-in",
}
# Values BookBridge writes for itself. Listed so nobody wonders, never meant to be set.
SET_BY_BOOKBRIDGE = {
    "SERVICE_GATE_RECONCILED", "DIAGNOSTICS_PROMPTED", "DIAGNOSTICS_INSTANCE_ID",
    "DIAGNOSTICS_LAST_SENT", "DIAGNOSTICS_INGEST_TOKEN",
    "READEST_ACCESS_TOKEN", "READEST_REFRESH_TOKEN", "READEST_TOKEN_EXPIRES_AT",
}


def registry_groups() -> dict:
    """Group ALL_SETTINGS by the comment above each key in config_loader.py."""
    source = Path(cl.__file__).read_text(encoding="utf-8")
    body = source.split("ALL_SETTINGS = [", 1)[1].split("\n]\n", 1)[0]
    groups, group, seen = {}, None, []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            group = GROUPS[line.lstrip("# ").strip()]
            continue
        for key in re.findall(r"'([A-Z0-9_]+)'", line):
            heading = "Set by BookBridge" if key in SET_BY_BOOKBRIDGE else KEY_GROUPS.get(key, group)
            groups.setdefault(heading, []).append(key)
            seen.append(key)
    if seen != list(cl.ALL_SETTINGS):
        raise SystemExit("Parsed keys differ from ALL_SETTINGS; update registry_groups().")
    groups["Set by BookBridge"] = groups.pop("Set by BookBridge")
    return groups


def detail_anchors(doc: str) -> dict:
    """Map each key that has a table row on the page to the heading above that row."""
    anchors, heading = {}, None
    for line in doc.split(START)[0].splitlines() + doc.split(END)[-1].splitlines():
        m = re.match(r"#{2,4} (.+)", line)
        if m:
            heading = slugify(m.group(1).strip(), "-")
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and heading:
            k = re.fullmatch(r"`([A-Z][A-Z0-9_]+)`", cells[1])
            if k:
                anchors.setdefault(k.group(1), heading)
    return anchors


def render(doc: str) -> str:
    """The index block, markers included."""
    anchors = detail_anchors(doc)
    out = [START, ""]
    for heading, keys in registry_groups().items():
        out += [f"### {heading} {{ #every-setting-{slugify(heading, '-')} }}", "",
                "| Variable | Default | Applies to | Restart |", "| --- | --- | --- | --- |"]
        for key in keys:
            default = str(cl.DEFAULT_CONFIG.get(key, ""))
            default = f"`{default}`".replace("|", "\\|") if default else "empty"
            name = f"[`{key}`](#{anchors[key]})" if key in anchors else f"`{key}`"
            if key in SERVICE_ENABLE_KEYS:
                who = "server and each reader"
            elif key in PER_USER_CREDENTIAL_KEYS:
                who = "each reader"
            elif key in PER_USER_PREFERENCE_KEYS or key in DISPLAY_SETTINGS:
                who = "server default; each reader can override"
            else:
                who = "server"
            restart = "yes" if key in cl.RESTART_REQUIRED_SETTINGS else ""
            out.append(f"| {name} | {default} | {who} | {restart} |")
        out.append("")
    out.append(END)
    return "\n".join(out)


def main() -> int:
    doc = DOC.read_text(encoding="utf-8")
    if doc.count(START) != 1 or doc.count(END) != 1:
        raise SystemExit(f"{DOC} needs exactly one {START} and one {END}.")
    new = doc.split(START)[0] + render(doc) + doc.split(END)[1]
    if "--check" in sys.argv:
        if new != doc:
            print("docs/configuration.md settings index is out of date; run scripts/docs_settings_index.py")
            return 1
        print("settings index up to date")
        return 0
    DOC.write_text(new, encoding="utf-8", newline="\n")
    print(f"wrote {sum(len(k) for k in registry_groups().values())} settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
