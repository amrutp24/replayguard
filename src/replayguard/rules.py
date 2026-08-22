"""Determinism rules.

Rules read the IR and emit findings. They are language-agnostic by construction
- if a rule needs to know which language it's looking at, that's a signal the
frontend should have normalized something it didn't.
"""

from __future__ import annotations

from collections.abc import Iterator

from . import catalog
from .findings import Confidence, Finding, Severity
from .ir import Handler, OuterWrite, Region


def _through(via: tuple[str, ...]) -> str:
    """Render the helper path that leads to a finding.

    A violation inside a helper is invisible at the call site, so naming the
    route is the difference between an actionable finding and a confusing one.
    """
    if not via:
        return ""
    return " Reached from the handler through " + " -> ".join(f"{n}()" for n in via) + "."

#: Registry populated by @rule. Order here is presentation order.
_RULES: list = []


def rule(fn):
    _RULES.append(fn)
    return fn


@rule
def rg001_nondeterministic_call(h: Handler) -> Iterator[Finding]:
    """RG001 - a clock, random, or identity source outside a step.

    The canonical violation. On replay the handler re-runs from the top, this
    call produces a different value, and everything derived from it diverges.
    """
    for call in h.calls:
        if call.region is not Region.DURABLE:
            continue
        cat = catalog.lookup(h.language, call.dotted)
        if cat is None or cat in (
            catalog.Category.NETWORK,
            catalog.Category.FILESYSTEM,
        ):
            continue  # handled by RG002
        yield Finding(
            rule="RG001",
            message=f"`{call.shown}` runs outside a durable step",
            loc=call.loc,
            severity=Severity.ERROR,
            confidence=Confidence.HIGH,
            rationale=f"On replay, {cat.why}." + _through(call.via),
            fix=f"Move `{call.shown}` inside a `step()` and use the returned "
            "value, so the result is checkpointed once and reused on replay.",
        )


@rule
def rg002_external_io(h: Handler) -> Iterator[Finding]:
    """RG002 - network or filesystem access outside a step.

    Worse than RG001: as well as diverging, it re-executes the side effect on
    every replay. A payment call here charges the customer again.
    """
    for call in h.calls:
        if call.region is not Region.DURABLE:
            continue
        cat = catalog.lookup(h.language, call.dotted)
        is_sdk = catalog.is_aws_sdk_call(h.language, call.dotted) or call.external_client
        if cat not in (catalog.Category.NETWORK, catalog.Category.FILESYSTEM) and not is_sdk:
            continue
        why = cat.why if cat else "external calls re-execute on every replay"
        yield Finding(
            rule="RG002",
            message=f"external I/O `{call.shown}` runs outside a durable step",
            loc=call.loc,
            severity=Severity.ERROR,
            confidence=Confidence.HIGH if cat else Confidence.MEDIUM,
            rationale=f"On replay, {why}. Side effects are repeated, not "
            "resumed." + _through(call.via),
            fix="Wrap the call in a `step()` so it executes once and the result "
            "is checkpointed.",
        )


def _is_read_elsewhere(h: Handler, w: OuterWrite) -> bool:
    """Is the written value ever consumed somewhere the write may not have run?

    Step bodies do not re-run on replay, so a write inside one is lost. That only
    *matters* if something reads the value later. Three cases:

      * read in the durable region  -> stale value reaches the handler. Lost.
      * read in a *different* step body -> that body did not re-run either, so
        the value it sees is still stale. Lost.
      * read only inside the same step body -> everything that consumes it ran
        in the same execution as the write. Harmless.

    The third case is the write-only observability instrument -- a log, a
    counter, a span collector -- which is common and correct, and which this
    rule reported as a bug until the check existed.
    """
    for r in h.reads:
        if r.name != w.target:
            continue
        if r.region is not Region.STEP_BODY:
            return True
        if r.step_id != w.step_id:
            return True
    return False


