# wb-exec

> Delegate bounded implementation work to another CLI agent; keep planning, supervision and review for yourself.

`wb-exec` is a **DeepSeek Harness plugin (skill bundle)**: the host agent hands **coding / refactoring / test-writing** subtasks to a **headless CLI executor** and gets back a **structured result** plus the **real diff**.

The default executor is **WorkBuddy / CodeBuddy (DeepSeek V4.1 Flash, effort `max`)**, pinned on purpose. If you do not have it, switch backends with `--executor` — `codex`, `claude`, `aider`, or `custom` (any CLI, via an argument template). Isolation, supervision, structured capture and the out-of-scope check are identical on every backend.

> ⚠️ **Prerequisite.** Whichever backend you pick must be installed on the same machine. Without it the plugin installs but the skill cannot do anything: its first step, `doctor`, reports `ok=false` and stops. It never installs dependencies or rewrites configuration on its own.

**Fits:** tasks with a clear boundary that can be finished independently, with an enumerable set of files; using a cheaper executor for volume while the host only plans and reviews; work that deserves an isolated worktree, reviewed by a human before it lands.

**Does not fit:** exploratory work, requirements still moving, cross-module rewrites, or anything that needs architectural judgement. Keep those in the host.

![Figure 1](docs/figures/fig1-architecture.png)

*Figure 1. Install-time registration, run-time delegation to the executor, and the audit path that returns artifacts to the review gate — drawn as three columns, read left to right: installation, execution, audit. The execution column shows the ordering that matters: `dispatch` claims the paths from the lock *before* it spawns the CLI executor, and the executor edits only the worktree `wt/`, bounded by the `owned_paths` whitelist. The footnote restates that bound and records that a corrupt lock is refused rather than treated as empty.*

---

## Features

| Feature | What it does |
|---|---|
| **Path whitelist** | `owned_paths` in the task package is the set of paths the executor may touch. Changes outside it are detected and reported in `result.violations` rather than silently accepted. An empty list is enforced as **additions only**, not as "no check" |
| **Failures stay collectable** | If dispatch or the supervisor fails, `exit.json` and `result.json` are still written, so the job arrives as `failed` with a blocker instead of vanishing. The CLI is probed before any claim is taken, so a missing executor leaves no live lock |
| **Directory isolation** | On a Git repository the run happens in a dedicated worktree on a `wb-exec/<id8>` branch: no commit, no push, no auto-merge |
| **Real diff first** | The executor's `summary` is only its own account. `change.patch` and `changed_files_git` come from the actual `git diff`; when the two disagree, the diff wins |
| **Structured capture** | A `--json-schema` constraint forces structured output; the script then adds the git diff and the out-of-scope check to produce a machine-readable `result.json` |
| **Recursion guard** | The child process gets `WB_SUBAGENT=1`; the script refuses to dispatch when it sees that variable (exit code 3), so an executor cannot spawn another executor |
| **Path-level dispatch lock** | A second dispatch whose `owned_paths` overlap a running job is refused (`PATHS_LOCKED`, exit code 4) instead of racing it. Disjoint paths still run in parallel. Whether a claim is still alive is decided by the recorded **pid**, not by a heartbeat age: a supervisor that died before writing its first heartbeat leaves no heartbeat while the executor it spawned may still be editing the claimed paths |
| **Failed-closed lock** | A `dispatch.lock` that exists but cannot be parsed is refused rather than treated as "no claims". Coercing a damaged lock to empty would admit every job and then rewrite the file with only the new claim, discarding live holds silently |
| **Bounded output** | `stdout.jsonl` plus `stderr.log` are capped (64 MiB, `WB_EXEC_LOG_CAP` overrides). Crossing the cap kills the child, marks the job `failed` and adds a blocker naming the limit, so a truncated transcript can never be read as a finished run |
| **`jobs`, read-only** | One command prints the live claims (paths, pid, liveness, age) and the recent job history. It never prunes or releases anything, so a refused user has a way to see what is holding the paths instead of deleting the lock by hand |
| **`needs_human`, not prose** | Every result carries a structured `needs_human` (`{"required": bool, "reasons": [...]}`) derived from status, `blockers`, failing tests and `violations`. "A person must decide something" is no longer something you infer by reading `summary` |
| **Non-blocking long runs** | `dispatch` returns a `job_id` immediately; `status` polls, `collect` retrieves, and both heartbeat and timeout are observable |
| **No dependencies** | Python standard library only, Python 3.10+ |

