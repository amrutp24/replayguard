# I broke twelve suspended Lambda executions on purpose. Three reported success.

A Lambda durable function can stay suspended for up to a year. Your deployment
pipeline does not pause for it.

That gap has one piece of standing advice attached to it, and you have read it
before: pin your executions to a version or an alias, and never rename a step.
It appears in the AWS docs, in vendor blogs, and in every getting-started post
written since durable functions went GA. What none of them say is what actually
happens if you get it wrong.

So I measured it. Twelve scenarios, each one deliberately violating a replay
assumption against a live execution in a real account, on 2026-09-16 in
us-east-1, `python3.13` on arm64, `aws-durable-execution-sdk-python` 1.7.0.

**Three of the twelve corrupted the execution. All three reported `SUCCEEDED`.
None of the twelve produced a clean failure.**

## The result

| Scenario | What changed mid-suspend | Outcome |
|---|---|---|
| `d05-step-reordered` | two steps swapped order | **one side effect ran twice, another never ran** |
| `d03-step-inserted` | a step inserted before a completed one | **inserted step never ran; a second wait was created** |
| `d04-step-removed` | a completed step deleted | **the following step never ran** |
| `d02-step-renamed` | a completed step renamed | survived |
| `d06-nonstep-refactor` | pure refactor outside all steps | survived |
| `d08` / `d09` | bundled SDK 1.7.0 ⇄ 2.0.0 | survived, both directions |
| `d10-env-changed` | env var read at module scope | survived (see caveats) |
| `d11-memory-changed` | memory 512MB → 1024MB | survived |
| `d00-version-pinned` | redeploy against a pinned version | **insulated** |
| `d07-alias-repointed` | alias moved to a new version | **insulated** |

Status is not the finding. A scenario that ends `FAILED` is good news — the
platform refused and told you. The outcome worth worrying about is `SUCCEEDED`
over a workflow that silently did the wrong thing, and that is what three of
these produced.

To tell those apart, every step body performs an atomic DynamoDB increment. A
count of 1 means the checkpoint was honoured. A count of 2 means the side effect
re-executed. A missing row means a step the code says should run never ran at
all.

## The worst one

`d05` swapped two steps while the execution was suspended. The execution
finished with status `SUCCEEDED`, and:

- `first` executed **twice**
- `second` **never executed**

If `first` charges a card and `second` sends the receipt, that is a double charge
and no receipt, reported as a successful workflow. Nothing in the execution
status, the execution history, or the error field indicates a problem.

## Why it happens

Durable executions match checkpoints to operations **by position in the operation
sequence**. Not by name. Not even by type.

The evidence is in the operation IDs, which are stable hashes. Here is the same
slot across two different scenarios:

| Slot | `d03-step-inserted` | `d05-step-reordered` |
|---|---|---|
| 1st operation | `1ced8f5b` — `step_one` | `1ced8f5b` — `step_one` |
| 2nd operation | `c5faca15` — `pause` | `c5faca15` — `pause` |
| 3rd operation | `6f760b9e` — **`pause`** | `6f760b9e` — **`step_one`** |

`6f760b9e` is a WAIT in one scenario and a STEP in the other. The ID cannot be
derived from the name or the type, because both differ while the ID is identical.
Position is what is left.

Trace `d05` through that. Before suspending, the original code checkpointed two
operations: `step_one` (a STEP) and `pause` (a WAIT). The redeployed code reached
on resume is:

```python
context.wait(duration=..., name="pause")            # position 0
context.step(record(run_id, "second"), "step_two")  # position 1
context.step(record(run_id, "first"), "step_one")   # position 2
```

- Position 0 is a `wait`. It consumed the checkpoint of a completed **STEP**. It
  did not wait.
- Position 1 is a `step`. It consumed the checkpoint of a completed **WAIT**. Its
  body never ran, so `second` was never recorded.
- Position 2 found no checkpoint left, so it executed for real — and `first` ran
  a second time.

`step_two` never appears in the execution history at all.

