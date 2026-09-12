import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "token_usage.py"
sys.path.insert(0, str(ROOT / "scripts"))

from token_usage import role_of  # noqa: E402

SESSION = "0000aaaa-root"


def rollout(thread_id, source, model, effort, usages, limits=None, day="2026/09/10"):
    lines = [
        {
            "type": "session_meta",
            "timestamp": "2026-09-10T08:00:00Z",
            "payload": {
                "id": thread_id,
                "session_id": SESSION,
                "source": source,
                "timestamp": "2026-09-10T08:00:00Z",
                "cwd": "/work/app",
                "cli_version": "0.154.0",
            },
        },
        {"type": "turn_context", "timestamp": "2026-09-10T08:00:01Z", "payload": {"model": model, "effort": effort}},
        {"type": "response_item", "payload": {"type": "message", "content": "x" * 5000}},
    ]
    for i, usage in enumerate(usages):
        lines.append(
            {"type": "token_usage_record", "timestamp": f"2026-09-10T08:0{i + 1}:00Z", "payload": {"usage": usage}}
        )
    if limits:
        for stamp, used in limits:
            lines.append(
                {
                    "type": "event_msg",
                    "timestamp": stamp,
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {"plan_type": "plus", "primary": {"used_percent": used}, "secondary": {"used_percent": used / 10}},
                    },
                }
            )
    return day, f"rollout-{thread_id}.jsonl", "\n".join(json.dumps(line) for line in lines) + "\n"


def usage(inp, cached, out):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "reasoning_output_tokens": 1, "total_tokens": inp + out}


class RoleTests(unittest.TestCase):
    def test_role_shapes(self):
        self.assertEqual(role_of({"id": "a", "session_id": "a", "source": "cli"}), ("root", ""))
        self.assertEqual(role_of({"source": {"subagent": "review"}}), ("review", ""))
        self.assertEqual(
            role_of({"source": {"subagent": {"thread_spawn": {"agent_role": "builder", "agent_nickname": "Nova"}}}}),
            ("builder", "Nova"),
        )
        self.assertEqual(role_of({"source": {"subagent": {"other": "guardian"}}}), ("guardian", ""))
        self.assertEqual(role_of({"id": "b", "session_id": "a", "thread_source": "guardian_review"}), ("guardian_review", ""))


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sessions = Path(cls._tmp.name)
        spawn = lambda role: {"subagent": {"thread_spawn": {"agent_role": role}}}  # noqa: E731
        files = [
            rollout(SESSION, "cli", "gpt-6-astra", "medium", [usage(1000, 900, 50), usage(2000, 1900, 70)],
                    limits=[("2026-09-10T08:01:00Z", 10), ("2026-09-10T08:30:00Z", 25)]),
            rollout("0000aaab-scout", spawn("scout"), "gpt-5.6-luna", "max", [usage(500, 400, 30)]),
            rollout("0000aaac-guard", {"subagent": {"other": "guardian"}}, "gpt-6-astra", "low", [usage(99, 0, 1)]),
            # A solo session on another day.
            ("2026/09/11", "rollout-solo.jsonl", json.dumps({"type": "session_meta", "payload": {"id": "solo", "session_id": "solo", "source": "cli", "timestamp": "2026-09-11T00:00:00Z"}}) + "\n"),
        ]
        for day, name, body in files:
            folder = cls.sessions / day
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(body, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.sessions), *args], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_sessions_lists_only_orchestrated_by_default(self):
        out = self.cli("sessions")
        self.assertRegex(out, r"0000aaaa\s+1\s+/work/app")
        self.assertNotIn("solo", out)
        self.assertIn("solo", self.cli("sessions", "--all"))

    def test_date_filter(self):
        self.assertIn("no sessions with subagents", self.cli("sessions", "--date", "2026-09-11"))

    def test_markdown_report_totals_exclude_auto_review(self):
        out = self.cli("report")
        self.assertIn("threads: 3 (2 counted)", out)
        self.assertIn("| root | gpt-6-astra / medium | 2 | 200 | 2,800 | 120 |", out)
        self.assertIn("| scout | gpt-5.6-luna / max | 1 | 100 | 400 | 30 |", out)
        self.assertIn("| **all** | 2 | 3 | 300 | 3,200 | 150 |", out)
        self.assertIn("5h 10% -> 25%", out)
        self.assertIn("Excluded 1 Codex auto-review", out)

    def test_include_auto_review(self):
        self.assertIn("| **all** | 3 | 4 | 399 |", self.cli("report", "0000", "--include-auto-review"))

    def test_csv_and_json(self):
        table = list(csv.DictReader(io.StringIO(self.cli("report", "--format", "csv"))))
        self.assertEqual([row["role"] for row in table], ["root", "scout"])
        self.assertEqual(sum(int(row["output_tokens"]) for row in table), 150)

        report = json.loads(self.cli("report", SESSION, "--format", "json"))
        self.assertEqual(report["session_id"], SESSION)
        self.assertEqual(len(report["threads"]), 3)


if __name__ == "__main__":
    unittest.main()