---

## How it works

![Figure 2](docs/figures/fig2-workflow.png)

*Figure 2. One delegation, drawn as a single unidirectional pass: task authoring, path claim, CLI spawn, worktree edit and `git diff`, with the observable outputs collected alongside. No cycle is drawn on purpose — a failed review does not re-run the same executor, it re-splits into a smaller package, which is the `re-split task` edge in Figure 1. The host never merges on its own: review is a separate, mandatory step.*

1. The host writes the task package to disk as JSON (`goal`, `owned_paths`, `acceptance`, ...)
2. `dispatch` (background) or `run` (foreground) starts the call; Git repositories get a dedicated worktree by default
3. The executor works and emits stream-json events; the supervisor writes a heartbeat every 5 seconds
4. `collect` parses the structured result and merges in the real diff and the out-of-scope check
5. **The host reads the patch and runs the acceptance commands itself before integrating anything.** `wb-exec` never integrates for you

---

## Requirements

- **Python 3.10+** (standard library only, no `pip install`)
- **One supported CLI executor** — pick with `--executor` or `WB_EXECUTOR`:
  - `workbuddy` (default): the script probes for a `codebuddy` executable and for the CLI embedded in a local WorkBuddy install; set `WB_CLI` to point at one explicitly
  - `codex`, `claude`, `aider`: found on `PATH`, or via `WB_CLI`
  - `custom`: `WB_CLI` (the executable) plus `WB_CLI_ARGV` (a JSON array argument template, see below)
- **Git** — optional. Used for worktree isolation and diff capture; a non-Git directory degrades to in-place execution
- On the `workbuddy` backend, an account or plan that is allowed to use `deepseek-v4.1-flash`

Check the environment first:

```bash
python <SKILL_DIR>/scripts/wb_exec.py doctor
python <SKILL_DIR>/scripts/wb_exec.py doctor --executor codex   # check another backend
```

It returns JSON with `ok` / `cli` (the resolved entry point) / `cli_version` / `executor` / `available_executors` / `git` / `is_git_repo` / `state_root`. When `ok=false` or `cli_error` is non-empty, stop and hand the raw output to the user — do not install anything or change configuration.

---

## Install

This repository is a **DSH plugin package** (`package.json` declares `dsh.bundle`). Once installed, a `ctx.skills` provider publishes `skills/wb-exec/` as a skill.

**Option 1 — plugin market or CLI (recommended)**

Install it from **Plugins → Plugin Market** in DSH Desktop, or from a terminal:

```bash
dsh plugin --profile <profile> add github:Luminousyyh/skill-wb-exec
```

> DSH Desktop manages its own profile. A CLI install may land in `profiles/web` instead of the profile the desktop app uses; on the desktop, install through **Settings → Plugins**.

**Option 2 — use it as a plain skill (no plugin)**

Copy `skills/wb-exec/` into your agent's skills directory. The loader discovers a skill at `<skills-root>/<name>/SKILL.md`:

```bash
git clone https://github.com/Luminousyyh/skill-wb-exec.git
cp -r skill-wb-exec/skills/wb-exec ~/.agents/skills/wb-exec
```

Always invoke the script through **this skill's absolute path** rather than the terminal's current directory:

```bash
python <SKILL_DIR>/scripts/wb_exec.py <subcommand> ...
```

On Windows, if `python` is not on `PATH`, use `py -3` or the absolute path of a local Python.

---

## Quick start

### 1. Write the task package (the most important step)

Save it as a file, for example `.wb-exec/tasks/001.json`:

