#!/usr/bin/env python3
"""End-to-end tests for the whole dispatch -> supervise -> collect loop.

The unit tests next door check pieces in isolation. These drive the real CLI
against ``fake_cli.py``, so the parts that only fail when wired together —
argv assembly, environment hand-off, stream-json capture, git diff collection,
the out-of-scope check and the exit codes — are exercised as a user would.

Nothing here calls a model. ``fake_cli.py`` replays a stream-json transcript.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "skills" / "wb-exec" / "scripts" / "wb_exec.py"
FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"


def run_cli(args, cwd, env_extra=None, timeout=180):
    env = dict(os.environ)
    env.update({
        # Point the resolver at the stub, which answers --version.
        "WB_CLI": json.dumps([sys.executable, str(FAKE_CLI)]),
        # The script writes UTF-8 regardless of the console codepage; make the
        # child agree so its own output is never mis-decoded.
        "PYTHONIOENCODING": "utf-8",
    })
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=timeout,
        # Windows would otherwise decode with the ANSI codepage (cp936 here) and
        # the reader thread dies, leaving stdout as None. Pin both streams.
        encoding="utf-8", errors="replace",
    )
    return proc


def parse_json(proc):
    text = proc.stdout.strip()
    start = text.find("{")
    if start == -1:
        raise AssertionError(f"no JSON in stdout:\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return json.loads(text[start:])


@unittest.skipUnless(shutil.which("git"), "git is required for the worktree path")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbexec-e2e-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.state = Path(self.tmp) / "state"
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.repo / "hello.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "init")
        self.task = self.repo / "task.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=str(self.repo),
                              capture_output=True, text=True, check=True)

    def _write_task(self, **over):
        task = {
            "goal": "add a farewell helper",
            "owned_paths": ["hello.py"],
            "acceptance": ['python -c "import hello; print(hello.farewell())"'],
        }
        task.update(over)
        self.task.write_text(json.dumps(task), encoding="utf-8")
        return self.task

    def _env(self, **extra):
        base = {"WB_EXEC_HOME": str(self.state)}
        base.update(extra)
        return base

    def test_full_cycle_produces_a_patch_and_a_clean_result(self):
        """The loop a user actually runs: dispatch, wait, collect."""
        self._write_task()
        proc = run_cli(["run", "--task", str(self.task), "--cwd", str(self.repo)],
                       self.repo, self._env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = parse_json(proc)

        self.assertTrue(out["ok"], out)
        self.assertEqual("done", out["status"])
        self.assertEqual(0, out["exit_code"])
        result = out["result"]
        self.assertIn("farewell", result["summary"])
        self.assertEqual([], result["violations"])

        # A real worktree was used and a real diff was captured.
        self.assertTrue(out["isolated"], out)
        patch = Path(result["artifacts"]["patch"])
        self.assertTrue(patch.is_file(), "change.patch missing")
        self.assertIn("farewell", patch.read_text(encoding="utf-8"))

        # The main working tree keeps the original hello.py: the executor edited
        # the worktree, not this checkout. (task.json is created by this test and
        # is expected to show up as untracked.)
        self._git("diff", "--quiet")          # raises if tracked files changed
        original = (self.repo / "hello.py").read_text(encoding="utf-8")
        self.assertNotIn("farewell", original,
                         "the main working tree was edited directly")

    def test_out_of_scope_write_is_reported_not_swallowed(self):
        """FAKE_VIOLATION makes the stub touch a file outside owned_paths."""
        self._write_task()
        proc = run_cli(["run", "--task", str(self.task), "--cwd", str(self.repo)],
                       self.repo, self._env(FAKE_VIOLATION="1"))
        out = parse_json(proc)
        violations = out["result"]["violations"]
        self.assertTrue(violations, "the stray README.md write was not detected")
        self.assertTrue(any("README" in v for v in violations), violations)

    def test_dispatch_returns_a_job_id_and_status_reaches_a_terminal_state(self):
        """dispatch is background: it must return at once and be pollable."""
        self._write_task()
        proc = run_cli(["dispatch", "--task", str(self.task), "--cwd", str(self.repo)],
                       self.repo, self._env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        job = parse_json(proc)["job_id"]

        self.assertTrue((self.state / "jobs" / job / "job.json").is_file())

        # Then collect with --wait, which returns once the job terminates.
        proc = run_cli(["collect", "--job", job, "--wait", "--wait-timeout", "120"],
                       self.repo, self._env())
        out = parse_json(proc)
        self.assertEqual("done", out["status"], out)
        self.assertTrue(out["ok"], out)

        proc = run_cli(["status", "--job", job], self.repo, self._env())
        status = parse_json(proc)
        self.assertIn(status.get("status"), {"done", "failed", "timeout", "cancelled"},
                      f"job never reached a terminal state: {status}")

    def test_dry_run_starts_nothing(self):
        self._write_task()
        proc = run_cli(["dispatch", "--task", str(self.task), "--cwd", str(self.repo),
                        "--dry-run"], self.repo, self._env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = parse_json(proc)
        self.assertTrue(out["dry_run"])
        self.assertIn("-p", out["argv"])
        jobs = self.state / "jobs"
        self.assertFalse(jobs.exists() and any(jobs.iterdir()),
                         "dry run created a job directory")

    def test_recursion_is_refused_with_exit_code_3(self):
        """A dispatched executor must not be able to dispatch again."""
        self._write_task()
        proc = run_cli(["dispatch", "--task", str(self.task), "--cwd", str(self.repo)],
                       self.repo, self._env(WB_SUBAGENT="1"))
        self.assertEqual(3, proc.returncode)
        out = parse_json(proc)
        self.assertEqual("RECURSION_BLOCKED", out["error"])


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_ok_with_a_resolvable_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(["doctor", "--cwd", tmp], Path(tmp),
                           {"WB_EXEC_HOME": str(Path(tmp) / "state")})
            out = parse_json(proc)
            self.assertTrue(out["ok"], out)
            self.assertEqual("workbuddy", out["executor"])
            self.assertIn("available_executors", out)
            self.assertIn("0.0.0-fake", out.get("cli_version", ""))


if __name__ == "__main__":
    unittest.main()
