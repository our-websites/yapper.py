#!/usr/bin/env python3
"""Receiver for yapper.py telemetry. Standard library only; run it on your own server (it is not part of the pip package).

    export TELEMETRY_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"   # keep this, it signs the tokens
    python3 telemetry_server.py approve 123456789 --name "Cool Bot"    # add a bot to the approved list
    python3 telemetry_server.py serve --host 127.0.0.1 --port 8080     # then put HTTPS in front of it (Caddy, nginx, a tunnel)

How it works (the bots do this for you, see yapper/telemetry.py):
  POST /        {"bot_id": "..."}  ->  200 {"token", "expires_in"}   when the bot is on the approved list
                                       403 {"error": "not_approved", "message": ...} when it is not: the message tells
                                       them to join the support server and ask for their bot to be added, and to bring
                                       their bot's ID and proof that yapper.py is their main SDK
                                       400 when the bot ID is missing (it asks for it)
  POST /report  Authorization: Bearer <token>, body = the report  ->  204, appended to the reports file
  GET  /health  ->  200

Tokens are signed with TELEMETRY_SECRET and expire (TELEMETRY_TOKEN_TTL seconds, default 3600). Nothing is stored about
who sent a report except the bot ID inside it: IP addresses are not logged or saved.

Settings (environment variables):
  TELEMETRY_SECRET        required for `serve`: at least 32 characters
  TELEMETRY_APPROVED      approved bots file, default ./approved_bots.json (re-read when it changes, no restart needed)
  TELEMETRY_DATA          reports file, one JSON object per line, default ./telemetry_reports.jsonl
  TELEMETRY_SUPPORT_URL   support server invite shown to bots that are not approved
  TELEMETRY_TOKEN_TTL     token lifetime in seconds, default 3600
  TELEMETRY_TRUST_PROXY   set to 1 when a reverse proxy in front of this sets X-Forwarded-For (for rate limits only)

A limit worth knowing about: a bot ID is public, so anyone who knows an approved bot's ID can ask for a token for it and
send reports in its name. Reports only ever change what you see in your own data, but if that matters to you, hand each
approved bot a private key when you approve it and check it in handle_token().
"""
import argparse
import base64
import hashlib
import hmac
import json
import dotenv
dotenv.load_dotenv()
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_SUPPORT_URL = "https://www.yapin.gg/invite/pythonlibrary"
BOT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_BODY = 64 * 1024

NOT_APPROVED = ("Your bot ({bot_id}) is not on the telemetry approved list yet, so nothing is sent. Join the support "
                "server ({support}) and ask someone to add your bot to the approved list. Please bring: "
                "(1) your bot's ID, and (2) proof that yapper.py is your main SDK, for example a link to your bot's source "
                "code that uses `import yapper`, or the output of `telemetry.preview(bot)`.")
NEED_BOT_ID = ("Send your bot's ID as JSON, {{\"bot_id\": \"<your bot's ID>\"}}. You also need to be on the approved list: "
               "join the support server ({support}), ask someone to add your bot, and bring your bot's ID and proof that "
               "yapper.py is your main SDK.")


class Config:
    def __init__(self, secret, approved_path="approved_bots.json", data_path="telemetry_reports.jsonl",
                 support_url=DEFAULT_SUPPORT_URL, token_ttl=3600, trust_proxy=False):
        if not secret or len(secret) < 32:
            raise ValueError("TELEMETRY_SECRET must be at least 32 characters (try: python3 -c 'import secrets; print(secrets.token_hex(32))')")
        self.secret = secret.encode()
        self.approved = Approved(approved_path)
        self.data_path = data_path
        self.support_url = support_url
        self.token_ttl = int(token_ttl)
        self.trust_proxy = trust_proxy
        self.write_lock = threading.Lock()
        self.auth_limit = RateLimiter(30, 60)          # token requests per minute per address
        self.report_limit = RateLimiter(20, 3600)      # reports per hour per bot (they send every 30 minutes)

    @classmethod
    def from_env(cls):
        env = os.environ
        return cls(env.get("TELEMETRY_SECRET", ""), env.get("TELEMETRY_APPROVED", "approved_bots.json"),
                   env.get("TELEMETRY_DATA", "telemetry_reports.jsonl"), env.get("TELEMETRY_SUPPORT_URL", DEFAULT_SUPPORT_URL),
                   env.get("TELEMETRY_TOKEN_TTL", "3600"), env.get("TELEMETRY_TRUST_PROXY", "") in ("1", "true", "yes"))


# ---- approved list ----

