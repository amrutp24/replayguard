# I broke twelve suspended Lambda executions on purpose. Three reported success.

A Lambda durable function can stay suspended for up to a year. Your deployment
pipeline doesn't pause for it.

The standard advice for that situation is to pin executions to a version or an
alias and never rename a step. It's in the AWS docs, in vendor blog posts, and
in most getting-started guides written since durable functions went GA. What
none of them explain is what actually happens if you don't follow it.

I wanted to know, so I measured it. Twelve scenarios, each one deliberately
violating a replay assumption against a live execution in a real account, on
2026-09-16 in us-east-1, running `python3.13` on arm64 with
`aws-durable-execution-sdk-python` 1.7.0.

Three of the twelve corrupted the execution, and all three reported
`SUCCEEDED`. None of the twelve produced a clean failure.

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

Execution status turned out not to be useful for this. A scenario that ends in
`FAILED` is actually the good outcome, because the platform detected the problem
and reported it. The outcome to worry about is `SUCCEEDED` on a workflow that
quietly did the wrong thing, and three of the scenarios produced that.

To distinguish the two, every step body performs an atomic increment on a
DynamoDB row. A count of 1 means the checkpoint was honoured. A count of 2 means
the side effect ran again. A missing row means a step that the code says should
run never ran at all.

## The worst case

In `d05`, two steps were swapped while the execution was suspended. The
execution finished with status `SUCCEEDED`, and:

- `first` ran **twice**
- `second` **never ran**

If `first` charges a card and `second` sends the receipt, that's a double charge
and no receipt, recorded as a successful workflow. Nothing in the execution
status, the execution history, or the error field indicates that anything went
wrong.

## Why it happens

Checkpoints are matched to operations by their position in the operation
sequence, not by name, and as far as I can tell not by type either.

The operation IDs show this. They're stable hashes, and here is the same slot
across two different scenarios:

| Slot | `d03-step-inserted` | `d05-step-reordered` |
|---|---|---|
| 1st operation | `1ced8f5b` — `step_one` | `1ced8f5b` — `step_one` |
| 2nd operation | `c5faca15` — `pause` | `c5faca15` — `pause` |
| 3rd operation | `6f760b9e` — **`pause`** | `6f760b9e` — **`step_one`** |

`6f760b9e` is a WAIT in one scenario and a STEP in the other. The ID can't be
derived from the name or the type, because both are different while the ID is
the same. Position is the only thing left.

Here is `d05` traced through that model. Before suspending, the original code
had checkpointed two operations: `step_one` (a STEP) and `pause` (a WAIT). The
code it resumed into was:

```python
context.wait(duration=..., name="pause")            # position 0
context.step(record(run_id, "second"), "step_two")  # position 1
context.step(record(run_id, "first"), "step_one")   # position 2
```

- Position 0 is a `wait`. It received the checkpoint of a completed STEP, so it
  didn't wait.
- Position 1 is a `step`. It received the checkpoint of a completed WAIT, so its
  body never ran and `second` was never recorded.
- Position 2 found no checkpoint left, so it executed for real. That is why
  `first` ran a second time.

`step_two` doesn't appear in the execution history at all.

The other results follow from the same model. Renaming a step is harmless
because the name was never the identity. Refactoring outside the steps is
harmless because the sequence doesn't change. Inserting or deleting a step
shifts every position after it, and operations start receiving each other's
results.

## This is the opposite of the advice

The usual guidance is to treat step names as immutable IDs and never rename
them. In this SDK version, renaming a step was the only code change I tested
that was completely harmless.

What matters is the sequence of durable operations before the point where your
executions are suspended, and I couldn't find any guidance that describes it
that way. AWS's own
[best-practices page](https://docs.aws.amazon.com/lambda/latest/dg/durable-best-practices.html)
comes closest. It says in-flight executions "can fail to resume or produce
incorrect results", which names both outcomes without saying which one you get.
In twelve scenarios I never got the first one.

## The mitigation works

Two scenarios tested the recommended mitigation, and both held.

`d00` ran an execution on published version 1, then published a version 2 with
a renamed step. Published Lambda versions are immutable, so the execution was
never affected.

`d07` is the more realistic one, because it's what people actually do: invoke
through an alias, publish a new version on each deploy, and move the alias. I
did that while the execution was suspended. The execution reported `Version='1'`
throughout and finished on the original code.

Lambda resolves the alias once, when the execution starts, and the execution
stays on that version for its entire life.

So the rule is simple and mechanical:

> Invoke durable functions through a published version or an alias. Every
> corruption in this matrix required `$LATEST`.

## The result that surprised me

I also swapped the bundled SDK from 1.7.0 to 2.0.0 underneath a suspended
execution, and then back the other way. Both directions completed cleanly, with
every step body running exactly once.

I'm pointing this out because I expected the opposite. My own static analysis
had flagged this upgrade as risky: 50 differences between the two versions, 12
of which can reach an in-flight execution, including changes to the checkpoint
serialisation, the replay engine, and the exception hierarchy. Every public
method on `DurableContext` is byte-for-byte identical across the bump, but the
machinery underneath it changed substantially.

In practice, a checkpoint written by 1.7.0 was read correctly by 2.0.0. The
static analysis identifies risk, not breakage, and for this workflow shape the
risk didn't materialise.

That leaves an uncomfortable asymmetry. The dependency upgrade, which is the
kind of change that gets a changelog review and a staged rollout, did no damage.
A routine reorder of two steps deployed to `$LATEST` silently ran a side effect
twice. Code edits like that don't usually get reviewed as a durability risk.

## What this doesn't establish

- One account, one region, one runtime, one execution per scenario. These are
  observations, not a statistical characterisation. A scenario that survived
  once isn't proven safe.
- The suspend was 90 seconds. A real execution suspended for months would cross
  platform changes that this harness can't stage. That's the case the question
  is really about, and it's still unmeasured.
- Python only. The JS, Java and .NET SDKs have their own replay engines and may
  behave differently.
- `d10` is weaker than it looks in the table. The handler reads the drifting
  environment variable but never branches on it, so it shows the value drifts
  without triggering the control-flow divergence AWS warns about.
- The type-blindness, where a `wait` accepts a `step`'s checkpoint without
  complaint, is the part most likely to be version-specific and the first thing
  I'd re-check.

## Run it yourself

The harness ships inside [replayguard](https://github.com/amrutp24/replayguard) as the `probe` and `drift` commands, MIT.

```bash
pip install 'replayguard[live]'
replayguard probe --region us-east-1
```

It creates one IAM role, one DynamoDB table and twelve short-lived Lambda
functions, all prefixed `rd-`, and tears them down afterwards. It verifies
they're gone with a direct `get_function` call rather than relying on a list.
Expected spend is well under $0.10.

There's also an offline half that doesn't need an AWS account:

```bash
replayguard drift 1.7.0 2.0.0 --fail-on-inflight
```

It compares two SDK versions and fails the build if anything in the upgrade can
reach an execution that's already suspended.

The raw output of the run above is in the repo as
`drift-study/live-matrix.json`, and the findings document is generated from it
rather than written by hand, so every number here can be traced to the run that
produced it.

If you get a different result, especially on another runtime, I'd like to hear
about it.
