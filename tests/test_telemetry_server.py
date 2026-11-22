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
        self.cfg = S.Config(SECRET, os.path.join(self.tmp.name, "t.json"), owners=["boss"], user_id_pattern=r"^[\w-]+$", support_url="https://example.test/support",
                            panel_url="", require_key=self.require_key, dev_login=self.dev_login)
        self.store = self.cfg.store
        self.addCleanup(self.store.close)
        self.srv = S.make_server(self.cfg, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.cfg.oauth = self.mock.oauth(f"http://127.0.0.1:{self.port}/auth/callback")

    # -- helpers --
    def location(self, response):
        return response[3].getheader("Location")

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

    def view(self, user, path="/panel"):
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
        r = self.call("GET", "/panel")
        self.assertEqual((r[0], r[3].getheader("Location")), (303, "/?msg=login_needed"))   # the panel is its own page: log in first
        self.assertEqual(self.call("GET", "/panel/key")[0], 303)   # not logged in: sent to the landing page
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
        self.assertEqual((status, resp.getheader("Location")), (303, "/panel"))
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
        text = self.view("own1", "/bots/b1")
        for needle in ("Cool Bot", "Active", "Next expected", "SECRET-COMMAND-TEXT", "Servers", "99"):
            self.assertIn(needle, text)
        self.assertNotIn("Approvals", text)

    def test_private_key_is_not_in_the_page_until_clicked(self):
        key = self.store.get_bot("b1")["key"]
        text = self.view("own1", "/bots/b1")
        self.assertNotIn(key, text)
        self.assertIn('data-key-for="', text)
        cookie, token = self.csrf("own1")
        hdr = {"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-CSRF-Token": token}
        status, body, _, resp = self.call("POST", "/panel/key", {"bot_id": "b1"}, hdr)
        self.assertEqual((status, body), (200, {"key": key}))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIn("Hide key", self.call("GET", "/panel")[2] + panel.JS)   # the same button hides it again

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
        self.assertIn("Approved, waiting for the first report", self.view("claimer", "/bots/pend"))
        self.assertIn("No report yet", self.view("claimer"))

    def test_staff_see_bots_but_never_the_data_or_keys(self):
        key = self.store.get_bot("b1")["key"]
        for user in ("boss", "man", "app"):
            text = self.view(user, "/panel/approvals")
            self.assertIn("Cool Bot", text, user)
            self.assertIn("Pending Bot", text, user)
            self.assertNotIn("SECRET-COMMAND-TEXT", text, user)
            self.assertNotIn(key, text, user)
            self.assertNotIn('data-key-for="', text, user)

    def test_tabs_by_role(self):
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', self.view("boss")), ["approvals", "people", "activity", "team", "stats", "announce"])
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', self.view("man")), ["approvals", "people", "activity", "team", "stats", "announce"])
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', self.view("app")), ["approvals", "people", "activity"])
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', self.view("own1")), ["bots"])

    def test_staff_who_own_bots_get_an_extra_tab(self):
        self.store.approve("b9", "app", "boss", "Approver's Bot")
        text = self.view("app")
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', text), ["approvals", "bots", "people", "activity"])
        self.assertIn("Approver&#x27;s Bot", self.view("app", "/panel/bots"))
        self.assertIn("Waiting for approval", self.view("app", "/panel/approvals"))   # can still approve others

    def test_unknown_or_forbidden_tab_falls_back(self):
        self.assertIn("Waiting for approval", self.view("app", "/panel/team"))
        self.assertIn("Cool Bot", self.view("own1", "/panel/approvals"))

    def test_team_tab_options_depend_on_role(self):
        owner = self.view("boss", "/panel/team")
        self.assertIn('<option value="manager">', owner)
        self.assertIn('<option value="approver">', owner)
        self.assertIn("Remove role", owner)
        manager = self.view("man", "/panel/team")
        self.assertNotIn('<option value="manager">', manager)
        self.assertIn('<option value="approver">', manager)
        self.assertNotIn("Remove role", manager)

    def test_everything_is_escaped(self):
        evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
        self.store.see_bot("evil", evil, "<b>x</b>", None, None)
        self.store.approve("b2", "own1", "boss")
        self.store.record_report("b2", {"bot": {"id": "b2", "username": evil}, "commands": [{"name": evil, "description": evil, "options": []}]})
        for user, path in (("boss", "/panel/approvals"), ("own1", "/panel"), ("claimer", "/panel")):
            text = self.view(user, path)
            self.assertNotIn("<script>alert", text)
            self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.view("own1"))

    def test_messages_only_from_a_fixed_list(self):
        self.assertIn("Bot approved", self.view("boss", "/panel/approvals?msg=approved"))
        text = self.view("boss", "/panel?msg=<script>alert(1)</script>")
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
        self.assertEqual(self.location(r), "/panel/approvals?msg=approved")
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
        self.assertIn("msg=need_login", self.location(self.act("boss", "/panel/role", user_id="bad id!", role="approver")))   # not an ID: a username that has to log in first
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
        self.assertEqual(r[3].getheader("Location"), "/?msg=login_needed")
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
        self.assertIn("Real Name", self.view("own9"))
        page = self.view("own9", "/bots/flow")
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