def load_approved(path):
    """{bot_id: info} from the file. Accepts {"bots": {"id": {...}}}, {"bots": ["id", ...]} or just ["id", ...]."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    bots = data.get("bots", data) if isinstance(data, dict) else data
    if isinstance(bots, list):
        return {str(b): {} for b in bots}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in bots.items()}


def save_approved(path, bots):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bots": bots}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


class Approved:
    """The approved list, re-read whenever the file changes, so approving a bot needs no restart."""

    def __init__(self, path):
        self.path, self._stamp, self._bots, self._lock = path, None, {}, threading.Lock()

    def bots(self):
        with self._lock:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                stamp = None
            if stamp != self._stamp:
                self._bots, self._stamp = load_approved(self.path), stamp
            return self._bots

    def __contains__(self, bot_id):
        return str(bot_id) in self.bots()


# ---- tokens ----

def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_token(secret, bot_id, ttl, now=None):
    payload = _b64(json.dumps({"b": str(bot_id), "e": int((now or time.time()) + ttl)}, separators=(",", ":")).encode())
    return f"{payload}.{hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()}"


def read_token(secret, token, now=None):
    """The bot ID a valid, unexpired token was made for, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data["b"]) if data["e"] > (now or time.time()) else None
    except Exception:
        return None


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

    def log_request(self, code="-", size="-"):   # method, path and status only: no addresses
        sys.stderr.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {self.command} {urlparse(self.path).path} {code}\n")

    def log_message(self, fmt, *args):
        pass

    def client(self):
        if self.cfg.trust_proxy and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def reply(self, status, body=None, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def body(self):
        """The JSON object in the request body ({} when there is none), or None after answering with an error."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.reply(413, {"error": "too_large", "message": f"Reports are at most {MAX_BODY} bytes."})
            return None
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            self.reply(400, {"error": "bad_json", "message": "The body must be a JSON object."})
            return None
        if not isinstance(data, dict):
            self.reply(400, {"error": "bad_json", "message": "The body must be a JSON object."})
            return None
        return data

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self.reply(200, {"ok": True})
        if url.path == "/":
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            return self.handle_token(query)
        self.reply(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/", "/report"):
            return self.reply(404, {"error": "not_found"})
        data = self.body()
        if data is None:
            return
        self.handle_token(data) if path == "/" else self.handle_report(data)

    # POST / (or GET /?bot_id=...): trade a bot ID for a token
    def handle_token(self, data):
        cfg = self.cfg
        if not cfg.auth_limit.allow(self.client()):
            return self.reply(429, {"error": "slow_down", "message": "Too many requests, try again in a minute."}, {"Retry-After": "60"})
        bot_id = str(data.get("bot_id") or "").strip()
        if not BOT_ID.match(bot_id):
            return self.reply(400, {"error": "bot_id_required", "support_server": cfg.support_url,
                                    "message": NEED_BOT_ID.format(support=cfg.support_url)})
        if bot_id not in cfg.approved:
            return self.reply(403, {"error": "not_approved", "bot_id": bot_id, "support_server": cfg.support_url,
                                    "needed": ["your bot's ID", "proof that yapper.py is your main SDK"],
                                    "message": NOT_APPROVED.format(bot_id=bot_id, support=cfg.support_url)})
        self.reply(200, {"token": make_token(cfg.secret, bot_id, cfg.token_ttl), "expires_in": cfg.token_ttl})

    # POST /report: store one report
    def handle_report(self, report):
        cfg = self.cfg
        auth = self.headers.get("Authorization") or ""
        bot_id = read_token(cfg.secret, auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        if not bot_id:
            return self.reply(401, {"error": "bad_token", "message": "Ask for a new token with POST /."}, {"WWW-Authenticate": "Bearer"})
        if bot_id not in cfg.approved:   # approval withdrawn since the token was made
            return self.reply(403, {"error": "not_approved", "message": NOT_APPROVED.format(bot_id=bot_id, support=cfg.support_url)})
        bot = report.get("bot")
        if not isinstance(report.get("schema"), int) or not isinstance(bot, dict) or str(bot.get("id")) != bot_id:
            return self.reply(403, {"error": "token_mismatch", "message": "The token is for a different bot than the report."})
        if not cfg.report_limit.allow(bot_id):
            return self.reply(429, {"error": "slow_down", "message": "Reports are every 30 minutes."}, {"Retry-After": "600"})
        record = {"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "report": report}
        with cfg.write_lock, open(cfg.data_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.reply(204)


def make_server(cfg, host="127.0.0.1", port=8080):
    server = ThreadingHTTPServer((host, port), Handler)
    server.cfg = cfg
    server.daemon_threads = True
    return server


# ---- command line ----

def main(argv=None):
    p = argparse.ArgumentParser(description="yapper.py telemetry receiver")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    a = sub.add_parser("approve", help="add a bot to the approved list")
    a.add_argument("bot_id")
    a.add_argument("--name", default="")
    a.add_argument("--note", default="", help="e.g. where you saw proof they use yapper.py")
    r = sub.add_parser("revoke", help="remove a bot from the approved list")
    r.add_argument("bot_id")
    sub.add_parser("list", help="show the approved list")
    args = p.parse_args(argv)
    path = os.environ.get("TELEMETRY_APPROVED", "approved_bots.json")

    if args.cmd == "serve":
        cfg = Config.from_env()
        if not cfg.approved.bots():
            print(f"note: {path} is empty or missing, so no bot can get a token yet. Add one: python3 {sys.argv[0]} approve <bot id>")
        server = make_server(cfg, args.host, args.port)
        print(f"telemetry server on http://{args.host}:{server.server_address[1]} (put HTTPS in front of it; bots require it)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("stopped")
        return 0

    bots = load_approved(path)
    if args.cmd == "approve":
        if not BOT_ID.match(args.bot_id):
            print("that does not look like a bot ID", file=sys.stderr)
            return 2
        bots[args.bot_id] = {k: v for k, v in (("name", args.name), ("note", args.note),
                                                ("approved_at", datetime.now(timezone.utc).strftime("%Y-%m-%d"))) if v}
        save_approved(path, bots)
        print(f"approved {args.bot_id} ({len(bots)} bots on the list)")
    elif args.cmd == "revoke":
        if bots.pop(args.bot_id, None) is None:
            print(f"{args.bot_id} was not on the list", file=sys.stderr)
            return 1
        save_approved(path, bots)
        print(f"removed {args.bot_id} ({len(bots)} bots on the list)")
    else:
        for bot_id, info in sorted(bots.items()):
            print(bot_id, info.get("name", ""), info.get("approved_at", ""))
        print(f"{len(bots)} approved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
