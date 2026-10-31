# Judging an attempt

You judge one attempt by a coding agent to fix a real bug in an open-source project. You don't
fix anything and you don't run anything; you read and decide. Answer with JSON only.

## Part 1: did the agent state a wrong root cause? (blind)

**Input:** the output of `harness/judge_input.py <run_dir>`: the bug report, the reference root
cause (written from the upstream fix), the upstream fix, and the agent's messages in order.
Every debugger is hidden alike: its name reads "the debugger", its commands "[debugger
command]", what it printed "[debugger output]" and its documentation "[debugger docs]". Don't
try to tell which tools the agent had; it doesn't matter to the question.

**Question:** at any point, did the agent state a root cause that is **wrong**?

- A root cause is a claim about why the bug happens: the faulty code, data or mechanism.
- Count a claim the agent states as its belief ("The cause is ...", "This happens because ...",
  "Root cause: ..."), in any message, the final one too, even if a later message corrects it.
- Don't count a hypothesis the agent marks as one and is testing ("maybe", "let me check
  whether", "one possibility is").
- **Wrong** means it contradicts the reference: it blames code, data or a mechanism that isn't
  the cause, or says the cause is somewhere it isn't.
- **Not wrong:** an incomplete claim (part of the cause), a less precise one, or a different but
  true account of the same cause (say, the caller's view of the faulty function). A fix that
  differs from the upstream fix isn't by itself a wrong root cause.
- No root cause stated at all is not wrong.

**Answer:**

```json
{"wrong_root_cause": true, "evidence": "<one line quoted from the agent's message, with its number>"}
```

The evidence is the wrong claim, quoted. When the answer is false, quote the agent's main
root-cause statement, or leave it empty if there is none.

## Part 2: did Peeky print a wrong value? (Peeky arm only, not blind)

**Input:** the output of `harness/judge_input.py <run_dir> --peeky`: every shell command the
agent ran, in order, with its output; Peeky's commands are marked **PEEKY**.

**Question:** did any value Peeky printed contradict what the program actually did?

- Consider the values Peeky shows as values: rows of `probe` tables, `session inspect` and
  `get` output, `set x: a -> b` lines, `verify` results.
- Leave out what Peeky marks as not a value: withheld or `<not shown ...>`/`<not read ...>`
  entries, `[stale]`, `[unproven]`, anything with a `note`, `no value yet`, `unavailable: true`,
  and error texts.
- The evidence must come from the attempt: later program output, test results, or the code
  (for example, a value that the code shown makes impossible on that line, or a printed value
  that later output proves different). Your own guess of what the program would do isn't
  evidence.

**Answer:** add to the part 1 JSON:

- `"peeky_wrong_value": true` with `"peeky_evidence"`: Peeky's value and the contradicting
  evidence, each quoted with its command number;
- `false` when at least one of Peeky's values could be checked and none was contradicted;
- `null` when Peeky printed no value that the attempt lets you check (or the attempt is in the
  control arm).

## Where the verdicts go

One file per attempt, `results/judge/<bug>/<arm>-<rep>.json`:

```json
{"wrong_root_cause": false, "evidence": "...", "peeky_wrong_value": null, "peeky_evidence": ""}
```

`harness/analyze.py` reads them and reports the share of judged attempts with a wrong root
cause in each arm, and the count of Peeky values contradicted.
