import http.client
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
SECRET = "k" * 40


def load_server():
    spec = importlib.util.spec_from_file_location("telemetry_server_under_test", SERVER_DIR / "telemetry_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.Handler.log_request = lambda *a, **k: None   # quiet tests
    return mod


S = load_server()
auth, panel, store_mod = S.auth, S.panel, S.Store.__module__ and sys.modules[S.Store.__module__]
Store = S.Store


class MockYapin:
    """A stand-in for Yapin's OAuth endpoints."""

    def __init__(self, user=None):
        outer = self
        self.user = user or {"id": "777", "username": "Mock User"}
        self.fail_token = False

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                if outer.fail_token or form.get("code") != ["goodcode"] or form.get("client_secret") != ["shh"]:
                    return self._json(400, {"error": "invalid_grant"})
                self._json(200, {"access_token": "at_1"})

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer at_1":
                    return self._json(401, {})
                self._json(200, outer.user)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def oauth(self, redirect_uri):
        return auth.OAuth("cid", "shh", self.base + "/authorize", self.base + "/token", self.base + "/userinfo", redirect_uri)

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class Case(unittest.TestCase):
    require_key = True
    dev_login = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mock = MockYapin()
        self.addCleanup(self.mock.close)
        self.cfg = S.Config(SECRET, os.path.join(self.tmp.name, "t.db"), owners=["boss"], support_url="https://example.test/support",
                            panel_url="", require_key=self.require_key, dev_login=self.dev_login)
        self.store = self.cfg.store
        self.addCleanup(self.store._db.close)
        self.srv = S.make_server(self.cfg, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.cfg.oauth = self.mock.oauth(f"http://127.0.0.1:{self.port}/auth/callback")

    # -- helpers --
    def call(self, method, path, body=None, headers=None, raw=None, user=None):
        headers = dict(headers or {})
        if user:
            headers["Cookie"] = f"{auth.SESSION_COOKIE}={auth.make_session(self.cfg.secret, user, 'Name ' + user)}"
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        return resp.status, parsed, text, resp

    def csrf(self, user):
        value = auth.make_session(self.cfg.secret, user, "x")
        return value, auth.read_session(self.cfg.secret, value).csrf

    def act(self, user, path, **form):
        """A form post from the panel, as the logged-in user."""
        cookie, token = self.csrf(user)
        body = urllib.parse.urlencode(dict(form, csrf=token))
        return self.call("POST", path, raw=body, headers={"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "Content-Type": "application/x-www-form-urlencoded"})

    def view(self, user, path="/"):
        return self.call("GET", path, user=user)[2]

    def approve(self, bot_id="b1", owner="own1", name=None):
        return self.store.approve(bot_id, owner, "boss", name)

    def token(self, bot_id="b1", **extra):
        return self.call("POST", "/", dict({"bot_id": bot_id}, **extra))

    def report(self, token, bot_id="b1", **extra):
        rep = {"schema": 1, "bot": {"id": bot_id, "username": "Real Name"}, "servers": {"count": 3, "members_total": 99, "online_total": 9},
               "commands": [{"name": "ping", "description": "SECRET-COMMAND-TEXT", "options": []}], "sdk": {"version": "1.8.4", "package_version": "1.8.4"}}
        rep.update(extra)
        return self.call("POST", "/report", rep, {"Authorization": f"Bearer {token}"})


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"])

    def test_who_can_give_which_role(self):
        self.s.set_role("boss", "m1", "manager")
        self.s.set_role("boss", "a1", "approver")
        self.s.set_role("m1", "a2", "approver")
        self.assertEqual({m["user_id"]: m["role"] for m in self.s.staff()}, {"boss": "owner", "m1": "manager", "a1": "approver", "a2": "approver"})
        with self.assertRaises(PermissionError):
            self.s.set_role("m1", "m2", "manager")
        with self.assertRaises(PermissionError):
            self.s.set_role("a1", "a3", "approver")
        with self.assertRaises(PermissionError):
            self.s.set_role("nobody", "a3", "approver")

    def test_managers_cannot_change_existing_managers(self):
        self.s.set_role("boss", "m1", "manager")
        self.s.set_role("boss", "m2", "manager")
        with self.assertRaises(PermissionError):
            self.s.set_role("m1", "m2", "approver")

    def test_only_owners_remove_roles(self):
        self.s.set_role("boss", "m1", "manager")
        self.s.set_role("boss", "a1", "approver")
        with self.assertRaises(PermissionError):
            self.s.remove_role("m1", "a1")
        self.s.remove_role("boss", "a1")
        self.assertIsNone(self.s.role_of("a1"))
        with self.assertRaises(ValueError):
            self.s.remove_role("boss", "a1")

    def test_configured_owners_are_fixed_and_bad_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.s.set_role("boss", "boss", "manager")
        with self.assertRaises(ValueError):
            self.s.set_role("console", "boss", "manager")
        with self.assertRaises(ValueError):
            self.s.set_role("boss", "not a valid id!", "approver")
        with self.assertRaises(PermissionError):
            self.s.set_role("boss", "x1", "owner")
        with self.assertRaises(ValueError):
            self.s.remove_role("boss", "boss")

    def test_console_acts_as_owner(self):
        self.s.set_role(store_mod.CONSOLE, "m1", "manager")
        self.s.remove_role(store_mod.CONSOLE, "m1")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"])

    def test_pending_then_approved_with_a_stable_private_key(self):
        bot = self.s.see_bot("b1", "Name", "u1", {"version": "1.8.4"}, 1800)
        self.assertEqual((bot["status"], bot["key"], bot["claimed_owner_id"]), ("pending", None, "u1"))
        self.assertEqual([b["bot_id"] for b in self.s.waiting_for("u1")], ["b1"])
        approved = self.s.approve("b1", "u1", "boss")
        self.assertTrue(approved["key"].startswith("tk_") and len(approved["key"]) > 30)
        self.assertEqual(self.s.approve("b1", "u2", "boss")["key"], approved["key"])   # approving again never changes the key
        self.assertEqual(self.s.waiting_for("u1"), [])
        self.assertEqual([b["bot_id"] for b in self.s.owned_by("u2")], ["b1"])

    def test_keys_are_checked_in_constant_time_and_never_empty(self):
        self.s.approve("b1", "u1", "boss")
        key = self.s.get_bot("b1")["key"]
        self.assertTrue(self.s.check_key("b1", key))
        self.assertTrue(self.s.check_key("b1", f"  {key}\n"))
        self.assertFalse(self.s.check_key("b1", key + "x"))
        self.assertFalse(self.s.check_key("b1", ""))
        self.assertFalse(self.s.check_key("b1", None))
        self.assertFalse(self.s.check_key("missing", key))

    def test_names_come_from_what_the_bot_sends(self):
        self.s.see_bot("b1", "From Token", None, None, None)
        self.assertEqual(self.s.get_bot("b1")["name"], "From Token")
        self.s.approve("b1", "u1", "boss")
        self.s.record_report("b1", {"bot": {"id": "b1", "username": "From Report"}, "sdk": {"version": "2.0"}})
        bot = self.s.get_bot("b1")
        self.assertEqual((bot["name"], bot["sdk_version"]), ("From Report", "2.0"))
        self.assertEqual(bot["last_report"]["bot"]["username"], "From Report")

    def test_claimed_owner_never_overrides_the_real_one(self):
        self.s.see_bot("b1", None, "liar", None, None)
        self.s.approve("b1", "real", "boss")
        self.s.see_bot("b1", None, "liar2", None, None)
        self.assertEqual(self.s.get_bot("b1")["owner_id"], "real")
        self.assertEqual(self.s.waiting_for("liar"), [])

    def test_pending_bots_per_claimed_owner_are_capped(self):
        for i in range(15):
            self.s.see_bot(f"b{i}", None, "spammer", None, None)
        self.assertEqual(len(self.s.waiting_for("spammer")), store_mod.MAX_PENDING_PER_OWNER)

    def test_report_history_is_trimmed(self):
        self.s.approve("b1", "u1", "boss")
        for i in range(store_mod.KEEP_REPORTS + 20):
            self.s.record_report("b1", {"bot": {"id": "b1"}, "n": i})
        self.assertEqual(len(self.s._all("SELECT id FROM reports")), store_mod.KEEP_REPORTS)
        self.assertEqual(self.s.history("b1", 1)[0]["report"]["n"], store_mod.KEEP_REPORTS + 19)

    def test_interval_is_clamped(self):
        self.assertEqual(self.s.see_bot("a", None, None, None, 5)["interval"], 300)
        self.assertEqual(self.s.see_bot("b", None, None, None, 10 ** 9)["interval"], 86400)
        self.assertEqual(self.s.see_bot("c", None, None, None, "nope")["interval"], 1800)

    def test_states(self):
        now = 1_000_000
        bot = {"status": "pending", "last_report_at": None, "interval": 1800}
        self.assertEqual(store_mod.bot_state(bot, now)[0], "pending")
        self.assertEqual(store_mod.bot_state(dict(bot, status="approved"), now)[0], "waiting")
        self.assertEqual(store_mod.bot_state(dict(bot, status="approved", last_report_at=now - 1000), now)[0], "active")
        self.assertEqual(store_mod.bot_state(dict(bot, status="approved", last_report_at=now - 5000), now)[0], "quiet")
        self.assertEqual(store_mod.bot_state(dict(bot, status="revoked"), now)[0], "removed")

    def test_revoke_and_owner_changes(self):
        with self.assertRaises(ValueError):
            self.s.revoke("nope")
        self.s.approve("b1", "u1", "boss")
        self.s.set_owner("b1", "u2")
        self.assertEqual(self.s.get_bot("b1")["owner_id"], "u2")
        self.s.revoke("b1")
        self.assertEqual(self.s.get_bot("b1")["status"], "revoked")

    def test_importing_the_first_versions_files(self):
        with tempfile.TemporaryDirectory() as d:
            approved, data = os.path.join(d, "a.json"), os.path.join(d, "r.jsonl")
            json.dump({"bots": {"old1": {"name": "Old"}, "old2": {}}}, open(approved, "w"))
            open(data, "w").write(json.dumps({"received_at": "2026-01-01T00:00:00+00:00", "report": {"bot": {"id": "old1", "username": "Old Bot"}}}) + "\n")
            self.assertEqual(self.s.import_legacy(approved, data), 2)
            self.assertEqual(self.s.import_legacy(approved, data), 0)
        bot = self.s.get_bot("old1")
        self.assertEqual((bot["status"], bot["name"], bot["owner_id"]), ("approved", "Old Bot", None))
        self.assertTrue(bot["key"])
        self.assertIsNone(self.s.import_legacy("/nope/missing.json") or None)


class TokenApiTests(Case):
    def test_unknown_bot_is_pending_and_told_how_to_get_approved(self):
        status, body, _, _ = self.token("b1", username="Cool", owner_id="u1")
        self.assertEqual(status, 403)
        self.assertEqual((body["error"], body["status"]), ("not_approved", "pending"))
        self.assertIn("https://example.test/support", body["message"])
        self.assertIn("proof", body["message"])
        self.assertEqual(self.store.get_bot("b1")["status"], "pending")
        self.assertEqual(self.store.get_bot("b1")["name"], "Cool")

    def test_bot_id_is_required(self):
        for body in ({}, {"bot_id": ""}, {"bot_id": "has space"}, {"bot_id": "x" * 100}):
            status, data, _, _ = self.call("POST", "/", body)
            self.assertEqual((status, data["error"]), (400, "bot_id_required"))

    def test_approved_bot_must_send_its_private_key(self):
        self.approve()
        status, body, _, _ = self.token()
        self.assertEqual((status, body["error"]), (401, "key_required"))
        self.assertIn("TELEMETRY_KEY", body["message"])
        status, body, _, _ = self.token(key="tk_wrong")
        self.assertEqual((status, body["error"]), (401, "key_invalid"))
        status, body, _, _ = self.token(key=self.store.get_bot("b1")["key"])
        self.assertEqual(status, 200)
        self.assertEqual(S.read_token(self.cfg.secret, body["token"]), "b1")

    def test_revoked_bot_is_told_so(self):
        self.approve()
        self.store.revoke("b1")
        status, body, _, _ = self.token(key=self.store.get_bot("b1")["key"])
        self.assertEqual((status, body["error"]), (403, "revoked"))

    def test_get_with_a_bot_id_no_longer_returns_a_token(self):
        self.approve()
        status, body, text, resp = self.call("GET", f"/?bot_id=b1&key={self.store.get_bot('b1')['key']}")
        self.assertEqual(status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        self.assertNotIn("expires_in", text)

    def test_reports_need_a_token_for_the_same_bot(self):
        self.approve()
        self.approve("b2", "own2")
        token = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.assertEqual(self.call("POST", "/report", {"schema": 1, "bot": {"id": "b1"}})[0], 401)
        self.assertEqual(self.report(token, "b2")[0], 403)          # token is for b1
        self.assertEqual(self.report(token + "x")[0], 401)
        self.assertEqual(self.report(token)[0], 204)
        bot = self.store.get_bot("b1")
        self.assertEqual(bot["name"], "Real Name")                    # the name is set from what the bot sent
        self.assertEqual(bot["last_report"]["servers"]["count"], 3)

    def test_revoking_stops_reports_with_a_live_token(self):
        self.approve()
        token = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.store.revoke("b1")
        self.assertEqual(self.report(token)[0], 403)

    def test_bad_bodies(self):
        self.assertEqual(self.call("POST", "/", raw="{nope")[0], 400)
        self.assertEqual(self.call("POST", "/", raw="[1]")[0], 400)
        self.assertEqual(self.call("POST", "/", raw="x" * (S.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.call("POST", "/nothing", {})[0], 404)
        self.assertEqual(self.call("GET", "/health")[1], {"ok": True})

    def test_token_requests_are_rate_limited(self):
        self.cfg.auth_limit = S.RateLimiter(3, 60)
        codes = [self.token("b1")[0] for _ in range(5)]
        self.assertEqual(codes, [403, 403, 403, 429, 429])

    def test_tokens_cannot_be_forged_or_reused_across_purposes(self):
        self.assertIsNone(S.read_token(self.cfg.secret, S.make_token(b"other" * 10, "b1", 60)))
        self.assertIsNone(S.read_token(self.cfg.secret, S.make_token(self.cfg.secret, "b1", -5)))
        session = auth.make_session(self.cfg.secret, "boss", "x")
        self.assertIsNone(S.read_token(self.cfg.secret, session))


class OldClientTests(Case):
    require_key = False

    def test_clients_without_a_key_work_when_the_key_is_not_required(self):
        self.approve()
        self.assertEqual(self.token()[0], 200)
        self.assertEqual(self.token(key="tk_wrong")[0], 401)   # a wrong key is still refused


class LandingTests(Case):
    def test_everyone_else_gets_the_landing_page(self):
        status, _, text, resp = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        for needle in ("What is sent", "What is never sent", "How the data is used", "feature", "TELEMETRY_KEY", "telemetry.preview(bot)", "https://example.test/support"):
            self.assertIn(needle, text)
        self.assertIn('&quot;schema&quot;: 1', text)   # the example report

    def test_security_headers_and_no_inline_styles(self):
        _, _, text, resp = self.call("GET", "/")
        csp = resp.getheader("Content-Security-Policy")
        nonce = re.search(r"style-src 'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'<style nonce="{nonce}">', text)
        self.assertIn(f'<script nonce="{nonce}">', text)
        self.assertNotIn("style=\"", text)
        self.assertNotRegex(text, r"\son[a-z]+=")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_nonce_changes_per_response(self):
        a = self.call("GET", "/")[3].getheader("Content-Security-Policy")
        b = self.call("GET", "/")[3].getheader("Content-Security-Policy")
        self.assertNotEqual(a, b)

    def test_dark_mode_only(self):
        text = self.call("GET", "/")[2]
        self.assertIn("color-scheme:dark", text)
        self.assertNotIn("prefers-color-scheme: light", text)

    def test_other_paths_and_html_only_on_get_root(self):
        self.assertEqual(self.call("GET", "/panel")[0], 404)
        self.assertEqual(self.call("GET", "/panel/key")[0], 404)
        self.assertIsNone(self.call("POST", "/", {"bot_id": "b1"})[2] and None)
        self.assertTrue(self.call("POST", "/", {"bot_id": "b1"})[3].getheader("Content-Type").startswith("application/json"))

    def test_login_link_depends_on_setup(self):
        self.assertIn('href="/login"', self.call("GET", "/")[2])
        self.cfg.oauth = auth.OAuth()
        text = self.call("GET", "/")[2]
        self.assertNotIn('href="/login"', text)
        self.assertIn("not set up", text)
        self.assertEqual(self.call("GET", "/login")[3].getheader("Location"), "/?msg=login_off")


class LoginTests(Case):
    def start(self):
        resp = self.call("GET", "/login")[3]
        self.assertEqual(resp.status, 303)
        loc = urllib.parse.urlparse(resp.getheader("Location"))
        q = urllib.parse.parse_qs(loc.query)
        self.assertEqual((q["client_id"], q["response_type"], q["scope"]), (["cid"], ["code"], ["identify"]))
        self.assertTrue(q["redirect_uri"][0].endswith("/auth/callback"))
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        return q["state"][0], cookie.split(";")[0]

    def test_full_login_with_yapin(self):
        state, state_cookie = self.start()
        status, _, _, resp = self.call("GET", "/auth/callback?" + urllib.parse.urlencode({"code": "goodcode", "state": state}), headers={"Cookie": state_cookie})
        self.assertEqual((status, resp.getheader("Location")), (303, "/"))
        cookies = resp.msg.get_all("Set-Cookie")
        session = next(c for c in cookies if c.startswith(auth.SESSION_COOKIE + "="))
        self.assertIn("HttpOnly", session)
        sess = auth.read_session(self.cfg.secret, session.split(";")[0].split("=", 1)[1])
        self.assertEqual((sess.user_id, sess.username), ("777", "Mock User"))

    def test_user_id_field_can_be_configured(self):
        self.mock.user = {"user": {"snowflake": 4242, "handle": "nested"}}
        self.cfg.oauth.id_field, self.cfg.oauth.name_field = "user.snowflake", "user.handle"
        state, state_cookie = self.start()
        resp = self.call("GET", "/auth/callback?" + urllib.parse.urlencode({"code": "goodcode", "state": state}), headers={"Cookie": state_cookie})[3]
        session = next(c for c in resp.msg.get_all("Set-Cookie") if c.startswith(auth.SESSION_COOKIE + "="))
        self.assertEqual(auth.read_session(self.cfg.secret, session.split(";")[0].split("=", 1)[1]).user_id, "4242")

    def failed(self, path, cookie=""):
        resp = self.call("GET", path, headers={"Cookie": cookie} if cookie else {})[3]
        self.assertEqual(resp.getheader("Location"), "/?msg=login_failed")
        self.assertFalse(any(c.startswith(auth.SESSION_COOKIE + "=") and "Max-Age=0" not in c for c in resp.msg.get_all("Set-Cookie") or []))

    def test_callback_rejects_bad_state_missing_cookie_and_bad_code(self):
        state, state_cookie = self.start()
        good = urllib.parse.urlencode({"code": "goodcode", "state": state})
        self.failed("/auth/callback?" + good)                                                 # no state cookie: a forged link
        self.failed("/auth/callback?" + urllib.parse.urlencode({"code": "goodcode", "state": state + "x"}), state_cookie)
        self.failed("/auth/callback?" + urllib.parse.urlencode({"code": "goodcode"}), state_cookie)
        self.failed("/auth/callback?" + urllib.parse.urlencode({"code": "badcode", "state": state}), state_cookie)
        self.failed("/auth/callback?" + urllib.parse.urlencode({"state": state}), state_cookie)
        other_state, other_cookie = self.start()
        self.failed("/auth/callback?" + urllib.parse.urlencode({"code": "goodcode", "state": state}), other_cookie)   # state from another login

    def test_provider_without_a_user_id_fails(self):
        self.mock.user = {"username": "no id"}
        state, state_cookie = self.start()
        self.failed("/auth/callback?" + urllib.parse.urlencode({"code": "goodcode", "state": state}), state_cookie)

    def test_session_cookies_cannot_be_forged_or_expire(self):
        self.assertIsNone(auth.read_session(self.cfg.secret, auth.make_session(b"x" * 40, "boss", "n")))
        self.assertIsNone(auth.read_session(self.cfg.secret, auth.make_session(self.cfg.secret, "boss", "n", now=time.time() - 8 * 24 * 3600)))
        self.assertIsNone(auth.read_session(self.cfg.secret, "garbage"))
        value = auth.make_session(self.cfg.secret, "boss", "n")
        body, mac = value.rsplit(".", 1)
        self.assertIsNone(auth.read_session(self.cfg.secret, body + "A." + mac))
        self.assertIsNone(auth.read_session(self.cfg.secret, auth.sign(self.cfg.secret, "bot", {"u": "boss", "c": "x"}, 60)))   # other purpose

    def test_secure_cookies_when_the_panel_is_https(self):
        self.cfg.cookie_secure = True
        self.assertIn("Secure", self.call("GET", "/login")[3].getheader("Set-Cookie"))

    def test_logout_clears_the_cookie(self):
        status, _, _, resp = self.act("boss", "/logout")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", resp.getheader("Set-Cookie"))


class DevLoginTests(Case):
    def test_off_by_default(self):
        self.assertEqual(self.call("GET", "/dev-login?user=boss")[0], 404)

    def test_on_only_for_this_machine(self):
        self.cfg.dev_login = True
        status, _, _, resp = self.call("GET", "/dev-login?user=boss")
        self.assertEqual(status, 303)
        self.assertIn(auth.SESSION_COOKIE, resp.getheader("Set-Cookie"))
        self.assertEqual(self.call("GET", "/dev-login?user=boss", headers={"X-Forwarded-For": "8.8.8.8"})[0], 404)   # came through a proxy
        self.assertEqual(self.call("GET", "/dev-login?user=bad%20id!")[0], 404)


class PanelViewTests(Case):
    def setUp(self):
        super().setUp()
        self.store.set_role("boss", "man", "manager")
        self.store.set_role("boss", "app", "approver")
        self.approve("b1", "own1", "Cool Bot")
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Cool Bot"}, "servers": {"count": 3, "members_total": 99, "online_total": 9},
                                        "commands": [{"name": "ping", "description": "SECRET-COMMAND-TEXT", "options": []}], "sdk": {"version": "1.8.4"}})
        self.store.see_bot("pend", "Pending Bot", "claimer", None, None)

    def test_logging_in_without_access_says_so(self):
        text = self.view("stranger")
        self.assertIn("don't have any bots with telemetry", text)
        self.assertNotIn("Approvals", text)
        self.assertNotIn("Team", text)
        self.assertIn("Log out", text)

    def test_owner_sees_their_bot_data_and_when_the_next_update_is_due(self):
        text = self.view("own1")
        for needle in ("Cool Bot", "Active", "Next expected", "SECRET-COMMAND-TEXT", "Servers", "99"):
            self.assertIn(needle, text)
        self.assertNotIn("Approvals", text)

    def test_private_key_is_not_in_the_page_until_clicked(self):
        key = self.store.get_bot("b1")["key"]
        text = self.view("own1")
        self.assertNotIn(key, text)
        self.assertIn('data-key-for="', text)
        cookie, token = self.csrf("own1")
        hdr = {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token}
        status, body, _, resp = self.call("POST", "/panel/key", {"bot_id": "b1"}, hdr)
        self.assertEqual((status, body), (200, {"key": key}))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIn("Hide key", self.call("GET", "/")[2] + panel.JS)   # the same button hides it again

    def test_only_the_bots_owner_can_fetch_the_key(self):
        for user in ("boss", "man", "app", "claimer", "stranger"):
            cookie, token = self.csrf(user)
            status = self.call("POST", "/panel/key", {"bot_id": "b1"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token})[0]
            self.assertEqual(status, 404, user)
        cookie, _ = self.csrf("own1")
        self.assertEqual(self.call("POST", "/panel/key", {"bot_id": "b1"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}"})[0], 403)        # no CSRF token
        self.assertEqual(self.call("POST", "/panel/key", {"bot_id": "b1"})[0], 403)                                                      # not logged in
        self.assertEqual(self.call("POST", "/panel/key", {"bot_id": "b1"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": "nope"})[0], 403)

    def test_no_key_for_pending_or_removed_bots(self):
        self.store.revoke("b1")
        cookie, token = self.csrf("own1")
        self.assertEqual(self.call("POST", "/panel/key", {"bot_id": "b1"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token})[0], 404)

    def test_bot_that_names_its_owner_shows_status_but_no_data(self):
        text = self.view("claimer")
        self.assertIn("Pending Bot", text)
        self.assertIn("Pending approval", text)
        self.assertNotIn('data-key-for="', text)
        self.assertNotIn("don't have any bots", text)
        self.store.approve("pend", "claimer", "boss")
        self.assertIn("Approved, waiting for the first report", self.view("claimer"))

    def test_staff_see_bots_but_never_the_data_or_keys(self):
        key = self.store.get_bot("b1")["key"]
        for user in ("boss", "man", "app"):
            text = self.view(user, "/?tab=approvals")
            self.assertIn("Cool Bot", text, user)
            self.assertIn("Pending Bot", text, user)
            self.assertNotIn("SECRET-COMMAND-TEXT", text, user)
            self.assertNotIn(key, text, user)
            self.assertNotIn('data-key-for="', text, user)

    def test_tabs_by_role(self):
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', self.view("boss")), ["approvals", "team"])
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', self.view("man")), ["approvals", "team"])
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', self.view("app")), ["approvals"])
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', self.view("own1")), ["bots"])

    def test_staff_who_own_bots_get_an_extra_tab(self):
        self.store.approve("b9", "app", "boss", "Approver's Bot")
        text = self.view("app")
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', text), ["approvals", "bots"])
        self.assertIn("Approver&#x27;s Bot", self.view("app", "/?tab=bots"))
        self.assertIn("Waiting for approval", self.view("app", "/?tab=approvals"))   # can still approve others

    def test_unknown_or_forbidden_tab_falls_back(self):
        self.assertIn("Waiting for approval", self.view("app", "/?tab=team"))
        self.assertIn("Cool Bot", self.view("own1", "/?tab=approvals"))

    def test_team_tab_options_depend_on_role(self):
        owner = self.view("boss", "/?tab=team")
        self.assertIn('<option value="manager">', owner)
        self.assertIn('<option value="approver">', owner)
        self.assertIn("Remove role", owner)
        manager = self.view("man", "/?tab=team")
        self.assertNotIn('<option value="manager">', manager)
        self.assertIn('<option value="approver">', manager)
        self.assertNotIn("Remove role", manager)

    def test_everything_is_escaped(self):
        evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
        self.store.see_bot("evil", evil, "<b>x</b>", None, None)
        self.store.approve("b2", "own1", "boss")
        self.store.record_report("b2", {"bot": {"id": "b2", "username": evil}, "commands": [{"name": evil, "description": evil, "options": []}]})
        for user, path in (("boss", "/?tab=approvals"), ("own1", "/"), ("claimer", "/")):
            text = self.view(user, path)
            self.assertNotIn("<script>alert", text)
            self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.view("own1"))

    def test_messages_only_from_a_fixed_list(self):
        self.assertIn("Bot approved", self.view("boss", "/?tab=approvals&msg=approved"))
        text = self.view("boss", "/?msg=<script>alert(1)</script>")
        self.assertNotIn("alert(1)", text)


class PanelActionTests(Case):
    def setUp(self):
        super().setUp()
        self.store.set_role("boss", "man", "manager")
        self.store.set_role("boss", "app", "approver")
        self.store.see_bot("p1", "Pending", "claimer", None, None)

    def location(self, response):
        return response[3].getheader("Location")

    def test_approving_sets_the_owner_and_creates_a_key(self):
        r = self.act("app", "/panel/approve", bot_id="p1", owner_id="claimer")
        self.assertEqual(self.location(r), "/?tab=approvals&msg=approved")
        bot = self.store.get_bot("p1")
        self.assertEqual((bot["status"], bot["owner_id"], bot["approved_by"]), ("approved", "claimer", "app"))
        self.assertTrue(bot["key"])

    def test_approve_a_bot_that_never_contacted_the_server(self):
        self.act("boss", "/panel/approve", bot_id="fresh", owner_id="u5")
        self.assertEqual(self.store.get_bot("fresh")["status"], "approved")

    def test_approvers_can_only_approve(self):
        self.approve("b1", "o")
        for path, form in (("/panel/revoke", {"bot_id": "b1"}),
                           ("/panel/role", {"user_id": "n1", "role": "approver"}), ("/panel/remove-role", {"user_id": "man"})):
            self.assertIn("msg=denied", self.location(self.act("app", path, **form)), path)
        self.assertEqual(self.store.get_bot("b1")["status"], "approved")
        self.assertEqual(self.store.role_of("man"), "manager")

    def test_people_without_a_role_can_do_nothing(self):
        for path, form in (("/panel/approve", {"bot_id": "p1", "owner_id": "x"}), ("/panel/role", {"user_id": "n1", "role": "approver"})):
            self.assertIn("msg=denied", self.location(self.act("stranger", path, **form)))
        self.assertEqual(self.store.get_bot("p1")["status"], "pending")
        self.assertIsNone(self.store.role_of("n1"))

    def test_owner_owning_a_bot_still_cannot_approve_others_without_a_role(self):
        self.approve("b1", "own1")
        self.assertIn("msg=denied", self.location(self.act("own1", "/panel/approve", bot_id="p1", owner_id="x")))

    def test_managers_can_revoke_and_change_owners_and_add_only_approvers(self):
        self.approve("b1", "o")
        self.assertIn("msg=owner", self.location(self.act("man", "/panel/owner", bot_id="b1", owner_id="o2")))
        self.assertIn("msg=revoked", self.location(self.act("man", "/panel/revoke", bot_id="b1")))
        self.assertIn("msg=role_added", self.location(self.act("man", "/panel/role", user_id="n1", role="approver")))
        self.assertIn("msg=denied", self.location(self.act("man", "/panel/role", user_id="n2", role="manager")))
        self.assertIn("msg=denied", self.location(self.act("man", "/panel/remove-role", user_id="n1")))
        self.assertEqual((self.store.role_of("n1"), self.store.role_of("n2")), ("approver", None))

    def test_owners_add_managers_and_approvers_and_remove_roles(self):
        self.act("boss", "/panel/role", user_id="n1", role="manager")
        self.act("boss", "/panel/role", user_id="n2", role="approver")
        self.assertEqual((self.store.role_of("n1"), self.store.role_of("n2")), ("manager", "approver"))
        self.assertIn("msg=role_removed", self.location(self.act("boss", "/panel/remove-role", user_id="n1")))
        self.assertIsNone(self.store.role_of("n1"))
        self.assertIn("msg=invalid", self.location(self.act("boss", "/panel/role", user_id="bad id!", role="approver")))
        self.assertIn("msg=denied", self.location(self.act("boss", "/panel/role", user_id="n3", role="owner")))

    def test_new_roles_take_effect_right_away(self):
        self.assertIn("msg=denied", self.location(self.act("n1", "/panel/approve", bot_id="p1", owner_id="x")))
        self.act("boss", "/panel/role", user_id="n1", role="approver")
        self.assertIn("msg=approved", self.location(self.act("n1", "/panel/approve", bot_id="p1", owner_id="x")))

    def test_bad_ids_are_rejected(self):
        self.assertIn("msg=invalid", self.location(self.act("boss", "/panel/approve", bot_id="no good!", owner_id="u")))
        self.assertIn("msg=invalid", self.location(self.act("boss", "/panel/approve", bot_id="ok1", owner_id="")))
        self.assertIn("msg=invalid", self.location(self.act("boss", "/panel/revoke", bot_id="never-approved")))

    def test_csrf_and_origin_are_enforced(self):
        cookie, token = self.csrf("boss")
        hdr = {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode({"bot_id": "p1", "owner_id": "x"})
        self.assertIn("msg=csrf", self.call("POST", "/panel/approve", raw=body, headers=hdr)[3].getheader("Location"))
        bad = urllib.parse.urlencode({"bot_id": "p1", "owner_id": "x", "csrf": "wrong"})
        self.assertIn("msg=csrf", self.call("POST", "/panel/approve", raw=bad, headers=hdr)[3].getheader("Location"))
        good = urllib.parse.urlencode({"bot_id": "p1", "owner_id": "x", "csrf": token})
        self.assertEqual(self.call("POST", "/panel/approve", raw=good, headers=dict(hdr, Origin="https://evil.example"))[0], 403)
        self.assertEqual(self.store.get_bot("p1")["status"], "pending")
        # someone else's CSRF token does not work for me
        _, other = self.csrf("man")
        mixed = urllib.parse.urlencode({"bot_id": "p1", "owner_id": "x", "csrf": other})
        self.assertIn("msg=csrf", self.call("POST", "/panel/approve", raw=mixed, headers=hdr)[3].getheader("Location"))
        self.assertEqual(self.call("POST", "/panel/approve", raw=good, headers=dict(hdr, Origin=f"http://127.0.0.1:{self.port}"))[0], 303)
        self.assertEqual(self.store.get_bot("p1")["status"], "approved")

    def test_not_logged_in_posts_do_nothing(self):
        r = self.call("POST", "/panel/approve", raw="bot_id=p1&owner_id=x&csrf=whatever", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r[3].getheader("Location"), "/")
        self.assertEqual(self.store.get_bot("p1")["status"], "pending")

    def test_the_whole_flow_from_first_contact_to_data(self):
        """pending -> owner sees status -> approved -> owner copies key -> bot sends data -> owner sees it."""
        status = self.call("POST", "/", {"bot_id": "flow", "username": "Flow Bot", "owner_id": "own9"})
        self.assertEqual(status[0], 403)
        self.assertIn("Pending approval", self.view("own9"))
        self.act("app", "/panel/approve", bot_id="flow", owner_id="own9")
        self.assertEqual(self.call("POST", "/", {"bot_id": "flow"})[1]["error"], "key_required")
        cookie, token = self.csrf("own9")
        key = self.call("POST", "/panel/key", {"bot_id": "flow"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token})[1]["key"]
        tok = self.call("POST", "/", {"bot_id": "flow", "key": key})[1]["token"]
        self.assertEqual(self.report(tok, "flow")[0], 204)
        page = self.view("own9")
        self.assertIn("Real Name", page)
        self.assertIn("SECRET-COMMAND-TEXT", page)
        self.assertIn("Active", page)


class VerifiedOwnerTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"])

    def test_once_verified_other_bots_claiming_the_id_do_not_show_up(self):
        self.s.see_bot("b1", None, "u1", None, None)
        self.assertEqual(len(self.s.waiting_for("u1")), 1)            # not verified yet: the claim shows a status
        self.s.approve("b1", "u1", "boss")                            # an approver verified u1
        self.s.see_bot("fake", "Imposter", "u1", None, None)
        self.assertEqual(self.s.waiting_for("u1"), [])               # u1 is verified now: claims no longer count
        self.assertTrue(self.s.is_verified_owner("u1"))
        self.assertFalse(self.s.is_verified_owner("u2"))

    def test_verified_owners_apply_while_logged_in_for_more_bots(self):
        self.s.approve("b1", "u1", "boss")
        self.s.apply("u1", "b2")
        self.s.apply("u1", "b3")
        self.assertEqual({b["bot_id"] for b in self.s.waiting_for("u1")}, {"b2", "b3"})
        self.s.approve("b2", "u1", "boss")
        self.assertEqual({b["bot_id"] for b in self.s.owned_by("u1")}, {"b1", "b2"})

    def test_application_rules(self):
        self.s.apply("u1", "b1")
        self.s.apply("u1", "b1")                                      # again is fine
        with self.assertRaises(ValueError):
            self.s.apply("u2", "b1")                                  # someone else got there first
        with self.assertRaises(ValueError):
            self.s.apply("u1", "bad id!")
        self.s.approve("b1", "u1", "boss")
        with self.assertRaises(ValueError):
            self.s.apply("u3", "b1")                                  # already registered
        for i in range(store_mod.MAX_PENDING_PER_OWNER):
            self.s.apply("u4", f"x{i}")
        with self.assertRaises(ValueError):
            self.s.apply("u4", "one-too-many")

    def test_an_application_beats_someone_elses_claim_for_the_same_bot(self):
        self.s.see_bot("b1", None, "claimer", None, None)
        self.s.apply("real", "b1")
        bot = self.s.get_bot("b1")
        self.assertEqual((bot["applicant_id"], bot["claimed_owner_id"]), ("real", "claimer"))
        self.assertEqual([b["bot_id"] for b in self.s.waiting_for("real")], ["b1"])

    def test_rotating_a_key_is_owner_only_and_kills_the_old_key(self):
        self.s.approve("b1", "u1", "boss")
        old = self.s.get_bot("b1")["key"]
        with self.assertRaises(PermissionError):
            self.s.rotate_key("b1", "boss")
        self.s.rotate_key("b1", "u1")
        self.assertFalse(self.s.check_key("b1", old))
        self.assertTrue(self.s.check_key("b1", self.s.get_bot("b1")["key"]))

    def test_old_databases_get_the_new_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            import sqlite3
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE bots(bot_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL DEFAULT 'pending', owner_id TEXT, claimed_owner_id TEXT, key TEXT, "
                      "first_seen INTEGER, last_seen INTEGER, approved_by TEXT, approved_at INTEGER, interval INTEGER NOT NULL DEFAULT 1800, sdk_version TEXT, "
                      "package_version TEXT, last_report_at INTEGER, last_report TEXT)")
            c.commit(); c.close()
            s = Store(path)
            s.apply("u1", "b1")
            self.assertEqual(s.get_bot("b1")["applicant_id"], "u1")
            s._db.close()


class MultipleBotsPanelTests(Case):
    def test_owner_manages_several_bots_and_applies_for_more(self):
        self.approve("b1", "own1", "One")
        self.approve("b2", "own1", "Two")
        text = self.view("own1")
        self.assertEqual(text.count('data-key-for="'), 2)
        self.assertIn("One", text)
        self.assertIn("Two", text)
        r = self.act("own1", "/panel/apply", bot_id="b3")
        self.assertIn("msg=applied", r[3].getheader("Location"))
        page = self.view("own1")
        self.assertIn("b3", page)
        self.assertIn("Pending approval", page)

    def test_staff_see_who_applied_and_the_form_is_prefilled(self):
        self.act("own1", "/panel/apply", bot_id="b3")
        page = self.view("boss", "/?tab=approvals")
        self.assertIn("applied for by", page)
        self.assertIn('value="own1"', page)

    def test_applications_need_login_and_csrf(self):
        r = self.call("POST", "/panel/apply", raw="bot_id=zzz&csrf=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r[3].getheader("Location"), "/")
        self.assertIsNone(self.store.get_bot("zzz"))
        self.assertIn("msg=invalid", self.act("own1", "/panel/apply", bot_id="not valid!")[3].getheader("Location"))

    def test_owner_can_make_a_new_key_only_for_their_own_bot(self):
        self.approve("b1", "own1")
        old = self.store.get_bot("b1")["key"]
        self.assertIn("msg=denied", self.act("someone", "/panel/rotate-key", bot_id="b1")[3].getheader("Location"))
        self.assertIn("msg=denied", self.act("boss", "/panel/rotate-key", bot_id="b1")[3].getheader("Location"))
        self.assertEqual(self.store.get_bot("b1")["key"], old)
        self.assertIn("msg=rotated", self.act("own1", "/panel/rotate-key", bot_id="b1")[3].getheader("Location"))
        self.assertNotEqual(self.store.get_bot("b1")["key"], old)
        self.assertEqual(self.token("b1", key=old)[1]["error"], "key_invalid")

    def test_an_imposter_claiming_a_verified_owner_sees_nothing_and_the_owner_sees_no_fake(self):
        self.approve("b1", "own1")
        self.call("POST", "/", {"bot_id": "fake", "username": "Fake", "owner_id": "own1"})
        self.assertNotIn("Fake", self.view("own1"))
        self.assertNotIn("Fake</h3>", self.view("imposter"))


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"])
        self.s.approve("b1", "own", "boss")

    def test_owner_shares_as_viewer_or_manager(self):
        self.s.share("own", "b1", "v1", "viewer")
        self.s.share("own", "b1", "m1", "manager")
        self.assertEqual([self.s.access_level("b1", u) for u in ("own", "m1", "v1", "nobody")], ["owner", "manager", "viewer", None])
        self.assertEqual([b["bot_id"] for b in self.s.shared_with("v1")], ["b1"])
        self.assertEqual(self.s.shared_with("v1")[0]["level"], "viewer")

    def test_managers_add_viewers_only_and_viewers_nothing(self):
        self.s.share("own", "b1", "m1", "manager")
        self.s.share("own", "b1", "v1", "viewer")
        self.s.share("m1", "b1", "v2", "viewer")
        with self.assertRaises(PermissionError):
            self.s.share("m1", "b1", "m2", "manager")
        with self.assertRaises(PermissionError):
            self.s.share("v1", "b1", "v3", "viewer")
        with self.assertRaises(PermissionError):
            self.s.share("boss", "b1", "v3", "viewer")      # staff are not owners of the bot
        with self.assertRaises(PermissionError):
            self.s.share("own", "b1", "v3", "owner")
        self.s.share("own", "b1", "m3", "manager")
        with self.assertRaises(PermissionError):
            self.s.share("m1", "b1", "m3", "viewer")       # a manager can't demote another manager

    def test_removing_access(self):
        self.s.share("own", "b1", "m1", "manager")
        self.s.share("own", "b1", "v1", "viewer")
        with self.assertRaises(PermissionError):
            self.s.unshare("v1", "b1", "m1")
        with self.assertRaises(PermissionError):
            self.s.unshare("m1", "b1", "own")
        self.s.unshare("m1", "b1", "v1")
        self.s.unshare("m1", "b1", "m1")                    # leaving
        self.assertEqual(self.s.members("b1"), [])
        with self.assertRaises(ValueError):
            self.s.unshare("own", "b1", "ghost")

    def test_bad_requests_and_limits(self):
        for user in ("own", "bad id!"):
            with self.assertRaises(ValueError):
                self.s.share("own", "b1", user, "viewer")
        for i in range(store_mod.MAX_MEMBERS):
            self.s.share("own", "b1", f"u{i}", "viewer")
        with self.assertRaises(ValueError):
            self.s.share("own", "b1", "one-more", "viewer")
        self.s.share("own", "b1", "u0", "manager")           # changing an existing person is fine at the limit

    def test_pending_and_removed_bots_cannot_be_shared(self):
        self.s.see_bot("p", None, None, None, None)
        with self.assertRaises(PermissionError):
            self.s.share("own", "p", "v1", "viewer")
        self.s.share("own", "b1", "v1", "viewer")
        self.s.revoke("b1")
        self.assertIsNone(self.s.access_level("b1", "v1"))
        self.assertEqual(self.s.shared_with("v1"), [])

    def test_switching_owner_ends_old_sharing_and_can_replace_the_key(self):
        self.s.share("own", "b1", "v1", "viewer")
        key = self.s.get_bot("b1")["key"]
        self.s.set_owner("b1", "newown")
        self.assertEqual((self.s.access_level("b1", "own"), self.s.access_level("b1", "v1"), self.s.access_level("b1", "newown")), (None, None, "owner"))
        self.assertEqual(self.s.get_bot("b1")["key"], key)
        self.s.set_owner("b1", "newown2", new_key_too=True)
        self.assertNotEqual(self.s.get_bot("b1")["key"], key)

    def test_managers_can_make_new_keys(self):
        self.s.share("own", "b1", "m1", "manager")
        self.s.share("own", "b1", "v1", "viewer")
        self.s.rotate_key("b1", "m1")
        with self.assertRaises(PermissionError):
            self.s.rotate_key("b1", "v1")


class SharingPanelTests(Case):
    def setUp(self):
        super().setUp()
        self.store.set_role("boss", "app", "approver")
        self.approve("b1", "own", "Shared Bot")
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Shared Bot"}, "servers": {"count": 3},
                                        "commands": [{"name": "ping", "description": "SECRET-COMMAND-TEXT", "options": []}]})
        self.key = self.store.get_bot("b1")["key"]

    def key_status(self, user):
        cookie, token = self.csrf(user)
        return self.call("POST", "/panel/key", {"bot_id": "b1"}, {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token})[0]

    def loc(self, r):
        return r[3].getheader("Location")

    def test_owner_shares_through_the_panel(self):
        self.assertIn("msg=shared", self.loc(self.act("own", "/panel/share", bot_id="b1", user_id="v1", level="viewer")))
        self.assertIn("msg=shared", self.loc(self.act("own", "/panel/share", bot_id="b1", user_id="m1", level="manager")))
        self.assertIn("Who can see this bot (2)", self.view("own"))

    def test_viewers_see_data_but_not_the_key(self):
        self.store.share("own", "b1", "v1", "viewer")
        page = self.view("v1")
        for needle in ("Shared Bot", "SECRET-COMMAND-TEXT", "Shared with you as a viewer", "Next expected"):
            self.assertIn(needle, page)
        self.assertNotIn('data-key-for="', page)
        self.assertNotIn("Who can see this bot", page)
        self.assertEqual(self.key_status("v1"), 404)
        self.assertIn("msg=denied", self.loc(self.act("v1", "/panel/rotate-key", bot_id="b1")))
        self.assertIn("msg=denied", self.loc(self.act("v1", "/panel/share", bot_id="b1", user_id="x1", level="viewer")))

    def test_managers_get_the_key_and_can_add_viewers_only(self):
        self.store.share("own", "b1", "m1", "manager")
        page = self.view("m1")
        self.assertIn('data-key-for="', page)
        self.assertIn("Shared with you as a manager", page)
        self.assertEqual(self.key_status("m1"), 200)
        self.assertIn("msg=shared", self.loc(self.act("m1", "/panel/share", bot_id="b1", user_id="v9", level="viewer")))
        self.assertIn("msg=denied", self.loc(self.act("m1", "/panel/share", bot_id="b1", user_id="m9", level="manager")))
        self.assertIn("msg=rotated", self.loc(self.act("m1", "/panel/rotate-key", bot_id="b1")))
        self.assertNotIn('<option value="manager">', page)

    def test_staff_and_strangers_cannot_share_or_see_a_bot_through_sharing(self):
        for user in ("boss", "app", "stranger"):
            self.assertIn("msg=denied", self.loc(self.act(user, "/panel/share", bot_id="b1", user_id="x1", level="viewer")), user)
            self.assertEqual(self.key_status(user), 404)
        self.assertEqual(self.store.members("b1"), [])

    def test_removing_someone_takes_effect_at_once(self):
        self.store.share("own", "b1", "v1", "viewer")
        self.assertIn("SECRET-COMMAND-TEXT", self.view("v1"))
        self.assertIn("msg=unshared", self.loc(self.act("own", "/panel/unshare", bot_id="b1", user_id="v1")))
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("v1"))
        self.assertIn("don't have any bots", self.view("v1"))

    def test_staff_who_are_given_a_bot_get_the_my_bots_tab(self):
        self.store.share("own", "b1", "app", "viewer")
        self.assertEqual(re.findall(r'<a href="/\?tab=(\w+)"', self.view("app")), ["approvals", "bots"])

    def test_approvers_can_switch_the_owner_to_a_new_account(self):
        page = self.view("app", "/?tab=approvals")
        self.assertIn("Switch owner", page)
        self.assertNotIn("Remove access", page)               # approvers still can't remove access
        self.store.share("own", "b1", "v1", "viewer")
        self.assertIn("msg=owner", self.loc(self.act("app", "/panel/owner", bot_id="b1", owner_id="own-new", new_key="1")))
        bot = self.store.get_bot("b1")
        self.assertEqual(bot["owner_id"], "own-new")
        self.assertNotEqual(bot["key"], self.key)
        self.assertIn("SECRET-COMMAND-TEXT", self.view("own-new"))
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("own"))      # the old account is out
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("v1"))
        self.assertIn("msg=denied", self.loc(self.act("stranger", "/panel/owner", bot_id="b1", owner_id="x")))
        self.assertIn("msg=denied", self.loc(self.act("own", "/panel/owner", bot_id="b1", owner_id="x")))   # the bot's owner can't hand it over alone


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"])

    def run_(self, line):
        return S.run_command(self.s, line)

    def test_console_does_everything_without_a_browser(self):
        self.assertIn("0 pending", self.run_("status"))
        self.s.see_bot("b1", "Bot", "u1", None, None)
        self.assertIn("b1", self.run_("list pending"))
        self.assertIn("approved b1", self.run_("approve b1 u1"))
        self.assertIn("b1", self.run_("list"))
        self.assertIn("owner of b1 is now u2", self.run_("owner b1 u2"))
        self.assertIn("removed b1", self.run_("revoke b1"))
        self.assertIn("b1", self.run_("list removed"))
        self.assertIn("u3 is now manager", self.run_("add u3 manager"))
        self.assertIn("u3  manager", self.run_("team"))
        self.assertIn("removed u3", self.run_("remove u3"))

    def test_console_never_prints_private_keys(self):
        self.s.approve("b1", "u1", "boss")
        key = self.s.get_bot("b1")["key"]
        for line in ("list", "approve b1 u1", "status", "team", "help", "owner b1 u1"):
            self.assertNotIn(key, self.run_(line))

    def test_console_errors_and_unknown_commands(self):
        self.assertTrue(self.run_("approve nope").startswith("unknown"))
        self.assertTrue(self.run_("revoke ghost").startswith("error"))
        self.assertTrue(self.run_("add bad! manager").startswith("error"))
        self.assertTrue(self.run_("add u1 owner").startswith("error"))
        self.assertEqual(self.run_(""), "")
        self.assertIn("approve <bot_id> <owner_id>", self.run_("help"))


class ConfigTests(unittest.TestCase):
    def cfg(self, **env):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        env.setdefault("TELEMETRY_DB", os.path.join(d, "t.db"))
        cfg = S.Config.from_env(env)
        self.addCleanup(cfg.store._db.close)
        return cfg

    def test_port_and_host_come_from_the_environment(self):
        self.assertEqual((self.cfg(TELEMETRY_SECRET=SECRET).host, self.cfg(TELEMETRY_SECRET=SECRET).port), ("0.0.0.0", 8080))
        c = self.cfg(TELEMETRY_SECRET=SECRET, SERVER_PORT="25577", SERVER_HOST="127.0.0.1")
        self.assertEqual((c.host, c.port), ("127.0.0.1", 25577))

    def test_bad_ports_and_short_secrets_are_errors(self):
        for port in ("abc", "70000", "-1"):
            with self.assertRaises(ValueError):
                self.cfg(TELEMETRY_SECRET=SECRET, SERVER_PORT=port)
        with self.assertRaises(ValueError):
            self.cfg(TELEMETRY_SECRET="short")

    def test_owners_and_flags_are_read(self):
        c = self.cfg(TELEMETRY_SECRET=SECRET, TELEMETRY_OWNERS=" 1, 2 ,,3", TELEMETRY_PANEL_URL="https://p.example/", TELEMETRY_REQUIRE_KEY="0", TELEMETRY_TRUST_PROXY="1")
        self.assertEqual(c.store.owners, frozenset({"1", "2", "3"}))
        self.assertEqual((c.panel_url, c.cookie_secure, c.require_key, c.trust_proxy), ("https://p.example", True, False, True))
        self.assertEqual(c.oauth.redirect_uri, "https://p.example/auth/callback")
        self.assertFalse(self.cfg(TELEMETRY_SECRET=SECRET, TELEMETRY_PANEL_URL="http://localhost:1").cookie_secure)

    def test_a_missing_secret_is_made_once_and_kept(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        env = {"TELEMETRY_DB": os.path.join(d, "t.db")}
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            a = S.Config.from_env(env)
        b = S.Config.from_env(env)
        self.addCleanup(a.store._db.close)
        self.addCleanup(b.store._db.close)
        self.assertEqual(a.secret, b.secret)
        self.assertEqual(oct(os.stat(env["TELEMETRY_DB"] + ".secret").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(env["TELEMETRY_DB"]).st_mode & 0o777), "0o600")

    def test_startup_notes_warn_about_the_important_gaps(self):
        c = self.cfg(TELEMETRY_SECRET=SECRET)
        notes = " ".join(S.startup_notes(c))
        for needle in ("TELEMETRY_OWNERS", "YAPIN_CLIENT_ID", "Secure"):
            self.assertIn(needle, notes)

    def test_the_port_in_use_is_reported_not_crashed(self):
        import contextlib, io
        busy = S.make_server(self.cfg(TELEMETRY_SECRET=SECRET), "127.0.0.1", 0)
        self.addCleanup(busy.server_close)
        env = {"TELEMETRY_SECRET": SECRET, "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(busy.server_address[1]),
               "TELEMETRY_DB": os.path.join(tempfile.mkdtemp(), "t.db")}
        old = dict(os.environ)
        os.environ.update(env)
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(S.main([]), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn("SERVER_PORT", err.getvalue())


class FilesTests(unittest.TestCase):
    def test_env_example_matches_what_the_code_reads(self):
        example = (SERVER_DIR / ".env.example").read_text()
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z_]+)=", example, re.M))
        source = "".join((SERVER_DIR / f).read_text() for f in ("telemetry_server.py", "telemetry_auth.py"))
        used = set(re.findall(r'\bg\("([A-Z_]+)"', source)) | set(re.findall(r'environ\.get\("([A-Z_]+)"', source))
        used -= {"TELEMETRY_APPROVED", "TELEMETRY_DATA"}   # only for importing the first version's files
        self.assertEqual(used - documented, set(), "read by the code but missing from .env.example")
        self.assertEqual(documented - used, set(), "in .env.example but never read")
        self.assertIn("SERVER_PORT", documented)

    def test_requirements_and_gitignore(self):
        self.assertIn("python-dotenv", (SERVER_DIR / "requirements.txt").read_text())
        ignore = (ROOT / ".gitignore").read_text()
        for needle in ("server/.env", "telemetry.db"):
            self.assertIn(needle, ignore)

    def test_server_does_not_import_from_the_sdk(self):
        for path in SERVER_DIR.glob("*.py"):
            self.assertNotRegex(path.read_text(), r"^\s*(import|from) yapper\b", path.name)

    def test_example_report_is_valid_json_and_in_the_landing_page(self):
        json.dumps(panel.EXAMPLE_REPORT)
        self.assertEqual(panel.EXAMPLE_REPORT["schema"], 1)


if __name__ == "__main__":
    unittest.main()