```json
{
  "goal": "Rate-limit failed logins on /login: lock an account for 10 minutes after 5 consecutive failures within 5 minutes, and return 429",
  "context": "The rate-limiting middleware lives in src/auth/mw.py. A Redis client wrapper already exists in src/common/redis.py; reuse it and do not create a new connection pool.",
  "inputs": ["src/auth/mw.py", "src/common/redis.py", "tests/test_auth.py"],
  "owned_paths": ["src/auth/", "tests/test_auth.py"],
  "acceptance": ["pytest -q tests/test_auth.py"],
  "notes": "Do not add dependencies. Count failures with Redis INCR + EXPIRE under the key auth:fail:<account>."
}
```

Three hard rules:

1. `goal` says what changed once the work is done, in one sentence — **no implementation steps**
2. `owned_paths` must be enumerable; **an empty list means the executor may only create new files**
3. `acceptance` holds executable commands, never "make sure it works"

### 2. Inspect the call before making it (optional)

```bash
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo> --dry-run
```

`--dry-run` prints the argv and starts nothing, which is how you check paths and flags.

### 3. Dispatch

Long jobs — most of them — go to the background:

```bash
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo>
```

Short jobs can be waited on in the foreground:

```bash
python <SKILL_DIR>/scripts/wb_exec.py run --task .wb-exec/tasks/001.json --cwd <repo> --timeout 600
```

### 4. Supervise

```bash
python <SKILL_DIR>/scripts/wb_exec.py status --job <JOB_ID>
```

- `running` — normal; look again in 30–60 seconds, never in a tight loop
- `stalled` — the heartbeat is more than 90 seconds old. `collect` first to see where it stopped, then decide whether to `cancel` and re-dispatch a smaller package
- `cancel_requested` — the cancellation flag is written; waiting for the process to exit

```bash
python <SKILL_DIR>/scripts/wb_exec.py cancel --job <JOB_ID>
```

### 5. Collect and review

```bash
python <SKILL_DIR>/scripts/wb_exec.py collect --job <JOB_ID> --wait
```

The result (excerpt):

```json
{
  "protocol": 1,
  "ok": true,
  "job_id": "3f9c1a7b2e04",
  "status": "done",
  "exit_code": 0,
  "elapsed_s": 42.1,
  "isolated": true,
  "executor": "workbuddy",
  "worktree": ".../wt",
  "result": {
    "summary": "Added token validation middleware and 3 tests",
    "changed_files": ["src/auth/mw.py", "tests/test_mw.py"],
    "tests": [{"cmd": "pytest -q tests/test_mw.py", "status": "pass", "detail": "12 passed"}],
    "blockers": [],
    "artifacts": {"patch": ".../change.patch", "stdout": ".../stdout.jsonl", "stderr": ".../stderr.log"},
    "changed_files_git": ["src/auth/mw.py"],
    "violations": [],
    "needs_human": {"required": false, "reasons": []}
  }
}
```

`needs_human` is the field to route on. It is `false` here because the status is
`done`, there are no blockers, no failing tests and no out-of-scope writes. It
becomes `true` with a reason list such as `["failed"]`, `["out_of_scope_writes"]`,
`["blocked"]` or `["failing_tests"]` — never by reading the summary's wording.

`protocol` is the envelope version. It is a plain integer that only changes when
the shape of this envelope changes, so a host written against an older release
can tell what it is reading instead of failing deep inside its own logic after
an upgrade. Unknown keys are safe to ignore; this one is not inferred from text.

**Review discipline — none of these is optional:**

1. **Read the patch yourself before judging.** `artifacts.patch` is the real diff; `summary` is only the executor's account. When they disagree, the diff wins
2. A non-empty `violations` means the executor wrote outside its assignment. Do not quietly accept it and do not revert it yourself — report it to the user first
3. A `pass` inside `tests` **is not independent verification**. After integrating, run the acceptance commands yourself in the main working tree
4. Integration belongs to the host: `git apply --check` → `git apply` → run acceptance → report. **Do not run `git commit` or `git push`** unless the user explicitly asks for it

---

## Command reference

