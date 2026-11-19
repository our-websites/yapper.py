#!/usr/bin/env python3
"""Telemetry server + web panel for yapper.py bots. Run it on your own server (it is not part of the pip package).

    pip install -r requirements.txt
    cp .env.example .env      # fill it in once
    python3 telemetry_server.py

One command, no subcommands: everything else (approving bots, setting owners, adding managers and approvers) is done in
the web panel, or by typing commands into the console while it runs (type `help`).

What it serves:
  GET  /          the panel when you are logged in, otherwise a landing page about the telemetry (HTML, nothing else)
  POST /          {"bot_id", "username", "key", "owner_id", "interval", "sdk"} -> {"token", "expires_in"} (the bot API)
  POST /report    Authorization: Bearer <token>, body = the report -> 204
  GET  /health    {"ok": true}
  /login, /auth/callback, /logout, POST /panel/*   the panel's login and actions (browsers only)

Settings are environment variables (a .env file next to this one is read too); see .env.example for all of them.
"""
import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import dotenv
except ImportError:   # python-dotenv is in requirements.txt; real environment variables work without it
    dotenv = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_auth as auth  # noqa: E402
import telemetry_panel as panel  # noqa: E402
from telemetry_store import CONSOLE, Store, can_approve, can_change_owner, can_revoke, valid_id  # noqa: E402

DEFAULT_SUPPORT_URL = "https://www.yapin.gg/invite/pythonlibrary"
MAX_BODY = 64 * 1024
MAX_FORM = 8 * 1024
TRUE = ("1", "true", "yes", "on")

NOT_APPROVED = ("Your bot ({bot_id}) is not on the telemetry approved list yet, so nothing is sent. Join the support "
                "server ({support}) and ask someone to add your bot. Please bring: (1) your bot's ID, and (2) proof that "
                "yapper.py is your main SDK, for example a link to your bot's source code that uses `import yapper`, or the "
                "output of `telemetry.preview(bot)`.{status}")
NEED_BOT_ID = ("Send your bot's ID as JSON, {{\"bot_id\": \"<your bot's ID>\"}}. You also need to be on the approved list: "
               "join the support server ({support}), ask someone to add your bot, and bring your bot's ID and proof that "
               "yapper.py is your main SDK.")
KEY_REQUIRED = ("Your bot ({bot_id}) is approved, but it didn't send its private key. Log in at {panel} with the Yapin' "
                "account that owns the bot, copy the key from your bot's page and set it as TELEMETRY_KEY in your bot's "
                "environment or .env file. If you are on an older yapper.py, update it (pip install -U yapper.py).")
KEY_INVALID = ("The TELEMETRY_KEY your bot ({bot_id}) sent is not right. Copy it again from {panel} (log in with the "
               "account that owns the bot) and make sure there are no spaces or quotes around it.")
REVOKED = "Telemetry access for your bot ({bot_id}) was removed. Ask in the support server ({support}) if that is a mistake."


