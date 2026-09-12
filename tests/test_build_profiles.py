import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_profiles  # noqa: E402

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


class BuildProfilesTests(unittest.TestCase):
    def test_committed_profiles_are_up_to_date(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_profiles.py"), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_plan_has_every_role_and_the_skill(self):
        files = build_profiles.render_all()
        for plan in build_profiles.PROFILES:
            self.assertIn(Path(plan, "codex", "config.toml"), files)
            self.assertIn(Path(plan, "agents", "skills", "sage-orchestrator", "SKILL.md"), files)
            for role in build_profiles.ROLES:
                self.assertIn(Path(plan, "codex", "agents", f"{role}.toml"), files)

    def test_skill_matches_role_models(self):
        for plan, profile in build_profiles.PROFILES.items():
            skill = build_profiles.render_skill(profile)
            self.assertNotIn("{{", skill)
            self.assertTrue(skill.startswith("---\nname: sage-orchestrator\n"))
            for role in build_profiles.ROLES:
                model, effort = build_profiles.role_model(profile, role)
                self.assertRegex(skill, rf"\n{role}\s+{re.escape(model)}\s+{effort}\s")

    @unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
    def test_generated_toml_parses_with_expected_values(self):
        for rel, content in build_profiles.render_all().items():
            if rel.suffix != ".toml":
                continue
            with self.subTest(file=str(rel)):
                data = tomllib.loads(content)
                profile = build_profiles.PROFILES[rel.parts[0]]
                if rel.name == "config.toml":
                    self.assertEqual((data["model"], data["model_reasoning_effort"]), profile["root"])
                    self.assertIs(data["agents"]["enabled"], True)
                else:
                    role = rel.stem
                    self.assertEqual(data["name"], role)
                    self.assertEqual((data["model"], data["model_reasoning_effort"]), build_profiles.role_model(profile, role))
                    self.assertEqual(data["sandbox_mode"], build_profiles.ROLES[role]["sandbox"])
                    template = (ROOT / "templates" / "roles" / f"{role}.md").read_text(encoding="utf-8")
                    self.assertEqual(data["developer_instructions"].strip(), template.strip())


if __name__ == "__main__":
    unittest.main()