| Subcommand | Arguments | Purpose |
|---|---|---|
| `doctor` | `[--cwd]` `[--executor]` | Self-check: CLI, git, environment, state directory, available backends |
| `dispatch` | `--task` `[--cwd]` `[--timeout 1800]` `[--worktree auto\|off]` `[--executor]` `[--dry-run]` | Dispatch in the background, return a `job_id` immediately |
| `run` | same, minus `--dry-run` | Run in the foreground and wait for collection |
| `status` | `--job` | Query the job state |
| `collect` | `--job` `[--wait]` `[--wait-timeout 1800]` | Collect the result; `--wait` blocks until the job ends |
| `cancel` | `--job` | Request cancellation (writes a flag file; the supervisor reacts within 0.5 s) |
| `jobs` | `[--limit 20]` | Read-only listing of the live claims (paths, pid, liveness, age) and the recent job history. It prunes and releases nothing |

General notes:

- `--timeout` is in seconds and defaults to `1800`
- `--worktree auto` (default) creates a dedicated worktree on a Git repository; `off` runs in place
- `--executor` selects the backend (`workbuddy` by default; also `codex`, `claude`, `aider`, `custom`). `WB_EXECUTOR` sets the same default through the environment
- **The `workbuddy` backend is pinned**: DeepSeek V4.1 Flash at effort `max`, hard-coded, with no `--model` or `--effort` flag (passing one is rejected by argparse on purpose). When the account refuses that model the run fails; **do not retry with a different model**, hand `stderr.log` to the user. Other backends use whatever model their own CLI is configured with — `wb-exec` injects no model or effort flag for them
- **On this backend the prompt travels on stdin, not on argv.** A multi-line argv prompt is truncated to its first line by the CLI, and the task package is always multi-line, so `-p` acts as the read-from-stdin switch. `job.json` therefore stores the prompt itself: `argv` alone no longer reproduces a run

### Choosing a backend

```bash
# default: WorkBuddy / CodeBuddy
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task t.json --cwd <repo>

# any other supported CLI
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task t.json --cwd <repo> --executor claude

# anything else, through an argument template
WB_CLI=my-agent WB_CLI_ARGV='["run","--prompt","{prompt}","--dir","{cwd}"]' \
  python <SKILL_DIR>/scripts/wb_exec.py dispatch --task t.json --cwd <repo> --executor custom
```

The `custom` template may use `{prompt}`, `{cwd}`, `{schema}`, `{session}`, `{model}` and `{effort}`; each placeholder is expanded for the job before the process starts. A backend name that does not exist fails loudly rather than silently falling back to WorkBuddy.

---

## Task package protocol

| Field | Required | Meaning |
|---|---|---|
| `goal` | yes | The objective, without implementation steps; the script renders it into the executor prompt |
| `context` | no | Background and known constraints |
| `inputs` | no | Read-only reference material; paths are passed to the executor with `--add-dir` |
| `owned_paths` | no | **Whitelist of writable paths**; when empty only new files may be created, and edits or deletions of existing files are reported. Always use `/` as the separator. Entries are resolved against `--cwd`; one that escapes it, or the repository root itself, is rejected |
| `acceptance` | no | Executable commands; the executor should run them and record the outcome in `tests` |
| `notes` | no | Catch-all constraints |

`owned_paths` matching: a path qualifies when it is **exactly equal, or starts with the entry plus `/`**. `src/auth/` covers `src/auth/mw.py`; `src/auth` covers it too. Anything outside the whitelist is recorded in `result.violations`. An empty list is not "no check": additions pass, but a run that modifies or deletes an existing file shows up there, which is what "new files only" has always meant.

The task file is read as `utf-8-sig`, so a package written by Windows PowerShell with a BOM loads instead of failing on a stray character.

Full field definitions and the state machine live in [protocol.md](skills/wb-exec/references/protocol.md); a ready-to-edit template is [task.example.json](skills/wb-exec/assets/task.example.json).

---

## States and failure handling

![Figure 3](docs/figures/fig3-states.png)

*Figure 3. State classification. The decision sequence — spawn, heartbeat write, staleness check, `exit.json` probe, classify — points at the persisted state it selects; branches are listed beside the sequence rather than drawn as a fan-out. The footnote gives the criteria for the four terminal states and records that `stalled` is derived at read time from heartbeat age, never stored as a sixth state, and is not reported by the executor.*

