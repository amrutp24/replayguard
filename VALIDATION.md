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
| `pgdad/durable-rust` (Rust SDK) | 1 | 107 | 105 | 1 |
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

| Rule | AWS | Community | Confirmed real | Fires in py/ts/java/rust | Known false positives |
|---|---:|---:|---:|:-:|---|
| RG001 clock/random/identity | 11 | 2 | yes | 4/4 | none known |
| RG002 external I/O | 0 | 0 | not yet in the wild | 4/4 | n/a |
| RG003 outer write in step | 0 | 5 | yes | 4/4 | **none known** |
| RG004 nondeterministic branch | 0 | 0 | not yet in the wild | 4/4 | none known |
| RG005 unstable operation name | 0 | 0 | not yet in the wild | 4/4 | none known |
| RG900 coverage gap (note) | 32 | 19 | n/a | n/a | n/a |

The last two columns answer different questions and neither substitutes for the
other. **Fires in 4/4** means the rule was handed that mistake, written the way
each language actually expresses it, and reported it -- `tests/test_rule_matrix.py`,
20 cells, enforced on every run. **Confirmed real** means someone else's code
contained the mistake. A rule can be 4/4 and still have found nothing real,
which is exactly the position RG002, RG004 and RG005 are in.

The same file runs the negative half: a correct handler in each language -- the
same work, every clock checkpointed and every value returned from its step
rather than written from inside one -- must produce **nothing**. 4/4 silent.

**Rust corpus: 105 handlers, 134 operations, zero findings.** The SDK authors'
own code is deterministic, which is what one would hope -- and it is a
false-positive check on a fourth language at the same time.

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

**Working, but unproven in the field.** RG002, RG004 and RG005 have never
produced a true positive on code written by someone else. Two separate things
were done about that. A deliberate hunt for evidence (2026-08-19) concluded that
**the silence is correct, not broken** -- see below. And the rule matrix
(2026-08-27) hands each of them its own mistake in all four languages and
requires a finding, so "no findings" can no longer quietly mean "no detector".
Neither of those makes them proven: a rule that works on a snippet written by
the person who wrote the rule has not yet met code written under deadline.

---

## Dynamic replay-divergence — independent corroboration

The static conclusion above was reached by inspecting code. The dynamic harness
tests the same question by *running* it, and agrees.

**Method.** Run a handler twice: once as a control, once in a world where the
clock has moved 13 hours, entropy is reseeded, and identity generators return
different values. Diff the operation journals -- kinds, names, order, nesting.
A handler that is a pure function of its inputs and checkpointed results is
unaffected. One that is not changes shape, and the change is the evidence.

**Result: 51 AWS conformance handlers executed twice each. Zero divergences.**
Five more suspend on a callback the local runner never delivers and cannot be
checked this way; one had no handler symbol.

That is corroboration in both directions:

* AWS's handlers really are deterministic, so RG002/RG004/RG005 having nothing
  to report was correct, not a detection failure.
* The harness produced **no false alarms on 51 correct handlers**, which is the
  property that decides whether anyone leaves it switched on.

**What it catches that no static rule can.** A handler whose operation order is
driven by `random.sample` diverges under perturbation. There is no catalog entry
for that and none could reasonably be written -- the harness measures the
effect, not the cause.

**What it cannot do.** Prove determinism. A handler that survives this
perturbation may diverge under another, and the report says so rather than
claiming a clean bill of health. It also cannot check handlers that suspend
waiting for a callback, and it now refuses to answer at all for a handler that
raises before its first operation — see bug 14 below.

## Why RG002, RG004 and RG005 never fire

A dedicated hunt, because three silent rules could mean either "the code is
clean" or "the rules are broken". The two have very different consequences and
the distinction was worth establishing rather than assuming.

**The durable regions were inspected directly** across all 837 handlers, not
just the findings:

| Evidence | Result |
|---|---|
| Branch conditions in durable regions | **85 found.** Every one tests stable data -- `isinstance`, `Array.isArray`, `len`, checkpointed step results (`result.failed`, `decision.shouldRetry`), or event fields. **Not one tests a clock or a random source.** |
| Non-literal operation names | **38 found.** Every one derives from stable data -- `tx.id`, `event.id`, `specialist.name`, a loop index. **Not one from a clock.** |
| Calls in durable regions | Dominated by durable operations, logging, and pure helpers. **No I/O.** |

