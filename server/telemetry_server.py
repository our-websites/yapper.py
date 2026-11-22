#!/usr/bin/env python3
"""Telemetry server + web panel for yapper.py bots. Run it on your own server (it is not part of the pip package).

    pip install -r requirements.txt
    cp .env.example .env      # fill it in once
    python3 telemetry_server.py

One command, no subcommands: everything else (approving bots, setting owners, adding managers and approvers) is done in
the web panel, or by typing commands into the console while it runs (type `help`).

What it serves:
  GET  /          the landing page, always (it has a Panel button when you are logged in, Log in otherwise)
  GET  /panel/<tab>  the panel (approvals, bots, people, stats, announcements, activity), /settings your timezone and API keys
  GET  /api/bots  /api/bots/<id>  /api/bots/<id>/history   read-only JSON with `Authorization: Bearer uk_...` (an API key from /settings)
  POST /          {"bot_id", "username", "key", "owner_id", "interval", "sdk"} -> {"token", "expires_in"} (the bot API)
  POST /report    Authorization: Bearer <token>, body = the report -> 204
  GET  /health    {"ok": true}
  /login, /auth/callback, /logout, POST /panel/*   the panel's login and actions (browsers only)

Settings are environment variables (a .env file next to this one is read too); see .env.example for all of them.
"""
import base64
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
import telemetry_badge as badge  # noqa: E402
import telemetry_panel as panel  # noqa: E402
from telemetry_store import CONSOLE, ReasonRequired, Store, UserLookupError, can_approve, can_change_owner, can_revoke, valid_id  # noqa: E402

DEFAULT_SUPPORT_URL = "https://www.yapin.gg/invite/pythonlibrary"
MAX_BODY = 64 * 1024
DEFAULT_MAX_REPORT = 2 * 1024 ** 3     # TELEMETRY_MAX_REPORT_BYTES: only bots with a valid token can send this much
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
DENIED = ("Telemetry for your bot ({bot_id}) was denied. Reason: {reason} If you have fixed that, ask in the support server ({support}) "
          "and mention your bot's ID.")
REVOKED = "Telemetry access for your bot ({bot_id}) was removed. Ask in the support server ({support}) if that is a mistake."