@rule
def rg003_outer_write_in_step(h: Handler) -> Iterator[Finding]:
    """RG003 - a step body writing to state it does not own.

    The silent one, and the reason this checker is worth building. AWS's own
    docs describe it precisely: the first invocation looks correct because the
    body runs and the write lands; replay returns the cached result and skips
    the body, so the outer state stays at its initial value.

    No error is raised. Nothing fails. The value is just wrong, months later.
    """
    for w in h.outer_writes:
        if w.region is not Region.STEP_BODY:
            continue
        if not _is_read_elsewhere(h, w):
            continue
        scope = "module-level state" if w.is_global else "a captured variable"
        yield Finding(
            rule="RG003",
            message=f"step body writes to {scope} `{w.target}`",
            loc=w.loc,
            severity=Severity.ERROR,
            confidence=Confidence.HIGH,
            rationale="Step bodies do not re-run on replay: the cached result "
            "is returned and the body is skipped, so this write is silently "
            "lost. The first run looks correct, which is what makes it "
            "dangerous." + _through(w.via),
            fix=f"Return the value from the step, then apply it to "
            f"`{w.target}` outside the step body. Work done outside is "
            "rebuilt from the checkpointed result on replay; work done "
            "inside is not.",
        )


@rule
def rg004_nondeterministic_branch(h: Handler) -> Iterator[Finding]:
    """RG004 - control flow outside a step depending on a nondeterministic value.

    Distinct from RG001 because the damage is structural: a different branch on
    replay means a different sequence of steps, so checkpoints no longer line up
    with the operations requesting them.
    """
    for br in h.branches:
        if br.region is not Region.DURABLE:
            continue
        hits = [
            s for s in br.condition_symbols if catalog.lookup(h.language, s) is not None
        ]
        if not hits:
            continue
        yield Finding(
            rule="RG004",
            message=f"branch condition depends on `{hits[0]}`",
            loc=br.loc,
            severity=Severity.ERROR,
            confidence=Confidence.MEDIUM,
            rationale="Replay may take the other branch, producing a different "
            "sequence of steps than the one that was checkpointed."
            + _through(br.via),
            fix="Checkpoint the decision itself - compute it inside a `step()` "
            "and branch on the returned value.",
        )


@rule
def rg005_dynamic_step_name(h: Handler) -> Iterator[Finding]:
    """RG005 - a step name that isn't a stable literal.

    Checkpoints are matched by name and order. A name containing a timestamp or
    a uuid cannot be matched on resume, so the step re-executes.
    """
    for step in h.steps:
        if step.name_is_static:
            continue
        # A computed name is only a problem when it interpolates something
        # unstable. `f"item-{index}"` over a loop is the pattern AWS itself
        # recommends for distinguishing iterations, so flagging every computed
        # name would bury the real bug in noise.
        hits = [
            s for s in step.name_symbols if catalog.lookup(h.language, s) is not None
        ]
        if not hits:
            continue
        yield Finding(
            rule="RG005",
            message=f"`{step.kind}` name is built from `{hits[0]}`",
            loc=step.loc,
            severity=Severity.ERROR,
            confidence=Confidence.HIGH,
            rationale="Checkpoints are matched by name and order. A name that "
            "differs between the original run and the replay will not match, "
            "and the operation re-executes.",
            fix="Use a stable literal name. To distinguish loop iterations, "
            "derive the suffix from checkpointed data (an index or a step "
            "result), never from a clock or a random source.",
        )


@rule
def rg900_unresolved_region(h: Handler) -> Iterator[Finding]:
    """RG900 - the frontend could not determine which side of the boundary code is on.

    Emitted as a NOTE so coverage is honest. A checker that silently skips what
    it can't parse implies a clean bill of health it hasn't earned.
    """
    for loc in h.unresolved:
        yield Finding(
            rule="RG900",
            message="could not resolve whether this code runs inside a step",
            loc=loc,
            severity=Severity.NOTE,
            confidence=Confidence.HIGH,
            rationale="Usually a step body passed as a named reference rather "
            "than defined inline. Determinism here is unchecked, not verified.",
            fix="Define the step body inline as a lambda or nested function so "
            "it can be analysed, or review this call by hand.",
        )


def check(handler: Handler) -> list[Finding]:
    out: list[Finding] = []
    for fn in _RULES:
        out.extend(fn(handler))
    return sorted(out, key=lambda f: f.sort_key())


def all_rule_ids() -> list[str]:
    return [fn.__name__.split("_")[0].upper() for fn in _RULES]
