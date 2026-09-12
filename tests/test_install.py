import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.py"


def run(*args, stdin="", env=None):
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def install(self, *extra):
        result = run("--target", str(self.target), "--yes", *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_fresh_install_writes_all_components(self):
        self.install("--plan", "plus")
        self.assertEqual(
            (self.target / ".codex" / "config.toml").read_bytes(),
            (ROOT / "profiles" / "plus" / "codex" / "config.toml").read_bytes(),
        )
        for role in ("scout", "builder", "verifier", "scholar", "critic"):
            self.assertTrue((self.target / ".codex" / "agents" / f"{role}.toml").is_file())
        self.assertTrue((self.target / ".agents" / "skills" / "sage-orchestrator" / "SKILL.md").is_file())
        self.assertIn("codesage-orchestrator:begin", (self.target / "AGENTS.md").read_text())
        manifest = json.loads((self.target / ".codesage-orchestrator" / "manifest.json").read_text())
        self.assertEqual(manifest["plan"], "plus")
        self.assertIn(str(Path(".codex/config.toml")), manifest["files"])

    def test_reinstall_is_a_no_op(self):
        self.install()
        self.assertIn("Already up to date", self.install().stdout)

    def test_dry_run_writes_nothing(self):
        result = self.install("--dry-run")
        self.assertIn("+ .codex", result.stdout.replace(os.sep, "/"))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_only_limits_components(self):
        self.install("--only", "skill")
        self.assertTrue((self.target / ".agents").is_dir())
        self.assertTrue((self.target / ".codesage-orchestrator" / "manifest.json").is_file())
        self.assertFalse((self.target / ".codex").exists())

    def test_changed_files_are_kept_unless_overwrite(self):
        self.install()
        config = self.target / ".codex" / "config.toml"
        config.write_text("# my edits\n")
        self.assertIn("Keeping 1 existing file", self.install().stdout)
        self.assertEqual(config.read_text(), "# my edits\n")
        self.install("--overwrite")
        self.assertEqual(config.read_bytes(), (ROOT / "profiles" / "pro" / "codex" / "config.toml").read_bytes())

    def test_agents_md_is_appended_then_updated_in_place(self):
        agents_md = self.target / "AGENTS.md"
        agents_md.write_text("# My project\n\nKeep tests green.\n")
        self.install()
        first = agents_md.read_text()
        self.assertTrue(first.startswith("# My project\n\nKeep tests green.\n\n<!-- codesage"))

        # Simulate an older block and make sure it is replaced, not duplicated.
        agents_md.write_text(first.replace("For non-trivial", "OLD TEXT. For non-trivial") + "\nFooter\n")
        self.install()
        second = agents_md.read_text()
        self.assertEqual(second.count("codesage-orchestrator:begin"), 1)
        self.assertNotIn("OLD TEXT", second)
        self.assertTrue(second.endswith("\nFooter\n"))

    def test_crlf_agents_md_keeps_line_endings(self):
        agents_md = self.target / "AGENTS.md"
        agents_md.write_bytes(b"# Windows project\r\n")
        self.install()
        data = agents_md.read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_refuses_to_write_through_symlinks(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.target / ".codex").symlink_to(outside.name, target_is_directory=True)
        result = run("--target", str(self.target), "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_rejects_setup_repo_and_missing_target(self):
        self.assertIn("different directory", run("--target", str(ROOT), "--yes").stderr)
        self.assertIn("existing directory", run("--target", str(self.target / "nope"), "--yes").stderr)
        self.assertIn("--target is required", run("--yes").stderr)

    def test_doctor_reports_healthy_install_and_catches_problems(self):
        env = dict(os.environ, CODEX_HOME=str(self.target / "codex-home"))
        doctor = lambda: subprocess.run(  # noqa: E731
            [sys.executable, str(INSTALLER), "doctor", "--target", str(self.target)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(doctor().returncode, 1)

        self.install("--plan", "pro")
        result = doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("match the 'pro' profile", result.stdout)

        (self.target / ".codex" / "agents" / "critic.toml").unlink()
        result = doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("critic.toml is missing", result.stdout)

    def test_update_uses_recorded_plan_and_preserves_edits(self):
        self.install("--plan", "plus")
        skill = self.target / ".agents" / "skills" / "sage-orchestrator" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n# local note\n")
        result = run("update", "--target", str(self.target), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Keeping 1 existing file", result.stdout)
        self.assertIn("# local note", skill.read_text())

    def test_uninstall_removes_pristine_install(self):
        self.install("--plan", "plus")
        result = run("uninstall", "--target", str(self.target), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_uninstall_preserves_modified_files(self):
        self.install()
        config = self.target / ".codex" / "config.toml"
        config.write_text(config.read_text() + "\n# local setting\n")
        result = run("uninstall", "--target", str(self.target), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(config.is_file())
        self.assertIn("local setting", config.read_text())
        self.assertFalse((self.target / ".agents").exists())
        self.assertTrue((self.target / ".codesage-orchestrator" / "manifest.json").is_file())

    def test_uninstall_preserves_preexisting_project_config(self):
        codex = self.target / ".codex"
        codex.mkdir()
        config = codex / "config.toml"
        config.write_text((ROOT / "profiles" / "pro" / "codex" / "config.toml").read_text())
        self.install("--overwrite")
        result = run("uninstall", "--target", str(self.target), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(config.is_file())
        self.assertEqual(config.read_text(), (ROOT / "profiles" / "pro" / "codex" / "config.toml").read_text())

    def test_global_install_merges_config_and_uninstall_preserves_it(self):
        home = Path(self._tmp.name) / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        config = codex_home / "config.toml"
        config.write_text(
            'model = "old-model"\n'
            "custom_flag = true\n\n"
            "[agents]\n"
            "enabled = false\n"
        )
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home))
        result = run("global", "--plan", "lite", "--yes", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = config.read_text()
        self.assertIn('model = "gpt-5.6-luna"', merged)
        self.assertIn("custom_flag = true", merged)
        self.assertIn("enabled = true", merged)
        self.assertTrue((codex_home / "agents" / "scout.toml").is_file())
        self.assertTrue((home / ".agents" / "skills" / "sage-orchestrator" / "SKILL.md").is_file())
        self.assertTrue((codex_home / "codesage-orchestrator" / "manifest.json").is_file())
        result = run("update", "--global", "--yes", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Already up to date", result.stdout)

        result = run("uninstall", "--global", "--yes", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(config.is_file())
        self.assertIn("custom_flag = true", config.read_text())
        self.assertFalse((codex_home / "agents").exists())
        self.assertFalse((home / ".agents").exists())

    def test_bundle_contains_skill_directory(self):
        output = self.target / "sage-orchestrator-plus.zip"
        result = run("bundle", "--plan", "plus", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), ["sage-orchestrator/SKILL.md"])


if __name__ == "__main__":
    unittest.main()
