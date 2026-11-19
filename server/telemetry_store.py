"""Storage for the telemetry server: bots, their owners and private keys, staff roles and sharing, kept in a PickleDB file."""
import json
import os
import re
import secrets
import threading
import time

import pickledb

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")     # bot IDs and user IDs
ROLES = ("owner", "manager", "approver")        # owners come from TELEMETRY_OWNERS; the panel hands out the other two
SAVE_DELAY = 5.0                                # seconds a routine change (a report arriving) waits before it is written out
MAX_PENDING_PER_OWNER = 10                      # bots one (unverified) owner ID can have waiting
MAX_PENDING_TOTAL = 1000
DEFAULT_INTERVAL = 1800
CONSOLE = "console"                             # the actor name used by the console: it can do everything an owner can


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
    """(code, label) for where a bot stands: pending, waiting, active, quiet or removed."""
    now = now or time.time()
    if bot["status"] == "pending":
        return "pending", "Pending approval"
    if bot["status"] == "revoked":
        return "removed", "Access removed"
    if not bot["last_report_at"]:
        return "waiting", "Approved, waiting for the first report"
    if now - bot["last_report_at"] <= bot["interval"] * 2.2:
        return "active", "Active"
    return "quiet", "Quiet: no report for a while"


def _new_bot(bot_id, now, **fields):
    bot = {"bot_id": bot_id, "name": None, "status": "pending", "owner_id": None, "claimed_owner_id": None, "applicant_id": None,
           "key": None, "first_seen": now, "last_seen": now, "approved_by": None, "approved_at": None, "interval": DEFAULT_INTERVAL,
           "sdk_version": None, "package_version": None, "last_report_at": None, "last_report": None}
    bot.update(fields)
    return bot


