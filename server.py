"""Hosted entry point for Railway (or any always-on server, container or VM).

The Railway counterpart to refresh_server.py, which stays untouched and keeps
serving the local Windows workflow (Launch Dashboard.bat + Task Scheduler).
This is a real multi-user web server, so on top of the refresh API it also
handles logins, roles and per-project access, none of which the single-desktop
local setup needs.

Who sees what:
    admin  - every project; the Users tab (add/remove users, roles, project
             assignments, password resets); the Refresh button; edits to the
             shared Regions and Estimated/Promised Delivery Date overlays
    viewer - only their assigned projects, read-only. Their page is built from
             a filtered copy of the data (data_filter.py), so other projects'
             rows are never sent to their browser at all

Routes:
    GET  /login, POST /login, POST /logout     sign in / out (cookie session)
    GET  /                                     the dashboard, built per user
    GET  /status                               refresh progress (any signed-in user)
    POST /refresh                              start a data pull (admin)
    GET  /api/me, POST /api/password           current user / change own password
    GET|POST /api/users, PUT|DELETE /api/users/<name>     user admin (admin)
    PUT  /api/regions, PUT /api/delivery-dates             shared overlays (admin)

Accounts live in DATA_DIR/users.json. On first boot with no accounts, the admin
is created from DASHBOARD_USER / DASHBOARD_PASSWORD; after that, users are
managed from the Users tab and those variables are ignored. With neither, the
server refuses all sign-ins rather than running open.

Also runs the daily refresh in-process (REFRESH_HOUR_UTC, default 22 UTC ~= 6am
AWST), since Railway has no Windows Task Scheduler.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from data_filter import FilterError, filter_data
from users import ROLES, UserError, UserStore

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8080"))
REFRESH_HOUR_UTC = int(os.environ.get("REFRESH_HOUR_UTC", "22"))
COOKIE = "ssd_session"
MAX_BODY = 1_000_000
MAX_TAIL = 40

TEMPLATE_FILE = HERE / "dashboard_template.html"
DATA_FILE = DATA_DIR / "data.json"
REGION_FILE = DATA_DIR / "regions.json"
DELIVERY_FILE = DATA_DIR / "delivery_dates.json"
REGION_CHOICES = ("East", "West", "")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

store = UserStore(DATA_DIR)
overlay_lock = threading.Lock()

# ------------------------------------------------------------------ refresh
state_lock = threading.Lock()
state = {"phase": "idle", "startedAt": None, "finishedAt": None, "returncode": None, "tail": []}


def _run_fetch():
    with state_lock:
        if state["phase"] == "running":
            return
        state.update(phase="running", startedAt=time.time(), finishedAt=None,
                     returncode=None, tail=[])
    # "-u" keeps the live tail flowing. SITESENSE_* are inherited; DATA_DIR is
    # passed explicitly so the pull always writes where this server reads, even
    # when the variable is unset and the two scripts' defaults would differ.
    proc = subprocess.Popen([sys.executable, "-u", "fetch_data.py"], cwd=str(HERE),
                            env={**os.environ, "DATA_DIR": str(DATA_DIR)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        with state_lock:
            state["tail"] = (state["tail"] + [line.rstrip()])[-MAX_TAIL:]
    proc.wait()
    with state_lock:
        state.update(phase="done" if proc.returncode == 0 else "error",
                     finishedAt=time.time(), returncode=proc.returncode)


def start_refresh():
    with state_lock:
        if state["phase"] == "running":
            return False
    threading.Thread(target=_run_fetch, daemon=True).start()
    return True


def _daily_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=REFRESH_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(1.0, (target - now).total_seconds()))
        print(f"[scheduler] daily refresh ({REFRESH_HOUR_UTC}:00 UTC)")
        _run_fetch()


# -------------------------------------------------------- data and overlays
_cache_lock = threading.Lock()
_cache = {}   # path -> (mtime, parsed)


def _read_json(path, default):
    """Parse a JSON file, reusing the last parse while its mtime is unchanged."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return default, 0
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1], mtime
    parsed = json.loads(path.read_text(encoding="utf-8"))
    with _cache_lock:
        _cache[path] = (mtime, parsed)
    return parsed, mtime


