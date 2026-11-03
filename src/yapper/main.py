"""Yapin’ bot library for Python. One file, Python 3.9+, no required packages (python-dotenv, if it is installed,
loads a .env file with YAPPER_BOT_TOKEN=yb_... for you).

    import yapper

    bot = yapper.Bot()                     # token from YAPPER_BOT_TOKEN, or yapper.Bot("yb_...")

    @bot.command("ping", "Is the bot alive?")
    def ping(msg):
        msg.reply("pong")

    @bot.on("member_join")
    def welcome(ev):
        print(ev.username, "joined")

    @bot.command("vote", "Pizza?")
    def vote(msg):
        msg.reply(embeds=yapper.Embed().title("Pizza tonight?").color("blue"),
                  components=yapper.Row(yapper.Button.success("Yes", "vote:yes"), yapper.Button.danger("No", "vote:no")))

    @bot.button("vote:")                   # clicks on buttons whose custom_id starts with "vote:"
    def voted(i):                          # (bot.on_button("ping") for one exact custom_id, or a re.compile pattern)
        i.ephemeral("You picked " + i.custom_id[5:])

    bot.load_cogs("cogs")                  # optional: every cogs/*.py (see Cog below)
    bot.run()

Save this file next to your bot and run it with `python bot.py`. Nothing to install, nothing to host:
the bot keeps one WebSocket open to Yapin’ (the gateway) and gets its events pushed; when that is off or down
it fetches them itself (long polling). Docs: https://www.yapin.gg/developers
"""
import base64
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import random
import re
import select
import socket
import ssl
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request


def _load_dotenv():
    """Read a .env file (YAPPER_BOT_TOKEN=yb_...) when python-dotenv is installed (pip installs it with this package;
    used as one loose file it is optional). Looks in the folder of the script you ran, then the current folder, and
    in each parent folder, and uses the first one it finds. Variables already set in the environment win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    starts = []
    if sys.argv and sys.argv[0]:
        starts.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    starts.append(os.getcwd())
    seen = set()
    for folder in starts:
        while True:
            path = os.path.join(folder, ".env")
            if path not in seen:
                seen.add(path)
                if os.path.isfile(path):
                    load_dotenv(path)
                    return
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent


_load_dotenv()

__all__ = ["Bot", "VoiceCall", "Cog", "command", "listener", "button", "YapperError", "row", "btn", "link_button",
           "Embed", "Button", "Row", "TextDisplay", "Thumbnail", "Section", "MediaGallery", "Separator", "FileComponent",
           "Container", "IS_COMPONENTS_V2", "text", "thumbnail", "section", "media", "gallery", "separator", "file_display",
           "container", "Message", "Interaction", "Data", "VERSION"]
# Stamped from public/sdk/version.json by scripts/sdk-build.mjs (same as the PyPI package "yapin").
__version__ = "1.5.0"
VERSION = __version__

BASE = "https://bots.yapper.website/api/bot/v1"
# The website's address for the same API: used for 10 minutes when BASE can't be reached.
FALLBACK_BASE = "https://www.yapin.gg/api/bot/v1"
GATEWAY = "wss://rt.yapper.website/bot"
COMMAND_LIMIT = 100
EVENTS = ("message", "command", "dm_message", "member_join", "member_leave", "reaction_add", "reaction_remove",
          "voice_join", "voice_leave", "invite_create", "invite_delete", "interaction", "test", "ready", "error")


class _Form:
    """A multipart body for Bot.api (send with files)."""

    def __init__(self, data, content_type):
        self.data = data
        self.content_type = content_type


def _file_part(f):
    """(name, bytes) for one file: a path, an open binary file, or a (name, bytes) pair."""
    if isinstance(f, str):
        with open(f, "rb") as fh:
            return os.path.basename(f), fh.read()
    if isinstance(f, (tuple, list)) and len(f) == 2:
        name, data = f
        return str(name), data.read() if hasattr(data, "read") else bytes(data)
    if hasattr(f, "read"):
        return os.path.basename(str(getattr(f, "name", "") or "file")), f.read()
    raise ValueError('A file is a path, an open binary file, or ("card.png", bytes)')


def _form(body, files):
    """multipart/form-data: the JSON body in payload_json, the files in files[0], files[1]..."""
    files = list(files) if isinstance(files, (list, tuple)) and not (len(files) == 2 and isinstance(files[0], str) and isinstance(files[1], (bytes, bytearray))) else [files]
    if len(files) > 4:
        raise ValueError("Up to 4 files per message")
    boundary = "yapper" + os.urandom(12).hex()
    out = bytearray()

    def part(headers, data):
        out.extend(f"--{boundary}\r\n{headers}\r\n\r\n".encode())
        out.extend(data)
        out.extend(b"\r\n")

    part('Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json', json.dumps(body).encode())
    for i, f in enumerate(files):
        name, data = _file_part(f)
        safe = re.sub(r'["\r\n\\]', "_", name) or "file"
        part(f'Content-Disposition: form-data; name="files[{i}]"; filename="{safe}"\r\nContent-Type: application/octet-stream', data)
    out.extend(f"--{boundary}--\r\n".encode())
    return _Form(bytes(out), f"multipart/form-data; boundary={boundary}")


class YapperError(RuntimeError):
    """An API call failed. `.status` is the HTTP status, `.retry_after` the seconds to wait on a 429 (or None)."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Data(dict):
    """A dict you can also read with dots: ev.user_id is ev["user_id"]. Missing keys are None."""

    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value

    @classmethod
    def of(cls, data, bot):
        d = cls(data)
        object.__setattr__(d, "_bot", bot)   # kept off the dict, so json.dumps(msg) still works
        return d


class Message(Data):
    """A message event with helpers: msg.reply(text), msg.say(text), msg.author_mention.
    Commands also have msg.command, msg.args (the rest of the line) and msg.argv (split on spaces)."""

    def reply(self, content="", embeds=None, components=None, files=None):
        """Reply to this message (quoted). files=[...] like bot.send."""
        return self._bot.send(self.channel_id, content, embeds=embeds, components=components, reply_to=self.id, files=files)

    def say(self, content="", embeds=None, components=None):
        """Post in the same channel without quoting."""
        return self._bot.send(self.channel_id, content, embeds=embeds, components=components)

    @property
    def author_mention(self):
        return f"<@{self.author_id}>"


class Interaction(Data):
    """A button click: i.custom_id, i.user_id, i.username, and i.reply / i.ephemeral / i.update / i.ack.
    Answer within 3 seconds or the person sees "This interaction failed"."""

    def _answer(self, kind, content=None, embeds=None, components=None, layout=None):
        return self._bot.respond(self, kind, content=content, embeds=embeds, components=components, layout=layout)

    def reply(self, content="", embeds=None, components=None, layout=None):
        """A new message in the channel. layout=[...] sends a Components V2 message instead."""
        return self._answer("reply", None if layout is not None else content, embeds, components, layout)

    def ephemeral(self, content="", embeds=None):
        """A message only the person who clicked sees."""
        return self._answer("ephemeral", content, embeds)

    def update(self, content=None, embeds=None, components=None, layout=None):
        """Change the message the button is on (left out = kept). On a Components V2 message: update(layout=[...])."""
        return self._answer("update", content, embeds, components, layout)

    def ack(self):
        """Just say "got it", so no error shows."""
        return self._answer("ack")

    @property
    def user_mention(self):
        return f"<@{self.user_id}>"


class Webhook(Data):
    """A webhook the bot can post through: hook.send("hi", username="Alice", avatar_url="https://i.imgur.com/x.png"),
    hook.delete(). hook.token and hook.url only come with create_webhook; the bot token is enough to post."""

    def send(self, content="", username=None, avatar_url=None, embeds=None, components=None, allowed_mentions=None):
        """Post as this webhook with its own name and picture for this message. Returns the message."""
        return self._bot.send_webhook(self.id, content, username=username, avatar_url=avatar_url, embeds=embeds,
                                      components=components, allowed_mentions=allowed_mentions)

    def delete(self):
        """Delete it (its old messages stay)."""
        return self._bot.delete_webhook(self.id)


def _newer(a, b):
    """a > b for "1.10.0" style versions."""
    def parts(v):
        out = []
        for x in str(v).split(".")[:3]:
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out + [0] * (3 - len(out))
    return parts(a) > parts(b)


_version_checked = False


