# Roadmap

## The goal

> **Be the determinism check teams run in CI before their first long-running
> durable execution comes due.**

Durable functions went GA in early 2026 and can suspend for up to 366 days. The
first workflows started in 2026 come due through 2027. Determinism violations are
silent until resume — so the bugs being written right now are invisible right
now, and land later, in production, on workflows holding real state.

There is a window to be the tool that exists before that happens. That window is
the whole opportunity.

**Success looks like:** a team building on durable functions installs this
without being told to, because it is the obvious thing to install.

**Failure looks like:** technically correct, nobody uses it. Or worse — someone
installs it, it fires on their working code, and they uninstall it and tell
others not to bother.

> This goal statement is a proposal. If the real aim is different — a portfolio
> piece, a conference talk, a stepping stone to the Rust SDK — the sequencing
> below changes and should be rewritten first.

## Why this is winnable

- **AWS ships nothing.** Their docs state determinism as a developer obligation
  with no verification tooling.
- **The category is proven.** Temporal shipped `workflowcheck` for exactly this
  problem. The need is not speculative.
- **Verified absent for AWS.** GitHub repo search on four term combinations,
  code search, and PyPI all returned nothing (2026-08-17).
- **The nearest neighbour cannot pivot cheaply.** `durable-viz` parses
  Python/Java/C# with regex — adequate for drawing flowcharts, structurally
  incapable of scope resolution and dataflow.

## Where we are — M0 complete

Static checker across all three runtimes with an official AWS SDK. Shared IR, 6
rules, CLI with text/JSON/SARIF, 38 tests, validated against one real handler.

Runtime coverage is *finished*, not partial: Rust has no SDK, Go and .NET have
only community proofs of concept.

---

## M1 — Earn trust on real code  *(validation run done 2026-08-19)*

**The gate before anything is published.** A linter gets exactly one first
impression. One false positive on someone's working handler and it is uninstalled
permanently.

| Task | Why |
|---|---|
| Interprocedural analysis, intra-file | Largest known blind spot. A handler calling a private helper that does the I/O currently passes clean. Found when the checker missed a `Files.readString` in my own fixture. |
| Run against every public durable-functions repo | `aws-samples/sample-lambda-durable-functions`, `aws-samples/sample-ai-workflows-in-aws-lambda-durable-functions`, `singledigit/durable-function-video-scanner`, `singledigit/durable-serverlesspresso`, `lukehedger/lambda-durable-functions-examples`, `awedis/aws-lambda-durable-functions-callback`, plus the three official SDK repos' examples. |
| Triage every finding | Each is either a real bug (write it up — free credibility) or a false positive (fix it). |
| Cross-frontend diffing | Running equivalent fixtures through two frontends already caught one real inconsistency. Automate it. |

**Definition of done:** zero *unexplained* findings across every public repo
above. Every remaining finding is either a defensible real bug or a documented,
justified limitation.

### Result of the first validation run

Ran against AWS's own samples and all three official SDK repositories --
751 source files. **145 findings became 34.** Every false positive was traced,
fixed, and pinned with a regression test in `tests/test_real_world_regressions.py`.

Seven bugs, none of which the hand-written fixtures could have caught, because
the same person wrote both the fixtures and the frontends:

1. **TS client taint reached response data.** `output.content.find(...)` --
   an ordinary `Array.prototype.find` -- reported as external I/O because the
   taint propagated from an SDK client through a step result into its response.
2. **Python step bodies passed by keyword were missed.** AWS's own sample uses
   `wait_for_callback(submitter=fn)`; only `args[0]` was checked, so the body
   was analysed in the durable region.
3. **The durable surface is far wider than `step`.** `stepAsync`, `withRetry`,
   `map`, `wait`, `createCallback`, `runInChildContext` and their async variants
   were all unrecognised -- 13 missing in Python and TypeScript, 8 in Java.
4. **`await` + a generic type argument broke detection entirely.** tree-sitter
   parses `await context.waitForCallback<T>(...)` with the *await_expression*
   as the call's `function` field.
5. **Child contexts were not contexts.** `runInChildContext(async (childContext)
   => ...)` names its context freely; a fixed list of four receiver names missed it.
6. **Unnamed operations had their body walked twice**, once in the wrong region.
   `context.step(fn)` makes `positional[0]` the body itself.
7. **RG900 buried everything.** Coverage notes outnumbered real findings 7:1.
   Now suppressed by default with the count always reported, and the heuristic
   tightened so data arguments are not mistaken for unresolved callables.

**Remaining: 10 findings, all RG001, all read by hand and believed genuine** --
`System.nanoTime` and `Instant.now` in the durable region of Java SDK examples,
`datetime.now()` feeding `completed_at` in the FSI payment sample, `new Date()`
in the retail order sample, and ``task-${Date.now()}`` building a task id in the
AutonomousCodingAgent sample. That last one is the most consequential: the id
changes on every replay.

All are literal violations of AWS's documented rule. Most flow only into a
returned payload, so consequences are limited; the task-id case is not.

### Interprocedural analysis — done 2026-08-19

