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

TypeScript and Java need a parser:

```bash
pip install 'replayguard[all]'
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

## Developing

One command verifies everything locally:

```bash
python scripts/verify.py
```

It runs five gates — import, lint, tests with a coverage floor, the CLI's exit
codes and every output format, and a canary asserting the known-good fixtures
produce **zero** findings. Exit code is 0 only if all five pass.

The canary is the one that matters. A linter that fires on correct code gets
uninstalled, after which it catches nothing, so a false positive is a worse
failure than a missed bug and gets its own gate.

```bash
pip install -e ".[dev]"
```

## How far to trust it

[VALIDATION.md](VALIDATION.md) records what has actually been tested, against
whose code, and what is still unproven -- including the rules that have **never**
produced a true positive outside this repository's own fixtures, and the false
positives still known to be present.

Read it before relying on a clean run.

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

**All three runtimes with an official AWS durable execution SDK are supported.**

| Runtime | Status | Parser |
|---------|--------|--------|
| Python | ✅ | stdlib `ast` |
| TypeScript / JavaScript | ✅ | tree-sitter |
| Java | ✅ | tree-sitter |
| Rust | ✅ | tree-sitter |
| Go, .NET | ❌ Out of scope | Community proofs of concept only. |

Rust has no *official* AWS SDK; the frontend targets
[pgdad/durable-rust](https://github.com/pgdad/durable-rust), whose own docs say
its determinism rules are documented, not enforced. That is exactly this tool's job.

The three SDKs don't just differ in syntax — they differ in shape:

| | Handler | Step |
|---|---|---|
| Python | `@durable_execution` | `context.step(fn, name="x")` |
| JS/TS | `withDurableExecution(fn)` | `context.step("x", fn)` |
| Java | `extends DurableHandler<,>` | `ctx.step("x", Result.class, fn)` |
| Rust | param typed `*Context` | `ctx.step("x", \|\| async { .. })` |

The callback is first, second, and third respectively — and Java also has a
two-argument overload, so its body is located by *kind* rather than by position.
All three lower to one IR, so rules are written once. A rule that needed to know
its language would mean the frontend hadn't normalized enough.

Genuine semantic differences *are* encoded per-frontend, and RG003 is where they
show up:

- **Python** — a bare `x = 1` in a nested function creates a local binding, so it
  can never be an outer write. Only mutation and `global`/`nonlocal` reach out.
- **JavaScript** — the same assignment writes straight through to the enclosing
  scope, so RG003 has more ways to fire.
- **Java** — captured locals must be effectively final, so reassigning one is a
  *compile error* and that violation class cannot exist. What remains is
  collection mutation and instance/static field writes.
- **Rust** — the narrowest of the four. Step closures are `Send + 'static`, so
  capturing a borrowed reference does not compile at all; the borrow checker
  rejects the JavaScript shape outright. What is left is interior mutability
  through a shared handle — an `Arc<Mutex<_>>` locked and pushed to, or a
  `static mut`.

Four languages, four outer-write models, one rule.

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

**Calls are not followed across function boundaries.** A handler that calls a
private helper which does the I/O will not be flagged — analysis stops at the
handler body. This is the largest known blind spot and is not covered by RG900.

Static analysis also can't see nondeterminism inside a third-party library, data
tainted several hops back, iteration order over an unordered collection, or
concurrent completion order. It can't tell you what happens when the platform
changes underneath a suspended execution either.

## Dynamic replay-divergence

Static analysis reasons about what code *might* do. The dynamic half runs the
handler twice -- once normally, once in a world where the clock has moved and
entropy is reseeded -- and diffs the operation journals:

```bash
replayguard replay app.orders:handler --event '{"orderId": "A1"}'
```

```
replay-divergence: 1 divergence(s) found.

  operation 0: operation name changed -- checkpoints match by name
    control   : step(op-1787442395)
    perturbed : step(op-1787489626)
```

Or as an assertion next to the handler, so a determinism regression fails the
build rather than surfacing on a resume months later:

```python
from replayguard.dynamic import assert_deterministic

def test_handler_is_deterministic():
    assert_deterministic(handler, {"orderId": "A1"})
```

It needs no rule for the *source* of nondeterminism -- a clock inside a library,
an iteration order, a value tainted many hops back. It measures the effect. On
51 AWS conformance handlers it produced zero false alarms; on a handler whose
step order comes from `random.sample` -- which no static rule covers -- it
diverges.

It cannot prove determinism, only fail to disprove it, and the report says so.
Handlers that suspend on a callback can't be checked locally.

## Prior art

[Temporal's `workflowcheck`](https://github.com/temporalio/sdk-go) does this for
Temporal workflows — the category is proven, it just doesn't exist for AWS's
primitive. [durable-viz](https://github.com/gunnargrosch/durable-viz) statically
analyses durable handlers to draw flowcharts, but performs no validation.

## License

MIT
