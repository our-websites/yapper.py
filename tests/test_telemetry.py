import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_server():
    spec = importlib.util.spec_from_file_location("telemetry_server", ROOT / "server" / "telemetry_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["telemetry_server"] = mod
    spec.loader.exec_module(mod)
    mod.Handler.log_request = lambda *a, **k: None   # quiet tests
    return mod


server = load_server()


def run(code, extra_env=None):
    """Run Python in a fresh process, with the kill switches cleared unless a test sets them."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("YAPPER_", "DO_NOT_TRACK"))}
    env["PYTHONPATH"] = str(SRC)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)


class Env:
    """A mock Yapin' API (for the bot's own calls) plus the real telemetry server, both on localhost."""

    def __init__(self, approved=("bot1",), ttl=3600):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "telemetry.json")
        self.cfg = server.Config("s" * 40, self.db_path, owners=["boss"], token_ttl=ttl, require_key=False)
        self._reports = []
        real_record = self.cfg.store.record_report
        self.cfg.store.record_report = lambda bot_id, report, now=None: (self._reports.append({"report": report}), real_record(bot_id, report, now))[1]
        self.approve(*approved)
        self.tel = server.make_server(self.cfg, "127.0.0.1", 0)

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body = {"servers": [
                    {"id": "s1", "name": "Secret Server One", "member_count": 10, "online_count": 4},
                    {"id": "s2", "name": "Secret Server Two", "member_count": 32, "online_count": 7},
                ]} if self.path == "/servers" else {}
                data = json.dumps(body).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        self.api = HTTPServer(("127.0.0.1", 0), Api)
        for s in (self.api, self.tel):
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.api_url = f"http://127.0.0.1:{self.api.server_port}"
        self.endpoint = f"http://127.0.0.1:{self.tel.server_address[1]}"

    def approve(self, *ids):
        """Exactly these bots are approved from now on."""
        store = self.cfg.store
        for bot in store.with_status("approved"):
            if bot["bot_id"] not in ids:
                store.revoke(bot["bot_id"])
        for i in ids:
            store.approve(i, "owner1", "boss")

    def reports(self):
        return list(self._reports)

    def wait_for_reports(self, n=1, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.reports()) >= n:
                break
            time.sleep(0.02)
        return self.reports()

    def close(self):
        for s in (self.api, self.tel):
            s.shutdown()
            s.server_close()
        self.cfg.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_bot(env, bot_id="bot1", **kw):
    import yapper

    bot = yapper.Bot("yb_super_secret_token", base=env.api_url, **kw)
    bot.me = yapper.Data({"id": bot_id, "username": "Testy"})
    return bot


class TelemetryCase(unittest.TestCase):
    def setUp(self):
        for var in ("YAPPER_TELEMETRY", "DO_NOT_TRACK"):
            os.environ.pop(var, None)
        from yapper import telemetry
        self.t = telemetry
        self.addCleanup(setattr, telemetry, "ENDPOINT", telemetry.ENDPOINT)
        self.env = Env()
        self.addCleanup(self.env.close)

    def bot_with_stuff(self, bot_id="bot1", **kw):
        import yapper

        bot = make_bot(self.env, bot_id, **kw)
        bot.command("ping", "Is the bot alive?")(lambda m: None)
        bot.command("ban", "Ban someone", options=[{"name": "user", "type": "user", "required": True}, {"name": "reason"}])(lambda m: None)
        bot.on("member_join")(lambda e: None)
        bot.button("vote:")(lambda i: None)

        class Fun(yapper.Cog):
            @yapper.command("joke", "Tell a joke")
            def joke(self, msg): pass

        bot.add_cog(Fun(bot))
        return bot


class NotLoadedByDefaultTests(unittest.TestCase):
    """The whole point: a normal install must never load, start or send anything."""

    def test_importing_and_using_yapper_never_loads_telemetry(self):
        r = run("""