class PersistenceTests(unittest.TestCase):
    def test_every_bot_gets_its_own_private_file_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            data = os.path.join(d, "telemetry_data")
            s = Store(path, owners=["boss"])
            s.approve("b1", "u1", "boss", "Name")
            s.approve("b2", "u2", "boss")
            s.set_role("boss", "m1", "manager")
            s.share("u1", "b1", "v1", "viewer")
            s.apply("u3", "b3")
            s.record_report("b1", {"bot": {"id": "b1", "username": "Renamed"}, "servers": {"count": 7}})
            key = s.get_bot("b1")["key"]
            s.close()
            self.assertEqual(sorted(os.listdir(data)), ["b1_data.json", "b2_data.json", "b3_data.json"])
            self.assertEqual(sorted(os.listdir(d)), ["t.json", "telemetry_data"])                       # no temp files left behind
            self.assertEqual(oct(os.stat(data).st_mode & 0o777), "0o700")
            for name in os.listdir(data):
                self.assertEqual(oct(os.stat(os.path.join(data, name)).st_mode & 0o777), "0o600", name)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            one = json.load(open(os.path.join(data, "b1_data.json")))
            self.assertEqual((one["bot"]["key"], one["bot"]["last_report"]["servers"]["count"], list(one["members"])), (key, 7, ["v1"]))
            self.assertNotIn("bots", json.load(open(path)))                                    # roles only; no bots in the main file
            s2 = Store(path, owners=["boss"])
            bot = s2.get_bot("b1")
            self.assertEqual((bot["key"], bot["name"], bot["status"], bot["last_report"]["servers"]["count"]), (key, "Renamed", "approved", 7))
            self.assertEqual((s2.role_of("m1"), s2.access_level("b1", "v1"), s2.get_bot("b3")["applicant_id"]), ("manager", "viewer", "u3"))
            self.assertEqual([b["bot_id"] for b in s2.shared_with("v1")], ["b1"])
            self.assertEqual(s2.counts(), {"approved": 2, "pending": 1})
            self.assertTrue(s2.check_key("b1", key))
            s2.close()

    def test_a_custom_data_folder_and_unusual_ids(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.json"), data_dir=os.path.join(d, "elsewhere"))
            for bot_id in ("123456789012345678", "a.b-c_d", "x:y", "..", "UPPER"):
                s.approve(bot_id, "u1", "boss")
            s.close()
            names = sorted(os.listdir(os.path.join(d, "elsewhere")))
            self.assertIn("123456789012345678_data.json", names)
            self.assertIn("x~3ay_data.json", names)
            self.assertEqual(len(names), 5)
            self.assertEqual(os.listdir(d).count("elsewhere"), 1)
            self.assertFalse(any(os.sep in n for n in names))
            s2 = Store(os.path.join(d, "t.json"), data_dir=os.path.join(d, "elsewhere"))
            self.assertEqual(s2.counts(), {"approved": 5})
            self.assertEqual(s2.get_bot("x:y")["bot_id"], "x:y")
            s2.close()

    def test_changing_one_bot_only_rewrites_that_bots_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.json"))
            s.approve("a", "u1", "boss")
            s.approve("b", "u2", "boss")
            other = os.path.join(d, "telemetry_data", "b_data.json")
            before = (os.stat(other).st_mtime_ns, open(other).read())
            time.sleep(0.02)
            s.rotate_key("a", "u1")
            s.share("u1", "a", "v1", "viewer")
            self.assertEqual((os.stat(other).st_mtime_ns, open(other).read()), before)
            s.close()

    def test_reports_are_written_after_a_short_delay_without_closing(self):
        old, store_mod.SAVE_DELAY = store_mod.SAVE_DELAY, 0.05
        self.addCleanup(setattr, store_mod, "SAVE_DELAY", old)
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.json"))
            s.approve("b1", "u1", "boss")
            s.record_report("b1", {"bot": {"id": "b1"}, "marker": "fresh"})
            self.assertEqual(s.get_bot("b1")["last_report"]["marker"], "fresh")                       # visible at once
            file = os.path.join(d, "telemetry_data", "b1_data.json")
            end = time.time() + 5
            while time.time() < end and "fresh" not in open(file).read():
                time.sleep(0.02)
            self.assertIn("fresh", open(file).read())
            s.close()

    def test_a_staff_file_that_cannot_be_read_is_refused_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            open(path, "w").write("{this is not json")
            with self.assertRaises(RuntimeError):
                Store(path)
            self.assertEqual(open(path).read(), "{this is not json")

    def test_a_bot_file_that_cannot_be_read_is_skipped_and_never_overwritten(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            s = Store(path)
            s.approve("good", "u1", "boss")
            s.approve("bad", "u2", "boss")
            s.close()
            bad = os.path.join(d, "telemetry_data", "bad_data.json")
            open(bad, "w").write("{broken")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                s2 = Store(path)
            self.assertIn("bad_data.json", err.getvalue())
            self.assertEqual(s2.counts(), {"approved": 1})                  # the server still starts, with the other bots
            with self.assertRaises(RuntimeError):
                s2.see_bot("bad", "Name", None, None, None)                # and it won't write over the broken one
            self.assertEqual(open(bad).read(), "{broken")
            s2.close()

    def test_concurrent_use_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.json"))
            errors = []

            def work(n):
                try:
                    for i in range(30):
                        s.see_bot(f"t{n}-{i}", "x", None, None, None)
                        s.approve(f"a{n}-{i}", "u", "boss")
                        s.record_report(f"a{n}-{i}", {"bot": {"id": "x"}})
                except Exception as e:   # pragma: no cover
                    errors.append(e)
            threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            self.assertEqual(s.counts(), {"approved": 120, "pending": 120})
            s.close()
            self.assertEqual(len(os.listdir(os.path.join(d, "telemetry_data"))), 240)


class MultipleBotsPanelTests(Case):
    def test_owner_manages_several_bots_and_applies_for_more(self):
        self.approve("b1", "own1", "One")
        self.approve("b2", "own1", "Two")
        text = self.view("own1")
        self.assertIn('href="/bots/b1"', text)                       # approved bots are cards that open their own page
        self.assertIn('href="/bots/b2"', text)
        for bot in ("b1", "b2"):
            self.assertEqual(self.view("own1", f"/bots/{bot}").count('data-key-for="'), 1)
        self.assertIn("One", text)
        self.assertIn("Two", text)
        r = self.act("own1", "/panel/apply", bot_id="b3")
        self.assertIn("msg=applied", r[3].getheader("Location"))
        page = self.view("own1")
        self.assertIn("b3", page)
        self.assertIn("Pending approval", page)

    def test_staff_see_who_applied_and_the_form_is_prefilled(self):
        self.act("own1", "/panel/apply", bot_id="b3")
        page = self.view("boss", "/panel/approvals")
        self.assertIn("applied for by", page)
        self.assertIn('value="own1"', page)

    def test_applications_need_login_and_csrf(self):
        r = self.call("POST", "/panel/apply", raw="bot_id=zzz&csrf=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r[3].getheader("Location"), "/?msg=login_needed")
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
        self.assertIn("Who can see this bot (2)", self.view("own", "/bots/b1"))

    def test_viewers_see_data_but_not_the_key(self):
        self.store.share("own", "b1", "v1", "viewer")
        page = self.view("v1", "/bots/b1")
        for needle in ("Shared Bot", "SECRET-COMMAND-TEXT", "Shared with you as a viewer", "Next expected"):
            self.assertIn(needle, page)
        self.assertNotIn('data-key-for="', page)
        self.assertNotIn("Who can see this bot", page)
        self.assertEqual(self.key_status("v1"), 404)
        self.assertIn("msg=denied", self.loc(self.act("v1", "/panel/rotate-key", bot_id="b1")))
        self.assertIn("msg=denied", self.loc(self.act("v1", "/panel/share", bot_id="b1", user_id="x1", level="viewer")))

    def test_managers_get_the_key_and_can_add_viewers_only(self):
        self.store.share("own", "b1", "m1", "manager")
        page = self.view("m1", "/bots/b1")
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
        self.assertIn("SECRET-COMMAND-TEXT", self.view("v1", "/bots/b1"))
        self.assertIn("msg=unshared", self.loc(self.act("own", "/panel/unshare", bot_id="b1", user_id="v1")))
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("v1", "/bots/b1"))
        self.assertIn("don't have any bots", self.view("v1"))

    def test_staff_who_are_given_a_bot_get_the_my_bots_tab(self):
        self.store.share("own", "b1", "app", "viewer")
        self.assertEqual(re.findall(r'<a href="/panel/(approvals|bots|people|activity|team|stats|announce)"', self.view("app")), ["approvals", "bots", "people", "activity"])

    def test_approvers_can_switch_the_owner_to_a_new_account(self):
        page = self.view("app", "/panel/approvals")
        self.assertIn("Switch owner", page)
        self.assertNotIn("Remove access", page)               # approvers still can't remove access
        self.store.share("own", "b1", "v1", "viewer")
        self.assertIn("msg=owner", self.loc(self.act("app", "/panel/owner", bot_id="b1", owner_id="own-new", new_key="1")))
        bot = self.store.get_bot("b1")
        self.assertEqual(bot["owner_id"], "own-new")
        self.assertNotEqual(bot["key"], self.key)
        self.assertIn("SECRET-COMMAND-TEXT", self.view("own-new", "/bots/b1"))
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("own", "/bots/b1"))      # the old account is out
        self.assertNotIn("SECRET-COMMAND-TEXT", self.view("v1", "/bots/b1"))
        self.assertIn("msg=denied", self.loc(self.act("stranger", "/panel/owner", bot_id="b1", owner_id="x")))
        self.assertIn("msg=denied", self.loc(self.act("own", "/panel/owner", bot_id="b1", owner_id="x")))   # the bot's owner can't hand it over alone


class PanelFeatureTests(Case):
    """People, stats, announcements, activity, timezones, API keys, the invite link and profile pictures."""

    def setUp(self):
        super().setUp()
        self.store.set_role("boss", "man", "manager")
        self.store.set_role("boss", "app", "approver")
        for uid, name in (("boss", "TheBoss"), ("man", "Manny"), ("app", "Appy"), ("own", "Owny"), ("v1", "Viewy")):
            self.store.record_login(uid, name, "https://files.yapper.website/avatars/" + uid + ".png", display=name.upper())
        self.approve("b1", "own", "Promo Bot")
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Promo Bot"}, "servers": {"count": 3}})

    def test_landing_always_shows_with_a_panel_button_when_logged_in(self):
        out = self.call("GET", "/")[2]
        self.assertIn('href="/login"', out)
        self.assertNotIn('href="/panel"', out)
        mine = self.call("GET", "/", user="own")
        self.assertEqual(mine[0], 200)
        self.assertIn("A small, optional report", mine[2])
        self.assertIn('href="/panel"', mine[2])
        self.assertNotIn('href="/login"', mine[2])

    def test_search_by_username_or_id_and_ids_are_what_gets_saved(self):
        found = self.call("GET", "/panel/search?q=owny", user="app")[1]
        self.assertEqual([u["user_id"] for u in found], ["own"])
        self.assertIn("Owny", self.view("app", "/panel/people?q=owny"))
        self.assertIn("own", self.view("app", "/panel/people?q=ow"))
        self.assertIn("log in to the panel first", self.view("app", "/panel/people?q=nobody-here"))
        self.assertIn("msg=approved", self.location(self.act("app", "/panel/approve", bot_id="b2", owner_id="Viewy")))
        self.assertEqual(self.store.get_bot("b2")["owner_id"], "v1")                      # typed a username, saved the ID
        self.assertIn("msg=need_login", self.location(self.act("app", "/panel/approve", bot_id="b3", owner_id="no one")))
        self.assertIsNone(self.store.get_bot("b3"))
        self.assertEqual(self.call("GET", "/panel/search?q=owny")[0], 403)                # not logged in

    def test_people_who_are_not_staff_only_get_exact_matches(self):
        self.assertEqual(self.call("GET", "/panel/search?q=ow", user="v1")[1], [])
        self.assertEqual([u["user_id"] for u in self.call("GET", "/panel/search?q=Owny", user="v1")[1]], ["own"])

    def test_an_unseen_id_is_allowed_but_a_name_must_log_in_first(self):
        store = Store(":memory:", owners=["boss"])                                         # default: IDs are digits
        self.assertEqual(store.resolve_user("123456789"), "123456789")
        with self.assertRaises(S.UserLookupError) as ctx:
            store.resolve_user("someone")
        self.assertEqual(ctx.exception.code, "need_login")
        store.record_login("42", "someone", None)
        self.assertEqual(store.resolve_user("@Someone"), "42")
        store.record_login("43", "someone", None)
        with self.assertRaises(S.UserLookupError) as ctx:
            store.resolve_user("someone")
        self.assertEqual(ctx.exception.code, "ambiguous")

    def test_stats_are_for_owners_and_managers_only(self):
        self.assertIn("People who logged in", self.view("boss", "/panel/stats"))
        self.assertIn("People who logged in", self.view("man", "/panel/stats"))
        self.assertNotIn("People who logged in", self.view("app", "/panel/stats"))
        self.assertNotIn("People who logged in", self.view("own", "/panel/stats"))
        self.assertEqual(self.store.user_stats()["total"], 5)

    def test_announcements_wait_for_offline_people_until_dismissed(self):
        self.assertIn("msg=announced", self.location(self.act("man", "/panel/announce", audience="all", title="Maintenance", body="Back soon")))
        self.assertIn("msg=announced", self.location(self.act("man", "/panel/announce", audience="one", person="Owny", title="Hi Owny", body="Your bot is featured")))
        text = self.view("own", "/panel/bots")
        self.assertIn("Maintenance", text)
        self.assertIn("Your bot is featured", text)
        self.assertNotIn("Your bot is featured", self.view("v1", "/panel/bots"))
        self.assertIn("Maintenance", self.view("v1", "/panel/bots"))
        self.assertIn("Maintenance", self.view("own", "/settings"))                      # on every page, until dismissed
        top = max(a["id"] for a in self.store.unread_for("own"))
        self.assertIn("/panel", self.location(self.act("own", "/panel/dismiss", upto=str(top), back="/panel")))
        self.assertNotIn("Maintenance", self.view("own", "/panel/bots"))
        self.assertIn("Maintenance", self.view("v1", "/panel/bots"))                     # others still have it
        for user in ("app", "own"):
            self.assertIn("msg=denied", self.location(self.act(user, "/panel/announce", audience="all", title="x", body="y")))

    def test_activity_levels(self):
        self.act("app", "/panel/approve", bot_id="b2", owner_id="v1")
        self.view("man", "/panel/team")                                                  # a manager opens the team tab
        self.view("app", "/panel/approvals")
        owner_view = self.view("boss", "/panel/activity")
        self.assertIn("approved", owner_view)
        self.assertIn("opened the team tab", owner_view)                                 # owners see views
        approver_view = self.view("app", "/panel/activity")
        self.assertIn("approved", approver_view)
        self.assertNotIn("opened the team tab", approver_view)                           # approvers don't get the manager level
        self.assertNotIn("Activity", self.view("own", "/panel/bots").split("<nav", 1)[1].split("</nav>")[0])
        self.assertIn("Activity on this bot", self.view("own", "/bots/b1"))

    def test_bot_owner_sees_what_others_did_to_their_bot(self):
        self.act("own", "/panel/share", bot_id="b1", user_id="Viewy", level="viewer")
        self.act("own", "/panel/invite", bot_id="b1", invite_url="https://www.yapin.gg/invite/promo")
        events = [(e["kind"], e["actor"]) for e in self.store.read_audit(bot_id="b1")]
        self.assertIn(("bot.share", "own"), events)
        self.assertIn(("bot.invite", "own"), events)
        self.assertIn("gave access to", self.view("own", "/bots/b1"))

    def test_times_follow_the_viewers_timezone(self):
        ts = 1_700_000_000                                                               # 2023-11-14 22:13:20 UTC
        self.store.set_timezone("app", "UTC")
        S.panel.set_context(S.panel.Context(self.store, "UTC"))
        self.assertIn("2023-11-14 22:13 UTC", S.panel.when(ts))
        S.panel.set_context(S.panel.Context(self.store, "Asia/Tokyo"))
        self.assertIn("2023-11-15 07:13 JST", S.panel.when(ts))
        S.panel.set_context(S.panel.Context(self.store, "auto"))
        self.assertIn('<time datetime="2023-11-14T22:13:20Z">', S.panel.when(ts))
        S.panel.set_context(S.panel.Context())
        self.assertIn("msg=saved", self.location(self.act("own", "/panel/settings", timezone="America/New_York")))
        self.assertEqual(self.store.timezone_of("own"), "America/New_York")
        self.assertIn("msg=saved", self.location(self.act("own", "/panel/settings", timezone="UTC")))
        self.assertEqual(self.store.timezone_of("own"), "UTC")
        self.assertIn("msg=invalid", self.location(self.act("own", "/panel/settings", timezone="Mars/Base")))
        self.assertIn("America/New York", self.view("own", "/settings"))

    def test_api_keys_read_only_and_for_owned_or_managed_bots(self):
        cookie, token = self.csrf("own")
        r = self.call("POST", "/panel/api-key", raw=urllib.parse.urlencode({"csrf": token, "name": "dash"}), headers={
            "Cookie": f"{auth.SESSION_COOKIE}={cookie}", "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r[0], 200)
        key = re.search(r"uk_[\w-]+", r[2]).group(0)
        self.assertNotIn(key, self.view("own", "/settings"))                             # shown once
        self.assertNotIn(key, json.dumps(self.store._users))                             # only a hash is kept
        hdr = {"Authorization": f"Bearer {key}"}
        bots = self.call("GET", "/api/bots", headers=hdr)[1]["bots"]
        self.assertEqual([b["bot_id"] for b in bots], ["b1"])
        one = self.call("GET", "/api/bots/b1", headers=hdr)[1]
        self.assertEqual(one["report"]["servers"]["count"], 3)
        self.assertEqual(self.call("GET", "/api/bots/b1/history", headers=hdr)[1]["columns"][0], "time")
        self.assertEqual(self.call("GET", "/api/bots/other", headers=hdr)[0], 404)
        self.assertEqual(self.call("POST", "/api/bots", headers=hdr)[0], 404)            # read-only
        self.assertEqual(self.call("GET", "/api/bots")[0], 401)
        self.assertEqual(self.call("GET", "/api/bots", headers={"Authorization": "Bearer uk_nope"})[0], 401)
        # people with a viewer share, and staff, get nothing from their own keys
        self.store.share("own", "b1", "v1", "viewer")
        viewer_key = self.store.create_api_key("v1", "v")
        self.assertEqual(self.call("GET", "/api/bots", headers={"Authorization": f"Bearer {viewer_key}"})[1]["bots"], [])
        staff_key = self.store.create_api_key("boss", "s")
        self.assertEqual(self.call("GET", "/api/bots/b1", headers={"Authorization": f"Bearer {staff_key}"})[0], 404)
        self.store.share("own", "b1", "v1", "manager")
        self.assertEqual([b["bot_id"] for b in self.call("GET", "/api/bots", headers={"Authorization": f"Bearer {viewer_key}"})[1]["bots"]], ["b1"])
        key_id = self.store.api_keys("own")[0]["id"]
        self.assertIn("msg=key_removed", self.location(self.act("own", "/panel/api-key/remove", key_id=str(key_id))))
        self.assertEqual(self.call("GET", "/api/bots", headers=hdr)[0], 401)

    def test_api_key_limit(self):
        for _ in range(S.MAX_API_KEYS if hasattr(S, "MAX_API_KEYS") else 5):
            self.store.create_api_key("own", "k")
        with self.assertRaises(ValueError):
            self.store.create_api_key("own", "one more")

    def test_invite_link_is_saved_by_owners_and_the_readme_link_is_for_staff_only(self):
        for bad in ("https://evil.example/invite/x", "http://www.yapin.gg/invite/x", "https://www.yapin.gg/invite/x y", "javascript:alert(1)"):
            self.assertIn("msg=invalid", self.location(self.act("own", "/panel/invite", bot_id="b1", invite_url=bad)), bad)
        self.assertIn("msg=invite_saved", self.location(self.act("own", "/panel/invite", bot_id="b1", invite_url="https://www.yapin.gg/invite/promo")))
        line = "[**Promo Bot**](https://www.yapin.gg/invite/promo)"
        for page in (self.view("own", "/bots/b1"), self.view("own", "/bots/b1")):
            self.assertIn("https://www.yapin.gg/invite/promo", page)                       # owners see and can change the link...
            self.assertNotIn("**Promo Bot**", page)                                         # ...but not the Markdown
            self.assertNotIn("README link", page)
        self.assertIn(line, self.view("app", "/panel/approvals"))                           # staff get it in the approvals list
        self.assertIn(line, self.view("app", "/panel/b1"))                                  # and on the bot's staff page
        self.assertIn("msg=denied", self.location(self.act("app", "/panel/invite", bot_id="b1", invite_url="https://www.yapin.gg/invite/xx")))
        self.store.share("own", "b1", "v1", "viewer")
        self.assertIn("msg=denied", self.location(self.act("v1", "/panel/invite", bot_id="b1", invite_url="https://www.yapin.gg/invite/xx")))
        self.act("own", "/panel/invite", bot_id="b1", invite_url="")
        self.assertIsNone(self.store.get_bot("b1")["invite_url"])

    def test_bot_data_page_and_who_can_open_it(self):
        self.store.share("own", "b1", "v1", "viewer")
        for user in ("own", "v1"):
            r = self.call("GET", "/bots/b1", user=user)
            self.assertEqual(r[0], 200, user)
            self.assertIn("Promo Bot", r[2])
            self.assertIn('data-live="/bots/b1/live"', r[2])                                # polling is on
        self.assertIn("Private key", self.view("own", "/bots/b1"))
        self.assertNotIn("Private key", self.view("v1", "/bots/b1"))                        # viewers never get the key
        for user in ("app", "man", "boss"):                                                 # the team doesn't get bots' data
            r = self.call("GET", "/bots/b1", user=user)
            self.assertEqual((r[0], r[3].getheader("Location")), (303, "/panel/b1"), user)
        stranger = self.call("GET", "/bots/b1", user="viewer-less")
        self.assertEqual((stranger[0], stranger[3].getheader("Location")), (303, "/panel/bots?msg=denied"))
        self.assertEqual(self.call("GET", "/bots/b1")[3].getheader("Location"), "/?msg=login_needed")
        self.assertEqual(self.call("GET", "/bots/b1/extra/more", user="own")[0], 404)
        self.assertEqual(self.call("GET", "/bots/b1/data", user="own")[0], 404)
        self.assertEqual(self.call("GET", "/bots/bad%20id", user="own")[0], 404)
        self.assertIn('href="/bots/b1"', self.view("own", "/panel/bots"))                    # the list links to it

    def test_bot_data_page_refreshes_with_the_live_fragment(self):
        live = self.call("GET", "/bots/b1/live", user="own")
        self.assertEqual(live[0], 200)
        self.assertTrue(live[2].startswith('<div class="live"'))
        self.assertNotIn("<html", live[2])
        self.assertNotIn("Private key", live[2])                                             # forms and the key stay out of what is swapped in
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Promo Bot"}, "servers": {"count": 99}})
        self.assertIn("<dd>99</dd>", self.call("GET", "/bots/b1/live", user="own")[2])
        self.assertEqual(self.call("GET", "/bots/b1/live", user="v1")[0], 404)               # not shared: no data, no redirect
        self.assertEqual(self.call("GET", "/bots/b1/live", user="app")[0], 404)
        self.assertEqual(self.call("GET", "/bots/b1/live")[0], 303)
        self.assertIn("setTimeout(refresh", panel.JS)
        self.assertIn("document.hidden", panel.JS)                                           # no polling in a hidden tab

    def test_staff_bot_page_shows_report_data_but_never_the_key_and_views_are_logged(self):
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Promo Bot"}, "servers": {"count": 3}, "commands": [{"name": "SECRET-COMMAND-TEXT"}]})
        text = self.view("app", "/panel/b1")
        self.assertIn("Promo Bot", text)
        self.assertIn("SECRET-COMMAND-TEXT", text)                                           # staff can read the report data...
        self.assertIn("Latest report", text)
        self.assertNotIn('data-key-for="', text)                                             # ...but never the key
        self.assertNotIn(self.store.get_bot("b1")["key"], text)
        self.assertIn("viewed", self.view("boss", "/panel/activity"))                        # managers and owners see who looked at which bot
        self.assertNotIn("viewed", self.view("app", "/panel/activity"))                      # approvers don't get the manager level
        self.assertEqual(self.call("GET", "/panel/nope", user="app")[0], 200)                # not a bot: the first tab

    def test_version_facts_are_sdk_and_pypi_only(self):
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1"}, "sdk": {"version": "1.2.3", "package_version": "1.2.0", "yapin_version": "9.9.9"}})
        page = self.view("own", "/bots/b1")
        self.assertIn("SDK version (main.py)", page)
        self.assertIn("<dd>1.2.3</dd>", page)
        self.assertIn("PyPI version", page)
        self.assertIn("<dd>1.2.0</dd>", page)
        self.assertNotIn("<dd>9.9.9</dd>", page)

    def test_bot_pictures_from_paths(self):
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "username": "Promo Bot", "avatar": "abc/pic 1.png"}})
        self.assertEqual(self.store.get_bot("b1")["avatar"], "https://files.yapper.website/avatars/abc/pic%201.png")
        self.assertIn('src="/avatar/b1.png"', self.view("own", "/bots/b1"))
        self.assertIn('src="/avatar/b1.png"', self.view("app", "/panel/b1"))
        for bad in ("../../etc/passwd", "javascript:alert(1)", "//evil.example/x.png", "a" * 400, 5, ""):
            self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "avatar": bad}})
            self.assertEqual(self.store.get_bot("b1")["avatar"], "https://files.yapper.website/avatars/abc/pic%201.png", repr(bad))   # kept the good one
        self.store.record_report("b1", {"schema": 1, "bot": {"id": "b1", "avatar": "/uploads/a.png"}})
        self.assertEqual(self.store.get_bot("b1")["avatar"], "https://www.yapin.gg/uploads/a.png")

    def test_promo_line_is_markdown_with_icon_and_name_and_escapes_everything(self):
        bot = {"bot_id": "5e872d4a-1892-43ce-aaae-40ecd7acd768", "name": 'Sh"a[p]es <x> *_`', "invite_url": None,
               "avatar": "https://files.yapper.website/avatars/a.png"}
        self.assertEqual(panel.promo_line(bot), '[<img src="https://files.yapper.website/avatars/a.png" width="24" height="24" align="absmiddle"> '
                                                '**Sh"a\\[p\\]es \\<x\\> \\*\\_\\`**](https://yapin.gg/bot/5e872d4a-1892-43ce-aaae-40ecd7acd768)')
        self.assertEqual(panel.promo_line(dict(bot, bot_id="12345")), "")                    # not a Yapin' bot ID: needs a saved link
        self.assertEqual(panel.promo_line(dict(bot, name="Plain", avatar=None, invite_url="https://www.yapin.gg/invite/own")),
                         "[**Plain**](https://www.yapin.gg/invite/own)")
        self.assertNotIn("<img", panel.promo_line(dict(bot, avatar="http://tracker.example/x.png")))

    def test_pictures_only_come_from_trusted_hosts(self):
        self.assertIn("https://files.yapper.website/avatars/own.png", self.view("boss", "/panel/people?q=own"))
        self.store.record_login("evil", "Evil", "https://tracker.example/pixel.png")
        text = self.view("boss", "/panel/people?q=evil")
        self.assertNotIn("tracker.example", text)
        csp = self.call("GET", "/", user="boss")[3].getheader("Content-Security-Policy")
        self.assertIn("img-src 'self' data: https://files.yapper.website https://www.yapin.gg", csp)
        self.assertNotIn("tracker.example", csp)

    def test_big_reports_only_after_the_token_checks_out(self):
        self.assertEqual(self.cfg.max_report_bytes, 2 * 1024 ** 3)
        self.cfg.max_report_bytes = 300
        self.store.set_role  # noqa: B018
        key = self.store.get_bot("b1")["key"]
        token = self.call("POST", "/", {"bot_id": "b1", "key": key})[1]["token"]
        big = {"schema": 1, "bot": {"id": "b1"}, "extra": {"x": "y" * 500}}
        self.assertEqual(self.call("POST", "/report", big, {"Authorization": f"Bearer {token}"})[0], 413)
        self.assertEqual(self.call("POST", "/report", raw="x" * 200_000, headers={"Authorization": "Bearer nope"})[0], 401)   # no token, no big body
        self.cfg.max_report_bytes = 10 * 1024 ** 2
        self.assertEqual(self.call("POST", "/report", big, {"Authorization": f"Bearer {token}"})[0], 204)

    def test_console_finds_people_and_leaves_announcements(self):
        self.assertIn("own", S.run_command(self.store, "find owny"))
        self.assertIn("5 people", S.run_command(self.store, "stats"))
        self.assertIn("announcement left", S.run_command(self.store, "announce all Hello | Nice to see you"))
        self.assertTrue(any(a["title"] == "Hello" for a in self.store.unread_for("own")))
        self.assertIn("announcement left for v1", S.run_command(self.store, "announce Viewy Psst | Just you"))
        self.assertIn("announce.send", S.run_command(self.store, "activity"))


