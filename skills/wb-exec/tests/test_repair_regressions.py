"""Behavioral regressions for the Windows desktop installation.

These came from the desktop repair line, which had also reduced the script to a
single backend. The merge kept every behavioural fix here and dropped that
restriction: the deployment policy ("only WorkBuddy on this desktop") is not a
property of the tool. `test_other_backends_stay_selectable_after_the_desktop_
hardening` pins the decision so it cannot creep back in.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "wb_repair", Path(__file__).resolve().parents[1] / "scripts" / "wb_exec.py")
wb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wb)


class DispatchRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.task = self.root / "task.json"
        self.task.write_text(json.dumps({"goal": "test", "owned_paths": ["hello.py"]}),
                             encoding="utf-8")
        self.args = SimpleNamespace(task=str(self.task), cwd=str(self.repo),
            worktree="auto", timeout=10, executor=None, dry_run=False)
        self.patches = [mock.patch.object(wb, "STATE_ROOT", self.state),
                        mock.patch.dict(os.environ, {"WB_EXECUTOR": "workbuddy"})]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # The dispatcher sets this on the child it spawns, so a suite run from
        # inside a dispatched job inherits it and cmd_dispatch returns the
        # recursion refusal before doing anything. That guard has its own test;
        # here the point is dispatch itself.
        self._saved_subagent = os.environ.pop("WB_SUBAGENT", None)
        self.addCleanup(self._restore_subagent)

    def _restore_subagent(self):
        if self._saved_subagent is not None:
            os.environ["WB_SUBAGENT"] = self._saved_subagent

    def claims(self):
        return wb.read_json(self.state / "dispatch.lock") or []

    def test_windows_utf8_bom_task_loads(self):
        self.task.write_text(json.dumps({"goal": "test"}), encoding="utf-8-sig")
        self.assertEqual("test", wb.load_task(self.task)["goal"])

    def test_relative_inputs_use_the_requested_working_directory(self):
        (self.repo / "input.txt").write_text("context", encoding="utf-8")
        self.task.write_text(json.dumps({"goal": "test", "inputs": ["input.txt"]}),
                             encoding="utf-8")
        self.args.dry_run = True
        with mock.patch.object(wb, "find_cli", return_value=["fake"]):
            argv, meta, jd = wb._assemble(self.args)
        self.assertIn(str(self.repo), argv)
        self.assertEqual(2, argv.count(str(self.repo)), argv)
        self.assertFalse(self.state.exists(), "dry-run must stay read-only")

    def test_missing_cli_leaves_no_live_claim(self):
        with mock.patch.object(wb, "find_cli", side_effect=wb.CliNotFound("missing")), \
             mock.patch.object(wb, "is_git_repo", return_value=False):
            with self.assertRaises(wb.CliNotFound):
                wb._assemble(self.args)
        self.assertEqual([], self.claims())

    def test_monitor_launch_failure_releases_claim_and_finishes_job(self):
        stdout = io.StringIO()
        with mock.patch.object(wb, "find_cli", return_value=["fake"]), \
             mock.patch.object(wb, "is_git_repo", return_value=False), \
             mock.patch.object(wb.subprocess, "Popen", side_effect=OSError("launch failed")), \
             contextlib.redirect_stdout(stdout):
            code = wb.cmd_dispatch(self.args)
        self.assertEqual(1, code)
        self.assertEqual([], self.claims())
        response = json.loads(stdout.getvalue())
        self.assertFalse(response["ok"])
        self.assertEqual("failed", wb.job_status(response["job_id"])["status"])

    def test_isolation_failure_never_falls_back_to_editing_the_source(self):
        with mock.patch.object(wb, "find_cli", return_value=["fake"]), \
             mock.patch.object(wb, "is_git_repo", return_value=True), \
             mock.patch.object(wb, "make_worktree", return_value=None):
            with self.assertRaises(RuntimeError):
                wb._assemble(self.args)
        self.assertEqual([], self.claims())

    def test_explicit_unusable_cli_does_not_silently_use_another_installation(self):
        with mock.patch.dict(os.environ, {"WB_CLI": "nonexistent-explicit-cli"}), \
             mock.patch.object(wb, "_candidate_entries", return_value=[["missing"], ["other"]]), \
             mock.patch.object(wb.subprocess, "run", side_effect=[
                 FileNotFoundError("missing"),
                 subprocess.CompletedProcess([], 0, stdout=b"2.174.0")]):
            with self.assertRaises(wb.CliNotFound):
                wb.find_cli()

    def test_other_backends_stay_selectable_after_the_desktop_hardening(self):
        """The merge decision, pinned.

        The Windows-desktop repair line had reduced this script to a single
        backend and refused every other name. That is a deployment policy, not
        a defect fix, so the merged version keeps the multi-backend contract and
        still rejects a name that does not exist. Without a test the restriction
        tends to creep back, because "pin one backend" reads like a safety
        property rather than a product decision.
        """
        with mock.patch.dict(os.environ, {"WB_EXECUTOR": "codex"}):
            self.assertEqual("codex", wb._backend_env())
        argv = wb.build_executor_argv("codex", ["codex"], "hello", self.repo, "{}", [])
        self.assertEqual("exec", argv[1])
        with self.assertRaises(wb.CliNotFound):
            wb.build_executor_argv("definitely-not-a-backend", ["x"], "hello",
                                   self.repo, "{}", [])

    def test_host_model_environment_never_changes_the_executor(self):
        with mock.patch.dict(os.environ, {"CODEX_MODEL": "my-chosen-controller",
                 "WB_MODEL": "other", "WB_EFFORT": "low", "WB_EXECUTOR": "workbuddy"}):
            argv = wb.build_executor_argv("workbuddy", ["fake"], "hello", self.repo, "{}", [])
        self.assertEqual("deepseek-v4.1-flash", argv[argv.index("--model") + 1])
        self.assertEqual("max", argv[argv.index("--effort") + 1])
        self.assertNotIn("my-chosen-controller", argv)


class MonitorRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(wb, "STATE_ROOT", self.root / "state")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.jd = wb.job_dir("job1")
        self.jd.mkdir(parents=True)
        wb.write_json(self.jd / "job.json", {
            "job_id": "job1", "argv": ["fake"], "cwd": str(self.root),
            "executor": "workbuddy", "timeout": 0.2, "started_at": time.time(),
            "prompt": "x" * (1024 * 1024)})

    def test_executor_spawn_failure_produces_a_terminal_collectable_result(self):
        with mock.patch.object(wb.subprocess, "Popen", side_effect=FileNotFoundError("missing")):
            code = wb._monitor("job1")
        self.assertEqual(1, code)
        self.assertEqual("failed", wb.job_status("job1")["status"])
        result = wb.read_json(self.jd / "result.json")
        self.assertFalse(result["ok"])
        self.assertTrue(result["blockers"])

    def test_timeout_still_works_when_the_executor_never_reads_stdin(self):
        killed = threading.Event()
        class BlockedInput:
            def write(self, data):
                killed.wait(3)
            def close(self):
                pass
        class Child:
            stdin = BlockedInput()
            pid = 123456789
            def poll(self):
                return -9 if killed.is_set() else None
            def kill(self):
                killed.set()
            def wait(self):
                killed.set()
                return -9
        started = time.monotonic()
        with mock.patch.object(wb.subprocess, "Popen", return_value=Child()):
            wb._monitor("job1")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "stdin delivery blocked the timeout supervisor")
        self.assertEqual("timeout", wb.job_status("job1")["status"])


class GitPathRegressions(unittest.TestCase):
    def test_chinese_filenames_are_returned_without_git_quoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True,
                                      capture_output=True)
            git("init")
            git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-m", "fixture")
            filename = "\u6d4b\u8bd5.py"
            (repo / filename).write_text("print('ok')\n", encoding="utf-8")
            result = wb.capture_diff(repo, Path(tmp))
            self.assertEqual([filename], result["changed"])
            self.assertEqual([], wb.violations(result["changed"], [filename]))

    def test_new_files_only_reports_modification_of_existing_files(self):
        self.assertEqual(["existing.py"], wb.violations(
            ["existing.py", "new.py"], [], {"existing.py": "M", "new.py": "A"}))


if __name__ == "__main__":
    unittest.main()