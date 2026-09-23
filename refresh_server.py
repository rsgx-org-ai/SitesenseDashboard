"""Local helper that lets the dashboard's "Refresh data" button pull fresh
SiteSense data on demand, without waiting for the 6am scheduled task.

Runs once per Windows login (see the "RSGx PMO Dashboard Refresh Server"
scheduled task) and listens on 127.0.0.1 only - never reachable from another
machine. It exposes two endpoints:

    GET  /status   -> current state as JSON
    POST /refresh  -> starts fetch_data.py if nothing is already running

It never talks to SiteSense itself; it only shells out to the same
fetch_data.py the scheduled task runs, so the API key never passes through
this server or through the browser. If the button is clicked while this
helper is not running, the fetch simply fails to reach it - nothing silently
breaks, and no credentials are ever sent to the page.
"""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = 8799
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
        state.update(phase="running", startedAt=time.time(), finishedAt=None,
                     returncode=None, tail=[])

    proc = subprocess.Popen(
        # "-u": fetch_data.py's stdout would otherwise be fully block-buffered
        # once redirected to a pipe, so the live tail below would sit empty
        # for long stretches and then dump many lines at once.
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


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        # Wide open on purpose: this only ever accepts loopback connections
        # (bound to 127.0.0.1), so the only caller that can reach it is
        # something already running on this machine. The private-network
        # header answers Chrome's Private Network Access preflight, which a
        # public https page (the hosted artifact link) triggers when it
        # calls a loopback server - without it, that preflight fails closed
        # before the request ever reaches this handler.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            with state_lock:
                self._json(200, dict(state))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
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
        pass  # state is visible via /status; keep the console quiet


def main():
    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use - assuming another instance "
              f"of this helper is already running. Exiting quietly.")
        return
    print(f"Refresh helper listening on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
