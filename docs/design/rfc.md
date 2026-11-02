# RFC: wb-exec v1.3 — three layers, ranked

Synthesis of `protocol.md`, `coordination.md` and `stability.md`.
Nothing here is a new idea; it is the three analyses put in one place, ranked,
with the duplicates merged and the weak proposals dropped.

---

## 1. The three layers today, in one page

**Communication.** Host → executor is one prompt template plus a JSON Schema.
Schema constrains what the executor may *write*; nothing constrains what it may
*read*, and no document says so. Executor → host is `extract_result()`, which
parses one JSON object per line and takes the first carrying structured output.

**Coordination.** Path claims in a single `dispatch.lock`, taken before the
worktree exists, released in a `finally`. Overlap → refuse (exit 4). No queue,
no ordering, no cross-state-root visibility. Recursion is blocked by
`WB_SUBAGENT`. `needs_human` routes decisions to a person.

**Stability.** A 5 s heartbeat, a 0.5 s poll, a wall-clock timeout, cancel via
a flag file. Both cancel and timeout hard-kill the child. Claims are pruned
when the job has `exit.json` or has no heartbeat file at all.

---

## 2. Findings

| id | layer | what breaks | user harm | effort | risk of change |
|---|---|---|---|---|---|
| S-1 | stability | claim pruned when heartbeat file is *absent*, without checking whether the recorded pid is alive | a job can be admitted against files an orphan is still writing | ~30 lines | low |
| K-5 | coordination | an unparseable `dispatch.lock` silently becomes "no claims" | safety off, silently — the worst default for a lock | ~10 lines | low |
| C-3/S-3 | comm + stability | no size cap on `stdout.jsonl` / `stderr.log` | a chatty executor fills the disk; looks like a machine problem | ~10 lines | low |
| K-4/S-5 | coordination + stability | no read command for live claims or past jobs | user must delete `dispatch.lock` by hand — the one thing README forbids | ~60 lines | low |
| C-2 | communication | envelope has no protocol version | silent breakage across upgrades | 1 line | very low |
| K-2 | coordination | claiming a file does not protect its directory from a sibling rename | rare, after-the-fact `violations` surprises | 0 | none — document |
| K-3 | coordination | no way to express ordering | must sequence by hand | 0 | none — document |
| S-6 | stability | laptop suspend makes a healthy job read `stalled` | misleading label on resume | 0 | none — document |
| S-2 | stability | `proc.kill()` truncates child output | partial output cannot be used anyway | 0 | none — document |

**Omitted, deliberately:** nothing. All three analyses are fully represented
above. C-1 (fenced-JSON false positive), C-4 (one-shot communication) and S-4
(resume) were disproved or judged correct-as-is and are discussed in section 6.

---

## 3. Ranking

### MUST — do now, low risk, no behaviour change for a correct caller

| id | change | proof |
|---|---|---|
| S-1 | prune a claim only when `exit.json` exists, or the pid is dead; keep the missing-heartbeat fallback for unprobeable platforms | a claim whose job dir lacks a heartbeat but whose pid is alive must still refuse overlap; test both directions |
| K-5 | if `dispatch.lock` exists but does not parse, refuse with a clear error naming the path | corrupt-lock test: dispatch must exit non-zero and must not rewrite the file |
| C-3/S-3 | cap `stdout.jsonl` and `stderr.log`; on breach kill and report a blocker | a run emitting >N bytes must fail with a named blocker, not silently succeed |
| K-4/S-5 | one read-only `jobs` subcommand: live claims with paths and ages, plus past jobs with status and worktree presence | a test that dispatches two jobs and asserts the listing shows both, with the blocking one named |
| C-2 | add `"protocol": 1` to the envelope | existing envelope assertions still pass; the key is present |

All five are additive or fail-closed. None changes what a correct caller observes.

### SHOULD — worth it, needs your decision

Nothing. The five MUST items are the whole set I am willing to defend. Adding
more would be decorating.

### CONSIDER — do not build yet

- Directory-granularity claims (would serialise most parallelism — worse than
  the gap it closes).
- Test attestation (a second agent grading the same work).

### REJECTED — keep refusing

- Graceful terminate before kill (S-2). On Windows there is no POSIX-faithful
  SIGTERM; the partial result is unusable even if the child flushes cleanly.
- Job resume (S-4). Needs per-backend session semantics and would leak backend
  specifics into the protocol.

---

## 4. What MUST looks like in code

- `S-1`: `_acquire_claim` / `_overlapping_jobs` gain a `_pid_alive(pid)` helper;
  the skip condition becomes
  `exit.json exists or (no heartbeat and pid dead)`.
- `K-5`: `_acquire_claim` distinguishes "missing" from "present but corrupt"
  and raises instead of coercing to `[]`.
- `C-3/S-3`: the 0.5 s loop in `_monitor_body` compares `out_path.stat().st_size`
  against a constant and kills on breach.
- `K-4/S-5`: new `cmd_jobs` reading `dispatch.lock` plus `jobs/*/job.json`;
  read-only, prints JSON, no mutation.
- `C-2`: `_emit_collect` adds `"protocol": 1`.

Each needs one regression test that would fail before the change. The
pid-aliveness one needs both directions — alive must still refuse, dead must
prune — because a one-directional test passes for a broken implementation.

---

## 5. Conflicts between layers

- **C-3/S-3 vs S-2.** Capping the log kills the child; S-2 says a killed child
  leaves truncated output. They agree: the cap should report the cap as the
  reason, so truncated output is never mistaken for a completed run.
- **K-4/S-5 vs rejected automatic cleanup.** The read-only listing is one
  command away from "prune for me". It must stay read-only. If it ever deletes,
  it has become the rejected auto-cleanup.
- **K-5 vs convenience.** Refusing on a corrupt lock makes the tool harder to
  unstick. That is the point: a safety file that silently disables itself is
  worse than one that blocks and tells you where it is.

---

## 6. Against `rejected-designs.md`

None of the MUST items overturn a stated refusal:

| refusal | touched? | why not |
|---|---|---|
| Not a scheduler | no | nothing waits, retries or decides done |
| No auto-merger | no | no new integration path |
| Not a sandbox | no | no claim of confinement is added |
| No model switcher | no | untouched |
| No free-text guessing | no | `protocol` version is a number, not inferred text |
| No cross-project daemon | no | the `jobs` command reads local files only |
| No silent scope widening | no | K-5 makes a refusal stricter |
| No auto worktree cleanup | no | `jobs` is strictly read-only |
| No second opinion on the diff | no | no attestation built |
| No config file | no | constants only |
| No plugin executors | no | untouched |

Four proposals were considered and dropped before reaching the list: fenced-JSON
parsing (disproved by measurement), one-shot communication (correct by design),
graceful kill and resume (both add mechanisms the tool cannot use).

---

## 7. The honest summary

Five changes, all small, all either additive or fail-closed. One of them (S-1)
closes a real hole: a claim can be pruned while an orphan still writes. One
(K-5) changes a silent-unsafe default into a loud refusal. Two are usability
(a read-only listing and a log cap). One is a single integer that prevents
silent breakage across upgrades.

Everything else I looked at either already works, is correctly refused, or is
documentation. **The design is sounder than I expected going in.** The three
analyses disproved more of my hypotheses than they confirmed — C-1 and the
heartbeat-age half of S-2 were both wrong when tested.