class Config:
    def __init__(self, secret, db_path="telemetry.json", owners=(), data_dir=None, support_url=DEFAULT_SUPPORT_URL, panel_url="",
                 token_ttl=3600, trust_proxy=False, cookie_secure=None, dev_login=False, require_key=True,
                 oauth=None, host="0.0.0.0", port=8080):
        if not secret or len(secret) < 32:
            raise ValueError("TELEMETRY_SECRET must be at least 32 characters (try: python3 -c 'import secrets; print(secrets.token_hex(32))')")
        self.secret = secret.encode()
        self.store = Store(db_path, owners, data_dir)
        self.support_url = support_url
        self.panel_url = panel_url.rstrip("/")
        self.token_ttl = int(token_ttl)
        self.trust_proxy = trust_proxy
        self.cookie_secure = self.panel_url.startswith("https://") if cookie_secure is None else cookie_secure
        self.dev_login = dev_login
        self.require_key = require_key
        self.oauth = oauth or auth.OAuth()
        self.host, self.port = host, int(port)
        self.auth_limit = RateLimiter(30, 60)          # token requests per minute per address
        self.report_limit = RateLimiter(20, 3600)      # reports per hour per bot (they send every 30 minutes)
        self.web_limit = RateLimiter(60, 60)           # logins and panel actions per minute per address

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        g = lambda k, d="": (env.get(k) or d).strip()
        port = g("SERVER_PORT", "8080")
        if not port.isdigit() or not 0 <= int(port) <= 65535:
            raise ValueError(f"SERVER_PORT must be a port number, got {port!r}")
        db = g("TELEMETRY_DB", "telemetry.json")
        secret = g("TELEMETRY_SECRET")
        if not secret:   # first start with nothing set: make one and keep it, so one command is enough
            secret = _stored_secret(db + ".secret")
        panel_url = g("TELEMETRY_PANEL_URL").rstrip("/")
        secure = g("TELEMETRY_COOKIE_SECURE")
        return cls(secret, db, [o.strip() for o in g("TELEMETRY_OWNERS").split(",") if o.strip()], g("TELEMETRY_DATA_DIR") or os.path.join(os.path.dirname(db), "telemetry_data"),
                   g("TELEMETRY_SUPPORT_URL", DEFAULT_SUPPORT_URL), panel_url, g("TELEMETRY_TOKEN_TTL", "3600"),
                   g("TELEMETRY_TRUST_PROXY").lower() in TRUE, None if not secure else secure.lower() in TRUE,
                   g("TELEMETRY_DEV_LOGIN").lower() in TRUE, g("TELEMETRY_REQUIRE_KEY", "1").lower() in TRUE,
                   auth.OAuth.from_env(env, panel_url + "/auth/callback" if panel_url else ""), g("SERVER_HOST", "0.0.0.0"), int(port))


def _stored_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    print(f"TELEMETRY_SECRET was not set, so one was made and saved in {path}. Set TELEMETRY_SECRET to use your own.")
    return value


# ---- bot tokens ----

def make_token(secret, bot_id, ttl, now=None):
    return auth.sign(secret, "bot", {"b": str(bot_id)}, ttl, now)


def read_token(secret, token, now=None):
    """The bot ID a valid, unexpired token was made for, else None."""
    data = auth.unsign(secret, "bot", token or "", now)
    return str(data["b"]) if data and "b" in data else None


class RateLimiter:
    def __init__(self, limit, seconds):
        self.limit, self.seconds, self._hits, self._lock = limit, seconds, {}, threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.seconds]
            ok = len(hits) < self.limit
            if ok:
                hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10000:   # forget old keys so memory stays bounded
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.seconds}
            return ok


# ---- http ----