class Store:
    """Everything lives in memory and is written to one PickleDB file (path), or nowhere for ":memory:".

    Approvals, roles, keys and sharing are written straight away. A routine change (a bot's report arriving, a bot being seen
    again) is written within SAVE_DELAY seconds, so a busy server doesn't rewrite the file for every report. close() writes
    anything left."""

    def __init__(self, path, owners=()):
        self.owners = frozenset(str(o) for o in owners)
        self._lock = threading.RLock()
        self._path = None if path == ":memory:" else path
        self._db = None
        self._dirty = False
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._bots, self._staff, self._members = {}, {}, {}
        if self._path:
            self._db = pickledb.PickleDB(self._path)
            try:
                self._db.load()
            except Exception as e:   # never start empty over a file we couldn't read: that would overwrite the data on the next save
                raise RuntimeError(f"{self._path} can't be read as a telemetry database ({type(e).__name__}). "
                                   f"Fix it or move it away; nothing was changed.") from e
            self._bots = self._db.get("bots", {}) or {}
            self._staff = self._db.get("staff", {}) or {}
            self._members = self._db.get("members", {}) or {}
            self._save()   # makes the file (private) right away
            threading.Thread(target=self._flusher, name="telemetry-store", daemon=True).start()

    # ---- saving ----

    def _save(self):
        with self._lock:
            if not self._db:
                return
            tmp = self._path + ".tmp"
            os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600))   # the file holds private keys: never readable by others, even briefly
            self._db.set("bots", self._bots)
            self._db.set("staff", self._staff)
            self._db.set("members", self._members)
            self._db.save()
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            self._dirty = False

    def _changed(self, urgent=True):
        with self._lock:
            self._dirty = True
            if urgent:
                self._save()
            else:
                self._wake.set()

    def _flusher(self):
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._closed.wait(SAVE_DELAY):
                break
            with self._lock:
                if self._dirty:
                    try:
                        self._save()
                    except OSError:
                        self._dirty = True   # try again next time

    def close(self):
        """Write anything not yet saved and stop. Safe to call twice."""
        self._closed.set()
        self._wake.set()
        with self._lock:
            if self._db and self._dirty:
                self._save()

    @staticmethod
    def _copy(bot):
        return dict(bot) if bot else None

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
            self._changed()

    def remove_role(self, actor, user_id):
        user_id = str(user_id).strip()
        with self._lock:
            if not can_remove_roles(self._actor_role(actor)):
                raise PermissionError("Only owners can remove a role.")
            if user_id in self.owners:
                raise ValueError("Owners are set with TELEMETRY_OWNERS and can't be removed here.")
            if self._staff.pop(user_id, None) is None:
                raise ValueError("That user has no role.")
            self._changed()

    # ---- bots ----

    def get_bot(self, bot_id):
        with self._lock:
            return self._copy(self._bots.get(str(bot_id)))

    def _pending(self):
        return [b for b in self._bots.values() if b["status"] == "pending"]

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
            bot = self._bots.get(bot_id)
            if bot is None:
                pending = self._pending()
                if len(pending) >= MAX_PENDING_TOTAL:
                    return {"bot_id": bot_id, "status": "pending", "name": name, "owner_id": None, "claimed_owner_id": claimed, "stored": False}
                if claimed and sum(1 for b in pending if b["claimed_owner_id"] == claimed) >= MAX_PENDING_PER_OWNER:
                    claimed = None
                self._bots[bot_id] = _new_bot(bot_id, now, name=name, claimed_owner_id=claimed, interval=interval or DEFAULT_INTERVAL,
                                              sdk_version=versions[0], package_version=versions[1])
                self._changed()
            else:
                bot["name"] = name or bot["name"]
                bot["last_seen"] = now
                bot["interval"] = interval or bot["interval"]
                bot["sdk_version"] = versions[0] or bot["sdk_version"]
                bot["package_version"] = versions[1] or bot["package_version"]
                if not bot["claimed_owner_id"] and bot["status"] == "pending":
                    bot["claimed_owner_id"] = claimed
                self._changed(urgent=False)
            return self._copy(self._bots[bot_id])

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
            bot = self._bots.get(bot_id)
            if bot is None:
                bot = self._bots[bot_id] = _new_bot(bot_id, now)
            bot.update(status="approved", owner_id=owner_id, key=bot["key"] or new_key(), approved_by=str(by), approved_at=now,
                       name=bot["name"] or name)
            self._changed()
            return self._copy(bot)

    def revoke(self, bot_id):
        with self._lock:
            bot = self._bots.get(str(bot_id))
            if not bot or bot["status"] != "approved":
                raise ValueError("That bot is not approved.")
            bot["status"] = "revoked"
            self._changed()

    def set_owner(self, bot_id, owner_id, new_key_too=False):
        """Switch a bot to another account (e.g. its owner got a new one). Anyone the old owner shared it with loses access,
        so the new owner starts clean; `new_key_too` also replaces the private key."""
        owner_id = str(owner_id).strip()
        if not valid_id(owner_id):
            raise ValueError("Enter the owner's user ID.")
        with self._lock:
            bot = self._bots.get(str(bot_id))
            if not bot or bot["status"] == "pending":
                raise ValueError("That bot is not approved.")
            bot["owner_id"] = owner_id
            self._members.pop(bot["bot_id"], None)
            if new_key_too:
                bot["key"] = new_key()
            self._changed()

    # ---- sharing a bot with other people ----

    def access_level(self, bot_id, user_id):
        """owner, manager or viewer for this person and bot; None when they have no access. Only approved bots can be reached."""
        with self._lock:
            bot = self._bots.get(str(bot_id))
            if not bot or bot["status"] != "approved":
                return None
            user_id = str(user_id)
            if bot["owner_id"] == user_id:
                return "owner"
            row = self._members.get(bot["bot_id"], {}).get(user_id)
            return row["level"] if row else None

    def members(self, bot_id):
        with self._lock:
            rows = self._members.get(str(bot_id), {}).values()
            return [dict(r) for r in sorted(rows, key=lambda r: (0 if r["level"] == "manager" else 1, r["user_id"]))]

    def share(self, actor, bot_id, user_id, level):
        """Give someone access to a bot. Owners can give viewer or manager, managers can give viewer."""
        user_id = str(user_id).strip()
        if not valid_id(user_id):
            raise ValueError("That does not look like a user ID.")
        with self._lock:
            mine = self.access_level(bot_id, actor)
            if level not in SHARE_LEVELS or mine not in ("owner", "manager") or (mine == "manager" and level != "viewer"):
                raise PermissionError("You can't give that access.")
            bot = self._bots[str(bot_id)]
            if user_id == bot["owner_id"]:
                raise ValueError("That person already owns this bot.")
            if user_id == str(actor):
                raise ValueError("You already have access.")
            current = self.access_level(bot_id, user_id)
            if mine == "manager" and current == "manager":
                raise PermissionError("Only the owner can change a manager.")
            group = self._members.setdefault(bot["bot_id"], {})
            if current is None and len(group) >= MAX_MEMBERS:
                raise ValueError(f"A bot can be shared with up to {MAX_MEMBERS} people.")
            group[user_id] = {"bot_id": bot["bot_id"], "user_id": user_id, "level": level, "added_by": str(actor), "added_at": int(time.time())}
            self._changed()

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
            group = self._members.get(str(bot_id), {})
            group.pop(user_id, None)
            if not group:
                self._members.pop(str(bot_id), None)
            self._changed()

    def shared_with(self, user_id):
        """Approved bots other people shared with this person, with their level."""
        user_id = str(user_id)
        with self._lock:
            out = [dict(self._bots[bid], level=group[user_id]["level"]) for bid, group in self._members.items()
                   if user_id in group and bid in self._bots and self._bots[bid]["status"] == "approved"]
        return sorted(out, key=lambda b: (b["name"] or b["bot_id"]).casefold())

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
        name = str(info["username"])[:80] if info.get("username") else None
        with self._lock:
            bot = self._bots.get(str(bot_id))
            if bot is None:
                return
            bot.update(last_report=report, last_report_at=now, last_seen=now, name=name or bot["name"],
                       sdk_version=str(sdk.get("version") or "")[:32] or bot["sdk_version"],
                       package_version=str(sdk.get("package_version") or "")[:32] or bot["package_version"])
            self._changed(urgent=False)

    # ---- lists for the panel ----

    def owned_by(self, user_id):
        """Bots this person owns (approved or not), newest first."""
        with self._lock:
            rows = [self._copy(b) for b in self._bots.values() if b["owner_id"] == str(user_id)]
        return sorted(rows, key=lambda b: b["approved_at"] or b["first_seen"], reverse=True)

    def is_verified_owner(self, user_id):
        """True once an approver has set this ID as the owner of a bot. From then on, bots that merely *say* they belong to
        this person don't show up in their panel: they have to apply for a bot while logged in as themselves."""
        with self._lock:
            return any(b["owner_id"] == str(user_id) for b in self._bots.values())

    def waiting_for(self, user_id):
        """Pending bots this person can follow: ones they applied for while logged in, plus (unless they already are a
        verified owner) ones whose own telemetry named them. Either way it is only a status; data comes after approval."""
        user_id = str(user_id)
        verified = self.is_verified_owner(user_id)
        with self._lock:
            rows = [self._copy(b) for b in self._pending()
                    if b["owner_id"] is None and (b["applicant_id"] == user_id or (not verified and b["claimed_owner_id"] == user_id))]
        return sorted(rows, key=lambda b: b["first_seen"], reverse=True)

    def apply(self, user_id, bot_id, now=None):
        """A logged-in person applies for a bot. Their ID is verified by the login, so staff see it as such."""
        user_id, bot_id = str(user_id), str(bot_id).strip()
        if not valid_id(bot_id):
            raise ValueError("That does not look like a bot ID.")
        now = int(now or time.time())
        with self._lock:
            bot = self._bots.get(bot_id)
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
                self._bots[bot_id] = _new_bot(bot_id, now, applicant_id=user_id)
            else:
                bot["applicant_id"] = user_id
            self._changed()

    def rotate_key(self, bot_id, user_id):
        """A new private key for a bot, only by its owner or a manager they added. The old key stops working at once."""
        with self._lock:
            if self.access_level(bot_id, user_id) not in ("owner", "manager"):
                raise PermissionError("That is not your bot.")
            self._bots[str(bot_id)]["key"] = new_key()
            self._changed()

    def with_status(self, status):
        with self._lock:
            rows = [self._copy(b) for b in self._bots.values() if b["status"] == status]
        if status == "pending":
            return sorted(rows, key=lambda b: b["first_seen"], reverse=True)
        return sorted(rows, key=lambda b: (b["name"] or b["bot_id"]).casefold())

    def counts(self):
        with self._lock:
            out = {}
            for b in self._bots.values():
                out[b["status"]] = out.get(b["status"], 0) + 1
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
                    self._bots[bot_id]["owner_id"] = None   # no owner known: set one in the panel
                    self._changed()
            rec = latest.get(bot_id)
            if rec:
                try:
                    stamp = int(time.mktime(time.strptime(rec["received_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
                except (KeyError, ValueError):
                    stamp = int(now or time.time())
                self.record_report(bot_id, rec["report"], stamp)
            imported += 1
        return imported