State machine: `running` → `done` | `failed` | `timeout` | `cancelled` | `stalled`

`stalled` is derived (`exit.json` absent and the heartbeat older than 90 seconds). The process may still be alive or already dead — `collect` first and read the tail of the output.

| Symptom | Action |
|---|---|
| `exit_code != 0` with a non-empty `blockers` | Carry the blocker back into planning, narrow or split the package, re-dispatch **once** |
| `ok=false, blockers=["could not parse structured result"]` | Read the tail of `artifacts.stdout`; usually the executor did not follow the schema. Add "the last message must be JSON" to `notes` |
| `status=timeout` | The package is too large. Split it into 2–3 packages whose `owned_paths` do not overlap |
| `status=stalled` | `collect`, then `cancel`; re-dispatch with `--worktree auto` |
| `cli_error` | Stop and report to the user; do not install dependencies |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Execution failed, or the result could not be parsed (`collect` returns `ok=false`) |
| 3 | Recursion refused (this process is already an executor) |

---

## Safety boundaries

![Figure 4](docs/figures/fig4-isolation.png)

*Figure 4. The whitelist and the worktree are enforced in code rather than requested in the prompt. Left: the worktree is cut from `HEAD` at dispatch, so uncommitted changes do not carry over. Right: every changed path is classified — in-scope paths land in `changed_files_git`, out-of-scope paths in `violations`, and they are reported rather than auto-reverted. The footnote states the three hard constraints: no commit, no push, no auto-merge. This is isolation, not confinement: a process that ignores the boundary is not stopped by it.*

- **Directory isolation and `owned_paths` are the real security boundary, not optional extras.** On the `workbuddy` backend the child command passes both `-y` and `--permission-mode bypassPermissions`. `-y` alone is documented as "HIGH/CRITICAL still ask", and a non-interactive run has no prompt to answer, so `Write`/`Edit`/`Bash` were refused and every task came back with blockers and an untouched worktree at exit code 0. Isolation plus a whitelist is what actually constrains the run
- **Never recurse**: the executor is already forbidden to dispatch. The dispatch lock also refuses a second job whose paths overlap a running one, so the old "do not do this" convention is now enforced rather than remembered
- **Isolation failure aborts the dispatch**: if the worktree cannot be created, nothing runs. There is no silent fallback to editing the source tree in place, because that would quietly turn an isolation guarantee into no isolation
- **An unusable CLI fails loudly**: the executor is found and probed before any path is claimed, and an explicit `WB_CLI` is authoritative — a broken one fails the run rather than falling back to another installation. A missing CLI therefore leaves no live lock
- **Never trigger `dispatch` from inside a WorkBuddy / CodeBuddy session**: the child process collides with that session's daemon and hangs silently — measured at 45–300 seconds with no stdout or stderr at all. Call the script from the host agent or a normal terminal
- **Overlapping work is refused, not queued**: `dispatch` exits `4` with `error: PATHS_LOCKED` and names the holding job. Run `jobs` to see what is holding the paths, then widen the split, wait, or cancel the other job — do not work around it by clearing the lock file
- **A runaway executor is stopped, not tolerated**: the combined `stdout.jsonl` and `stderr.log` size is checked in the same supervision loop as cancel and timeout. Crossing the cap kills the child and records the cap in `blockers`, so a truncated transcript is never mistaken for a completed run
- **Never dump full logs at the user**: give `summary`, the relevant `tests` and `violations`; the logs stay in the `artifacts` paths for on-demand reading
- **Never re-run by hand with `-y`**: the script already handles the permission flags; bypassing it bypasses isolation and the out-of-scope check
- **Never clean up the user's worktree**: after `collect` the directory stays in place for review, and removal is the user's decision
- **In-place execution on a non-Git directory** (`isolated=false`, no diff capture) leaves `owned_paths` as the only defence — **it must be written**
- **The lock is not a security boundary either**: it stops two cooperating jobs from colliding, not a process that ignores it. Two projects with separate `WB_EXEC_HOME` do not see each other's claims

---

## What it deliberately does not do

