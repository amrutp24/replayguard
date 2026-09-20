"""Tests for the document that gets published.

This module had no tests for a while, which is exactly the gap worth being
suspicious of: the rules engine was well covered and the thing that turns its
output into a claim anyone reads was not. The failure that hides there is not a
crash -- it is a report that renders cleanly while quietly dropping or softening
a result.

So the assertions below are mostly about what must NOT disappear: a duplicated
side effect must be visible, an unmeasured scenario must not be silently
omitted, and the headline counts must match the rows.
"""

from __future__ import annotations

import pytest

from replayguard.probe.findings import VERDICT_ORDER, render


def obs(**overrides):
    base = {
        "scenario": "d01-control",
        "question": "Does it work?",
        "advice": "It must.",
        "mutation": "nothing",
        "run_id": "d01-control-abcd1234",
        "execution_arn": "arn:aws:lambda:us-east-1:1:function:x/durable-execution/y",
        "pinned_version": "1",
        "status": "SUCCEEDED",
        "error": {},
        "result": {"ok": True},
        "side_effects": {"first": 1, "second": 1},
        "events": ["ExecutionStarted", "StepSucceeded", "ExecutionSucceeded"],
        "suspended_after_s": 12.6,
        "total_s": 120.0,
        "notes": [],
        "verdict": "survived",
    }
    base.update(overrides)
    return base


def doc(observations):
    return render(
        observations, region="us-east-1", account_note="One account.", run_date="2026-09-16"
    )


def test_a_duplicated_side_effect_is_flagged_in_the_matrix():
    """The single most important thing this document can fail to say."""
    out = doc(
        [
            obs(
                scenario="d02-step-renamed",
                status="SUCCEEDED",
                side_effects={"first": 2, "second": 1},
                verdict="silent-reexecution",
            )
        ]
    )
    assert "**(!)**" in out
    assert "`first`=2" in out
    assert "silent-reexecution" in out
    assert "ran a side effect twice" in out


def test_worst_verdict_sorts_to_the_top_of_the_matrix():
    out = doc(
        [
            obs(scenario="d01-control", verdict="survived"),
            obs(
                scenario="d02-step-renamed",
                verdict="silent-reexecution",
                side_effects={"first": 2},
            ),
            obs(scenario="d03-step-inserted", verdict="clean-failure", status="FAILED"),
        ]
    )
    table = out.split("## The matrix")[1].split("## Scenario by scenario")[0]
    rows = [ln for ln in table.splitlines() if ln.startswith("| `d")]
    assert rows[0].startswith("| `d02-step-renamed`"), "worst result must lead"
    assert rows[-1].startswith("| `d01-control`")


def test_an_unmeasured_scenario_is_reported_not_dropped():
    """A matrix with a quietly missing row reads as complete and is not."""
    out = doc(
        [
            obs(
                scenario="d08-sdk-upgraded",
                status="",
                verdict="inconclusive",
                notes=["never observed a WaitStarted event"],
                side_effects={},
            )
        ]
    )
    assert "d08-sdk-upgraded" in out
    assert "**Not measured.**" in out
    assert "never observed a WaitStarted event" in out


def test_headline_counts_match_the_rows():
    out = doc(
        [
            obs(scenario="d01-control", verdict="survived"),
            obs(scenario="d02-step-renamed", verdict="clean-failure", status="FAILED"),
            obs(scenario="d03-step-inserted", verdict="clean-failure", status="FAILED"),
        ]
    )
    assert "Of 3 scenarios, 2 produced a clean failure" in out
    assert "1 survived untouched" in out


def test_the_error_message_is_reproduced_verbatim():
    """The exact wording is the finding for a clean failure."""
    message = "Operation id step_one not found in execution history"
    out = doc(
        [
            obs(
                scenario="d02-step-renamed",
                status="FAILED",
                verdict="clean-failure",
                error={"ErrorMessage": message},
            )
        ]
    )
    assert message in out


def test_limitations_section_is_always_present():
    """The caveats are not optional and must not depend on the results."""
    out = doc([obs()])
    assert "## What this does not establish" in out
    assert "90 seconds" in out
    assert "Python only" in out


def test_advice_under_test_is_carried_into_the_document():
    out = doc([obs(advice="AWS says do not rename steps.")])
    assert "AWS says do not rename steps." in out


