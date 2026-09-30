"""The drift matrix.

Each scenario names one piece of published advice and arranges for it to be
violated against a live execution, so the advice can be replaced with an
observation. The `advice` field is not decoration: it is the claim under test,
and a scenario whose result agrees with it is as much a finding as one that does
not.

## Why the drift scenarios run on $LATEST

The first version of this file published an immutable version for every scenario
and invoked through it. Every code-drift scenario came back "survived", which
looked like a finding and was an artifact: a published Lambda version cannot
change, so redeploying during the suspend produced a *new* version while the
execution went on replaying the old one. Nine of twelve scenarios were measuring
nothing, and measuring it convincingly.

So the scenarios that need drift to actually reach the execution deploy with
`publish=False` and run on $LATEST, which is mutable. The insulation this
exposed is worth reporting rather than discarding, and is now
`d00-version-pinned` in its own right -- that scenario exists to demonstrate the
mitigation working, and its "survived" result means something precisely because
the others are arranged so drift can reach them.

`d01-control` runs on $LATEST too, and is untouched. Without it, a failure in the
$LATEST scenarios could not be separated from "running on $LATEST is itself the
problem".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# deploy is imported inside the mutation closures, not here. It pulls in
# botocore at import time, and this module is also what `probe --list` and
# `probe --report` read -- both documented as needing no account and no extra.
# A top-level import made them crash on a plain `pip install replayguard`, and
# the tests never noticed because the dev environment has boto3.


@dataclass(frozen=True)
class Scenario:
    id: str
    question: str
    advice: str
    mutation: str
    #: kwargs for the initial deploy.
    initial: dict[str, Any] = field(default_factory=dict)
    #: Applied once the execution has genuinely suspended.
    mutate: Callable[..., None] = lambda *a, **k: None
    #: When set, the execution is started through this alias rather than a
    #: pinned version -- which is the configuration the mitigation advice
    #: actually recommends.
    use_alias: str = ""
    #: True when the scenario's whole point is that the mutation must NOT reach
    #: the execution. For d00 and d07 non-delivery is the success condition --
    #: the version or alias insulated it -- so reporting them as 'measured
    #: nothing' would invert the finding. Everywhere else non-delivery means the
    #: scenario failed to test what it claims to.
    expect_insulated: bool = False
    #: What this scenario does NOT establish, when that is not obvious from a
    #: clean result. A scenario that passes for the wrong reason is worse than
    #: one that fails, because it gets cited.
    caveat: str = ""
    #: Every step-body label this scenario's *final* code should have executed.
    #:
    #: This exists because d03 exposed a hole in the verdict logic. The harness
    #: was built to catch a side effect running twice, and the damage that
    #: scenario actually caused was the opposite: a step whose body never ran at
    #: all, while the execution reported SUCCEEDED. Counting duplicates cannot
    #: see an omission, so the expected set has to be declared.
    expect_labels: tuple[str, ...] = ("first", "second")


def _redeploy(**overrides):
    """A mutation that rebuilds and redeploys the function mid-suspend."""

    def apply(infra, name, *, wait_seconds, cache_dir):
        from replayguard.probe import deploy as deploy_mod

        deploy_mod.deploy(
            infra, name, wait_seconds=wait_seconds, cache_dir=cache_dir, **overrides
        )

    return apply


def _redeploy_and_move_alias(alias: str, **overrides):
    """Redeploy, publish a new version, and repoint the alias at it.

    This is the procedure the guidance recommends, carried out at the worst
    possible moment. If the alias insulates in-flight executions, this scenario
    survives; if the platform re-resolves the alias on resume, it does not.
    """

    def apply(infra, name, *, wait_seconds, cache_dir):
        from replayguard.probe import deploy as deploy_mod

        resp = deploy_mod.deploy(
            infra,
            name,
            wait_seconds=wait_seconds,
            cache_dir=cache_dir,
            publish=True,
            **overrides,
        )
        deploy_mod.set_alias(infra, name, alias, resp.get("Version", "$LATEST"))

    return apply


SCENARIOS: list[Scenario] = [
    Scenario(
        id="d00-version-pinned",
        question=(
            "Does pinning an execution to a published version insulate it from a "
            "redeploy, as the mitigation claims?"
        ),
        advice=(
            "AWS: invoke with version numbers or aliases to pin executions to "
            "specific code versions. (Lambda dev guide, durable-best-practices)"
        ),
        mutation=(
            "published a renamed-step version 2 while the execution ran on the "
            "pinned, immutable version 1"
        ),
        initial={"body": "baseline", "publish": True},
        mutate=_redeploy(body="step_renamed", publish=True),
        expect_insulated=True,
    ),
    Scenario(
        id="d01-control",
        question="Does an untouched execution on $LATEST suspend and resume cleanly?",
        advice=(
            "It must. This is the control for every $LATEST scenario below -- "
            "without it, a failure there could not be separated from 'running on "
            "$LATEST is itself the problem'."
        ),
        mutation="nothing",
        initial={"body": "baseline", "publish": False},
    ),
    Scenario(
        id="d02-step-renamed",
        question="What happens when a completed step is renamed mid-flight?",
        advice=(
            "AWS: don't rename steps or change their behaviour in ways that break "
            "replay; doing so while executions are in progress means they can fail "
            "to resume or produce incorrect results. Both outcomes are named. "
            "Which one you get is not. (Lambda dev guide, durable-best-practices)"
        ),
        mutation="renamed step_one to step_one_renamed on $LATEST while suspended",
        initial={"body": "baseline", "publish": False},
        mutate=_redeploy(body="step_renamed", publish=False),
    ),
    Scenario(
        id="d03-step-inserted",
        question="What happens when a new step is inserted before a completed one?",
        advice=(
            "AWS: ensure new code versions can handle state from older versions. "
            "Community guidance is blunter -- do not reorder steps, the engine "
            "expects a specific sequence."
        ),
        mutation="inserted step_zero ahead of the already-checkpointed step_one",
        initial={"body": "baseline", "publish": False},
        mutate=_redeploy(body="step_inserted", publish=False),
        # The inserted step has its own label, so its absence from the table is
        # measurable rather than merely suspected.
        expect_labels=("inserted", "first", "second"),
    ),
    Scenario(
        id="d04-step-removed",
        question="What happens when a completed step is deleted from the code?",
        advice=(
            "The history now holds a checkpoint the code does not account for. "
            "No published guidance states the outcome."
        ),
        mutation="deleted step_one, which had already checkpointed",
        initial={"body": "baseline", "publish": False},
        mutate=_redeploy(body="step_removed", publish=False),
    ),
    Scenario(
        id="d05-step-reordered",
        question="What happens when two steps swap order across a suspend?",
        advice="Community guidance: do not reorder steps.",
        mutation="swapped step_one and step_two",
        initial={"body": "baseline", "publish": False},
        mutate=_redeploy(body="step_reordered", publish=False),
    ),
    Scenario(
        id="d06-nonstep-refactor",
        question=(
            "Is a pure refactor outside every step safe across a suspend, as the "
            "determinism contract implies?"
        ),
        advice=(
            "AWS: code outside a durable operation runs on every replay and must be "
            "a pure function of the handler inputs and completed operations. Adding "
            "pure local computation should therefore be safe. This is the scenario "
            "whose failure would be most surprising."
        ),
        mutation="added pure local computation outside all steps",
        initial={"body": "baseline", "publish": False},
        mutate=_redeploy(body="nonstep_refactor", publish=False),
    ),
    Scenario(
        id="d07-alias-repointed",
        question=(
            "Does an alias insulate an in-flight execution, or does the platform "
            "re-resolve it on resume?"
        ),
        advice=(
            "AWS: invoke with version numbers or aliases to pin executions to "
            "specific code versions. Community guides go further and claim "
            "in-flight executions continue on the version they started with. This "
            "is THE recommended mitigation and it is untested in public."
        ),
        mutation="published a renamed-step version and repointed the alias at it",
        initial={"body": "baseline", "publish": True},
        use_alias="live",
        mutate=_redeploy_and_move_alias("live", body="step_renamed"),
        expect_insulated=True,
    ),
    Scenario(
        id="d08-sdk-upgraded",
        question=(
            "What happens when the bundled durable SDK goes 1.7.0 to 2.0.0 "
            "underneath a suspended execution?"
        ),
        advice=(
            "AWS: ensure new code versions can handle state from older versions. "
            "That burden is placed on the developer and no tooling is shipped to "
            "discharge it. The offline diff says this upgrade changes the replay "
            "engine, the checkpoint state handling and the exception hierarchy "
            "while leaving every public method identical."
        ),
        mutation="swapped the bundled SDK from 1.7.0 to 2.0.0, handler unchanged",
        initial={"body": "baseline", "sdk_version": "1.7.0", "publish": False},
        mutate=_redeploy(body="baseline", sdk_version="2.0.0", publish=False),
    ),
    Scenario(
        id="d09-sdk-downgraded",
        question=(
            "And in reverse: what happens on a rollback, 2.0.0 back to 1.7.0, "
            "across a suspend?"
        ),
        advice=(
            "Rollback is the operator's instinct when a deploy goes wrong, and it "
            "is the direction nobody tests."
        ),
        mutation="rolled the bundled SDK back from 2.0.0 to 1.7.0",
        initial={"body": "baseline", "sdk_version": "2.0.0", "publish": False},
        mutate=_redeploy(body="baseline", sdk_version="1.7.0", publish=False),
    ),
    Scenario(
        id="d10-env-changed",
        question=(
            "An environment variable is read at module scope. What does replay see "
            "after it changes?"
        ),
        advice=(
            "AWS: env vars read at runtime can change between the first invocation "
            "and a replay, so capture the value inside a step. The stated "
            "consequences are control flow walking a different branch, a downstream "
            "step running with wrong inputs, or a return from an operation that "
            "never ran. (Durable Execution SDK guide, best-practices/determinism)"
        ),
        mutation="changed RD_PROBE, which the handler reads outside any step",
        initial={"body": "baseline", "probe": "before", "publish": False},
        mutate=_redeploy(body="baseline", probe="after", publish=False),
        caveat=(
            "A clean result here is weaker than it looks. The handler records "
            "RD_PROBE but never branches on it, so this shows the value does "
            "drift under a running execution -- the returned probe is 'after' "
            "when the execution began on 'before' -- without exercising the "
            "consequence AWS documents, which is control flow taking a "
            "different path on replay. Read this as confirming the premise, "
            "not as evidence that environment drift is harmless."
        ),
    ),
    Scenario(
        id="d11-memory-changed",
        question="Does changing memory mid-suspend disturb a running execution?",
        advice="No guidance found either way.",
        mutation="raised memory from 512MB to 1024MB while suspended",
        initial={"body": "baseline", "memory": 512, "publish": False},
        mutate=_redeploy(body="baseline", memory=1024, publish=False),
        caveat=(
            "Memory is not part of the build stamp the handler reports, so "
            "unlike every other drift scenario this one has no independent "
            "evidence inside the execution that the change landed before the "
            "resume. The configuration update was accepted by Lambda, but the "
            "delivery is not self-verified the way a variant or SDK swap is."
        ),
    ),
]

BY_ID = {s.id: s for s in SCENARIOS}


def select(ids: list[str] | None) -> list[Scenario]:
    if not ids:
        return list(SCENARIOS)
    missing = [i for i in ids if i not in BY_ID]
    if missing:
        raise KeyError(
            f"unknown scenario(s): {', '.join(missing)}. Available: {', '.join(BY_ID)}"
        )
    return [BY_ID[i] for i in ids]
