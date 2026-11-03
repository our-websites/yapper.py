import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HAS_DOTENV = importlib.util.find_spec("dotenv") is not None


def run(code_or_script, cwd, extra_env=None, script=False):
    """Run Python in a fresh process (the .env is read at import time) with no YAPPER_* variables set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YAPPER_")}
    env["PYTHONPATH"] = str(SRC)
    env.update(extra_env or {})
    args = [sys.executable, str(code_or_script)] if script else [sys.executable, "-c", code_or_script]
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


TOKEN = "import yapper; print(yapper.Bot(log=False).token)"


class PackageExportsTests(unittest.TestCase):
    def test_package_exports_everything_main_lists(self):
        import yapper
        from yapper import main

        for name in main.__all__:
            self.assertTrue(hasattr(yapper, name), name)
            self.assertIn(name, yapper.__all__)

    def test_version_comes_from_main(self):
        import yapper
        from yapper import main

        self.assertEqual(yapper.__version__, main.__version__)
        self.assertEqual(yapper.VERSION, main.__version__)


@unittest.skipUnless(HAS_DOTENV, "python-dotenv is not installed")
class DotenvTests(unittest.TestCase):
    def test_env_file_in_current_folder_is_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / ".env").write_text("YAPPER_BOT_TOKEN=yb_from_dotenv\n")
            r = run(TOKEN, d)
            self.assertEqual(r.stdout.strip(), "yb_from_dotenv", r.stderr)

    def test_env_file_in_parent_folder_is_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / ".env").write_text("YAPPER_BOT_TOKEN=yb_parent\n")
            sub = pathlib.Path(d) / "a" / "b"
            sub.mkdir(parents=True)
            self.assertEqual(run(TOKEN, sub).stdout.strip(), "yb_parent")

    def test_env_file_next_to_the_script_is_loaded_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as elsewhere:
            bot = pathlib.Path(d) / "bot.py"
            bot.write_text(TOKEN + "\n")
            (pathlib.Path(d) / ".env").write_text('YAPPER_BOT_TOKEN="yb_by_script"\n')
            r = run(bot, elsewhere, script=True)
            self.assertEqual(r.stdout.strip(), "yb_by_script", r.stderr)

    def test_real_environment_variable_beats_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / ".env").write_text("YAPPER_BOT_TOKEN=yb_from_file\n")
            r = run(TOKEN, d, {"YAPPER_BOT_TOKEN": "yb_from_env"})
            self.assertEqual(r.stdout.strip(), "yb_from_env")

    def test_no_env_file_still_imports_and_asks_for_a_token(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(TOKEN, d)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("YAPPER_BOT_TOKEN", r.stderr)
            self.assertNotIn("Traceback", r.stderr)  # a plain "pass your token" message, not a crash


class WithoutDotenvTests(unittest.TestCase):
    def test_library_imports_without_python_dotenv(self):
        """Used as one loose file, python-dotenv is optional: importing must not fail without it."""
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / ".env").write_text("YAPPER_BOT_TOKEN=yb_ignored\n")
            code = "import sys; sys.modules['dotenv'] = None; import yapper; print('imported', yapper.__version__)"
            r = run(code, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("imported "))
            # ...and the .env is simply not read, so a token is still required
            r = run("import sys; sys.modules['dotenv'] = None; " + TOKEN, d)
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
