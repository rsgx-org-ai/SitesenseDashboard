"""Hosted entry point for Railway (or any always-on server, container or VM).

This is the Railway counterpart to refresh_server.py, which stays untouched
and keeps serving the existing local Windows workflow (Launch Dashboard.bat +
Task Scheduler + Startup folder). This file exists instead of modifying that
one because the two run in genuinely different situations: refresh_server.py
only ever talks to 127.0.0.1 for a single desktop user who opens the dashboard
straight off disk; this one is a real multi-visitor web server, so it also has
to serve the dashboard page itself and gate everything behind a login, which
refresh_server.py deliberately never needed to.

Routes, all behind HTTP Basic Auth when DASHBOARD_USER/DASHBOARD_PASSWORD are
both set (see .env.example - strongly recommended, since the dashboard shows
real vendor names, staff names and dollar values):

    GET  /            -> the built dashboard (PMO_Dashboard.html), or a small
                          "first refresh in progress" page before the first
                          fetch has ever completed
    GET  /status       -> current refresh state as JSON (same contract as
                          refresh_server.py, so the dashboard's existing
                          "Refresh data" button JS works against this too)
    POST /refresh      -> starts fetch_data.py if nothing is already running

Also runs a daily background refresh (REFRESH_HOUR_UTC, default 22 UTC ~=
6am AWST) so the hosted copy does not depend on anyone clicking the button -
Railway has no Windows Task Scheduler to do this instead.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8080"))
REFRESH_HOUR_UTC = int(os.environ.get("REFRESH_HOUR_UTC", "22"))
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASSWORD)

DASHBOARD_FILE = DATA_DIR / "PMO_Dashboard.html"
MAX_TAIL = 40

state_lock = threading.Lock()
state = {
    "phase": "idle",       # idle | running | done | error
    "startedAt": None,
    "finishedAt": None,
    "returncode": None,
    "tail": [],
}


def _run_fetch():
    with state_lock:
        already = state["phase"] == "running"
        if already:
            return
        state.update(phase="running", startedAt=time.time(), finishedAt=None,
                     returncode=None, tail=[])

    proc = subprocess.Popen(
        # "-u": fetch_data.py's stdout would otherwise be fully block-buffered
        # once redirected to a pipe, so the live tail below would sit empty
        # for long stretches and then dump many lines at once. env is
        # inherited, which is how fetch_data.py sees SITESENSE_*, DATA_DIR.
        [sys.executable, "-u", "fetch_data.py"],
        cwd=str(HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        with state_lock:
            state["tail"] = (state["tail"] + [line.rstrip()])[-MAX_TAIL:]
    proc.wait()

    with state_lock:
        state["phase"] = "done" if proc.returncode == 0 else "error"
        state["finishedAt"] = time.time()
        state["returncode"] = proc.returncode


def _daily_scheduler():
    """Mirrors the Windows Task Scheduler job the local setup relies on -
    Railway has no equivalent, so this thread is the hosted replacement."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=REFRESH_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(1.0, (target - now).total_seconds()))
        print(f"[scheduler] kicking off the daily refresh ({REFRESH_HOUR_UTC}:00 UTC)")
        _run_fetch()


PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>RSGx PMO Dashboard</title>
<meta http-equiv="refresh" content="15">
<style>body{{font:15px system-ui,sans-serif;max-width:640px;margin:15vh auto;
padding:0 20px;color:#333}} .dot{{display:inline-block;width:8px;height:8px;
border-radius:50%;background:{color};margin-right:8px}}</style></head><body>
<p><span class="dot"></span>{message}</p>
<p style="color:#777;font-size:13px">This page refreshes itself every 15 seconds.</p>
</body></html>"""


def _placeholder(message, color="#e0a020"):
    return PLACEHOLDER_HTML.format(message=message, color=color).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        if not AUTH_ENABLED:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return user == DASHBOARD_USER and password == DASHBOARD_PASSWORD

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="RSGx PMO Dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, body_bytes):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if not self._authorized():
            self._require_auth()
            return
        if self.path == "/status":
            with state_lock:
                self._json(200, dict(state))
        elif self.path in ("/", "/index.html"):
            if DASHBOARD_FILE.exists():
                self._html(200, DASHBOARD_FILE.read_bytes())
            else:
                with state_lock:
                    running = state["phase"] == "running"
                msg = ("First data pull is under way - this can take 15-20 minutes."
                       if running else
                       "No data pulled yet. Starting the first refresh now - this can take 15-20 minutes.")
                if not running:
                    threading.Thread(target=_run_fetch, daemon=True).start()
                self._html(200, _placeholder(msg))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._require_auth()
            return
        if self.path == "/refresh":
            with state_lock:
                already = state["phase"] == "running"
            if already:
                self._json(200, {"started": False, "reason": "already running"})
                return
            threading.Thread(target=_run_fetch, daemon=True).start()
            self._json(200, {"started": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def main():
    if not AUTH_ENABLED:
        print("!! DASHBOARD_USER / DASHBOARD_PASSWORD not set - this dashboard "
              "is reachable by anyone with the link. Set both as soon as "
              "possible: it shows real vendor names, staff names and dollar "
              "values.")
    threading.Thread(target=_daily_scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on 0.0.0.0:{PORT} (auth {'ON' if AUTH_ENABLED else 'OFF'}, "
          f"data dir {DATA_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    main()
