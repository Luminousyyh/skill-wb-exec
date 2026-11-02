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

    def test_vanished_job_stops_blocking(self):
        """A claim left behind by a dead process must be pruned, not wedge."""
        self._make_live_job("aaaa", ["src/a.py"])
        wb_exec._acquire_claim(["src/a.py"], "aaaa")
        import shutil
        shutil.rmtree(self.state / "jobs" / "aaaa")
        wb_exec._acquire_claim(["src/a.py"], "bbbb")   # must not raise
        wb_exec._release_claim("bbbb")


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