Eleven features were considered and dropped on purpose; the reasoning is in
[rejected-designs.md](skills/wb-exec/references/rejected-designs.md). The short
version:

| Not a | Because |
|---|---|
| Scheduler | Retrying is a judgement about whether the work is still wanted; a silent retry can duplicate a change you already rejected |
| Auto-merger | An agent that both writes and grades the change has verified nothing |
| Sandbox | A process running as your OS user can reach anything you can. This is isolation, not confinement |
| Model switcher | A pinned executor keeps results comparable, and removes the temptation to "try another model" instead of reading `stderr.log` |
| Mind reader | `needs_human` comes from structured fields only, never from the wording of `summary` |

---

## State directory layout

```
~/.wb-exec/
  dispatch.lock   claims of every running job: job_id, owned_paths, pid, since
  dispatch.guard  transient mutex held only while dispatch.lock is rewritten
  jobs/<job_id>/
    job.json        task package snapshot, actual argv, cwd, worktree, timeout,
                    the prompt itself and the log cap in force at dispatch time
    heartbeat.json  updated by the supervisor every 5 seconds
    stdout.jsonl    raw executor output (stream-json), capped with stderr.log
    stderr.log      executor log; model and permission errors usually land here
    change.patch    the real diff (Git repository with isolation enabled)
    result.json     parsed structured result
    exit.json       state, exit code, elapsed time
    cancel.request  cancellation flag
    wt/             the dedicated worktree (--worktree auto on a Git repository)
```

Override the state root with the `WB_EXEC_HOME` environment variable.

`dispatch.lock` is advisory between cooperating jobs and scoped to a single `WB_EXEC_HOME`. It is **not** a security boundary: a process that ignores it is not stopped by it.

Two properties of the lock are worth knowing before you rely on it. A claim is
considered live while its job has no `exit.json` **and** either has a heartbeat
or its recorded pid can still be probed as running; liveness is a fact about the
process, not a guess from a missing file. A lock that cannot be parsed is
refused, never silently replaced — a safety file that disables itself on
corruption is worse than one that blocks and tells you where it is.

**Isolation semantics**: `--worktree auto` (default) on a Git repository creates a `wb-exec/<id8>` branch and a `wt/` worktree from the `HEAD` at dispatch time. Nothing is committed, pushed or auto-merged, and the user's uncommitted changes are **not** carried over. Removing the worktree is the user's decision; the script never deletes it.

---

## Development and testing

The tests stub the CLI, so **they run offline without WorkBuddy installed**:

```bash
python -m unittest discover -s skills/wb-exec/tests -v
# or one file
python skills/wb-exec/tests/test_wb_exec.py
```

Nothing calls a model. `tests/fake_cli.py` replays a stream-json transcript, so the suite runs offline.

- **Unit tests** cover environment injection, a standalone CLI entry taking precedence over the embedded one, a native error result never being reported as success, plain prose never counting as a structured success, and the executor backends — including a test that pins the WorkBuddy argv so a refactor cannot change it silently.
- **Lock tests** pin both directions: overlapping and nested paths are refused, a path prefix is not treated as a boundary (`src/auth` ≠ `src/authz`), Windows separators normalise, disjoint paths still run. Claim liveness is tested from the side that matters: a job directory with **no heartbeat but a live pid** must still block (that was the crash window where an orphan kept writing), a provably dead pid must be pruned, and an unprobeable pid falls back to the old heuristic so the lock cannot wedge forever. A corrupt lock is asserted to refuse and to be left byte-identical on disk.
- **`jobs` tests** pin that the listing reports claims and history, that it is strictly read-only (the lock file is byte-identical before and after), that a dead claim is shown but not removed, and that a corrupt lock is reported with exit code 0.
- **Desktop-regression tests** pin what the Windows installation made easy to get wrong: a UTF-8 BOM package loads, `owned_paths` escaping the working directory is refused, a missing CLI leaves no live claim, an isolation failure never falls back to in-place editing, a spawn failure still produces a collectable terminal result, and a timeout still fires when the executor never reads its stdin
- **`needs_human` tests** pin the derivation: a clean success needs nobody, while failure, out-of-scope writes, blockers and failing tests each raise it — and passing tests alone do not.
- **End-to-end tests** drive the real script through `dispatch` → `status` → `collect` against a temporary Git repo: a run produces a patch, the main working tree is left alone, an out-of-scope write is reported in `violations`, `--dry-run` leaves no job directory behind, a dispatched executor cannot dispatch again (exit code 3), and a chatty executor that exceeds the output cap is stopped with the cap named in `blockers` rather than reported as done.

