"""Replay-divergence harness tests.

The harness runs a handler twice under different worlds and diffs the operation
journals. These pin both directions: it must catch nondeterminism a static rule
has no entry for, and it must stay silent on correct handlers.
"""

import logging

import pytest

pytest.importorskip("aws_durable_execution_sdk_python")
pytest.importorskip("aws_durable_execution_sdk_python_testing")

import datetime  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402

from aws_durable_execution_sdk_python import (  # noqa: E402
    DurableContext,
    durable_execution,
)

from replayguard.dynamic import Journal, check_handler  # noqa: E402
from replayguard.dynamic.journal import Entry  # noqa: E402
from replayguard.dynamic.perturb import baseline, perturbed  # noqa: E402

logging.disable(logging.CRITICAL)


# -- journal model -----------------------------------------------------------


def test_signature_ignores_nothing_that_replay_matches_on():
    j = Journal([Entry("step", "a"), Entry("wait", None, depth=1)])
    assert j.signature() == (("step", "a", 0), ("wait", None, 1))


def test_empty_journal_renders_readably():
    assert "no operations" in Journal().render()


# -- perturbation ------------------------------------------------------------


def test_perturbation_moves_the_clock_and_restores_it():
    before = time.time()
    with perturbed():
        during = time.time()
    after = time.time()
    assert during > before + 3600, "clock must move far enough to cross boundaries"
    assert abs(after - before) < 5, "the real clock must be restored"


def test_perturbation_shifts_datetime_and_returns_a_real_datetime():
    """The shim must not leak its own type into handler code.

    `datetime.datetime` is the shim *inside* the block, but what handlers
    receive has to be an ordinary datetime, or date arithmetic and serialisation
    start behaving differently under perturbation for reasons unrelated to the
    handler.
    """
    real_cls = datetime.datetime
    before = real_cls.now()
    with perturbed():
        during = datetime.datetime.now()
    assert isinstance(during, real_cls)
    assert (during - before).total_seconds() > 3600

    # Restored, and calling it again must not recurse through the shim.
    assert isinstance(datetime.datetime.now(), real_cls)


def test_baseline_seeds_randomness_so_correct_handlers_are_stable():
    with baseline():
        first = [random.random() for _ in range(3)]
    with baseline():
        second = [random.random() for _ in range(3)]
    assert first == second, "the control run must be reproducible"


# -- divergence detection ----------------------------------------------------


@durable_execution
def _deterministic(event, context: DurableContext):
    a = context.step(lambda _: "a", name="one")
    b = context.step(lambda _: "b", name="two")
    return {"a": a, "b": b}


@durable_execution
def _clock_named_step(event, context: DurableContext):
    return context.step(lambda _: 1, name=f"op-{int(time.time())}")


@durable_execution
def _clock_branch(event, context: DurableContext):
    # Parity, not `< 12`. The perturbation shifts the clock by an odd number of
    # hours, so parity always flips; a noon boundary is only crossed for some
    # starting times, which made this test pass or fail depending on when it ran.
    if datetime.datetime.now().hour % 2 == 0:
        return context.step(lambda _: "even", name="even-hour")
    return context.step(lambda _: "odd", name="odd-hour")


@durable_execution
def _opaque_ordering(event, context: DurableContext):
    for pick in random.sample(["a", "b", "c", "d"], 2):
        context.step(lambda _, p=pick: p, name=f"handle-{pick}")
    return "done"


def test_deterministic_handler_shows_no_divergence():
    report = check_handler(_deterministic, {})
    assert not report.harness_error, report.harness_error
    assert not report.diverged, report.render()


def test_clock_derived_operation_name_diverges():
    report = check_handler(_clock_named_step, {})
    assert report.diverged, report.render()
    assert "name changed" in report.divergences[0].reason


def test_clock_branch_changes_the_operation_sequence():
    report = check_handler(_clock_branch, {})
    assert report.diverged, report.render()


def test_catches_nondeterminism_no_static_rule_covers():
    """Iteration order driven by `random.sample`.

    No catalog entry exists for this and none could reasonably be written. The
    dynamic harness needs no rule -- it measures the effect, not the cause,
    which is the whole reason it exists alongside the static rules.
    """
    report = check_handler(_opaque_ordering, {})
    assert report.diverged, report.render()


# -- honesty about limits ----------------------------------------------------


def test_clean_report_does_not_claim_proof():
    report = check_handler(_deterministic, {})
    assert "not proof" in report.render()