So published durable-function code puts its I/O inside steps, branches on
checkpointed data, and names operations stably. The three rules guard against
mistakes competent authors do not make in example code they publish.

**The rules do fire.** `tests/test_rule_matrix.py` gives each rule its own
minimal violation in each of the four languages -- an unstepped `fetch`, a branch
on `Date.now()`, an operation named ``op-${Date.now()}`` -- and fails if the rule
stays quiet. All 20 cells fire. Each violation is written the way its language
would actually express it: the Rust RG003 case uses `Arc<Mutex<..>>`, because a
borrowed capture will not compile under that SDK's `Send + 'static` bound, so
interior mutability is the only shape the bug can take there.

This is synthetic and is **not** evidence that anyone writes these bugs. It only
separates "nothing to find" from "broken" -- and it now does so on every test
run, rather than once by hand.

**What this means.** These rules are unproven on real code and likely to stay
that way against public examples. If they matter, the evidence will come from
production code written under deadline, which is not accessible here. Do not
describe them as validated.

**The hunt also found two real bugs**, listed below: annotated assignments not
binding names, and module-level durable operations going unrecognised -- which
had left 488 files effectively unanalysed.

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

Ten so far. None were catchable by the fixtures, because the same person wrote
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
11. **Annotated assignments did not bind names.** `result: str = await invoke(...)`
    is `ast.AnnAssign`, and only `ast.Assign` was handled, so annotated names
    looked unbound to both the unresolved-callable heuristic and the outer-write
    scope check. Typed assignment is idiomatic in exactly the SDK-heavy code
    this tool targets.
12. **Module-level durable operations were unrecognised.** Some SDKs expose
    `step`/`invoke`/`wait` as imported functions rather than context methods
    (`from async_durable_execution import step`). Requiring a context receiver
    left every step boundary in those files unrecognised, so whole handlers read
    as durable region -- 488 of the 685 community files. Now matched, guarded by
    import provenance since `step` is far too common a name to match bare.
13. **`parallel([...])` is an unnamed overload** — argument 0 is an array of
    branch bodies, not a name. Binding it as the name made RG005 report a span
    covering 200 lines, against the file whose author had most carefully avoided
    exactly that hazard.

Fixing 9 over-corrected — branch arrays passed to `parallel`/`map` were marked
unknown when they are step bodies, tripling coverage notes until corrected. Only
re-running the corpus caught it; the unit tests were green throughout.

### From testing the CLI (2026-08-27)

14. **A handler that crashed immediately was reported as clean.** Both runs of
    the dynamic harness failed the same way, so no journal entry differed, so the
    report said "no divergence across 0 operation(s)" and `replayguard replay`
    exited 0. A handler that never executed an operation was getting a green CI
    check from the tool whose entire purpose is to not do that.

    Now split on whether anything was actually compared: both runs failing with
    an **empty** journal is a harness error (exit 2, "nothing was compared, so
    this run says nothing about determinism"); a handler that fails *after* some
    operations still gets a real comparison, with the shared failure stated in
    the report rather than hidden behind "no divergence". The second half matters
    because a durable execution ending FAILED is a legitimate path someone may
    want checked.

    Found by writing the first tests for the `replay` subcommand, which had 0%
    coverage — the dynamic engine was well tested, the only way anyone reaches it
    was not.

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

1. **A corpus that exercises RG002/RG004/RG005.** Three rules with no real-world
   evidence is the biggest gap in this record, and the rule matrix does not close
   it -- it only rules out the alternative explanation. Public example code does
   not contain these mistakes; production code written under deadline does, and
   none is accessible here.
2. **Cross-file analysis.** A handler calling into another module is currently a
   coverage gap (RG900), so a violation one call away is invisible to every rule.
3. **A false-positive count from someone else's repository.** The 1,547-file
   corpus was chosen by the tool's author. A repository chosen by its own
   maintainer, judged by that maintainer, is the number that would actually
   settle whether this is usable.

Done since this section was written: the read-back check for RG003 (2026-08-19),
the dynamic replay-divergence harness, and the rule matrix (2026-08-27).
