import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "server" / "telemetry_server.py"
SECRET = "k" * 40


def load_server():
    spec = importlib.util.spec_from_file_location("telemetry_server_under_test", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.Handler.log_request = lambda *a, **k: None   # quiet tests
    return mod


S = load_server()


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.approved_path = os.path.join(self.tmp.name, "approved.json")
        self.data_path = os.path.join(self.tmp.name, "reports.jsonl")
        S.save_approved(self.approved_path, {"bot1": {"name": "Bot One"}})
        self.cfg = S.Config(SECRET, self.approved_path, self.data_path, support_url="https://example.test/support")
        self.srv = S.make_server(self.cfg, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        try:
            return resp.status, json.loads(text) if text else None
        except ValueError:
            return resp.status, text

    def token(self, bot_id="bot1"):
        status, body = self.call("POST", "/", {"bot_id": bot_id})
        self.assertEqual(status, 200, body)
        return body["token"]

    def report(self, bot_id="bot1", **extra):
        return {"schema": 1, "event": "startup", "bot": {"id": bot_id, "username": "x"}, **extra}

    def post_report(self, token, report=None):
        return self.call("POST", "/report", report if report is not None else self.report(),
                         {"Authorization": f"Bearer {token}"} if token else {})


class TokenEndpointTests(ServerCase):
    def test_approved_bot_gets_a_token(self):
        status, body = self.call("POST", "/", {"bot_id": "bot1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["expires_in"], 3600)
        self.assertEqual(S.read_token(SECRET.encode(), body["token"]), "bot1")

    def test_a_plain_get_with_the_bot_id_works_too(self):
        status, body = self.call("GET", "/?bot_id=bot1")
        self.assertEqual(status, 200)
        self.assertIn("token", body)

    def test_unapproved_bot_is_told_to_join_the_support_server_with_its_id_and_proof(self):
        status, body = self.call("POST", "/", {"bot_id": "nobody-9"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "not_approved")
        self.assertNotIn("token", body)
        self.assertEqual(body["support_server"], "https://example.test/support")
        self.assertEqual(body["needed"], ["your bot's ID", "proof that yapper.py is your main SDK"])
        msg = body["message"]
        for needle in ("nobody-9", "https://example.test/support", "support server", "approved list",
                       "bot's ID", "proof that yapper.py is your main SDK"):
            self.assertIn(needle, msg)

    def test_missing_bot_id_asks_for_it(self):
        for body in ({}, {"bot_id": ""}, {"bot_id": "has spaces!"}, {"bot_id": "x" * 65}):
            with self.subTest(body=body):
                status, answer = self.call("POST", "/", body)
                self.assertEqual(status, 400)
                self.assertEqual(answer["error"], "bot_id_required")
                self.assertIn("bot's ID", answer["message"])
                self.assertIn("support server", answer["message"])
        self.assertEqual(self.call("GET", "/")[0], 400)
        self.assertEqual(self.call("POST", "/", raw="not json")[0], 400)
        self.assertEqual(self.call("POST", "/", raw="[1, 2]")[0], 400)

    def test_approving_a_bot_needs_no_restart(self):
        self.assertEqual(self.call("POST", "/", {"bot_id": "late"})[0], 403)
        S.save_approved(self.approved_path, {"bot1": {}, "late": {}})
        os.utime(self.approved_path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.call("POST", "/", {"bot_id": "late"})[0], 200)

    def test_token_requests_are_rate_limited(self):
        self.cfg.auth_limit = S.RateLimiter(3, 60)
        codes = [self.call("POST", "/", {"bot_id": "bot1"})[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/nope", {})[0], 404)


class ReportEndpointTests(ServerCase):
    def test_a_valid_report_is_stored(self):
        status, _ = self.post_report(self.token())
        self.assertEqual(status, 204)
        lines = pathlib.Path(self.data_path).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["report"]["bot"]["id"], "bot1")
        self.assertIn("received_at", record)
        self.assertNotIn("127.0.0.1", lines[0])   # no addresses are kept

    def test_no_token_or_a_bad_token_is_rejected(self):
        self.assertEqual(self.post_report(None)[0], 401)
        self.assertEqual(self.post_report("garbage")[0], 401)
        self.assertEqual(self.post_report("a.b")[0], 401)
        good = self.token()
        payload, sig = good.rsplit(".", 1)
        self.assertEqual(self.post_report(payload + "." + "0" * 64)[0], 401)      # wrong signature
        self.assertEqual(self.post_report(S.make_token(b"x" * 40, "bot1", 3600))[0], 401)   # signed with another secret
        self.assertFalse(os.path.exists(self.data_path))

    def test_an_expired_token_is_rejected(self):
        self.assertEqual(self.post_report(S.make_token(SECRET.encode(), "bot1", 60, now=time.time() - 120))[0], 401)

    def test_token_for_one_bot_cannot_report_for_another(self):
        S.save_approved(self.approved_path, {"bot1": {}, "bot2": {}})
        status, body = self.post_report(self.token("bot1"), self.report("bot2"))
        self.assertEqual((status, body["error"]), (403, "token_mismatch"))
        self.assertFalse(os.path.exists(self.data_path))

    def test_reports_must_look_like_reports(self):
        token = self.token()
        self.assertEqual(self.post_report(token, {"bot": {"id": "bot1"}})[0], 403)      # no schema
        self.assertEqual(self.post_report(token, {"schema": 1})[0], 403)                # no bot
        self.assertEqual(self.call("POST", "/report", raw="nope", headers={"Authorization": f"Bearer {token}"})[0], 400)

    def test_approval_withdrawn_after_the_token_was_made(self):
        token = self.token()
        S.save_approved(self.approved_path, {})
        os.utime(self.approved_path, (time.time() + 5, time.time() + 5))
        status, body = self.post_report(token)
        self.assertEqual((status, body["error"]), (403, "not_approved"))
        self.assertFalse(os.path.exists(self.data_path))

    def test_oversized_reports_are_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/report")
        conn.putheader("Authorization", f"Bearer {self.token()}")
        conn.putheader("Content-Length", str(S.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_reports_are_rate_limited_per_bot(self):
        self.cfg.report_limit = S.RateLimiter(2, 3600)
        token = self.token()
        self.assertEqual([self.post_report(token)[0] for _ in range(3)], [204, 204, 429])


class UnitTests(unittest.TestCase):
    def test_token_roundtrip_expiry_and_tamper(self):
        t = S.make_token(SECRET.encode(), "abc", 100, now=1000)
        self.assertEqual(S.read_token(SECRET.encode(), t, now=1050), "abc")
        self.assertIsNone(S.read_token(SECRET.encode(), t, now=1101))
        self.assertIsNone(S.read_token(b"y" * 40, t, now=1050))
        self.assertIsNone(S.read_token(SECRET.encode(), t[:-1] + ("0" if t[-1] != "0" else "1"), now=1050))
        self.assertIsNone(S.read_token(SECRET.encode(), "", now=1050))

    def test_config_refuses_a_weak_secret(self):
        for bad in ("", "short", "x" * 31):
            with self.assertRaises(ValueError):
                S.Config(bad)

    def test_approved_file_formats(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            self.assertEqual(S.load_approved(path), {})   # missing file: nobody approved
            for content, expect in (('["1", 2]', {"1", "2"}), ('{"bots": ["3"]}', {"3"}),
                                    ('{"bots": {"4": {"name": "x"}, "5": null}}', {"4", "5"})):
                with open(path, "w") as f:
                    f.write(content)
                self.assertEqual(set(S.load_approved(path)), expect, content)

    def test_rate_limiter_window(self):
        r = S.RateLimiter(2, 0.2)
        self.assertEqual([r.allow("k"), r.allow("k"), r.allow("k"), r.allow("other")], [True, True, False, True])
        time.sleep(0.25)
        self.assertTrue(r.allow("k"))


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_approve_list_revoke(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "approved.json")
            old = os.environ.get("TELEMETRY_APPROVED")
            os.environ["TELEMETRY_APPROVED"] = path
            self.addCleanup(lambda: os.environ.pop("TELEMETRY_APPROVED") if old is None else os.environ.update(TELEMETRY_APPROVED=old))
            self.assertEqual(self.run_cli("approve", "123", "--name", "Cool Bot", "--note", "repo seen")[0], 0)
            self.assertEqual(self.run_cli("approve", "456")[0], 0)
            data = json.loads(pathlib.Path(path).read_text())["bots"]
            self.assertEqual(data["123"]["name"], "Cool Bot")
            self.assertEqual(data["123"]["note"], "repo seen")
            self.assertIn("approved_at", data["123"])
            code, out, _ = self.run_cli("list")
            self.assertIn("123 Cool Bot", out)
            self.assertIn("2 approved", out)
            self.assertEqual(self.run_cli("revoke", "123")[0], 0)
            self.assertEqual(self.run_cli("revoke", "123")[0], 1)   # already gone
            self.assertEqual(list(json.loads(pathlib.Path(path).read_text())["bots"]), ["456"])
            self.assertEqual(self.run_cli("approve", "bad id!")[0], 2)


if __name__ == "__main__":
    unittest.main()
