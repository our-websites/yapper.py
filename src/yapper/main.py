"""Yapin’ bot library for Python. One file, standard library only, Python 3.9+.

    import yapper

    bot = yapper.Bot()                     # token from YAPPER_BOT_TOKEN, or yapper.Bot("yb_...")

    @bot.command("ping", "Is the bot alive?")
    def ping(msg):
        msg.reply("pong")

    @bot.on("member_join")
    def welcome(ev):
        print(ev.username, "joined")

    @bot.button("vote:")                   # clicks on buttons whose custom_id starts with "vote:"
    def vote(i):
        i.ephemeral("You picked " + i.custom_id[5:])

    bot.load_extensions("cogs")            # optional: every cogs/*.py (see Cog below)
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
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dotenv import load_dotenv

load_dotenv()

__all__ = ["Bot", "VoiceCall", "Cog", "command", "listener", "button", "YapperError", "row", "btn", "link_button"]
__version__ = "1.2"

BASE = "https://bots.yapper.website/api/bot/v1"
# The website's address for the same API: used for 10 minutes when BASE can't be reached.
FALLBACK_BASE = "https://www.yapin.gg/api/bot/v1"
GATEWAY = "wss://rt.yapper.website/bot"
COMMAND_LIMIT = 100
EVENTS = ("message", "command", "member_join", "member_leave", "reaction_add", "reaction_remove",
          "voice_join", "voice_leave", "interaction", "test", "ready", "error")


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

    def reply(self, content="", embeds=None, components=None):
        """Reply to this message (quoted)."""
        return self._bot.send(self.channel_id, content, embeds=embeds, components=components, reply_to=self.id)

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
        Events: message, command, member_join, member_leave, reaction_add, reaction_remove, voice_join,
        voice_leave, interaction, test, ready, error."""
        def deco(f):
            self._listeners.setdefault(event, []).append(f)
            owner = self._loading[-1] if self._loading else None
            if owner:
                owner.listeners.append((event, f))
            return f
        return deco(fn) if fn else deco

    def event(self, fn):
        """Decorator: the function name says the event. @bot.event def on_member_join(ev): ..."""
        name = fn.__name__
        return self.on(name[3:] if name.startswith("on_") else name, fn)

    def button(self, prefix):
        """Decorator for button clicks whose custom_id starts with `prefix`: @bot.button("vote:")."""
        def deco(fn):
            owner = self._loading[-1] if self._loading else None
            entry = {"prefix": prefix, "fn": fn, "owner": owner}
            self._buttons.append(entry)
            if owner:
                owner.buttons.append(entry)
            return fn
        return deco

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
                        entry = {"prefix": raw.__yapper_button__, "fn": fn, "owner": owner}
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

    def api(self, method, path, body=None, timeout=40, retries=3):
        """Raw API call: bot.api("GET", "/servers"). A 429 with Retry-After, or Yapin’s own 503 "busy",
        is waited out and retried (nothing was done, so that's safe). Other errors raise YapperError."""
        data = None if body is None else json.dumps(body).encode()
        on_fallback = bool(self._fallback) and time.time() < self._fallback_until
        base = self._fallback if on_fallback else self.base
        req = urllib.request.Request(base + path, method=method, data=data, headers={
            "Authorization": f"Bot {self.token}", "Content-Type": "application/json", "User-Agent": f"yapper-py/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (502, 504) and self._fallback and not on_fallback:
                self._use_fallback(f"answered {e.code}")
                if method == "GET" and retries > 0:
                    return self.api(method, path, body, timeout, retries - 1)
            try:
                err = json.loads(e.read() or b"{}")
            except Exception:
                err = {}
            if not isinstance(err, dict):
                err = {}
            busy = e.code == 503 and "retry_after" in err
            wait = 0
            if e.code == 429 or busy:
                try:
                    wait = float(e.headers.get("Retry-After") or err.get("retry_after") or 0)
                except (TypeError, ValueError):
                    wait = 0
            if 0 < wait <= 60 and retries > 0:
                if self.log:
                    print(f"[yapper] {'busy' if busy else 'rate limited'}, retrying {method} {path} in {wait:g}s")
                time.sleep(wait * (1 + random.random() * 0.5))
                return self.api(method, path, body, timeout, retries - 1)
            raise YapperError(err.get("error") or f"{method} {path} failed ({e.code})", e.code, wait or None) from None
        except urllib.error.URLError:
            # Our API host unreachable: the website's address for 10 minutes. A GET is asked again there at once;
            # anything else isn't (it may have gone through), the next call goes there.
            if not self._fallback or on_fallback:
                raise
            self._use_fallback("unreachable")
            if method == "GET" and retries > 0:
                return self.api(method, path, body, timeout, retries - 1)
            raise

    def _use_fallback(self, why):
        self._fallback_until = time.time() + 600
        if self.log:
            print(f"[yapper] {self.base} {why}, using {self._fallback} for 10 minutes")

    # ---- messages and more ----

    def send(self, channel_id, content="", embeds=None, components=None, reply_to=None):
        """Post a message. Embeds and buttons are plain dicts (see /developers). Returns the message."""
        body = {"content": str(content or "")}
        if reply_to:
            body["reply_to"] = reply_to
        if embeds:
            body["embeds"] = embeds if isinstance(embeds, list) else [embeds]
        if components:
            body["components"] = components if isinstance(components, list) else [components]
        return Data(self.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {})

    def send_layout(self, channel_id, components, reply_to=None):
        """Post a Components V2 message: the layout is the whole message (no content or embeds).
        bot.send_layout(ch, [container(text("# Hi"), section("Ready?", accessory=btn("Go", "go")), accent="blue")])"""
        body = {"flags": IS_COMPONENTS_V2, "components": components if isinstance(components, list) else [components]}
        if reply_to:
            body["reply_to"] = reply_to
        return Data(self.api("POST", f"/channels/{channel_id}/messages", body).get("message") or {})

    def edit_layout(self, channel_id, message_id, components):
        """Swap the layout of one of the bot's Components V2 messages."""
        body = {"flags": IS_COMPONENTS_V2, "components": components if isinstance(components, list) else [components]}
        return Data(self.api("PATCH", f"/channels/{channel_id}/messages/{message_id}", body).get("message") or {})

    def edit(self, channel_id, message_id, content=None, embeds=None, components=None):
        body = {}
        if content is not None:
            body["content"] = str(content)
        if embeds is not None:
            body["embeds"] = embeds
        if components is not None:
            body["components"] = components
        return Data(self.api("PATCH", f"/channels/{channel_id}/messages/{message_id}", body).get("message") or {})

    def delete(self, channel_id, message_id):
        return self.api("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def react(self, channel_id, message_id, emoji):
        return self.api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote(emoji)}")

    def respond(self, interaction, kind, content=None, embeds=None, components=None, layout=None):
        """Answer a button click: kind is reply, ephemeral, update or ack. layout=[...]: a Components V2 layout."""
        body = {"type": kind}
        if content is not None:
            body["content"] = str(content)
        if embeds is not None:
            body["embeds"] = embeds
        if components is not None:
            body["components"] = components
        if layout is not None:
            body["flags"] = IS_COMPONENTS_V2
            body["components"] = layout if isinstance(layout, list) else [layout]
        return self.api("POST", f"/interactions/{interaction['id']}/{interaction['token']}/callback", body)

    def servers(self):
        return [Data(s) for s in self.api("GET", "/servers")["servers"]]

    def server(self, server_id):
        return Data(self.api("GET", f"/servers/{server_id}")["server"])

    def channels(self, server_id=None, type=None):
        """Channels the bot can see, in sidebar order. channels(server_id, type="text") for text only."""
        q = f"?type={type}" if type else ""
        path = f"/servers/{server_id}/channels{q}" if server_id else f"/channels{q}"
        return [Data(c) for c in self.api("GET", path)["channels"]]

    def channel(self, channel_id):
        try:
            return Data(self.api("GET", f"/channels/{channel_id}")["channel"])
        except YapperError:
            return None

    def members(self, server_id, after=None):
        return [Data(m) for m in self.api("GET", f"/servers/{server_id}/members?limit=200" + (f"&after={after}" if after else ""))["members"]]

    def member(self, server_id, user_id):
        return Data(self.api("GET", f"/servers/{server_id}/members/{user_id}")["member"])

    def roles(self, server_id):
        return [Data(r) for r in self.api("GET", f"/servers/{server_id}/roles")["roles"]]

    def user(self, user_id):
        return Data(self.api("GET", f"/users/{user_id}")["user"])

    def messages(self, channel_id, limit=50):
        out = self.api("GET", f"/channels/{channel_id}/messages?limit={limit}")
        by = {a["id"]: a for a in out.get("authors") or []}
        return [Data(m, author=by.get(m.get("author_id"))) for m in out.get("messages") or []]

    def add_role(self, server_id, user_id, role_id):
        return self.api("PUT", f"/servers/{server_id}/members/{user_id}/roles/{role_id}")

    def remove_role(self, server_id, user_id, role_id):
        return self.api("DELETE", f"/servers/{server_id}/members/{user_id}/roles/{role_id}")

    def kick(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/kick", {"user_id": user_id})

    def ban(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/bans", {"user_id": user_id})

    def unban(self, server_id, user_id):
        return self.api("DELETE", f"/servers/{server_id}/bans/{user_id}")

    def timeout(self, server_id, user_id, minutes, reason=""):
        """Time someone out for `minutes` (0 clears it)."""
        return self.api("POST", f"/servers/{server_id}/timeouts", {"user_id": user_id, "minutes": minutes, "reason": reason})

    # ---- voice (Move Members for moves; 30 moves and 60 lookups a minute per bot) ----
    def voice(self, server_id):
        """Who is in voice: [{channel_id, user_ids}] for every voice channel the bot can see (empty ones too)."""
        return self.api("GET", f"/servers/{server_id}/voice")["voice"]

    def move_member(self, server_id, user_id, channel_id):
        return self.api("POST", f"/servers/{server_id}/voice/move", {"user_id": user_id, "channel_id": channel_id})

    def disconnect_member(self, server_id, user_id):
        return self.api("POST", f"/servers/{server_id}/voice/disconnect", {"user_id": user_id})

    # ---- the bot in voice: music bots (10 joins and leaves a minute; one voice channel per server) ----
    def join_voice(self, server_id, channel_id):
        """A ticket for a voice channel: {url, token, e2ee, voice_muted, channel_id, server_id}. Connect with any
        LiveKit SDK within a minute. The bot sends one audio track (as a mic) and hears nobody. Joining another
        channel of the same server drops it from the first. Most bots want connect_voice, which does all of it."""
        return self.api("POST", f"/servers/{server_id}/voice/join", {"channel_id": channel_id})

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
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False))
        return VoiceCall(room, source, track, bool(media_key), keys)

    def dm(self, user_id):
        """Open a DM with someone. Returns the channel id."""
        return self.api("POST", f"/users/{user_id}/dm")["channel_id"]

    def set_status(self, status="online", activity=None):
        return self.api("PUT", "/me/status", {"status": status, "activity": activity})

    def set_profile(self, display_name=None, bio=None):
        return self.api("PATCH", "/me", {k: v for k, v in (("display_name", display_name), ("bio", bio)) if v is not None})

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
            msg = Message.of(data, self)
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
            i = Interaction.of(data, self)
            for b in list(self._buttons):
                if str(data.get("custom_id") or "").startswith(b["prefix"]):
                    try:
                        b["fn"](i)
                    except Exception as e:
                        self._fail(e, b["owner"], "button " + b["prefix"])
            self._emit("interaction", i)
        else:
            self._emit(kind, Data(data, type=kind))

    def sync_commands(self):
        """Send the command list to Yapin’ (run() does it, and again after cogs change)."""
        self._dirty = False
        cmds = [{"name": n, "description": c["description"]} for n, c in self.commands.items()]
        self.api("PUT", "/commands", {"commands": cmds})
        return cmds

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
