"""Storage for the telemetry server.

Each bot gets its own PickleDB file, telemetry_data/<bot id>_data.json (its record, private key, latest report and who it is
shared with), so the number of bots is not limited by one big file and saving a bot only rewrites that bot's file. Staff roles
live in one small PickleDB file. Memory only holds a light index of every bot (status, owner, name, times)."""
import hashlib
import json
import os
import re
import sys
import zoneinfo
import secrets
import threading
import time

import pickledb

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")     # bot IDs and user IDs
ROLES = ("owner", "manager", "approver")        # owners come from TELEMETRY_OWNERS; the panel hands out the other two
SAVE_DELAY = 5.0                                # seconds a routine change (a report arriving) waits before it is written out
SAMPLE_EVERY = 3600                             # seconds between growth points kept for a bot
MAX_SAMPLES = 1500
ANNOUNCEMENTS_KEPT = 300
MAX_API_KEYS = 5
ANNOUNCEMENT_DAYS = 30
MAX_AUDIT_READ = 2_000_000                      # bytes of an audit file looked at when showing recent activity
DATA_DIR = "telemetry_data"                     # one <bot id>_data.json file per bot in here
SUFFIX = "_data.json"
HEAVY = ("last_report", "key", "samples")                   # kept in the bot's file only, not in the in-memory index
MAX_PENDING_PER_OWNER = 10                      # bots one (unverified) owner ID can have waiting
MAX_PENDING_TOTAL = 1000
DEFAULT_INTERVAL = 1800
CONSOLE = "console"                             # the actor name used by the console: it can do everything an owner can


def clean_avatar(value):
    """A bot's picture as an https link. Older bots report Yapin's path (avatar/path.png): that lives under files.yapper.website/avatars/."""
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        return None
    value = value.strip()
    if value.startswith("https://"):
        return value.replace(" ", "%20")
    if value.startswith("/") and not value.startswith("//"):
        return "https://www.yapin.gg" + value.replace(" ", "%20")
    if re.match(r"^[\w./~-][\w./ ~%-]*$", value) and ".." not in value and not value.startswith("//"):
        return "https://files.yapper.website/avatars/" + value.lstrip("/").replace(" ", "%20")
    return None


def clean_description(value, limit=300):
    """A bot's own description as one line of plain text, or None."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(c if c >= " " and c != "\x7f" else " " for c in value).split())
    return text[:limit] or None


INVITE = re.compile(r"^https://(?:www\.)?yapin\.gg/(?:invite|i|bot)/[A-Za-z0-9_-]{2,64}$")


def valid_invite(value):
    """A Yapin' invite or bot page link (https://www.yapin.gg/invite/<code>, /i/<code> or /bot/<id>)."""
    return isinstance(value, str) and bool(INVITE.match(value))


def valid_id(value):
    return isinstance(value, str) and bool(ID.match(value))


def new_key():
    return "tk_" + secrets.token_urlsafe(24)


# ---- what each role may do ----

def can_approve(role):
    return role in ("owner", "manager", "approver")


def can_revoke(role):
    return role in ("owner", "manager")


def can_change_owner(role):
    """Approvers can switch a bot to a new owner account (someone lost or replaced their account)."""
    return role in ("owner", "manager", "approver")


SHARE_LEVELS = ("viewer", "manager")    # what a bot's owner can give others: view the data, or also manage the bot's key and sharing
MAX_MEMBERS = 20


def assignable_roles(role):
    """Roles someone with this role can give out: owners give approver and manager, managers only approver."""
    return {"owner": ("manager", "approver"), "manager": ("approver",)}.get(role, ())


def can_remove_roles(role):
    return role == "owner"


def bot_state(bot, now=None):
    """(code, label) for where a bot stands: pending, denied, waiting, active, quiet or removed."""
    now = now or time.time()
    if bot["status"] == "pending":
        return "pending", "Pending approval"
    if bot["status"] == "denied":
        return "denied", "Denied"
    if bot["status"] == "revoked":
        return "removed", "Access removed"
    if not bot["last_report_at"]:
        return "waiting", "Approved, waiting for the first report"
    if now - bot["last_report_at"] <= bot["interval"] * 2.2:
        return "active", "Active"
    return "quiet", "Quiet: no report for a while"


MIN_REASON, MAX_REASON = 5, 500


class ReasonRequired(ValueError):
    pass