Calls are now followed into functions defined in the same file, in all three
languages. The region travels with the call, not the definition: a helper is a
violation when the durable region calls it and correct when a step body does.
Findings name their route ("Reached from the handler through `readConfig()`").

Built by three parallel agents, one per frontend, against a fixed IR contract.
All three independently reported that the depth cap truncated *silently* --
which contradicts the honest-coverage stance RG900 exists for. It now records a
coverage gap at the cutoff.

**The re-run against AWS's repos was the real test, and it introduced five new
false positives** that the unit tests could not have caught:

- **`@durable_step` bodies are deferred.** The decorator makes calling the
  function return a step descriptor, so `context.step(validate(event), ...)`
  runs the body *inside* the step. Following it as an ordinary durable-region
  call reported every clock and SDK call in the body — four findings against the
  FSI payment sample and the Python conformance tests.
- **Lazy client initialisation is not a lost update.** The memoised singleton
  (`if _client is None: _client = boto3.client(...)`) tripped RG003, but losing
  that write on replay costs a re-initialisation, not correctness. Firing on it
  would hit almost every real handler.

Both are fixed and pinned. **Net result: 34 findings -> 35, real findings
10 -> 11.** Interprocedural analysis found exactly one genuine new violation
(`buildPaymentResult()` reaching `new Date()` in the retail sample) and, once
corrected, added no noise.

**Known limits, consistent across the three frontends:** only bare-name calls
resolve — `self.helper()`, `obj.helper()`, and calls through a field are not
followed; cross-file calls are out of scope; a helper reached from two call
sites is walked once, so the reported route is the first one found.

**Why first:** it is cheap, it is the highest-risk-if-skipped step, and finding
real bugs in AWS's own samples would be the single most persuasive thing that
could happen to this project.

---

## M2 — Ship it

Distribution is not an afterthought. A correct tool nobody installs has failed.

| Task | Notes |
|---|---|
| PyPI release | `pip install replayguard` |
| GitHub repo, public | Name/visibility is the owner's call |
| GitHub Action | Three lines in a workflow. SARIF already emitted, so findings render inline on the PR that introduced them — which is the whole point for a bug class nobody notices for months. |
| `pre-commit` hook | Catches it before the PR exists |
| README quickstart | Already largely written |

**Definition of done:** on a clean machine, `pip install replayguard` then
`replayguard check .` works; a GitHub Action annotates a PR with a real finding.

**Sequencing note:** M2 depends on M1. Publishing first and fixing later is the
one ordering that can permanently lose.

---

## M3 — The differentiator: dynamic replay-divergence

Without this, replayguard is a competent `workflowcheck` analogue for a different
platform. With it, it is a category nobody occupies.

Static analysis structurally cannot see: nondeterminism inside a third-party
library, data tainted several hops back, iteration order over unordered
collections, concurrent completion order, or the platform changing underneath a
suspended execution.

Dynamic checking sees all of it, because it does not need to predict the cause.

| Task | Notes |
|---|---|
| **Check `aws/aws-durable-execution-conformance-tests` first** | Almost certainly validates *SDK protocol* conformance, not *user workflow* determinism — but this was never confirmed, and it decides whether this milestone is novel. |
| Journal capture | AWS's testing SDK exposes execution status, operation results, ordering, and counts. |
| Forced replay + diff | Run, replay, compare journals. Any difference is a determinism bug. |
| Perturbed replay | Replay under a different wall clock, different machine, swapped SDK version. This is the fault-injection half, and it answers the owner's own Rule 4 question — whether pinning the SDK is actually sufficient. |

**Definition of done:** the harness detects a deliberately injected divergence
that the static checker provably cannot catch. That single demo is the proof the
whole milestone exists for.

---

## M4 — The finding nobody has published

Turn the tooling into knowledge. **What actually happens when replay determinism
is violated?** Clean failure? Silent re-execution of a side effect? A stuck
execution? A checkpoint result delivered to the wrong operation?

AWS's docs imply consequences (they name double-charging) but nothing documents
observed behaviour. M3's harness is exactly the instrument for measuring it.

**Definition of done:** a written, dated, reproducible account of each observed
failure mode — which also feeds back into rule severity, since severity is
currently reasoned rather than measured.

This is the part that is genuinely research rather than engineering, and it is
what a talk or a post would be built on.

---

## M5 — Conditional: the Rust SDK  *(gate cleared 2026-08-22)*

**The gate is cleared.** `experiments/durable_runtime_probe.py` was run on
2026-08-22 against a real account (us-east-1). All three cells came back
ACCEPTED:

```
[A] python3.13       + DurableConfig  -> ACCEPTED   durable execution available here
[B] provided.al2023  + DurableConfig  -> ACCEPTED   THE QUESTION
[C] provided.al2023  no DurableConfig -> ACCEPTED   custom runtimes work
```

**Lambda does not gate `DurableConfig` to managed runtimes.** All three
functions were deleted afterwards and the account verified clean.

**What that establishes, precisely:** function *creation* is not gated. Combined
with the nine public durable-execution APIs, the fully specified
`OperationUpdate` wire format, and AWS's language-neutral conformance suite with
its extension API, the control-plane path to a Rust SDK is open.

