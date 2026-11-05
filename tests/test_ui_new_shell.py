from __future__ import annotations

from html.parser import HTMLParser
from itertools import product
from pathlib import Path
from types import SimpleNamespace
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape


PROJECT_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "templates" / "_nav.html").is_file()),
    None,
)
if PROJECT_ROOT is None:
    raise RuntimeError("Could not locate the BookBridge project root from this test file")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append({"attrs": attrs, "text": ""})
            self.stack.append(self.links[-1])
        elif tag in ("nav", "details"):
            self.stack.append({"tag": tag, "attrs": attrs})

    def handle_endtag(self, tag):
        if tag == "a":
            for i in range(len(self.stack) - 1, -1, -1):
                if "attrs" in self.stack[i] and "href" in self.stack[i]["attrs"]:
                    self.stack = self.stack[:i]
                    break
        elif tag in ("nav", "details") and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for item in reversed(self.stack):
            if "text" in item:
                item["text"] += data.strip() + " "
                break


def _env():
    return Environment(
        loader=FileSystemLoader(PROJECT_ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
    )


def _context(*, admin=False, providers=None, overrides=False, disabled=(), shelfmark=False, shelfmark_legacy=False,
             shelfmark_off=False, abs_url="", queued=0, hide_chrome=False):
    providers = providers or {}
    vals = {}
    settings = {}
    provider_fields = {
        "abs": ("ABS_WEB_URL", "ABS_SERVER", None),
        "grimmory": ("BOOKLORE_WEB_URL", "BOOKLORE_SERVER", "BOOKLORE_ENABLED"),
        "bookorbit": ("BOOKORBIT_WEB_URL", "BOOKORBIT_SERVER", "BOOKORBIT_ENABLED"),
        "kavita": ("KAVITA_WEB_URL", "KAVITA_SERVER", "KAVITA_ENABLED"),
        "cwa": ("CWA_WEB_URL", "CWA_SERVER", "CWA_ENABLED"),
    }
    for provider, (web_key, server_key, enabled_key) in provider_fields.items():
        if providers.get(provider):
            settings[server_key] = f"https://{provider}.example/server"
            if enabled_key:
                settings[enabled_key] = provider not in disabled
            if overrides:
                vals[web_key] = f"https://{provider}.example/web"
    if abs_url:
        settings["ABS_SERVER"] = abs_url
    if shelfmark:
        vals["SHELFMARK_URL"] = "https://shelfmark.example"
        vals["SHELFMARK_ENABLED"] = "" if shelfmark_legacy else ("false" if shelfmark_off else "true")
    user = SimpleNamespace(username="reader", is_admin=admin)

    def get_val(key, default=""):
        return vals.get(key, default)

    def get_user_val(key, default=""):
        return settings.get(key, default)

    def get_user_bool(key):
        return bool(settings.get(key, False))

    def get_bool(key):
        return str(vals.get(key, "")).strip().lower() in ("true", "1", "yes", "on")

    return {
        "current_user": user,
        "active_page": "library",
        "hide_chrome": hide_chrome,
        "shelfmark_url": vals.get("SHELFMARK_URL", ""),
        "abs_server": settings.get("ABS_SERVER", ""),
        "booklore_server": settings.get("BOOKLORE_SERVER", ""),
        "get_locale": lambda: "en",
        "_": lambda text: text,
        "url_for": lambda endpoint, **kwargs: (
            "/account/interface?next=" + kwargs.get("next", "/")
            if endpoint == "account_interface"
            else {"suggestions": "/suggestions", "job_queue_page": "/job-queue"}[endpoint]
        ),
        "request": SimpleNamespace(path="/"),
        "csrf_token": lambda: "test-csrf-token",
        "get_val": get_val,
        "get_user_val": get_user_val,
        "get_user_bool": get_user_bool,
        "get_bool": get_bool,
        "match_queue_count": lambda: queued,
    }


def _render(template, **context):
    return _env().get_template(template).render(**_context(**context))


def _external_targets(markup):
    parser = Links()
    parser.feed(markup)
    return sorted(link["attrs"].get("href", "") for link in parser.links if link["attrs"].get("target") == "_blank")


def test_provider_link_targets_match_classic_nav_for_every_provider_combination():
    env = _env()
    for bits in product((False, True), repeat=5):
        providers = dict(zip(("abs", "grimmory", "bookorbit", "kavita", "cwa"), bits))
        for overrides in (False, True):
            context = _context(providers=providers, overrides=overrides, shelfmark=True)
            classic = env.get_template("_nav.html").render(**context)
            new_shell = env.get_template("next/base.html").render(**context)
            new_rail = new_shell.split("<aside class=\"rail\"", 1)[1].split("</aside>", 1)[0]
            assert _external_targets(new_rail) == _external_targets(classic), (providers, overrides)


def test_abs_disabled_sentinel_and_disabled_provider_gates_match_classic_nav():
    env = _env()
    for sentinel in ("disabled", "DISABLED", "Disabled"):
        context = _context(providers={"grimmory": True}, abs_url=sentinel, shelfmark=True)
        classic = env.get_template("_nav.html").render(**context)
        new = env.get_template("next/base.html").render(**context)
        rail = new.split("<aside class=\"rail\"", 1)[1].split("</aside>", 1)[0]
        assert _external_targets(rail) == _external_targets(classic)
        assert "https://grimmory.example/server" in _external_targets(rail)
        assert "https://disabled.example/server" not in _external_targets(rail)
    for provider in ("grimmory", "bookorbit", "kavita", "cwa"):
        context = _context(providers={provider: True}, disabled={provider})
        classic = env.get_template("_nav.html").render(**context)
        new = env.get_template("next/base.html").render(**context)
        rail = new.split("<aside class=\"rail\"", 1)[1].split("</aside>", 1)[0]
        assert _external_targets(rail) == _external_targets(classic)


def test_role_navigation_queue_badge_and_logout_are_rendered():
    admin = _render("next/base.html", admin=True, queued=7)
    reader = _render("next/base.html", admin=False, queued=0)
    assert 'href="/stats"' in admin and 'href="/stats"' in reader
    assert 'href="/logs"' in admin and 'href="/settings"' in admin
    assert 'href="/logs"' not in reader and 'href="/settings"' not in reader
    assert 'href="/job-queue"' in admin and 'href="/job-queue"' in reader
    assert 'href="/account/integrations"' in reader
    assert 'href="/logout"' in admin and 'href="/logout"' in reader
    admin_phone_menu = admin.split('id="bb-next-mobile-user-menu"', 1)[1].split("</details>", 1)[0]
    reader_phone_menu = reader.split('id="bb-next-mobile-user-menu"', 1)[1].split("</details>", 1)[0]
    assert 'href="/logs"' in admin_phone_menu
    assert 'href="/logs"' not in reader_phone_menu
    assert '<span class="count">7</span>' in admin
    assert '<span class="count">0</span>' not in reader


def test_mobile_app_menu_and_hidden_chrome_fallback():
    reader = _render("next/base.html", admin=False, providers={"abs": True, "cwa": True})
    assert 'class="mobile-app-menu"' in reader
    assert 'aria-label="My apps"' in reader
    assert 'class="tabbar"' in reader
    assert 'href="https://abs.example/server"' in reader
    assert 'href="https://cwa.example/server"' in reader
    hidden = _render("next/base.html", admin=False, hide_chrome=True)
    assert 'class="app-shell"' not in hidden
    assert 'id="main-content"' in hidden


def test_shelfmark_current_legacy_and_disabled_gates():
    env = _env()
    for kwargs, expected in (({"shelfmark": True}, True), ({"shelfmark": True, "shelfmark_legacy": True}, True),
                             ({"shelfmark": True, "shelfmark_off": True}, False), ({}, False)):
        classic = env.get_template("_nav.html").render(**_context(**kwargs))
        new = env.get_template("next/base.html").render(**_context(**kwargs))
        rail = new.split("<aside class=\"rail\"", 1)[1].split("</aside>", 1)[0]
        assert ("/shelfmark" in _external_targets(rail)) is expected
        assert _external_targets(rail) == _external_targets(classic)


def test_shell_sprite_font_and_static_script_references_exist():
    html = _render("next/base.html", admin=True)
    sprite = (PROJECT_ROOT / "static" / "icons.svg").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "static" / "css" / "next.css").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "static" / "js" / "next" / "shell.js").read_text(encoding="utf-8")
    fonts = PROJECT_ROOT / "static" / "fonts"
    font_css = (fonts / "next-fonts.css").read_text(encoding="utf-8")
    assert "https://" not in font_css
    for asset in set(re.findall(r"/static/fonts/([^)]+)", font_css)):
        assert (fonts / asset).read_bytes()[:4] == b"wOF2"
    for family in ("literata", "instrumentsans", "ibmplexmono"):
        assert "SIL OPEN FONT LICENSE" in (fonts / f"OFL-{family}.txt").read_text(encoding="utf-8")
    assert "/static/fonts/next-fonts.css" in html
    assert "/static/css/next.css" in html and "/static/js/next/shell.js" in html
    parser = Links()
    parser.feed(html)
    referenced = set(re.findall(r"icons\.svg#([\w-]+)", html))
    defined = set(re.findall(r'<symbol id="([\w-]+)"', sprite))
    assert referenced <= defined
    assert "prefers-reduced-motion" in css
    assert "localStorage.getItem" in script and "localStorage.setItem" in script
    assert "event.key === 'Escape'" in script