class Config:
    def __init__(self, secret, db_path="telemetry.json", owners=(), data_dir=None, support_url=DEFAULT_SUPPORT_URL, panel_url="",
                 token_ttl=3600, trust_proxy=False, cookie_secure=None, dev_login=False, require_key=True,
                 oauth=None, host="0.0.0.0", port=8080, max_report_bytes=DEFAULT_MAX_REPORT, user_id_pattern=r"^\d+$"):
        if not secret or len(secret) < 32:
            raise ValueError("TELEMETRY_SECRET must be at least 32 characters (try: python3 -c 'import secrets; print(secrets.token_hex(32))')")
        self.secret = secret.encode()
        self.store = Store(db_path, owners, data_dir, user_id_pattern)
        self.support_url = support_url
        self.panel_url = panel_url.rstrip("/")
        self.token_ttl = int(token_ttl)
        self.trust_proxy = trust_proxy
        self.cookie_secure = self.panel_url.startswith("https://") if cookie_secure is None else cookie_secure
        self.dev_login = dev_login
        self.require_key = require_key
        self.oauth = oauth or auth.OAuth()
        self.host, self.port = host, int(port)
        self.max_report_bytes = int(max_report_bytes)
        self.extra_image_hosts = set()
        self.fetch_image = badge.fetch_image            # (url, allowed hosts) -> data: URI or None; replaced in tests
        self.badge_limit = RateLimiter(240, 60)         # badges per minute per address
        self.api_limit = RateLimiter(120, 60)          # API reads per minute per address
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
        if not g("TELEMETRY_MAX_REPORT_BYTES", "1").isdigit():
            raise ValueError("TELEMETRY_MAX_REPORT_BYTES must be a number of bytes")
        return cls(secret, db, [o.strip() for o in g("TELEMETRY_OWNERS").split(",") if o.strip()], g("TELEMETRY_DATA_DIR") or os.path.join(os.path.dirname(db), "telemetry_data"),
                   g("TELEMETRY_SUPPORT_URL", DEFAULT_SUPPORT_URL), panel_url, g("TELEMETRY_TOKEN_TTL", "3600"),
                   g("TELEMETRY_TRUST_PROXY").lower() in TRUE, None if not secure else secure.lower() in TRUE,
                   g("TELEMETRY_DEV_LOGIN").lower() in TRUE, g("TELEMETRY_REQUIRE_KEY", "1").lower() in TRUE,
                   auth.OAuth.from_env(env, panel_url + "/auth/callback" if panel_url else ""), g("SERVER_HOST", "0.0.0.0"), int(port),
                   int(g("TELEMETRY_MAX_REPORT_BYTES", str(DEFAULT_MAX_REPORT))), g("TELEMETRY_USER_ID_PATTERN", r"^\d+$"))


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
        csp = (f"default-src 'none'; img-src 'self' data: {self.image_sources()}; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               f"form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self._send(status, make(nonce), "text/html; charset=utf-8",
                   [("Content-Security-Policy", csp), ("Cache-Control", "no-store"), ("Referrer-Policy", "same-origin"),
                    ("X-Frame-Options", "DENY")] + (extra or []))

    def image_sources(self):
        return " ".join("https://" + h for h in sorted(self.cfg.oauth.image_hosts() | self.cfg.extra_image_hosts))

    def redirect(self, location, cookies=()):
        self._send(303, b"", None, [("Location", location), ("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in cookies])

    def read(self, limit):
        """The raw request body, or None after answering 413."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.reply(413, {"error": "too_large", "message": f"Requests are at most {limit} bytes."})
            return None
        return self.rfile.read(length) if length else b""

    def json_body(self, limit=MAX_BODY):
        """The JSON object in the request body ({} when there is none), or None after answering with an error."""
        raw = self.read(limit)
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

    def use_context(self, session):
        """Tell the page helpers who is looking: their timezone, whether they are staff, which picture hosts to trust."""
        staff = bool(session and self.store.role_of(session.user_id))
        tz = self.store.timezone_of(session.user_id) if session else "auto"
        panel.set_context(panel.Context(self.store, tz, self.cfg.oauth.image_hosts() | self.cfg.extra_image_hosts, staff,
                                         self.cfg.panel_url or "http://" + self.headers.get("Host", "localhost")))

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("X-Forwarded-Host") if self.cfg.trust_proxy else None) or self.headers.get("Host") or ""
        allowed = {host, urlparse(self.cfg.panel_url).netloc} - {""}
        return urlparse(origin).netloc in allowed

    def redirect_uri(self):
        """Where Yapin' sends people back: TELEMETRY_PANEL_URL/auth/callback, or worked out from this request."""
        if self.cfg.oauth.redirect_uri:
            return self.cfg.oauth.redirect_uri
        host = (self.headers.get("X-Forwarded-Host") if self.cfg.trust_proxy else None) or self.headers.get("Host") or ""
        proto = "https" if self.cfg.cookie_secure else "http"
        return f"{proto}://{host}/auth/callback"

    def login_url(self):
        return "/login" if (self.cfg.oauth.configured or self.cfg.dev_login) else None

    # ---- routing ----

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        if path == "/health":
            return self.reply(200, {"ok": True})
        if path == "/":
            return self.landing_page(query)
        if path.startswith("/avatar/") and path.endswith(".png"):
            return self.bot_avatar(path[len("/avatar/"):-len(".png")])
        if path.startswith("/badge/") and path.endswith(".svg"):
            return self.badge(path[len("/badge/"):-len(".svg")])
        if path == "/panel/search":
            return self.search(query)
        if path.startswith("/bots/"):
            return self.bot_page(path)
        if path in ("/settings", "/panel") or path.startswith("/panel/"):
            return self.panel_page(path, query)
        if path.startswith("/api/"):
            return self.api(path)
        if path == "/login":
            return self.login()
        if path == "/auth/callback":
            return self.callback(query)
        if path == "/dev-login":
            return self.dev_login(query)
        self.reply(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/":
            data = self.json_body()
            return None if data is None else self.handle_token(data)
        if path == "/report":
            return self.handle_report()
        if path == "/logout" or path.startswith("/panel/"):
            return self.panel_post(path)
        self.reply(404, {"error": "not_found"})

    # ---- the pages ----

    def landing_page(self, query):
        """The landing page always shows, logged in or not. Logged in people get a Panel button."""
        session = self.session()
        message = query.get("msg") if query.get("msg") in panel.MESSAGES else None
        self.use_context(session)
        self.html(200, lambda n: panel.landing(n, self.cfg.support_url, self.login_url(), message, session, session.csrf if session else ""))

    def panel_page(self, path, query):
        session = self.session()
        if not session:
            return self.redirect("/?msg=login_needed")
        store, me, cfg = self.store, session.user_id, self.cfg
        self.use_context(session)
        message = query.get("msg") if query.get("msg") in panel.MESSAGES else None
        unread = store.unread_for(me)
        banner = panel.banners(unread, session.csrf, "/settings" if path == "/settings" else "/panel")
        if path == "/settings":
            new_key = None
            return self.html(200, lambda n: panel.settings_page(store, session, n, message, new_key, banner))
        role = store.role_of(me)
        shown = {}
        slug = path[len("/panel/"):] if path.startswith("/panel/") else ""
        if slug and slug not in panel.TAB_LABELS and role and valid_id(slug) and store.get_bot(slug):
            bot = store.get_bot(slug)
            store.audit_view(me, "bot.view", "manager", slug)
            return self.html(200, lambda n: panel.page(f"{panel.bot_title(bot)} | yapper.py telemetry", panel.staff_bot_page(bot, role, session.csrf, time.time(), store), n,
                                                       session, session.csrf, banners=banner))

        def make(n):
            body, tab = panel.dashboard(store, session, role, path[len("/panel/"):] if path.startswith("/panel/") else None, message, n, cfg.support_url, query=query.get("q", ""), banner_html=banner)
            shown["tab"] = tab
            return body
        self.html(200, make)
        if role:   # managers and owners can see which staff opened which tab; approvers' views are only for them
            store.audit_view(me, "page.view", "approver" if shown["tab"] == "approvals" else "manager", detail=shown["tab"])

    def svg(self, data):
        self._send(200, data, "image/svg+xml", [("Cache-Control", "public, max-age=300"),
                                                 ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:"),
                                                 ("Access-Control-Allow-Origin", "*")])

    def bot_avatar(self, bot_id):
        """A bot's picture, fetched and shrunk by this server, so pages never load images from hosts we don't control."""
        cfg = self.cfg
        if not cfg.badge_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down"}, {"Retry-After": "60"})
        bot = cfg.store.get_bot(bot_id) if valid_id(bot_id) else None
        uri = cfg.fetch_image(bot["avatar"], cfg.oauth.image_hosts() | cfg.extra_image_hosts, public=True) if bot and bot.get("avatar") else None
        if not uri or not uri.startswith("data:image/"):
            return self.reply(404, {"error": "not_found"})
        head, _, b64 = uri.partition(",")
        self._send(200, base64.b64decode(b64), head[5:].split(";")[0], [("Cache-Control", "public, max-age=3600"), ("Content-Security-Policy", "default-src 'none'"),
                                                                     ("Cross-Origin-Resource-Policy", "cross-origin")])

    def badge(self, name):
        """Public live badges for READMEs (see telemetry_badge.py). A bot's badge exists only once it is approved and its owner saved an invite link."""
        cfg = self.cfg
        if not cfg.badge_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down"}, {"Retry-After": "60"})
        if name == "stats":
            bots = cfg.store.with_status("approved")
            counts = [(b.get("servers") or 0) for b in bots if b.get("last_report_at")]
            return self.svg(badge.stats_badge(len(counts), sum(counts)))
        if name == "featured":   # every approved bot, so the README's list fills itself in
            from concurrent.futures import ThreadPoolExecutor
            hosts = cfg.oauth.image_hosts() | cfg.extra_image_hosts
            bots = [b for b in cfg.store.with_status("approved")][:100]
            with ThreadPoolExecutor(max_workers=8) as pool:
                icons = list(pool.map(lambda b: cfg.fetch_image(b.get("avatar"), hosts, public=True) if b.get("avatar") else None, bots))
            return self.svg(badge.featured_badge([(b["name"] or b["bot_id"], b.get("servers"), b.get("members"), b.get("description"), i) for b, i in zip(bots, icons)]))
        bot = cfg.store.get_bot(name) if valid_id(name) else None
        if not bot or bot["status"] != "approved" or not bot.get("invite_url"):
            return self.reply(404, {"error": "not_found"})
        report = bot.get("last_report") or {}
        servers = (report.get("servers") or {}).get("count")
        icon = cfg.fetch_image(bot.get("avatar"), cfg.oauth.image_hosts() | cfg.extra_image_hosts, public=True) if bot.get("avatar") else None
        self.svg(badge.bot_badge(bot["name"] or bot["bot_id"], servers, icon))

    def bot_page(self, path):
        """/bots/<id>: a bot's data for its owner, managers and viewers. /bots/<id>/live is the part that refreshes itself."""
        session = self.session()
        if not session:
            return self.redirect("/?msg=login_needed")
        parts = path.strip("/").split("/")[1:]
        if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] != "live") or not valid_id(parts[0]):
            return self.reply(404, {"error": "not_found"})
        store, me = self.store, session.user_id
        bot_id, live = parts[0], len(parts) == 2
        level = store.access_level(bot_id, me)
        bot = store.get_bot(bot_id) if level else None
        if not bot:
            if live:
                return self.reply(404, {"error": "not_found"})
            if store.role_of(me) and store.get_bot(bot_id):       # the team doesn't get bots' data: they get the staff view
                return self.redirect(f"/panel/{bot_id}")
            return self.redirect("/panel/bots?msg=denied")
        self.use_context(session)
        now = time.time()
        if live:
            return self.html(200, lambda n: panel.bot_live(bot, now, level, link=False, poll=True).encode())
        banner = panel.banners(store.unread_for(me), session.csrf, "/panel")
        members = store.members(bot_id)
        self.html(200, lambda n: panel.page(f"{panel.bot_title(bot)} | yapper.py telemetry", f"{panel.bot_page(bot, session.csrf, now, level, members)}", n, session, session.csrf, banners=banner))

    def search(self, query):
        session = self.session()
        if not session or not self.cfg.web_limit.allow(self.client()):
            return self.reply(403, {"error": "forbidden"})
        q = query.get("q", "")
        staff = bool(self.store.role_of(session.user_id))
        users = self.store.search_users(q, limit=8 if staff else 3)
        if not staff:   # bot owners picking someone to share with only get exact matches
            users = [u for u in users if q.strip().lstrip("@").casefold() in (u["user_id"].casefold(), (u.get("username") or "").casefold())]
        self.reply(200, [{"user_id": u["user_id"], "username": u.get("display") or u.get("username"), "avatar": u.get("avatar")} for u in users])

    def api(self, path):
        """Read-only JSON for people's own bots, with an API key from the settings page."""
        cfg = self.cfg
        if not cfg.api_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down"}, {"Retry-After": "60"})
        header = self.headers.get("Authorization") or ""
        found = self.store.user_for_api_key(header[7:].strip()) if header.lower().startswith("bearer ") else None
        if not found:
            return self.reply(401, {"error": "bad_key", "message": "Send Authorization: Bearer <your API key>. Make one in the panel's settings."}, {"WWW-Authenticate": "Bearer"})
        user_id = found[0]
        parts = path.strip("/").split("/")[1:]       # after "api"
        bots = {b["bot_id"]: b for b in self.store.api_bots(user_id)}
        if parts == ["bots"]:
            return self.reply(200, {"bots": [{"bot_id": b["bot_id"], "name": b["name"], "owner_id": b["owner_id"], "last_report_at": b["last_report_at"], "invite_url": b.get("invite_url"), "support_url": b.get("support_url"), "description": b.get("description"),
                                              "sdk_version": b["sdk_version"], "package_version": b["package_version"]} for b in bots.values()]})
        bot = bots.get(parts[1]) if len(parts) >= 2 and parts[0] == "bots" else None
        if not bot or len(parts) > 3:
            return self.reply(404, {"error": "not_found", "message": "No bot with that ID that your key can read."})
        self.store.audit_view(user_id, "api.read", "manager", bot["bot_id"], found[1], every=3600)
        if len(parts) == 2:
            return self.reply(200, {"bot_id": bot["bot_id"], "name": bot["name"], "owner_id": bot["owner_id"], "avatar": bot.get("avatar"),
                                    "last_report_at": bot["last_report_at"], "report": bot["last_report"]})
        if parts[2] == "history":
            return self.reply(200, {"bot_id": bot["bot_id"], "columns": ["time", "servers", "members", "online", "commands"], "samples": bot.get("samples") or []})
        self.reply(404, {"error": "not_found"})

    def login(self):
        cfg = self.cfg
        if not cfg.web_limit.allow(self.client()):
            return self.redirect("/?msg=login_failed")
        if not cfg.oauth.configured:
            return self.redirect("/?msg=login_off")
        nonce = secrets.token_urlsafe(24)
        state = auth.sign(cfg.secret, "state", {"n": nonce}, auth.STATE_TTL)
        self.redirect(cfg.oauth.login_url(state, self.redirect_uri()), [auth.cookie(auth.STATE_COOKIE, nonce, auth.STATE_TTL, cfg.cookie_secure)])

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
            user_id, name, display, picture = cfg.oauth.identify(query["code"], self.redirect_uri())
        except auth.LoginError:
            return self.redirect("/?msg=login_failed", [clear])
        self.store.record_login(user_id, name, picture, display=display)
        self.start_session(user_id, name, [clear])

    def start_session(self, user_id, name, cookies=()):
        value = auth.make_session(self.cfg.secret, user_id, name)
        self.redirect("/panel", list(cookies) + [auth.cookie(auth.SESSION_COOKIE, value, auth.SESSION_TTL, self.cfg.cookie_secure)])

    def dev_login(self, query):
        """Local testing only: TELEMETRY_DEV_LOGIN=1 and the request really comes from this machine."""
        try:
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            local = False
        if not (self.cfg.dev_login and local) or self.headers.get("X-Forwarded-For") or not valid_id(query.get("user", "")):
            return self.reply(404, {"error": "not_found"})
        self.store.record_login(query["user"], query.get("name", "Dev"), None)
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
            return self.redirect("/?msg=login_needed")
        if not auth.csrf_ok(session, form.get("csrf")):
            return self.redirect("/panel?msg=csrf")
        if path == "/logout":
            return self.redirect("/", [auth.cookie(auth.SESSION_COOKIE, "", 0, cfg.cookie_secure)])
        store, me = self.store, session.user_id
        role = store.role_of(me)
        tab, ok = "approvals", None
        who = lambda field: store.resolve_user(form.get(field, ""))      # IDs are what is saved, even when a username was typed
        bot_id = form.get("bot_id", "")
        try:
            if path == "/panel/apply":
                tab = "bots"
                store.apply(me, bot_id)
                store.audit(me, "bot.apply", "approver", bot_id)
                ok = "applied"
            elif path == "/panel/invite":
                tab = "bots"
                store.set_invite(me, bot_id, form.get("invite_url", ""))
                store.audit(me, "bot.invite", "approver", bot_id, None, form.get("invite_url") or "cleared")
                ok = "invite_saved"
            elif path == "/panel/support":
                tab = "bots"
                store.set_support(me, bot_id, form.get("support_url", ""))
                store.audit(me, "bot.support", "approver", bot_id, None, form.get("support_url") or "cleared")
                ok = "support_saved"
            elif path == "/panel/rotate-key":
                tab = "bots"
                store.rotate_key(bot_id, me)
                store.audit(me, "key.rotate", "approver", bot_id)
                ok = "rotated"
            elif path == "/panel/approve":
                if not can_approve(role):
                    raise PermissionError
                owner = who("owner_id")
                store.approve(bot_id, owner, me)
                store.audit(me, "bot.approve", "approver", bot_id, owner)
                ok = "approved"
            elif path == "/panel/deny":
                if not can_approve(role):
                    raise PermissionError
                bot = store.deny(bot_id, me, form.get("reason", ""))
                store.audit(me, "bot.deny", "approver", bot_id, None, bot["deny_reason"])
                ok = "bot_denied"
            elif path == "/panel/revoke":
                if not can_revoke(role):
                    raise PermissionError
                store.revoke(bot_id)
                store.audit(me, "bot.revoke", "approver", bot_id)
                ok = "revoked"
            elif path == "/panel/owner":
                if not can_change_owner(role):
                    raise PermissionError
                owner = who("owner_id")
                store.set_owner(bot_id, owner, form.get("new_key") == "1")
                store.audit(me, "bot.owner", "approver", bot_id, owner, "new key" if form.get("new_key") == "1" else None)
                ok = "owner"
            elif path in ("/panel/share", "/panel/unshare"):
                tab = "bots"
                target = who("user_id")
                if path == "/panel/share":
                    store.share(me, bot_id, target, form.get("level", ""))
                    store.audit(me, "bot.share", "approver", bot_id, target, form.get("level"))
                    ok = "shared"
                else:
                    store.unshare(me, bot_id, target)
                    store.audit(me, "bot.unshare", "approver", bot_id, target)
                    ok = "unshared"
            elif path == "/panel/role":
                tab = "team"
                target = who("user_id")
                store.set_role(me, target, form.get("role", ""))
                store.audit(me, "role.add", "manager", None, target, form.get("role"))
                ok = "role_added"
            elif path == "/panel/remove-role":
                tab = "team"
                target = who("user_id")
                store.remove_role(me, target)
                store.audit(me, "role.remove", "manager", None, target)
                ok = "role_removed"
            elif path == "/panel/announce":
                tab = "announce"
                if role not in ("owner", "manager"):
                    raise PermissionError
                target = who("person") if form.get("audience") == "one" else "all"
                store.announce(me, target, form.get("title", ""), form.get("body", ""))
                store.audit(me, "announce.send", "manager", None, target, form.get("title"))
                ok = "announced"
            elif path == "/panel/dismiss":
                upto = form.get("upto", "")
                store.dismiss(me, int(upto) if upto.isdigit() else 0)
                return self.redirect(form.get("back") if form.get("back") in ("/panel", "/settings") else "/panel")
            elif path == "/panel/settings":
                store.set_timezone(me, form.get("timezone", ""))
                store.audit(me, "settings.timezone", "manager", None, None, form.get("timezone"))
                return self.redirect("/settings?msg=saved")
            elif path == "/panel/api-key":
                plain = store.create_api_key(me, form.get("name", ""))
                store.audit(me, "apikey.create", "manager", None, None, form.get("name") or None)
                self.use_context(session)
                return self.html(200, lambda n: panel.settings_page(store, session, n, "saved", plain, panel.banners(store.unread_for(me), session.csrf, "/settings")))
            elif path == "/panel/api-key/remove":
                key_id = form.get("key_id", "")
                store.remove_api_key(me, int(key_id) if key_id.isdigit() else -1)
                store.audit(me, "apikey.remove", "manager")
                return self.redirect("/settings?msg=key_removed")
            else:
                return self.reply(404, {"error": "not_found"})
        except PermissionError:
            return self.redirect(f"/panel/{tab}?msg=denied")
        except ReasonRequired:
            return self.redirect(f"/panel/{tab}?msg=reason_needed")
        except UserLookupError as e:
            return self.redirect(f"/panel/{tab}?msg={e.code}")
        except ValueError:
            return self.redirect(f"/panel/{tab}?msg=invalid")
        self.redirect(f"/panel/{tab}?msg={ok}")

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
        self.store.audit(session.user_id, "key.reveal", "approver", bot["bot_id"])
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
        if bot["status"] == "denied":
            return self.reply(403, {"error": "denied", "status": "denied", "reason": bot["deny_reason"], "support_server": cfg.support_url,
                                    "message": DENIED.format(bot_id=bot_id, reason=bot["deny_reason"], support=cfg.support_url)})
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

    def handle_report(self):
        cfg = self.cfg
        auth_header = self.headers.get("Authorization") or ""
        bot_id = read_token(cfg.secret, auth_header[7:].strip()) if auth_header.lower().startswith("bearer ") else None
        if not bot_id:
            return self.reply(401, {"error": "bad_token", "message": "Ask for a new token with POST /."}, {"WWW-Authenticate": "Bearer"})
        bot = cfg.store.get_bot(bot_id)
        if not bot or bot["status"] != "approved":   # approval withdrawn since the token was made
            return self.reply(403, {"error": "not_approved", "message": NOT_APPROVED.format(bot_id=bot_id, support=cfg.support_url, status="")})
        if not cfg.report_limit.allow(bot_id):
            return self.reply(429, {"error": "slow_down", "message": "Reports are every 30 minutes."}, {"Retry-After": "600"})
        report = self.json_body(cfg.max_report_bytes)    # the big limit only applies after the token checked out
        if report is None:
            return
        info = report.get("bot")
        if not isinstance(report.get("schema"), int) or not isinstance(info, dict) or str(info.get("id")) != bot_id:
            return self.reply(403, {"error": "token_mismatch", "message": "The token is for a different bot than the report."})
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
  deny <bot_id> <reason>      say no to a waiting bot (the reason is required; its owner sees it)
  revoke <bot_id>             remove a bot's access
  owner <bot_id> <user_id>    change a bot's owner
  team                        everyone with a role
  add <user_id> <manager|approver>  give someone a role
  remove <user_id>            take someone's role away
  find <username or id>       look someone up (from their Yapin' login)
  stats                       login numbers for the whole panel
  announce all|<user> <title> | <message>   leave an announcement (shown when they open the page)
  activity [n]                the latest panel activity
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
            return f"{c.get('pending', 0)} pending, {c.get('approved', 0)} approved, {c.get('denied', 0)} denied, {c.get('revoked', 0)} removed"
        if cmd == "list":
            status = {"removed": "revoked"}.get(args[0], args[0]) if args else "approved"
            bots = store.with_status(status)
            return "\n".join(f"{b['bot_id']}  {b['name'] or '-'}  owner={b['owner_id'] or b['claimed_owner_id'] or '-'}" for b in bots) or f"no {status} bots"
        if cmd == "approve" and len(args) == 2:
            bot = store.approve(args[0], store.resolve_user(args[1]), CONSOLE)
            store.audit(CONSOLE, "bot.approve", "approver", bot["bot_id"], bot["owner_id"])
            return f"approved {bot['bot_id']} (owner {bot['owner_id']}); the owner can copy its key in the panel"
        if cmd == "deny" and len(args) >= 2:
            bot = store.deny(args[0], CONSOLE, " ".join(args[1:]))
            store.audit(CONSOLE, "bot.deny", "approver", bot["bot_id"], None, bot["deny_reason"])
            return f"denied {bot['bot_id']}; the owner sees the reason"
        if cmd == "revoke" and len(args) == 1:
            store.revoke(args[0])
            return f"removed {args[0]}"
        if cmd == "owner" and len(args) == 2:
            store.set_owner(args[0], store.resolve_user(args[1]))
            return f"owner of {args[0]} is now {args[1]}"
        if cmd == "find" and args:
            found = store.search_users(" ".join(args), 10)
            return "\n".join(f"{u['user_id']}  {u.get('username') or '-'}  {u.get('display') or ''}  role={u.get('role') or '-'}" for u in found) or "nobody has logged in with that. Use their user ID, or ask them to log in to the panel."
        if cmd == "stats":
            st = store.user_stats()
            return (f"{st['total']} people logged in ({st['logins']} logins), active: {st['active_24h']} in 24h, {st['active_7d']} in 7d, {st['active_30d']} in 30d; "
                    f"new: {st['new_24h']} / {st['new_7d']} / {st['new_30d']}; bot owners {st['bot_owners']}")
        if cmd == "announce" and len(args) >= 3 and "|" in line:
            head, _, message = line.partition("|")
            who, *title = head.split()[1:]
            target = "all" if who == "all" else store.resolve_user(who)
            store.announce(CONSOLE, target, " ".join(title), message)
            store.audit(CONSOLE, "announce.send", "manager", None, target, " ".join(title))
            return f"announcement left for {'everyone' if target == 'all' else target}"
        if cmd == "activity":
            n = int(args[0]) if args and args[0].isdigit() else 15
            return "\n".join(f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(e['t']))}  {e['actor']}  {e['kind']}  {e.get('bot') or ''} {e.get('target') or ''} {e.get('detail') or ''}".rstrip()
                             for e in store.read_audit(limit=n)) or "nothing yet"
        if cmd == "team":
            return "\n".join(f"{m['user_id']}  {m['role']}" for m in store.staff()) or "nobody yet"
        if cmd == "add" and len(args) == 2:
            store.set_role(CONSOLE, store.resolve_user(args[0]), args[1].lower())
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