class DenyTests(Case):
    """Staff can say no to a waiting bot, with a reason the owner sees."""

    def setUp(self):
        super().setUp()
        self.store.set_role("boss", "app", "approver")
        self.store.set_role("boss", "man", "manager")
        self.store.record_login("own", "Owny", None)
        self.store.apply("own", "pend1")
        self.store.see_bot("pend1", "Pending Bot")

    def test_a_reason_is_required(self):
        for reason in ("", "   ", "no", "x" * 4):
            self.assertIn("msg=reason_needed", self.location(self.act("app", "/panel/deny", bot_id="pend1", reason=reason)), repr(reason))
            self.assertEqual(self.store.get_bot("pend1")["status"], "pending")
        with self.assertRaises(S.ReasonRequired):
            self.store.deny("pend1", "app", "")

    def test_deny_with_a_reason_and_the_owner_sees_it(self):
        self.assertIn("msg=bot_denied", self.location(self.act("app", "/panel/deny", bot_id="pend1", reason="  Not   built on yapper.py  ")))
        bot = self.store.get_bot("pend1")
        self.assertEqual((bot["status"], bot["deny_reason"], bot["denied_by"]), ("denied", "Not built on yapper.py", "app"))
        page = self.view("own", "/panel/bots")
        self.assertIn("Denied", page)
        self.assertIn("Not built on yapper.py", page)
        self.assertNotIn('data-key-for="', page)
        self.assertIn("Not built on yapper.py", self.view("app", "/panel/approvals"))
        self.assertIn("denied", self.view("app", "/panel/activity"))
        self.assertEqual(self.store.counts()["denied"], 1)
        self.assertEqual(self.store.with_status("pending"), [])

    def test_the_bot_is_told_why(self):
        self.act("app", "/panel/deny", bot_id="pend1", reason="Please add proof that yapper.py is your main SDK")
        r = self.call("POST", "/", {"bot_id": "pend1"})
        self.assertEqual(r[0], 403)
        self.assertEqual((r[1]["error"], r[1]["status"]), ("denied", "denied"))
        self.assertIn("Please add proof that yapper.py is your main SDK", r[1]["message"])
        self.assertEqual(self.store.get_bot("pend1")["status"], "denied")                       # asking again doesn't bring it back

    def test_only_waiting_bots_can_be_denied_and_only_by_staff_who_can_approve(self):
        self.approve("b1", "own")
        self.assertIn("msg=invalid", self.location(self.act("app", "/panel/deny", bot_id="b1", reason="Changed my mind")))
        self.assertEqual(self.store.get_bot("b1")["status"], "approved")
        self.assertIn("msg=invalid", self.location(self.act("app", "/panel/deny", bot_id="nobody", reason="Changed my mind")))
        for user in ("own", "v1"):
            self.assertIn("msg=denied", self.location(self.act(user, "/panel/deny", bot_id="pend1", reason="Because I said so")), user)
        self.assertEqual(self.store.get_bot("pend1")["status"], "pending")
        self.assertIn("msg=bot_denied", self.location(self.act("man", "/panel/deny", bot_id="pend1", reason="Because I said so")))

    def test_denied_bots_can_be_approved_later_and_cannot_be_applied_for_again(self):
        self.act("app", "/panel/deny", bot_id="pend1", reason="Missing proof of the SDK")
        with self.assertRaises(ValueError) as ctx:
            self.store.apply("own", "pend1")
        self.assertIn("Missing proof of the SDK", str(ctx.exception))
        self.assertIn("Approve anyway", self.view("app", "/panel/approvals"))
        self.assertIn("msg=approved", self.location(self.act("app", "/panel/approve", bot_id="pend1", owner_id="own")))
        bot = self.store.get_bot("pend1")
        self.assertEqual((bot["status"], bot["owner_id"], bot["deny_reason"]), ("approved", "own", None))

    def test_deny_is_logged_and_on_the_console(self):
        self.act("app", "/panel/deny", bot_id="pend1", reason="Missing proof of the SDK")
        self.assertIn(("bot.deny", "app"), [(e["kind"], e["actor"]) for e in self.store.read_audit(bot_id="pend1")])
        self.store.see_bot("pend2", "Second")
        self.assertIn("denied pend2", S.run_command(self.store, "deny pend2 Spam bot, not on the SDK"))
        self.assertEqual(self.store.get_bot("pend2")["deny_reason"], "Spam bot, not on the SDK")
        self.assertEqual(self.store.get_bot("pend1")["status"], "denied")
        self.assertNotIn("denied pend1", S.run_command(self.store, "deny pend1"))               # no reason: it isn't done again or silently
        self.assertIn("pend2", S.run_command(self.store, "list denied"))
        self.assertIn("2 denied", S.run_command(self.store, "status"))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:", owners=["boss"], id_pattern=r"^[\w-]+$")

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
        env.setdefault("TELEMETRY_DB", os.path.join(d, "t.json"))
        cfg = S.Config.from_env(env)
        self.addCleanup(cfg.store.close)
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
        env = {"TELEMETRY_DB": os.path.join(d, "t.json")}
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            a = S.Config.from_env(env)
        b = S.Config.from_env(env)
        self.addCleanup(a.store.close)
        self.addCleanup(b.store.close)
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
               "TELEMETRY_DB": os.path.join(tempfile.mkdtemp(), "t.json")}
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
        for needle in ("server/.env", "telemetry.json", "telemetry_data"):
            self.assertIn(needle, ignore)

    def test_server_does_not_import_from_the_sdk(self):
        for path in SERVER_DIR.glob("*.py"):
            self.assertNotRegex(path.read_text(), r"^\s*(import|from) yapper\b", path.name)

    def test_example_report_is_valid_json_and_in_the_landing_page(self):
        json.dumps(panel.EXAMPLE_REPORT)
        self.assertEqual(panel.EXAMPLE_REPORT["schema"], 1)


