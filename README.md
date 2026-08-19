# replayguard

**A determinism checker for AWS Lambda durable functions.**

Durable functions re-run your handler from the top on every resume. Completed
steps aren't re-executed — the SDK returns the checkpointed result and the
handler fast-forwards back to where it suspended.

That only works if the handler takes the same path every time. AWS states the
obligation plainly:

> Any code that is not inside a durable operation must be a pure function of the
> handler inputs and the results of completed operations.

No clocks, no randomness, no I/O, no mutable global state outside a `step()`.
**AWS ships no tooling to check any of it.** replayguard is that check.

## Why it matters

The failure mode is silent. From AWS's own documentation:

> The first invocation looks correct because the body runs and the write lands.
> Replay returns the cached result and skips the body, so the outer state stays
> at its initial value.

Nothing throws. Tests pass. The bug surfaces on resume — which for a workflow
that can suspend for up to 366 days might be months later, in production, on
something holding real money. AWS specifically calls out double-charging as a
consequence.

## Install

```bash
pip install replayguard
```

TypeScript support needs a parser:

```bash
pip install 'replayguard[typescript]'
```

## Use

```bash
replayguard check src/
```

```bash
replayguard check src/ --explain
```

```bash
replayguard check src/ --format sarif -o replayguard.sarif
```

Exits non-zero when anything at or above `--fail-on` (default `error`) is found,
so it gates a build without extra wiring.

```
tests/fixtures/python/bad_handler.py
    25:17  error   RG001  `time.time` runs outside a durable step
    37:14  error   RG002  external I/O `requests.get` runs outside a durable step
    53:8   error   RG003  step body writes to a captured variable `receipts`
    43:7   error   RG004  branch condition depends on `datetime.datetime.now`
    68:4   error   RG005  `step` name is built from `time.time`
    71:17  note    RG900  could not resolve whether this code runs inside a step
```

## Rules

| ID | What it catches | Why it breaks replay |
|----|-----------------|----------------------|
| **RG001** | Clock, random, or identity source outside a step | Produces a different value on replay; everything derived from it diverges |
| **RG002** | Network or filesystem access outside a step | Diverges *and* repeats the side effect on every replay |
| **RG003** | A step body writing to state it doesn't own | Silently lost — the body is skipped on replay, so the write never happens again |
| **RG004** | Control flow depending on a nondeterministic value | Replay takes the other branch, so the operation sequence no longer matches the journal |
| **RG005** | A step name built from an unstable source | Checkpoints match by name and order; a name that changes can't be matched, so the step re-executes |
| **RG900** | Code whose region couldn't be resolved | Not a violation — a coverage gap, reported so a clean run means something |

`replayguard rules --explain` prints the full rationale for each.

## Language support

| Runtime | Status | Notes |
|---------|--------|-------|
| Python | ✅ | stdlib `ast` |
| TypeScript / JavaScript | ✅ | tree-sitter |
| Java | ⬜ Planned | [official SDK](https://github.com/aws/aws-durable-execution-sdk-java) exists |
| Rust, Go, .NET | ❌ Out of scope | No official durable execution SDK. Go and .NET have community PoCs only; Rust has none. |

Python and JS don't just differ in syntax — they differ in shape:

| | Python | JS/TS |
|---|---|---|
| Handler | `@durable_execution` | `withDurableExecution(fn)` |
| Step | `context.step(fn, name="x")` | `context.step("x", fn)` |

Both lower to one IR, so rules are written once. A frontend that needs a rule to
know its language means the frontend didn't normalize enough.

One real semantic difference *is* encoded per-frontend: in Python a bare `x = 1`
inside a nested function creates a local binding, so it can never be an outer
write. In JavaScript it writes straight through to the enclosing scope. RG003
therefore has more ways to fire in JS.

## Design

```
source ──▶ frontend ──▶ IR ──▶ rules ──▶ findings ──▶ reporter
           (per-lang)   (shared)  (shared)            text/json/sarif
```

**Why AST and not pattern matching.** RG003 has to know whether a mutated name
belongs to the step body or an enclosing scope. RG004 has to know whether a
branch condition derives from a nondeterministic source. Neither question can be
answered by matching source text, which is why this is a different kind of tool
from the extractors that draw workflow diagrams.

**Accuracy over coverage.** A linter that fires on correct code gets switched
off, and then it catches nothing. Two rules exist mostly to prevent that: RG005
ignores computed step names unless they interpolate something genuinely unstable
(`` `item-${index}` `` is the pattern AWS recommends), and RG900 reports
unresolved regions rather than silently passing them.

## What it doesn't do

Static analysis can't see nondeterminism inside a third-party library, data
tainted several hops back, iteration order over an unordered collection, or
concurrent completion order. It also can't tell you what happens when the
platform changes underneath a suspended execution.

Those need **dynamic replay-divergence checking**: run the workflow, force a
replay, and diff the operation journals. Any difference is a determinism bug
regardless of cause. That's the planned second half of this tool, and nothing
like it exists today.

## Prior art

[Temporal's `workflowcheck`](https://github.com/temporalio/sdk-go) does this for
Temporal workflows — the category is proven, it just doesn't exist for AWS's
primitive. [durable-viz](https://github.com/gunnargrosch/durable-viz) statically
analyses durable handlers to draw flowcharts, but performs no validation.

## License

MIT
