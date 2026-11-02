#!/usr/bin/env python3
"""Guard tests for the pluggable-executor refactor.

The point of these tests is *backward compatibility*: adding backends must not
change a single argument on the default WorkBuddy path, because existing jobs,
logs and users depend on that exact request. The remaining tests pin the wiring
of the other backends so a typo cannot silently ship.
"""

import importlib.util
import unittest
import uuid
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wb_exec.py"
SPEC = importlib.util.spec_from_file_location("wb_exec", MODULE_PATH)
wb_exec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wb_exec)


class ExecutorBackendTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path("/repo")
        self.schema = '{"type":"object"}'
        self.prompt = "do the thing"
        self.refs = ["/a.py"]
        self._real_uuid4 = uuid.uuid4
        uuid.uuid4 = lambda: uuid.UUID(int=0)

    def tearDown(self):
        uuid.uuid4 = self._real_uuid4

    def test_workbuddy_argv_is_unchanged_by_the_backend_registry(self):
        """The pre-refactor argv, spelled out literally.

        The prompt is deliberately absent: it now travels on stdin, because a
        multi-line argv prompt is truncated to its first line by this CLI.
        """
        expected = ["node", "cli"] + [
            "-p",
            "--output-format", "stream-json",
            "--json-schema", self.schema,
            "-y",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
            "--add-dir", str(self.cwd),
            "--session-id", str(uuid.uuid4()),
            "--add-dir", "/a.py",
            "--model", wb_exec.EXECUTOR_MODEL,
            "--effort", wb_exec.EXECUTOR_EFFORT,
        ]
        actual = wb_exec.build_executor_argv(
            "workbuddy", ["node", "cli"], self.prompt, self.cwd, self.schema, self.refs
        )
        self.assertEqual(expected, actual)

    def test_workbuddy_does_not_put_a_multiline_prompt_on_argv(self):
        """A multi-line prompt on argv is truncated to its first line.

        Measured against codebuddy 2.174.0: "Line one AAA.\\nLine two BBB."
        arrives as "AAA.", while the same text on stdin arrives intact. The
        prompt must therefore never appear in argv for this backend.
        """
        multiline = "first line\nsecond line\nthird line"
        argv = wb_exec.build_executor_argv(
            "workbuddy", ["node", "cli"], multiline, self.cwd, self.schema, self.refs
        )
        self.assertNotIn(multiline, argv)
        for part in ("first line", "second line", "third line"):
            self.assertNotIn(part, argv)

    def test_workbuddy_opens_tools_in_non_interactive_runs(self):
        """`-y` alone is not enough; without --permission-mode nothing is writable.

        Measured against codebuddy 2.174.0: `-y` is documented as "Bypass
        permission prompts (HIGH/CRITICAL still ask)", and a non-interactive
        `-p` run has no prompt to answer. Every task therefore came back with
        blockers and an untouched worktree, exit code 0. Passing
        --permission-mode bypassPermissions makes the write actually land.
        """
        argv = wb_exec.build_executor_argv(
            "workbuddy", ["node", "cli"], self.prompt, self.cwd, self.schema, self.refs
        )
        self.assertIn("--permission-mode", argv)
        mode = argv[argv.index("--permission-mode") + 1]
        self.assertIn(mode, ("bypassPermissions", "acceptEdits", "dontAsk"))
        self.assertIn("-y", argv)

    def test_workbuddy_keeps_its_pinned_model_and_effort(self):
        self.assertEqual("deepseek-v4.1-flash", wb_exec.EXECUTOR_MODEL)
        self.assertEqual("max", wb_exec.EXECUTOR_EFFORT)

    def test_default_executor_is_workbuddy(self):
        self.assertEqual("workbuddy", wb_exec.DEFAULT_EXECUTOR)
        self.assertIn("workbuddy", wb_exec.EXECUTOR_BACKENDS)

    def test_other_backends_do_not_inject_workbuddy_model_flags(self):
        for backend in ("codex", "claude", "aider"):
            with self.subTest(backend=backend):
                argv = wb_exec.build_executor_argv(
                    backend, [backend], self.prompt, self.cwd, self.schema, self.refs
                )
                self.assertIn(self.prompt, argv)
                self.assertNotIn("--model", argv)
                self.assertNotIn("--effort", argv)

    def test_custom_backend_expands_placeholders(self):
        template = ["-p", "{prompt}", "--dir", "{cwd}", "--schema", "{schema}"]
        real = wb_exec._custom_argv_template
        wb_exec._custom_argv_template = lambda: template
        try:
            argv = wb_exec.build_executor_argv(
                "custom", ["mycli"], self.prompt, self.cwd, self.schema, []
            )
        finally:
            wb_exec._custom_argv_template = real
        self.assertEqual(
            ["mycli", "-p", self.prompt, "--dir", str(self.cwd),
             "--schema", self.schema],
            argv,
        )

    def test_unknown_backend_never_falls_through_to_workbuddy(self):
        """A typo must fail loudly instead of silently using the wrong CLI."""
        with self.assertRaises(Exception):
            wb_exec.find_cli("definitely-not-a-backend")

    def test_doctor_reports_the_requested_backend_even_when_it_is_missing(self):
        """Regression: a missing CLI used to make doctor claim 'workbuddy'.

        The backend name has to be recorded before the CLI probe runs, so a
        failed probe still reports what the user actually asked for rather
        than silently impersonating the default.
        """
        import argparse
        import json as _json
        import io
        import contextlib

        args = argparse.Namespace(cwd=".", executor="codex")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wb_exec.cmd_doctor(args)
        info = _json.loads(buf.getvalue())
        self.assertEqual("codex", info["executor"])
        self.assertNotIn("model", info["executor_settings"])


if __name__ == "__main__":
    unittest.main()
