#!/usr/bin/env python3
"""Guard tests for the pluggable-executor refactor.

The point of these tests is *backward compatibility*: adding backends must not
change a single argument on the default WorkBuddy path, because existing jobs,
logs and users depend on that exact request. The remaining tests pin the wiring
of the other backends so a typo cannot silently ship.
"""

import importlib.util
import os
import tempfile
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
        self.refs = ["/repo/a.py"]
        self._real_uuid4 = uuid.uuid4
        uuid.uuid4 = lambda: uuid.UUID(int=0)

    def tearDown(self):
        uuid.uuid4 = self._real_uuid4

    def test_workbuddy_argv_is_unchanged_by_the_backend_registry(self):
        """The pre-refactor argv, spelled out literally.

        The prompt is deliberately absent: it now travels on stdin, because a
        multi-line argv prompt is truncated to its first line by this CLI.

        One entry changed on purpose. The ref is a file (/repo/a.py) but the
        argv says /repo: `--add-dir` is documented as
        `--add-dir <directories...>`, so a file input is widened to its parent
        directory before it reaches the CLI. Everything else -- order, flags,
        spacing -- is still pinned verbatim.
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
            "--add-dir", "/repo",
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


class AddDirPathTests(unittest.TestCase):
    """`--add-dir` is documented as `--add-dir <directories...>`.

    Task `inputs` name files (assets/task.example.json), so the raw refs were
    handed to a flag that only accepts directories. These tests pin the fix:
    widen to the parent, once per directory, and never to a root.
    """

    def _values(self, argv, flag):
        return [argv[i + 1] for i, part in enumerate(argv) if part == flag]

    def test_a_file_is_widened_to_its_parent_directory(self):
        self.assertEqual(["/repo/src"], wb_exec._add_dir_paths(["/repo/src/a.py"]))

    def test_two_files_in_one_directory_produce_one_entry(self):
        self.assertEqual(
            ["/repo/src"],
            wb_exec._add_dir_paths(["/repo/src/a.py", "/repo/src/b.py"]),
        )

    def test_order_follows_first_appearance(self):
        self.assertEqual(
            ["/repo/b", "/repo/a"],
            wb_exec._add_dir_paths(["/repo/b/x.py", "/repo/a/y.py", "/repo/b/z.py"]),
        )

    def test_a_directory_ref_is_passed_through_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([tmp], wb_exec._add_dir_paths([tmp]))

    def test_a_root_is_never_used_as_a_widening(self):
        """Widening /a.py to / would hand the executor the whole filesystem."""
        self.assertEqual(["/a.py"], wb_exec._add_dir_paths(["/a.py"]))

    @unittest.skipIf(os.name != "nt", "drive roots only exist under ntpath")
    def test_a_drive_root_is_never_used_as_a_widening(self):
        self.assertEqual(["C:\\a.py"], wb_exec._add_dir_paths(["C:\\a.py"]))

    def test_every_add_dir_value_is_a_directory_for_the_workbuddy_backend(self):
        refs = ["/repo/src/a.py", "/repo/src/b.py", "/repo/tests/test_auth.py"]
        argv = wb_exec.build_executor_argv(
            "workbuddy", ["node", "cli"], "p", Path("/repo"), "{}", refs
        )
        self.assertEqual([str(Path("/repo")), "/repo/src", "/repo/tests"],
                         self._values(argv, "--add-dir"))

    def test_claude_and_codex_only_receive_directories(self):
        # claude passes the working directory via --add-dir as well; codex
        # passes it via -C, so only the widened ref shows up there.
        expected = {
            "claude": [str(Path("/repo")), "/repo/src"],
            "codex": ["/repo/src"],
        }
        for backend, want in expected.items():
            with self.subTest(backend=backend):
                argv = wb_exec.build_executor_argv(
                    backend, [backend], "p", Path("/repo"), "{}", ["/repo/src/a.py"]
                )
                self.assertEqual(want, self._values(argv, "--add-dir"))

    def test_aider_still_reads_the_named_files(self):
        """aider's `--read` takes files, so its refs must not be widened."""
        argv = wb_exec.build_executor_argv(
            "aider", ["aider"], "p", Path("/repo"), "{}", ["/repo/src/a.py"]
        )
        self.assertEqual(["/repo/src/a.py"], self._values(argv, "--read"))


if __name__ == "__main__":
    unittest.main()
