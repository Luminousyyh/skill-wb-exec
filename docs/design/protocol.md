# Communication layer

How the host talks to the executor, and what the contract can and cannot see.
Every claim below was checked against the code; the two measurements marked
`MEASURED` were produced by calling the real functions, not by reading them.

---

## What the contract is today

Two directions, two very different degrees of rigour.

**Host → executor.** One Chinese prompt template, `build_prompt()`
(`wb_exec.py:318-351`). Fixed sections: goal, context, inputs, working
directory, allowed paths, acceptance, hard constraints, notes. The hard
constraints are five numbered lines of prose. A JSON Schema is passed through
`--json-schema` to constrain the *shape* of the answer.

**Executor → host.** `extract_result()`. It scans for JSON objects and takes
the first one that carries a `structured_output` / `structuredOutput` dict, or
fails that, the first `type: "result"` object whose `result`/`message`/`content`
holds a dict or a JSON string.

The asymmetry is the finding. **What the executor may WRITE is machine-checked
(JSON Schema); what it may READ is prose only.** Nothing constrains which paths
the executor opens, and nothing stops it from reading the rest of the user's
disk. That is consistent with the project's stated boundary — isolation, not
confinement — but it is nowhere stated in the protocol, so a reader of
`protocol.md` can easily come away believing the contract is symmetric.

---

## C-1. `extract_result` does not mistake a fenced JSON sample for a result

This was my hypothesis; the measurement refutes it. Reported here because it
was put to me as a finding and it would be dishonest to drop it silently.

`MEASURED`:

```
transcript: a single-line fenced json block {"type":"result", ...} followed by prose
            "That is the schema. I did not run anything."
extract_result(...) -> {"ok": false, "blockers": ["无法解析结构化结论"],
                        "raw_tail": <the whole transcript>}

genuine one-line stream-json result
                 -> {"ok": true, "summary": "added a helper", "changed_files": [...]}
```

Why the sample is rejected: the fence line ````json` and the closing ```` ``
are not JSON, so the object inside the fence never begins a line with `{`.
Only a line that *starts* with `{` or `[` is parsed. The genuine result works
because stream-json is one object per line.

**Verdict: not a defect. Do not "fix" it.**

The residual risk is narrow and worth stating: an executor that emits a bare
JSON object on its own line, as prose, before the real result, would be
mistaken for the result. That requires the executor to print a well-formed
result-shaped object outside the schema mechanism — which is roughly the
behaviour the schema exists to prevent. Not worth code. **Document the
one-object-per-line assumption** in `protocol.md`, which currently does not
state it.

---

## C-2. The result envelope carries no version

`result.json` has no `schema_version` and no `protocol_version`. A host written
against v1.2 reads a v1.3 result with no way to tell whether the shape changed.

**Harm:** silent breakage across an upgrade. A host that reads
`result["violations"]` keeps working until a release renames or nests a field,
then fails deep inside its own logic with no clue.

**Smallest fix:** add `"protocol": 1` to the envelope every `collect` emits.
Additive; old hosts ignore an unknown key; new hosts can branch.

**Cost to existing hosts:** none. It is one extra key.

---

## C-3. Nothing caps `stdout.jsonl` / `stderr.log`

`_monitor_body` opens both files `"wb"` and lets the executor write without
limit (`wb_exec.py:647-655`). "Do not print long logs" is instruction 5 of the
prompt — prose, addressed to a model, on a disk shared with the host machine.

**Harm:** a chatty or looping executor fills the user's disk. The failure is
slow, invisible, and looks like "the machine got full", not "wb-exec did this".

**Smallest fix:** check the file size in the 0.5 s supervision loop; on
crossing a limit, kill and set `status="failed"` with a blocker naming the
limit. The loop already polls there.

**Cost:** one `stat()` per half-second. Negligible.

---

## C-4. Communication is strictly one-shot

The executor cannot ask a clarifying question and resume. Its only channel
back is `blockers`, filed at the end.

**This is deliberate and correct.** A round-trip needs a protocol where the
child waits for input; that reopens exactly the "who decides what" question
the two-role split exists to answer. `rejected-designs.md` refuses a scheduler
for the neighbouring reason.

**Verdict: keep. Document it as a limitation** so nobody designs around a
capability that will not arrive.

---

## C-5. Nothing verifies that `tests[]` were actually run

`result["tests"]` is the executor's own account. `protocol.md` already warns
that a `pass` is not independent verification, and README repeats it. That is
documentation doing its job.

A machine check is available and cheap: `-x`/`--trace` on pytest,
`--reporter` on jest, or simply comparing reported commands against the
`acceptance` list. But the honest fix is the one already chosen — **the host
re-runs acceptance in the main working tree before integrating**. Making
`wb-exec` attest to tests would be a second agent grading the same work.

**Verdict: keep as documented. Do not build attestation.**

---

## What I would change, ranked

| id | change | risk | why |
|---|---|---|---|
| C-2 | `"protocol": 1` in the envelope | very low | cheap insurance against silent drift |
| C-3 | size cap on the log files | low | real disk-filling failure, fixed in the loop we already run |
| — | document the one-object-per-line parsing assumption | none | it is real behaviour nobody wrote down |
| — | document that reads are not constrained | none | prevents a wrong inference from an asymmetric contract |

Explicitly **not** changing: C-1 (hypothesis disproved), C-4 (deliberate
design), C-5 (documentation is the correct fix).
