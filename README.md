# replayguard

A determinism checker for AWS Lambda durable functions.

Durable functions re-run your handler from the top on every resume. Completed
steps aren't re-executed; the SDK returns the checkpointed result and the
handler fast-forwards back to where it suspended. This only works if the
handler takes the same path every time, and AWS's docs are explicit about
whose job that is:

> Any code that is not inside a durable operation must be a pure function of the
> handler inputs and the results of completed operations.

No clocks, no randomness, no I/O, no writes to shared state outside a `step()`.
Nothing enforces this. Break the rule and nothing throws, your tests stay
green, and the bug surfaces whenever the workflow next resumes. An execution
can suspend for up to 366 days, so that can be months later, in production.
AWS's docs name double-charging as a possible consequence.

replayguard checks the rule two ways: statically, by analyzing handler source
in Python, TypeScript, Java, and Rust, and dynamically, by running a handler
twice under different clocks and diffing what it did.

## Status

v0.1.0. Validated against 1,547 files of durable-function code written by
other people; [VALIDATION.md](VALIDATION.md) records what that established and
what it didn't.

Two known gaps. Calls aren't followed across files, so a handler that reaches
another module for its I/O passes clean. And three of the six rules have
working detectors but no confirmed real-world finding yet, because published
example code doesn't contain the mistakes they catch.

Reports from real codebases are the most useful thing anyone can contribute
right now, in either direction: a finding it caught, or a false positive it
shouldn't have raised.

## Install

```bash
pip install replayguard
```

TypeScript, Java, and Rust need a parser:

```bash
pip install 'replayguard[all]'
```

## Use

```bash
replayguard check src/
replayguard check src/ --explain
replayguard check src/ --format sarif -o replayguard.sarif
```

Exits non-zero when anything at or above `--fail-on` (default `error`) is
found, so it can gate a build without extra wiring.

```
tests/fixtures/python/bad_handler.py
    25:17  error   RG001  `time.time` runs outside a durable step
    37:14  error   RG002  external I/O `requests.get` runs outside a durable step
    53:8   error   RG003  step body writes to a captured variable `receipts`
    43:7   error   RG004  branch condition depends on `datetime.datetime.now`
    68:4   error   RG005  `step` name is built from `time.time`
    71:17  note    RG900  could not resolve whether this code runs inside a step
```

## In CI

SARIF output means findings render inline on the pull request that introduced
them:

```yaml
- uses: amrutp24/replayguard@v1
  with:
    path: src/
```

The job needs `permissions: security-events: write` for the annotations. Set
`fail-on: never` to annotate without blocking the merge.

There is also a pre-commit hook:

```yaml
- repo: https://github.com/amrutp24/replayguard
  rev: v0.1.0
  hooks:
    - id: replayguard
```

## Rules

| ID | What it catches | Why it breaks replay |
|----|-----------------|----------------------|
| **RG001** | Clock, random, or identity source outside a step | Produces a different value on replay; everything derived from it diverges |
| **RG002** | Network or filesystem access outside a step | Diverges, and repeats the side effect on every replay |
| **RG003** | A step body writing to state it doesn't own | The write lands on the first run and is skipped on replay, so the outer state silently reverts |
| **RG004** | Control flow depending on a nondeterministic value | Replay can take the other branch, so the operation sequence no longer matches the journal |
| **RG005** | A step name built from an unstable source | Checkpoints match by name and order; a changed name can't be matched, so the step re-executes |
| **RG900** | Code whose region couldn't be resolved | Not a violation. A coverage gap, reported so a clean run means something |

`replayguard rules --explain` prints the rationale for each.

## Language support

