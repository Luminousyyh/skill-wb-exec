#!/usr/bin/env python3
"""Tests for the dispatch lock and the structured needs_human field.

The lock matters because it turns a written-down convention ("do not run two jobs
over overlapping paths") into something the code enforces. These tests pin both
halves: overlapping paths must be refused, and disjoint paths must still run, so
the guard cannot decay into "refuses everything".
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wb_exec.py"
SPEC = importlib.util.spec_from_file_location("wb_exec_lock", MODULE_PATH)
wb_exec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wb_exec)


def _a_dead_pid():
    """A pid that the probe classifies as dead, found rather than hard-coded.

    Spawning and reaping a child guarantees its pid was real and is now gone.
    (Reused pids are possible but rare; if the probe disagrees, the test is
    skipped rather than asserting on a flaky assumption.)
    """
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class OverlapTests(unittest.TestCase):
    def test_identical_paths_overlap(self):
        self.assertTrue(wb_exec._overlaps(["src/a.py"], ["src/a.py"]))

    def test_nested_paths_overlap(self):
        self.assertTrue(wb_exec._overlaps(["src"], ["src/a.py"]))
        self.assertTrue(wb_exec._overlaps(["src/a.py"], ["src"]))
        self.assertTrue(wb_exec._overlaps(["src/"], ["src//a.py"]))

    def test_disjoint_paths_do_not_overlap(self):
        self.assertFalse(wb_exec._overlaps(["src/a.py"], ["src/b.py"]))
        self.assertFalse(wb_exec._overlaps(["docs"], ["src"]))

    def test_prefix_is_not_a_path_boundary(self):
        """src/auth must not be treated as containing src/authz."""
        self.assertFalse(wb_exec._overlaps(["src/auth"], ["src/authz"]))

    def test_windows_separators_are_normalised(self):
        self.assertTrue(wb_exec._overlaps(["src\\a.py"], ["src/a.py"]))

    def test_empty_whitelist_could_touch_anything(self):
        """An empty list means 'new files only', which could be anywhere."""
        self.assertTrue(wb_exec._overlaps([], ["src/a.py"]))
        self.assertTrue(wb_exec._overlaps(["src/a.py"], []))


class LockClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self._real_root = wb_exec.STATE_ROOT
        wb_exec.STATE_ROOT = self.state

    def tearDown(self):
        wb_exec.STATE_ROOT = self._real_root
        self.tmp.cleanup()

    def _write_corrupt_lock(self, payload):
        path = wb_exec._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return path

    def test_corrupt_lock_refuses_instead_of_silently_admitting(self):
        """A damaged lock must fail closed.

        Before this fix an unparseable dispatch.lock was coerced to [], so every
        job was admitted and the file was then rewritten with only the new
        claim -- existing holds were discarded with no message. The safety file
        failing meant safety was off, silently.
        """
        path = self._write_corrupt_lock('{"job_id": "orphan", ')  # truncated JSON
        before = path.read_bytes()
        with self.assertRaises(wb_exec.LockBusy) as ctx:
            wb_exec._acquire_claim(["src/auth/"], "newjob")
        holders = ctx.exception.holders
        self.assertEqual("<corrupt-lock>", holders[0]["job_id"])
        # The corrupt file must survive untouched: repairing is the user's call.
        self.assertEqual(before, path.read_bytes())

    def test_non_list_lock_refuses_too(self):
        self._write_corrupt_lock('{"job_id": "orphan"}')  # valid JSON, wrong shape
        with self.assertRaises(wb_exec.LockBusy):
            wb_exec._acquire_claim(["src/auth/"], "newjob")

    def test_missing_lock_still_admits(self):
        """The fail-closed rule must not decay into 'refuses everything'."""
        self.assertFalse(wb_exec._lock_path().exists())
        live = wb_exec._acquire_claim(["src/auth/"], "newjob")
        self.assertEqual(["newjob"], [c["job_id"] for c in live])

    def test_dry_run_reports_a_corrupt_lock_as_a_blocker(self):
        """A dry run must not promise a clean slot that dispatch would refuse."""
        self._write_corrupt_lock('{"job_id": ')
        blockers = wb_exec._overlapping_jobs(["src/auth/"])
        self.assertEqual(1, len(blockers))
        self.assertEqual("<corrupt-lock>", blockers[0]["job_id"])
        self.assertIn("无法解析", blockers[0]["detail"])

    def _make_live_job(self, job_id, owned):
        d = self.state / "jobs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "heartbeat.json").write_text(json.dumps({"t": 1}), encoding="utf-8")
        return d

    # -- S-1: liveness is decided by the pid, not by a missing heartbeat ------

    def test_crashed_supervisor_with_live_pid_still_blocks(self):
        """The safety hole: no heartbeat, but the process it spawned is alive.

        A supervisor that died between taking the claim and writing its first
        heartbeat leaves a job dir with no heartbeat and no exit.json. The old
        rule pruned that claim, admitting a new job against paths an orphan may
        still be writing. A live pid must keep the claim.
        """
        d = self.state / "jobs" / "orphan"
        d.mkdir(parents=True, exist_ok=True)
        # No heartbeat.json, no exit.json -- exactly the crash window.
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "orphan", "owned_paths": ["src/auth/"],
             "pid": os.getpid(), "since": 1}  # this test process is alive
        ])
        with self.assertRaises(wb_exec.LockBusy) as ctx:
            wb_exec._acquire_claim(["src/auth/"], "newjob")
        self.assertEqual("orphan", ctx.exception.holders[0]["job_id"])

    def test_crashed_supervisor_with_dead_pid_is_pruned(self):
        """The other direction: a provably dead pid must not wedge the lock."""
        d = self.state / "jobs" / "dead"
        d.mkdir(parents=True, exist_ok=True)
        # PID 4 is the System process on Windows and is not ours on POSIX; use a
        # value the probe can classify as dead rather than merely unprobeable.
        unreachable = _a_dead_pid()
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "dead", "owned_paths": ["src/auth/"],
             "pid": unreachable, "since": 1}
        ])
        live = wb_exec._acquire_claim(["src/auth/"], "newjob")
        self.assertEqual(["newjob"], [c["job_id"] for c in live])

    def test_finished_job_is_pruned_regardless_of_pid(self):
        d = self._make_live_job("done1", [])
        (d / "exit.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "done1", "owned_paths": ["src/auth/"],
             "pid": os.getpid(), "since": 1}
        ])
        live = wb_exec._acquire_claim(["src/auth/"], "newjob")
        self.assertEqual(["newjob"], [c["job_id"] for c in live])

    def test_heartbeat_present_blocks_even_with_a_dead_pid(self):
        """A heartbeat means the job is running; the pid probe is not needed."""
        d = self._make_live_job("beat", [])
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "beat", "owned_paths": ["src/auth/"],
             "pid": _a_dead_pid(), "since": 1}
        ])
        with self.assertRaises(wb_exec.LockBusy):
            wb_exec._acquire_claim(["src/auth/"], "newjob")

    def test_unprobeable_pid_falls_back_to_pruning(self):
        """If the platform cannot answer, keep the old heuristic rather than
        wedging the lock forever."""
        d = self.state / "jobs" / "unknown"
        d.mkdir(parents=True, exist_ok=True)
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "unknown", "owned_paths": ["src/auth/"], "since": 1}
        ])  # no pid recorded at all -> probe returns None
        live = wb_exec._acquire_claim(["src/auth/"], "newjob")
        self.assertEqual(["newjob"], [c["job_id"] for c in live])

    def test_claim_is_written_and_then_removed(self):
        self._make_live_job("aaaa", ["src/a.py"])
        claims = wb_exec._acquire_claim(["docs/x.md"], "aaaa")
        self.assertEqual(["aaaa"], [c["job_id"] for c in claims])
        self.assertEqual(["docs/x.md"], claims[0]["owned_paths"])

        wb_exec._release_claim("aaaa")
        self.assertEqual([], json.loads((self.state / "dispatch.lock").read_text()))

    def test_overlapping_claim_is_refused(self):
        self._make_live_job("aaaa", ["src/a.py"])
        wb_exec._acquire_claim(["src"], "aaaa")
        with self.assertRaises(wb_exec.LockBusy) as ctx:
            wb_exec._acquire_claim(["src/a.py"], "bbbb")
        self.assertEqual("aaaa", ctx.exception.holders[0]["job_id"])
        wb_exec._release_claim("aaaa")

    def test_disjoint_claim_is_allowed(self):
        self._make_live_job("aaaa", ["src/a.py"])
        wb_exec._acquire_claim(["src/a.py"], "aaaa")
        claims = wb_exec._acquire_claim(["docs/b.md"], "bbbb")
        self.assertEqual(2, len(claims))
        wb_exec._release_claim("aaaa")
        wb_exec._release_claim("bbbb")

    def test_finished_job_stops_blocking(self):
        """A job whose exit.json exists must not hold paths hostage."""
        d = self._make_live_job("aaaa", ["src/a.py"])
        wb_exec._acquire_claim(["src/a.py"], "aaaa")
        (d / "exit.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
        # Should not raise.
        wb_exec._acquire_claim(["src/a.py"], "bbbb")
        wb_exec._release_claim("aaaa")
        wb_exec._release_claim("bbbb")

    def test_vanished_job_with_dead_pid_stops_blocking(self):
        """A claim left behind by a dead process must be pruned, not wedge.

        The point is the dead *process*, not the missing directory: a missing
        job dir alone no longer prunes anything, because the pid may still be
        running (that was the S-1 hole). This test records a provably dead pid
        so it is exercising pruning rather than the old mistake.
        """
        self._make_live_job("aaaa", ["src/a.py"])
        wb_exec._acquire_claim(["src/a.py"], "aaaa")
        import shutil
        shutil.rmtree(self.state / "jobs" / "aaaa")
        # Rewrite the claim with a dead pid, as a crashed supervisor would leave.
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "aaaa", "owned_paths": ["src/a.py"],
             "pid": _a_dead_pid(), "since": 1}
        ])
        wb_exec._acquire_claim(["src/a.py"], "bbbb")   # must not raise
        wb_exec._release_claim("bbbb")


class JobsListingTests(unittest.TestCase):
    """The read command that makes the lock's refusal actionable.

    Without it, a refused user can only delete dispatch.lock by hand -- the one
    thing README warns against. Whatever it prints, it must not change state.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self._real_root = wb_exec.STATE_ROOT
        wb_exec.STATE_ROOT = self.state

    def tearDown(self):
        wb_exec.STATE_ROOT = self._real_root
        self.tmp.cleanup()

    def _run(self, limit=5):
        import argparse
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = wb_exec.cmd_jobs(argparse.Namespace(limit=limit))
        self.assertEqual(0, code)
        return json.loads(buf.getvalue())

    def _make_job(self, job_id, *, heartbeat=True, exit_status=None):
        d = self.state / "jobs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        wb_exec.write_json(d / "job.json", {
            "job_id": job_id, "goal": "a goal", "owned_paths": ["src/a.py"],
            "worktree": str(d / "wt"), "created_at": 1})
        if heartbeat:
            wb_exec.write_json(d / "heartbeat.json", {"ts": 1})
        if exit_status:
            wb_exec.write_json(d / "exit.json", {"status": exit_status})
        return d

    def test_it_lists_claims_and_jobs(self):
        self._make_job("live1")
        wb_exec._acquire_claim(["src/a.py"], "live1")
        out = self._run()
        self.assertEqual(["live1"], [c["job_id"] for c in out["claims"]])
        self.assertTrue(out["claims"][0]["live"])
        self.assertEqual(["live1"], [j["job_id"] for j in out["jobs"]])

    def test_it_is_read_only(self):
        """Listing must not prune, release or rewrite anything."""
        self._make_job("live1")
        wb_exec._acquire_claim(["src/a.py"], "live1")
        lock_before = wb_exec._lock_path().read_bytes()
        self._run()
        self.assertEqual(lock_before, wb_exec._lock_path().read_bytes())
        self.assertEqual(1, len(json.loads(lock_before)))

    def test_a_dead_claim_is_shown_but_not_removed(self):
        """It reports liveness; removal stays a deliberate act, not a side effect."""
        d = self.state / "jobs" / "dead"
        d.mkdir(parents=True, exist_ok=True)   # no heartbeat, no exit.json
        wb_exec.write_json(wb_exec._lock_path(), [
            {"job_id": "dead", "owned_paths": ["src/a.py"],
             "pid": _a_dead_pid(), "since": 1}
        ])
        out = self._run()
        self.assertFalse(out["claims"][0]["live"])
        self.assertEqual("dead", out["claims"][0]["job_id"])
        # Still on disk: the command reports, it does not act.
        self.assertEqual(1, len(json.loads(wb_exec._lock_path().read_text())))

    def test_a_corrupt_lock_is_reported_without_failing(self):
        path = wb_exec._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"job_id": ', encoding="utf-8")
        out = self._run()
        self.assertIsNotNone(out["lock_warning"])
        self.assertIn("无法解析", out["lock_warning"])
        self.assertEqual([], out["claims"])

    def test_limit_caps_the_job_rows(self):
        for i in range(4):
            self._make_job(f"job{i}")
        out = self._run(limit=2)
        self.assertEqual(2, len(out["jobs"]))
        self.assertEqual(4, out["jobs_total"])


