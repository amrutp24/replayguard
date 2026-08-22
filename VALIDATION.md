# Validation record

What has actually been tested, against whose code, and what is still unproven.

This exists because the evidence was living in commit messages, and nobody reads
`git log` to decide whether a tool is trustworthy. Every number here comes from a
run that was performed, not from an estimate.

**Last run: 2026-08-19.** Re-run before publishing; these numbers date.

---

## Corpora

| Corpus | Repos | Files | Durable handlers | Authors |
|---|---:|---:|---:|---|
| AWS samples + all three official SDKs | 5 | 755 | 415 | AWS |
| Community | 11 | 685 | 422 | ~10 independent |
| **Total** | **16** | **1,440** | **837** | |

Community repos: `singledigit/durable-serverlesspresso`,
`singledigit/durable-function-video-scanner`,
`lukehedger/lambda-durable-functions-examples`,
`awedis/aws-lambda-durable-functions-callback`,
`misskecupbung/aws-lambda-durable-functions`, `TrickSumo/DurableLambdaCourse`,
`zhongkechen/async-durable-execution`,
`dobeerman/aws-lambda-durable-slack-approval-demo`,
`hsaenzG/durable-funtions-demo`, `brianleroux/arc-plugin-workflow`,
`gunnargrosch/durable-viz`.

The community corpus matters more than the AWS one. AWS's samples are written in
a single house style; ten independent authors hit shapes the fixtures never
covered, and that is where every real bug in this tool was found.

---

## Current state

| Rule | AWS | Community | Confirmed real | Known false positives |
|---|---:|---:|---:|---|
| RG001 clock/random/identity | 11 | 2 | yes | none known |
| RG002 external I/O | 0 | 0 | **never fired** | n/a |
| RG003 outer write in step | 0 | 5 | yes | **none known** |
| RG004 nondeterministic branch | 0 | 0 | **never fired** | none known |
| RG005 unstable operation name | 0 | 0 | **never fired** | none known |
| RG900 coverage gap (note) | 32 | 19 | n/a | n/a |

**RG003 now has a read-back reachability check** (added 2026-08-19): a write in a
step body is only reported when the value is read somewhere the write may not
have run -- the durable region, or a *different* step body. All four known false
positives are gone and no true positive was lost.

### Confidence, honestly

**Proven.** RG001, and the region-detection machinery underneath every rule.
RG001 produced 13 findings across 1,440 files and drove every bug fix. One
finding is confirmed by the code's own author, who named the file
`handler-with-violation.ts`, shipped a matching `handler-fixed.ts`, and wrote a
test asserting the bug manifests.

**Proven, with caveats.** RG003. All five of its current findings are confirmed
true positives -- the saga, and two `resizeAttempts` counters -- with no known
false positives remaining after the read-back check.

**Unproven.** RG002, RG004, RG005 have **never produced a true positive on code
written by someone else.** They pass unit tests and fire correctly on constructed
input. That is not the same thing. RG002 has never fired at all across 1,440
files, so it is entirely untested in the wild.

---

## Confirmed true positives

**`lambda-durable-functions-examples/examples/saga/function.ts`** — the strongest
finding, independently verified. `completedSteps` accumulates rollback entries
*inside* step bodies. On resume the handler re-initialises it to `[]`, the
checkpointed step bodies do not re-run, and the compensation loop iterates zero
times — returning 500 as though rollback succeeded. Inventory stays reserved.

Two refinements from verification worth keeping straight: on this example's
`simulateFailure` path payment is never taken (it throws before its push), and
the rollbacks here are `logger.info` no-ops. The harm is that this is published
as *the* reference saga, and the bug is **latent** — three fast steps usually
complete in one invocation, so it manifests only on resume.

A sharper second manifestation: interruption *during* compensation leaves partial
rollback silently reported as complete.

**`hsaenzG/durable-funtions-demo/src/handler-transient-failure.ts:70`** — module-level
`resizeAttempts` incremented inside a step body (line 70) and read back outside
it into the return value (lines 112, 129). Non-test handler code. Found only
after the `++` fix below.

**`hsaenzG/durable-funtions-demo/src/handler-with-violation.ts:22`** — a
deliberate, documented violation with a matching fixed variant. Author-confirmed.

---

## Known false positives