def test_execution_history_is_shown():
    out = doc([obs()])
    assert "ExecutionStarted -> StepSucceeded -> ExecutionSucceeded" in out


def test_empty_run_still_renders_a_document():
    """An empty results file must not produce a confident-looking empty report."""
    out = doc([])
    assert "Of 0 scenarios" in out
    assert "## What this does not establish" in out


@pytest.mark.parametrize("verdict", VERDICT_ORDER)
def test_every_verdict_has_a_reading(verdict):
    """A verdict with no explanation would render as a bare word in the table."""
    out = doc([obs(verdict=verdict, status="FAILED", side_effects={"first": 1})])
    if verdict == "inconclusive":
        return
    assert verdict in out
    body = out.split("## Scenario by scenario")[1]
    assert "**Reading.**" in body


# --------------------------------------------------------------------------
# Re-scoring against expected labels.
#
# Added after d03 and d04 both scored "survived" while a step body never ran.
# The original verdict counted duplicate executions and was structurally blind
# to an omission, which is the damage those two scenarios actually caused.
# --------------------------------------------------------------------------


def test_a_missing_step_body_rescoring_beats_survived():
    """d04's real shape: SUCCEEDED, and `second` never executed."""
    out = doc(
        [
            obs(
                scenario="d04-step-removed",
                status="SUCCEEDED",
                verdict="survived",
                side_effects={"first": 1},
            )
        ]
    )
    assert "silent-omission" in out
    assert "survived |" not in out
    assert "**Correction.**" in out
    assert "second" in out


def test_the_inserted_step_is_expected_for_d03():
    """d03 declares three labels, so the absent one is measurable."""
    out = doc(
        [
            obs(
                scenario="d03-step-inserted",
                status="SUCCEEDED",
                verdict="survived",
                side_effects={"first": 1, "second": 1},
            )
        ]
    )
    assert "silent-omission" in out
    assert "inserted" in out


def test_a_complete_run_is_not_rescored():
    """The canary: nothing missing must stay exactly as the runner scored it."""
    out = doc(
        [
            obs(
                scenario="d01-control",
                status="SUCCEEDED",
                verdict="survived",
                side_effects={"first": 1, "second": 1},
            )
        ]
    )
    assert "**Correction.**" not in out
    assert "silent-omission" not in out


def test_a_missing_label_on_a_failed_run_is_noted_not_rescored():
    """A clean failure that skipped a step is still a clean failure.

    The omission is expected there -- the execution stopped. Promoting it to
    silent-omission would overstate the finding, since nothing was silent.
    """
    out = doc(
        [
            obs(
                scenario="d04-step-removed",
                status="FAILED",
                verdict="clean-failure",
                side_effects={"first": 1},
            )
        ]
    )
    assert "clean-failure" in out
    assert "silent-omission" not in out


def test_an_unmeasured_scenario_is_never_rescored():
    """not-delivered and notes must survive re-scoring untouched."""
    out = doc(
        [
            obs(
                scenario="d04-step-removed",
                status="",
                verdict="inconclusive",
                notes=["never observed a WaitStarted event"],
                side_effects={},
            )
        ]
    )
    assert "**Not measured.**" in out
    assert "silent-omission" not in out


def test_a_pinned_scenario_that_was_insulated_is_not_called_not_delivered():
    """d00 and d07 exist to show a mitigation working.

    For them the mutation failing to arrive IS the result. Scoring that as
    'not-delivered' would report the mitigation succeeding as a measurement
    failure, which inverts the finding.
    """
    out = doc(
        [
            obs(
                scenario="d07-alias-repointed",
                status="SUCCEEDED",
                verdict="survived",
                mutation="repointed the alias",
                side_effects={"first": 1, "second": 1},
                mutation_reached=False,
            )
        ]
    )
    assert "insulated" in out
    assert "not-delivered" not in out
    assert "success condition" in out


def test_insulation_is_only_claimed_for_scenarios_that_expect_it():
    """A drift scenario whose mutation did not arrive is still a failed test."""
    out = doc(
        [
            obs(
                scenario="d02-step-renamed",
                status="SUCCEEDED",
                verdict="not-delivered",
                mutation="renamed step_one",
                side_effects={"first": 1, "second": 1},
                mutation_reached=False,
            )
        ]
    )
    assert "not-delivered" in out
    assert "measured nothing" in out
