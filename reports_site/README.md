# BookBridge Diagnostics Dashboard

Private, local dashboard for the opt-in diagnostics pipeline. It reads the
receiver API on demand, presents Bugscout findings in plain language, links
findings to written user feedback, and saves maintainer responses without ever
sending the receiver token to the browser.

## What it shows

- An inbox-first home page where each written user report appears once, even
  when that submission contains many warning findings.
- Seven-day installation, report, warning, reviewed, and Bugscout-queue totals.
- Clickable Bugscout category totals for actionable code bugs, configuration
  issues, documentation gaps, environment problems, and unclassified findings.
  These totals retain archived evidence so repeated setup or docs confusion is
  still visible after individual findings are reviewed.
- Bugscout-reviewed anomalies by default, with unreviewed raw findings behind a
  separate filter instead of mixed into the actionable list.
- Finding detail with the hypothesis and next step up front, links back to user
  reports, a ready-made Codex/Claude prompt, and escaped technical evidence
  collapsed below it. Review controls mark a finding fixed, reviewed with no
  action, or reopened; completed reviews remain available under Archived.
- Report detail that puts reviewed anomalies first and collapses findings still
  waiting for Bugscout. Response forms exist only when the user wrote a message
  and update the submission-level receiver response.

The dashboard is deliberately a small review queue rather than a ticket tracker.
It reuses the receiver's existing finding statuses and leaves code changes in the
normal BookBridge development workflow.

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
