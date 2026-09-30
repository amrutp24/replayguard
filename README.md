# replayguard

Replay safety for AWS Lambda durable functions.

[![PyPI](https://img.shields.io/pypi/v/replayguard)](https://pypi.org/project/replayguard/)
[![Python](https://img.shields.io/pypi/pyversions/replayguard)](https://pypi.org/project/replayguard/)
[![CI](https://github.com/amrutp24/replayguard/actions/workflows/ci.yml/badge.svg)](https://github.com/amrutp24/replayguard/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/replayguard)](LICENSE)

## What is it?

A durable function is re-run from the top every time it resumes. Steps that
already completed return their checkpointed result instead of executing again,
and the handler fast-forwards to wherever it left off. That only works if two
things hold: the handler takes the same path every time, and the code that
resumes is the code that suspended.

Nothing enforces either one. Break the first and nothing throws — your tests
stay green and the bug shows up on the next resume, which can be up to 366 days
later, in production. Break the second and the execution can finish with status
`SUCCEEDED` having run a side effect twice or skipped one entirely.

replayguard checks both halves: it lints your handler for determinism in four
languages, replays it under a shifted clock to catch what a linter can't, and
tells you whether an SDK upgrade or a deploy can reach an execution that is
already suspended.

## Table of contents

- [Main features](#main-features)
- [Where to get it](#where-to-get-it)
- [Quick start](#quick-start)
- [In CI](#in-ci)
- [Rules](#rules)
- [Drift: the other half of replay](#drift-the-other-half-of-replay)
- [Language support](#language-support)
- [What it doesn't do](#what-it-doesnt-do)
- [Validation](#validation)
- [Contributing](#contributing)
- [Developing](#developing)
- [Prior art](#prior-art)
- [License](#license)

## Main features

- **Static determinism check** across Python, TypeScript/JavaScript, Java and
  Rust — five rules for clocks, I/O, captured state, nondeterministic control
  flow and unstable step names, written once against a shared IR
- **Replay-divergence harness** that runs a handler twice under different
  clocks and entropy and diffs what it did, catching nondeterminism no static
  rule covers
- **SDK drift analysis** that compares two versions of the durable-execution
  SDK and reports which changes can reach an execution that is already
  suspended — offline, stdlib-only, made for CI
- **A live probe** that suspends a real execution, changes something underneath
  it on purpose, and records what the platform actually did
- **SARIF output**, a GitHub Action and a pre-commit hook, so findings land on
  the pull request that introduced them
- **Honest coverage**: code whose region can't be resolved is reported as a
  gap, not passed silently, so a clean run means something

## Where to get it

```bash
pip install replayguard
```

TypeScript, Java and Rust need a parser; the live probe needs boto3:

```bash
pip install 'replayguard[all]'
```

Python 3.11 or later. The static checker and drift analysis have no
dependencies of their own.

## Quick start

```bash
replayguard check src/                 # is this handler deterministic?
replayguard replay app.orders:handler  # does it diverge when replayed?
replayguard drift 1.7.0 2.0.0          # can this SDK upgrade reach in-flight work?
replayguard rules --explain            # what each rule catches, and why
```

`check` exits non-zero when anything at or above `--fail-on` (default `error`)
is found, so it gates a build without extra wiring. Add `--explain` for the
rationale and the fix, or `--format sarif` for CI.

```
tests/fixtures/python/bad_handler.py
    25:17  error   RG001  `time.time` runs outside a durable step
    37:14  error   RG002  external I/O `requests.get` runs outside a durable step
    53:8   error   RG003  step body writes to a captured variable `receipts`
    43:7   error   RG004  branch condition depends on `datetime.datetime.now`
    68:4   error   RG005  `step` name is built from `time.time`
    71:17  note    RG900  could not resolve whether this code runs inside a step
```

`replay` runs the handler once normally and once with the clock moved and
entropy reseeded, then diffs the operation journals. It needs no rule for the
source of the nondeterminism — a clock inside a library, an iteration order, a
value tainted several hops back — because it measures the effect, not the
cause:

```
replay-divergence: 1 divergence(s) found.

  operation 0: operation name changed -- the name depends on something nondeterministic
    control   : step(op-1787442395)
    perturbed : step(op-1787489626)
```

The same check works as a test, so a determinism regression fails the build
rather than surfacing on a resume months later:

```python
from replayguard.dynamic import assert_deterministic

def test_handler_is_deterministic():
    assert_deterministic(handler, {"orderId": "A1"})
```

It can't prove determinism, only fail to disprove it, and the report says so.
Handlers that suspend on a callback can't be replayed locally.

## In CI

Findings render inline on the pull request:

```yaml
- uses: amrutp24/replayguard@v0.1.1
  with:
    path: src/
```

The job needs `permissions: security-events: write` for the annotations. Set
`fail-on: never` to annotate without blocking the merge.

As a pre-commit hook:

```yaml
- repo: https://github.com/amrutp24/replayguard
  rev: v0.1.1
  hooks:
    - id: replayguard
```

To fail a build that would upgrade the SDK across a risky boundary:

```bash
replayguard drift $CURRENT $TARGET --fail-on-inflight
```

## Rules

| ID        | What it catches                                    | Why it matters                                                                                       |
| --------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **RG001** | Clock, random, or identity source outside a step   | Produces a different value on replay; everything derived from it diverges                            |
| **RG002** | Network or filesystem access outside a step        | Diverges, and repeats the side effect on every replay                                                |
| **RG003** | A step body writing to state it doesn't own        | The write lands on the first run and is skipped on replay, so the outer state silently reverts       |
| **RG004** | Control flow depending on a nondeterministic value | Replay can take the other branch, so the operation sequence no longer matches the journal            |
| **RG005** | A step name built from an unstable source          | The name differs on every run; AWS treats step names as stable identifiers, and a handler can't rely on how a given SDK resolves a mismatch |
| **RG900** | Code whose region couldn't be resolved             | Not a violation. A coverage gap, reported so a clean run means something                             |

## Drift: the other half of replay

A determinism check answers "will this code take the same path twice?" It
cannot answer "is the code that wakes up the code that went to sleep?" — and a
handler with no clocks, no randomness and no I/O outside a step, one that
`replayguard check` passes with zero findings, can still be corrupted by a
deploy that lands while it is suspended.

That is measured, not supposed. Twelve scenarios against a live account, each
breaking a replay assumption on purpose:

| Changed while suspended                | What happened                                     |
| -------------------------------------- | ------------------------------------------------- |
| Rename a step                          | safe — the name was never the identity            |
| Edit code outside all steps            | safe                                              |
| Insert a step before a completed one   | inserted step never ran; a second wait was created |
| Delete a completed step                | the step *after* it never ran                     |
| **Reorder two steps**                  | **one side effect ran twice, another never ran**  |
| Upgrade or roll back the SDK           | safe, both directions                             |

Three of twelve corrupted the execution. All three reported `SUCCEEDED`. None
produced a clean failure. Checkpoints turned out to be matched by position, not
by name — which inverts the usual advice — and every corruption required the
execution to be running on `$LATEST`. Pin to a version or an alias and none of
it is reachable.

```bash
replayguard drift 1.7.0 2.0.0   # compare two SDK versions, offline
replayguard drift --sweep       # every adjacent release pair
replayguard probe --list        # the live scenarios
replayguard probe               # run them against your own account
```

`drift` is stdlib-only and safe to run anywhere. `probe` needs
`pip install 'replayguard[live]'`, creates billable AWS resources — all prefixed
`rd-` — and tears them down afterwards, verifying by direct lookup rather than
absence from a list. Expected spend is well under $0.10.

The full record is in [drift-study/](drift-study/): the findings, the mechanism
and how to falsify it, and the raw run output the findings are generated from.

## Language support

Python, TypeScript/JavaScript and Java are the runtimes with an official AWS
durable execution SDK. Rust has no official SDK; the frontend targets
[pgdad/durable-rust](https://github.com/pgdad/durable-rust). Go and .NET are
out of scope for now.

| Runtime                 | Parser       | Handler                     | Step                              |
| ----------------------- | ------------ | --------------------------- | --------------------------------- |
| Python                  | stdlib `ast` | `@durable_execution`        | `context.step(fn, name="x")`      |
| TypeScript / JavaScript | tree-sitter  | `withDurableExecution(fn)`  | `context.step("x", fn)`           |
| Java                    | tree-sitter  | `extends DurableHandler<,>` | `ctx.step("x", Result.class, fn)` |
| Rust                    | tree-sitter  | param typed `*Context`      | `ctx.step("x", \|\| async { .. })` |

Everything lowers to one shared IR, and the rules are written once with no
knowledge of which language they're inspecting. The one place language
semantics matter is RG003, a step body writing to state it doesn't own:

- **Python** — a bare `x = 1` in a nested function creates a local binding, so
  only mutation and `global`/`nonlocal` reach out
- **JavaScript** — the same assignment writes straight through to the enclosing
  scope, so there are more ways to fire
- **Java** — captured locals must be effectively final, so what remains is
  collection mutation and field writes
- **Rust** — step closures are `Send + 'static`, so what's left is interior
  mutability through a shared handle, like an `Arc<Mutex<_>>`

```
source ──▶ frontend ──▶ IR ──▶ rules ──▶ findings ──▶ reporter
           (per-lang)  (shared) (shared)              text/json/sarif
```

This is AST analysis with scope resolution, not pattern matching. RG003 has to
know whether a mutated name belongs to the step body or an enclosing scope;
RG004 has to know whether a branch condition derives from a nondeterministic
source. Neither can be answered by matching text.

## What it doesn't do

- **Calls are not followed across files.** Within a file they are, and findings
  name the route, but a handler that reaches another module for its I/O passes
  clean. This is the largest known blind spot.
- Static analysis can't see nondeterminism inside a third-party library, data
  tainted several hops back, iteration order over an unordered collection, or
  concurrent completion order. The replay harness catches some of that.
- The drift analysis compares source surfaces. It flags risk, not breakage: a
  finding means "look here", and the live probe is what establishes whether it
  breaks anything. Our own headline upgrade, 1.7.0 to 2.0.0, was flagged hardest
  and did no damage when run live.
- The live probe measures one runtime, one SDK version, one execution per
  scenario, with 90-second suspends. It is evidence, not a statistical
  characterisation.

## Validation

Validated against 1,547 files of durable-function code written by other people.
[VALIDATION.md](VALIDATION.md) records what that established and what it
didn't: which rules have confirmed real-world findings, which have working
detectors but no confirmed finding yet, the false positives that were found and
fixed, and the bugs the validation found in the tool itself. Read it before
relying on a clean run.

## Contributing

Reports from real codebases are the most useful thing anyone can contribute, in
either direction: a finding it caught, or a false positive it shouldn't have
raised. Three of the six rules have working detectors and no confirmed
real-world finding yet, because published example code doesn't contain the
mistakes they catch — if your code does, please
[open an issue](https://github.com/amrutp24/replayguard/issues).

Results from the live probe on other runtimes or SDK versions are equally
welcome. The JavaScript, Java and .NET SDKs have their own replay engines and
may not behave the way the Python one did.

## Developing

```bash
pip install -e ".[dev]"
python scripts/verify.py
```

`verify.py` runs five gates: import, lint, tests with a coverage floor, the
CLI's exit codes and output formats, and a canary asserting the known-good
fixtures produce zero findings in every language. CI runs the same script on
Python 3.11 and 3.13, then runs the checker over its own fixtures and exercises
the GitHub Action as a consumer would.

## Prior art

[Temporal's workflowcheck](https://github.com/temporalio/sdk-go) does the
static half for Temporal workflows, so the category is proven.
[durable-viz](https://github.com/gunnargrosch/durable-viz) analyses durable
handlers to draw flowcharts but performs no validation. AWS's own
[testing SDK](https://github.com/aws/aws-durable-execution-sdk-js) replays your
handler against one SDK version; the drift analysis here compares one SDK
version against another, which is a different axis. Azure publishes a
[formal versioning guide](https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-versioning)
for the same problem; AWS has no equivalent document.

## License

MIT
