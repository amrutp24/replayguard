"""Verdict logic, which is pure and therefore testable without an account.

The rule these encode: a scenario whose mutation never reached the execution has
measured nothing, and must never be reported as 'survived'. That mistake already
cost this project a complete run -- nine scenarios returned a confident green
while pinned to an immutable version the mutation could not touch.
"""

from __future__ import annotations

import pytest

from replayguard.probe.runner import Observation


def make(**overrides) -> Observation:
    base = {
        "scenario": "d02-step-renamed",
        "question": "What happens?",
        "advice": "AWS says do not.",
        "mutation": "renamed step_one",
        "run_id": "x",
        "status": "SUCCEEDED",
        "side_effects": {"first": 1, "second": 1},
        "initial_build": {"variant": "baseline", "probe": "initial", "sdk": "1.7.0"},
        "final_build": {"variant": "step_renamed", "probe": "initial", "sdk": "1.7.0"},
    }
    base.update(overrides)
    return Observation(**base)


# --------------------------------------------------------------------------
# mutation_reached
# --------------------------------------------------------------------------


def test_a_changed_variant_proves_the_mutation_landed():
    assert make().mutation_reached is True


def test_an_unchanged_build_means_the_mutation_never_landed():
    """The exact signature of the bug that invalidated the first matrix."""
    obs = make(final_build={"variant": "baseline", "probe": "initial", "sdk": "1.7.0"})
    assert obs.mutation_reached is False


def test_an_sdk_swap_counts_as_delivery():
    obs = make(
        mutation="swapped the bundled SDK",
        initial_build={"variant": "baseline", "probe": "initial", "sdk": "1.7.0"},
        final_build={"variant": "baseline", "probe": "initial", "sdk": "2.0.0"},
    )
    assert obs.mutation_reached is True


def test_an_env_change_counts_as_delivery():
    obs = make(
        mutation="changed RD_PROBE",
        initial_build={"variant": "baseline", "probe": "before", "sdk": "1.7.0"},
        final_build={"variant": "baseline", "probe": "after", "sdk": "1.7.0"},
    )
    assert obs.mutation_reached is True


def test_unanswerable_is_none_not_false():
    """None and False mean different things and must not collapse.

    False means 'the mutation demonstrably did not land'. None means 'no
    evidence either way'. Reporting the second as the first would invent a
    finding.
    """
    assert make(final_build={}).mutation_reached is None
    assert make(initial_build={}).mutation_reached is None


def test_a_control_has_nothing_to_deliver():
    obs = make(
        scenario="d01-control",
        mutation="nothing",
        final_build={"variant": "baseline", "probe": "initial", "sdk": "1.7.0"},
    )
    assert obs.mutation_reached is None
    assert obs.verdict == "survived"


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------


def test_undelivered_outranks_survived():
    """The whole point. A green status over an undelivered mutation is a lie."""
    obs = make(
        status="SUCCEEDED",
        final_build={"variant": "baseline", "probe": "initial", "sdk": "1.7.0"},
    )
    assert obs.verdict == "not-delivered"


def test_success_with_a_duplicated_side_effect_is_silent_reexecution():
    assert make(status="SUCCEEDED", side_effects={"first": 2}).verdict == (
        "silent-reexecution"
    )


def test_success_with_single_execution_is_survived():
    assert make(status="SUCCEEDED").verdict == "survived"


def test_failure_without_duplication_is_a_clean_failure():
    assert make(status="FAILED").verdict == "clean-failure"


def test_failure_with_duplication_is_worse_than_a_clean_failure():
    obs = make(status="FAILED", side_effects={"first": 2, "second": 1})
    assert obs.verdict == "failure-with-reexecution"


def test_timeout_is_stuck():
    assert make(status="TIMED_OUT").verdict == "stuck"


def test_no_status_is_inconclusive():
    assert make(status="", final_build={}, initial_build={}).verdict == "inconclusive"


@pytest.mark.parametrize(
    "status,effects,expected",
    [
        ("SUCCEEDED", {"first": 1, "second": 1}, "survived"),
        ("SUCCEEDED", {"first": 2, "second": 1}, "silent-reexecution"),
        ("FAILED", {"first": 1}, "clean-failure"),
        ("TIMED_OUT", {"first": 1}, "stuck"),
        ("STOPPED", {"first": 1}, "stopped"),
    ],
)
def test_verdict_table(status, effects, expected):
    assert make(status=status, side_effects=effects).verdict == expected
