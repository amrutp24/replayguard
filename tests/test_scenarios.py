"""Guards on the scenario matrix itself.

These exist because of a real bug, not a hypothetical one. The first version of
the matrix published an immutable Lambda version for every scenario and invoked
through it, so a mid-suspend redeploy created a *new* version while the execution
carried on replaying the old one. Nine of twelve scenarios were measuring
nothing, and every one of them returned a confident "survived".

That is the worst failure mode available to this project: not a crash, but a
clean green result that means nothing. The tests below encode the invariant that
would have caught it.
"""

from __future__ import annotations

import pytest

from replayguard.probe.scenarios import BY_ID, SCENARIOS, select

#: Scenarios that deliberately pin to an immutable version, because the thing
#: they measure IS the insulation. Everything else that mutates must be able to
#: have its mutation reach the running execution.
PINNED_BY_DESIGN = {"d00-version-pinned", "d07-alias-repointed"}


def _mutates(scenario) -> bool:
    """Does this scenario actually change anything mid-suspend?"""
    return scenario.mutation.strip().lower() != "nothing"


def test_mutating_scenarios_run_on_latest_or_are_pinned_by_design():
    """The invariant the original matrix violated.

    A published version cannot change. So a scenario that mutates code and also
    pins to a published version is measuring the immutability of versions, not
    the thing its question asks about -- and it will say "survived" either way.
    """
    offenders = [
        s.id
        for s in SCENARIOS
        if _mutates(s)
        and s.id not in PINNED_BY_DESIGN
        and s.initial.get("publish", True) is not False
    ]
    assert offenders == [], (
        "these scenarios mutate code but pin to an immutable published version, "
        f"so the mutation cannot reach the running execution: {offenders}"
    )


def test_pinned_by_design_scenarios_really_do_publish():
    """The converse: the insulation scenarios must actually pin something."""
    for sid in PINNED_BY_DESIGN:
        assert BY_ID[sid].initial.get("publish") is True, (
            f"{sid} exists to demonstrate version pinning and must publish"
        )


def test_there_is_an_untouched_control_on_latest():
    """Without it, a $LATEST failure cannot be attributed to the drift."""
    control = BY_ID["d01-control"]
    assert not _mutates(control)
    assert control.initial.get("publish") is False


def test_every_scenario_names_the_advice_it_tests():
    for s in SCENARIOS:
        assert s.advice.strip(), f"{s.id} has no advice under test"
        assert s.question.strip().endswith("?"), f"{s.id}'s question is not a question"


def test_scenario_ids_are_unique_and_sorted():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids), "ids carry their run order; keep them sorted"


def test_select_returns_everything_by_default():
    assert select(None) == SCENARIOS
    assert select([]) == SCENARIOS


def test_select_names_the_unknown_id():
    with pytest.raises(KeyError, match="nope"):
        select(["nope"])


def test_select_preserves_requested_order():
    assert [s.id for s in select(["d02-step-renamed", "d01-control"])] == [
        "d02-step-renamed",
        "d01-control",
    ]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_initial_deploy_kwargs_are_accepted_by_deploy(scenario):
    """Catch a typo in a scenario's kwargs here, not 90 seconds into a run."""
    import inspect

    from replayguard.probe import deploy as deploy_mod

    accepted = set(inspect.signature(deploy_mod.deploy).parameters)
    unknown = set(scenario.initial) - accepted
    assert not unknown, f"{scenario.id} passes unknown deploy kwargs: {unknown}"
