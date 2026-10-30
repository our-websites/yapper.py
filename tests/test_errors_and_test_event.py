import contextlib, importlib.util, io, pathlib, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("yapper_main", ROOT / "src" / "yapper" / "main.py")
y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(y)


def make_bot():
    return y.Bot("yb_test")


class TestEventTests(unittest.TestCase):
    def test_test_event_prints_without_listener(self):
        bot = make_bot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bot.handle({"id": 1, "type": "test", "data": {}})
        self.assertIn("test event received", out.getvalue())

    def test_test_event_still_reaches_listener_and_stays_quiet(self):
        bot = make_bot()
        got = []
        bot.on("test")(lambda e: got.append(e))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bot.handle({"id": 1, "type": "test", "data": {}})
        self.assertEqual(len(got), 1)
        self.assertEqual(out.getvalue(), "")


class RunErrorTests(unittest.TestCase):
    def test_bad_token_is_printed_not_raised_as_traceback(self):
        bot = make_bot()
        def boom(*a, **k):
            raise y.YapperError("bad token", 401)
        bot.api = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            bot.run()
        self.assertIn("could not start (401)", out.getvalue())
        self.assertIn("Settings -> Developer", out.getvalue())


if __name__ == "__main__":
    unittest.main()