def _check_version(base, log):
    """Once per process: one line when the server knows a newer SDK (GET /version, no token). Runs in the
    background and never raises. YAPPER_NO_UPDATE_CHECK=1 (or log=False) turns it off."""
    global _version_checked
    if _version_checked or not log or os.environ.get("YAPPER_NO_UPDATE_CHECK"):
        return
    _version_checked = True

    def check():
        try:
            req = urllib.request.Request(base + "/version", headers={"User-Agent": f"yapper-py/{__version__}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                latest = (json.loads(r.read().decode() or "{}").get("sdk") or {}).get("python")
            if isinstance(latest, str) and _newer(latest, __version__):
                print(f"[yapper] SDK {__version__} is behind the latest {latest}: pip install -U yapin, "
                      "or download https://www.yapin.gg/sdk/yapper.py again", file=sys.stderr)
        except Exception:
            pass  # offline or an older server: no warning
    threading.Thread(target=check, daemon=True).start()


def row(*buttons):
    """A row of up to 5 buttons: row(btn("Yes", "vote:yes", "success"), btn("No", "vote:no", "danger"))."""
    return {"type": "row", "components": list(buttons)}


def btn(label, custom_id, style="secondary", emoji=None, disabled=False):
    """A button. Styles: primary, secondary, success, danger."""
    b = {"type": "button", "style": style, "label": label, "custom_id": custom_id, "disabled": disabled}
    if emoji:
        b["emoji"] = emoji
    return b


def link_button(label, url):
    """A button that opens a https link."""
    return {"type": "button", "style": "link", "label": label, "url": url}


# ---- Components V2 (bot.send_layout): layout components, standard JSON shape and type numbers ----

IS_COMPONENTS_V2 = 1 << 15
_COLORS = {"red": 0xED4245, "green": 0x57F287, "blue": 0x5865F2, "yellow": 0xFEE75C, "purple": 0x9B59B6,
           "orange": 0xE67E22, "grey": 0x95A5A6, "gray": 0x95A5A6}


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and v is not False}


def text(content):
    """Markdown text (type 10). Mentions, links and emoji work like in messages; no HTML."""
    return {"type": 10, "content": str(content)}


def thumbnail(url, description=None, spoiler=False):
    """A small picture (type 11), only as a section's accessory."""
    return _clean({"type": 11, "media": {"url": url}, "description": description, "spoiler": spoiler})


def section(*texts, accessory):
    """1 to 3 lines of text with a button or a thumbnail beside them (type 9): section("Ready?", accessory=btn("Go", "go"))."""
    return {"type": 9, "components": [t if isinstance(t, dict) else text(t) for t in texts], "accessory": accessory}


def media(url, description=None, spoiler=False):
    """One picture or video for gallery()."""
    return _clean({"media": {"url": url}, "description": description, "spoiler": spoiler})


def gallery(*items):
    """1 to 10 pictures or videos in a grid (type 12): gallery("https://…", media("https://…", spoiler=True))."""
    return {"type": 12, "items": [i if isinstance(i, dict) else media(i) for i in items]}


def separator(divider=True, spacing="small"):
    """A line (or just space with divider=False), spacing "small" or "large" (type 14)."""
    return {"type": 14, "divider": bool(divider), "spacing": 2 if spacing in ("large", 2) else 1}


def file_display(url, spoiler=False):
    """A file to download (type 13): an https link to a Yapin’ upload or an allowed host."""
    return _clean({"type": 13, "file": {"url": url}, "spoiler": spoiler})


def container(*components, accent=None, spoiler=False):
    """A card holding other components (type 17). accent: 0x5865F2, "#5865f2" or a name like "blue"."""
    c = {"type": 17, "components": [x if isinstance(x, dict) else text(x) for x in components]}
    if accent is not None:
        c["accent_color"] = accent if isinstance(accent, int) else _COLORS.get(str(accent).lower()) or int(str(accent).lstrip("#"), 16)
    if spoiler:
        c["spoiler"] = True
    return c


def _color(c):
    """'#5865f2', 0x5865f2 or a name like "blue" -> the number."""
    if isinstance(c, int):
        return c
    return _COLORS.get(str(c).lower()) or int(str(c).lstrip("#"), 16)


def _plain(x):
    """Builders (Embed, Button, Row, Container ...) -> plain dicts, inside lists and dicts too, so they can be sent."""
    if hasattr(x, "to_dict"):
        return _plain(x.to_dict())
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def _listed(v):
    """One item or a list -> a plain list (None stays None)."""
    if v is None:
        return None
    return _plain(v if isinstance(v, (list, tuple)) else [v])


# ---- builders: the same as the JS SDK's classes. Plain dicts (row(), btn(), container() ...) still work too. ----

class Embed:
    """Builds an embed card. Every setter returns the embed, so they chain:

        yapper.Embed().title("Score").description("**3** wins").color("green") \\
            .field("Kills", "12", True).field("Deaths", "4", True).thumbnail(url).footer("Season 2").timestamp()

    Pictures must be https links to Yapin’ uploads or common image hosts (full list on /developers).
    Limits: title 256, description 4096, 10 fields (name 256, value 1024), 6000 characters in all."""

    def __init__(self, data=None, **kwargs):
        self.data = dict(data or {}, **kwargs)

    def title(self, t):
        self.data["title"] = str(t)
        return self

    def description(self, t):
        self.data["description"] = str(t)
        return self

    def url(self, u):
        self.data["url"] = str(u)
        return self

    def color(self, c):
        """'#5865f2', 0x5865f2 or 'red' | 'green' | 'blue' | 'yellow' | 'purple' | 'orange' | 'grey'."""
        self.data["color"] = _color(c)
        return self

    def field(self, name, value, inline=False):
        self.data.setdefault("fields", []).append({"name": str(name), "value": str(value), "inline": bool(inline)})
        return self

    def author(self, name, url=None, icon=None):
        self.data["author"] = _clean({"name": str(name), "url": url, "icon_url": icon})
        return self

    def footer(self, text, icon=None):
        self.data["footer"] = _clean({"text": str(text), "icon_url": icon})
        return self

    def thumbnail(self, url):
        self.data["thumbnail"] = {"url": str(url)}
        return self

    def image(self, url):
        self.data["image"] = {"url": str(url)}
        return self

    def timestamp(self, t=None):
        """A datetime, a unix time in seconds or an ISO string; now when left out."""
        import datetime
        if t is None:
            t = datetime.datetime.now(datetime.timezone.utc)
        elif isinstance(t, (int, float)):
            t = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
        self.data["timestamp"] = t.isoformat() if hasattr(t, "isoformat") else str(t)
        return self

    def to_dict(self):
        return dict(self.data)


class Button:
    """A button for `components`. Shortcuts: Button.primary(label, custom_id), .secondary, .success, .danger,
    Button.link(label, url). Row(Button.success("Yes", "vote:yes"), Button.danger("No", "vote:no")).
    Limits: 5 rows of 5 buttons, label 80, custom_id 100 (each one different), link urls https only."""

    def __init__(self, data=None, **kwargs):
        self.data = {"type": "button", "style": "secondary", **(data or {}), **kwargs}

    @classmethod
    def primary(cls, label, custom_id):
        return cls(style="primary", label=label, custom_id=custom_id)

    @classmethod
    def secondary(cls, label, custom_id):
        return cls(style="secondary", label=label, custom_id=custom_id)

    @classmethod
    def success(cls, label, custom_id):
        return cls(style="success", label=label, custom_id=custom_id)

    @classmethod
    def danger(cls, label, custom_id):
        return cls(style="danger", label=label, custom_id=custom_id)

    @classmethod
    def link(cls, label, url):
        return cls(style="link", label=label, url=url)

    def style(self, s):
        self.data["style"] = s
        return self

    def label(self, t):
        self.data["label"] = str(t)
        return self

    def emoji(self, e):
        """One emoji: "🔥" or a server emoji "<:name:id>"."""
        self.data["emoji"] = str(e)
        return self

    def id(self, custom_id):
        self.data["custom_id"] = str(custom_id)
        return self

    def url(self, u):
        self.data["style"] = "link"
        self.data["url"] = str(u)
        self.data.pop("custom_id", None)
        return self

    def disabled(self, on=True):
        self.data["disabled"] = bool(on)
        return self

    def to_dict(self):
        return dict(self.data)


class Row:
    """A row of up to 5 buttons. Works under a normal message and inside a Components V2 layout."""

    def __init__(self, *buttons):
        self.buttons = _flat(buttons)

    def add(self, *buttons):
        self.buttons += _flat(buttons)
        return self

    def to_dict(self):
        return {"type": "row", "components": _plain(self.buttons)}


def _flat(items):
    out = []
    for x in items:
        out.extend(x if isinstance(x, (list, tuple)) else [x])
    return out


def _text_of(t):
    return t if isinstance(t, (TextDisplay, dict)) else TextDisplay(t)


class TextDisplay:
    """Markdown text (type 10). Mentions, links and emoji work like in messages; no HTML."""

    def __init__(self, content):
        self.content = str(content)

    def to_dict(self):
        return text(self.content)


class Thumbnail:
    """A small picture (type 11), only as a Section's accessory."""

    def __init__(self, url, description=None, spoiler=False):
        self.data = thumbnail(url, description, spoiler)

    def to_dict(self):
        return dict(self.data)


class Section:
    """1 to 3 lines of text with a button or a thumbnail beside them (type 9)."""

    def __init__(self, *texts):
        self.texts = [_text_of(t) for t in _flat(texts)]
        self.side = None

    def text(self, t):
        self.texts.append(_text_of(t))
        return self

    def accessory(self, a):
        """A Button, or a Thumbnail (or just a picture url)."""
        self.side = Thumbnail(a) if isinstance(a, str) else a
        return self

    def to_dict(self):
        return {"type": 9, "components": _plain(self.texts), "accessory": _plain(self.side)}


class MediaGallery:
    """1 to 10 pictures or videos in a grid (type 12)."""

    def __init__(self, *urls):
        self.items = []
        for u in _flat(urls):
            self.add(u)

    def add(self, url, description=None, spoiler=False):
        self.items.append(media(url, description, spoiler))
        return self

    def to_dict(self):
        return {"type": 12, "items": list(self.items)}


class Separator:
    """A line or empty space (type 14): Separator(divider=False, spacing="large")."""

    def __init__(self, divider=True, spacing="small"):
        self.data = separator(divider, spacing)

    def to_dict(self):
        return dict(self.data)


class FileComponent:
    """A file to download (type 13): an https link to a Yapin’ upload or an allowed host."""

    def __init__(self, url, spoiler=False):
        self.data = file_display(url, spoiler)

    def to_dict(self):
        return dict(self.data)


class Container:
    """A card holding other components (type 17), with an optional accent colour bar and spoiler.
    Plain strings become text displays."""

    def __init__(self, *components):
        self.parts = _flat(components)
        self.data = {"type": 17}

    def accent(self, c):
        """'#5865f2', 0x5865f2 or 'red' | 'green' | 'blue' | 'yellow' | 'purple' | 'orange' | 'grey'."""
        self.data["accent_color"] = _color(c)
        return self

    def spoiler(self, on=True):
        self.data["spoiler"] = bool(on)
        return self

    def add(self, *components):
        self.parts += _flat(components)
        return self

    def text(self, t):
        return self.add(_text_of(t))

    def separator(self, divider=True, spacing="small"):
        return self.add(Separator(divider, spacing))

    def to_dict(self):
        return {**self.data, "components": [_plain(text(p) if isinstance(p, str) else p) for p in self.parts]}


# ---- decorators for cogs (on a Cog's methods) ----

def command(name=None, description=""):
    """Marks a Cog method as a command: @yapper.command("joke", "Tell a joke"). Leave out the name to use the method's."""
    def deco(fn):
        fn.__yapper_command__ = (name or fn.__name__, description)
        return fn
    return deco


def listener(event=None):
    """Marks a Cog method as an event listener: @yapper.listener("member_join"), or @yapper.listener() on on_member_join."""
    def deco(fn):
        ev = event or fn.__name__
        fn.__yapper_listener__ = ev[3:] if ev.startswith("on_") else ev
        return fn
    return deco


def button(prefix):
    """Marks a Cog method as a button handler for custom_ids starting with `prefix`: @yapper.button("vote:")."""
    def deco(fn):
        fn.__yapper_button__ = prefix
        return fn
    return deco


class Cog:
    """A group of commands, listeners and buttons, usually one file per topic (cogs/fun.py, cogs/mod.py).

        class Fun(yapper.Cog):
            @yapper.command("joke", "Tell a joke")
            def joke(self, msg):
                msg.reply("I know a UDP joke, but you might not get it")

            @yapper.listener("member_join")
            def hello(self, ev):
                print(ev.username, "joined")

            def check(self, msg):       # optional: return False to skip this cog's commands
                return True

        bot.add_cog(Fun(bot))

    Optional hooks: cog_load(self) and cog_unload(self). `self.bot` is the bot.
    """

    name = None  # defaults to the class name

    def __init__(self, bot):
        self.bot = bot

    def check(self, msg):
        return True

    def cog_load(self):
        pass

    def cog_unload(self):
        pass


class _Owner:
    """What one cog or extension added, so it can be taken out again."""

    def __init__(self, name, label):
        self.name = name
        self.label = label
        self.commands = set()
        self.listeners = []   # (event, fn)
        self.buttons = []     # entries in bot._buttons
        self.cogs = []        # for a file: the cogs its setup added


def _backoff(n):
    """Seconds to wait before try number n (1, 2, ...): 1, 2, 4 ... up to 30, with some jitter so many bots don't sync up."""
    return min(30, 2 ** (n - 1)) * (0.75 + random.random() / 2)


class _GatewayClosed(Exception):
    """The gateway closed the socket. `.code` is the close code (0 when the connection just dropped)."""

    def __init__(self, code=0, reason=""):
        super().__init__(reason or f"gateway closed ({code})")
        self.code = code
        self.reason = reason


def _mask(data, key):
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


class _WebSocket:
    """Just enough of a WebSocket client (RFC 6455) for the gateway: text messages, ping/pong, close codes, TLS.
    Standard library only, so the SDK still needs nothing installed."""

    def __init__(self, url, timeout=15):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError(f"Not a WebSocket URL: {url}")
        tls = u.scheme == "wss"
        port = u.port or (443 if tls else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self._buf = b""
        self._frag = None   # [opcode, bytearray] of a message still arriving in pieces
        self.sock = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if tls:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            host = u.hostname if port == (443 if tls else 80) else f"{u.hostname}:{port}"
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                               f"User-Agent: yapper-py/{__version__}\r\n\r\n").encode())
            head = self._read_until(b"\r\n\r\n")
            status, _, rest = head.partition(b"\r\n")
            if b" 101 " not in status:
                raise OSError(f"gateway handshake failed: {status.decode(errors='replace')[:80]}")
            headers = {}
            for line in rest.split(b"\r\n"):
                k, _, v = line.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()
            want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if headers.get("sec-websocket-accept") != want:
                raise OSError("gateway handshake failed: bad Sec-WebSocket-Accept")
        except BaseException:
            self.sock.close()
            raise

    def _read_until(self, mark):
        while mark not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("gateway closed during the handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise OSError("gateway handshake answer too long")
        head, self._buf = self._buf.split(mark, 1)
        return head

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise _GatewayClosed(0, "connection lost")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode, payload=b""):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", n)
        key = os.urandom(4)
        self.sock.sendall(head + key + _mask(payload, key))

    def send(self, obj):
        self._frame(1, json.dumps(obj).encode())

    def readable(self, wait):
        """True when something can be read within `wait` seconds (bytes TLS already took off the wire count too)."""
        if self._buf or getattr(self.sock, "pending", lambda: 0)():
            return True
        r, _, _ = select.select([self.sock], [], [], max(0.0, wait))
        return bool(r)

    def recv(self):
        """The next text message as a string (None for a binary one). Answers pings itself; raises _GatewayClosed
        when the other side closes."""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > 16 * 1024 * 1024:
                raise OSError("gateway frame too big")
            key = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if key:
                data = _mask(data, key)
            if opcode == 8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 0
                try:
                    self._frame(8, data[:2])
                except OSError:
                    pass
                raise _GatewayClosed(code, data[2:].decode("utf-8", "replace"))
            if opcode == 9:
                self._frame(10, data)
            elif opcode in (1, 2):
                if fin:
                    return data.decode("utf-8", "replace") if opcode == 1 else None
                self._frag = [opcode, bytearray(data)]
            elif opcode == 0 and self._frag:
                self._frag[1] += data
                if fin:
                    op, whole = self._frag
                    self._frag = None
                    return bytes(whole).decode("utf-8", "replace") if op == 1 else None

    def close(self, code=1000, reason=""):
        try:
            self._frame(8, struct.pack("!H", code) + reason.encode()[:120])
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Bot:
    def __init__(self, token=None, base=None, prefixes=("/", "!"), log=True, ignore_bots=True, gateway=GATEWAY):
        """`token` defaults to the YAPPER_BOT_TOKEN environment variable (bot hosting sets it for you).
        `ignore_bots` (default True): skip messages from other bots and webhooks, so bots can't reply to each other forever.
        `gateway` (default on): where events come from over a WebSocket; used when it's on for your bot, otherwise
        the bot long-polls. Pass None (or False) to always poll."""
        token = (token or os.environ.get("YAPPER_BOT_TOKEN") or "").strip()
        if not token.startswith("yb_"):
            raise SystemExit("Pass your bot token (starts with yb_) or set YAPPER_BOT_TOKEN. Get one in Settings -> Developer.")
        self.token = token
        # YAPPER_API_URL: set by bot hosting (usable anywhere) to point at another API host.
        chosen = base or os.environ.get("YAPPER_API_URL")
        self.base = (chosen or BASE).rstrip("/")
        # Hosted bots get our host through YAPPER_API_URL: they keep the fallback too; any other chosen base doesn't.
        self._fallback = None if chosen and self.base != BASE else FALLBACK_BASE
        self._fallback_until = 0
        self.prefixes = tuple(prefixes)
        self.log = log
        # Most seconds one call spends waiting out 429s / "busy" 503s in total before it raises (three 10 s waits with
        # 50% jitter used to hold an edit for up to 45 s). Set bot.max_retry_wait to change it.
        self.max_retry_wait = 30.0
        self._cf_warned = False
        self.ignore_bots = ignore_bots
        self.gateway = gateway or None
        self._ws = None
        self.me = None
        self.last = 0
        self.running = False
        self.commands = {}     # name -> {"description", "fn", "owner", "check"}
        self.cogs = {}         # cog name -> Cog
        self.extensions = {}   # "cogs.fun" -> module
        self._listeners = {}   # event -> [fn]
        self._buttons = []     # {"prefix", "fn", "owner"}
        self._owners = {}      # cog or extension name -> _Owner
        self._loading = []     # the extension being set up right now (bot.command inside setup belongs to it)
        self._registered = False
        self._dirty = False

    # ---- registering ----

    def command(self, name, description=""):
        """Decorator: @bot.command("ping", "Is the bot alive?"). People type /ping or !ping."""
        def deco(fn):
            self.add_command(name, description, fn)
            return fn
        return deco

    def add_command(self, name, description, fn, owner=None, check=None):
        owner = owner or (self._loading[-1] if self._loading else None)
        n = str(name).lower().lstrip("/!")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", n):
            raise ValueError(f'Bad command name "{name}": letters, numbers, - and _ only')
        old = self.commands.get(n)
        if old and old["owner"] is not owner and (old["owner"] or owner):
            where = lambda o: o.label if o else "your main file"
            raise ValueError(f"Command /{n} is in both {where(old['owner'])} and {where(owner)}. Rename one of them.")
        if not old and len(self.commands) >= COMMAND_LIMIT:
            raise ValueError(f"Up to {COMMAND_LIMIT} commands per bot, /{n} is one too many")
        self.commands[n] = {"description": str(description or "")[:100], "fn": fn, "owner": owner, "check": check}
        if owner:
            owner.commands.add(n)
        self._dirty = True
        return fn

    def on(self, event, fn=None):
        """Listen for an event: bot.on("member_join", fn), or as a decorator @bot.on("member_join").
        Events: message, command, dm_message, member_join, member_leave, reaction_add, reaction_remove, voice_join,
        voice_leave, invite_create, invite_delete, interaction, test, ready, error.
        member_join also has invite_code and inviter_id when known (bots with Manage Server).
        dm_message is someone DMing the bot: a Message whose reply / say answer in the DM, with msg.dm = True."""
        def deco(f):
            self._listeners.setdefault(event, []).append(f)
            owner = self._loading[-1] if self._loading else None
            if owner:
                owner.listeners.append((event, f))
            return f
        return deco(fn) if fn else deco

    def on_dm(self, fn=None):
        """Someone DMed the bot: bot.on_dm(fn), or as a decorator @bot.on_dm. msg has id, channel_id (the DM),
        author_id, author, content, attachment / attachments, reply_to_id, created_at, is_dm (True), dm_channel_id,
        and msg.reply("hi!").
        Only DMs with this bot ever come in. Same as bot.on("dm_message")."""
        return self.on("dm_message", fn)

    def on_reaction_on_mine(self, fn=None):
        """Someone reacted to a message this bot sent (in a server channel it can see, or in its DMs), or took it off:
        bot.on_reaction_on_mine(fn), or @bot.on_reaction_on_mine. r has message_id, channel_id, server_id (None in a DM),
        user_id, username, emoji, added (True for reaction_add, False for reaction_remove) and type."""
        def deco(f):
            def mine(ev):
                me = (self.me or {}).get("id")
                if me and ev.get("message_author_id") == me:
                    return f(Data(ev, added=ev.get("type") == "reaction_add"))
                return None
            mine.__name__ = getattr(f, "__name__", "on_reaction_on_mine")
            self.on("reaction_add", mine)
            self.on("reaction_remove", mine)
            return f
        return deco(fn) if fn else deco

    def event(self, fn):
        """Decorator: the function name says the event. @bot.event def on_member_join(ev): ..."""
        name = fn.__name__
        return self.on(name[3:] if name.startswith("on_") else name, fn)

    def button(self, prefix):
        """Decorator for button clicks whose custom_id starts with `prefix`: @bot.button("vote:")."""
        def deco(fn):
            self._add_button({"prefix": prefix, "fn": fn, "label": prefix})
            return fn
        return deco

    def on_button(self, custom_id, fn=None):
        """Handle clicks on buttons with exactly this custom_id, or matching a regex (re.compile(r"^vote:")):
        bot.on_button("ping", fn), or as a decorator @bot.on_button(re.compile(r"^vote:")).
        `fn(i)` gets the click: i.custom_id, i.user_id, i.username, i.message_id, i.channel_id and
        i.reply(...) / i.ephemeral(...) / i.update(...) / i.ack(). Answer within 3 seconds; then you have 15 minutes."""
        def deco(f):
            label = custom_id.pattern if isinstance(custom_id, re.Pattern) else custom_id
            self._add_button({"match": custom_id, "fn": f, "label": label})
            return f
        return deco(fn) if fn else deco

    def _add_button(self, entry):
        owner = self._loading[-1] if self._loading else None
        entry["owner"] = owner
        self._buttons.append(entry)
        if owner:
            owner.buttons.append(entry)

    @staticmethod
    def _button_hit(b, cid):
        if "prefix" in b:
            return cid.startswith(b["prefix"])
        m = b["match"]
        return bool(m.search(cid)) if isinstance(m, re.Pattern) else m == cid

    # ---- cogs and extensions ----

    def add_cog(self, cog):
        """Add a cog: bot.add_cog(Fun(bot)). Its commands, listeners and buttons start working right away."""
        if inspect.isclass(cog):
            cog = cog(self)
        if not isinstance(cog, Cog):
            raise TypeError("add_cog needs a Cog, like bot.add_cog(Fun(bot))")
        name = cog.name or type(cog).__name__
        if name in self.cogs:
            raise ValueError(f'A cog called "{name}" is already loaded')
        if getattr(cog, "bot", None) is None:
            cog.bot = self
        ext = self._loading[-1] if self._loading else None
        owner = _Owner(name, ext.label if ext else f"cog {name}")
        self._owners[name] = owner
        self.cogs[name] = cog
        if ext:
            ext.cogs.append(name)
        try:
            seen = set()
            for klass in type(cog).__mro__:
                for attr, raw in vars(klass).items():
                    if attr in seen:
                        continue
                    seen.add(attr)
                    if not callable(raw):
                        continue
                    fn = getattr(cog, attr)
                    if hasattr(raw, "__yapper_command__"):
                        n, desc = raw.__yapper_command__
                        self.add_command(n, desc, fn, owner=owner, check=cog.check)
                    if hasattr(raw, "__yapper_listener__"):
                        ev = raw.__yapper_listener__
                        self._listeners.setdefault(ev, []).append(fn)
                        owner.listeners.append((ev, fn))
                    if hasattr(raw, "__yapper_button__"):
                        entry = {"prefix": raw.__yapper_button__, "fn": fn, "owner": owner, "label": raw.__yapper_button__}
                        self._buttons.append(entry)
                        owner.buttons.append(entry)
            cog.cog_load()
        except Exception:
            self._drop(owner)
            self.cogs.pop(name, None)
            self._owners.pop(name, None)
            raise
        self._dirty = True
        return cog

    def remove_cog(self, name):
        """Take a cog out again (runs its cog_unload). Returns the cog, or None if there was none."""
        cog = self.cogs.pop(name, None)
        owner = self._owners.pop(name, None)
        if cog is None:
            return None
        try:
            cog.cog_unload()
        except Exception as e:
            self._fail(e, owner, "cog_unload")
        self._drop(owner)
        self._dirty = True
        return cog

    def load_extension(self, name):
        """Load a cog file: bot.load_extension("cogs.fun") (or "cogs/fun.py").
        The file has `def setup(bot): bot.add_cog(Fun(bot))`. Without a setup, every Cog class in it is added."""
        name, path = _module_name(name)
        if name in self.extensions:
            raise ValueError(f'"{name}" is already loaded. To load it again: bot.reload_extension("{name}")')
        return self._load(name, path)

    def _load(self, name, path):
        if path:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None:
                raise ImportError(f"Can't load {path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            try:
                # Read the source every time (no cached .pyc), so a file edited a moment ago is what runs.
                with open(path, encoding="utf-8") as f:
                    exec(compile(f.read(), path, "exec"), mod.__dict__)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        else:
            importlib.invalidate_caches()   # files added while the bot runs
            mod = importlib.import_module(name)
        self._setup(name, mod)
        return name

    def load_extensions(self, folder="cogs"):
        """Load every .py file in a folder, A to Z. Files starting with _ are skipped. Returns the names."""
        base = folder
        if not os.path.isabs(base):
            here = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else os.getcwd()
            base = next((p for p in (os.path.join(here, folder), os.path.join(os.getcwd(), folder)) if os.path.isdir(p)), None)
            if base is None:
                raise FileNotFoundError(f"Can't find the folder {folder}")
        names = []
        for f in sorted(os.listdir(base)):
            if f.endswith(".py") and not f.startswith(("_", ".")):
                names.append(self.load_extension(os.path.join(base, f)))
        return names

    def unload_extension(self, name):
        """Unload a cog file: its cogs, commands, listeners and buttons go away, and teardown(bot) runs if it has one."""
        name, _ = _module_name(name)
        mod = self.extensions.pop(name, None)
        if mod is None:
            raise ValueError(f'"{name}" is not loaded')
        owner = self._owners.pop(name, None)
        for cog_name in (owner.cogs if owner else []):
            self.remove_cog(cog_name)
        if hasattr(mod, "teardown"):
            try:
                mod.teardown(self)
            except Exception as e:
                self._fail(e, owner, "teardown")
        self._drop(owner)
        sys.modules.pop(name, None)
        self._dirty = True

    def reload_extension(self, name):
        """Load a cog file again, fresh from disk, without restarting. If the new version breaks, the old one stays."""
        name, _ = _module_name(name)
        mod = self.extensions.get(name)
        if mod is None:
            raise ValueError(f'"{name}" is not loaded')
        path = getattr(mod, "__file__", None)
        self.unload_extension(name)
        try:
            self._load(name, path)
        except Exception:
            sys.modules[name] = mod
            self._setup(name, mod)
            raise
        return name

    # Short names, like the JS SDK's bot.load / loadCogs / unload / reload.

    def load(self, path):
        """Load a cog file: bot.load("cogs/fun.py") or bot.load("cogs.fun"). Same as load_extension. Returns its name."""
        return self.load_extension(path)

    def load_cogs(self, folder="cogs"):
        """Load every .py file in a folder, A to Z (files starting with _ are skipped). Same as load_extensions."""
        return self.load_extensions(folder)

    def unload(self, name):
        """Take a cog file or a cog out again: bot.unload("fun") (or "cogs.fun", "cogs/fun.py", or a cog's name).
        Its commands, listeners and buttons go away and cog_unload / teardown run. Returns the name."""
        kind, key = self._find_loaded(name)
        if kind == "ext":
            self.unload_extension(key)
        else:
            self.remove_cog(key)
        return key

    def reload(self, name):
        """Load a cog file again, fresh from disk, no restart: edit cogs/fun.py, then bot.reload("fun").
        If the new version breaks, the old one stays and the error is raised. A cog from a file reloads its file."""
        kind, key = self._find_loaded(name)
        if kind == "cog":
            ext = next((n for n, o in self._owners.items() if n in self.extensions and key in o.cogs), None)
            if ext is None:
                raise ValueError(f'"{key}" wasn\'t loaded from a file, so it can\'t be reloaded')
            key = ext
        return self.reload_extension(key)

    def _find_loaded(self, name):
        """("ext", "cogs.fun") or ("cog", "Fun") for a name as people write it: "fun", "cogs.fun", "cogs/fun.py", "Fun"."""
        s = str(name)
        short = re.sub(r"\.py$", "", s.replace("\\", "/").rsplit("/", 1)[-1])
        dotted = re.sub(r"\.py$", "", s).replace("\\", "/").replace("/", ".")
        for key in (s, dotted):
            if key in self.extensions:
                return "ext", key
        if s in self.cogs:
            return "cog", s
        for key in self.extensions:
            if key.rsplit(".", 1)[-1] == short:
                return "ext", key
        for key in self.cogs:
            if key.lower() == short.lower():
                return "cog", key
        loaded = ", ".join(list(self.extensions) + [c for c in self.cogs if not any(c in o.cogs for o in self._owners.values())])
        raise ValueError(f'No cog called "{s}" is loaded' + (f" (loaded: {loaded})" if loaded else ""))

    def _setup(self, name, mod):
        owner = _Owner(name, (getattr(mod, "__file__", None) and os.path.relpath(mod.__file__).replace(os.sep, "/")) or name)
        self._owners[name] = owner
        self.extensions[name] = mod
        self._loading.append(owner)
        try:
            if hasattr(mod, "setup"):
                mod.setup(self)
            else:
                classes = [c for c in vars(mod).values() if inspect.isclass(c) and issubclass(c, Cog) and c is not Cog and c.__module__ == mod.__name__]
                if not classes:
                    raise ValueError(f"{owner.label} has no setup(bot) function and no Cog class. Add one of them.")
                for c in classes:
                    self.add_cog(c(self))
        except BaseException:
            for cog_name in owner.cogs:   # undo what setup managed to add before it broke
                self.cogs.pop(cog_name, None)
                self._drop(self._owners.pop(cog_name, None))
            self._drop(owner)
            self._owners.pop(name, None)
            self.extensions.pop(name, None)
            sys.modules.pop(name, None)
            raise
        finally:
            self._loading.pop()
        self._dirty = True

    def _drop(self, owner):
        if owner is None:
            return
        for n in owner.commands:
            if n in self.commands and self.commands[n]["owner"] is owner:
                del self.commands[n]
        for ev, fn in owner.listeners:
            if fn in self._listeners.get(ev, []):
                self._listeners[ev].remove(fn)
        self._buttons = [b for b in self._buttons if b["owner"] is not owner]
        owner.commands.clear()
        owner.listeners.clear()
        owner.buttons.clear()

    # ---- HTTP ----

    def api(self, method, path, body=None, timeout=40, retries=3, _waited=0.0):
        """Raw API call: bot.api("GET", "/servers"). A 429 with Retry-After, or Yapin’s own 503 "busy",
        is waited out and retried, up to 3 times and max_retry_wait seconds in total (nothing was done, so that's
        safe). Other errors raise YapperError."""
        # A _Form (send with files) is already multipart bytes with its own content type.
        data = None if body is None else body.data if isinstance(body, _Form) else json.dumps(body).encode()
        ctype = body.content_type if isinstance(body, _Form) else "application/json"
        on_fallback = bool(self._fallback) and time.time() < self._fallback_until
        base = self._fallback if on_fallback else self.base
        req = urllib.request.Request(base + path, method=method, data=data, headers={
            "Authorization": f"Bot {self.token}", "Content-Type": ctype, "User-Agent": f"yapper-py/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (502, 504) and self._fallback and not on_fallback:
                self._use_fallback(f"answered {e.code}")
                if method == "GET" and retries > 0:
                    return self.api(method, path, body, timeout, retries - 1, _waited)
            try:
                err = json.loads(e.read() or b"{}")
            except Exception:
                err = {}
            if not isinstance(err, dict):
                err = {}
            if e.code == 429 and "json" not in (e.headers.get("Content-Type") or "") and not self._cf_warned:
                # Not our JSON: Cloudflare in front of us limited this IP address, not the bot.
                self._cf_warned = True
                print("[yapper] Cloudflare rate limited this machine's IP address (not a Yapin' bot limit). If several bots share this IP, tell us in Yapin' HQ.")
            busy = e.code == 503 and "retry_after" in err
            wait = 0
            if e.code == 429 or busy:
                try:
                    wait = float(e.headers.get("Retry-After") or err.get("retry_after") or 0)
                except (TypeError, ValueError):
                    wait = 0
            pause = wait * (1 + random.random() * 0.2)   # up to 20% jitter: bots don't all come back at once
            if 0 < wait <= 60 and retries > 0 and _waited + pause <= self.max_retry_wait:
                if self.log:
                    print(f"[yapper] {'busy' if busy else 'rate limited'}, retrying {method} {path} in {wait:g}s")
                time.sleep(pause)
                return self.api(method, path, body, timeout, retries - 1, _waited + pause)
            raise YapperError(err.get("error") or f"{method} {path} failed ({e.code})", e.code, wait or None) from None
        except urllib.error.URLError:
            # Our API host unreachable: the website's address for 10 minutes. A GET is asked again there at once;
            # anything else isn't (it may have gone through), the next call goes there.
            if not self._fallback or on_fallback:
                raise
            self._use_fallback("unreachable")
            if method == "GET" and retries > 0:
                return self.api(method, path, body, timeout, retries - 1, _waited)
            raise

    def _use_fallback(self, why):
        self._fallback_until = time.time() + 600
        if self.log:
            print(f"[yapper] {self.base} {why}, using {self._fallback} for 10 minutes")

    # ---- messages and more ----

    # API reference: https://www.yapin.gg/developers/api/messages#post-channels-id-messages
    def send(self, channel_id, content="", embeds=None, components=None, reply_to=None, files=None):
        """Post a message. Embeds and buttons are plain dicts (see /developers). Returns the message.
        files (in testing, flag bot_uploads): up to 4, each a file path, an open binary file, or ("card.png", bytes).
        Pictures, videos, sounds, TXT, PDF, ZIP, 10 MB each, 12 MB in all. Every file becomes its own message (the first one has
        the text) and is checked before it shows; content can be "" then.
        bot.send(ch, "Welcome!", files=[("welcome.png", png_bytes)])"""
        body = {"content": str(content or "")}
        if reply_to:
            body["reply_to"] = reply_to
        if embeds:
            body["embeds"] = _listed(embeds)
        if components:
            body["components"] = _listed(components)
        if files:
            body = _form(body, files)
        return Data(self.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {})

    # API reference: https://www.yapin.gg/developers/api/messages#post-channels-id-messages
    def send_layout(self, channel_id, components, reply_to=None):
        """Post a Components V2 message: the layout is the whole message (no content or embeds).
        bot.send_layout(ch, [container(text("# Hi"), section("Ready?", accessory=btn("Go", "go")), accent="blue")])"""
        body = {"flags": IS_COMPONENTS_V2, "components": _listed(components)}
        if reply_to:
            body["reply_to"] = reply_to
        return Data(self.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {})

    # API reference: https://www.yapin.gg/developers/api/messages#patch-channels-id-messages-mid
    def edit_layout(self, channel_id, message_id, components):
        """Swap the layout of one of the bot's Components V2 messages."""
        body = {"flags": IS_COMPONENTS_V2, "components": _listed(components)}
        return Data(self.api("PATCH", f"/channels/{channel_id}/messages/{message_id}", body).get("message") or {})

    # API reference: https://www.yapin.gg/developers/api/messages#patch-channels-id-messages-mid
    def edit(self, channel_id, message_id, content=None, embeds=None, components=None):
        """Edit one of the bot's own messages. Left out (None) = kept; embeds=[] / components=[] removes them."""
        body = {}
        if content is not None:
            body["content"] = str(content)
        if embeds is not None:
            body["embeds"] = _listed(embeds)
        if components is not None:
            body["components"] = _listed(components)
        return Data(self.api("PATCH", f"/channels/{channel_id}/messages/{message_id}", body).get("message") or {})

    # API reference: https://www.yapin.gg/developers/api/messages#delete-channels-id-messages-mid
    def delete(self, channel_id, message_id):
        """Delete a message: the bot's own, or anyone's if its roles have Manage messages."""
        return self.api("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def send_embed(self, channel_id, embed, content=""):
        """Send just a card (or a list of up to 5): bot.send_embed(ch, yapper.Embed().title("Hi"))."""
        return self.send(channel_id, content, embeds=embed)

    # API reference: https://www.yapin.gg/developers/api/messages#get-channels-id-messages-mid
    def message(self, channel_id, message_id):
        """One message by id, with its `author` profile (None if the account is gone)."""
        out = self.api("GET", f"/channels/{channel_id}/messages/{message_id}")
        if not out.get("message"):
            return Data(out)
        return Data(out["message"], author=Data(out["author"]) if out.get("author") else None)

    # API reference: https://www.yapin.gg/developers/api/messages#get-channels-id-messages
    def messages_after(self, channel_id, after, limit=50):
        """Messages after a time (a datetime, unix seconds or an ISO string), oldest first, each with its `author`."""
        return self._with_authors(self.api("GET", f"/channels/{channel_id}/messages?limit={limit}&after={urllib.parse.quote(_iso(after))}"))

    # API reference: https://www.yapin.gg/developers/api/messages#post-channels-id-messages-bulk-delete
    def bulk_delete(self, channel_id, message_ids):
        """Delete up to 100 messages at once (needs Manage messages for other people's). Returns {deleted, skipped}."""
        return self.api("POST", f"/channels/{channel_id}/messages/bulk-delete", {"ids": list(message_ids)})

    # API reference: https://www.yapin.gg/developers/api/messages#put-channels-id-messages-mid-pin
    def pin(self, channel_id, message_id):
        return self.api("PUT", f"/channels/{channel_id}/messages/{message_id}/pin")

    # API reference: https://www.yapin.gg/developers/api/messages#delete-channels-id-messages-mid-pin
    def unpin(self, channel_id, message_id):
        return self.api("DELETE", f"/channels/{channel_id}/messages/{message_id}/pin")

    # API reference: https://www.yapin.gg/developers/api/messages#put-channels-id-messages-mid-reactions-emoji
    def react(self, channel_id, message_id, emoji):
        """Add the bot's reaction: "🔥" or a server emoji "<:name:id>"."""
        return self.api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote(emoji, safe='')}")

    # API reference: https://www.yapin.gg/developers/api/messages#delete-channels-id-messages-mid-reactions-emoji
    def unreact(self, channel_id, message_id, emoji):
        """Take the bot's reaction off again."""
        return self.api("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote(emoji, safe='')}")

    # API reference: https://www.yapin.gg/developers/api/messages#get-channels-id-messages-mid-reactions
    def reactions(self, channel_id, message_id):
        """The reactions on a message: [{emoji, count, user_ids, ...}]."""
        return [Data(r) for r in self.api("GET", f"/channels/{channel_id}/messages/{message_id}/reactions")["reactions"]]

    # API reference: https://www.yapin.gg/developers/api/interactions#post-interactions-id-token-callback
    def respond(self, interaction, kind, content=None, embeds=None, components=None, layout=None):
        """Answer a button click: kind is reply, ephemeral, update or ack. layout=[...]: a Components V2 layout."""
        body = {"type": kind}
        if content is not None:
            body["content"] = str(content)
        if embeds is not None:
            body["embeds"] = _listed(embeds)
        if components is not None:
            body["components"] = _listed(components)
        if layout is not None:
            body["flags"] = IS_COMPONENTS_V2
            body["components"] = _listed(layout)
        return self.api("POST", f"/interactions/{interaction['id']}/{interaction['token']}/callback", body)

    # API reference: https://www.yapin.gg/developers/api/servers#get-servers
    def servers(self):
        """Servers the bot is in (with member_count, and online_count where live counts are on)."""
        return [Data(s) for s in self.api("GET", "/servers")["servers"]]

    # API reference: https://www.yapin.gg/developers/api/servers#get-servers-id
    def server(self, server_id):
        """Server details; online_count (people online now) sits next to member_count where live counts are on."""
        return Data(self.api("GET", f"/servers/{server_id}")["server"])

    # API reference: https://www.yapin.gg/developers/api/servers#get-servers-id-counts
    def member_counts(self, server_id):
        """Live counts for a "Members: N / Online: N" counter: {member_count, online_count}. Online is refreshed once
        a minute, so ask every few minutes (member_join / member_leave carry member_count in between)."""
        return Data(self.api("GET", f"/servers/{server_id}/counts"))

    # API reference: https://www.yapin.gg/developers/api/channels#get-channels
    # API reference: https://www.yapin.gg/developers/api/channels#get-servers-id-channels
    def channels(self, server_id=None, type=None):
        """Channels the bot can see, in sidebar order. channels(server_id, type="text") for text only."""
        q = f"?type={type}" if type else ""
        path = f"/servers/{server_id}/channels{q}" if server_id else f"/channels{q}"
        return [Data(c) for c in self.api("GET", path)["channels"]]

    # API reference: https://www.yapin.gg/developers/api/channels#get-channels-id
    def channel(self, channel_id):
        try:
            return Data(self.api("GET", f"/channels/{channel_id}")["channel"])
        except YapperError:
            return None

    # API reference: https://www.yapin.gg/developers/api/servers#get-servers-id-members
    def members(self, server_id, after=None):
        return [Data(m) for m in self.api("GET", f"/servers/{server_id}/members?limit=200" + (f"&after={after}" if after else ""))["members"]]

    # API reference: https://www.yapin.gg/developers/api/servers#get-servers-id-members-uid
    def member(self, server_id, user_id):
        return Data(self.api("GET", f"/servers/{server_id}/members/{user_id}")["member"])

    # API reference: https://www.yapin.gg/developers/api/roles#get-servers-id-roles
    def roles(self, server_id):
        return [Data(r) for r in self.api("GET", f"/servers/{server_id}/roles")["roles"]]

    # API reference: https://www.yapin.gg/developers/api/users#get-users-id
    def user(self, user_id):
        return Data(self.api("GET", f"/users/{user_id}")["user"])

    # API reference: https://www.yapin.gg/developers/api/messages#get-channels-id-messages
    def messages(self, channel_id, limit=50):
        """Recent messages in a channel, oldest first. Each has its `author` profile ({id, username, display_name, ...})."""
        return self._with_authors(self.api("GET", f"/channels/{channel_id}/messages?limit={limit}"))

    @staticmethod
    def _with_authors(out):
        by = {a["id"]: a for a in out.get("authors") or []}
        return [Data(m, author=Data(by[m["author_id"]]) if m.get("author_id") in by else None) for m in out.get("messages") or []]

    # API reference: https://www.yapin.gg/developers/api/roles#put-servers-id-members-uid-roles-rid
    def add_role(self, server_id, user_id, role_id):
        return self.api("PUT", f"/servers/{server_id}/members/{user_id}/roles/{role_id}")

    # API reference: https://www.yapin.gg/developers/api/roles#delete-servers-id-members-uid-roles-rid
    def remove_role(self, server_id, user_id, role_id):
        return self.api("DELETE", f"/servers/{server_id}/members/{user_id}/roles/{role_id}")

    # ---- managing roles (Manage Roles; only roles below the bot's highest, only permissions the bot has itself) ----

    # API reference: https://www.yapin.gg/developers/api/roles#post-servers-id-roles
    def create_role(self, server_id, name, color=None, mentionable=False, permissions=None):
        """Make a role, just below the bot's highest: create_role(server, "Mods", color="#5865f2",
        permissions={"kick_members": True}). Returns the role. 30 role and permission changes a minute per bot."""
        body = {"name": name, "mentionable": bool(mentionable)}
        if color is not None:
            body["color"] = color
        if permissions is not None:
            body["permissions"] = permissions
        return Data(self.api("POST", f"/servers/{server_id}/roles", body).get("role") or {})

    # API reference: https://www.yapin.gg/developers/api/roles#patch-servers-id-roles-rid
    def edit_role(self, server_id, role_id, name=None, color=None, mentionable=None, permissions=None, position=None):
        """Change a role; what you leave out stays (permissions: only the keys you pass change). color="" removes the colour."""
        body = {k: v for k, v in (("name", name), ("color", color), ("mentionable", mentionable),
                                  ("permissions", permissions), ("position", position)) if v is not None}
        return Data(self.api("PATCH", f"/servers/{server_id}/roles/{role_id}", body).get("role") or {})

    # API reference: https://www.yapin.gg/developers/api/roles#delete-servers-id-roles-rid
    def delete_role(self, server_id, role_id):
        return self.api("DELETE", f"/servers/{server_id}/roles/{role_id}")

    # ---- custom emojis (Manage Emoji; 30 changes a minute per bot; in testing, flag bot_emojis) ----

    # API reference: https://www.yapin.gg/developers/api/emojis#get-servers-id-emojis
    def emojis(self, server_id):
        """The server's custom emojis: {"emojis": [{id, name, animated, url, ...}], "limit": emoji slots}."""
        return Data(self.api("GET", f"/servers/{server_id}/emojis"))

    # API reference: https://www.yapin.gg/developers/api/emojis#post-servers-id-emojis
    def create_emoji(self, server_id, name, image):
        """Add a custom emoji: create_emoji(server, "party", "party.gif"). image: a file path, bytes or an open binary
        file. PNG, GIF or WebP up to 1 MB. Checked like every new emoji. Returns the emoji, or None when the check
        wasn't sure and it waits for a moderator."""
        if isinstance(image, str):
            with open(image, "rb") as f:
                data = f.read()
        elif hasattr(image, "read"):
            data = image.read()
        else:
            data = bytes(image)
        out = self.api("POST", f"/servers/{server_id}/emojis", {"name": name, "image": base64.b64encode(data).decode("ascii")})
        return Data(out["emoji"]) if out.get("emoji") else None

    # API reference: https://www.yapin.gg/developers/api/emojis#patch-servers-id-emojis-eid
    def edit_emoji(self, server_id, emoji_id, name):
        """Rename a custom emoji. Returns the emoji."""
        return Data(self.api("PATCH", f"/servers/{server_id}/emojis/{emoji_id}", {"name": name}).get("emoji") or {})

    # API reference: https://www.yapin.gg/developers/api/emojis#delete-servers-id-emojis-eid
    def delete_emoji(self, server_id, emoji_id):
        return self.api("DELETE", f"/servers/{server_id}/emojis/{emoji_id}")

    # API reference: https://www.yapin.gg/developers/api/roles#patch-servers-id-roles-everyone
    def set_everyone_perms(self, server_id, permissions):
        """@everyone's permissions (the basic ones): set_everyone_perms(server, {"send_messages": False})."""
        return self.api("PATCH", f"/servers/{server_id}/roles/everyone", {"permissions": permissions})

    # API reference: https://www.yapin.gg/developers/api/channels#get-channels-id-permissions
    def channel_permissions(self, channel_id):
        """A channel's permission overrides: [{role_id (None = @everyone), allow, deny}]."""
        return [Data(o) for o in self.api("GET", f"/channels/{channel_id}/permissions")["overrides"]]

    # API reference: https://www.yapin.gg/developers/api/channels#put-channels-id-permissions-role
    def set_channel_permissions(self, channel_id, role_id, allow=(), deny=()):
        """Allow or deny permissions for a role (or "everyone") in one channel:
        set_channel_permissions(ch, role_id, allow=["send_messages"], deny=["view_channel"]). Empty lists remove it."""
        return self.api("PUT", f"/channels/{channel_id}/permissions/{role_id}", {"allow": list(allow), "deny": list(deny)})

    # API reference: https://www.yapin.gg/developers/api/channels#delete-channels-id-permissions-role
    def clear_channel_permissions(self, channel_id, role_id):
        return self.api("DELETE", f"/channels/{channel_id}/permissions/{role_id}")

    # API reference: https://www.yapin.gg/developers/api/channels#put-servers-id-categories-cid-permissions-role
    def set_category_permissions(self, server_id, category_id, role_id, allow=(), deny=()):
        """The same for a whole category (its channels follow it unless they have their own)."""
        return self.api("PUT", f"/servers/{server_id}/categories/{category_id}/permissions/{role_id}",
                        {"allow": list(allow), "deny": list(deny)})

    # API reference: https://www.yapin.gg/developers/api/channels#delete-servers-id-categories-cid-permissions-role
    def clear_category_permissions(self, server_id, category_id, role_id):
        return self.api("DELETE", f"/servers/{server_id}/categories/{category_id}/permissions/{role_id}")

    # API reference: https://www.yapin.gg/developers/api/moderation#post-servers-id-kick
    def kick(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/kick", {"user_id": user_id})

    # API reference: https://www.yapin.gg/developers/api/moderation#post-servers-id-bans
    def ban(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/bans", {"user_id": user_id})

    # API reference: https://www.yapin.gg/developers/api/moderation#delete-servers-id-bans-uid
    def unban(self, server_id, user_id):
        return self.api("DELETE", f"/servers/{server_id}/bans/{user_id}")

    # API reference: https://www.yapin.gg/developers/api/moderation#patch-servers-id-members-uid
    def set_nickname(self, server_id, user_id, nickname):
        """Set someone's nickname ("" clears it)."""
        return self.api("PATCH", f"/servers/{server_id}/members/{user_id}", {"nickname": nickname})

    # API reference: https://www.yapin.gg/developers/api/moderation#get-servers-id-bans
    def bans(self, server_id):
        """Who is banned: [{user_id, banned_by, created_at, user}] (needs Ban members)."""
        return [Data(b) for b in self.api("GET", f"/servers/{server_id}/bans")["bans"]]

    # API reference: https://www.yapin.gg/developers/api/moderation#get-servers-id-invites
    def invites(self, server_id):
        """The server's invites: [{code, url, inviter_id, uses, max_uses, expires_at, vanity, created_at}].
        Needs Manage Server. Yapin’ invites don't run out: max_uses is 0 (no limit) and expires_at None.
        member_join events then also say invite_code and inviter_id when known (invite trackers)."""
        return [Data(i) for i in self.api("GET", f"/servers/{server_id}/invites")["invites"]]

    # API reference: https://www.yapin.gg/developers/api/moderation#post-servers-id-timeouts
    def timeout(self, server_id, user_id, minutes, reason=""):
        """Time someone out for `minutes` (0 clears it)."""
        return self.api("POST", f"/servers/{server_id}/timeouts", {"user_id": user_id, "minutes": minutes, "reason": reason})

    # ---- voice (Move Members for moves; 30 moves and 60 lookups a minute per bot) ----
    # API reference: https://www.yapin.gg/developers/api/voice#get-servers-id-voice
    def voice(self, server_id):
        """Who is in voice: [{channel_id, user_ids}] for every voice channel the bot can see (empty ones too)."""
        return self.api("GET", f"/servers/{server_id}/voice")["voice"]

    # API reference: https://www.yapin.gg/developers/api/voice#post-servers-id-voice-move
    def move_member(self, server_id, user_id, channel_id):
        return self.api("POST", f"/servers/{server_id}/voice/move", {"user_id": user_id, "channel_id": channel_id})

    # API reference: https://www.yapin.gg/developers/api/voice#post-servers-id-voice-disconnect
    def disconnect_member(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/voice/disconnect", {"user_id": user_id})

    # ---- the bot in voice: music bots (10 joins and leaves a minute; one voice channel per server) ----
    # API reference: https://www.yapin.gg/developers/api/voice#post-servers-id-voice-join
    def join_voice(self, server_id, channel_id):
        """A ticket for a voice channel: {url, token, e2ee, voice_muted, channel_id, server_id}. Connect with any
        LiveKit SDK within a minute. The bot sends one audio track (as a mic) and hears nobody. Joining another
        channel of the same server drops it from the first. Most bots want connect_voice, which does all of it."""
        return self.api("POST", f"/servers/{server_id}/voice/join", {"channel_id": channel_id})

    # API reference: https://www.yapin.gg/developers/api/voice#post-servers-id-voice-leave
    def leave_voice(self, server_id):
        """Take the bot out of voice in that server."""
        return self.api("POST", f"/servers/{server_id}/voice/leave")

    async def connect_voice(self, server_id, channel_id, sample_rate=48000, channels=2):
        """Join a voice channel and get an audio source to play into. Needs `pip install livekit cryptography`
        (only for this; the rest of this file stays standard library). Returns a VoiceCall: .room, .source (an
        rtc.AudioSource: await source.capture_frame(rtc.AudioFrame(...))) and `await call.leave()`.
        Calls on Yapin’ are end-to-end encrypted: this hands the bot's media key to everyone in the call the way the
        app does (sealed to each person, the server never sees it) and encrypts the audio with it."""
        import asyncio
        from livekit import rtc  # noqa: deferred so the library works without it
        t = await asyncio.to_thread(self.join_voice, server_id, channel_id)
        media_key = os.urandom(32) if t.get("e2ee") else None
        room = rtc.Room()
        opts = rtc.RoomOptions(auto_subscribe=False)
        if media_key:
            # Shared key mode encrypts our frames with media_key; HKDF (1) is how the app's browsers turn it into the frame key.
            # `e2ee`, not `encryption` (which livekit marks as the new name): `encryption` also encrypts data messages,
            # so the key handoff went out sealed under the key it carries and the app never got it (silent bot).
            opts.e2ee = rtc.E2EEOptions(key_provider_options=rtc.KeyProviderOptions(
                shared_key=media_key, ratchet_window_size=0, key_ring_size=16, key_derivation_function=1))
        await room.connect(t["url"], t["token"], opts)
        keys = await _hand_out_voice_key(room, media_key) if media_key else None
        source = rtc.AudioSource(sample_rate, channels)
        track = rtc.LocalAudioTrack.create_audio_track("audio", source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False, red=False))
        return VoiceCall(room, source, track, bool(media_key), keys)

    # ---- channels (Manage channels) ----

    # API reference: https://www.yapin.gg/developers/api/channels#get-servers-id-channels
    def categories(self, server_id):
        """Channel categories of a server, in order: [{id, name, position}]."""
        return [Data(c) for c in self.api("GET", f"/servers/{server_id}/channels").get("categories") or []]

    # ---- webhooks (flag bot_webhooks; the bot needs Manage Webhooks in that channel) ----
    # API reference: https://www.yapin.gg/developers/api/webhooks#post-channels-id-webhooks
    def create_webhook(self, channel_id, name):
        """Make a webhook in a text channel and post through it with a name and picture per message (PluralKit style):
        hook = bot.create_webhook(ch, "Proxy"); hook.send("hi!", username="Alice", avatar_url="https://i.imgur.com/abc.png").
        The message shows a BOT tag. Up to 10 webhooks a channel, 10 makes and deletes a minute."""
        return self.wrap_webhook(self.api("POST", f"/channels/{channel_id}/webhooks", {"name": name})["webhook"])

    # API reference: https://www.yapin.gg/developers/api/webhooks#get-channels-id-webhooks
    def webhooks(self, channel_id):
        """Every webhook in a channel (people's too, without tokens)."""
        return [self.wrap_webhook(w) for w in self.api("GET", f"/channels/{channel_id}/webhooks")["webhooks"]]

    # API reference: https://www.yapin.gg/developers/api/webhooks#get-webhooks
    def my_webhooks(self):
        """The bot's own webhooks in every server, newest first: find them again after a restart."""
        return [self.wrap_webhook(w) for w in self.api("GET", "/webhooks")["webhooks"]]

    # API reference: https://www.yapin.gg/developers/api/webhooks#delete-webhooks-id
    def delete_webhook(self, webhook_id):
        """Delete a webhook (its old messages stay)."""
        return self.api("DELETE", f"/webhooks/{webhook_id}")

    # API reference: https://www.yapin.gg/developers/api/webhooks#post-webhooks-id
    def send_webhook(self, webhook_id, content="", username=None, avatar_url=None, embeds=None, components=None, allowed_mentions=None):
        """Post through one of the bot's own webhooks. avatar_url: an https link to Yapin’ uploads or a known image host.
        components: link buttons only. Limits: 5 per 2 seconds per webhook, 30 a minute per channel, plus the bot's own."""
        body = {"content": str(content or "")}
        if username:
            body["username"] = username
        if avatar_url:
            body["avatar_url"] = avatar_url
        if embeds:
            body["embeds"] = _listed(embeds)
        if components:
            body["components"] = _listed(components)
        if allowed_mentions is not None:
            body["allowed_mentions"] = allowed_mentions
        return self.api("POST", f"/webhooks/{webhook_id}", body)["message"]

    def wrap_webhook(self, data):
        """A webhook's dict -> a Webhook with send / delete."""
        return Webhook.of(data, self)

    # API reference: https://www.yapin.gg/developers/api/channels#post-servers-id-channels
    def create_channel(self, server_id, name, kind="text", category_id=None):
        """Make a channel: kind "text" or "voice", optionally in a category. Returns the channel."""
        body = {"name": name, "kind": kind}
        if category_id:
            body["category_id"] = category_id
        return Data(self.api("POST", f"/servers/{server_id}/channels", body).get("channel") or {})

    # ---- events (in testing) ----

    # API reference: https://www.yapin.gg/developers/api/scheduled-events#get-servers-id-events
    def events(self, server_id):
        """Upcoming and live events of a server."""
        return [Data(e) for e in self.api("GET", f"/servers/{server_id}/events").get("events") or []]

    # API reference: https://www.yapin.gg/developers/api/scheduled-events#post-servers-id-events
    def create_event(self, server_id, title, starts_at, ends_at=None, channel_id=None, location=None,
                     announce_channel_id=None, repeat="none", tz="UTC", description=""):
        """Make an event (Manage Events): a voice channel_id or a location. Times are datetimes or ISO strings."""
        iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else d
        body = {"title": title, "starts_at": iso(starts_at), "ends_at": iso(ends_at), "channel_id": channel_id,
                "location": location, "announce_channel_id": announce_channel_id, "repeat": repeat, "tz": tz,
                "description": description}
        return Data(self.api("POST", f"/servers/{server_id}/events", body).get("event") or {})

    # API reference: https://www.yapin.gg/developers/api/channels#patch-channels-id
    def edit_channel(self, channel_id, name=None, category_id=None, private=None, role_ids=None, read_only=None,
                     post_role_ids=None, slowmode=None):
        """Change a channel; what you leave out stays. Returns the channel."""
        body = {k: v for k, v in (("name", name), ("category_id", category_id), ("private", private), ("role_ids", role_ids),
                                  ("read_only", read_only), ("post_role_ids", post_role_ids), ("slowmode", slowmode)) if v is not None}
        return Data(self.api("PATCH", f"/channels/{channel_id}", body).get("channel") or {})

    # API reference: https://www.yapin.gg/developers/api/channels#delete-channels-id
    def delete_channel(self, channel_id):
        return self.api("DELETE", f"/channels/{channel_id}")

    # API reference: https://www.yapin.gg/developers/api/channels#post-servers-id-categories
    def create_category(self, server_id, name):
        """Make a channel category. Returns {id, name}."""
        return Data(self.api("POST", f"/servers/{server_id}/categories", {"name": name}).get("category") or {})

    # API reference: https://www.yapin.gg/developers/api/channels#delete-servers-id-categories-cid
    def delete_category(self, server_id, category_id):
        return self.api("DELETE", f"/servers/{server_id}/categories/{category_id}")

    # ---- DMs ----

    # API reference: https://www.yapin.gg/developers/api/users#post-users-id-dm
    def dm(self, user_id):
        """Open a DM with someone (they must allow DMs from the bot). Returns the channel id; then bot.send(channel_id, ...)."""
        return self.api("POST", f"/users/{user_id}/dm")["channel_id"]

    def dm_send(self, user_id, content="", embeds=None, components=None, files=None):
        """Open a DM and send one message in it (files=[...] like bot.send). Returns the message."""
        return self.send(self.dm(user_id), content, embeds=embeds, components=components, files=files)

    # API reference: https://www.yapin.gg/developers/api/bot#put-me-status
    def set_status(self, status="online", activity=None):
        return self.api("PUT", "/me/status", {"status": status, "activity": activity})

    # API reference: https://www.yapin.gg/developers/api/bot#patch-me
    def set_profile(self, display_name=None, bio=None):
        """Change the bot's display name and/or bio: set_profile(display_name="Dice Bot", bio="Rolls dice")."""
        return self.api("PATCH", "/me", {k: v for k, v in (("display_name", display_name), ("bio", bio)) if v is not None})

    # API reference: https://www.yapin.gg/developers/api/bot#put-me-avatar
    def set_avatar(self, image, type=None):
        """Set the bot's profile picture: a file path, bytes or an open binary file. PNG, JPG, GIF or WebP, up to 8 MB.
        `type` (like "image/png") is needed only for bytes without a file name."""
        return self._upload("/me/avatar", image, type)

    # API reference: https://www.yapin.gg/developers/api/bot#put-me-banner
    def set_banner(self, image, type=None):
        """The bot's banner, same inputs as set_avatar."""
        return self._upload("/me/banner", image, type)

    def _upload(self, path, image, type=None):
        name = image if isinstance(image, str) else getattr(image, "name", "")
        if isinstance(image, str):
            with open(image, "rb") as f:
                data = f.read()
        elif hasattr(image, "read"):
            data = image.read()
        else:
            data = bytes(image)
        ext = str(name).rsplit(".", 1)[-1].lower() if "." in str(name) else ""
        type = type or {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(ext)
        if not type:
            raise ValueError('Pass the image type too, like type="image/png"')
        base = self._fallback if self._fallback and time.time() < self._fallback_until else self.base
        req = urllib.request.Request(base + path, method="PUT", data=data, headers={
            "Authorization": f"Bot {self.token}", "Content-Type": type, "User-Agent": f"yapper-py/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except Exception:
                err = {}
            raise YapperError((err.get("error") if isinstance(err, dict) else None) or f"upload failed ({e.code})", e.code) from None

    # ---- events ----

    def _fail(self, e, owner=None, where=None):
        if owner is not None and getattr(e, "cog", None) is None:
            try:
                e.cog, e.where = owner.name, where
            except Exception:
                pass
        if self._listeners.get("error"):
            for fn in list(self._listeners["error"]):
                try:
                    fn(e)
                except Exception as e2:
                    print("[yapper] error listener failed:", e2)
        elif self.log:
            tag = f"[yapper] [{e.cog}{' ' + e.where if getattr(e, 'where', None) else ''}]" if getattr(e, "cog", None) else "[yapper]"
            print(tag, f"{type(e).__name__}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__, limit=-3)

    def _emit(self, event, payload):
        for fn in list(self._listeners.get(event, [])):
            try:
                fn(payload)
            except Exception as e:
                self._fail(e, self._owner_of(fn), event)

    def emit(self, event, payload):
        """Run the listeners of an event yourself: bot.emit("my_event", data). Errors go to the "error" listeners."""
        self._emit(event, payload)

    def fail(self, e):
        """Report an error the way the bot does: to the "error" listeners, or the log when there are none."""
        self._fail(e)

    def wrap(self, data):
        """A message event's dict -> a Message with reply / say / author_mention (handle() does it for you)."""
        return Message.of(data, self)

    def wrap_interaction(self, data):
        """A button click's dict -> an Interaction with reply / ephemeral / update / ack / user_mention."""
        return Interaction.of(data, self)

    def _owner_of(self, fn):
        for o in self._owners.values():
            if any(f is fn for _, f in o.listeners) or any(b["fn"] is fn for b in o.buttons):
                return o
        return None

    def handle(self, ev):
        """Handle one event ({"id", "type", "data"}). run() calls this for you."""
        data = ev.get("data") or {}
        kind = ev.get("type")
        if kind == "message":
            if self.ignore_bots and data.get("author_bot"):
                return
            msg = self.wrap(data)
            name, args = data.get("command"), data.get("args")
            # "/fish @OtherBot": a command picked for another bot (command_for) isn't ours to answer, even if we have /fish.
            cf = data.get("command_for")
            for_other = isinstance(cf, list) and (self.me or {}).get("id") not in cf
            if not name and not for_other:
                # A prefix can be longer than one character ("yap!"); the longest one that fits goes first,
                # so "!!" wins over "!".
                content = data.get("content") or ""
                for p in sorted(self.prefixes, key=len, reverse=True):
                    if not p or not content.startswith(p):
                        continue
                    m = re.match(r"^([a-z0-9_-]{1,32})(?:\s+([\s\S]*))?$", content[len(p):], re.I)
                    if m and m.group(1).lower() in self.commands:
                        name, args = m.group(1).lower(), (m.group(2) or "").strip()
                        break
            c = self.commands.get(name) if name else None
            if c:
                msg.command, msg.args = name, args or ""
                msg.argv = msg.args.split() if msg.args else []
                ok = True
                if c["check"]:
                    try:
                        ok = c["check"](msg) is not False
                    except Exception as e:
                        ok = False
                        self._fail(e, c["owner"], "check")
                if ok:
                    try:
                        c["fn"](msg)
                    except Exception as e:
                        self._fail(e, c["owner"], "/" + name)
                    self._emit("command", msg)
            self._emit("message", msg)
        elif kind == "interaction":
            i = self.wrap_interaction(data)
            cid = str(data.get("custom_id") or "")
            for b in list(self._buttons):
                if self._button_hit(b, cid):
                    try:
                        b["fn"](i)
                    except Exception as e:
                        self._fail(e, b["owner"], "button " + str(b["label"]))
            self._emit("interaction", i)
        elif kind == "dm_message":
            # A DM to this bot. Commands aren't run here (they are for servers); answer in on_dm / on("dm_message").
            if self.ignore_bots and data.get("author_bot"):
                return
            msg = self.wrap(data)
            msg.dm = True
            msg.is_dm = True
            msg.dm_channel_id = data.get("dm_channel_id") or data.get("channel_id")
            self._emit("dm_message", msg)
        else:
            self._emit(kind, Data(data, type=kind))

    # API reference: https://www.yapin.gg/developers/api/bot#put-commands
    def sync_commands(self):
        """Send the command list to Yapin’ (run() does it, and again after cogs change)."""
        self._dirty = False
        cmds = [{"name": n, "description": c["description"]} for n, c in self.commands.items()]
        self.api("PUT", "/commands", {"commands": cmds})
        return cmds

    # API reference: https://www.yapin.gg/developers/api/events#get-events
    # API reference: https://www.yapin.gg/developers/api/bot#get-me
    def run(self):
        """Register the commands and handle events until stopped (Ctrl+C)."""
        try:
            self._run()
        except KeyboardInterrupt:
            print("[yapper] stopped")
        finally:
            self.running = False
            self._registered = False

    def _run(self):
        self.running = True
        _check_version(self.base, self.log)
        fails = 0
        while self.running:
            try:
                self.me = Data(self.api("GET", "/me"))
                cmds = self.sync_commands()
                self.last = self.api("GET", "/events?after=0&wait=0").get("last") or 0   # skip old events
                break
            except YapperError as e:
                if e.status in (400, 401, 403):
                    self.running = False
                    raise
                fails += 1
                self._fail(e)
                time.sleep(min(30, 2 ** fails))
            except OSError as e:   # network down: keep trying
                fails += 1
                self._fail(e)
                time.sleep(min(30, 2 ** fails))
        if not self.running:
            return
        self._registered = True
        if self.log:
            print(f"[yapper] {self.me.username} is online" + (f" with {len(cmds)} command{'' if len(cmds) == 1 else 's'}" if cmds else ""))
        self._emit("ready", self.me)
        fails = 0
        gw_fails = 0
        gw_retry_at = 0.0
        while self.running:
            self._sync_if_dirty()
            # The gateway first; when it's off for this bot or keeps failing, the long poll below for a while.
            if self.gateway and time.time() >= gw_retry_at:
                r = self._gateway_session()
                if not self.running:
                    break
                if r == "bad_token":
                    raise SystemExit("Token rejected: make a new one in Settings -> Developer")
                if r == "disabled":
                    gw_retry_at = time.time() + 10 * 60
                elif r == "ok":
                    gw_fails = 0
                    time.sleep(_backoff(1))
                    continue
                else:
                    gw_fails += 1
                    if gw_fails >= 3:
                        if self.log:
                            print("[yapper] gateway unavailable, long polling for 5 minutes")
                        gw_fails = 0
                        gw_retry_at = time.time() + 5 * 60
                    else:
                        time.sleep(_backoff(gw_fails))
                        continue
            try:
                out = self.api("GET", f"/events?after={self.last}&wait=25", retries=0)
                fails = 0
            except Exception as e:
                fails += 1
                if getattr(e, "status", None) == 401:
                    raise SystemExit("Token rejected: make a new one in Settings -> Developer")
                self._fail(e)
                time.sleep(max(getattr(e, "retry_after", None) or 0, min(30, 2 ** fails) * (0.75 + random.random() / 2)))
                continue
            for ev in out.get("events") or []:
                if not ev["id"] > self.last:   # already handled
                    continue
                self.last = ev["id"]
                self.handle(ev)
            self.last = max(self.last, out.get("last") or 0)

    start = run

    def _sync_if_dirty(self):
        if not self._dirty:   # a cog was loaded or unloaded: send the new list once
            return
        try:
            n = len(self.sync_commands())
            if self.log:
                print(f"[yapper] commands updated ({n})")
        except Exception as e:
            self._fail(e)

    def _gateway_session(self):
        """One gateway connection (docs: /developers#gateway). Picks up after self.last, so nothing is missed between
        a long poll and the socket or across reconnects. Returns when the socket closes: "ok" (was up a while, connect
        again), "disabled" (not on for this bot: long poll), "bad_token" or "failed"."""
        try:
            ws = _WebSocket(self.gateway, timeout=15)
        except (OSError, ValueError, _GatewayClosed):
            return "failed"
        self._ws = ws
        ready_at = 0.0
        interval = 30.0
        next_beat = 0.0
        acked = True
        result = "failed"
        try:
            while self.running:
                if ready_at and time.time() >= next_beat:
                    if not acked:   # no answer to the last heartbeat: the connection is dead, start over
                        result = "ok"
                        break
                    acked = False
                    ws.send({"op": "heartbeat"})
                    next_beat = time.time() + interval
                    continue
                # A socket that never gets to ready doesn't hold the bot up: 15 s, then the next try (or the long poll).
                if not ws.readable(next_beat - time.time() if ready_at else 15.0):
                    if not ready_at:
                        break
                    continue
                raw = ws.recv()
                if raw is None:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                op = msg.get("op")
                if op == "hello":
                    ws.send({"op": "resume", "token": self.token, "seq": self.last})
                elif op == "ready":
                    ready_at = time.time()
                    interval = max(5.0, (msg.get("heartbeat_interval") or 30000) / 1000)
                    next_beat = ready_at + interval
                    if self.log:
                        print("[yapper] connected to the gateway")
                elif op == "heartbeat_ack":
                    acked = True
                elif op == "event" and isinstance(msg.get("d"), dict):
                    ev = msg["d"]
                    # Each event once, in order: anything at or below the cursor was already handled.
                    if isinstance(ev.get("id"), int) and ev["id"] > self.last:
                        self.last = ev["id"]
                        try:
                            self.handle(ev)
                        except Exception as e:
                            self._fail(e)
                    self._sync_if_dirty()
        except _GatewayClosed as c:
            up = ready_at and time.time() - ready_at > 10
            result = "bad_token" if c.code == 4004 else "disabled" if c.code == 4003 else "ok" if up else "failed"
        except OSError:
            result = "ok" if ready_at and time.time() - ready_at > 10 else "failed"
        finally:
            ws.close(1000 if not self.running else 4000)
            self._ws = None
        return result

    def stop(self):
        """Stop after the current poll (or close the gateway socket)."""
        self.running = False
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _iso(t):
    """A datetime, unix seconds or a string -> an ISO time for the API."""
    import datetime
    if isinstance(t, (int, float)):
        t = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    if isinstance(t, datetime.datetime):
        if t.tzinfo is None:
            t = t.astimezone()
        return t.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(t)


def _module_name(name):
    """ "cogs.fun" -> ("cogs.fun", None); "cogs/fun.py" -> ("cogs.fun", "/abs/cogs/fun.py")."""
    s = str(name)
    if s.endswith(".py") or os.sep in s or "/" in s:
        path = s if s.endswith(".py") else s + ".py"
        if not os.path.isabs(path):
            here = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else os.getcwd()
            path = next((p for p in (os.path.join(here, path), os.path.join(os.getcwd(), path)) if os.path.exists(p)), path)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Can't find {s}")
        parent = os.path.basename(os.path.dirname(path))
        mod = os.path.splitext(os.path.basename(path))[0]
        return (f"{parent}.{mod}" if parent.isidentifier() else mod), path
    return s, None


class VoiceCall:
    """A bot in a voice channel (Bot.connect_voice): .room, .source, .track, .e2ee, and `await call.leave()`."""

    def __init__(self, room, source, track, e2ee, keys):
        self.room, self.source, self.track, self.e2ee, self._keys = room, source, track, e2ee, keys

    async def leave(self):
        if self._keys:
            self._keys()
        await self.room.disconnect()


async def _hand_out_voice_key(room, media_key):
    """Encrypted calls: the bot's half of the app's key handoff. Our P-256 public key goes in the participant
    attribute `e2ee`; everyone who has one gets our media key over the data channel (topic yapper-e2ee), sealed with
    AES-GCM under the ECDH secret the two of us share and bound to "<from>|<to>|<index>". The bot hears nobody, so it
    never needs anyone's key back, only to answer when someone sends theirs. Returns a function that stops it."""
    import asyncio
    import base64
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    me = room.local_participant.identity
    priv = ec.generate_private_key(ec.SECP256R1())
    my_pub = base64.b64encode(priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).decode()
    sent, shared, state = {}, {}, {"stopped": False}

    async def send_to(p, pub=None):
        their = (p.attributes or {}).get("e2ee") or pub
        if state["stopped"] or not their or p.identity == me or sent.get(p.identity) == their:
            return
        sent[p.identity] = their
        try:
            if their not in shared:
                peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(their))
                shared[their] = AESGCM(priv.exchange(ec.ECDH(), peer))
            iv = os.urandom(12)
            ct = shared[their].encrypt(iv, media_key, f"{me}|{p.identity}|0".encode())
            msg = json.dumps({"i": 0, "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode(), "k": my_pub})
            await room.local_participant.publish_data(msg.encode(), reliable=True, topic="yapper-e2ee", destination_identities=[p.identity])
        except Exception:
            sent.pop(p.identity, None)  # muted by a moderator (no data permission) or a bad key: try again on the next change

    def on_peer(p):
        asyncio.ensure_future(send_to(p))

    def on_attrs(_changed, p):
        asyncio.ensure_future(send_to(p))

    def on_data(packet):
        if packet.topic != "yapper-e2ee" or packet.participant is None:
            return
        try:
            k = json.loads(packet.data.decode()).get("k")
        except Exception:
            return
        if isinstance(k, str):
            sent.pop(packet.participant.identity, None)
            asyncio.ensure_future(send_to(packet.participant, k))

    room.on("participant_connected", on_peer)
    room.on("participant_attributes_changed", on_attrs)
    room.on("data_received", on_data)
    await room.local_participant.set_attributes({"e2ee": my_pub})
    for p in list(room.remote_participants.values()):
        await send_to(p)

    def stop():
        state["stopped"] = True
        room.off("participant_connected", on_peer)
        room.off("participant_attributes_changed", on_attrs)
        room.off("data_received", on_data)
    return stop
