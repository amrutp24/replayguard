# Why these failures happen

Measured 2026-09-16, us-east-1, `aws-durable-execution-sdk-python` 1.7.0,
`python3.13`. Raw evidence in
[`results/live-matrix.json`](results/live-matrix.json).

This is an inference from observed behaviour, not from AWS source or
documentation. It explains every result in the matrix, and it makes predictions
that the matrix confirms — but it is a model, and it should be treated as one.

## The claim

**A durable execution matches checkpoints to operations by position in the
operation sequence. Not by name, and not by type.**

On replay the handler runs from the top. Each durable operation it reaches —
`step()`, `wait()`, whatever — consumes the next checkpoint in the recorded
history. If a checkpoint exists at that position, the operation returns the
recorded result and its body does not execute. If none exists, the operation
runs for real and a new checkpoint is appended.

Nothing verifies that the checkpoint being consumed belongs to the operation
consuming it.

## The evidence

Operation IDs are stable hashes. The same slot produces the same ID across
different executions of different scenarios:

| Slot | `d03-step-inserted` | `d05-step-reordered` |
|---|---|---|
| 1st operation | `1ced8f5b` — `step_one` | `1ced8f5b` — `step_one` |
| 2nd operation | `c5faca15` — `pause` | `c5faca15` — `pause` |
| 3rd operation | `6f760b9e` — **`pause`** | `6f760b9e` — **`step_one`** |

`6f760b9e` is a WAIT in one scenario and a STEP in the other. The ID cannot be
derived from the operation's name or its type, because both differ while the ID
is identical. Position is what is left.

### `d05-step-reordered`, traced

Original code checkpointed two operations before suspending:

```
op1  1ced8f5b  STEP  step_one  -> {"label": "first"}
op2  c5faca15  WAIT  pause     -> completed when the timer fired
```

The redeployed code, reached on resume, is:

```python
context.wait(duration=..., name="pause")            # position 0
context.step(record(run_id, "second"), "step_two")  # position 1
context.step(record(run_id, "first"), "step_one")   # position 2
```

What the history shows happening:

- position 0, a `wait`, consumed **op1 — a completed STEP**. It did not wait.
- position 1, a `step`, consumed **op2 — a completed WAIT**. Its body never ran,
  so `second` was never recorded.
- position 2 found no checkpoint left, so it executed for real: `first` ran a
  **second** time, appended as op3 `6f760b9e`.
- The execution reported `SUCCEEDED`.

`step_two` never appears in the execution history at all.

### The model predicts the rest of the matrix

| Scenario | Prediction | Observed |
|---|---|---|
| `d02` rename a step | Safe — the name is not identity | survived |
| `d03` insert before a completed step | Everything shifts; trailing op finds no checkpoint and creates a second wait | inserted step never ran; two `pause` waits |
| `d04` remove a completed step | Following op consumes the wrong checkpoint | `second` never ran |
| `d05` reorder steps | Duplicate and omission together | `first`=2, `second` absent |
| `d06` refactor outside all steps | Safe — the operation sequence is unchanged | survived |

## What this inverts

The advice repeated across community guides is to treat step names as immutable
IDs and never rename them. In this SDK version, renaming a step was the only code
change measured here that was **harmless**, because the name is not what
identifies the operation.

The thing that actually matters — the sequence of durable operations reached
before the first uncompleted one — is not what the guidance names. AWS's own
[best-practices page](https://docs.aws.amazon.com/lambda/latest/dg/durable-best-practices.html)
is closer: it says in-flight executions can fail to resume *or produce incorrect
results*. What is measured here is the second, in every case, with no diagnostic
of any kind.

## What would falsify this

- An operation ID that changes when only the name changes, position held
  constant.
- A step consuming a wait's checkpoint and raising, rather than silently
  accepting it.
- Different behaviour in the JS, Java or .NET SDKs, which have their own replay
  engines. **Untested here** — this is one SDK, one version, one runtime.

The type-blindness is the part most likely to be version-specific, and the part
most worth re-checking against 2.0.0.

## Practical consequence

The safe-deploy rule that falls out of this is narrower and more mechanical than
the published advice:

> While executions are in flight, do not change the *sequence* of durable
> operations reached before the point where those executions are suspended.
> Renaming is safe. Reordering, inserting and removing are not, and they fail
> silently.

The reliable mitigation is not code discipline at all. It is
[`d00-version-pinned`](README.md): a published Lambda version is immutable, so an
execution pinned to one cannot be reached by any of this.