def test_suspended_handler_is_a_harness_limit_not_a_divergence():
    """A handler awaiting a callback cannot be checked locally.

    Reporting it as divergence would be a false accusation, and hanging would be
    worse -- callback handlers are exactly the long-lived ones people most want
    to check.
    """
    from aws_durable_execution_sdk_python.config import (
        Duration,
        WaitForCallbackConfig,
    )

    @durable_execution
    def suspends(event, context: DurableContext):
        return context.wait_for_callback(
            lambda cb_id, _c: None,
            name="cb",
            config=WaitForCallbackConfig(timeout=Duration.from_seconds(600)),
        )

    report = check_handler(suspends, {}, timeout=3)
    assert report.harness_error
    assert not report.diverged
    assert "suspends" in report.harness_error


def test_handler_that_never_runs_is_not_reported_as_clean():
    """A handler that raises immediately gets no verdict at all.

    Both runs fail the same way, so nothing diverges -- and the honest reading
    of that is not "deterministic", it is "nothing was compared". Reported as a
    clean run it would hand a green CI check to a handler that never executed
    an operation, which is the single worst thing this tool could do.
    """

    @durable_execution
    def explodes(event, context: DurableContext):
        return context.step(lambda _: 1, name=f"op-{event['missing']}")

    report = check_handler(explodes, {})
    assert report.harness_error, report.render()
    assert not report.diverged
    assert "nothing was compared" in report.harness_error


def test_handler_failing_after_operations_still_gets_a_verdict():
    """Only the vacuous case is refused.

    A durable execution can legitimately end FAILED -- a validation path, a step
    that gives up. Its operations up to that point are still worth comparing, so
    the run gets a real answer, with the failure stated rather than hidden
    behind "no divergence".
    """

    @durable_execution
    def fails_late(event, context: DurableContext):
        context.step(lambda _: "ok", name="first")
        raise ValueError("rejected")

    report = check_handler(fails_late, {})
    assert not report.harness_error, report.harness_error
    assert not report.diverged, report.render()
    assert "Both runs failed identically" in report.render()
    assert "rejected" in report.render()


# -- divergence classification ------------------------------------------------
# _compare is pure over two journals, so every kind of divergence the report can
# name is pinned here directly -- no handler needed. These reasons are what a
# user reads when their build goes red; each must attach to the right shape.

from replayguard.dynamic.diverge import _compare  # noqa: E402


def test_extra_operation_is_named_as_such():
    control = Journal([Entry("step", "a")])
    other = Journal([Entry("step", "a"), Entry("step", "b")])
    (d,) = _compare(control, other)
    assert d.reason == "extra operation under perturbation"
    assert d.control is None


def test_missing_operation_is_named_as_such():
    control = Journal([Entry("step", "a"), Entry("step", "b")])
    other = Journal([Entry("step", "a")])
    (d,) = _compare(control, other)
    assert d.reason == "operation missing under perturbation"
    assert d.perturbed is None


def test_changed_kind_is_control_flow_divergence():
    control = Journal([Entry("step", "a")])
    other = Journal([Entry("wait", "a")])
    (d,) = _compare(control, other)
    assert "kind" in d.reason


def test_changed_nesting_is_structure_divergence():
    control = Journal([Entry("step", "a", depth=0)])
    other = Journal([Entry("step", "a", depth=1)])
    (d,) = _compare(control, other)
    assert "nesting" in d.reason


def test_one_run_raising_is_itself_a_divergence():
    """Same operations, but only the perturbed world failed.

    The journals match entry for entry, so without this check the run would
    read as clean -- when in fact the perturbation changed the outcome, which
    is the strongest divergence there is.
    """
    entries = [Entry("step", "a")]
    control = Journal(list(entries))
    other = Journal(list(entries), error="boom")
    divs = _compare(control, other)
    assert any("one run raised" in d.reason for d in divs)


def test_identical_failures_alone_are_not_a_divergence():
    """Both worlds failing the same way is deterministic, not divergent."""
    control = Journal([Entry("step", "a")], error="boom")
    other = Journal([Entry("step", "a")], error="boom")
    assert _compare(control, other) == []


# -- the pytest-facing API ----------------------------------------------------


def test_assert_deterministic_raises_on_divergence():
    """This is the function users put in their own test suites.

    It must raise AssertionError (so pytest reports a failure, not an error)
    and carry the rendered report, because the traceback is all the user sees.
    """
    from replayguard.dynamic import assert_deterministic

    with pytest.raises(AssertionError, match="name changed"):
        assert_deterministic(_clock_named_step, {})


def test_assert_deterministic_passes_a_clean_handler():
    from replayguard.dynamic import assert_deterministic

    assert_deterministic(_deterministic, {})


def test_assert_deterministic_raises_runtime_error_when_it_cannot_answer():
    """A harness limit must not read as a pass or a failure of the handler."""
    from replayguard.dynamic import assert_deterministic

    @durable_execution
    def explodes(event, context: DurableContext):
        return context.step(lambda _: 1, name=f"op-{event['missing']}")

    with pytest.raises(RuntimeError, match="nothing was compared"):
        assert_deterministic(explodes, {})