def _write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def project_catalog():
    data, _ = _read_json(DATA_FILE, None)
    return [{"id": p["id"], "name": p["name"]} for p in (data or {}).get("projects", [])]


_page_lock = threading.Lock()
_page_cache = {}   # scope key -> (version tuple, bytes)


def build_page(user):
    """The dashboard HTML for this user, with only their projects' data inside."""
    data, data_m = _read_json(DATA_FILE, None)
    if data is None:
        return None
    regions, reg_m = _read_json(REGION_FILE, {})
    dates, del_m = _read_json(DELIVERY_FILE, {})
    tmpl_m = TEMPLATE_FILE.stat().st_mtime
    is_admin = user["role"] == "admin"
    scope = "ALL" if is_admin else tuple(user.get("projects", []))
    version = (data_m, reg_m, del_m, tmpl_m, user["username"], user["role"])
    with _page_lock:
        hit = _page_cache.get(scope)
        if hit and hit[0] == version:
            return hit[1]

    # Overlays are read live here rather than baked at refresh time, so an
    # admin's edit shows for everyone on their next page load.
    merged = dict(data)
    merged["projects"] = [{**p, "region": regions.get(str(p["id"]), p.get("region", ""))}
                          for p in data["projects"]]
    merged["delivery_dates"] = dates
    payload = merged if is_admin else filter_data(merged, user.get("projects", []))

    context = {"username": user["username"], "role": user["role"]}
    def js(v):   # "</" must not appear raw inside a <script> block
        return json.dumps(v, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace(
        "/*__SITESENSE_DATA__*/",
        f"const SITESENSE_DATA = {js(payload)};\nconst SERVER_CONTEXT = {js(context)};")
    body = html.encode("utf-8")
    with _page_lock:
        _page_cache[scope] = (version, body)
    return body


# --------------------------------------------------------------- page shells
SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSGx PMO Dashboard</title>{refresh}
<style>
:root{{color-scheme:light dark;--bg:#fbfaf9;--ink:#2a2523;--ink2:#6b625e;--rule:#e2dcd8;--accent:#c8102e;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1d1a19;--ink:#f3efed;--ink2:#b3aaa6;--rule:#3b3533;--card:#262221}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:380px;margin:14vh auto;padding:0 20px}}
.card{{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:26px 24px}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:var(--ink2);margin:0 0 20px;font-size:13px}}
.rule{{display:inline-block;width:22px;height:3px;background:var(--accent);border-radius:2px;margin-right:9px;vertical-align:middle}}
label{{display:block;font-size:12px;font-weight:600;color:var(--ink2);margin:14px 0 5px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border:1px solid var(--rule);border-radius:6px;background:var(--bg);color:var(--ink)}}
button{{margin-top:20px;width:100%;font:600 14px system-ui,sans-serif;padding:9px;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}}
.err{{background:#c8102e1a;border:1px solid #c8102e55;border-radius:6px;padding:9px 11px;margin-bottom:6px;font-size:13px}}
.note{{color:var(--ink2);font-size:13px}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def shell(body, auto_refresh=False):
    return SHELL.format(body=body, refresh='<meta http-equiv="refresh" content="15">' if auto_refresh else "").encode("utf-8")


def login_page(error="", username=""):
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    if not store.has_users():
        err = ('<div class="err">No accounts exist yet. Set <b>DASHBOARD_USER</b> and '
               '<b>DASHBOARD_PASSWORD</b> in Railway\'s Variables tab to create the first '
               'admin, then reload.</div>')
    return shell(f"""<h1><span class="rule"></span>RSGx PMO Dashboard</h1>
<p class="sub">SiteSense - sign in to continue</p>{err}
<form method="post" action="/login" autocomplete="on">
<label for="u">Username</label><input id="u" name="username" value="{escape(username)}" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>""")


def message_page(title, text, auto_refresh=False):
    return shell(f'<h1><span class="rule"></span>{escape(title)}</h1><p class="note">{text}</p>'
                 '<form method="post" action="/logout"><button type="submit" '
                 'style="background:transparent;color:var(--ink2);border:1px solid var(--rule)">'
                 'Sign out</button></form>', auto_refresh)


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "SiteSenseDashboard"

    # -- plumbing
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload, headers=()):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json", headers)

    def _redirect(self, to, headers=()):
        self._send(303, b"", headers=[("Location", to), *headers])

    def _https(self):
        return self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"

    def _session_cookie(self, user):
        secure = "; Secure" if self._https() else ""
        return ("Set-Cookie", f"{COOKIE}={store.make_cookie_value(user)}; Path=/; HttpOnly; "
                              f"SameSite=Lax; Max-Age={7 * 86400}{secure}")

    def _clear_cookie(self):
        return ("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def _user(self):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE)
        return store.user_from_cookie(morsel.value) if morsel else None

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise UserError("Request too large.")
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        try:
            value = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            raise UserError("Malformed request.")
        if not isinstance(value, dict):
            raise UserError("Malformed request.")
        return value

    def _api_guard(self, admin=False):
        """Signed in (and admin, if asked), plus the CSRF header on writes.

        Pages call these endpoints with an X-Requested-With header. A browser
        will not attach a custom header to a cross-site request without a CORS
        preflight this server never approves, so a forged request from another
        site cannot reach them even though the session cookie would ride along.
        """
        user = self._user()
        if not user:
            self._json(401, {"error": "Signed out - reload the page and sign in again."})
            return None
        if self.command != "GET" and self.headers.get("X-Requested-With") != "dashboard":
            self._json(403, {"error": "Missing request header."})
            return None
        if admin and user["role"] != "admin":
            self._json(403, {"error": "Admins only."})
            return None
        return user

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    # -- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            if self._user():
                return self._redirect("/")
            return self._send(200, login_page())
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")

        if path in ("/", "/index.html"):
            user = self._user()
            if not user:
                return self._redirect("/login")
            if user["role"] != "admin" and not user.get("projects"):
                return self._send(200, message_page(
                    "No projects assigned yet",
                    f"You're signed in as <b>{escape(user['username'])}</b>, but no projects "
                    "have been assigned to you. Ask a dashboard admin to add you to a project."))
            try:
                page = build_page(user)
            except FilterError as exc:
                print(f"!! refusing to serve a filtered page: {exc}")
                return self._send(500, message_page(
                    "Dashboard temporarily unavailable",
                    "The data format changed and project filtering needs updating before it "
                    "can be shown safely. Ask a dashboard admin to check the server logs."))
            if page is None:
                with state_lock:
                    snap = dict(state)
                # A pull that just failed (e.g. missing SiteSense variables) is not
                # retried on every 15-second reload; it waits ten minutes or an admin.
                recent_fail = snap["phase"] == "error" and time.time() - (snap["finishedAt"] or 0) < 600
                if not recent_fail:
                    start_refresh()
                    return self._send(200, message_page(
                        "First data pull in progress",
                        "No data has been pulled yet. The first refresh is running and can take "
                        "15-20 minutes. This page checks again every 15 seconds.", auto_refresh=True))
                last = escape(snap["tail"][-1]) if snap["tail"] else "no output"
                detail = (f"<br><br>Last line of output:<br><code>{last}</code>"
                          if user["role"] == "admin" else " An admin has been told via the server logs.")
                return self._send(200, message_page(
                    "First data pull failed",
                    f"The first refresh could not complete and will be retried in a few minutes.{detail}",
                    auto_refresh=True))
            return self._send(200, page)

        if path == "/status":
            if not self._api_guard():
                return
            with state_lock:
                return self._json(200, dict(state))
        if path == "/api/me":
            user = self._api_guard()
            if user:
                self._json(200, store.public(user))
            return
        if path == "/api/users":
            if self._api_guard(admin=True):
                self._json(200, {"users": store.list_public(), "projects": project_catalog(), "roles": ROLES})
            return
        self._json(404, {"error": "not found"})

    # -- POST / PUT / DELETE
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/login":
                return self._login()
            if path == "/logout":
                return self._redirect("/login", [self._clear_cookie()])
            if path == "/refresh":
                if self._api_guard(admin=True):
                    started = start_refresh()
                    self._json(200, {"started": started, **({} if started else {"reason": "already running"})})
                return
            if path == "/api/password":
                user = self._api_guard()
                if user:
                    body = self._json_body()
                    updated = store.change_own_password(user["username"], str(body.get("current", "")),
                                                        str(body.get("new", "")))
                    self._json(200, {"ok": True}, [self._session_cookie(updated)])
                return
            if path == "/api/users":
                if self._api_guard(admin=True):
                    b = self._json_body()
                    created = store.create(str(b.get("username", "")), str(b.get("password", "")),
                                           b.get("role"), b.get("projects") or [])
                    self._json(200, {"user": created})
                return
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/users/"):
                admin = self._api_guard(admin=True)
                if not admin:
                    return
                name = unquote(path[len("/api/users/"):])
                b = self._json_body()
                updated = store.update(name, role=b.get("role"), projects=b.get("projects"),
                                       password=b.get("password"))
                headers = []
                if store._key(name) == store._key(admin["username"]):
                    headers.append(self._session_cookie(updated))   # editing yourself keeps you signed in
                return self._json(200, {"user": store.public(updated)}, headers)
            if path == "/api/regions":
                if self._api_guard(admin=True):
                    self._save_regions(self._json_body())
                return
            if path == "/api/delivery-dates":
                if self._api_guard(admin=True):
                    self._save_delivery_date(self._json_body())
                return
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._json(400, {"error": str(exc)})

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/users/"):
                admin = self._api_guard(admin=True)
                if not admin:
                    return
                name = unquote(path[len("/api/users/"):])
                if store._key(name) == store._key(admin["username"]):
                    raise UserError("You can't remove your own account.")
                store.delete(name)
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._json(400, {"error": str(exc)})

    # -- actions
    def _login(self):
        form = parse_qs(self._body().decode("utf-8", "replace"))
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        wait = store.locked_for(username)
        if wait:
            return self._send(429, login_page(
                f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).", username))
        user = store.authenticate(username, password)
        if not user:
            wait = store.locked_for(username)
            msg = (f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s)."
                   if wait else "Incorrect username or password.")
            return self._send(401, login_page(msg, username))
        self._redirect("/", [self._session_cookie(user)])

    def _save_regions(self, body):
        known = {str(p["id"]) for p in project_catalog()}
        with overlay_lock:
            regions, _ = _read_json(REGION_FILE, {})
            regions = dict(regions)
            for pid, region in body.items():
                if str(pid) in known and region in REGION_CHOICES:
                    regions[str(pid)] = region
            _write_json(REGION_FILE, regions)
        self._json(200, {"ok": True})

    def _save_delivery_date(self, body):
        guid, field, value = str(body.get("guid", "")), body.get("field"), str(body.get("value") or "")
        if not guid or len(guid) > 100 or field not in ("estimated", "promised"):
            raise UserError("Unknown delivery date field.")
        if value and not DATE_RE.match(value):
            raise UserError("Dates must be YYYY-MM-DD.")
        with overlay_lock:
            dates, _ = _read_json(DELIVERY_FILE, {})
            dates = {k: dict(v) for k, v in dates.items()}
            entry = dates.get(guid, {})
            if value:
                entry[field] = value
            else:
                entry.pop(field, None)
            if entry:
                dates[guid] = entry
            else:
                dates.pop(guid, None)
            _write_json(DELIVERY_FILE, dates)
        self._json(200, {"ok": True})


def main():
    if store.bootstrap(os.environ.get("DASHBOARD_USER", ""), os.environ.get("DASHBOARD_PASSWORD", "")):
        print(f"Created the first admin account ({os.environ.get('DASHBOARD_USER')}) from "
              f"DASHBOARD_USER/DASHBOARD_PASSWORD. Manage users from the dashboard's Users tab from now on.")
    if not store.has_users():
        print("!! No user accounts and no DASHBOARD_USER/DASHBOARD_PASSWORD set - every sign-in "
              "will be refused until they are.")
    threading.Thread(target=_daily_scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on 0.0.0.0:{PORT} (data dir {DATA_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    main()
