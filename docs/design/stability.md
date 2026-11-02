# Stability layer

What happens when things go wrong: crash, hang, cancel, timeout, orphans.
Values marked `MEASURED` were produced by calling the real functions.

---

## What supervision is today

`_monitor_body` (`wb_exec.py:627-704`):

- a heartbeat thread writes `heartbeat.json` every **5 s**
- the main loop polls `proc.poll()` every **0.5 s**
- it watches for `cancel.request` and for the wall clock exceeding
  `DEFAULT_TIMEOUT` (1800 s)
- both cancel and timeout call `proc.kill()`
- the claim is released in a `finally` wrapper (`_monitor`, L614-626)

Reading paths consult `HEARTBEAT_STALE = 90` to label a job `stalled`.

---

## S-1. (CORRECTED) The orphan race is narrower than stated — but it is real

I was told to check whether a dead supervisor leaves a claim that is pruned
while the orphan still writes, admitting a new job against files under
concurrent edit. I tested it rather than reasoning about it.

First reading of the pruning rule in `_acquire_claim` (L526) and
`_overlapping_jobs` (L603):

```python
if (other_dir / "exit.json").is_file() or not (other_dir / "heartbeat.json").is_file():
    continue          # prune this claim
```

**Note what is absent: heartbeat AGE is not part of the prune test.** A stale
heartbeat does not prune anything. Only a *missing* heartbeat file, or a
present `exit.json`, does.

`MEASURED`:

```
orphan: heartbeat 200 s old (> HEARTBEAT_STALE=90), exit.json absent
Case A (claim present, heartbeat stale)  -> REFUSED   (paths stay locked)
Case B (heartbeat file missing)          -> ADMITTED  ['newjob03']
```

So my original "stale heartbeat admits an orphan" claim was **wrong** and is
withdrawn. The stale case refuses correctly.

**But Case B is a genuine hole.** A supervisor that dies *before* the heartbeat
thread ever writes — a crash inside the few milliseconds between taking the
claim and the first `write_json(heartbeat.json)` at L643 — leaves a job
directory with `job.json` but no heartbeat and no `exit.json`. That claim is
pruned on the next dispatch, while the CLI it spawned (if it was spawned at
all) may still be running.

**How reachable is this?** The window is small but not zero: the claim is taken
at L826, the job dir is made at L828, the monitor starts later. A crash in that
gap is plausible under a container kill or a `Ctrl-C` at exactly the wrong
moment.

**Verdict: real, low-probability, and the pruning rule is the wrong test.**
The right test is "is the recorded pid still alive?" — `pid` is already in
every claim. That converts a timing heuristic into an actual fact, and it is
portable enough: `os.kill(pid, 0)` on POSIX, and on Windows either
`OpenProcess` via a small ctypes shim or a fallback to the heartbeat age.

**Fix:** prune only when `exit.json` exists, or the heartbeat file is absent
**and** the recorded pid is dead. Keep a heartbeat-age fallback for
platforms where pid probing fails.

**Risk:** low, and it makes the lock stricter, never looser.

---

## S-2. `proc.kill()` gives the executor no chance to finish

On POSIX `kill()` is SIGKILL; on Windows it is `TerminateProcess`. The child
cannot flush a partial result, remove temp files, or close a stream cleanly.

**Cost of escalate-then-kill:** on POSIX, SIGTERM then SIGKILL after a grace
period is simple. **On Windows it is not** — `TerminateProcess` is the only
unconditional option, and there is no SIGTERM equivalent that a console
process reliably handles; the usual workaround is a console control event,
which Python's `subprocess` does not expose portably. Implementing it would
mean `GenerateConsoleCtrlEvent` via ctypes plus process-group juggling, for a
benefit that only appears on timeout and cancel.

**Verdict: do not build it.** The partial result is not recoverable even if the
child does flush — the supervisor already reads `stdout.jsonl` after the kill
and cannot distinguish "partial" from "final". A graceful termination would
buy nothing the tool can use.

**Document instead:** on timeout or cancel, everything in `stdout.jsonl` may be
truncated mid-line, and `raw_tail` may end mid-token.

---

## S-3. No size cap on the log files

Same finding as protocol C-3, same fix. Cross-referenced here so the two
documents do not disagree: a size check in the 0.5 s loop, kill on breach.

---

## S-4. Nothing resumes an interrupted job

There is no resume, and no `--continue`. A killed job leaves its worktree
intact (deliberate: rejecting automatic cleanup is a stated design choice) and
`job.json` records the argv.

**Verdict: correct refusal.** Resume needs the executor's own session handle.
WorkBuddy and codex expose session flags, but the contract for "resume a
previous run safely" differs per backend and would leak backend specifics into
the protocol — the same reason the executor stays a plain plugin point.

What is missing is not resume but **visibility**: a user cannot list stale
jobs and decide. That is S-5.

---

## S-5. The state directory grows forever

One directory per job, never pruned. There is no `wb-exec jobs` listing, no
`prune`, no "show me the last twenty jobs".

**Harm:** the state directory becomes an archaeology problem. What you cannot
see, you cannot clean, and `~/.wb-exec` grows silently.

**Minimal, non-destructive fix:** a read-only `jobs` subcommand listing
`job_id`, status, age, owned paths and worktree presence. **No automatic
deletion** — that is exactly the rejected "automatic worktree cleanup".

**This is the same command proposed as coordination K-4.** They should be one
subcommand covering both claims and jobs, because a claim is a property of a
job.

---

## S-6. Is 5 s / 90 s / 0.5 s sane?

- Worst-case detection of a hang: heartbeat is written every 5 s, considered
  stale after 90 s, so `stalled` appears 90-95 s after the process froze.
- Cancellation latency: up to 0.5 s. Fine.
- Timeout latency: up to 0.5 s. Fine.

**Laptop suspend:** the heartbeat thread uses `stop.wait(5)`, which on POSIX
uses the monotonic clock and generally keeps counting through suspend, but the
process is frozen, so no heartbeat is written for the whole suspended interval.
On resume, the age exceeds 90 s immediately and the job reads `stalled` even
though nothing went wrong.

**Harm:** cosmetic and misleading on laptops — a user resuming from sleep sees
a healthy job reported as stalled.

**Verdict: document only.** Distinguishing "frozen by suspend" from "frozen by
bug" requires a suspend detector, which is platform-specific and not worth the
complexity. Say it in `protocol.md` so the label is not trusted blindly.

---

## Ranked

| id | change | risk | verdict |
|---|---|---|---|
| S-1 | prune claims by pid liveness + heartbeat presence, not heartbeat age alone | low | **fix** — the only true safety hole here |
| S-3 | size cap on `stdout.jsonl` / `stderr.log` | low | **fix** (same as C-3) |
| S-5 | read-only `jobs` listing (merged with K-4) | low | **fix** — the only way a user can act |
| — | document truncated output on kill | none | document |
| S-6 | document that suspend can make a healthy job read `stalled` | none | document |
| S-2 | escalate-then-kill | — | **reject** — costs Windows complexity, buys nothing |
| S-4 | resume | — | **reject** — leaks backend specifics into the protocol |

S-2 and S-4 are the two I most wanted to build and talked myself out of. Both
fail the same test: they add a mechanism whose benefit the tool cannot
actually use.
