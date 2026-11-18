import pathlib, tomllib, unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MetadataTests(unittest.TestCase):
    def test_pyproject_has_expected_publish_metadata(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["license"], "GPL-3.0-only")
        self.assertIn("python-dotenv>=1.2.4", project["dependencies"])

    def test_voice_extra_installs_the_voice_dependencies(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        voice = data["project"]["optional-dependencies"]["voice"]
        names = {d.split(">")[0].split("=")[0].strip() for d in voice}
        self.assertEqual(names, {"livekit", "cryptography"})
        # ...and they are not hard dependencies: plain `pip install yapper.py` stays light
        hard = {d.split(">")[0].split("=")[0].strip() for d in data["project"]["dependencies"]}
        self.assertFalse(names & hard)

    def test_readme_exists_and_is_not_empty(self):
        readme = ROOT / "README.md"
        self.assertTrue(readme.exists())
        self.assertGreater(len(readme.read_text(encoding="utf-8").strip()), 0)

    def test_readme_points_voice_users_at_the_voice_extra(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('pip install "yapper.py[voice]"', readme)
        self.assertNotIn("pip install livekit cryptography", readme)

    def test_readme_explains_telemetry_approval_and_featuring(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for needle in ("## Telemetry (optional, off by default)", "from yapper import telemetry", "telemetry.preview(bot)",
                       "every 30 minutes", "approved list", "your bot's ID", "proof that yapper.py is your main SDK",
                       "featured", "YAPPER_TELEMETRY=0", "## Support"):
            self.assertIn(needle, readme, needle)

    def test_telemetry_server_is_not_shipped_in_the_package(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])
        self.assertTrue((ROOT / "server" / "telemetry_server.py").exists())
        self.assertFalse((ROOT / "src" / "yapper" / "telemetry_server.py").exists())


if __name__ == "__main__":
    unittest.main()
