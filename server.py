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
    standard - only their assigned projects; can also edit those projects'
             Estimated/Promised Delivery Dates
    viewer - only their assigned projects, read-only. Viewer and standard
             pages are built from a filtered copy of the data (data_filter.py),
             so other projects' rows are never sent to their browser at all
    Everyone can comment on Inventory, Storage and Receipts rows they can see;
    authors delete their own comments, admins any.

Routes:
    GET  /login, POST /login, POST /logout     sign in / out (cookie session)
    GET  /                                     the dashboard, built per user
    GET  /status                               refresh progress (any signed-in user)
    POST /refresh                              start a data pull (admin)
    GET  /api/me, POST /api/password           current user / change own password
    GET|POST /api/users, PUT|DELETE /api/users/<name>     user admin (admin)
    PUT  /api/regions                                      shared overlay (admin)
    PUT  /api/delivery-dates                               shared overlay (admin, standard)
    POST /api/comments, DELETE /api/comments/<id>          row comments (everyone)

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
import secrets
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
COMMENTS_FILE = DATA_DIR / "comments.json"
REGION_CHOICES = ("East", "West", "")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A comment's row key starts with its table and SiteSense project id, e.g.
# "inv|14214|<material guid>" - the project id is what access is checked on.
COMMENT_KEY_RE = re.compile(r"^(inv|sto|rcp)\|(\d{1,9})\|.{1,500}$", re.S)
COMMENT_MAX = 2000

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


def allowed_projects(user):
    """SiteSense project ids this user may see, or None meaning every project."""
    return None if user["role"] == "admin" else {int(p) for p in user.get("projects", [])}


_guid_projects = {"mtime": None, "map": {}}


def material_projects(guid):
    """Which projects a material GUID appears in, so a standard user can only
    change delivery dates on materials inside their own projects."""
    data, mtime = _read_json(DATA_FILE, None)
    with _cache_lock:
        if _guid_projects["mtime"] != mtime:
            mapping = {}
            if data:
                strings, mats, projects = data["strings"], data["materials"], data["projects"]
                for table in ("inventory", "storage", "transfers", "assets", "receipts", "fim", "issuing"):
                    for row in data.get(table, []):
                        g = strings[mats[row[1]][4]]
                        mapping.setdefault(g, set()).add(int(projects[row[0]]["id"]))
            _guid_projects.update(mtime=mtime, map=mapping)
        return _guid_projects["map"].get(guid, set())


def comments_for(user):
    """The comment threads on rows this user can see (keyed by row key)."""
    comments, _ = _read_json(COMMENTS_FILE, {})
    allowed = allowed_projects(user)
    if allowed is None:
        return comments
    out = {}
    for key, thread in comments.items():
        m = COMMENT_KEY_RE.match(key)
        if m and int(m.group(2)) in allowed:
            out[key] = thread
    return out


_page_lock = threading.Lock()
_page_cache = {}   # scope key -> (version tuple, bytes)


def build_page(user):
    """The dashboard HTML for this user, with only their projects' data inside."""
    data, data_m = _read_json(DATA_FILE, None)
    if data is None:
        return None
    regions, reg_m = _read_json(REGION_FILE, {})
    dates, del_m = _read_json(DELIVERY_FILE, {})
    _, com_m = _read_json(COMMENTS_FILE, {})
    tmpl_m = TEMPLATE_FILE.stat().st_mtime
    is_admin = user["role"] == "admin"
    scope = "ALL" if is_admin else tuple(user.get("projects", []))
    version = (data_m, reg_m, del_m, com_m, tmpl_m, user["username"], user["role"])
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

    context = {"username": user["username"], "role": user["role"], "comments": comments_for(user)}
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
<title>Sitesense Dashboard</title>{refresh}
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
    return shell(f"""<h1><span class="rule"></span>Sitesense Dashboard</h1>
<p class="sub">Sign in to continue</p>{err}
<form method="post" action="/login" autocomplete="on">
<label for="u">Username</label><input id="u" name="username" value="{escape(username)}" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>""")