import sys, threading, yapper
bot = yapper.Bot("yb_x", log=False)
bot.command("ping", "x")(lambda m: None)
bot.handle({"id": 1, "type": "message", "data": {"content": "/ping"}})
from yapper import *
print("telemetry" in dir(yapper), "yapper.telemetry" in sys.modules, "telemetry" in yapper.__all__,
      hasattr(bot, "_yapper_telemetry"), [t.name for t in threading.enumerate() if "telemetry" in t.name])
""")
        self.assertEqual(r.stdout.strip(), "True False False False []", r.stderr)

    def test_main_py_does_not_mention_telemetry(self):
        self.assertNotIn("telemetry", (SRC / "yapper" / "main.py").read_text(encoding="utf-8").lower())

    def test_loading_the_module_alone_starts_nothing(self):
        r = run("""
import sys, threading
from yapper import telemetry
print("yapper.telemetry" in sys.modules, threading.active_count())
""")
        self.assertEqual(r.stdout.strip(), "True 1", r.stderr)

    def test_from_yapper_import_telemetry_works(self):
        from yapper import telemetry
        import yapper

        self.assertIs(yapper.telemetry, telemetry)
        self.assertEqual(telemetry.__name__, "yapper.telemetry")
        with self.assertRaises(AttributeError):
            yapper.nope  # the lazy hook only knows about telemetry


class EnableTests(TelemetryCase):
    def test_reports_every_30_minutes_by_default(self):
        self.assertEqual(self.t.DEFAULT_INTERVAL, 1800)
        bot = make_bot(self.env, log=False)
        self.t.enable(bot, endpoint=self.env.endpoint)
        self.assertEqual(bot._yapper_telemetry.interval, 1800)
        self.t.disable(bot)

    def test_interval_has_a_floor(self):
        bot = make_bot(self.env, log=False)
        self.t.enable(bot, endpoint=self.env.endpoint, interval=1)
        self.assertEqual(bot._yapper_telemetry.interval, self.t.MIN_INTERVAL)
        self.t.disable(bot)

    def test_no_endpoint_raises(self):
        self.t.ENDPOINT = ""
        with self.assertRaises(ValueError):
            self.t.enable(make_bot(self.env, log=False))

    def test_endpoint_must_be_https_unless_loopback(self):
        bot = make_bot(self.env, log=False)
        for bad in ("http://example.com/x", "ftp://example.com", "example.com/x", "https://"):
            with self.assertRaises(ValueError, msg=bad):
                self.t.enable(bot, endpoint=bad)
        self.assertTrue(self.t.enable(bot, endpoint="https://example.com/"))
        self.assertEqual(bot._yapper_telemetry.endpoint, "https://example.com")   # no trailing slash
        self.t.disable(bot)

    def test_kill_switches_beat_loading_it(self):
        for var, val in (("YAPPER_TELEMETRY", "0"), ("YAPPER_TELEMETRY", "off"), ("DO_NOT_TRACK", "1")):
            with self.subTest(var=var, val=val):
                os.environ[var] = val
                bot = make_bot(self.env, log=False)
                self.assertFalse(self.t.enable(bot, endpoint=self.env.endpoint))
                self.assertIsNone(getattr(bot, "_yapper_telemetry", None))
                bot.emit("ready", bot.me)
                time.sleep(0.2)
                self.assertEqual(self.env.reports(), [])
                os.environ.pop(var)

    def test_kill_switch_does_not_even_need_an_endpoint(self):
        os.environ["YAPPER_TELEMETRY"] = "0"
        self.t.ENDPOINT = ""
        self.assertFalse(self.t.enable(make_bot(self.env, log=False)))

    def test_enable_is_idempotent_and_prints_a_notice(self):
        bot = make_bot(self.env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.t.enable(bot, endpoint=self.env.endpoint))
            self.assertTrue(self.t.enable(bot, endpoint=self.env.endpoint))
        text = out.getvalue()
        self.assertEqual(text.count("telemetry is ON"), 1)
        self.assertIn("every 30 minutes", text)
        self.assertIn("approved list", text)
        self.assertIn("127.0.0.1", text)
        self.assertNotIn("yb_super_secret_token", text)
        self.t.disable(bot)


class ReportTests(TelemetryCase):
    def test_landing_page_example_has_the_shape_of_a_real_report(self):
        real = self.t.collect(self.bot_with_stuff())
        example = server.panel.EXAMPLE_REPORT
        self.assertEqual(set(example), set(real))
        for key in ("sdk", "runtime", "bot", "servers"):
            self.assertEqual(set(example[key]), set(real[key]), key)
        self.assertEqual(set(example["commands"][0]), set(real["commands"][0]))

    def test_report_contents(self):
        import yapper

        report = self.t.preview(self.bot_with_stuff())
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["event"], "preview")
        self.assertEqual(report["sdk"]["version"], yapper.__version__)
        self.assertIn("python", report["runtime"])
        self.assertEqual(report["bot"], {"id": "bot1", "username": "Testy", "avatar": None, "description": None, "uptime_seconds": None})
        self.assertEqual(report["extra"], {})
        self.assertEqual(set(report["sdk"]), {"version", "package_version"})        # the SDK's (main.py) and PyPI's, nothing else
        self.assertEqual(report["servers"], {"count": 2, "members_total": 42, "online_total": 11})
        self.assertEqual([c["name"] for c in report["commands"]], ["ban", "joke", "ping"])
        ban = report["commands"][0]
        self.assertEqual(ban["description"], "Ban someone")
        self.assertEqual(ban["options"], [{"name": "user", "type": "user", "required": True},
                                          {"name": "reason", "type": None, "required": False}])
        self.assertEqual(report["cogs"], ["Fun"])
        self.assertEqual(report["listeners"], {"member_join": 1})
        self.assertGreaterEqual(report["buttons"], 1)

    def test_extra_data_is_added_removed_and_limited(self):
        bot = self.bot_with_stuff()
        self.assertTrue(self.t.add_data(bot, "games", 12))
        self.assertTrue(self.t.add_data(bot, "cache", lambda: 3))
        self.assertTrue(self.t.add_data(bot, "nested", {"a": [1, 2]}))
        self.assertEqual(self.t.preview(bot)["extra"], {"games": 12, "cache": 3, "nested": {"a": [1, 2]}})
        self.assertFalse(self.t.add_data(bot, "x" * 65, 1))
        self.assertFalse(self.t.add_data(bot, "", 1))
        self.assertFalse(self.t.add_data(bot, "big", "x" * 20000))
        self.assertFalse(self.t.add_data(bot, "bad", object()))
        self.assertTrue(self.t.remove_data(bot, "games"))
        self.assertFalse(self.t.remove_data(bot, "games"))
        self.assertNotIn("games", self.t.preview(bot)["extra"])

    def test_extra_data_has_a_key_limit_and_a_broken_value_never_breaks_the_report(self):
        bot = self.bot_with_stuff()
        for i in range(self.t.MAX_EXTRA_KEYS):
            self.assertTrue(self.t.add_data(bot, f"k{i}", i))
        self.assertFalse(self.t.add_data(bot, "one-too-many", 1))
        self.assertTrue(self.t.add_data(bot, "k0", 99))        # changing an existing one is fine
        self.t.remove_data(bot, "k1")
        self.t.add_data(bot, "boom", lambda: 1 / 0)
        report = self.t.preview(bot)
        self.assertNotIn("boom", report["extra"])
        self.assertEqual(report["extra"]["k0"], 99)

    def test_enable_accepts_extra_and_sends_it(self):
        bot = self.bot_with_stuff("bot1")
        self.t.enable(bot, endpoint=self.env.endpoint, extra={"mood": "great"})
        self.assertEqual(self.t.preview(bot)["extra"], {"mood": "great"})
        self.t.disable(bot)

    def test_avatar_is_only_a_public_https_link(self):
        for me, expected in (({"avatar_url": "https://cdn.example/a.png"}, "https://cdn.example/a.png"), ({"avatar": "https://cdn.example/b.png"}, "https://cdn.example/b.png"),
                             ({"avatar": "http://cdn.example/b.png"}, None), ({"avatar": "javascript:alert(1)"}, None), ({"avatar": "https://x/" + "a" * 400}, None), ({}, None),
                             ({"avatar_path": "abc/def 1.png"}, "https://files.yapper.website/avatars/abc/def%201.png"),
                             ({"avatar": "/uploads/a.png"}, "https://www.yapin.gg/uploads/a.png"), ({"avatar": "data:image/png;base64,AAA"}, None),
                             ({"avatar_url": "http://x/a.png", "avatar_path": "p.png"}, "https://files.yapper.website/avatars/p.png"), ({"avatar": 5}, None)):
            self.assertEqual(self.t._avatar(me), expected)

    def test_description_comes_from_the_profile_or_the_bots_member_info(self):
        class FakeBot:
            def __init__(self, member, servers=({"id": "s1"},)):
                self._member, self._servers = member, servers
            def servers(self):
                return list(self._servers)
            def user(self, uid):
                if isinstance(self._member, dict) and "profile" in self._member:
                    return self._member["profile"]
                raise RuntimeError("no profile")
            def member(self, sid, uid):
                if isinstance(self._member, Exception):
                    raise self._member
                return self._member
        t = self.t
        t._desc_cache.clear()
        self.assertEqual(t._description(FakeBot({}), {"id": "b", "bio": "  Rolls\n dice\x00 "}), "Rolls dice")
        t._desc_cache.clear()
        self.assertEqual(t._description(FakeBot({"profile": {"bio": "From the user profile"}}), {"id": "b"}), "From the user profile")
        t._desc_cache.clear()
        self.assertEqual(t._description(FakeBot({"user": {"bio": "From member info"}}), {"id": "b"}), "From member info")
        t._desc_cache.clear()
        self.assertEqual(t._description(FakeBot({"bio": "x" * 900}), {"id": "b"}), "x" * t.MAX_DESCRIPTION)
        for bad in (RuntimeError("down"), {}, {"bio": 5}, {"about": "ignored", "description": "ignored"}):
            t._desc_cache.clear()
            self.assertIsNone(t._description(FakeBot(bad), {"id": "b"}))
        t._desc_cache.clear()

    def test_report_never_contains_secrets_or_identifying_names(self):
        text = json.dumps(self.t.preview(self.bot_with_stuff()))
        for secret in ("yb_super_secret_token", "Secret Server", '"s1"', '"s2"', "Authorization"):
            self.assertNotIn(secret, text)

    def test_report_survives_the_api_being_down(self):
        bot = self.bot_with_stuff()
        bot.base = "http://127.0.0.1:9"   # nothing listens here
        report = self.t.preview(bot)
        self.assertIsNone(report["servers"])
        self.assertEqual(len(report["commands"]), 3)

    def test_enabling_alone_sends_nothing_then_ready_sends_one_report(self):
        bot = self.bot_with_stuff(log=False)
        self.t.enable(bot, endpoint=self.env.endpoint)
        time.sleep(0.2)
        self.assertEqual(self.env.reports(), [])   # it waits for the bot to be online
        bot.emit("ready", bot.me)
        got = self.env.wait_for_reports(1)
        self.assertEqual(len(got), 1)
        report = got[0]["report"]
        self.assertEqual(report["event"], "startup")
        self.assertEqual(report["bot"]["id"], "bot1")
        self.assertEqual(report["servers"]["count"], 2)
        self.assertIsInstance(report["bot"]["uptime_seconds"], int)
        self.assertNotIn("yb_super_secret_token", json.dumps(got))
        self.t.disable(bot)

    def test_enabling_on_a_running_bot_reports_straight_away(self):
        bot = self.bot_with_stuff(log=False)
        bot.running = True
        self.t.enable(bot, endpoint=self.env.endpoint)
        self.assertEqual(len(self.env.wait_for_reports(1)), 1)
        self.t.disable(bot)

    def test_a_dead_endpoint_never_breaks_the_bot(self):
        bot = self.bot_with_stuff()
        self.t.enable(bot, endpoint="http://127.0.0.1:9")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
            self.assertFalse(bot._yapper_telemetry.send("heartbeat"))
        self.assertEqual(out.getvalue().count("could not send"), 1)   # said once, then quiet
        self.t.disable(bot)

    def test_disable_stops_the_thread_and_listener(self):
        bot = self.bot_with_stuff(log=False)
        self.t.enable(bot, endpoint=self.env.endpoint)
        bot.emit("ready", bot.me)
        self.env.wait_for_reports(1)
        thread = bot._yapper_telemetry._thread
        self.assertTrue(self.t.disable(bot))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(bot._listeners.get("ready", []), [])
        self.assertFalse(self.t.disable(bot))   # already off

    def test_load_extension_turns_it_on_and_unload_turns_it_off(self):
        bot = self.bot_with_stuff(log=False)
        self.t.ENDPOINT = self.env.endpoint
        try:
            bot.load_extension("yapper.telemetry")
            self.assertIsNotNone(bot._yapper_telemetry)
            bot.emit("ready", bot.me)
            got = self.env.wait_for_reports(1)
            self.assertEqual(len(got), 1)
            self.assertNotIn("yapper.telemetry", got[0]["report"]["extensions"])   # it does not report itself
            thread = bot._yapper_telemetry._thread
            bot.unload_extension("yapper.telemetry")
            self.assertIsNone(bot._yapper_telemetry)
            thread.join(5)
            self.assertFalse(thread.is_alive())
        finally:
            sys.modules.setdefault("yapper.telemetry", self.t)   # unload_extension drops it from sys.modules

    def test_works_with_the_single_file_sdk(self):
        """Used next to the loose yapper.py download (no package): the version still comes from the SDK file."""
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(SRC / "yapper" / "main.py", pathlib.Path(d) / "yapper.py")
            shutil.copy(SRC / "yapper" / "telemetry.py", pathlib.Path(d) / "telemetry.py")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("YAPPER_", "DO_NOT_TRACK", "PYTHONPATH"))}
            r = subprocess.run([sys.executable, "-c",
                                "import yapper, telemetry; b = yapper.Bot('yb_x', log=False); "
                                "print(telemetry.preview(b)['sdk']['version'] == yapper.__version__, yapper.__version__)"],
                               cwd=d, env=env, capture_output=True, text=True, timeout=60)
            self.assertTrue(r.stdout.startswith("True "), r.stdout + r.stderr)


class ApprovalTests(TelemetryCase):
    """The token handshake: only approved bots get through, the rest are told how to get approved."""

    def test_private_key_from_the_environment_is_sent_and_asked_for(self):
        self.env.cfg.require_key = True
        bot = self.bot_with_stuff("bot1")
        self.t.enable(bot, endpoint=self.env.endpoint)
        out = io.StringIO()
        os.environ.pop("TELEMETRY_KEY", None)
        with contextlib.redirect_stdout(out):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
        self.assertIn("TELEMETRY_KEY", out.getvalue())
        os.environ["TELEMETRY_KEY"] = "tk_wrong"
        self.addCleanup(os.environ.pop, "TELEMETRY_KEY", None)
        with contextlib.redirect_stdout(out):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
        self.assertIn("is not right", out.getvalue())
        os.environ["TELEMETRY_KEY"] = self.env.cfg.store.get_bot("bot1")["key"]
        self.assertTrue(bot._yapper_telemetry.send("startup"))
        self.assertEqual(len(self.env.reports()), 1)
        self.t.disable(bot)

    def test_owner_id_lets_the_owner_see_a_pending_bot(self):
        bot = self.bot_with_stuff("newbot")
        self.t.enable(bot, endpoint=self.env.endpoint, owner_id="user-5")
        with contextlib.redirect_stdout(io.StringIO()):
            bot._yapper_telemetry.send("startup")
        self.assertEqual([b["bot_id"] for b in self.env.cfg.store.waiting_for("user-5")], ["newbot"])
        self.t.disable(bot)

    def test_unapproved_bot_sends_nothing_and_is_told_how_to_get_approved(self):
        bot = self.bot_with_stuff("stranger-77")
        self.t.enable(bot, endpoint=self.env.endpoint)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
            self.assertFalse(bot._yapper_telemetry.send("heartbeat"))
        text = out.getvalue()
        self.assertEqual(self.env.reports(), [])
        self.assertEqual(text.count("not on the telemetry approved list"), 1)   # said once
        for needle in ("stranger-77", "support server", "bot's ID", "proof", "yapper.py is your main SDK"):
            self.assertIn(needle, text)
        self.t.disable(bot)

    def test_it_asks_again_so_approval_needs_no_restart(self):
        bot = self.bot_with_stuff("newbie")
        self.t.enable(bot, endpoint=self.env.endpoint, interval=300)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
        self.env.approve("bot1", "newbie")
        self.assertTrue(bot._yapper_telemetry.send("heartbeat"))
        self.assertEqual(self.env.reports()[0]["report"]["bot"]["id"], "newbie")
        self.t.disable(bot)

    def test_token_is_reused_between_reports(self):
        calls = []
        real = server.make_token
        server.make_token = lambda *a, **k: calls.append(1) or real(*a, **k)
        self.addCleanup(setattr, server, "make_token", real)
        bot = self.bot_with_stuff(log=False)
        self.t.enable(bot, endpoint=self.env.endpoint)
        self.assertTrue(bot._yapper_telemetry.send("startup"))
        self.assertTrue(bot._yapper_telemetry.send("heartbeat"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.env.reports()), 2)
        self.t.disable(bot)

    def test_a_bad_or_expired_token_is_replaced_automatically(self):
        bot = self.bot_with_stuff(log=False)
        self.t.enable(bot, endpoint=self.env.endpoint)
        reporter = bot._yapper_telemetry
        reporter._token, reporter._token_until = "garbage.token", time.time() + 999
        self.assertTrue(reporter.send("heartbeat"))   # 401 -> asks again -> works
        self.assertNotEqual(reporter._token, "garbage.token")
        self.t.disable(bot)

    def test_approval_withdrawn_stops_reports(self):
        bot = self.bot_with_stuff()
        self.t.enable(bot, endpoint=self.env.endpoint)
        self.assertTrue(bot._yapper_telemetry.send("startup"))
        self.env.approve("someone-else")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(bot._yapper_telemetry.send("heartbeat"))
        self.assertEqual(len(self.env.reports()), 1)
        self.assertIn("access for your bot (bot1) was removed", out.getvalue())
        self.t.disable(bot)

    def test_text_from_the_server_is_cleaned_before_printing(self):
        self.assertEqual(self.t._clean("ok\x1b[31m red\x00\x07 done"), "ok[31m red done")
        self.assertEqual(len(self.t._clean("x" * 5000)), 600)

    def test_an_unexpected_server_answer_is_reported_quietly(self):
        class Broken(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.send_response(503); self.send_header("Content-Length", "0"); self.end_headers()
        broken = HTTPServer(("127.0.0.1", 0), Broken)
        threading.Thread(target=broken.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(broken.server_close)
        self.addCleanup(broken.shutdown)
        bot = self.bot_with_stuff()
        self.t.enable(bot, endpoint=f"http://127.0.0.1:{broken.server_port}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(bot._yapper_telemetry.send("startup"))
        self.assertIn("answered 503", out.getvalue())
        self.t.disable(bot)


if __name__ == "__main__":
    unittest.main()
