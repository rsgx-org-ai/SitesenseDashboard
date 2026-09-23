# RSGx PMO Dashboard

A SiteSense data dashboard: pulls Purchase Orders, Receipts, Inventory, Storage,
Transfers, Asset Tracking, FIM and Issuing data across every RSGx project into
one searchable, filterable set of tabs, built as a single static HTML file
with the data baked in.

There are two ways this runs, and both read the same `fetch_data.py` and
`dashboard_template.html`:

- **Local (Windows desktop)** - unchanged, still works exactly as before.
  `fetch_data.py` writes `PMO_Dashboard.html` next to itself; you open it
  straight off disk. `refresh_server.py` is a small local helper (started at
  login, see below) that the dashboard's "Refresh data" button talks to on
  `127.0.0.1:8799`.
- **Hosted (Railway)** - `server.py` is a small always-on web server that
  serves the built dashboard *and* the refresh button's API from the same
  URL, so anyone with the link (and, once you set it up, the password) sees
  live data without needing anything running on their own machine.

## Local setup (unchanged)

1. Copy `config.json.example` to `config.json` and fill in the real
   `auth_header_value`. **Never commit `config.json`** - it holds the live
   SiteSense credentials and is gitignored for exactly that reason.
2. Double-click `Launch Dashboard.bat`. It starts `refresh_server.py` if it
   is not already running, then opens `PMO_Dashboard.html`.
3. To pull fresh data without the button, run `python fetch_data.py` directly.

The existing daily 6am Task Scheduler job and the Startup-folder shortcut for
`refresh_server.py` are untouched by anything below.

## Deploying to Railway

### 1. Push this folder to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-org>/<your-repo>.git
git push -u origin main
```

Before that first commit, double check `git status` does **not** list
`config.json`, `data.json`, `PMO_Dashboard.html`, `PMO_Dashboard_artifact.html`,
`regions.json` or `delivery_dates.json` - `.gitignore` already excludes all of
them, but it is worth a glance, especially if any of them were ever added to
git before this file existed. If one was committed previously, removing it
from the working tree is not enough - it is still in history; ask before
doing anything about that, since rewriting history is disruptive to a shared
repo.

Decide now whether the GitHub repo is public or private. Given this dashboard
surfaces real financial figures, vendor names and staff names, **private** is
the sensible default regardless of what protects the *hosted* copy.

### 2. Create the Railway service

1. In Railway, **New Project → Deploy from GitHub repo**, pick this repo.
   Railway detects it as Python via `requirements.txt` and runs the
   `Procfile`'s `web: python server.py` automatically - no Dockerfile needed.
2. **Add a Volume** (Railway project → your service → Settings → Volumes) and
   mount it at, say, `/data`. Without this, every redeploy or restart wipes
   the container's disk - meaning the data your team enters into Estimated
   Delivery Date, Promised Delivery Date and Regions would be lost, along
   with the pulled data itself until the next scheduled refresh completes.
3. Set these **Variables** (Railway project → your service → Variables):

   | Variable | Value |
   |---|---|
   | `SITESENSE_API_ENDPOINT` | same as `config.json`'s `api_endpoint` |
   | `SITESENSE_AUTH_HEADER` | same as `config.json`'s `auth_header_value` |
   | `DASHBOARD_USER` | a username your team will use to view the dashboard |
   | `DASHBOARD_PASSWORD` | a password for the same |
   | `DATA_DIR` | `/data` (must match the Volume's mount path from step 2) |
   | `REFRESH_HOUR_UTC` | optional, defaults to `22` (≈ 6am AWST) |

   Railway sets `PORT` itself - do not add it yourself.

4. Deploy. First boot has no data yet, so it shows a small "first refresh in
   progress" page that auto-refreshes every 15 seconds and starts pulling
   data immediately - the first pull typically takes 15-20 minutes given the
   size of the SiteSense dataset (tens of thousands of rows across several
   document types), same as it does locally.

### Why a Volume, and why Basic Auth

- **Volume**: Railway containers are ephemeral by default - local disk resets
  on every redeploy. `regions.json` and `delivery_dates.json` are real,
  manually-entered data (which project is East/West, delivery dates), not
  something to regenerate from scratch each time. `data.json` and the built
  dashboard also survive between deploys this way, so a redeploy does not
  force a 15-20 minute wait before anyone can see anything.
- **Basic Auth**: the dashboard is not aggregate/anonymised - it shows real PO
  values, vendor names and who checked in each receipt. `DASHBOARD_USER` /
  `DASHBOARD_PASSWORD` gate every route, including `/refresh`, so a public
  Railway URL is not also a public "hit the SiteSense API on demand" button
  for anyone who finds the link. Leaving both unset is possible (server.py
  prints a loud warning and runs unauthenticated) but is only meant for a
  quick throwaway test, not real use.

### Updating the deployed dashboard

Railway redeploys automatically on every push to the branch it is watching
(usually `main`). Code changes (template, `fetch_data.py`, `server.py`) ship
this way; the data itself refreshes on its own daily schedule or via the
"Refresh data" button, independent of deploys.

## What changed to make this possible

- `fetch_data.py` reads credentials from `SITESENSE_API_ENDPOINT` /
  `SITESENSE_AUTH_HEADER` first, falling back to `config.json` - so local use
  is completely unaffected.
- `fetch_data.py` writes its generated files under `DATA_DIR` (defaults to its
  own folder, i.e. unchanged locally; Railway points it at the mounted
  Volume) instead of always next to itself.
- `dashboard_template.html`'s refresh button targets `127.0.0.1:8799` only
  when opened via `file://` (the local workflow); served over http(s) it
  calls the same origin instead, which is what makes one template work for
  both `refresh_server.py` locally and `server.py` on Railway.
- `server.py` is new and Railway-only: it serves the dashboard, gates
  everything behind Basic Auth, and runs the daily refresh in-process since
  Railway has no Task Scheduler equivalent. `refresh_server.py` is untouched.

## Files

| File | Purpose |
|---|---|
| `fetch_data.py` | Pulls SiteSense data, writes `data.json` + the built dashboard |
| `dashboard_template.html` | The dashboard's HTML/CSS/JS, data-free |
| `refresh_server.py` | Local-only refresh helper (Windows desktop workflow) |
| `server.py` | Hosted web server for Railway (dashboard + refresh + daily schedule) |
| `config.json` / `config.json.example` | SiteSense credentials (local dev only, gitignored) |
| `.env.example` | Documents the environment variables `server.py` and Railway need |
| `regions.json`, `delivery_dates.json` | Manually-entered overlays, not derived from SiteSense |
| `data.json`, `PMO_Dashboard*.html` | Generated output, gitignored, rebuilt by `fetch_data.py` |
