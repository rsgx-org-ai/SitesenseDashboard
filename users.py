"""User accounts, roles and login sessions for the hosted dashboard (server.py).

Accounts live in DATA_DIR/users.json (on the Railway Volume, never in git).
Passwords are stored only as salted PBKDF2-SHA256 hashes. Sessions are signed
cookies: no server-side session table to lose on restart, and each carries the
user's `session_version`, so resetting a password or deleting a user logs that
user out everywhere immediately.

Roles:
    admin    - sees every project, manages users, triggers refreshes, edits the
               shared Regions and delivery-date overlays
    standard - sees only the projects assigned to them; can also edit their
               projects' Estimated/Promised Delivery Dates
    viewer   - sees only the projects assigned to them, read-only
Every role can comment on rows it can see.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

ROLES = ("admin", "standard", "viewer")
ITERATIONS = 260_000
SESSION_DAYS = 7
MIN_PASSWORD = 10
LOCKOUT_FAILURES = 8
LOCKOUT_SECONDS = 10 * 60


class UserError(ValueError):
    """A request to change users that cannot be applied (shown to the admin)."""


def _hash(password, salt, iterations=ITERATIONS):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def check_password_strength(password):
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(set(password)) < 4:
        raise UserError("Password is too repetitive - use a mix of characters or a passphrase.")


class UserStore:
    def __init__(self, data_dir):
        self.path = data_dir / "users.json"
        self.secret_path = data_dir / ".session_secret"
        self._lock = threading.Lock()
        self._failures = {}   # username key -> (count, first_failure_time, locked_until)
        self.secret = self._load_secret()

    # ------------------------------------------------------------ persistence
    def _load_secret(self):
        env = os.environ.get("SESSION_SECRET")
        if env:
            return env.encode("utf-8")
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        value = secrets.token_bytes(32)
        self.secret_path.write_bytes(value)
        return value

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8")).get("users", {})

    def _write(self, users):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"users": users}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)   # atomic: a crash mid-write never leaves half a file

    @staticmethod
    def _key(username):
        return (username or "").strip().lower()

    def has_users(self):
        with self._lock:
            return bool(self._read())

    def bootstrap(self, username, password):
        """Create the first admin from DASHBOARD_USER/DASHBOARD_PASSWORD, once.

        Only runs while there are no users at all, so changing those Railway
        variables later does not override accounts managed in the dashboard.
        """
        with self._lock:
            if self._read() or not (username and password):
                return False
            salt = secrets.token_bytes(16)
            self._write({self._key(username): {
                "username": username.strip(), "role": "admin", "projects": [],
                "salt": salt.hex(), "hash": _hash(password, salt), "iterations": ITERATIONS,
                "session_version": 1, "created_at": int(time.time()),
            }})
            return True

    # ----------------------------------------------------------------- lookups
    def get(self, username):
        with self._lock:
            u = self._read().get(self._key(username))
        return dict(u) if u else None

    def public(self, u):
        return {"username": u["username"], "role": u["role"], "projects": u.get("projects", [])}

    def list_public(self):
        with self._lock:
            users = self._read()
        return sorted((self.public(u) for u in users.values()), key=lambda u: u["username"].lower())

    # ---------------------------------------------------------------- login
    def locked_for(self, username):
        """Seconds until this account accepts login attempts again, else 0."""
        count, first, until = self._failures.get(self._key(username), (0, 0, 0))
        return max(0, int(until - time.time()))

    def authenticate(self, username, password):
        key = self._key(username)
        if self.locked_for(key):
            return None
        u = self.get(key)
        ok = False
        if u:
            ok = hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"]), u["iterations"]), u["hash"])
        else:
            _hash(password, b"0" * 16)   # same cost either way, so timing does not reveal valid usernames
        if ok:
            self._failures.pop(key, None)
            return u
        count, first, until = self._failures.get(key, (0, time.time(), 0))
        if time.time() - first > LOCKOUT_SECONDS:
            count, first = 0, time.time()
        count += 1
        until = time.time() + LOCKOUT_SECONDS if count >= LOCKOUT_FAILURES else 0
        self._failures[key] = (count, first, until)
        return None

    # -------------------------------------------------------------- sessions
    def make_cookie_value(self, u):
        expires = int(time.time()) + SESSION_DAYS * 86400
        payload = f"{self._key(u['username'])}|{u['session_version']}|{expires}"
        sig = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")

    def user_from_cookie(self, value):
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            key, version, expires, sig = raw.rsplit("|", 3)
        except Exception:
            return None
        payload = f"{key}|{version}|{expires}"
        want = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want) or int(expires) < time.time():
            return None
        u = self.get(key)
        if not u or str(u["session_version"]) != version:
            return None
        return u

    # ------------------------------------------------------------- admin edits
    def _admin_count(self, users):
        return sum(1 for u in users.values() if u["role"] == "admin")

    def create(self, username, password, role, projects):
        username = (username or "").strip()
        key = self._key(username)
        if not key or any(c in key for c in "|:/\\ ") or len(key) > 64:
            raise UserError("Username must be 1-64 characters with no spaces or |, :, / or \\.")
        if role not in ROLES:
            raise UserError("Role must be admin, standard or viewer.")
        check_password_strength(password)
        with self._lock:
            users = self._read()
            if key in users:
                raise UserError(f"A user called {username} already exists.")
            salt = secrets.token_bytes(16)
            users[key] = {
                "username": username, "role": role,
                "projects": [] if role == "admin" else sorted({int(p) for p in projects}),
                "salt": salt.hex(), "hash": _hash(password, salt), "iterations": ITERATIONS,
                "session_version": 1, "created_at": int(time.time()),
            }
            self._write(users)
            return self.public(users[key])

    def update(self, username, role=None, projects=None, password=None):
        key = self._key(username)
        with self._lock:
            users = self._read()
            u = users.get(key)
            if not u:
                raise UserError(f"No user called {username}.")
            if role is not None:
                if role not in ROLES:
                    raise UserError("Role must be admin, standard or viewer.")
                if u["role"] == "admin" and role != "admin" and self._admin_count(users) == 1:
                    raise UserError("This is the only admin. Make someone else an admin first.")
                u["role"] = role
            if u["role"] == "admin":
                u["projects"] = []
            elif projects is not None:
                u["projects"] = sorted({int(p) for p in projects})
            if password is not None:
                check_password_strength(password)
                salt = secrets.token_bytes(16)
                u.update(salt=salt.hex(), hash=_hash(password, salt), iterations=ITERATIONS)
            if password is not None or role is not None:
                # Signs this user out of every browser; the acting admin gets a fresh cookie.
                u["session_version"] = u.get("session_version", 1) + 1
            self._write(users)
            return u

    def delete(self, username):
        key = self._key(username)
        with self._lock:
            users = self._read()
            u = users.get(key)
            if not u:
                raise UserError(f"No user called {username}.")
            if u["role"] == "admin" and self._admin_count(users) == 1:
                raise UserError("This is the only admin and cannot be removed.")
            del users[key]
            self._write(users)

    def change_own_password(self, username, current, new):
        u = self.get(username)
        if not u or not hmac.compare_digest(
                _hash(current, bytes.fromhex(u["salt"]), u["iterations"]), u["hash"]):
            raise UserError("Current password is incorrect.")
        return self.update(username, password=new)
