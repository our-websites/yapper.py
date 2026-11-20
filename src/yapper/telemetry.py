"""Optional, opt-in telemetry for yapper.py bots. Nothing in yapper imports this file, and it does nothing unless you
load it yourself, so a normal install sends nothing, ever.

    from yapper import telemetry
    telemetry.enable(bot)                      # or: bot.load_extension("yapper.telemetry")

Once on, it sends a small JSON report (see `collect()`) when the bot comes online and then every 30 minutes: bot id and
name, command names/descriptions/options, cog and extension names, how many listeners and buttons it has, how many
servers it is in and their total member count, the bot's profile picture link and description (its bio), the yapper version, the Python version and
the OS name, plus any extra values you add yourself with `add_data(bot, name, value)`. It never sends
the bot token, message content, user, server or channel names or ids, or anything else.

Reports are only accepted from bots on an approved list. The first time, the bot asks the telemetry server for a token
with its bot id; a bot that is not approved yet is told how to get on the list (see "Telemetry" in the README) and
nothing is sent until it is. Once approved, the bot's owner logs in to the server's panel, copies the bot's private key
and sets it as TELEMETRY_KEY (environment or .env). The bot tries again every 30 minutes, so neither step needs a restart
of anything but the bot itself.

See exactly what would be sent with `telemetry.preview(bot)`. Turn it off by removing the line that loads it, calling
`telemetry.disable(bot)`, or setting the environment variable YAPPER_TELEMETRY=0 (DO_NOT_TRACK=1 is honoured too).

This file is self-contained (standard library only), so it also works next to the single-file yapper.py download:
`import telemetry; telemetry.enable(bot)`.
"""
import json
import os
import platform
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from importlib import metadata

# The telemetry server's base URL (https only, no path), e.g. "https://telemetry.example.com". The bot asks it for a
# token at "/" and sends reports to "/report". Can be overridden per bot with enable(bot, endpoint="https://...").
ENDPOINT = "https://yapper-py.rustdactyl.tech" # Do not purposely exploit the fact that the endpoint is public, I will add more authentication if needed.

SCHEMA = 1                 # bump when the shape of the report changes
DEFAULT_INTERVAL = 1800    # seconds between reports: every 30 minutes
MIN_INTERVAL = 300         # reports are never sent more often than this
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_NOT_APPROVED = ("Your bot ({bot_id}) is not on the telemetry approved list yet, so nothing is sent. Join the support "
                 "server (the invite is in the yapper.py README) and ask someone to add your bot. Bring your bot's ID "
                 "and proof that yapper.py is your main SDK.")

__all__ = ["enable", "disable", "preview", "collect", "add_data", "remove_data", "setup", "teardown", "ENDPOINT"]


def _disabled_by_env():
    """YAPPER_TELEMETRY=0 (or off/false/no) or DO_NOT_TRACK=1 turns telemetry off, even when it is loaded."""
    if os.environ.get("YAPPER_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no", "disabled"):
        return True
    return os.environ.get("DO_NOT_TRACK", "").strip().lower() in ("1", "true", "yes", "on")


def _check_endpoint(url):
    url = (url or "").strip()
    if not url:
        raise ValueError("telemetry has no endpoint: pass enable(bot, endpoint='https://...') or set telemetry.ENDPOINT")
    parts = urllib.parse.urlparse(url)
    local = parts.hostname in _LOOPBACK
    if not parts.hostname or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local):
        raise ValueError(f"telemetry endpoint must be an https:// URL, got {url!r}")
    return url.rstrip("/")


def _clean(text, limit=600):
    """Text that came from the server, safe to print: no control characters or terminal escapes, and not endless."""
    return "".join(ch for ch in str(text) if ch.isprintable() or ch == "\n")[:limit]


def _dist_version(name):
    try:
        return metadata.version(name)
    except Exception:
        return None


def _sdk_version(bot):
    """The version of the SDK file the bot class came from: works for the package and for the loose single file."""
    return getattr(sys.modules.get(type(bot).__module__), "__version__", None)


MAX_DESCRIPTION = 300
_DESC_KEYS = ("bio",)
_desc_cache = {}