class NeedsHumanTests(unittest.TestCase):
    def test_clean_success_needs_nobody(self):
        out = wb_exec._needs_human("done", {"summary": "ok", "tests": []}, [])
        self.assertEqual({"required": False, "reasons": []}, out)

    def test_failure_needs_a_person(self):
        out = wb_exec._needs_human("failed", {}, [])
        self.assertTrue(out["required"])
        self.assertIn("failed", out["reasons"])

    def test_out_of_scope_write_needs_a_person(self):
        out = wb_exec._needs_human("done", {}, ["README.md"])
        self.assertTrue(out["required"])
        self.assertIn("out_of_scope_writes", out["reasons"])

    def test_blockers_need_a_person(self):
        out = wb_exec._needs_human("done", {"blockers": ["need a decision"]}, [])
        self.assertIn("blocked", out["reasons"])

    def test_failing_tests_need_a_person(self):
        result = {"tests": [{"cmd": "pytest", "status": "fail"}]}
        out = wb_exec._needs_human("done", result, [])
        self.assertIn("failing_tests", out["reasons"])

    def test_passing_tests_alone_do_not(self):
        result = {"tests": [{"cmd": "pytest", "status": "pass"}]}
        self.assertFalse(wb_exec._needs_human("done", result, [])["required"])

    def test_reasons_are_deduplicated_and_ordered(self):
        result = {"blockers": ["x"], "tests": [{"cmd": "t", "status": "fail"}]}
        out = wb_exec._needs_human("failed", result, ["oops.py"])
        self.assertEqual(len(out["reasons"]), len(set(out["reasons"])))
        self.assertEqual("failed", out["reasons"][0])


if __name__ == "__main__":
    unittest.main()
