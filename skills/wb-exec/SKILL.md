---
name: wb-exec
description: Delegate bounded coding, refactoring and test-writing tasks to a headless CLI executor (WorkBuddy by default, or codex/claude/aider/custom) and collect a structured result plus the real diff. Use when the task can be finished independently, the file set is enumerable, and the host agent only wants to plan and review instead of writing code itself.
metadata:
  # Codex reads name/description/metadata; a bare `whenToUse` key was not part of
  # the frontmatter shape any shipped skill uses, so the same guidance lives in
  # the key Codex demonstrably defines. The `description` above still carries the
  # "use when" clause, so nothing is lost for loaders that only read that.
  short-description: "Outsource bounded implementation to a headless CLI executor: enumerable file sets, cheaper volume work, or changes needing an isolated worktree."
---

# wb-exec

You are the host: you plan, supervise and review. **The WorkBuddy subprocess does the implementation; you do not write business code yourself.**

Always address the script through this skill's absolute directory, never through the terminal's current directory:

```
python <SKILL_DIR>/scripts/wb_exec.py <subcommand> ...
```

On Windows, if `python` is not on `PATH`, use `py -3` or the absolute path of a local Python.

## 0. Self-check first (at most once per session)

```
python <SKILL_DIR>/scripts/wb_exec.py doctor
```

Returns JSON with `ok` / `cli` (the resolved entry point) / `cli_version` / `git` / `is_git_repo` / `state_root`.
`ok=false` or a non-empty `cli_error` → stop, report the raw output to the user, and do not install or reconfigure anything yourself.

`doctor` proves the CLI is present and answers `--version`. It does **not** prove authentication, quota or model access — a run can still fail after a clean self-check, and that failure belongs in `stderr.log`, not in a guess.

## 1. Write the task package (this is your most important job)

The task package is JSON; field definitions are in `references/protocol.md` and an example is in `assets/task.example.json`. Three hard rules:

1. `goal` says in one sentence what changed once the work is done. No implementation steps.
2. `owned_paths` must be enumerable. For a directory write `src/auth/`; for a file write the full path. **An empty list means the executor may only create new files.**
3. `acceptance` is executable commands (`pytest -q tests/test_auth.py`), never "make sure it works".

Write the package to a file first (for example `.wb-exec/tasks/001.json`), then dispatch. Never stuff the package into command-line arguments.

## 2. Dispatch

Long jobs — most of them — go to the background:

```
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo>
```

Short jobs can be waited on in the foreground:

```
python <SKILL_DIR>/scripts/wb_exec.py run --task .wb-exec/tasks/001.json --cwd <repo> --timeout 600
```

Common flags:
- `--dry-run`: print the argv that would run, to confirm paths and flags; starts nothing.
- `--worktree off`: disable isolation (only when the directory is not a Git repository, or when you explicitly accept in-place changes).
- `--timeout`: seconds, default 1800.
- `--executor`: which CLI runs the task. `workbuddy` (default), `codex`, `claude`, `aider`, or `custom`. `WB_EXECUTOR` sets the same default through the environment.

**The `workbuddy` backend is pinned**: WorkBuddy / DeepSeek V4.1 Flash at effort `max`, hard-coded, with no `--model` or `--effort` flag. When the account or plan refuses that model the run fails — **do not retry with another model**. Report `stderr.log` to the user. The other backends use whatever their own CLI is configured with; `wb-exec` injects no model or effort for them, so do not claim a specific model on those paths.

Do not switch backends to "make it work" after a failure. Pick the backend the user has installed, and only change it when the user asks.

## 3. Supervise

```
python <SKILL_DIR>/scripts/wb_exec.py status --job <JOB_ID>
```

- `running`: normal. Look again in 30–60 seconds; never poll per second.
- `stalled`: the heartbeat is older than 90 seconds. `collect` first to see where it stopped, then decide whether to `cancel` and re-dispatch a smaller package, or re-run at another effort tier.
- `cancel_requested`: the cancellation flag is written; waiting for the process to exit.

Cancel:

```
python <SKILL_DIR>/scripts/wb_exec.py cancel --job <JOB_ID>
```

## 4. Collect and review

```
python <SKILL_DIR>/scripts/wb_exec.py collect --job <JOB_ID> --wait
```

Result shape (excerpt):

```json
{
  "ok": true,
  "status": "done",
  "result": {
    "summary": "Added token validation middleware and 3 tests",
    "changed_files": ["src/auth/mw.py", "tests/test_mw.py"],
    "tests": [{"cmd": "pytest -q tests/test_mw.py", "status": "pass"}],
    "blockers": []
  },
  "result.artifacts": {"patch": ".../change.patch", "stdout": ".../stdout.jsonl"},
  "result.violations": ["src/billing/x.py"]
}
```

Review discipline:

1. **Read the patch yourself before judging.** `artifacts.patch` is the real diff; `summary` is only the executor's own account. When they disagree, the diff wins.
2. A non-empty `violations` means the executor wrote files outside its assignment. Do not quietly accept it and do not revert it yourself — report it to the user first.
3. A `pass` inside `tests` is not independent verification. After integrating, run the acceptance commands yourself in the main working tree.
4. Integration is yours: `git apply --check` → `git apply` → run acceptance → report. Do not run `git commit` or `git push`; that requires an explicit request from the user.

## 5. Boundaries (violating them causes incidents)

- **Never recurse**: the executor is already forbidden to dispatch, and you must not dispatch a second task over the same files while a worker is running.
- **Git isolation starts from `HEAD`**: it does not copy the user's uncommitted edits. If the task needs files that are not committed yet, commit or stage them first, because the worktree will not contain them and the run will fail on a missing input.
- **A failed isolation attempt stops the job**: the script aborts instead of falling back to editing the source tree in place. Never work around that by re-running with `--worktree off` unless in-place execution is what the user actually authorised.
- **Never trigger `dispatch` from inside a WorkBuddy / CodeBuddy session**: the child process collides with that session's daemon and hangs with zero output — measured at 45–300 seconds with no stdout or stderr at all. Call this script from the host agent or a normal terminal.
- **Never modify the same file in parallel**: the `owned_paths` of concurrent jobs must not overlap; overlapping work is serialised.
- **Never return full logs to the user**: give `summary`, the relevant `tests` and `violations`. The logs stay in the `artifacts` paths for on-demand reading.
- **Never re-run by hand with `-y`**: the script already handles the permission flags; bypassing the script bypasses isolation and the out-of-scope check.
- **Never clean up the user's worktree**: after `collect` the directory stays in place for review, and removal is the user's decision.

## Failure handling

| Symptom | Action |
|---|---|
| `exit_code != 0` with a non-empty `blockers` | Carry the blocker back into planning unchanged, narrow or split the package, re-dispatch once |
| `ok=false, blockers=["could not parse structured result"]` | Read the tail of `artifacts.stdout`; usually the executor did not follow the schema. Add "the last message must be JSON" to the package `notes` |
| `status=timeout` | The package is too large. Split it into 2–3 packages whose `owned_paths` do not overlap |
| `status=stalled` | `collect`, then `cancel`; re-dispatch with `--worktree auto` |
| `cli_error` | Stop and report to the user; do not install dependencies |