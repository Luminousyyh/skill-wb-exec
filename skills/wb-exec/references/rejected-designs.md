# Designs considered and rejected

A feature list says what a tool does. It does not say what the tool refuses to
do, and that is usually the part a user needs before trusting it. Every entry
below was a real option that was dropped on purpose; each states the option, the
reason, and what happens instead. If you are about to propose one of these, the
reason is here — if the reason no longer holds, say so and change it.

## Not a scheduler

**Rejected:** an internal queue that runs jobs later, retries them on failure, or
runs them on a timer.

`wb-exec` dispatches once and reports. It has no queue, no backoff, no cron.
Retrying is a decision about whether the work is still wanted, and that belongs
to whoever asked. A tool that silently retries can duplicate a change you already
reviewed and rejected. Instead: the caller decides, and `needs_human` says when a
retry is worth considering.

## Not an auto-merger

**Rejected:** applying the patch, or opening and merging a pull request, when the
run succeeds.

The pipeline stops at "here is the patch, here are the violations". The host runs
`git apply --check`, then `git apply`, then the acceptance commands. This is the
single most important boundary in the design: the executor's own summary is not
evidence, and an agent that both writes and grades the change has not verified
anything. Automation here would trade a real review for an imagined one.

## Not a sandbox

**Rejected:** presenting the worktree and the wrappers as a security boundary.

A process spawned as your OS user can still reach anything you can. The worktree
prevents *accidental* collisions and keeps your uncommitted work out of the run;
it does not confine a hostile executor. The honest framing is "isolation, not
confinement", and it is stated as such so nobody builds on a guarantee that does
not exist. For real confinement, run the whole thing in a container or under a
different OS user.

## No model or effort switching

**Rejected:** `--model` and `--effort` flags on the WorkBuddy backend.

The backend is pinned to one model at one effort. Exposing a choice would make
every report ambiguous — a result could no longer be compared with an earlier one
because the two may have run on different models — and would tempt a caller to
"try something else" after a failure instead of reading `stderr.log`. When the
account refuses the model, the run fails loudly. Note the asymmetry: other
backends (`codex`, `claude`, `aider`, `custom`) do pass through to whatever those
CLIs are configured with, because there the choice is the user's, made outside
this tool.

## No free-text guessing

**Rejected:** inferring "the executor is waiting for a person" from the wording of
`summary`.

Text classification would make the answer depend on phrasing, and it fails
silently: a reworded summary flips the routing with no error anywhere. Instead
`needs_human` is derived from structured fields only — `blockers`, failing
`tests`, `violations`, and the job status. If a situation should route to a
person and no field expresses it, the fix is a new structured field, not a
smarter regex.

## No cross-project coordination

**Rejected:** one daemon that schedules work across several repositories.

The lock is a file under the state directory, scoped to one `WB_EXEC_HOME`. Two
projects with separate state roots do not see each other, by design. A global
scheduler would need a service, a port, a failure mode when it dies, and a
protocol version — a large amount of machinery to prevent a collision that the
operator can avoid by not running two jobs over the same files. The lock covers
the common case; the rest is the caller's judgement.

## No silent scope widening

**Rejected:** expanding `owned_paths` automatically when the executor reports it
needed another file.

That request is exactly the signal a person should see. Auto-widening turns the
whitelist into a suggestion and removes the only thing that makes the violations
list meaningful. Instead the run ends with a blocker and `needs_human.required`,
and a human grants the wider scope explicitly by dispatching again.

## No automatic worktree cleanup

**Rejected:** deleting the worktree when a job finishes.

The worktree is where the unapplied change lives. Deleting it on completion would
destroy the evidence before a person has looked at it, and a failed run is
precisely when that evidence matters most. `wb-exec` never deletes it; removing
it is an explicit act. Disk usage is the accepted cost, and the job directory
records the path so it can be found later.

## No second opinion on the diff

**Rejected:** having the tool judge whether the patch is *good*, beyond checking
scope.

"Good" needs the requirements, the surrounding design and the team's taste — none
of which are in the task package. A judgement here would be confident and
unfounded. The tool checks what it can check mechanically (scope, exit status,
declared test results) and refuses to guess the rest. That is also why a `pass`
inside `tests` is labelled as the executor's claim, not as verification.

## No config file

**Rejected:** a `wb-exec.toml` for defaults.

The task package already carries everything a run needs, and it travels with the
work. A config file would create a second place where scope can be defined, and
the two would drift — with the file winning in ways nobody can see from the task
alone. Environment variables cover the few process-wide knobs (`WB_CLI`,
`WB_EXECUTOR`, `WB_EXEC_HOME`) and are visible in the command that used them.

## No plugin-provided executors

**Rejected:** letting third parties register backends at runtime.

Backends are a closed list in the source. A plugin would have to describe how to
invoke an arbitrary CLI *and* how to parse its output, and a wrong parser fails
quietly as "no structured result". The `custom` backend already covers the real
need with a declared argument template, and its failures are visible in the same
report shape as everything else.
