# I broke twelve suspended Lambda executions on purpose. Three reported success.

A Lambda durable function can sit suspended for up to a year. Your deploy
pipeline doesn't know that, and it doesn't wait.

Everyone has the same advice for this: pin your executions to a version or an
alias, and don't rename steps. It's in the AWS docs, it's in the vendor blogs,
it's in every getting-started post since durable functions went GA. What nobody
says is what actually happens when you don't.

So I found out. Twelve scenarios, each one breaking a replay assumption on
purpose against a live execution in a real account. 2026-09-16, us-east-1,
`python3.13` on arm64, `aws-durable-execution-sdk-python` 1.7.0.

Three of the twelve corrupted the execution. All three said `SUCCEEDED`. Not
one of the twelve failed cleanly.

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

I went in assuming the status would tell me something. It didn't. A run that
ends `FAILED` is the good outcome, because the platform noticed and told you.
The one to worry about is `SUCCEEDED` over a workflow that quietly did the wrong
thing, and I got three of those.

The way I told them apart was crude and it worked: every step body does an
atomic increment on a DynamoDB row. Count of 1, the checkpoint was honoured.
Count of 2, the side effect ran again. No row at all, a step the code says
should run never did.

## The worst one

`d05` swapped two steps while the execution was suspended. It finished
`SUCCEEDED`, and:

- `first` ran **twice**
- `second` **never ran**

If `first` is the card charge and `second` is the receipt, that's a double
charge and no receipt, filed as a success. Nothing in the status, the execution
history, or the error field says anything is wrong. I checked all three.

## Why it happens

Checkpoints get matched to operations by their position in the sequence. Not by
name. As far as I can tell, not by type either.

I could see this in the operation IDs, which are stable hashes. Same slot, two
different scenarios:

| Slot | `d03-step-inserted` | `d05-step-reordered` |
|---|---|---|
| 1st operation | `1ced8f5b` — `step_one` | `1ced8f5b` — `step_one` |
| 2nd operation | `c5faca15` — `pause` | `c5faca15` — `pause` |
| 3rd operation | `6f760b9e` — **`pause`** | `6f760b9e` — **`step_one`** |

`6f760b9e` is a WAIT in one run and a STEP in the other. The ID can't come from
the name or the type, because both changed and it didn't. That only leaves
position.

Trace `d05` with that in mind. Before suspending, the original code had
checkpointed two operations: `step_one` (a STEP) and `pause` (a WAIT). The code
it resumed into was:

```python
context.wait(duration=..., name="pause")            # position 0
context.step(record(run_id, "second"), "step_two")  # position 1
context.step(record(run_id, "first"), "step_one")   # position 2
```

- Position 0 is a `wait`. It picked up the checkpoint for a finished STEP and
  didn't wait.
- Position 1 is a `step`. It picked up the checkpoint for a finished WAIT, so
  its body never ran and `second` was never recorded.
- Position 2 found nothing left, so it ran for real. That's the second execution
  of `first`.

`step_two` isn't in the execution history at all.

Everything else in the table follows from the same thing. Rename a step and
nothing happens, because the name was never the identity. Refactor outside the
steps and nothing happens, because the sequence didn't change. Insert or delete
a step and every position after it shifts, and operations start eating each
other's results.

## Which is backwards from the advice

The advice is: treat step names as immutable IDs, never rename them. In this SDK
version, renaming a step was the one code change I made that was completely
harmless.

What actually matters is the sequence of durable operations before the point
where your executions are parked, and I couldn't find anything that puts it
that way. AWS's own
[best-practices page](https://docs.aws.amazon.com/lambda/latest/dg/durable-best-practices.html)
gets closest. It says in-flight executions "can fail to resume or produce
incorrect results". Both outcomes, no indication of which you'll get. In twelve
tries I never got the first one.

## The fix works, and it isn't discipline

Two scenarios tested the recommended mitigation. Both held.

`d00` started an execution on published version 1, then published a version 2
with a renamed step. Published versions are immutable. The execution never saw
it.

`d07` is the one I actually cared about, because it's what people do in
practice: invoke through an alias, publish a new version on deploy, move the
alias. I did that mid-suspend. The execution reported `Version='1'` the whole
way through and finished on the original code.

Lambda resolves the alias once, when the execution starts, and that's the
version it stays on for life.

So the rule is mechanical, not a matter of being careful:

> Invoke durable functions through a published version or an alias. Every
> corruption in this matrix needed `$LATEST`.

## The one that surprised me

I also swapped the bundled SDK from 1.7.0 to 2.0.0 underneath a suspended
execution. Then back the other way. Both directions came through clean, every
step body running exactly once.

I'm calling that out because I expected the opposite. My own static analysis
had flagged this upgrade hard: 50 differences between those two versions, 12 of
them able to reach an in-flight execution, including the checkpoint
serialisation, the replay engine, and the exception hierarchy. Every public
method on `DurableContext` is byte-for-byte identical across the bump.
Everything under it moved.

And a checkpoint written by 1.7.0 was read fine by 2.0.0. The static analysis
finds risk, not breakage, and for this workflow shape the risk didn't turn into
anything.

Which leaves an awkward asymmetry. The dependency upgrade, the change that gets
a changelog read and a review and a staged rollout, did nothing. A routine
reorder of two steps pushed to `$LATEST` double-ran a side effect. Nobody
reviews a code edit as a durability risk. I didn't either, until I watched it.

## What this doesn't show

- One account, one region, one runtime, one execution per scenario. These are
  observations, not statistics. A scenario that survived once isn't safe, it
  survived once.
- The suspend was 90 seconds. A real execution parked for months crosses
  platform changes I can't stage. That's the case the question is really about,
  and it's still unmeasured.
- Python only. The JS, Java and .NET SDKs have their own replay engines and
  might do something else entirely.
- `d10` is weaker than the table makes it look. The handler reads the drifting
  environment variable but never branches on it, so it shows the value drifts
  without triggering the control-flow divergence AWS warns about.
- The type-blindness, a `wait` happily taking a `step`'s checkpoint, is the part
  most likely to be version-specific and the part I'd re-check first.

## Run it yourself

The harness ships inside [replayguard](https://github.com/amrutp24/replayguard) as the `probe` and `drift` commands, MIT.

```bash
pip install 'replayguard[live]'
replayguard probe --region us-east-1
```

It creates one IAM role, one DynamoDB table and twelve short-lived Lambda
functions, all prefixed `rd-`, and tears them all down afterwards. It checks
they're gone with a direct `get_function` rather than trusting a list. Expect
to spend well under $0.10.

There's an offline half too, no AWS account needed:

```bash
replayguard drift 1.7.0 2.0.0 --fail-on-inflight
```

It compares two SDK versions and fails the build if anything in the upgrade can
reach an execution that's already suspended.

The raw output of the run above is in the repo as
`drift-study/live-matrix.json`, and the findings document is generated from it,
not written by hand. Every number here traces back to the run that produced it.

If you get a different result, especially on another runtime, I'd like to hear
about it.
