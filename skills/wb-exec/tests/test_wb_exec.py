#!/usr/bin/env python3

import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wb_exec.py"
SPEC = importlib.util.spec_from_file_location("wb_exec", MODULE_PATH)
wb_exec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wb_exec)


class WorkBuddyEnvironmentTests(unittest.TestCase):
    def test_job_uses_lite_bundle_and_existing_product_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "task.json"
            task_path.write_text(json.dumps({
                "goal": "create a file",
                "owned_paths": ["hello.py"],
            }), encoding="utf-8")
            catalog = root / ".workbuddy" / "cache" / "acc-product-config-v3.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                task=str(task_path), cwd=str(root), worktree="off", timeout=60,
            )
            cli = ["node", "WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy"]
            with mock.patch.object(wb_exec, "STATE_ROOT", root / "state"), \
                    mock.patch.object(wb_exec, "find_cli", return_value=cli), \
                    mock.patch.object(wb_exec.Path, "home", return_value=root):
                _, meta, _ = wb_exec._assemble(args)

            self.assertEqual(meta["env"]["CODEBUDDY_FORCE_LITE_WB_BUNDLE"], "1")
            self.assertEqual(meta["env"]["CODEBUDDY_CONFIG_DIR"], str(catalog.parent.parent))
            self.assertEqual(meta["env"]["ACC_PRODUCT_CONFIG_PATH"], str(catalog))

    def test_standalone_cli_does_not_get_workbuddy_bundle_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "task.json"
            task_path.write_text(json.dumps({"goal": "create a file"}), encoding="utf-8")
            args = argparse.Namespace(
                task=str(task_path), cwd=str(root), worktree="off", timeout=60,
            )
            with mock.patch.object(wb_exec, "STATE_ROOT", root / "state"), \
                    mock.patch.object(wb_exec, "find_cli", return_value=["codebuddy.cmd"]):
                _, meta, _ = wb_exec._assemble(args)
            self.assertEqual(meta["env"], {})

    def test_standalone_cli_is_preferred_over_embedded_workbuddy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = root / "codebuddy.cmd"
            wrapper.write_text("", encoding="utf-8")
            entry = root / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")

            def which(name):
                if name in ("node", "node.exe"):
                    return "node"
                if name == "codebuddy":
                    return str(wrapper)
                return None

            with mock.patch.dict(wb_exec.os.environ, {}, clear=False) as env, \
                    mock.patch.object(wb_exec.shutil, "which", side_effect=which):
                env.pop("WB_CLI", None)
                candidates = wb_exec._candidate_entries()
            self.assertEqual(candidates[0], ["node", str(entry)])

    def test_native_error_result_is_not_reported_as_success(self):
        event = {
            "type": "result",
            "is_error": True,
            "errors": ["Authentication required"],
            "result": "Authentication required",
        }
        result = wb_exec.extract_result(json.dumps(event))
        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"], ["Authentication required"])

    def test_plain_assistant_text_is_not_structured_success(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Authentication required"}]},
        }
        result = wb_exec.extract_result(json.dumps(event))
        self.assertFalse(result["ok"])


class CollectEnvelopeTests(unittest.TestCase):
    def test_collect_envelope_carries_the_protocol_version(self):
        """A host must be able to tell which envelope shape it is reading."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            jd = state / "jobs" / "job1"
            jd.mkdir(parents=True)
            (jd / "job.json").write_text(json.dumps({"job_id": "job1"}), encoding="utf-8")
            (jd / "exit.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
            (jd / "result.json").write_text(json.dumps({"ok": True, "summary": "s"}),
                                            encoding="utf-8")

            buf = io.StringIO()
            with mock.patch.object(wb_exec, "STATE_ROOT", state), \
                    contextlib.redirect_stdout(buf):
                code = wb_exec._emit_collect("job1", wait=False)

        out = json.loads(buf.getvalue())
        self.assertEqual(0, code)
        self.assertIsInstance(out["protocol"], int)
        self.assertEqual(1, out["protocol"])


if __name__ == "__main__":
    unittest.main()
