"""Storage for the telemetry server: bots, their owners and private keys, staff roles, and report history (SQLite)."""
import json
import os
import re
import secrets
import sqlite3
import threading
import time

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")     # bot IDs and user IDs
ROLES = ("owner", "manager", "approver")        # owners come from TELEMETRY_OWNERS; the panel hands out the other two
KEEP_REPORTS = 200                              # report history kept per bot
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


class Store:
    def __init__(self, path, owners=()):
        self.owners = frozenset(str(o) for o in owners)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)   # the file holds private keys
            except OSError:
                pass
        with self._lock:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS staff(user_id TEXT PRIMARY KEY, role TEXT NOT NULL, added_by TEXT, added_at INTEGER);
                CREATE TABLE IF NOT EXISTS bots(
                    bot_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL DEFAULT 'pending',
                    owner_id TEXT, claimed_owner_id TEXT, applicant_id TEXT, key TEXT,
                    first_seen INTEGER, last_seen INTEGER, approved_by TEXT, approved_at INTEGER,
                    interval INTEGER NOT NULL DEFAULT 1800, sdk_version TEXT, package_version TEXT,
                    last_report_at INTEGER, last_report TEXT);
                CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, received_at INTEGER NOT NULL, report TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reports_by_bot ON reports(bot_id, id);
            """)
            if "applicant_id" not in {r["name"] for r in self._db.execute("PRAGMA table_info(bots)")}:
                self._db.execute("ALTER TABLE bots ADD COLUMN applicant_id TEXT")   # databases made before applications existed

    def _all(self, sql, args=()):
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    @staticmethod
    def _row(row):
        d = dict(row)
        if "last_report" in d:
            try:
                d["last_report"] = json.loads(d["last_report"]) if d["last_report"] else None
            except ValueError:
                d["last_report"] = None
        return d

    # ---- staff ----

    def role_of(self, user_id):
        user_id = str(user_id)
        if user_id in self.owners:
            return "owner"
        row = self._one("SELECT role FROM staff WHERE user_id=?", (user_id,))
        return row["role"] if row else None

    def staff(self):
        """Everyone with a role: the configured owners first, then the people the panel added."""
        out = [{"user_id": o, "role": "owner", "added_by": None, "added_at": None, "configured": True} for o in sorted(self.owners)]
        out += [dict(r, configured=False) for r in self._all("SELECT * FROM staff ORDER BY CASE role WHEN 'manager' THEN 0 ELSE 1 END, user_id")]
        return out

    def _actor_role(self, actor):
        return "owner" if actor == CONSOLE else self.role_of(actor)

    def set_role(self, actor, user_id, role, now=None):
        """Give someone a role. PermissionError when the actor may not; ValueError for a bad request."""
        user_id = str(user_id).strip()
        if not valid_id(user_id):
            raise ValueError("That does not look like a user ID.")
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
        self._run("INSERT INTO staff(user_id, role, added_by, added_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, added_by=excluded.added_by, added_at=excluded.added_at",
                  (user_id, role, str(actor), int(now or time.time())))

    def remove_role(self, actor, user_id):
        if not can_remove_roles(self._actor_role(actor)):
            raise PermissionError("Only owners can remove a role.")
        user_id = str(user_id).strip()
        if user_id in self.owners:
            raise ValueError("Owners are set with TELEMETRY_OWNERS and can't be removed here.")
        if self._run("DELETE FROM staff WHERE user_id=?", (user_id,)).rowcount == 0:
            raise ValueError("That user has no role.")

    # ---- bots ----

    def get_bot(self, bot_id):
        return self._one("SELECT * FROM bots WHERE bot_id=?", (str(bot_id),))

    def see_bot(self, bot_id, name=None, claimed_owner=None, sdk=None, interval=None, now=None):
        """A bot asked for a token. Remember it (as pending when new) so its owner can see where it stands in the panel."""
        now = int(now or time.time())
        bot_id = str(bot_id)
        sdk = sdk if isinstance(sdk, dict) else {}
        name = str(name)[:80] if name else None
        claimed = claimed_owner if valid_id(claimed_owner) else None
        interval = max(300, min(86400, int(interval))) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else None
        versions = (str(sdk.get("version") or "")[:32] or None, str(sdk.get("package_version") or "")[:32] or None)
        bot = self.get_bot(bot_id)
        if bot is None:
            if self._one("SELECT COUNT(*) AS n FROM bots WHERE status='pending'")["n"] >= MAX_PENDING_TOTAL:
                return {"bot_id": bot_id, "status": "pending", "name": name, "owner_id": None, "claimed_owner_id": claimed, "stored": False}
            if claimed and self._one("SELECT COUNT(*) AS n FROM bots WHERE status='pending' AND claimed_owner_id=?", (claimed,))["n"] >= MAX_PENDING_PER_OWNER:
                claimed = None
            self._run("INSERT INTO bots(bot_id, name, status, claimed_owner_id, first_seen, last_seen, interval, sdk_version, package_version) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (bot_id, name, "pending", claimed, now, now, interval or DEFAULT_INTERVAL, *versions))
        else:
            self._run("UPDATE bots SET name=COALESCE(?, name), last_seen=?, interval=COALESCE(?, interval), sdk_version=COALESCE(?, sdk_version), "
                      "package_version=COALESCE(?, package_version), claimed_owner_id=COALESCE(claimed_owner_id, ?) WHERE bot_id=?",
                      (name, now, interval, *versions, claimed if bot["status"] == "pending" else None, bot_id))
        return self.get_bot(bot_id)

    def approve(self, bot_id, owner_id, by, name=None, now=None):
        """Approve a bot and set the person who owns it. A private key is made the first time."""
        bot_id, owner_id = str(bot_id).strip(), str(owner_id).strip()
        if not valid_id(bot_id):
            raise ValueError("That does not look like a bot ID.")
        if not valid_id(owner_id):
            raise ValueError("Enter the owner's user ID.")
        now = int(now or time.time())
        existing = self.get_bot(bot_id)
        if existing is None:
            self._run("INSERT INTO bots(bot_id, name, status, first_seen, last_seen, interval) VALUES(?,?,?,?,?,?)",
                      (bot_id, (name or None) and str(name)[:80], "pending", now, now, DEFAULT_INTERVAL))
        self._run("UPDATE bots SET status='approved', owner_id=?, key=COALESCE(key, ?), approved_by=?, approved_at=?, "
                  "name=COALESCE(name, ?) WHERE bot_id=?", (owner_id, new_key(), str(by), now, (name or None) and str(name)[:80], bot_id))
        return self.get_bot(bot_id)

    def revoke(self, bot_id):
        if self._run("UPDATE bots SET status='revoked' WHERE bot_id=? AND status='approved'", (str(bot_id),)).rowcount == 0:
            raise ValueError("That bot is not approved.")

    def set_owner(self, bot_id, owner_id):
        owner_id = str(owner_id).strip()
        if not valid_id(owner_id):
            raise ValueError("Enter the owner's user ID.")
        if self._run("UPDATE bots SET owner_id=? WHERE bot_id=? AND status!='pending'", (owner_id, str(bot_id))).rowcount == 0:
            raise ValueError("That bot is not approved.")

    def check_key(self, bot_id, key):
        bot = self.get_bot(bot_id)
        if not bot or not bot["key"] or not isinstance(key, str):
            return False
        return secrets.compare_digest(bot["key"].encode(), key.strip().encode())

    def record_report(self, bot_id, report, now=None):
        """Store a report as the bot's latest, set its name from the name it sent, and trim old history."""
        now = int(now or time.time())
        bot = report.get("bot") if isinstance(report.get("bot"), dict) else {}
        sdk = report.get("sdk") if isinstance(report.get("sdk"), dict) else {}
        name = str(bot["username"])[:80] if bot.get("username") else None
        text = json.dumps(report, separators=(",", ":"))
        with self._lock:
            self._db.execute("UPDATE bots SET last_report=?, last_report_at=?, last_seen=?, name=COALESCE(?, name), "
                             "sdk_version=COALESCE(?, sdk_version), package_version=COALESCE(?, package_version) WHERE bot_id=?",
                             (text, now, now, name, str(sdk.get("version") or "")[:32] or None, str(sdk.get("package_version") or "")[:32] or None, str(bot_id)))
            self._db.execute("INSERT INTO reports(bot_id, received_at, report) VALUES(?,?,?)", (str(bot_id), now, text))
            self._db.execute("DELETE FROM reports WHERE bot_id=? AND id NOT IN (SELECT id FROM reports WHERE bot_id=? ORDER BY id DESC LIMIT ?)",
                             (str(bot_id), str(bot_id), KEEP_REPORTS))

    def history(self, bot_id, limit=50):
        return [dict(r, report=json.loads(r["report"])) for r in
                self._all("SELECT id, received_at, report FROM reports WHERE bot_id=? ORDER BY id DESC LIMIT ?", (str(bot_id), limit))]

    # ---- lists for the panel ----

    def owned_by(self, user_id):
        """Bots this person owns (approved or not), newest first."""
        return self._all("SELECT * FROM bots WHERE owner_id=? ORDER BY COALESCE(approved_at, first_seen) DESC", (str(user_id),))

    def is_verified_owner(self, user_id):
        """True once an approver has set this ID as the owner of a bot. From then on, bots that merely *say* they belong to
        this person don't show up in their panel: they have to apply for a bot while logged in as themselves."""
        return self._one("SELECT 1 AS x FROM bots WHERE owner_id=? LIMIT 1", (str(user_id),)) is not None

    def waiting_for(self, user_id):
        """Pending bots this person can follow: ones they applied for while logged in, plus (unless they already are a
        verified owner) ones whose own telemetry named them. Either way it is only a status; data comes after approval."""
        user_id = str(user_id)
        if self.is_verified_owner(user_id):
            return self._all("SELECT * FROM bots WHERE status='pending' AND owner_id IS NULL AND applicant_id=? ORDER BY first_seen DESC", (user_id,))
        return self._all("SELECT * FROM bots WHERE status='pending' AND owner_id IS NULL AND (applicant_id=? OR claimed_owner_id=?) ORDER BY first_seen DESC",
                         (user_id, user_id))

    def apply(self, user_id, bot_id, now=None):
        """A logged-in person applies for a bot. Their ID is verified by the login, so staff see it as such."""
        user_id, bot_id = str(user_id), str(bot_id).strip()
        if not valid_id(bot_id):
            raise ValueError("That does not look like a bot ID.")
        now = int(now or time.time())
        bot = self.get_bot(bot_id)
        if bot and bot["status"] != "pending":
            raise ValueError("That bot is already registered. If it is yours, ask in the support server.")
        if bot and bot["applicant_id"] and bot["applicant_id"] != user_id:
            raise ValueError("Someone has already applied for that bot. If it is yours, ask in the support server.")
        mine = self._one("SELECT COUNT(*) AS n FROM bots WHERE status='pending' AND applicant_id=? AND bot_id!=?", (user_id, bot_id))["n"]
        if mine >= MAX_PENDING_PER_OWNER:
            raise ValueError("You have too many applications waiting. Wait for some to be approved.")
        if bot is None:
            if self._one("SELECT COUNT(*) AS n FROM bots WHERE status='pending'")["n"] >= MAX_PENDING_TOTAL:
                raise ValueError("Too many applications are waiting right now. Try again later.")
            self._run("INSERT INTO bots(bot_id, status, applicant_id, first_seen, last_seen, interval) VALUES(?,?,?,?,?,?)",
                      (bot_id, "pending", user_id, now, now, DEFAULT_INTERVAL))
        else:
            self._run("UPDATE bots SET applicant_id=? WHERE bot_id=?", (user_id, bot_id))

    def rotate_key(self, bot_id, user_id):
        """A new private key for a bot, only by its owner. The old key stops working at once."""
        bot = self.get_bot(bot_id)
        if not bot or bot["status"] != "approved" or bot["owner_id"] != str(user_id):
            raise PermissionError("That is not your bot.")
        self._run("UPDATE bots SET key=? WHERE bot_id=?", (new_key(), bot["bot_id"]))

    def with_status(self, status):
        order = "first_seen DESC" if status == "pending" else "COALESCE(name, bot_id) COLLATE NOCASE"
        return self._all("SELECT * FROM bots WHERE status=? ORDER BY " + order, (status,))

    def counts(self):
        rows = self._all("SELECT status, COUNT(*) AS n FROM bots GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

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
                self._run("UPDATE bots SET owner_id=NULL WHERE bot_id=?", (bot_id,))   # no owner known: set one in the panel
            rec = latest.get(bot_id)
            if rec:
                try:
                    stamp = int(time.mktime(time.strptime(rec["received_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
                except (KeyError, ValueError):
                    stamp = int(now or time.time())
                self.record_report(bot_id, rec["report"], stamp)
            imported += 1
        return imported