def change_password_page(user, error=""):
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    forced = user.get("must_change_password")
    intro = ("You're signed in with a temporary password. Choose your own to continue."
             if forced else "Choose a new password.")
    cancel = "" if forced else '<p class="note" style="margin-top:14px"><a href="/">Back to the dashboard</a></p>'
    return shell(f"""<h1><span class="rule"></span>Choose a new password</h1>
<p class="sub">Signed in as <b>{escape(user['username'])}</b>. {intro}</p>{err}
<form method="post" action="/change-password" autocomplete="on">
<input type="text" name="username" value="{escape(user['username'])}" autocomplete="username" hidden>
<label for="c">{'Temporary password' if forced else 'Current password'}</label>
<input id="c" name="current" type="password" autocomplete="current-password" required autofocus>
<label for="n">New password</label>
<input id="n" name="new" type="password" autocomplete="new-password" minlength="6" required>
<label for="r">Confirm new password</label>
<input id="r" name="confirm" type="password" autocomplete="new-password" minlength="6" required>
<p class="note" style="margin:10px 0 0">At least 6 characters.</p>
<button type="submit">Save password and continue</button></form>
<form method="post" action="/logout"><button type="submit"
 style="background:transparent;color:var(--ink2);border:1px solid var(--rule);margin-top:10px">Sign out</button></form>{cancel}""")


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
        if user.get("must_change_password"):
            # Nothing works on a temporary password except choosing a new one.
            self._json(403, {"error": "Choose a new password first - reload the page."})
            return None
        if admin and user["role"] != "admin":
            self._json(403, {"error": "Admins only."})
            return None
        return user

    def _reject(self, exc):
        # Logged so a rejected request can be diagnosed later; messages never include passwords.
        print(f"!! {self.command} {urlparse(self.path).path} rejected: {exc}")
        self._json(400, {"error": str(exc)})

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
        if path == "/change-password":
            user = self._user()
            if not user:
                return self._redirect("/login")
            return self._send(200, change_password_page(user))

        if path in ("/", "/index.html"):
            user = self._user()
            if not user:
                return self._redirect("/login")
            if user.get("must_change_password"):
                return self._redirect("/change-password")
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
            if path == "/change-password":
                return self._change_password_form()
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
            if path == "/api/comments":
                user = self._api_guard()
                if user:
                    self._add_comment(self._json_body(), user)
                return
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._reject(exc)

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
                user = self._api_guard()
                if not user:
                    return
                if user["role"] not in ("admin", "standard"):
                    return self._json(403, {"error": "Your role can't change delivery dates."})
                self._save_delivery_date(self._json_body(), user)
                return
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._reject(exc)

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
            if path.startswith("/api/comments/"):
                user = self._api_guard()
                if user:
                    self._delete_comment(unquote(path[len("/api/comments/"):]), user)
                return
            self._json(404, {"error": "not found"})
        except UserError as exc:
            self._reject(exc)

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
        # A temporary (admin-set) password goes straight to choosing a new one.
        self._redirect("/change-password" if user.get("must_change_password") else "/",
                       [self._session_cookie(user)])

    def _change_password_form(self):
        user = self._user()
        if not user:
            return self._redirect("/login")
        form = parse_qs(self._body().decode("utf-8", "replace"))
        current = (form.get("current") or [""])[0]
        new = (form.get("new") or [""])[0]
        if new != (form.get("confirm") or [""])[0]:
            return self._send(400, change_password_page(user, "The new passwords don't match."))
        try:
            updated = store.change_own_password(user["username"], current, new)
        except UserError as exc:
            print(f"!! POST /change-password rejected for {user['username']}: {exc}")
            return self._send(400, change_password_page(user, str(exc)))
        self._redirect("/", [self._session_cookie(updated)])

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

    def _comment_key(self, key, user):
        """Validate a row key and check the user can see that row's project."""
        m = COMMENT_KEY_RE.match(key or "")
        if not m:
            raise UserError("Unknown row.")
        allowed = allowed_projects(user)
        if allowed is not None and int(m.group(2)) not in allowed:
            raise UserError("That row isn't in one of your projects.")
        return key

    def _add_comment(self, body, user):
        key = self._comment_key(str(body.get("key", "")), user)
        text = str(body.get("text", "")).strip()
        if not text:
            raise UserError("Comment is empty.")
        if len(text) > COMMENT_MAX:
            raise UserError(f"Comments are limited to {COMMENT_MAX} characters.")
        entry = {"id": secrets.token_hex(6), "user": user["username"], "text": text,
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with overlay_lock:
            comments, _ = _read_json(COMMENTS_FILE, {})
            comments = {k: list(v) for k, v in comments.items()}
            comments.setdefault(key, []).append(entry)
            _write_json(COMMENTS_FILE, comments)
        self._json(200, {"comment": entry, "thread": comments[key]})

    def _delete_comment(self, comment_id, user):
        with overlay_lock:
            comments, _ = _read_json(COMMENTS_FILE, {})
            comments = {k: list(v) for k, v in comments.items()}
            for key, thread in comments.items():
                hit = next((c for c in thread if c["id"] == comment_id), None)
                if not hit:
                    continue
                self._comment_key(key, user)   # must still be able to see the row
                if user["role"] != "admin" and hit["user"].lower() != user["username"].lower():
                    raise UserError("You can only delete your own comments.")
                thread.remove(hit)
                if not thread:
                    del comments[key]
                _write_json(COMMENTS_FILE, comments)
                return self._json(200, {"ok": True, "key": key, "thread": thread})
        raise UserError("That comment no longer exists.")

    def _save_delivery_date(self, body, user):
        guid, field, value = str(body.get("guid", "")), body.get("field"), str(body.get("value") or "")
        if not guid or len(guid) > 100 or field not in ("estimated", "promised"):
            raise UserError("Unknown delivery date field.")
        if value and not DATE_RE.match(value):
            raise UserError("Dates must be YYYY-MM-DD.")
        allowed = allowed_projects(user)
        if allowed is not None and not (material_projects(guid) & allowed):
            raise UserError("That material isn't in one of your projects.")
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
