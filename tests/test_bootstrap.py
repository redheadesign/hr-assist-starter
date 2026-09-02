import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import privacy_guard


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace with spaces"
        self.root.mkdir()
        for directory in ("scripts", "templates"):
            shutil.copytree(ROOT / directory, self.root / directory, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / ".gitignore", self.root / ".gitignore")

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, script, *args, expect=0):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", f"scripts/{script}.py", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, expect, result.stderr + result.stdout)
        return result.stdout

    def read(self, path):
        return json.loads((self.root / ".local" / path).read_text(encoding="utf-8"))

    def test_bootstrap_is_empty_idempotent_and_resumable(self):
        self.run_script("setup", "init")
        self.assertIsNone(self.read("config/profile.json")["name"])
        self.assertIsNone(self.read("config/settings.json")["target"]["salary"]["minimum"])
        self.assertFalse(self.read("config/permissions.json")["send_messages"])
        data = list((self.root / ".local/data").glob("*.jsonl"))
        self.assertEqual(len(data), 8)
        self.assertTrue(all(not p.read_bytes() for p in data))
        payload = self.root / ".local/inputs/basics.json"
        payload.write_text(json.dumps({"name": "Example Candidate", "timezone": "UTC"}), encoding="utf-8")
        self.run_script("setup", "save", "--stage", "basics", "--json", str(payload))
        self.run_script("setup", "skip", "--stage", "documents")
        before = (self.root / ".local/config/profile.json").read_bytes()
        self.run_script("setup", "init")
        self.assertEqual(before, (self.root / ".local/config/profile.json").read_bytes())
        self.assertEqual(json.loads(self.run_script("setup", "status"))["next_stage"], "experience")
        self.assertEqual(self.read("onboarding/state.json")["stages"]["documents"]["status"], "skipped")
        self.run_script("hr_store", "validate")

    def test_unknown_schema_does_not_overwrite_data(self):
        self.run_script("setup", "init")
        version = self.root / ".local/schema-version.json"
        version.write_text('{"version": 999}', encoding="utf-8")
        self.run_script("setup", "init", expect=1)
        self.assertEqual(json.loads(version.read_text())["version"], 999)

    def test_intake_keeps_original_and_deduplicates(self):
        self.run_script("setup", "init")
        source = self.root / ".local/inputs/example.txt"
        source.write_text("Synthetic document", encoding="utf-8")
        args = ("--file", str(source), "--kind", "resume", "--purpose", "Synthetic intake test")
        first = json.loads(self.run_script("intake", *args))
        second = json.loads(self.run_script("intake", *args))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertTrue(source.exists())
        self.assertEqual(len(self.read("documents/manifest.json")), 1)
        self.assertFalse(self.read("documents/manifest.json")[0]["external_upload_allowed"])

    def test_dashboard_cannot_escape_local_directory(self):
        self.run_script("setup", "init")
        self.run_script("hr_store", "dashboard", "--output", "../leaked.md", expect=1)
        self.assertFalse((self.root / "leaked.md").exists())

    def test_ready_allows_optional_stages_to_be_skipped(self):
        self.run_script("setup", "init")
        for stage in ("basics", "documents", "experience", "preferences", "writing", "channels", "automation"):
            self.run_script("setup", "skip", "--stage", stage)
        payload = self.root / ".local/inputs/review.json"
        payload.write_text("{}", encoding="utf-8")
        self.run_script("setup", "save", "--stage", "review", "--json", str(payload))
        self.assertTrue(self.read("onboarding/state.json")["ready"])
        self.assertTrue(self.read("onboarding/state.json")["partial"])


@unittest.skipUnless(shutil.which("git"), "Git required for release checks")
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Template Maintainer")
        self.git("config", "user.email", "maintainer@example.invalid")
        (self.root / ".release-allowlist").write_text(".release-allowlist\n.gitignore\nREADME.md\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".local/\n", encoding="utf-8")
        (self.root / "README.md").write_text("Clean synthetic fixture\n", encoding="utf-8")
        self.git("add", ".")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", *args], cwd=self.root, env=env, capture_output=True, check=True)

    def test_local_data_is_ignored_and_force_add_is_blocked(self):
        local = self.root / ".local"
        local.mkdir()
        (local / "private.md").write_text("Synthetic private record", encoding="utf-8")
        self.git("add", ".")
        self.assertNotIn(b"private.md", self.git("ls-files").stdout)
        self.git("add", "-f", ".local/private.md")
        self.assertTrue(any("not approved" in x for x in privacy_guard.check(self.root, index=True)))

    def test_guard_reads_staged_bytes_and_old_history(self):
        path = self.root / "README.md"
        value = "candidate" + "@" + "personal.invalid"
        path.write_text(value, encoding="utf-8")
        self.git("add", "README.md")
        path.write_text("Clean current file", encoding="utf-8")
        self.assertTrue(any("email" in x for x in privacy_guard.check(self.root, index=True)))
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic fixture")
        self.git("add", "README.md")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Clean fixture")
        self.assertEqual(privacy_guard.check(self.root, index=True), [])
        self.assertTrue(any("email" in x for x in privacy_guard.check(self.root, index=True, history=True)))


if __name__ == "__main__":
    unittest.main()