class SupportLinkTests(Case):
    def setUp(self):
        super().setUp()
        self.approve("b1", "own1", name="Dice")

    def test_owner_saves_and_clears_a_support_link(self):
        self.assertIn("msg=support_saved", self.location(self.act("own1", "/panel/support", bot_id="b1", support_url="https://www.yapin.gg/invite/dicehelp")))
        self.assertEqual(self.store.get_bot("b1")["support_url"], "https://www.yapin.gg/invite/dicehelp")
        self.assertIn('href="https://www.yapin.gg/invite/dicehelp"', self.view("own1", "/bots/b1"))
        self.act("own1", "/panel/support", bot_id="b1", support_url="")
        self.assertIsNone(self.store.get_bot("b1")["support_url"])

    def test_only_yapin_invites_and_only_the_owner(self):
        for bad in ("https://evil.test/invite/x", "javascript:alert(1)", "http://www.yapin.gg/invite/x", "https://www.yapin.gg/bot/abc"):
            self.assertIn("msg=invalid", self.location(self.act("own1", "/panel/support", bot_id="b1", support_url=bad)), bad)
        with self.assertRaises(PermissionError):
            self.store.set_support("stranger", "b1", "https://www.yapin.gg/invite/dicehelp")

    def test_description_is_stored_cleaned_and_escaped(self):
        tok = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.report(tok, bot={"id": "b1", "username": "Dice", "description": "Rolls <script>x</script>\n dice"})
        self.assertEqual(self.store.get_bot("b1")["description"], "Rolls <script>x</script> dice")
        page = self.view("own1", "/bots/b1")
        self.assertIn("Rolls &lt;script&gt;x&lt;/script&gt; dice", page)
        self.assertNotIn("<script>x", page)


