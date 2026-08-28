# Validation record

What has been tested, against whose code, and what is still unproven. Every
number here comes from a run that was actually performed.

Last full corpus run: 2026-08-19. The rule matrix and CLI tests were added
2026-08-27. These numbers date; re-run before trusting them blindly.

---

## Corpora

| Corpus | Repos | Files | Durable handlers | Authors |
|---|---:|---:|---:|---|
| AWS samples + all three official SDKs | 5 | 755 | 415 | AWS |
| Community | 11 | 685 | 422 | ~10 independent |
| `pgdad/durable-rust` (Rust SDK) | 1 | 107 | 105 | 1 |
| **Total** | **16** | **1,547** | **942** | |

Community repos: `singledigit/durable-serverlesspresso`,
`singledigit/durable-function-video-scanner`,
`lukehedger/lambda-durable-functions-examples`,
`awedis/aws-lambda-durable-functions-callback`,
`misskecupbung/aws-lambda-durable-functions`, `TrickSumo/DurableLambdaCourse`,
`zhongkechen/async-durable-execution`,
`dobeerman/aws-lambda-durable-slack-approval-demo`,
`hsaenzG/durable-funtions-demo`, `brianleroux/arc-plugin-workflow`,
`gunnargrosch/durable-viz`.

The community corpus turned out to matter more than the AWS one. AWS's samples
are written in a single house style; ten independent authors hit shapes the
fixtures never covered, and that's where most of the real bugs in this tool
were found.

---

## Current state

| Rule | AWS | Community | Confirmed real | Fires in py/ts/java/rust | Known false positives |
|---|---:|---:|---:|:-:|---|
| RG001 clock/random/identity | 11 | 2 | yes | 4/4 | none known |
| RG002 external I/O | 0 | 0 | not yet in the wild | 4/4 | n/a |
| RG003 outer write in step | 0 | 5 | yes | 4/4 | none known |
| RG004 nondeterministic branch | 0 | 0 | not yet in the wild | 4/4 | none known |
| RG005 unstable operation name | 0 | 0 | not yet in the wild | 4/4 | none known |
| RG900 coverage gap (note) | 32 | 19 | n/a | n/a | n/a |

The last two columns answer different questions. "Fires in 4/4" means the rule
was handed its own mistake, written the way each language actually expresses
it, and reported it (`tests/test_rule_matrix.py`, 20 cells, run on every test
pass). "Confirmed real" means someone else's code contained the mistake. A rule
can be 4/4 and still never have found anything real, which is where RG002,
RG004, and RG005 currently stand.

The same test file runs the reverse check: a correct handler in each language,
doing the same work with every clock checkpointed and every value returned from
its step, has to produce nothing at all. All four are silent.

The Rust corpus (105 handlers, 134 operations) produced zero findings. The SDK
authors' own code is deterministic, which is what you'd hope, and it doubles as
a false-positive check on the fourth language.

RG003 gained a read-back reachability check on 2026-08-19: a write in a step
body is only reported when the value is read somewhere the write may not have
run, meaning the durable region or a different step body. That cleared all four
known false positives without losing a true positive.

### Confidence

**RG001 and the region-detection machinery under every rule: solid.** RG001
produced 13 findings across the corpus and drove most of the bug fixes below.
One finding is confirmed by the code's own author, who named the file
`handler-with-violation.ts`, shipped a matching `handler-fixed.ts`, and wrote a
test asserting the bug manifests.

**RG003: solid, smaller sample.** All five current findings are confirmed true
positives (the saga, and two `resizeAttempts` counters), with no known false
positives after the read-back check.

**RG002, RG004, RG005: working, but unproven in the field.** None has produced
a true positive on code written by someone else. Two things were done about
that. A deliberate hunt (2026-08-19, below) concluded the silence is a property
of the corpus, not a detection failure. And the rule matrix (2026-08-27) hands
each of them its own mistake in all four languages and requires a finding, so
"no findings" can no longer quietly mean "no detector". Neither makes them
proven; a rule that catches a snippet written by the person who wrote the rule
has not yet met code written under deadline.

---

## Dynamic replay-divergence

The static conclusion above was reached by reading code. The dynamic harness
asks the same question by running it, and agrees.

Method: run a handler twice, once as a control and once with the clock moved
13 hours, entropy reseeded, and identity generators returning different
values. Diff the operation journals: kinds, names, order, nesting. A handler
that is a pure function of its inputs and checkpointed results is unaffected.
One that isn't changes shape, and the change is the evidence.

Result: 51 AWS conformance handlers executed twice each, zero divergences.
Five more suspend on a callback the local runner never delivers and can't be
checked this way; one had no handler symbol.