**None currently known.** The four write-only observability instruments in
`article3-scenarios.test.ts` were cleared by the read-back check.

One known mislabelling remains: closure variables are sometimes reported as
"module-level state". The finding is correct; the wording is not.

### The read-back check, and the trap in it

The condition is *not* "is it read in the durable region?" -- that would have
suppressed the saga, whose compensation list is read inside a
`runInChildContext` body. Neither that body nor the step bodies re-run on
replay, so a value written in one and read in another is still stale. The
condition is **read anywhere other than the step body that wrote it**.

A second trap: excluding every mutation receiver from reads (on the grounds that
`log.push(x)` writes rather than consumes) also suppressed the saga, because its
loop consumes the array `reverse()` returns. The exclusion applies only when the
result is **discarded** -- a bare expression statement.

Both traps were caught by re-running the corpora, not by the unit tests. Both
are now pinned by regression tests.

---

## Bugs this validation found — in the tool, not the code

Nine so far. None were catchable by the fixtures, because the same person wrote
the fixtures and the frontends.

### From the AWS corpus

1. **TS client taint reached response data** — `output.content.find(...)`, an
   ordinary `Array.prototype.find`, reported as external I/O.
2. **Python step bodies passed by keyword were missed** —
   `wait_for_callback(submitter=fn)` is AWS's own form.
3. **The durable surface is far wider than `step`** — `stepAsync`, `withRetry`,
   `map`, `wait`, `createCallback`, `runInChildContext` and async variants: 13
   missing in Python and TypeScript, 8 in Java.
4. **`await` + a generic type argument broke detection** — tree-sitter parses
   `await context.waitForCallback<T>(...)` with the *await_expression* as the
   call's `function` field.
5. **Child contexts were not contexts** — `runInChildContext(async (childContext)
   => ...)` names its context freely.
6. **Unnamed operations had their body walked twice**, once in the wrong region.
7. **`@durable_step` bodies are deferred** — the decorator makes the call return
   a step descriptor, so the body runs inside the step.
8. **Lazy client initialisation is not a lost update** — the memoised singleton
   tripped RG003, but losing that write costs a re-initialisation, not
   correctness.

### From the community corpus

9. **A stored closure was analysed at its definition site.** Saga compensation
   closures are defined at handler top level, pushed into an array, and executed
   later from inside a step. Five false positives in one file — while a
   structurally identical helper called directly from a step was correctly
   silent, which is what isolated the cause.
10. **`n++` was not recognised as a mutation** — tree-sitter gives it
    `update_expression`; only assignment forms were handled. This hid a genuine
    RG003 in the same file that produced four false positives.
11. **`parallel([...])` is an unnamed overload** — argument 0 is an array of
    branch bodies, not a name. Binding it as the name made RG005 report a span
    covering 200 lines, against the file whose author had most carefully avoided
    exactly that hazard.

Fixing 9 over-corrected — branch arrays passed to `parallel`/`map` were marked
unknown when they are step bodies, tripling coverage notes until corrected. Only
re-running the corpus caught it; the unit tests were green throughout.

---

## Known false negatives

- **Calls through a receiver are not followed.** `self.helper()`, `obj.helper()`,
  and calls through a field resolve to nothing. Only bare-name calls, and
  `this.`/implicit receivers in Java.
- **Cross-file calls are not followed** at all.
- **RG003 has no read-back check**, so it both over-reports (write-only
  instruments) and under-reports (values consumed outside the step).
- **A helper reached from two call sites is walked once**, so the reported route
  is the first one found.
- **Chains deeper than five frames** stop; the cutoff is recorded as RG900.

---

## Method

```bash
replayguard check <repo> --format json --show-coverage-gaps
```

Every finding was read against its source and classified real / false positive /
marginal. Triage of the community corpus was done by three independent reviewers
working from the source, one of them specifically tasked with **refuting** the
saga conclusion rather than confirming it. It confirmed the mechanism and
corrected two details, both incorporated above.

## What would raise confidence next

1. **The read-back check for RG003** — clears the four known false positives and
   catches the class it currently misses.
2. **A corpus that exercises RG002/RG004/RG005.** Three rules with no real-world
   evidence is the biggest gap in this record.
3. **Dynamic replay-divergence** — the only thing that can validate what static
   analysis structurally cannot see.