class Handler(BaseHTTPRequestHandler):
    server_version = "yapper-telemetry"
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self):
        return self.server.cfg

    @property
    def store(self):
        return self.server.cfg.store

    def log_request(self, code="-", size="-"):   # method, path and status only: no addresses
        sys.stderr.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {self.command} {urlparse(self.path).path} {code}\n")

    def log_message(self, fmt, *args):
        pass

    def client(self):
        if self.cfg.trust_proxy and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def _send(self, status, data=b"", ctype=None, headers=None):
        self.send_response(status)
        if ctype and data:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def reply(self, status, body=None, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self._send(status, data, "application/json", list((headers or {}).items()) + [("Cache-Control", "no-store")])

    def html(self, status, make, extra=None):
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               f"form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self._send(status, make(nonce), "text/html; charset=utf-8",
                   [("Content-Security-Policy", csp), ("Cache-Control", "no-store"), ("Referrer-Policy", "same-origin"),
                    ("X-Frame-Options", "DENY")] + (extra or []))

    def redirect(self, location, cookies=()):
        self._send(303, b"", None, [("Location", location), ("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in cookies])

    def read(self, limit):
        """The raw request body, or None after answering 413."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.reply(413, {"error": "too_large", "message": f"Requests are at most {limit} bytes."})
            return None
        return self.rfile.read(length) if length else b""

    def json_body(self):
        """The JSON object in the request body ({} when there is none), or None after answering with an error."""
        raw = self.read(MAX_BODY)
        if raw is None:
            return None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.reply(400, {"error": "bad_json", "message": "The body must be a JSON object."})
            return None
        return data

    def session(self):
        return auth.read_session(self.cfg.secret, auth.parse_cookies(self.headers.get("Cookie")).get(auth.SESSION_COOKIE))

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("X-Forwarded-Host") if self.cfg.trust_proxy else None) or self.headers.get("Host") or ""
        allowed = {host, urlparse(self.cfg.panel_url).netloc} - {""}
        return urlparse(origin).netloc in allowed

    def login_url(self):
        return "/login" if (self.cfg.oauth.configured or self.cfg.dev_login) else None

    # ---- routing ----

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/health":
            return self.reply(200, {"ok": True})
        if url.path == "/":
            return self.page(query)
        if url.path == "/login":
            return self.login()
        if url.path == "/auth/callback":
            return self.callback(query)
        if url.path == "/dev-login":
            return self.dev_login(query)
        self.reply(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/", "/report"):
            data = self.json_body()
            if data is None:
                return
            return self.handle_token(data) if path == "/" else self.handle_report(data)
        if path == "/logout" or path.startswith("/panel/"):
            return self.panel_post(path)
        self.reply(404, {"error": "not_found"})

    # ---- the pages: only GET / ever shows HTML ----

    def page(self, query):
        session = self.session()
        message = query.get("msg") if query.get("msg") in panel.MESSAGES else None
        cfg = self.cfg
        if not session:
            return self.html(200, lambda n: panel.landing(n, cfg.support_url, self.login_url(), message))
        role = self.store.role_of(session.user_id)
        self.html(200, lambda n: panel.dashboard(self.store, session, role, query.get("tab"), message, n, cfg.support_url))

    def login(self):
        cfg = self.cfg
        if not cfg.web_limit.allow(self.client()):
            return self.redirect("/?msg=login_failed")
        if not cfg.oauth.configured:
            return self.redirect("/?msg=login_off")
        nonce = secrets.token_urlsafe(24)
        state = auth.sign(cfg.secret, "state", {"n": nonce}, auth.STATE_TTL)
        self.redirect(cfg.oauth.login_url(state), [auth.cookie(auth.STATE_COOKIE, nonce, auth.STATE_TTL, cfg.cookie_secure)])

    def callback(self, query):
        cfg = self.cfg
        if not cfg.web_limit.allow(self.client()):
            return self.redirect("/?msg=login_failed")
        clear = auth.cookie(auth.STATE_COOKIE, "", 0, cfg.cookie_secure)
        data = auth.unsign(cfg.secret, "state", query.get("state", ""))
        mine = auth.parse_cookies(self.headers.get("Cookie")).get(auth.STATE_COOKIE, "")
        if not data or not mine or not hmac.compare_digest(str(data.get("n", "")), mine) or not query.get("code"):
            return self.redirect("/?msg=login_failed", [clear])
        try:
            user_id, name = cfg.oauth.identify(query["code"])
        except auth.LoginError:
            return self.redirect("/?msg=login_failed", [clear])
        self.start_session(user_id, name, [clear])

    def start_session(self, user_id, name, cookies=()):
        value = auth.make_session(self.cfg.secret, user_id, name)
        self.redirect("/", list(cookies) + [auth.cookie(auth.SESSION_COOKIE, value, auth.SESSION_TTL, self.cfg.cookie_secure)])

    def dev_login(self, query):
        """Local testing only: TELEMETRY_DEV_LOGIN=1 and the request really comes from this machine."""
        try:
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            local = False
        if not (self.cfg.dev_login and local) or self.headers.get("X-Forwarded-For") or not valid_id(query.get("user", "")):
            return self.reply(404, {"error": "not_found"})
        self.start_session(query["user"], query.get("name", "Dev"))

    # ---- panel actions ----

    def panel_post(self, path):
        cfg = self.cfg
        session = self.session()
        if not cfg.web_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down"}, {"Retry-After": "60"})
        if not self.same_origin():
            return self.reply(403, {"error": "bad_origin"})
        if path == "/panel/key":
            return self.reveal_key(session)
        raw = self.read(MAX_FORM)
        if raw is None:
            return
        form = {k: v[0].strip() for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if not session:
            return self.redirect("/")
        if not auth.csrf_ok(session, form.get("csrf")):
            return self.redirect("/?msg=csrf")
        if path == "/logout":
            return self.redirect("/", [auth.cookie(auth.SESSION_COOKIE, "", 0, cfg.cookie_secure)])
        store, me = self.store, session.user_id
        role = store.role_of(me)
        tab, ok = "approvals", None
        try:
            if path == "/panel/apply":
                tab = "bots"
                store.apply(me, form.get("bot_id", ""))
                ok = "applied"
            elif path == "/panel/rotate-key":
                tab = "bots"
                store.rotate_key(form.get("bot_id", ""), me)
                ok = "rotated"
            elif path == "/panel/approve":
                if not can_approve(role):
                    raise PermissionError
                store.approve(form.get("bot_id", ""), form.get("owner_id", ""), me)
                ok = "approved"
            elif path == "/panel/revoke":
                if not can_revoke(role):
                    raise PermissionError
                store.revoke(form.get("bot_id", ""))
                ok = "revoked"
            elif path == "/panel/owner":
                if not can_change_owner(role):
                    raise PermissionError
                store.set_owner(form.get("bot_id", ""), form.get("owner_id", ""), form.get("new_key") == "1")
                ok = "owner"
            elif path in ("/panel/share", "/panel/unshare"):
                tab = "bots"
                if path == "/panel/share":
                    store.share(me, form.get("bot_id", ""), form.get("user_id", ""), form.get("level", ""))
                    ok = "shared"
                else:
                    store.unshare(me, form.get("bot_id", ""), form.get("user_id", ""))
                    ok = "unshared"
            elif path == "/panel/role":
                tab = "team"
                store.set_role(me, form.get("user_id", ""), form.get("role", ""))
                ok = "role_added"
            elif path == "/panel/remove-role":
                tab = "team"
                store.remove_role(me, form.get("user_id", ""))
                ok = "role_removed"
            else:
                return self.reply(404, {"error": "not_found"})
        except PermissionError:
            return self.redirect(f"/?tab={tab}&msg=denied")
        except ValueError:
            return self.redirect(f"/?tab={tab}&msg=invalid")
        self.redirect(f"/?tab={tab}&msg={ok}")

    def reveal_key(self, session):
        """A bot's private key, only to its owner (or a manager they added), only when they ask, never cached."""
        data = self.json_body()
        if data is None:
            return
        if not session or not auth.csrf_ok(session, self.headers.get("X-CSRF-Token")):
            return self.reply(403, {"error": "forbidden"})
        bot = self.store.get_bot(str(data.get("bot_id", "")))
        if not bot or not bot["key"] or self.store.access_level(bot["bot_id"], session.user_id) not in ("owner", "manager"):
            return self.reply(404, {"error": "not_found"})
        self.reply(200, {"key": bot["key"]})

    # ---- the bot API ----

    def handle_token(self, data):
        cfg = self.cfg
        if not cfg.auth_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down", "message": "Too many requests, try again in a minute."}, {"Retry-After": "60"})
        bot_id = str(data.get("bot_id") or "").strip()
        if not valid_id(bot_id):
            return self.reply(400, {"error": "bot_id_required", "support_server": cfg.support_url,
                                    "message": NEED_BOT_ID.format(support=cfg.support_url)})
        username = data.get("username") if isinstance(data.get("username"), str) else None
        bot = cfg.store.see_bot(bot_id, username, data.get("owner_id"), data.get("sdk"), data.get("interval"))
        panel_link = cfg.panel_url or "the telemetry server's address in your browser"
        if bot["status"] == "pending":
            status = f" You can follow its status at {cfg.panel_url} by logging in with Yapin'." if cfg.panel_url and bot.get("claimed_owner_id") else ""
            return self.reply(403, {"error": "not_approved", "status": "pending", "bot_id": bot_id, "support_server": cfg.support_url,
                                    "needed": ["your bot's ID", "proof that yapper.py is your main SDK"],
                                    "message": NOT_APPROVED.format(bot_id=bot_id, support=cfg.support_url, status=status)})
        if bot["status"] == "revoked":
            return self.reply(403, {"error": "revoked", "status": "revoked", "support_server": cfg.support_url,
                                    "message": REVOKED.format(bot_id=bot_id, support=cfg.support_url)})
        key = data.get("key")
        if key:
            if not cfg.store.check_key(bot_id, key):
                return self.reply(401, {"error": "key_invalid", "message": KEY_INVALID.format(bot_id=bot_id, panel=panel_link)})
        elif cfg.require_key:
            return self.reply(401, {"error": "key_required", "status": "approved",
                                    "message": KEY_REQUIRED.format(bot_id=bot_id, panel=panel_link)})
        self.reply(200, {"token": make_token(cfg.secret, bot_id, cfg.token_ttl), "expires_in": cfg.token_ttl})

    def handle_report(self, report):
        cfg = self.cfg
        auth_header = self.headers.get("Authorization") or ""
        bot_id = read_token(cfg.secret, auth_header[7:].strip()) if auth_header.lower().startswith("bearer ") else None
        if not bot_id:
            return self.reply(401, {"error": "bad_token", "message": "Ask for a new token with POST /."}, {"WWW-Authenticate": "Bearer"})
        bot = cfg.store.get_bot(bot_id)
        if not bot or bot["status"] != "approved":   # approval withdrawn since the token was made
            return self.reply(403, {"error": "not_approved", "message": NOT_APPROVED.format(bot_id=bot_id, support=cfg.support_url, status="")})
        info = report.get("bot")
        if not isinstance(report.get("schema"), int) or not isinstance(info, dict) or str(info.get("id")) != bot_id:
            return self.reply(403, {"error": "token_mismatch", "message": "The token is for a different bot than the report."})
        if not cfg.report_limit.allow(bot_id):
            return self.reply(429, {"error": "slow_down", "message": "Reports are every 30 minutes."}, {"Retry-After": "600"})
        cfg.store.record_report(bot_id, report)
        self.reply(204)


def make_server(cfg, host=None, port=None):
    server = ThreadingHTTPServer((cfg.host if host is None else host, cfg.port if port is None else port), Handler)
    server.cfg = cfg
    server.daemon_threads = True
    return server


# ---- the console: type commands into the running server (works in panels like Pterodactyl) ----

HELP = """commands:
  status                      how many bots are pending / approved
  list [pending|approved|removed]   the bots
  approve <bot_id> <owner_id>       approve a bot and set its owner
  revoke <bot_id>             remove a bot's access
  owner <bot_id> <user_id>    change a bot's owner
  team                        everyone with a role
  add <user_id> <manager|approver>  give someone a role
  remove <user_id>            take someone's role away
  stop                        shut the server down
(the panel does all of this too, in your browser)"""


def run_command(store, line):
    """Run one console command and return what to print. The console can do everything an owner can."""
    parts = line.split()
    if not parts:
        return ""
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd in ("help", "?"):
            return HELP
        if cmd == "status":
            c = store.counts()
            return f"{c.get('pending', 0)} pending, {c.get('approved', 0)} approved, {c.get('revoked', 0)} removed"
        if cmd == "list":
            status = {"removed": "revoked"}.get(args[0], args[0]) if args else "approved"
            bots = store.with_status(status)
            return "\n".join(f"{b['bot_id']}  {b['name'] or '-'}  owner={b['owner_id'] or b['claimed_owner_id'] or '-'}" for b in bots) or f"no {status} bots"
        if cmd == "approve" and len(args) == 2:
            bot = store.approve(args[0], args[1], CONSOLE)
            return f"approved {bot['bot_id']} (owner {bot['owner_id']}); the owner can copy its key in the panel"
        if cmd == "revoke" and len(args) == 1:
            store.revoke(args[0])
            return f"removed {args[0]}"
        if cmd == "owner" and len(args) == 2:
            store.set_owner(args[0], args[1])
            return f"owner of {args[0]} is now {args[1]}"
        if cmd == "team":
            return "\n".join(f"{m['user_id']}  {m['role']}" for m in store.staff()) or "nobody yet"
        if cmd == "add" and len(args) == 2:
            store.set_role(CONSOLE, args[0], args[1].lower())
            return f"{args[0]} is now {args[1].lower()}"
        if cmd == "remove" and len(args) == 1:
            store.remove_role(CONSOLE, args[0])
            return f"removed {args[0]}'s role"
    except (ValueError, PermissionError) as e:
        return f"error: {e}"
    return "unknown command, type help"


def console(server):
    """Read commands from stdin until it closes. Quietly does nothing when there is no stdin."""
    try:
        for line in sys.stdin:
            line = line.strip()
            if line.lower() in ("stop", "exit", "quit"):
                print("stopping")
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            out = run_command(server.cfg.store, line)
            if out:
                print(out, flush=True)
    except (OSError, ValueError):
        pass


# ---- start ----

def startup_notes(cfg):
    notes = []
    if not cfg.store.owners:
        notes.append("TELEMETRY_OWNERS is empty: nobody can use the panel's team and approval tabs. Set it to your Yapin' user ID(s), comma separated.")
    if not cfg.oauth.configured and not cfg.dev_login:
        notes.append("Login is not set up (missing: " + ", ".join(cfg.oauth.missing()) + "). Only the landing page and the bot API work until it is.")
    if not cfg.cookie_secure:
        notes.append("Cookies are not marked Secure. Set TELEMETRY_PANEL_URL to your https:// address before going live.")
    if cfg.dev_login:
        notes.append("TELEMETRY_DEV_LOGIN is on: anyone on this machine can log in as any user. Turn it off for real use.")
    if not cfg.require_key:
        notes.append("TELEMETRY_REQUIRE_KEY is off: older bots without a private key can still send reports.")
    return notes


def main(argv=None):
    argparse.ArgumentParser(description="yapper.py telemetry server and panel. All settings come from the environment (.env).").parse_args(argv)
    if dotenv:
        dotenv.load_dotenv()
    try:
        cfg = Config.from_env()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    imported = cfg.store.import_legacy(os.environ.get("TELEMETRY_APPROVED", "approved_bots.json"), os.environ.get("TELEMETRY_DATA", "telemetry_reports.jsonl"))
    if imported:
        print(f"imported {imported} bots from the old approved list: set their owners in the panel")
    try:
        server = make_server(cfg)
    except OSError as e:
        print(f"error: can't listen on {cfg.host}:{cfg.port} ({e}). Change SERVER_PORT.", file=sys.stderr)
        return 2
    print(f"telemetry server and panel listening on {cfg.host}:{server.server_address[1]}" + (f" ({cfg.panel_url})" if cfg.panel_url else ""))
    for note in startup_notes(cfg):
        print("note:", note)
    print("type `help` for console commands")
    threading.Thread(target=console, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