def _pick_description(*sources):
    """The first non-empty bio text in these dicts (or in their nested "user"), cleaned and shortened; None if there is none."""
    for src in sources:
        for d in (src, src.get("user") if isinstance(src, dict) else None):
            if not isinstance(d, dict):
                continue
            for key in _DESC_KEYS:
                value = d.get(key)
                if isinstance(value, str) and value.strip():
                    text = " ".join("".join(c if c >= " " and c != "\x7f" else " " for c in value).split())
                    return text[:MAX_DESCRIPTION]
    return None


def _description(bot, me, ttl=3600):
    """The bot's own description (its bio). shapes-bot reads people's bios from the full user profile (bot.user(id): bio), so that is tried after `me`, and last the bot's member info in a server it is in (a bot is always a member of its
    servers). Looked up at most once an hour; any failure just means no description."""
    key, now = id(bot), time.time()
    hit = _desc_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    text = _pick_description(me)
    if text is None and me.get("id"):
        try:
            text = _pick_description(dict(bot.user(me["id"])))
        except Exception:
            text = None
    if text is None:
        try:
            for server in list(bot.servers())[:3]:
                sid = server.get("id") if isinstance(server, dict) else getattr(server, "id", None)
                if sid and me.get("id"):
                    text = _pick_description(dict(bot.member(sid, me["id"])))
                    if text:
                        break
        except Exception:
            text = None
    _desc_cache[key] = (now + ttl, text)
    return text


def _servers(bot):
    """(count, total members, total online) over every server the bot is in, or None when the lookup fails."""
    try:
        servers = list(bot.servers())
    except Exception:
        return None
    members = sum(int(s.get("member_count") or 0) for s in servers)
    online = [s.get("online_count") for s in servers if s.get("online_count") is not None]
    return {"count": len(servers), "members_total": members, "online_total": sum(int(o) for o in online) if online else None}


def _sdk_info(bot):
    """version: the SDK's own version (the __version__ in main.py, or in the single-file download). package_version: the PyPI package's."""
    return {"version": _sdk_version(bot), "package_version": _dist_version("yapper.py")}


MAX_EXTRA_KEYS = 50
MAX_EXTRA_BYTES = 10_000   # per value, as JSON


def add_data(bot, name, value):
    """Add your own extra telemetry value to every report, shown to you in the panel next to the rest of your data.
    `value` is anything JSON can hold (numbers, text, lists, dicts), or a function with no arguments that returns one,
    called each time a report is built (e.g. `lambda: len(my_cache)`). Returns True when it was added; False when it does
    not fit (name over 64 characters, over 50 values, or a value over 10 KB). It is part of `preview(bot)`, so you always
    see exactly what goes out. Only add what you are happy to share: it is sent as it is."""
    extra = bot.__dict__.setdefault("_yapper_telemetry_extra", {})
    name = str(name)
    if not name or len(name) > 64 or (name not in extra and len(extra) >= MAX_EXTRA_KEYS):
        return False
    if not callable(value):
        try:
            if len(json.dumps(value)) > MAX_EXTRA_BYTES:
                return False
        except (TypeError, ValueError):
            return False
    extra[name] = value
    return True


def remove_data(bot, name):
    """Stop sending an extra value added with add_data(). Returns False when there was none by that name."""
    return (getattr(bot, "_yapper_telemetry_extra", None) or {}).pop(str(name), None) is not None


def _extra(bot):
    out = {}
    for name, value in list((getattr(bot, "_yapper_telemetry_extra", None) or {}).items()):
        try:
            value = value() if callable(value) else value
            if len(json.dumps(value)) <= MAX_EXTRA_BYTES:
                out[name] = value
        except Exception:
            continue   # a broken value never breaks the report
    return out


ASSET_SITE = "https://www.yapin.gg"
ASSET_FILES = "https://files.yapper.website"


def _avatar(me):
    """The bot's profile picture as an https link. Yapin' gives pictures as a path (avatar_path / avatar), which lives under
    files.yapper.website/avatars/, or on www.yapin.gg when it starts with a slash; full https links are kept as they are."""
    for key in ("avatar_url", "avatar_path", "avatar"):
        value = me.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 300 or value.strip().startswith(("javascript:", "data:", "http://")):
            continue
        value = value.strip()
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("https://"):
            url = value
        elif ":" in value.split("/", 1)[0]:
            continue
        elif value.startswith("/"):
            url = ASSET_SITE + value
        else:
            url = f"{ASSET_FILES}/avatars/{value}"
        return url.replace(" ", "%20")
    return None