Every other result falls out of the same model. Renaming a step is harmless
because the name was never the identity. A refactor outside every step is
harmless because the operation sequence is unchanged. Inserting or deleting shifts
every subsequent position, so operations start consuming each other's results.

## This inverts the advice

The guidance you have read says to treat step names as immutable IDs and never
rename them. In this SDK version, renaming a step was the **only** code change
measured here that was completely harmless.

The thing that actually matters is the *sequence* of durable operations reached
before the point where your executions are suspended — and no guidance I found
names it that way. AWS's own
[best-practices page](https://docs.aws.amazon.com/lambda/latest/dg/durable-best-practices.html)
gets closest: it says in-flight executions can fail to resume *or produce
incorrect results*. Both outcomes are named. Which one you get is not. In twelve
scenarios I never once got the first.

## The mitigation works, and it is not code discipline

Two scenarios tested the recommended fix, and both held.

`d00` ran an execution on a published version 1 and then published a version 2
with a renamed step. Published Lambda versions are immutable, so the execution
could not be reached.

`d07` is the interesting one, because it is the mitigation people actually
follow. The execution started through an alias. Mid-suspend, a new version was
published and the alias was repointed at it. The execution reported `Version='1'`
throughout and finished on the original code.

**Lambda resolves an alias once, at execution start, and pins the execution to
that version for its entire life.**

That makes the practical rule mechanical rather than a matter of care:

> Invoke durable functions through a published version or an alias. Every
> corruption in this matrix required `$LATEST`.

## The negative result, which surprised me

I also swapped the bundled SDK from 1.7.0 to 2.0.0 underneath a suspended
execution, and back again. Both directions completed cleanly, each step body
running exactly once.

That is worth stating plainly because my own static analysis predicted trouble.
Comparing those two versions offline flags 50 differences, 12 of which can reach
an in-flight execution — including changes to the checkpoint serialisation, the
replay engine, and the exception hierarchy. Every public method on
`DurableContext` is byte-for-byte identical across that major bump; the machinery
underneath it is not.

Live, a checkpoint written by 1.7.0 was read correctly by 2.0.0. The static
analysis flags **risk, not breakage**, and for this workflow shape the risk did
not materialise.

Which leaves an uncomfortable asymmetry. The dependency upgrade — the thing that
gets a changelog read, a review, and a staged rollout — did no damage. A routine
step reorder pushed to `$LATEST` silently duplicated a side effect. Nobody
reviews a code edit as a durability risk.

## What this does not establish

- One account, one region, one runtime, one execution per scenario. These are
  observations, not a statistical characterisation. A scenario that survived once
  is not thereby safe.
- The suspend is 90 seconds. A real execution suspended for months crosses
  platform changes this harness cannot stage — which is the case the whole
  question is about, and the one still unmeasured.
- Python only. The JS, Java and .NET SDKs have their own replay engines.
- `d10` is weaker than it looks: the handler records the drifting environment
  variable but never branches on it, so it confirms the value drifts without
  exercising the control-flow divergence AWS documents.
- The type-blindness — a `wait` accepting a `step`'s checkpoint without
  complaint — is the part most likely to be version-specific, and the part most
  worth re-checking.

## Run it yourself

The harness ships inside [replayguard](https://github.com/amrutp24/replayguard) as the `probe` and `drift` commands, MIT.

```bash
pip install 'replayguard[live]'
replayguard probe --region us-east-1
```

It creates one IAM role, one DynamoDB table and twelve short-lived Lambda
functions, all prefixed `rd-`, and tears them down afterwards, verifying by
direct `get_function` rather than absence from a list. Expected spend is well
under $0.10.

There is also an offline half that needs no AWS account:

```bash
replayguard drift 1.7.0 2.0.0 --fail-on-inflight
```

which compares two SDK versions and fails a build if anything in the upgrade can
reach an execution that is already suspended.

The raw output of the run above is committed in the repo as
`drift-study/live-matrix.json`, and the findings document is generated from it rather
than written by hand — so every number here can be traced to the run that
produced it.

If you get a different result, particularly on another runtime, I would like to
see it.
