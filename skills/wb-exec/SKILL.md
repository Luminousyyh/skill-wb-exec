---
name: wb-exec
description: Delegate bounded coding, refactoring and test-writing tasks to a headless WorkBuddy executor (DeepSeek V4.1 Flash) and collect a structured result plus the real diff. Use when the task can be finished independently, the file set is enumerable, and the host agent only wants to plan and review instead of writing code itself.
whenToUse: When implementation-shaped subtasks should be outsourced to another CLI agent; when you want a cheaper executor for volume while you (the host) keep planning and review; when changes need an isolated worktree.
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

**The executor is fixed**: WorkBuddy / DeepSeek V4.1 Flash at effort `max`. The script hard-codes it and exposes no `--model` or `--effort`; do not try to specify either. When the account or plan refuses that model the run fails — **do not retry with another model**. Report `stderr.log` to the user.

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