def collect(bot, event="report", started=None):
    """Build the report as a plain dict. This is the complete list of what telemetry sends (nothing is added later)."""
    me = getattr(bot, "me", None) or {}
    commands = []
    for name, c in sorted((getattr(bot, "commands", None) or {}).items()):
        opts = [{"name": o.get("name"), "type": o.get("type"), "required": bool(o.get("required"))}
                for o in c.get("options") or []]
        commands.append({"name": name, "description": c.get("description") or "", "options": opts})
    ours = getattr(getattr(bot, "_yapper_telemetry", None), "_on_ready", None)
    listeners = {ev: len([f for f in fns if f != ours]) for ev, fns in (getattr(bot, "_listeners", None) or {}).items()}
    return {
        "schema": SCHEMA,
        "event": event,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sdk": _sdk_info(bot),
        "runtime": {"python": platform.python_version(), "os": platform.system()},
        "bot": {"id": me.get("id"), "username": me.get("username"), "avatar": _avatar(me), "description": _description(bot, me),
                "uptime_seconds": int(time.time() - started) if started else None},
        "servers": _servers(bot),
        "commands": commands,
        "cogs": sorted(getattr(bot, "cogs", None) or {}),
        "extensions": sorted(n for n in (getattr(bot, "extensions", None) or {}) if n != __name__),
        "listeners": {ev: n for ev, n in sorted(listeners.items()) if n},
        "buttons": len(getattr(bot, "_buttons", None) or []),
        "extra": _extra(bot),
    }