That corroborates in both directions. AWS's handlers really are deterministic,
so RG002/RG004/RG005 having nothing to report was correct. And the harness
raised no false alarm on 51 correct handlers, which is the property that
decides whether anyone leaves it turned on.

What it catches that no static rule can: a handler whose operation order is
driven by `random.sample` diverges under perturbation. There's no catalog
entry for that and none could reasonably be written. The harness measures the
effect, not the cause.

What it can't do: prove determinism. A handler that survives this perturbation
may diverge under another, and the report says so. It can't check handlers
that suspend waiting for a callback, and it refuses to answer for a handler
that raises before its first operation (bug 14 below).

## Why RG002, RG004, and RG005 never fire

Three silent rules could mean the code is clean or the rules are broken, and
those have very different consequences, so this was checked directly rather
than assumed. The durable regions of all 837 handlers in the AWS and community
corpora were inspected, not just the findings:

| Evidence | Result |
|---|---|
| Branch conditions in durable regions | 85 found. Every one tests stable data: `isinstance`, `Array.isArray`, `len`, checkpointed step results (`result.failed`, `decision.shouldRetry`), or event fields. Not one tests a clock or a random source. |
| Non-literal operation names | 38 found. Every one derives from stable data: `tx.id`, `event.id`, `specialist.name`, a loop index. Not one from a clock. |
| Calls in durable regions | Dominated by durable operations, logging, and pure helpers. No I/O. |

Published durable-function code puts its I/O inside steps, branches on
checkpointed data, and names operations stably. These three rules guard
against mistakes that competent authors don't make in example code they
publish.

The rules themselves do fire. `tests/test_rule_matrix.py` gives each one a
minimal violation in each of the four languages (an unstepped `fetch`, a
branch on `Date.now()`, an operation named ``op-${Date.now()}``) and fails if
the rule stays quiet. All 20 cells fire. Each violation is written the way its
language would actually express it; the Rust RG003 case uses `Arc<Mutex<..>>`
because a borrowed capture won't compile under that SDK's `Send + 'static`
bound, so interior mutability is the only shape the bug can take there.

That's synthetic, and it isn't evidence that anyone writes these bugs. It only
separates "nothing to find" from "broken". If real evidence ever shows up, it
will come from production code written under deadline, which isn't accessible
here. Until then these rules should not be described as validated.

The hunt also found two real bugs, listed below: annotated assignments not
binding names, and module-level durable operations going unrecognised, which
had left 488 files effectively unanalysed.

---

## Confirmed true positives

**`lambda-durable-functions-examples/examples/saga/function.ts`**: the
strongest finding. `completedSteps` accumulates rollback entries inside step
bodies. On resume the handler re-initialises it to `[]`, the checkpointed step
bodies don't re-run, and the compensation loop iterates zero times, returning
500 as though rollback succeeded. Inventory stays reserved.

Two details worth keeping straight, both corrected during review: on this
example's `simulateFailure` path payment is never taken (it throws before its
push), and the rollbacks here are `logger.info` no-ops. The harm is that this
is published as the reference saga, and the bug is latent; three fast steps
usually complete in one invocation, so it only manifests on resume. A second
manifestation is sharper: interruption during compensation leaves partial
rollback silently reported as complete.

**`hsaenzG/durable-funtions-demo/src/handler-transient-failure.ts:70`**:
module-level `resizeAttempts` incremented inside a step body (line 70) and
read back outside it into the return value (lines 112, 129). Non-test handler
code. Found only after the `++` fix below.

**`hsaenzG/durable-funtions-demo/src/handler-with-violation.ts:22`**: a
deliberate, documented violation with a matching fixed variant.
Author-confirmed.

---

## Known false positives

None currently known. The four write-only observability instruments in
`article3-scenarios.test.ts` were cleared by the read-back check.

One mislabelling remains: closure variables are sometimes reported as
"module-level state". The finding is correct; the wording isn't.

### The read-back check, and the trap in it

The condition is not "is it read in the durable region?", which would have
suppressed the saga: its compensation list is read inside a
`runInChildContext` body, and neither that body nor the step bodies re-run on
replay, so a value written in one and read in another is still stale. The
condition is: read anywhere other than the step body that wrote it.

A second trap: excluding every mutation receiver from reads (on the grounds
that `log.push(x)` writes rather than consumes) also suppressed the saga,
because its loop consumes the array `reverse()` returns. The exclusion applies
only when the result is discarded, i.e. a bare expression statement.

Both traps were caught by re-running the corpora, not by the unit tests. Both
are pinned by regression tests now.

---

## Bugs this validation found — in the tool, not the code

Fourteen so far: thirteen from running against other people's code, one from
testing the tool's own command line. None were catchable by the fixtures,
because the same person wrote the fixtures and the frontends.

### From the AWS corpus

1. TS client taint reached response data. `output.content.find(...)`, an
   ordinary `Array.prototype.find`, was reported as external I/O.
