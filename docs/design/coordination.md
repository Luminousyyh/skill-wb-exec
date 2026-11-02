# Coordination layer

How concurrent jobs avoid each other, and where that model runs out.
Every claim was checked against `wb_exec.py`; values marked `MEASURED` were
produced by calling the real functions.

---

## What coordination is today

Four mechanisms, all deliberately small:

1. **Path claims.** `dispatch`/`run` take `owned_paths` in `dispatch.lock`
   *before* creating a worktree (`wb_exec.py:824-826`), release it in a
   `finally` (`_monitor`), and refuse an overlapping job with exit 4.
2. **Disjoint paths run in parallel.** No queue, no fairness.
3. **Recursion guard.** `WB_SUBAGENT=1` in the child; the script refuses to
   dispatch when it sees it (exit 3).
4. **`needs_human`.** A structured boolean routes decisions to a person.

`rejected-designs.md` refuses a scheduler, an auto-merger, cross-project
coordination and silent scope widening. Everything below stays inside those
refusals or argues explicitly before touching one.

---

## K-1. The lock is advisory and single-state-root

`_lock_path()` is `STATE_ROOT / "dispatch.lock"`, and `STATE_ROOT` is
`WB_EXEC_HOME` or `~/.wb-exec`.

Two machine users, or one user with two `WB_EXEC_HOME` values, share no
coordination at all. Two jobs can edit the same file in the same repository
with no interference detection.

**Verdict: correct, and already documented** in README ("advisory between
cooperating jobs and scoped to a single `WB_EXEC_HOME`… not a security
boundary"). The alternative — a lock visible across users — is a daemon, which
is the cross-project coordination already refused.

**No change. This is the honest boundary of the tool.**

---

## K-2. Path granularity is the right collision key, with one real gap

`_overlaps` (L464-482) compares normalised path *segments*:

```python
x == y or x.startswith(y + "/") or y.startswith(x + "/")
```

`MEASURED` consequences:

| job A owns | job B owns | collide? | correct? |
|---|---|---|---|
| `src/` | `src/a.py` | yes | yes |
| `src/auth` | `src/authz/x.py` | no | yes — prefix is not a boundary |
| `src/a.py` | `src/b.py` | no | yes — different files |
| `src/a.py` | `src/a.py/` → `src/a.py` | yes | yes |

The gap: **two jobs owning different files in the same directory are allowed
to run concurrently.** That is intentional and normally right — it is how
parallel work is achieved at all. But a rename, a file deletion, a
`__init__.py` edit, or a formatter run over a directory touches files that
were never claimed, and no mechanism notices because each job's claim is
satisfied.

**Harm:** the collision the lock promised to prevent happens anyway, at a
granularity nobody claimed. It shows up as a surprising `violations` entry or
as a broken import, well after the fact.

**Is it worth fixing?** No. Fixing it means claiming at directory granularity,
which serialises almost all useful parallelism — worse than the disease. The
correct response is the one the tool already takes for out-of-scope writes:
**report, do not prevent**. What is missing is only the sentence in
`protocol.md` saying that claiming `src/a.py` does not protect its directory
from a sibling job that renames it.

**Verdict: document the gap. Do not narrow the key.**

---

## K-3. No ordering can be expressed between jobs

"Job B consumes job A's patch" must be sequenced by the caller: dispatch A,
`collect`, apply the patch, then dispatch B.

**Verdict: correct refusal, and it is the *same* refusal as the scheduler.**
Ordering is scheduling. A dependency graph would need a runner that waits,
retries and decides what "done" means — exactly `rejected-designs.md`'s "Not a
scheduler": *retrying is a judgement about whether the work is still wanted*.

A caller that wants ordering already has it for free: call `run` sequentially,
or `collect --wait` before dispatching the next one. Nothing is missing except
documentation. **README should say "sequence by hand", as a feature, not an
omission.**

---

## K-4. Refusal instead of queueing is right, but has no stuck-work story

Overlap → exit 4 immediately. The rationale in README ("排隊意味著我要猜你
什麼時候有空") is sound: you cannot plan around an unknown wait.

The gap is downstream. A caller that gets `PATHS_LOCKED` has three options:
widen the split, wait, or cancel the other job. **There is no command to see
which of those is available.** `status --job` works only if you know the
`job_id`, and the refusal does name holders — so the information exists, but
there is no `wb-exec jobs` listing live claims with their paths and ages.

**Smallest fix:** a read-only `claims` subcommand that prints the current
`dispatch.lock` with each holder's paths, age and whether its job is still
heartbeating. Ten lines of code, no new mechanism, no scheduling.

**This is the one genuine coordination gap worth code**, because without it a
user's only recourse is to delete `dispatch.lock` by hand — which README
explicitly warns against, for good reason.

---

## K-5. What happens when the lock itself is corrupt

`_acquire_claim` reads the file with `try/except` and falls back to `[]`
(L514-519):

```python
try:    claims = json.loads(path.read_text(...))
except: claims = []
```

A truncated or hand-edited `dispatch.lock` therefore **silently admits every
job**, and then rewrites the file with only the new claim. Existing claims are
discarded without a word.

**Harm:** the failure mode of a corrupted safety file is "safety off, silently".
That is the worst possible default for a lock.

**Smallest fix:** if the file exists but does not parse, refuse with a clear
error naming the path, and say how to recover. Refusing on an unreadable lock
is the opposite of convenient and exactly right.

---

## Ranked

| id | change | risk | verdict |
|---|---|---|---|
| K-5 | refuse when `dispatch.lock` exists but is unparseable | low | **fix** — fail-closed beats fail-silent |
| K-4 | add a read-only `claims` listing | low | **fix** — removes the hand-delete temptation |
| K-2 | document directory-level collisions | none | document |
| K-3 | document "sequence by hand" as a feature | none | document |
| K-1 | — | — | no change; already the stated boundary |

Nothing here overturns a stated refusal. K-4 is a read command, not a queue;
K-5 makes an existing refusal stricter, not looser.