**What it does not establish:** that the invoke/suspend path works. Lambda
accepting the configuration is not the same as a `provided.al2023` runtime
receiving durable execution context at invoke time, checkpointing through
`CheckpointDurableExecution`, and resuming. That is the next experiment, and it
needs a real Rust function rather than a config call.

The original conditional is therefore resolved this way:

- **Accepted (this is what happened)** → the strongest idea from this whole line
  of work is open: an SDK where nondeterminism is a **compile error** rather
  than a lint. Ownership of the durable context, borrow-checker enforcement of
  step boundaries. No durable SDK in any ecosystem does this, and Python and JS
  structurally cannot — which is precisely why they need replayguard.

### The invoke path works too — confirmed 2026-08-22

`experiments/durable_invoke_probe.py` deployed a `provided.al2023` function with
`DurableConfig`, invoked it, and had it echo back whatever it was handed. The
custom runtime received:

```json
{"DurableExecutionArn": "arn:aws:lambda:us-east-1:...:function:durable-invoke-probe:4
    /durable-execution/03229992-89d5-416f-b2a2-bb60b7dbdba5/c13a0886-...",
 "CheckpointToken": "<redacted>-kms..."}
```

**Both fields arrive. The gate was not cosmetic.**

Two supporting observations:

* An earlier attempt returned `{"probe":"ok"}` and Lambda rejected it with
  `Invalid Status in invocation output`. That error comes from the *durable
  layer*, which proves the durable machinery is engaged on a custom runtime
  rather than bypassed.
* The protocol needs no privileged runtime hook. It is plain JSON in the invoke
  body and plain AWS API calls out:

  | | Shape |
  |---|---|
  | in | `{DurableExecutionArn, CheckpointToken, InitialExecutionState, UpdatedOperationIds}` |
  | out | `{Status: SUCCEEDED \| FAILED \| PENDING \| RETRY, Result?, Error?}` |

  `PENDING` is how a handler suspends.

**A Rust durable SDK is therefore an engineering project, not a research
question.** Everything it needs is public: nine documented control-plane APIs, a
specified `OperationUpdate` wire format, a payload contract confirmed
empirically, and a language-neutral conformance suite with an extension API to
validate the result against.

### The full suspend/resume cycle works - proven 2026-08-22

`experiments/durable_suspend_resume_probe.py` closes the last gap. A
`provided.al2023` function checkpointed a WAIT operation, returned `PENDING`,
and the platform re-invoked it after the timer fired **with that operation in
its state**:

| | Operations in state | Updated |
|---|---|---|
| invoke 1 | `dd105579...` (execution) | `dd105579...` |
| invoke 2 | `dd105579...`, **`rg-wait-1`** | **`rg-wait-1`** |

Final execution status `SUCCEEDED`, result `"resumed"`. Suspend, checkpoint,
resume, and replay-against-state all work on a custom runtime.

**Three things this cost, worth knowing before repeating it:**

* Lambda rejects `{"Status": "PENDING"}` when nothing is outstanding --
  *"Cannot return PENDING status with no pending operations."* The operation has
  to be checkpointed **before** suspending, so checkpointing from outside
  afterwards cannot work; the token is spent by then.
* The execution role needs `AWSLambdaBasicDurableExecutionRolePolicy`.
  `AWSLambdaBasicExecutionRole` alone produces *"not authorized to perform:
  lambda:CheckpointDurableExecution"* -- which is Rule 3 of
  [SWA-001](../serverless/docs/standards/SWA-001-durable-function-module-design.md),
  encountered live rather than read.
* `provided.al2023` ships curl 8.17 with `--aws-sigv4`, and Lambda injects
  credentials as environment variables, so a shell bootstrap can sign its own
  checkpoint. No Rust toolchain was needed to answer a question about Rust.

**Nothing about a Rust durable SDK is now unknown.** The control plane is
public, the payload contract is confirmed, the response contract is confirmed,
and the full lifecycle has been exercised end to end from a custom runtime. What
remains is writing it.

---

## Risks

| Risk | Mitigation |
|---|---|
| AWS ships their own checker | Move on M1/M2. The dynamic half (M3) is much harder to replicate and is where the durable advantage is. |
| `durable-viz` adds validation | They would need to replace three regex parsers with real ASTs. Also genuinely complementary — coordination is more sensible than competition. |
| Durable functions adoption is slow | The tool has no users regardless of quality. Partly hedged by M4, which has value as knowledge even with few users. |
| False positives on first contact | This is what M1 exists for, and why it precedes M2. |

## Decisions needed

1. **Is the goal statement right?** Everything below it is sequenced from it.
2. **Public repo, and under what name?** Gates M2.
3. **PyPI under your name?** A published package is a standing maintenance
   commitment.
4. **Can credentials be put in front of the probe?** Cheap, and may reorder the
   back half.

## Sequencing summary

```
M1 trust  ──▶  M2 ship  ──▶  M3 dynamic  ──▶  M4 findings
   (gate)                          ▲
                                   │
M5 Rust SDK ◀── probe ─────────────┘  (run the probe early; it may reorder)
```