## Project layout

```
skill-wb-exec/
├── package.json               # plugin manifest: dsh.bundle.patch -> dsh/cordis.patch.yml
├── dsh/
│   ├── index.js               # server entry: publishes skills/wb-exec/ on ctx.skills (plain ESM, no build)
│   └── cordis.patch.yml       # inserts one row into the profile plugin tree (by package name, no relative path)
├── skills/
│   └── wb-exec/
│       ├── SKILL.md           # skill instructions: self-check, package, dispatch, supervise, collect, boundaries
│       ├── assets/
│       │   └── task.example.json     # task package template
│       ├── references/
│       │   ├── protocol.md           # fields, result shape, state machine, isolation, exit codes
│       │   └── rejected-designs.md   # 11 features considered and dropped, with reasons
│       ├── scripts/
│       │   └── wb_exec.py            # every subcommand (standard library only)
│       └── tests/
│           ├── fake_cli.py                   # stubbed CLI, replays a stream-json transcript
│           ├── test_wb_exec.py               # unit tests
│           ├── test_executor_backends.py     # backend registry, argv pinning
│           ├── test_end_to_end.py            # dispatch -> status -> collect, against a real repo
│           ├── test_repair_regressions.py    # Windows-installation regressions: BOM, spawn, isolation
│           └── test_lock_and_needs_human.py  # lock claims, liveness, jobs listing, needs_human
├── docs/
│   ├── figures/
│   │   ├── pixel_gate.py      # geometric validation: blank-band and ink of the committed PNGs
│   │   └── fig*.{svg,pdf,png} # SVG is the source; PDF to print, PNG (1248 px) to preview
│   ├── design/                # the reasoning behind the mechanism, not a sales page
│   │   ├── protocol.md        #   communication layer: what the contract can and cannot see
│   │   ├── coordination.md    #   concurrency, path granularity, the stated refusals
│   │   ├── stability.md       #   crash, hang, cancel, orphans
│   │   └── rfc.md             #   the three analyses synthesised and ranked
│   └── launch-drafts.md       # ready-to-post copy per channel, with its own fact-check
├── assets/
│   └── icon.png               # plugin-market storefront icon
├── screenshots.json           # market screenshot manifest
├── LICENSE                    # MIT
└── README.md
```

## Design notes

The mechanism is documented layer by layer, including what it refuses to do:
`docs/design/protocol.md` (host to executor and back), `coordination.md`
(concurrency and the stated refusals), `stability.md` (crash, hang, cancel,
orphans) and `rfc.md`, which ranks the findings and records what was rejected
and why. Several of those notes describe defects that were measured rather than
guessed at, including two hypotheses that the measurement disproved.

## Figures

The diagrams are code-native vector files: editable SVG, PDF for print, PNG for
preview. The SVG is the source of truth: each figure is edited directly and
re-rendered to PDF and to a 1248 px PNG with the architecture renderer.

```bash
python docs/figures/pixel_gate.py      # blank-band and ink check, no rendering
```

`pixel_gate.py` exists because a diagram can be wrong in ways only an eye would
normally catch. The defect that motivated it was a two-panel figure whose panels
were meant to be stacked but never moved: the canvas height had doubled, every
label stayed inside the canvas and the renderer reported zero errors, while 59%
of the image was empty and the panels were still side by side. The check
therefore measures the bounding box of the non-white pixels and refuses a figure
whose blank margin exceeds 35% on any side, or whose content covers less than 55%
of either axis.

They describe the implemented mechanism; they are not a benchmark or a proof of
anything.

## License

MIT — see [LICENSE](LICENSE).