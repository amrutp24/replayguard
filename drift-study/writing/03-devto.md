---
title: How to deploy safely while Lambda durable executions are in flight
published: false
tags: aws, serverless, lambda, devops
---

# How to deploy safely while Lambda durable executions are in flight

If you run Lambda durable functions, there is a deploy hazard that does not exist
in normal serverless work, and it fails silently.

A durable execution can be suspended for up to a year. When it resumes, it runs
against whatever code is deployed *at that moment*, not the code it started with.
Change the wrong thing in between and the execution will finish with status
`SUCCEEDED` having done something you did not ask for.

I measured twelve variations of this against a real account. Three corrupted the
execution. All three reported success. None of the twelve produced an error.

Here is what to do about it, shortest fix first.

## 1. The one-line fix: stop invoking `$LATEST`

Every corruption I was able to produce required the execution to be running
against `$LATEST`. Not one of them was reachable when the execution was pinned.

```python
# Hazardous: the execution resumes into whatever $LATEST is by then
lambda_client.invoke(
    FunctionName="my-workflow",
    DurableExecutionName=run_id,
    Payload=payload,
)

# Safe: pinned for the life of the execution
lambda_client.invoke(
    FunctionName="my-workflow",
    Qualifier="live",          # an alias, or a numbered version
    DurableExecutionName=run_id,
    Payload=payload,
)
```

**The alias keeps working the way you want it to.** I tested this directly:
started an execution through an alias, then mid-suspend published a new version
and repointed the alias at it. The execution reported `Version='1'` the whole
time and finished on the original code.

Lambda resolves the alias **once, at execution start**, and pins the execution to
that version for its entire life. So your normal deploy flow — publish a version,
move the alias — is already safe for in-flight work. You just have to actually
invoke through the alias.

If you take nothing else from this post, take that.

## 2. Know which code edits are dangerous

You still need this if you deploy to `$LATEST` anywhere, or if you are reasoning
about an execution that is already running.

Checkpoints are matched to operations **by position in the operation sequence** —
not by name, and not by type. So:

| Change | Safe? | What actually happened |
|---|---|---|
| Rename a step | ✅ safe | Completed normally. The name is not the identity. |
| Edit code outside all steps | ✅ safe | Completed normally. Sequence unchanged. |
| **Insert a step** before a suspend point | ❌ | The inserted step never ran, and the workflow waited a second time |
| **Delete a step** | ❌ | The step *after* it never ran |
| **Reorder steps** | ❌ | One side effect ran **twice**, another never ran |
| Change memory | ✅ safe | Completed normally |
| Upgrade/downgrade the durable SDK | ✅ safe | Both directions completed cleanly |

Note the first row. The advice you have probably read is "treat step names as
immutable IDs, never rename them." Renaming was the only code change that turned
out to be completely harmless. The thing that actually matters is the *order and
count* of your `step()` / `wait()` calls.

The practical rule:

> While executions are in flight, do not change the **sequence** of durable
> operations that occur before the point where those executions are suspended.
> Append-only is fine. Inserting, deleting and reordering are not.

## 3. Why it is worse than a crash

The failure has no diagnostic. Here is the reordering case:

- status: `SUCCEEDED`
- execution history: clean, no error events
- error field: empty
- reality: first step's side effect executed twice, second step's never executed

If step one charges a card and step two sends the receipt, that is a double
charge and no receipt, reported as a successful workflow.

You cannot detect this from the control plane. You detect it by counting side
effects, which is why my test workflow writes an atomic counter from inside every
step — a count of 1 means the checkpoint was honoured, 2 means it re-executed, a
missing row means the step never ran at all.

## 4. Add a CI gate for SDK upgrades

The durable SDK ships often. Of the ten upgrade steps in the Python SDK's
history that can be compared, **nine carry at least one change that can reach an
in-flight execution.** The only one that did not was a `.post1` release
containing no code. Two of the nine were *patch* releases. (Two further steps
are excluded because those pre-release wheels contain no importable package --
quietly dropping them would have flattered the ratio.)

The nastiest category is the one with no build error. Going 1.7.0 → 2.0.0:

```python
try:
    result = context.wait_for_callback(submitter, name="approval")
except ExecutionError:      # caught CallbackError in 1.7.0
    compensate()            # unreachable in 2.0.0
```

`CallbackError` was reparented off `ExecutionError`. That still compiles, still
runs, and silently stops catching. Meanwhile every public method on
`DurableContext` is byte-for-byte identical across that major version bump, so
nothing in your editor or your type checker notices.

I wrote a tool for this. It is stdlib-only and needs no AWS account:

```bash
pip install replaydrift
replaydrift diff 1.7.0 2.0.0
```

```
50 findings   12 can affect an in-flight execution   8 are silent

AFFECTS EXECUTIONS ALREADY IN FLIGHT
------------------------------------

  [RD003] HIGH   SILENT exceptions.py::CallbackError
      CallbackError was a subclass of ExecutionError, UnrecoverableError in
      1.7.0 and is not in 2.0.0 (now: DurableOperationError). `except
      ExecutionError:` still compiles and still runs and no longer catches
      CallbackError.
```

Findings are sorted by *when you find out*, not by size. A deleted function is a
big change and a harmless one — the import fails and you fix it. A reparented
exception is a small change that reaches production intact.

In CI:

```bash
replaydrift diff $CURRENT_SDK $TARGET_SDK --fail-on-inflight
```

Exits non-zero if the upgrade contains anything that can reach a suspended
execution.

**One honest caveat.** I also tested that 1.7.0 → 2.0.0 upgrade live, in both
directions, against a genuinely suspended execution — and it completed cleanly
both ways. The static analysis flags *risk*, not *breakage*. Treat a finding as
"go look at this," not as "this will break."

## 5. A deploy checklist

- [ ] Durable functions are invoked through an alias or a numbered version, never
      `$LATEST`
- [ ] Deploys publish a new version and move the alias, rather than mutating
      `$LATEST` in place
- [ ] Code review treats **inserting, deleting or reordering** `step()` /
      `wait()` calls as a durability change, not a refactor
- [ ] Renaming a step is understood to be safe, so nobody wastes review time on it
- [ ] SDK upgrades run `replaydrift diff --fail-on-inflight` in CI
- [ ] You know your longest possible suspension, because that is how long old
      code has to stay compatible

That last one is the one people miss. If your workflow can wait 30 days for a
human approval, then any execution started in the last 30 days can resume into
today's code.

## What I am not claiming

One account, one region, `python3.13`, SDK 1.7.0, one execution per scenario.
These are observations, not a statistical characterisation.

My suspensions were 90 seconds. The case I actually worry about — an execution
suspended for months, crossing runtime deprecations — is exactly what a day of
testing cannot stage.

Python only. The JS, Java and .NET SDKs have their own replay engines and may
behave differently. If you test one, I would like to see the result.

## Run it against your own account

```bash
pip install 'replaydrift[live]'
replaydrift run --region us-east-1
```

Twelve scenarios, about 45 minutes, well under $0.10. Everything it creates is
prefixed `rd-` and torn down afterwards, verified by direct `get_function` rather
than absence from a list.

Code and raw results: [github.com/amrutp24/replaydrift](https://github.com/amrutp24/replaydrift)
