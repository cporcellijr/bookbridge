"""Template source of the Classic or New Discover page with the shared script partial inlined.

`templates/next/_discover_main_script.html` holds the Discover script once for both pages; New-only
lines sit in `{% if discover_next %}` branches. Source-reading tests use this to see exactly the
script each page renders, as they did when each page carried its own copy.
"""
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_PARTIAL = "next/_discover_main_script.html"
_INCLUDES = {
    "suggestions.html": "{% include 'next/_discover_main_script.html' %}",
    "next/_discover_scripts.html": "{% set discover_next = true %}{% include 'next/_discover_main_script.html' %}",
}


def _resolve(partial: str, new: bool) -> str:
    """Keep the branch for one interface; drop the `discover_next` markers."""
    out, stack = [], []
    for line in partial.splitlines(keepends=True):
        tag = line.strip()
        if tag in ("{% if discover_next %}", "{% if not discover_next %}"):
            stack.append(new if tag == "{% if discover_next %}" else not new)
            continue
        if stack and tag == "{% else %}":
            stack[-1] = not stack[-1]
            continue
        if stack and tag == "{% endif %}":
            stack.pop()
            continue
        if all(stack):
            out.append(line)
    return "".join(out)


def discover_template_source(name: str = "suggestions.html") -> str:
    """Return `name`'s source with the shared Discover script resolved for that page's interface."""
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    include = _INCLUDES.get(name)
    if include is None or include not in text:
        return text
    partial = (_TEMPLATES / _PARTIAL).read_text(encoding="utf-8")
    body = _resolve(partial, new=name != "suggestions.html").rstrip("\n")
    return text.replace(include, body, 1)
