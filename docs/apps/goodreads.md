---
hide:
  - toc
description: "Connect Goodreads to BookBridge through a signed-in browser or a session cookie."
---

# Goodreads

Goodreads is a tracker: BookBridge posts your progress, page count and finished books to it. It never moves your place in other apps.

!!! warning "Unofficial"
    Goodreads has no public API, so BookBridge works through your own signed-in session, and updates appear in your feed like ones you post yourself. Goodreads' terms don't allow automated access and its pages can change, so updates may stop until BookBridge is updated.

## 1. Turn it on (admin)

**Settings → Services → Goodreads**

![Settings, Goodreads card](../assets/guide/app-goodreads-settings.webp){ .bb-shot loading=lazy }

<div class="bb-callouts" markdown>
1. **Enable**, then **Save Settings**.
</div>

## 2. Connect your account (each reader)

**Account → My Integrations → Goodreads**

![My Integrations, Goodreads](../assets/guide/app-goodreads-account.webp){ .bb-shot loading=lazy }

<div class="bb-callouts" markdown>
1. **Enable** it for your account.
2. **Signed-in Selenium URL** (recommended): the address of a Chrome that is signed in to Goodreads, for example `http://selenium:4444`.
3. **Browser Cookie header**: without a browser, paste your Goodreads `Cookie` header instead. It is the least reliable option.
</div>

Then **Save integrations**.

??? info "Setting up the signed-in browser"
    Goodreads often blocks plain requests as a bot, so a real browser works best. Run a
    `selenium/standalone-chrome` container on the same Docker network as BookBridge, with `shm_size: 2gb`.
    Open its noVNC view on port 7900 once, sign in to Goodreads and leave that tab open.

    Keep port 4444 off your host's ports: that browser is signed in as you. Each Goodreads account needs its
    own browser.

    A [goodreads-cookie-refresher](https://github.com/Lyfts/goodreads-cookie-refresher) service is the other
    option: enter its address and token as **Cookie refresher URL** and **Cookie refresher token**.
