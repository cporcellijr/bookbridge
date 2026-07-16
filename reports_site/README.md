# BookBridge Diagnostics Dashboard

Private, local dashboard for the opt-in diagnostics pipeline. It reads the
receiver API on demand, presents Bugscout findings in plain language, links
findings to written user feedback, and saves maintainer responses without ever
sending the receiver token to the browser.

## What it shows

- Current anomaly, Bugscout, feedback, and seven-day installation totals.
- A clickable active-anomaly list with Bugscout's pattern and suggested action.
- Finding detail with the hypothesis up front and escaped technical evidence
  collapsed below it.
- Manual submissions that contain a written user message. Response forms exist
  only on those submissions and update the submission-level receiver response.

The dashboard is deliberately not a ticket tracker. Status management and code
changes remain in the normal BookBridge review workflow.

## Configuration

The Compose service reuses the two local files already used by the scheduled
diagnostics scan:

- `../docs/automated-review/review-state.json` supplies
  `diagnostics_scan.endpoint`.
- `%USERPROFILE%/.bookbridge/diagnostics-read.key` is mounted read-only as a
  Compose secret.

Both files are read on each receiver request, so endpoint or token rotation does
not require rebuilding the image. The token is used only in server-side
`Authorization` headers and is never rendered into HTML.

Remote receiver endpoints must use HTTPS; plain HTTP is accepted only for
loopback or Docker's local host gateway during development. Redirects are
refused so the admin token cannot follow a response to another URL. Dashboard
timestamps are labeled in UTC.

## Run it

From `reports_site/`:

```powershell
docker compose up -d --build
```

Open <http://localhost:5761>. Compose binds the port to `127.0.0.1` only, and
the application also rejects non-loopback Host headers.

## Development

```powershell
python -m pip install -r reports_site/requirements.txt
python reports_site/app.py
pytest -q tests/test_reports_dashboard.py
```

Receiver failures are shown as generic dashboard errors so endpoint details and
credentials cannot leak into the page or logs.