Python, TypeScript/JavaScript, and Java are the three runtimes with an
official AWS durable execution SDK. Rust has no official SDK; the frontend
targets [pgdad/durable-rust](https://github.com/pgdad/durable-rust), whose own
documentation says its determinism rules are documented rather than enforced.
Go and .NET have community proofs of concept only and are out of scope for
now.

| Runtime | Parser |
|---------|--------|
| Python | stdlib `ast` |
| TypeScript / JavaScript | tree-sitter |
| Java | tree-sitter |
| Rust | tree-sitter |

The SDKs differ in shape, not just syntax:

| | Handler | Step |
|---|---|---|
| Python | `@durable_execution` | `context.step(fn, name="x")` |
| JS/TS | `withDurableExecution(fn)` | `context.step("x", fn)` |
| Java | `extends DurableHandler<,>` | `ctx.step("x", Result.class, fn)` |
| Rust | param typed `*Context` | `ctx.step("x", \|\| async { .. })` |

The step body sits in a different argument position in each, and Java has a
two-argument overload besides, so bodies are located by kind rather than by
position. Everything lowers to one shared IR and the rules are written once,
with no knowledge of which language they're inspecting.

The semantic differences that matter are handled per-frontend, and RG003 is
where they show up:

- **Python**: a bare `x = 1` in a nested function creates a local binding, so
  it can never be an outer write. Only mutation and `global`/`nonlocal` reach
  out.
- **JavaScript**: the same assignment writes straight through to the enclosing
  scope, so RG003 has more ways to fire.
- **Java**: captured locals must be effectively final, so reassigning one is a
  compile error and that violation class can't exist. What remains is
  collection mutation and field writes.
- **Rust**: the narrowest of the four. Step closures are `Send + 'static`, so
  capturing a borrowed reference doesn't compile. What's left is interior
  mutability through a shared handle, like an `Arc<Mutex<_>>` locked and
  pushed to, or a `static mut`.

## Design

```
source ──▶ frontend ──▶ IR ──▶ rules ──▶ findings ──▶ reporter
           (per-lang)  (shared) (shared)              text/json/sarif
```

This is AST analysis, not pattern matching, because the questions need scope
resolution: RG003 has to know whether a mutated name belongs to the step body
or an enclosing scope, and RG004 has to know whether a branch condition
derives from a nondeterministic source. Neither can be answered by matching
source text.

False positives get particular attention, since a linter that fires on
correct code gets uninstalled. RG005 ignores computed step names unless they
interpolate something genuinely unstable (`` `item-${index}` `` is the pattern
AWS recommends), and RG900 reports unresolved regions instead of silently
passing them.

## What it doesn't do

Calls are not followed across file boundaries. Within a file they are, and
findings name the route, but a handler that calls into another module for its
I/O will pass clean. This is the largest known blind spot.

Static analysis also can't see nondeterminism inside a third-party library,
data tainted several hops back, iteration order over an unordered collection,
or concurrent completion order. Some of that the dynamic half can catch.

## Dynamic replay-divergence

The static rules reason about what code might do. The dynamic harness runs
the handler twice, once normally and once with the clock moved and entropy
reseeded, and diffs the operation journals:

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
build instead of surfacing on a resume months later:

```python
from replayguard.dynamic import assert_deterministic

def test_handler_is_deterministic():
    assert_deterministic(handler, {"orderId": "A1"})
```

The harness needs no rule for the source of nondeterminism. A clock inside a
library, an iteration order, a value tainted many hops back: it measures the
effect, not the cause. On 51 AWS conformance handlers it produced zero false
alarms; on a handler whose step order comes from `random.sample`, which no
static rule covers, it diverges.

It can't prove determinism, only fail to disprove it, and the report says so.
Handlers that suspend on a callback can't be checked locally.

## Validation

[VALIDATION.md](VALIDATION.md) records what has been tested against whose
code: which rules have confirmed real-world findings, which are still
unproven, the false positives that were found and fixed, and the bugs the
validation found in the tool itself. Read it before relying on a clean run.

## Developing

```bash
pip install -e ".[dev]"
python scripts/verify.py
```

`verify.py` runs five gates: import, lint, tests with a coverage floor, the
CLI's exit codes and output formats, and a canary asserting the known-good
fixtures produce zero findings in every language. The canary matters most; a
false positive is a worse failure here than a missed bug.

## Prior art

[Temporal's workflowcheck](https://github.com/temporalio/sdk-go) does this for
Temporal workflows, so the category is proven; it just didn't exist for AWS's
primitive. [durable-viz](https://github.com/gunnargrosch/durable-viz)
statically analyses durable handlers to draw flowcharts, but performs no
validation.

## License

MIT