class UserLookupError(ValueError):
    """A typed name couldn't be turned into a user ID; .code is need_login or ambiguous."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _new_bot(bot_id, now, **fields):
    bot = {"bot_id": bot_id, "name": None, "status": "pending", "owner_id": None, "claimed_owner_id": None, "applicant_id": None,
           "key": None, "first_seen": now, "last_seen": now, "approved_by": None, "approved_at": None, "interval": DEFAULT_INTERVAL,
           "sdk_version": None, "package_version": None, "last_report_at": None, "last_report": None, "avatar": None, "samples": [], "invite_url": None, "support_url": None, "description": None,
           "deny_reason": None, "denied_by": None, "denied_at": None, "servers": None, "members": None}
    bot.update(fields)
    return bot


def file_name(bot_id):
    """telemetry_data file name for a bot: <bot id>_data.json, with anything unusual in an ID escaped as ~xx."""
    return "".join(c if c.isalnum() and c.isascii() or c in "_.-" else f"~{ord(c):02x}" for c in bot_id) + SUFFIX


def bot_id_from_file(name):
    return re.sub(r"~([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), name[:-len(SUFFIX)])


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_db(path, values):
    """Write a PickleDB file that only this user can read, even for the moment before it is moved into place."""
    os.close(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT, 0o600))
    db = pickledb.PickleDB(path)
    for k, v in values.items():
        db.set(k, v)
    db.save()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_db(path):
    db = pickledb.PickleDB(path)
    db.load()
    return db


class Store:
    """Staff roles in one PickleDB file (path); every bot in its own file in data_dir. ":memory:" keeps everything in memory.

    Approvals, roles, keys and sharing are written straight away. A routine change (a bot's report arriving, a bot being seen
    again) is written within SAVE_DELAY seconds, so a busy server doesn't write a file for every report. close() writes
    anything left."""

    def __init__(self, path, owners=(), data_dir=None, id_pattern=r"^\d+$"):
        self.owners = frozenset(str(o) for o in owners)
        self._id_pattern = re.compile(id_pattern)   # what a user ID looks like, to tell an ID from a username
        self._lock = threading.RLock()
        self._path = None if path == ":memory:" else path
        self._dir = None if not self._path else (data_dir or os.path.join(os.path.dirname(os.path.abspath(self._path)), DATA_DIR))
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._staff, self._users, self._announcements, self._daily, self._seq = {}, {}, [], {}, 0
        self._audit_mem, self._audit_bot_mem, self._last_view = [], {}, {}
        self._index = {}          # bot_id -> the bot's record without its key and latest report
        self._member_of = {}      # user_id -> {bot_id: level}
        self._bot_members = {}    # bot_id -> the user_ids it is shared with
        self._dirty = {}          # bot_id -> (full record, members) not written yet (in memory mode: everything)
        self._broken = set()      # bot files that couldn't be read: left alone, never overwritten
        self._staff_dirty = False
        if self._path:
            self._load()
            threading.Thread(target=self._flusher, name="telemetry-store", daemon=True).start()

    # ---- files ----

    def _load(self):
        if os.path.dirname(os.path.abspath(self._path)):
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        try:
            main = _read_db(self._path)
            self._staff = main.get("staff", {}) or {}
            self._users = main.get("users", {}) or {}
            self._announcements = main.get("announcements", []) or []
            self._daily = main.get("daily", {}) or {}
            self._seq = int(main.get("seq", 0) or 0)
        except Exception as e:   # never start empty over a file we couldn't read: that would overwrite the data on the next save
            raise RuntimeError(f"{self._path} can't be read as a telemetry database ({type(e).__name__}). "
                               f"Fix it or move it away; nothing was changed.") from e
        self._save_main()
        _private_dir(self._dir)
        for name in sorted(os.listdir(self._dir)):
            if not name.endswith(SUFFIX):
                continue
            try:
                db = _read_db(os.path.join(self._dir, name))
                rec, members = db.get("bot"), db.get("members", {}) or {}
                if not isinstance(rec, dict) or not rec.get("bot_id"):
                    raise ValueError("no bot in this file")
            except Exception as e:
                self._broken.add(bot_id_from_file(name))
                print(f"warning: {os.path.join(self._dir, name)} can't be read ({type(e).__name__}); that bot is skipped and the file is left as it is.",
                      file=sys.stderr)
                continue
            self._remember(rec, members)

    def _remember(self, rec, members):
        """Update the in-memory index for a bot from its full record."""
        bot_id = rec["bot_id"]
        for user_id in self._bot_members.pop(bot_id, ()):
            self._member_of.get(user_id, {}).pop(bot_id, None)
            if not self._member_of.get(user_id, True):
                del self._member_of[user_id]
        for user_id, m in members.items():
            self._member_of.setdefault(user_id, {})[bot_id] = m["level"]
        if members:
            self._bot_members[bot_id] = set(members)
        self._index[bot_id] = {k: v for k, v in rec.items() if k not in HEAVY}

    def _file(self, bot_id):
        return os.path.join(self._dir, file_name(bot_id))

    def _read(self, bot_id):
        """(record, members) for a bot, or (None, {}). A copy: change it and hand it to _put()."""
        bot_id = str(bot_id)
        with self._lock:
            if bot_id in self._dirty:
                rec, members = self._dirty[bot_id]
            elif bot_id in self._index and self._dir:
                try:
                    db = _read_db(self._file(bot_id))
                    rec, members = db.get("bot"), db.get("members", {}) or {}
                except Exception:
                    return None, {}
            else:
                return None, {}
            return (dict(rec), {u: dict(m) for u, m in members.items()}) if rec else (None, {})

    def _put(self, rec, members, urgent=True):
        bot_id = rec["bot_id"]
        with self._lock:
            if bot_id in self._broken:
                raise RuntimeError(f"The data file for bot {bot_id} can't be read, so it can't be changed. Fix or move {self._file(bot_id)}.")
            self._remember(rec, members)
            if not self._dir:
                self._dirty[bot_id] = (rec, members)      # memory mode: this is the store
            elif urgent:
                self._dirty.pop(bot_id, None)
                _write_db(self._file(bot_id), {"bot": rec, "members": members})
            else:
                self._dirty[bot_id] = (rec, members)
                self._wake.set()

    def _save_main(self):
        if self._path:
            _write_db(self._path, {"staff": self._staff, "users": self._users, "announcements": self._announcements,
                                   "daily": self._daily, "seq": self._seq})

    def _flush(self):
        with self._lock:
            if not self._dir:
                return
            for bot_id in list(self._dirty):
                rec, members = self._dirty.pop(bot_id)
                try:
                    _write_db(self._file(bot_id), {"bot": rec, "members": members})
                except OSError:
                    self._dirty[bot_id] = (rec, members)   # try again next time

    def _flusher(self):
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._closed.wait(SAVE_DELAY):
                break
            self._flush()

    def close(self):
        """Write anything not yet saved and stop. Safe to call twice."""
        self._closed.set()
        self._wake.set()
        self._flush()

    # ---- staff ----

    def role_of(self, user_id):
        user_id = str(user_id)
        if user_id in self.owners:
            return "owner"
        with self._lock:
            row = self._staff.get(user_id)
            return row["role"] if row else None

    def staff(self):
        """Everyone with a role: the configured owners first, then the people the panel added."""
        out = [{"user_id": o, "role": "owner", "added_by": None, "added_at": None, "configured": True} for o in sorted(self.owners)]
        with self._lock:
            rest = sorted(self._staff.values(), key=lambda r: (0 if r["role"] == "manager" else 1, r["user_id"]))
            return out + [dict(r, configured=False) for r in rest]

    def _actor_role(self, actor):
        return "owner" if actor == CONSOLE else self.role_of(actor)

    def set_role(self, actor, user_id, role, now=None):
        """Give someone a role. PermissionError when the actor may not; ValueError for a bad request."""
        user_id = str(user_id).strip()
        if not valid_id(user_id):
            raise ValueError("That does not look like a user ID.")
        with self._lock:
            actor_role = self._actor_role(actor)
            if role not in assignable_roles(actor_role):
                raise PermissionError("You can't give that role.")
            if user_id in self.owners:
                raise ValueError("Owners are set with TELEMETRY_OWNERS and can't be changed here.")
            if user_id == actor:
                raise ValueError("You can't change your own role.")
            current = self.role_of(user_id)
            if actor_role == "manager" and current not in (None, "approver"):
                raise PermissionError("Managers can only add approvers.")
            self._staff[user_id] = {"user_id": user_id, "role": role, "added_by": str(actor), "added_at": int(now or time.time())}
            self._save_main()

    def remove_role(self, actor, user_id):
        user_id = str(user_id).strip()
        with self._lock:
            if not can_remove_roles(self._actor_role(actor)):
                raise PermissionError("Only owners can remove a role.")
            if user_id in self.owners:
                raise ValueError("Owners are set with TELEMETRY_OWNERS and can't be removed here.")
            if self._staff.pop(user_id, None) is None:
                raise ValueError("That user has no role.")
            self._save_main()

    # ---- bots ----

    def get_bot(self, bot_id):
        """The full record, including its key and latest report."""
        return self._read(bot_id)[0]

    def _pending(self):
        return [b for b in self._index.values() if b["status"] == "pending"]

    def see_bot(self, bot_id, name=None, claimed_owner=None, sdk=None, interval=None, now=None):
        """A bot asked for a token. Remember it (as pending when new) so its owner can see where it stands in the panel."""
        now = int(now or time.time())
        bot_id = str(bot_id)
        sdk = sdk if isinstance(sdk, dict) else {}
        name = str(name)[:80] if name else None
        claimed = claimed_owner if valid_id(claimed_owner) else None
        interval = max(300, min(86400, int(interval))) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else None
        versions = (str(sdk.get("version") or "")[:32] or None, str(sdk.get("package_version") or "")[:32] or None)
        with self._lock:
            bot, members = self._read(bot_id)
            if bot is None:
                pending = self._pending()
                if len(pending) >= MAX_PENDING_TOTAL:
                    return {"bot_id": bot_id, "status": "pending", "name": name, "owner_id": None, "claimed_owner_id": claimed, "stored": False}
                if claimed and sum(1 for b in pending if b["claimed_owner_id"] == claimed) >= MAX_PENDING_PER_OWNER:
                    claimed = None
                bot = _new_bot(bot_id, now, name=name, claimed_owner_id=claimed, interval=interval or DEFAULT_INTERVAL,
                               sdk_version=versions[0], package_version=versions[1])
                self._put(bot, {})
            else:
                bot["name"] = name or bot["name"]
                bot["last_seen"] = now
                bot["interval"] = interval or bot["interval"]
                bot["sdk_version"] = versions[0] or bot["sdk_version"]
                bot["package_version"] = versions[1] or bot["package_version"]
                if not bot["claimed_owner_id"] and bot["status"] == "pending":
                    bot["claimed_owner_id"] = claimed
                self._put(bot, members, urgent=False)
            return dict(bot)

    def approve(self, bot_id, owner_id, by, name=None, now=None):
        """Approve a bot and set the person who owns it. A private key is made the first time."""
        bot_id, owner_id = str(bot_id).strip(), str(owner_id).strip()
        if not valid_id(bot_id):
            raise ValueError("That does not look like a bot ID.")
        if not valid_id(owner_id):
            raise ValueError("Enter the owner's user ID.")
        now = int(now or time.time())
        name = str(name)[:80] if name else None
        with self._lock:
            bot, members = self._read(bot_id)
            if bot is None:
                bot, members = _new_bot(bot_id, now), {}
            bot.update(status="approved", owner_id=owner_id, key=bot["key"] or new_key(), approved_by=str(by), approved_at=now,
                       name=bot["name"] or name, deny_reason=None, denied_by=None, denied_at=None)
            self._put(bot, members)
            return dict(bot)

    def deny(self, bot_id, by, reason, now=None):
        """Say no to a bot that is waiting, with a reason the owner will see. A denied bot can still be approved later."""
        reason = " ".join(str(reason or "").split())
        if len(reason) < MIN_REASON:
            raise ReasonRequired(f"Write a reason of at least {MIN_REASON} characters: the bot's owner sees it.")
        with self._lock:
            bot, members = self._read(str(bot_id).strip())
            if not bot or bot["status"] != "pending":
                raise ValueError("Only a bot that is waiting for approval can be denied.")
            bot.update(status="denied", deny_reason=reason[:MAX_REASON], denied_by=str(by), denied_at=int(now or time.time()))
            self._put(bot, members)
            return dict(bot)

    def revoke(self, bot_id):
        with self._lock:
            bot, members = self._read(bot_id)
            if not bot or bot["status"] != "approved":
                raise ValueError("That bot is not approved.")
            bot["status"] = "revoked"
            self._put(bot, members)

    def set_owner(self, bot_id, owner_id, new_key_too=False):
        """Switch a bot to another account (e.g. its owner got a new one). Anyone the old owner shared it with loses access,
        so the new owner starts clean; `new_key_too` also replaces the private key."""
        owner_id = str(owner_id).strip()
        if not valid_id(owner_id):
            raise ValueError("Enter the owner's user ID.")
        with self._lock:
            bot, _ = self._read(bot_id)
            if not bot or bot["status"] == "pending":
                raise ValueError("That bot is not approved.")
            bot["owner_id"] = owner_id
            if new_key_too:
                bot["key"] = new_key()
            self._put(bot, {})

    # ---- sharing a bot with other people ----

    def access_level(self, bot_id, user_id):
        """owner, manager or viewer for this person and bot; None when they have no access. Only approved bots can be reached."""
        with self._lock:
            bot = self._index.get(str(bot_id))
            if not bot or bot["status"] != "approved":
                return None
            if bot["owner_id"] == str(user_id):
                return "owner"
            return self._member_of.get(str(user_id), {}).get(bot["bot_id"])

    def members(self, bot_id):
        rows = self._read(bot_id)[1].values()
        return sorted(rows, key=lambda r: (0 if r["level"] == "manager" else 1, r["user_id"]))

    def share(self, actor, bot_id, user_id, level):
        """Give someone access to a bot. Owners can give viewer or manager, managers can give viewer."""
        user_id = str(user_id).strip()
        if not valid_id(user_id):
            raise ValueError("That does not look like a user ID.")
        with self._lock:
            mine = self.access_level(bot_id, actor)
            if level not in SHARE_LEVELS or mine not in ("owner", "manager") or (mine == "manager" and level != "viewer"):
                raise PermissionError("You can't give that access.")
            bot, members = self._read(bot_id)
            if user_id == bot["owner_id"]:
                raise ValueError("That person already owns this bot.")
            if user_id == str(actor):
                raise ValueError("You already have access.")
            current = self.access_level(bot_id, user_id)
            if mine == "manager" and current == "manager":
                raise PermissionError("Only the owner can change a manager.")
            if current is None and len(members) >= MAX_MEMBERS:
                raise ValueError(f"A bot can be shared with up to {MAX_MEMBERS} people.")
            members[user_id] = {"bot_id": bot["bot_id"], "user_id": user_id, "level": level, "added_by": str(actor), "added_at": int(time.time())}
            self._put(bot, members)

    def unshare(self, actor, bot_id, user_id):
        user_id = str(user_id).strip()
        with self._lock:
            mine = self.access_level(bot_id, actor)
            theirs = self.access_level(bot_id, user_id)
            if theirs == "owner":
                raise PermissionError("The owner can't be removed.")
            if theirs not in ("viewer", "manager"):
                raise ValueError("That person doesn't have access.")
            # owners remove anyone; managers remove viewers; anyone can leave
            if user_id != str(actor) and not (mine == "owner" or (mine == "manager" and theirs == "viewer")):
                raise PermissionError("You can't remove that person.")
            bot, members = self._read(bot_id)
            members.pop(user_id, None)
            self._put(bot, members)

    def shared_with(self, user_id):
        """Approved bots other people shared with this person, with their level (full records: they may see the data)."""
        with self._lock:
            ids = {bid: level for bid, level in self._member_of.get(str(user_id), {}).items()
                   if self._index.get(bid, {}).get("status") == "approved"}
        out = [dict(self.get_bot(bid), level=level) for bid, level in ids.items()]
        return sorted([b for b in out if b.get("bot_id")], key=lambda b: (b["name"] or b["bot_id"]).casefold())

    def check_key(self, bot_id, key):
        bot = self.get_bot(bot_id)
        if not bot or not bot["key"] or not isinstance(key, str):
            return False
        return secrets.compare_digest(bot["key"].encode(), key.strip().encode())

    def record_report(self, bot_id, report, now=None):
        """Keep a report as the bot's latest and set its name from the name it sent."""
        now = int(now or time.time())
        info = report.get("bot") if isinstance(report.get("bot"), dict) else {}
        sdk = report.get("sdk") if isinstance(report.get("sdk"), dict) else {}
        name = re.sub(r"_(\d{4})$", r"#\1", str(info["username"])[:80]) if info.get("username") else None   # name_0220 -> name#0220
        with self._lock:
            bot, members = self._read(bot_id)
            if bot is None:
                return
            samples = list(bot.get("samples") or [])
            if not samples or now - samples[-1][0] >= SAMPLE_EVERY:        # one point an hour is plenty to see a bot grow
                servers = report.get("servers") if isinstance(report.get("servers"), dict) else {}
                cmds = report.get("commands")
                samples.append([now] + [servers.get(k) if isinstance(servers.get(k), int) else None for k in ("count", "members_total", "online_total")]
                               + [len(cmds) if isinstance(cmds, list) else None])
                samples = samples[-MAX_SAMPLES:]
            avatar = clean_avatar(info.get("avatar"))
            count = (report.get("servers") or {}).get("count") if isinstance(report.get("servers"), dict) else None
            total = (report.get("servers") or {}).get("members_total") if isinstance(report.get("servers"), dict) else None
            bot.update(members=total if isinstance(total, int) and not isinstance(total, bool) and 0 <= total < 10 ** 12 else bot.get("members"),
                       servers=count if isinstance(count, int) and not isinstance(count, bool) and 0 <= count < 10 ** 9 else bot.get("servers"),
                       last_report=report, last_report_at=now, last_seen=now, name=name or bot["name"], samples=samples,
                       avatar=avatar or bot.get("avatar"), description=clean_description(info.get("description")) or bot.get("description"),
                       sdk_version=str(sdk.get("version") or "")[:32] or bot["sdk_version"],
                       package_version=str(sdk.get("package_version") or "")[:32] or bot["package_version"])
            self._put(bot, members, urgent=False)

    # ---- lists for the panel ----

    def owned_by(self, user_id):
        """Bots this person owns (approved or not), newest first. Full records."""
        with self._lock:
            ids = [b["bot_id"] for b in self._index.values() if b["owner_id"] == str(user_id)]
        rows = [b for b in (self.get_bot(i) for i in ids) if b]
        return sorted(rows, key=lambda b: b["approved_at"] or b["first_seen"], reverse=True)

    def is_verified_owner(self, user_id):
        """True once an approver has set this ID as the owner of a bot. From then on, bots that merely *say* they belong to
        this person don't show up in their panel: they have to apply for a bot while logged in as themselves."""
        with self._lock:
            return any(b["owner_id"] == str(user_id) for b in self._index.values())

    def waiting_for(self, user_id):
        """Pending bots this person can follow: ones they applied for while logged in, plus (unless they already are a
        verified owner) ones whose own telemetry named them. Either way it is only a status; data comes after approval."""
        user_id = str(user_id)
        verified = self.is_verified_owner(user_id)
        with self._lock:
            rows = [dict(b) for b in self._index.values()
                    if b["status"] in ("pending", "denied") and b["owner_id"] is None and (b["applicant_id"] == user_id or (not verified and b["claimed_owner_id"] == user_id))]
        return sorted(rows, key=lambda b: b["first_seen"], reverse=True)

    def apply(self, user_id, bot_id, now=None):
        """A logged-in person applies for a bot. Their ID is verified by the login, so staff see it as such."""
        user_id, bot_id = str(user_id), str(bot_id).strip()
        if not valid_id(bot_id):
            raise ValueError("That does not look like a bot ID.")
        now = int(now or time.time())
        with self._lock:
            bot, members = self._read(bot_id)
            if bot and bot["status"] == "denied":
                raise ValueError(f"That bot was denied: {bot['deny_reason']} If you have fixed that, ask in the support server.")
            if bot and bot["status"] != "pending":
                raise ValueError("That bot is already registered. If it is yours, ask in the support server.")
            if bot and bot["applicant_id"] and bot["applicant_id"] != user_id:
                raise ValueError("Someone has already applied for that bot. If it is yours, ask in the support server.")
            pending = self._pending()
            if sum(1 for b in pending if b["applicant_id"] == user_id and b["bot_id"] != bot_id) >= MAX_PENDING_PER_OWNER:
                raise ValueError("You have too many applications waiting. Wait for some to be approved.")
            if bot is None:
                if len(pending) >= MAX_PENDING_TOTAL:
                    raise ValueError("Too many applications are waiting right now. Try again later.")
                bot, members = _new_bot(bot_id, now, applicant_id=user_id), {}
            else:
                bot["applicant_id"] = user_id
            self._put(bot, members)

    def rotate_key(self, bot_id, user_id):
        """A new private key for a bot, only by its owner or a manager they added. The old key stops working at once."""
        with self._lock:
            if self.access_level(bot_id, user_id) not in ("owner", "manager"):
                raise PermissionError("That is not your bot.")
            bot, members = self._read(bot_id)
            bot["key"] = new_key()
            self._put(bot, members)

    def set_invite(self, user_id, bot_id, url):
        """The link people use to add this bot (a yapin.gg invite). Only its owner or a manager they added; empty clears it."""
        url = str(url or "").strip()
        if url and not valid_invite(url):
            raise ValueError("That must be a https://www.yapin.gg/invite/... (or /bot/...) link.")
        with self._lock:
            if self.access_level(bot_id, user_id) not in ("owner", "manager"):
                raise PermissionError("That is not your bot.")
            bot, members = self._read(bot_id)
            bot["invite_url"] = url or None
            self._put(bot, members)

    def set_support(self, user_id, bot_id, url):
        """The bot's support server link (a yapin.gg invite). Only its owner or a manager they added; empty clears it."""
        url = str(url or "").strip()
        if url and (not valid_invite(url) or "/bot/" in url):
            raise ValueError("That must be a https://www.yapin.gg/invite/... link to your support server.")
        with self._lock:
            if self.access_level(bot_id, user_id) not in ("owner", "manager"):
                raise PermissionError("That is not your bot.")
            bot, members = self._read(bot_id)
            bot["support_url"] = url or None
            self._put(bot, members)

    def with_status(self, status):
        """Bots in a status, for the staff lists: the light records, never keys or reports."""
        with self._lock:
            rows = [dict(b) for b in self._index.values() if b["status"] == status]
        if status == "pending":
            return sorted(rows, key=lambda b: b["first_seen"], reverse=True)
        return sorted(rows, key=lambda b: (b["name"] or b["bot_id"]).casefold())

    def counts(self):
        with self._lock:
            out = {}
            for b in self._index.values():
                out[b["status"]] = out.get(b["status"], 0) + 1
            return out

    # ---- people (what we know from their Yapin' login) ----

    def record_login(self, user_id, username, avatar=None, now=None, display=""):
        """Remember who logged in and what Yapin' says about them. Always keyed by the user ID."""
        user_id, now = str(user_id), int(now or time.time())
        with self._lock:
            user = self._users.get(user_id)
            new = user is None
            user = self._users[user_id] = dict(user or {"user_id": user_id, "first_login": now, "logins": 0, "timezone": "auto"})
            user.update(username=str(username or "")[:64], display=str(display or "")[:64], avatar=avatar if isinstance(avatar, str) and avatar.startswith("https://") else None,
                        last_login=now, logins=user["logins"] + 1)
            day = self._daily.setdefault(time.strftime("%Y-%m-%d", time.gmtime(now)), {"logins": 0, "new": 0})
            day["logins"] += 1
            day["new"] += 1 if new else 0
            for old in sorted(self._daily)[:-120]:
                del self._daily[old]
            self._save_main()
            return dict(user)

    def get_user(self, user_id):
        with self._lock:
            user = self._users.get(str(user_id))
            return dict(user) if user else None

    def _known_ids(self):
        return set(self._users) | self.owners | set(self._staff) | {b["owner_id"] for b in self._index.values() if b["owner_id"]}

    def search_users(self, query, limit=8):
        """People who have logged in, by username or user ID (a part of either), best matches first."""
        q = str(query).strip().lstrip("@").casefold()
        if not q:
            return []
        with self._lock:
            hits = []
            for u in self._users.values():
                names, uid = [(u.get("username") or "").casefold(), (u.get("display") or "").casefold()], u["user_id"].casefold()
                rank = (0 if q in names or q == uid else 1 if uid.startswith(q) or any(n.startswith(q) for n in names)
                        else 2 if q in uid or any(q in n for n in names) else None)
                if rank is not None:
                    hits.append((rank, names[0], dict(u)))
            hits.sort(key=lambda h: h[:2])
            return [dict(h[2], role=self.role_of(h[2]["user_id"])) for h in hits[:limit]]

    def resolve_user(self, text):
        """The user ID for what someone typed: a user ID, or a username of someone who has logged in to the panel. IDs are
        always what gets saved. Raises UserLookupError("need_login") for a username nobody has logged in with (they have to
        log in first), UserLookupError("ambiguous") when two people share it, ValueError for nothing usable."""
        t = str(text or "").strip().lstrip("@")
        if not t:
            raise ValueError("Enter a username or user ID.")
        with self._lock:
            if t in self._known_ids():
                return t
            named = [uid for uid, u in self._users.items() if t.casefold() in ((u.get("username") or "").casefold(), (u.get("display") or "").casefold())]
            if len(named) == 1:
                return named[0]
            if len(named) > 1:
                raise UserLookupError("ambiguous", "More than one person has that username. Use their user ID.")
        if self._id_pattern.match(t) and valid_id(t):
            return t   # not seen before, but it looks like an ID: allowed
        raise UserLookupError("need_login", "That username can't be found. They need to log in to the panel first, or you can use their user ID.")

    def timezone_of(self, user_id):
        user = self.get_user(user_id)
        return user["timezone"] if user else "auto"

    def set_timezone(self, user_id, tz):
        if tz not in ("auto", "UTC") and tz not in zoneinfo.available_timezones():
            raise ValueError("Unknown timezone.")
        with self._lock:
            user = self._users.get(str(user_id))
            if user is None:
                raise ValueError("Log in first.")
            user["timezone"] = tz
            self._save_main()

    def user_stats(self, now=None):
        """Numbers about everyone who logged in with Yapin', for owners and managers."""
        now = int(now or time.time())
        with self._lock:
            users = list(self._users.values())
            owners = {b["owner_id"] for b in self._index.values() if b["owner_id"]}
            daily = [(time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)),) for i in range(13, -1, -1)]
            series = [(d[0], self._daily.get(d[0], {}).get("logins", 0), self._daily.get(d[0], {}).get("new", 0)) for d in daily]
            within = lambda key, secs: sum(1 for u in users if now - u[key] <= secs)
            return {"total": len(users), "logins": sum(u["logins"] for u in users),
                    "active_24h": within("last_login", 86400), "active_7d": within("last_login", 7 * 86400), "active_30d": within("last_login", 30 * 86400),
                    "new_24h": within("first_login", 86400), "new_7d": within("first_login", 7 * 86400), "new_30d": within("first_login", 30 * 86400),
                    "with_bots": len({u["user_id"] for u in users} & owners), "bot_owners": len(owners), "staff": len({u["user_id"] for u in users if self.role_of(u["user_id"])}),
                    "series": series,
                    "recent": sorted((dict(u) for u in users), key=lambda u: u["last_login"], reverse=True)[:10]}

    # ---- API keys: so people can read their bots' data without writing code ----

    def create_api_key(self, user_id, name, now=None):
        """A new read-only key for this person. The key itself is returned once; only a hash of it is kept."""
        name = str(name or "").strip()[:40] or "My key"
        with self._lock:
            user = self._users.get(str(user_id))
            if user is None:
                raise ValueError("Log in first.")
            keys = user.setdefault("api_keys", [])
            if len(keys) >= MAX_API_KEYS:
                raise ValueError(f"You can have up to {MAX_API_KEYS} API keys. Remove one first.")
            plain = "uk_" + secrets.token_urlsafe(32)
            self._seq += 1
            keys.append({"id": self._seq, "name": name, "hash": hashlib.sha256(plain.encode()).hexdigest(), "prefix": plain[:7],
                         "created_at": int(now or time.time()), "last_used": None})
            self._save_main()
            return plain

    def api_keys(self, user_id):
        with self._lock:
            return [{k: v for k, v in key.items() if k != "hash"} for key in (self._users.get(str(user_id)) or {}).get("api_keys", [])]

    def remove_api_key(self, user_id, key_id):
        with self._lock:
            user = self._users.get(str(user_id)) or {}
            keys = user.get("api_keys", [])
            kept = [k for k in keys if k["id"] != key_id]
            if len(kept) == len(keys):
                raise ValueError("That key doesn't exist.")
            user["api_keys"] = kept
            self._save_main()

    def user_for_api_key(self, plain, now=None):
        """(user_id, key name) for a valid key, else None. Compares hashes, so a stolen data file doesn't give out keys."""
        if not isinstance(plain, str) or not plain.startswith("uk_") or len(plain) > 100:
            return None
        digest = hashlib.sha256(plain.encode()).hexdigest()
        with self._lock:
            for user in self._users.values():
                for key in user.get("api_keys", []):
                    if secrets.compare_digest(key["hash"], digest):
                        key["last_used"] = int(now or time.time())      # saved with the next change
                        return user["user_id"], key["name"]
        return None

    def api_bots(self, user_id):
        """The bots a key may read: the ones this person owns, or manages through sharing. Full records."""
        user_id = str(user_id)
        with self._lock:
            ids = [b["bot_id"] for b in self._index.values() if b["owner_id"] == user_id and b["status"] == "approved"]
            ids += [bid for bid, level in self._member_of.get(user_id, {}).items() if level == "manager" and self._index.get(bid, {}).get("status") == "approved"]
        return [b for b in (self.get_bot(i) for i in sorted(set(ids))) if b]

    def bot_ids_owned(self, user_id):
        with self._lock:
            return [b["bot_id"] for b in self._index.values() if b["owner_id"] == str(user_id)]

    def bot_label(self, bot_id):
        with self._lock:
            bot = self._index.get(str(bot_id))
            return (bot["name"] or bot["bot_id"]) if bot else str(bot_id)

    # ---- announcements ----

    def announce(self, actor, target, title, body, now=None):
        """Leave an announcement for everyone ("all") or one person (their user ID). It waits until they next load the panel."""
        title, body = str(title or "").strip()[:80], str(body or "").strip()[:1000]
        if not title or not body:
            raise ValueError("Write a title and a message.")
        target = "all" if target == "all" else str(target)
        with self._lock:
            self._seq += 1
            item = {"id": self._seq, "created_at": int(now or time.time()), "author": str(actor), "target": target, "title": title, "body": body}
            self._announcements = (self._announcements + [item])[-ANNOUNCEMENTS_KEPT:]
            self._save_main()
            return dict(item)

    def unread_for(self, user_id, now=None):
        """Announcements this person hasn't dismissed yet: ones for them, and ones for everyone sent since they first logged in."""
        now = int(now or time.time())
        with self._lock:
            user = self._users.get(str(user_id))
            if not user:
                return []
            seen = user.get("seen_upto", 0)
            return [dict(a) for a in self._announcements
                    if a["id"] > seen and now - a["created_at"] <= ANNOUNCEMENT_DAYS * 86400
                    and (a["target"] == str(user_id) or (a["target"] == "all" and a["created_at"] >= user["first_login"]))]

    def dismiss(self, user_id, upto):
        with self._lock:
            user = self._users.get(str(user_id))
            if user and isinstance(upto, int) and upto > user.get("seen_upto", 0):
                user["seen_upto"] = upto
                self._save_main()

    def sent_announcements(self, limit=30):
        with self._lock:
            return [dict(a) for a in reversed(self._announcements[-limit:])]

    # ---- the audit log: what was done, by whom, on which bot ----

    def _audit_paths(self, bot_id, now):
        month = time.strftime("%Y-%m", time.gmtime(now))
        return (os.path.join(self._dir, f"_audit_{month}.jsonl"),
                os.path.join(self._dir, file_name(bot_id)[:-len(SUFFIX)] + "_audit.jsonl") if bot_id else None)

    def audit(self, actor, kind, level="approver", bot_id=None, target=None, detail=None, now=None):
        """Write one line to the log. `level` says who may read it: "approver" (approvers, managers, owners) or "manager"
        (managers and owners only; page views are in this one). Events on a bot are also kept in that bot's own log."""
        now = int(now or time.time())
        event = {"t": now, "actor": str(actor), "kind": kind, "level": level}
        for k, v in (("bot", bot_id), ("target", target), ("detail", detail)):
            if v not in (None, ""):
                event[k] = str(v)[:200]
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with self._lock:
            if not self._dir:
                self._audit_mem.append(event)
                if bot_id:
                    self._audit_bot_mem.setdefault(str(bot_id), []).append(event)
                return
            for path in filter(None, self._audit_paths(bot_id, now)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)

    def audit_view(self, actor, kind, level="manager", bot_id=None, detail=None, every=600, now=None):
        """Log that someone looked at something, but not again for the same thing within `every` seconds."""
        now = int(now or time.time())
        key = (str(actor), kind, str(bot_id), str(detail))
        with self._lock:
            if now - self._last_view.get(key, 0) < every:
                return
            self._last_view[key] = now
            if len(self._last_view) > 5000:
                self._last_view = {k: t for k, t in self._last_view.items() if now - t < every}
        self.audit(actor, kind, level, bot_id, None, detail, now)

    def read_audit(self, levels=("approver", "manager"), bot_id=None, limit=100, kinds=None):
        """The newest events first, only of the given levels (and kinds, if given); one bot's log when bot_id is given."""
        with self._lock:
            if not self._dir:
                rows = list(self._audit_bot_mem.get(str(bot_id), [])) if bot_id else list(self._audit_mem)
            else:
                now = int(time.time())
                paths = [self._audit_paths(bot_id, now)[1]] if bot_id else [
                    os.path.join(self._dir, f"_audit_{time.strftime('%Y-%m', time.gmtime(now - d * 86400))}.jsonl") for d in (0, 31, 62)]
                rows = []
                for path in reversed(paths):
                    rows += self._tail(path)
        rows = [e for e in rows if e.get("level") in levels and (not kinds or e.get("kind") in kinds)]
        return sorted(rows, key=lambda e: e["t"], reverse=True)[:limit]

    @staticmethod
    def _tail(path):
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - MAX_AUDIT_READ))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        out = []
        for line in data.splitlines()[1 if size > MAX_AUDIT_READ else 0:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ---- moving from the first version (approved_bots.json + a JSON-lines reports file) ----

    def import_legacy(self, approved_path, data_path=None, now=None):
        """Bring over bots approved by the first version of the server. Returns how many were imported."""
        if not approved_path or not os.path.exists(approved_path):
            return 0
        try:
            with open(approved_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        bots = data.get("bots", data) if isinstance(data, dict) else data
        ids = {str(b): {} for b in bots} if isinstance(bots, list) else {str(k): (v if isinstance(v, dict) else {}) for k, v in bots.items()}
        latest = {}
        if data_path and os.path.exists(data_path):
            with open(data_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        latest[str(rec["report"]["bot"]["id"])] = rec
                    except (ValueError, KeyError, TypeError):
                        continue
        imported = 0
        for bot_id, info in ids.items():
            if not valid_id(bot_id) or self.get_bot(bot_id):
                continue
            self.approve(bot_id, info.get("owner_id") or CONSOLE, "import", info.get("name"), now)
            if not info.get("owner_id"):
                with self._lock:
                    legacy, members = self._read(bot_id)
                    legacy["owner_id"] = None   # no owner known: set one in the panel
                    self._put(legacy, members)
            rec = latest.get(bot_id)
            if rec:
                try:
                    stamp = int(time.mktime(time.strptime(rec["received_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
                except (KeyError, ValueError):
                    stamp = int(now or time.time())
                self.record_report(bot_id, rec["report"], stamp)
            imported += 1
        return imported