class _Reporter:
    """Asks the server for a token, then sends the report, from a background thread so the bot itself is never slowed
    down or broken by it."""

    def __init__(self, bot, endpoint, interval, owner_id=None):
        self.bot, self.endpoint, self.interval = bot, endpoint, interval
        self.owner_id = owner_id
        self.started = None
        self._stop = threading.Event()
        self._thread = None
        self._was_running = False
        self._token = None
        self._token_until = 0.0
        self._said = set()

    def _on_ready(self, _me=None):
        self.start()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self.started = time.time()
        self._thread = threading.Thread(target=self._loop, name="yapper-telemetry", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        event = "startup"
        while not self._stop.is_set():
            if getattr(self.bot, "running", None):
                self._was_running = True
            elif self._was_running:   # the bot was stopped: stop reporting too
                break
            self.send(event)
            event = "heartbeat"
            if self._stop.wait(self.interval * random.uniform(0.9, 1.1)):
                break

    def _say(self, message):
        """Tell the person running the bot something, once."""
        if message not in self._said and getattr(self.bot, "log", True):
            self._said.add(message)
            print(f"[yapper] telemetry: {message}")

    def _post(self, path, payload, token=None):
        headers = {"Content-Type": "application/json", "User-Agent": f"yapper-telemetry/{SCHEMA}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.endpoint + path, data=json.dumps(payload, separators=(",", ":")).encode(),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, raw = r.status, r.read(65536)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(65536)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        return status, body if isinstance(body, dict) else {}

    def _key(self):
        """The bot's private telemetry key: TELEMETRY_KEY in the environment (or .env). The server hands it out in its panel."""
        return (os.environ.get("TELEMETRY_KEY") or "").strip() or None

    def _owner_id(self):
        """Your Yapin' user ID, only used so you can see your bot's status (pending, approved) in the panel before approval."""
        owner = self.owner_id or os.environ.get("TELEMETRY_OWNER_ID") or (getattr(self.bot, "me", None) or {}).get("owner_id")
        return str(owner).strip() if owner else None

    def _authenticate(self):
        """Trade the bot's id (and private key) for a short-lived token. None (with an explanation) when not allowed yet."""
        me = getattr(self.bot, "me", None) or {}
        bot_id = me.get("id")
        if not bot_id:
            return None
        payload = {"bot_id": str(bot_id), "username": me.get("username"), "schema": SCHEMA, "sdk": _sdk_info(self.bot),
                   "interval": self.interval}
        if self._key():
            payload["key"] = self._key()
        if self._owner_id():
            payload["owner_id"] = self._owner_id()
        status, body = self._post("/", payload)
        if status == 200 and isinstance(body.get("token"), str):
            self._token = body["token"]
            self._token_until = time.time() + max(60, int(body.get("expires_in") or 3600)) - 60
            return self._token
        if status in (401, 403) and isinstance(body.get("message"), str):
            self._say(_clean(body["message"]))
        elif status == 403:
            self._say(_NOT_APPROVED.format(bot_id=bot_id))
        else:
            self._say(f"the telemetry server answered {status}; will try again later")
        return None

    def send(self, event):
        """One report. True when the server took it; False (never an exception) when not."""
        try:
            token = self._token if self._token and time.time() < self._token_until else self._authenticate()
            if not token:
                return False
            report = collect(self.bot, event, self.started)
            status, _ = self._post("/report", report, token)
            if status in (401, 403):   # token expired or approval withdrawn: ask again once
                self._token = None
                token = self._authenticate()
                status = self._post("/report", report, token)[0] if token else status
            return 200 <= status < 300
        except Exception as e:   # telemetry must never break a bot
            self._say(f"could not send a report ({type(e).__name__}); will try again later, quietly")
            return False


def enable(bot, endpoint=None, interval=DEFAULT_INTERVAL, owner_id=None, extra=None):
    """Turn telemetry on for this bot. Returns True when it is on, False when YAPPER_TELEMETRY=0 / DO_NOT_TRACK=1 is set.
    Reports start once the bot is online. `interval` (seconds, at least 300) is the time between reports, 30 minutes by
    default. Once your bot is approved you get a private key in the panel: set it as TELEMETRY_KEY in your environment or
    .env file. `owner_id` (or TELEMETRY_OWNER_ID) is your Yapin' user ID, so you can see your bot's status in the panel
    before it is approved. `extra` is a dict of your own values to send along (see add_data). Raises ValueError when there is no
    (https) endpoint."""
    if getattr(bot, "_yapper_telemetry", None) is not None:
        return True
    if _disabled_by_env():
        if getattr(bot, "log", True):
            print("[yapper] telemetry is loaded but switched off by YAPPER_TELEMETRY / DO_NOT_TRACK: nothing is sent")
        return False
    url = _check_endpoint(endpoint or ENDPOINT)
    reporter = _Reporter(bot, url, max(MIN_INTERVAL, int(interval)), owner_id)
    for name, value in (extra or {}).items():
        add_data(bot, name, value)
    bot._yapper_telemetry = reporter
    bot.on("ready", reporter._on_ready)
    if getattr(bot, "log", True):
        host = urllib.parse.urlparse(url).netloc
        print(f"[yapper] telemetry is ON (you loaded it): every {reporter.interval // 60} minutes the bot's name, commands, "
              f"server and member counts and the yapper/Python version go to {host}, if your bot is on the approved list "
              f"(see the README). No token, messages or user data. See it all: telemetry.preview(bot). "
              f"Off: remove the telemetry line or set YAPPER_TELEMETRY=0")
    if getattr(bot, "me", None) and getattr(bot, "running", False):   # enabled after the bot came online
        reporter.start()
    return True


def disable(bot):
    """Turn telemetry off again for this bot. Returns False when it was not on."""
    reporter = getattr(bot, "_yapper_telemetry", None)
    if reporter is None:
        return False
    reporter.stop()
    fns = (getattr(bot, "_listeners", None) or {}).get("ready", [])
    if reporter._on_ready in fns:
        fns.remove(reporter._on_ready)
    bot._yapper_telemetry = None
    return True


def preview(bot):
    """The exact report that would be sent right now, as a dict. Nothing is sent."""
    reporter = getattr(bot, "_yapper_telemetry", None)
    return collect(bot, "preview", getattr(reporter, "started", None))


def setup(bot):
    """bot.load_extension("yapper.telemetry") turns telemetry on (and bot.unload_extension turns it off again)."""
    enable(bot)


def teardown(bot):
    disable(bot)