class BadgeTests(Case):
    """Live SVG badges for READMEs."""

    def setUp(self):
        super().setUp()
        self.fetched = []
        self.cfg.fetch_image = lambda url, hosts, **kw: (self.fetched.append((url, set(hosts))), "data:image/png;base64,AAAA")[1]
        self.approve("b1", "own1", name="<b>Dice</b> & Co")
        self.store.set_invite("own1", "b1", "https://www.yapin.gg/invite/dice")
        tok = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.report(tok, bot={"id": "b1", "username": "<b>Dice</b> & Co", "avatar": "avatar/a.png"})

    def test_stats_badge_is_public_svg(self):
        status, _, text, resp = self.call("GET", "/badge/stats.svg")
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/svg+xml")
        self.assertIn("<svg", text)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_featured_badge_lists_approved_bots_only(self):
        self.store.record_login("own2", "Two", None)
        self.store.apply("own2", "pending1")
        self.store.see_bot("pending1", "Waiting Bot")
        self.store.set_invite("own1", "b1", "")          # shown even without an invite link
        tok = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.report(tok, bot={"id": "b1", "username": "<b>Dice</b>_0220", "description": "Rolls dice & more"})
        status, _, text, resp = self.call("GET", "/badge/featured.svg")
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/svg+xml")
        self.assertIn("&lt;b&gt;Dice&lt;/b&gt;#0220", text)
        self.assertNotIn("_0220", text)
        self.assertIn("Rolls dice &amp; more", text)
        self.assertNotIn("Waiting Bot", text)
        self.assertNotIn("<b>Dice", text)
        self.assertIn("3 servers", text)
        self.assertIn("99 members", text)

    def test_featured_badge_shows_up_to_100_bots(self):
        import telemetry_badge as tb
        svg = tb.featured_badge([(f"Bot{i}", 1, 2, None, None) for i in range(130)]).decode()
        self.assertEqual(svg.count("<rect x"), 100)

    def test_bot_lists_show_small_cards_that_open_the_stats(self):
        tok = self.token(key=self.store.get_bot("b1")["key"])[1]["token"]
        self.report(tok, bot={"id": "b1", "username": "Dice", "description": "Rolls dice"}, commands=[{"name": "SECRET-COMMAND-TEXT"}])
        for path in ("/panel/bots", "/panel"):
            page = self.view("own1", path)
            self.assertIn('class="card', page)
            self.assertIn('href="/bots/b1"', page)
            self.assertIn("Rolls dice", page)
            self.assertIn("Last report", page)
            self.assertNotIn("SECRET-COMMAND-TEXT", page)                    # the stats are on the bot's page
            self.assertNotIn('data-key-for="b1"', page)
        self.assertIn("SECRET-COMMAND-TEXT", self.view("own1", "/bots/b1"))

    def test_bot_pictures_come_through_the_server(self):
        self.assertIn('src="/avatar/b1.png"', self.view("own1", "/bots/b1"))
        status, _, _, resp = self.call("GET", "/avatar/b1.png")
        self.assertEqual((status, resp.getheader("Content-Type")), (200, "image/png"))
        self.assertEqual(self.call("GET", "/avatar/nobody.png")[0], 404)

    def test_any_public_host_is_ok_for_bot_pictures_but_never_private_ones(self):
        import telemetry_badge as tb
        for url in ("https://127.0.0.1/a.png", "https://localhost/a.png", "https://10.0.0.5/a.png", "http://cdn.example/a.png"):
            self.assertIsNone(tb.fetch_image(url, set(), public=True), url)

    def test_big_pictures_are_shrunk_before_embedding(self):
        import base64, io
        from PIL import Image
        import telemetry_badge as tb
        buf = io.BytesIO()
        Image.effect_noise((900, 900), 80).convert("RGB").save(buf, "PNG")
        self.assertGreater(len(buf.getvalue()), tb.MAX_EMBED_BYTES)
        uri = tb._embed(buf.getvalue(), "image/png")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        out = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
        self.assertLessEqual(max(out.size), tb.ICON_PX * 2)

    def test_featured_badge_with_no_bots(self):
        self.assertIn("No approved bots yet", __import__("telemetry_badge").featured_badge([]).decode())

    def test_bot_badge_escapes_the_name(self):
        status, _, text, _ = self.call("GET", "/badge/b1.svg")
        self.assertEqual(status, 200)
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;Dice", text)

    def test_bot_badge_needs_approval_and_an_invite(self):
        self.store.set_invite("own1", "b1", "")
        self.assertEqual(self.call("GET", "/badge/b1.svg")[0], 404)
        self.assertEqual(self.call("GET", "/badge/nobody.svg")[0], 404)
        self.store.set_invite("own1", "b1", "https://www.yapin.gg/invite/dice")
        self.store.record_login("own2", "Two", None)
        self.store.apply("own2", "p1")
        self.assertEqual(self.call("GET", "/badge/p1.svg")[0], 404)

    def test_staff_page_offers_the_live_badge_markdown(self):
        self.store.set_role("boss", "app", "approver")
        page = self.view("app", "/panel/b1")
        self.assertIn(f"/badge/b1.svg)](https://www.yapin.gg/invite/dice)", page)
        self.assertNotIn("<b>Dice", page)

    def test_icon_is_only_fetched_from_allowed_hosts(self):
        self.call("GET", "/badge/b1.svg")
        for _, hosts in self.fetched:
            self.assertEqual(hosts, self.cfg.oauth.image_hosts() | self.cfg.extra_image_hosts)

    def test_fetch_image_rejects_other_hosts_and_schemes(self):
        from telemetry_badge import fetch_image
        for url in ("http://www.yapin.gg/a.png", "https://evil.test/a.png", "https://www.yapin.gg@evil.test/a.png", "", None):
            self.assertIsNone(fetch_image(url, {"www.yapin.gg"}), url)


if __name__ == "__main__":
    unittest.main()
