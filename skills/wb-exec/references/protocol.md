# wb-exec protocol

## Task package (input)

```json
{
  "goal": "One sentence: what changed once the work is done",
  "context": "Necessary background: why this is needed, which module is involved, known pitfalls",
  "inputs": ["src/auth/mw.py", "docs/auth.md"],
  "owned_paths": ["src/auth/", "tests/test_auth_mw.py"],
  "acceptance": ["pytest -q tests/test_auth_mw.py"],
  "notes": "Extra constraints, for example: do not add dependencies"
}
```

| Field | Required | Meaning |
|---|---|---|
| `goal` | yes | The objective, without implementation steps. The script renders it into the executor prompt |
| `context` | no | Background and known constraints |
| `inputs` | no | Read-only reference material; paths are passed to the executor with `--add-dir` |
| `owned_paths` | no | **Whitelist of writable paths.** When empty the executor may only create new files. Always use `/` as the path separator |
| `acceptance` | no | Executable commands; the executor should run them and record the outcome in `tests` |
| `notes` | no | Catch-all constraints |

`owned_paths` matching: a path qualifies when it is exactly equal, or starts with the entry plus `/`.
`src/auth/` covers `src/auth/mw.py`; `src/auth` covers it too. Anything outside the whitelist is recorded in `result.violations`.

## Result (output)

```json
{
  "ok": true,
  "job_id": "3f9c1a7b2e04",
  "status": "done",
  "exit_code": 0,
  "elapsed_s": 42.1,
  "result": {
    "summary": "2-4 sentences, no code",
    "changed_files": ["src/auth/mw.py"],
    "tests": [{"cmd": "pytest -q", "status": "pass", "detail": "12 passed"}],
    "blockers": [],
    "notes": "",
    "artifacts": {"stdout": "...", "stderr": "...", "patch": "..."},
    "changed_files_git": ["src/auth/mw.py"],
    "violations": []
  }
}
```

- `ok` = status is `done` **and** the structured result was parsed. `ok=true` does not mean the code is correct; the host still has to read the patch and run acceptance.
- `changed_files` comes from the executor's own account; `changed_files_git` comes from the real `git diff`. Trust `changed_files_git`.
- `violations` is **always present** — `[]` on a clean run. It can only be non-empty when `owned_paths` was supplied, because without a whitelist there is nothing to violate.
- `isolated` on the collect envelope says whether a worktree was actually used. When it is `false`, no diff was captured at all, so an empty `changed_files_git` means "unknown", not "nothing changed".

## State machine

`running` → (`done` | `failed` | `timeout` | `cancelled` | `stalled`)

- `stalled` is derived: `exit.json` is absent and the heartbeat is older than 90 seconds. The process may still be running or already dead; `collect` first and read the tail of the output.
- `cancel` writes a flag file; the supervisor reacts within 0.5 seconds.

## Directory layout

```
~/.wb-exec/jobs/<job_id>/
  job.json        task package snapshot, actual argv, cwd, worktree, timeout
  heartbeat.json  updated by the supervisor every 5 seconds
  stdout.jsonl    raw executor output (stream-json)
  stderr.log      executor log; model and permission errors usually land here
  change.patch    the real diff (Git repository with isolation enabled)
  result.json     parsed structured result
  exit.json       state, exit code, elapsed time
  cancel.request  cancellation flag
  wt/             the dedicated worktree (--worktree auto on a Git repository)
```

`~/.wb-exec` can be overridden with the `WB_EXEC_HOME` environment variable.

## Isolation semantics

- `--worktree auto` (default) on a Git repository: create a `wb-exec/<id8>` branch and a `wt/` worktree from the `HEAD` at dispatch time. **No commit, no push, no auto-merge**, and the user's uncommitted changes are not carried over.
- Non-Git repository: in-place execution, `isolated=false`, no diff capture. `owned_paths` is then the only defence and must be written.
- Removing the worktree is the user's decision; the script never deletes it.

## Recursion and permissions

- The child environment always carries `WB_SUBAGENT=1` and `CROSS_CLI_COLLAB_WORKER=1`; when the script sees `WB_SUBAGENT` it refuses to dispatch with exit code 3.
- The actual child command includes `-p --output-format stream-json --json-schema <schema> -y --allowedTools Read,Write,Edit,Glob,Grep,Bash --add-dir <cwd> --session-id <uuid>`.
- `-y` skips routine permission prompts (HIGH/CRITICAL cases may still ask), which is why **directory isolation and `owned_paths` are the real security boundary** rather than optional extras.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Execution failed, or the result could not be parsed (`collect` returns `ok=false`) |
| 3 | Recursion refused (this process is already an executor) |

## Executors (pluggable)

`--executor` (or `WB_EXECUTOR`) selects which CLI runs the task. The rest of the pipeline — worktree isolation, heartbeats, structured capture, the out-of-scope check — is identical for every backend.

| Backend | Entry point | Model and effort |
|---|---|---|
| `workbuddy` (default) | probed `codebuddy`, or the CLI inside a local WorkBuddy install; `WB_CLI` overrides | pinned: `deepseek-v4.1-flash`, effort `max`, injected by `wb-exec` |
| `codex` | `codex` on `PATH`, or `WB_CLI` | whatever that CLI is configured with; `wb-exec` injects nothing |
| `claude` | `claude` / `claude-code` on `PATH`, or `WB_CLI` | same |
| `aider` | `aider` on `PATH`, or `WB_CLI` | same |
| `custom` | `WB_CLI` | same |

A backend name outside that table fails loudly: `find_cli` raises and `doctor` reports `ok=false`. There is no silent fallback to `workbuddy`, because falling back would run the task on a different model than the user selected.

### workbuddy (pinned)

Every dispatch hard-codes `--model deepseek-v4.1-flash --effort max`.

- There is exactly one model on this backend at exactly one effort. No second model and no effort options.
- The model ID comes from resolving the local product configuration (the `resolved ids` in `main.log` contain `deepseek-v4.1-flash`); the display name "DeepSeek V4.1 Flash" cannot be used as an ID.
- The CLI exposes no `--model` or `--effort`; passing either is rejected by argparse (`unrecognized arguments`), deliberately.
- When the account or plan refuses that model, the CLI exits non-zero and writes the reason to `stderr.log`. **No downgrade, no model switch** — hand the raw error to the user.
- This setting applies only to the dispatched executor and never to the host's own session.

### custom template

`WB_CLI_ARGV` is a JSON array of arguments. These placeholders expand per job: `{prompt}`, `{cwd}`, `{schema}`, `{session}`, `{model}`, `{effort}`. Example:

```bash
WB_CLI=my-agent \
WB_CLI_ARGV='["run","--prompt","{prompt}","--dir","{cwd}"]' \
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task t.json --executor custom
```

A `custom` backend that cannot produce structured output will surface as `ok=false` with a parse blocker; check `artifacts.stdout` before blaming isolation.

## Sessions and resuming

The script generates a fixed `--session-id` per job and records it in the job's `argv` inside `job.json`. To make an executor continue from its existing context, resume by hand with the same `session-id` plus `-r`. The normal approach, however, is to dispatch a smaller new package rather than resume a package that timed out.