2. Python step bodies passed by keyword were missed.
   `wait_for_callback(submitter=fn)` is AWS's own form.
3. The durable surface is far wider than `step`. `stepAsync`, `withRetry`,
   `map`, `wait`, `createCallback`, `runInChildContext` and async variants: 13
   missing in Python and TypeScript, 8 in Java.
4. `await` plus a generic type argument broke detection. tree-sitter parses
   `await context.waitForCallback<T>(...)` with the await_expression as the
   call's `function` field.
5. Child contexts weren't recognised as contexts.
   `runInChildContext(async (childContext) => ...)` names its context freely.
6. Unnamed operations had their body walked twice, once in the wrong region.
7. `@durable_step` bodies are deferred. The decorator makes the call return a
   step descriptor, so the body runs inside the step.
8. Lazy client initialisation is not a lost update. The memoised singleton
   tripped RG003, but losing that write costs a re-initialisation, not
   correctness.

### From the community corpus

9. A stored closure was analysed at its definition site. Saga compensation
   closures are defined at handler top level, pushed into an array, and
   executed later from inside a step. Five false positives in one file, while
   a structurally identical helper called directly from a step was correctly
   silent, which is what isolated the cause.
10. `n++` wasn't recognised as a mutation. tree-sitter gives it
    `update_expression`; only assignment forms were handled. This hid a
    genuine RG003 in the same file that produced four false positives.
11. Annotated assignments didn't bind names. `result: str = await invoke(...)`
    is `ast.AnnAssign`, and only `ast.Assign` was handled, so annotated names
    looked unbound to both the unresolved-callable heuristic and the
    outer-write scope check. Typed assignment is idiomatic in exactly the
    SDK-heavy code this tool targets.
12. Module-level durable operations were unrecognised. Some SDKs expose
    `step`/`invoke`/`wait` as imported functions rather than context methods
    (`from async_durable_execution import step`). Requiring a context receiver
    left every step boundary in those files unrecognised, so whole handlers
    read as durable region: 488 of the 685 community files. Now matched,
    guarded by import provenance, since `step` is far too common a name to
    match bare.
13. `parallel([...])` is an unnamed overload. Argument 0 is an array of branch
    bodies, not a name. Binding it as the name made RG005 report a span
    covering 200 lines, in the file whose author had most carefully avoided
    exactly that hazard.

Fixing 9 over-corrected: branch arrays passed to `parallel`/`map` were marked
unknown when they're actually step bodies, tripling coverage notes until
corrected. Only re-running the corpus caught it; the unit tests were green
throughout.

### From testing the CLI (2026-08-27)

14. A handler that crashed immediately was reported as clean. Both runs of the
    dynamic harness failed the same way, so no journal entry differed, so the
    report said "no divergence across 0 operation(s)" and `replayguard replay`
    exited 0. A handler that never executed an operation was getting a green
    CI check.

    The fix splits on whether anything was actually compared. Both runs
    failing with an empty journal is a harness error (exit 2: "nothing was
    compared, so this run says nothing about determinism"). A handler that
    fails after some operations still gets a real comparison, with the shared
    failure stated in the report, because a durable execution ending FAILED is
    a legitimate path someone may want checked.

    Found by writing the first tests for the `replay` subcommand, which had 0%
    coverage. The dynamic engine was well tested; the only way anyone reaches
    it wasn't.

---

## Known false negatives

- Calls through a receiver aren't followed. `self.helper()`, `obj.helper()`,
  and calls through a field resolve to nothing. Only bare-name calls, plus
  `this.`/implicit receivers in Java.
- Cross-file calls aren't followed at all.
- A helper reached from two call sites is walked once, so the reported route
  is the first one found.
- Chains deeper than five frames stop; the cutoff is recorded as RG900.

---

## Method

```bash
replayguard check <repo> --format json --show-coverage-gaps
```

Every finding was read against its source and classified real, false
positive, or marginal. The community-corpus findings were then triaged again
in separate passes, one of which started from the source with the goal of
refuting the saga conclusion rather than confirming it. The mechanism held;
two details were corrected and are incorporated above.

## What would raise confidence next

1. A corpus that exercises RG002/RG004/RG005. Three rules with no real-world
   evidence is the biggest gap in this record, and the rule matrix doesn't
   close it; it only rules out the alternative explanation. Public example
   code doesn't contain these mistakes. Production code written under deadline
   does, and none is accessible from here.
2. Cross-file analysis. A handler calling into another module is currently a
   coverage gap (RG900), so a violation one call away is invisible to every
   rule.
3. A false-positive count from someone else's repository. This corpus was
   chosen by the tool's author. A repository chosen by its own maintainer and
   judged by that maintainer is the number that would settle whether this is
   usable.
