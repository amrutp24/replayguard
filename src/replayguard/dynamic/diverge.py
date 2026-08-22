"""Replay-divergence checking.

Run a durable handler twice -- once as a control, once in a perturbed world --
and compare the operation journals. If the shape of the execution moved when the
clock and the entropy moved, the handler is not a pure function of its inputs
and checkpointed results, and replay cannot line it up with its checkpoints.

Why this catches what static analysis cannot: it never has to *recognise* the
source of nondeterminism. A clock read inside a third-party library, an
iteration order over a set, a locale-dependent format, a value tainted five
hops back -- none of it needs a rule. Only the effect is measured.

What it cannot do is prove determinism. A handler that shows no divergence under
this perturbation may still diverge under another. Absence of divergence is
evidence, not proof, and the report says so.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .journal import Journal, from_result
from .perturb import baseline, perturbed

#: A handler that suspends on `wait_for_callback` blocks forever under the local
#: runner, because no callback is ever delivered. Without a deadline the harness
#: hangs on exactly the handlers people most want to check.
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass
class Divergence:
    """One difference between the control journal and the perturbed one."""

    index: int
    control: str | None
    perturbed: str | None
    reason: str

    def render(self) -> str:
        return (
            f"  operation {self.index}: {self.reason}\n"
            f"    control   : {self.control or '(none)'}\n"
            f"    perturbed : {self.perturbed or '(none)'}"
        )


@dataclass
class Report:
    control: Journal
    perturbed_run: Journal
    divergences: list[Divergence] = field(default_factory=list)
    #: Set when the harness itself could not complete a run.
    harness_error: str | None = None

    @property
    def diverged(self) -> bool:
        return bool(self.divergences)

    def render(self) -> str:
        if self.harness_error:
            return f"replay-divergence: could not run -- {self.harness_error}"
        if not self.divergences:
            return (
                f"replay-divergence: no divergence across "
                f"{len(self.control)} operation(s).\n"
                "This is evidence of determinism, not proof: a handler may still "
                "diverge under a perturbation this run did not apply."
            )
        lines = [
            f"replay-divergence: {len(self.divergences)} divergence(s) found.",
            "",
            "The operation sequence changed when the clock and entropy changed, so",
            "this handler is not a pure function of its inputs and checkpointed",
            "results. On resume the journal will not line up with its checkpoints.",
            "",
        ]
        lines.extend(d.render() for d in self.divergences)
        return "\n".join(lines)


def _compare(control: Journal, other: Journal) -> list[Divergence]:
    out: list[Divergence] = []

    # A run that fails on one pass and not the other is a divergence in itself,
    # and usually a more serious one than a reordered operation.
    if bool(control.error) != bool(other.error):
        out.append(
            Divergence(
                index=-1,
                control=control.error or "completed",
                perturbed=other.error or "completed",
                reason="one run raised and the other did not",
            )
        )

    for i in range(max(len(control), len(other))):
        a = control.entries[i] if i < len(control) else None
        b = other.entries[i] if i < len(other) else None
        if a is None:
            out.append(
                Divergence(i, None, b.render().strip(), "extra operation under perturbation")
            )
            continue
        if b is None:
            out.append(
                Divergence(i, a.render().strip(), None, "operation missing under perturbation")
            )
            continue
        if (a.kind, a.name, a.depth) == (b.kind, b.name, b.depth):
            continue
        if a.name != b.name and a.kind == b.kind:
            reason = "operation name changed -- checkpoints match by name"
        elif a.kind != b.kind:
            reason = "different operation kind -- control flow diverged"
        else:
            reason = "nesting changed -- child-context structure diverged"
        out.append(Divergence(i, a.render().strip(), b.render().strip(), reason))
    return out


def check_handler(
    handler: Callable,
    event: Any = None,
    *,
    runner_factory: Callable | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Report:
    """Run `handler` twice and report journal divergence.

    `runner_factory` exists so the caller can supply a configured runner; by
    default the local test runner from the AWS testing SDK is used, which needs
    no AWS account.
    """
    if runner_factory is None:
        try:
            from aws_durable_execution_sdk_python_testing import (
                DurableFunctionTestRunner,
            )
        except ImportError:
            return Report(
                Journal(),
                Journal(),
                harness_error=(
                    "the AWS durable execution testing SDK is required: "
                    "pip install 'replayguard[dynamic]'"
                ),
            )

        def runner_factory():  # type: ignore[misc]
            return DurableFunctionTestRunner(handler)

    def run_once(world) -> Journal:
        runner = runner_factory()
        try:
            with world:
                return from_result(runner.run(event))
        finally:
            close = getattr(runner, "close", None)
            if callable(close):
                close()

    def run_with_deadline(world) -> Journal:
        """Run under a deadline, on a thread the process can abandon.

        A suspended handler is not a failure of the handler; it just cannot be
        checked this way, so it is reported as a harness limitation rather than
        as a divergence -- which would be a false accusation.

        The thread must be a daemon and must not be joined after the deadline.
        A ThreadPoolExecutor waits for its workers on exit, so it hangs on
        precisely the handlers this deadline exists to escape.
        """
        box: dict[str, object] = {}

        def target() -> None:
            try:
                box["journal"] = run_once(world)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["journal"]  # type: ignore[return-value]

    try:
        control = run_with_deadline(baseline())
        other = run_with_deadline(perturbed())
    except TimeoutError:
        return Report(
            Journal(),
            Journal(),
            harness_error=(
                f"handler did not complete within {timeout:g}s -- it most likely "
                "suspends on a callback, which the local runner never delivers"
            ),
        )
    except Exception as exc:  # the handler or the runner blew up
        return Report(Journal(), Journal(), harness_error=f"{type(exc).__name__}: {exc}")

    return Report(control, other, _compare(control, other))


def assert_deterministic(handler: Callable, event: Any = None) -> None:
    """pytest-friendly assertion.

    The intended use is a test beside the handler, so a determinism regression
    fails the build rather than surfacing on a resume months later.
    """
    report = check_handler(handler, event)
    if report.harness_error:
        raise RuntimeError(report.harness_error)
    if report.diverged:
        raise AssertionError(report.render())
