"""Render a findings document from a live run's raw JSON.

Generated rather than written, on purpose. The rule this project holds itself to
is that no published number is one nobody measured, and the cheapest way to keep
that rule is to make the published table impossible to type by hand: it comes
out of `results/live-matrix.json` or it does not exist.

Scenarios that did not produce a usable observation are rendered as
`(not measured)` with the reason attached, rather than omitted. A matrix with a
quietly missing row reads as a complete result and is not one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Ordered worst-first. The point of the document is the top of this list.
VERDICT_NOTE = {
    "silent-corruption": (
        "**Reported success, ran one side effect twice AND skipped another.** "
        "Both failure modes in a single execution, with status SUCCEEDED and no "
        "diagnostic anywhere. This is the worst cell in the matrix."
    ),
    "silent-omission": (
        "**Reported success, and a step body never ran.** The execution's own "
        "status says SUCCEEDED. A step that the code says should execute did "
        "not, and nothing anywhere reports it."
    ),
    "silent-reexecution": (
        "**Reported success, ran a side effect twice.** The execution's own "
        "status says SUCCEEDED. Nothing in the control plane flags it."
    ),
    "failure-with-reexecution": (
        "Failed *and* re-executed a side effect. The failure is visible; the "
        "duplicate write it already committed is not."
    ),
    "stuck": "Never reached a terminal status within the harness timeout.",
    "clean-failure": (
        "Refused and said so. This is the good outcome: the platform detected the "
        "violation and no side effect was duplicated."
    ),
    "survived": "Completed normally, each step body executing exactly once.",
    "insulated": (
        "The mutation was published and did **not** reach this execution. For "
        "this scenario that is the success condition: the qualifier pinned the "
        "execution to the code it started with."
    ),
    "not-delivered": (
        "The mutation never reached the running execution, so this scenario "
        "measured nothing. Not evidence of safety."
    ),
    "inconclusive": "No status recorded; this run says nothing about the scenario.",
}

VERDICT_ORDER = [
    "silent-corruption",
    "silent-omission",
    "silent-reexecution",
    "failure-with-reexecution",
    "stuck",
    "clean-failure",
    "survived",
    "insulated",
    "not-delivered",
    "inconclusive",
]


def corrected_verdict(obs: dict) -> tuple[str, list[str]]:
    """Re-score an observation against the labels its final code should have run.

    The runner's own verdict was built to catch a side effect executing twice.
    The damage two scenarios actually caused was the opposite: a step body that
    never executed at all, while the execution reported SUCCEEDED. A duplicate
    counter cannot see an omission, so both scenarios scored 'survived'.

    This re-scores from the raw record rather than rewriting it. The committed
    JSON keeps exactly what the run produced -- corrections belong in analysis,
    with a note -- and the note is the second return value.
    """
    from replayguard.probe.scenarios import BY_ID

    notes: list[str] = []
    verdict = obs.get("verdict", "inconclusive")

    scenario = BY_ID.get(obs.get("scenario", ""))
    if scenario is None or obs.get("notes"):
        return verdict, notes

    # For the two scenarios that exist to demonstrate a mitigation, the mutation
    # failing to arrive is the result, not a missed measurement.
    if scenario.expect_insulated and obs.get("status") == "SUCCEEDED":
        if obs.get("mutation_reached") is False or verdict in {
            "not-delivered",
            "survived",
        }:
            if verdict != "insulated":
                notes.append(
                    f"re-scored from '{verdict}' to 'insulated': the redeploy "
                    f"happened and this execution did not see it, which is what "
                    f"the scenario set out to test."
                )
            return "insulated", notes

    observed = {k for k, v in (obs.get("side_effects") or {}).items() if v > 0}
    missing = sorted(set(scenario.expect_labels) - observed)

    if missing and obs.get("status") == "SUCCEEDED":
        duplicated = sorted(
            k for k, v in (obs.get("side_effects") or {}).items() if v > 1
        )
        # Both failure modes at once is strictly worse than either, and
        # re-scoring must not discard the duplication to report the omission.
        target = "silent-corruption" if duplicated else "silent-omission"
        if verdict != target:
            detail = (
                f"{', '.join(duplicated)} ran more than once and "
                if duplicated
                else "the run recorded no execution of "
            )
            notes.append(
                f"re-scored from '{verdict}' to '{target}': {detail}"
                f"{', '.join(missing)} never ran at all, though the final code "
                f"says it should have. The harness's original verdict counted "
                f"duplicate executions only and could not see an omission."
            )
        verdict = target
    elif missing:
        notes.append(
            f"step bodies that never executed: {', '.join(missing)} "
            f"(status was {obs.get('status') or 'unknown'})"
        )

    return verdict, notes


def load(path: str | Path) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf8"))


def _effects(obs: dict) -> str:
    se = obs.get("side_effects") or {}
    if not se:
        return "none recorded"
    return ", ".join(f"`{k}`={v}" for k, v in sorted(se.items()))


def _duplicated(obs: dict) -> bool:
    return any(v > 1 for v in (obs.get("side_effects") or {}).values())


def _error_line(obs: dict) -> str:
    err = obs.get("error") or {}
    if not err:
        return ""
    for key in ("ErrorMessage", "Message", "message", "errorMessage", "raw"):
        if err.get(key):
            return str(err[key]).strip().replace("\n", " ")[:300]
    return json.dumps(err)[:300]


def render(observations: list[dict], *, region: str, account_note: str, run_date: str) -> str:
    from replayguard.probe.scenarios import BY_ID

    lines: list[str] = []
    w = lines.append

    # Re-score before anything is rendered, so the matrix, the counts and the
    # per-scenario sections cannot disagree with each other.
    rescored: list[dict] = []
    for o in observations:
        verdict, notes = corrected_verdict(o)
        rescored.append(dict(o, verdict=verdict, _rescore_notes=notes))
    observations = rescored

    w("# Findings: what AWS actually does when you break a suspended execution")
    w("")
    w(
        f"Measured {run_date}, {region}. {account_note} "
        f"Every number here comes from "
        "[`results/live-matrix.json`](results/live-matrix.json), which is the raw "
        "output of the run and is committed unedited."
    )
    w("")

    counts = {v: 0 for v in VERDICT_ORDER}
    for o in observations:
        counts[o.get("verdict", "inconclusive")] = (
            counts.get(o.get("verdict", "inconclusive"), 0) + 1
        )

    w("## The answer")
    w("")
    corrupted = (
        counts.get("silent-corruption", 0)
        + counts.get("silent-omission", 0)
        + counts.get("silent-reexecution", 0)
        + counts.get("failure-with-reexecution", 0)
    )
    w(
        f"Of {len(observations)} scenarios, {counts.get('clean-failure', 0)} produced a "
        f"clean failure, {counts.get('survived', 0)} survived untouched, "
        f"{counts.get('stuck', 0)} never reached a terminal state, and "
        f"**{corrupted} corrupted the execution while reporting success**."
    )
    w("")
    silent = counts.get("silent-corruption", 0) + counts.get("silent-omission", 0)
    if silent:
        w(
            f"Those {silent} are the result worth acting on. In each, a step body "
            f"the code says should run did not run -- and in "
            f"{counts.get('silent-corruption', 0)} of them another step body ran "
            f"twice as well. Every one finished with status SUCCEEDED. Nothing in "
            f"the execution history, the status or the error field says otherwise."
        )
        w("")
    if counts.get("insulated"):
        w(
            f"{counts['insulated']} scenario(s) were insulated: the redeploy "
            f"happened and the execution never saw it, because it was pinned to a "
            f"published version. That is the mitigation working, and it is the "
            f"practical answer this matrix produces."
        )
        w("")
    if counts.get("not-delivered"):
        w(
            f"{counts['not-delivered']} scenario(s) are reported as not-delivered: "
            f"the mutation never reached the running execution, so they measured "
            f"nothing and are not evidence either way."
        )
        w("")

    w("## The matrix")
    w("")
    w("| Scenario | Status | Verdict | Step bodies executed |")
    w("|---|---|---|---|")
    for o in sorted(
        observations,
        key=lambda x: (
            VERDICT_ORDER.index(x.get("verdict", "inconclusive"))
            if x.get("verdict") in VERDICT_ORDER
            else len(VERDICT_ORDER)
        ),
    ):
        flag = " **(!)**" if _duplicated(o) else ""
        w(
            f"| `{o['scenario']}` | {o.get('status') or '-'} | "
            f"{o.get('verdict', '?')}{flag} | {_effects(o)} |"
        )
    w("")
    w(
        "`first` and `second` count how many times each step body actually ran, "
        "measured by an atomic DynamoDB increment inside the step. A count of 1 "
        "means the checkpoint was honoured. A count of 2 means the side effect "
        "was re-executed."
    )
    w("")

    w("## Scenario by scenario")
    w("")
    for o in observations:
        w(f"### `{o['scenario']}`")
        w("")
        w(f"**Question.** {o.get('question', '')}")
        w("")
        w(f"**Advice under test.** {o.get('advice', '')}")
        w("")
        w(f"**What the harness did.** {o.get('mutation', '')}")
        w("")

        if o.get("notes"):
            w(f"**Not measured.** {' '.join(o['notes'])}")
            w("")
            continue

        for note in o.get("_rescore_notes", []):
            w(f"> **Correction.** {note}")
            w("")

        w(
            f"**Observed.** Status `{o.get('status') or 'unknown'}`. "
            f"Step bodies executed: {_effects(o)}. "
            f"Suspended after {o.get('suspended_after_s', '?')}s, "
            f"total {o.get('total_s', '?')}s."
        )
        if o.get("pinned_version"):
            w("")
            w(
                f"The platform reports this execution pinned to function version "
                f"`{o['pinned_version']}`."
            )
        err = _error_line(o)
        if err:
            w("")
            w("```")
            w(err)
            w("```")
        w("")
        note = VERDICT_NOTE.get(o.get("verdict", ""), "")
        if note:
            w(f"**Reading.** {note}")
            w("")
        scenario = BY_ID.get(o.get("scenario", ""))
        if scenario is not None and scenario.caveat:
            w(f"**What this does not show.** {scenario.caveat}")
            w("")
        if o.get("events"):
            w(f"Execution history: `{' -> '.join(o['events'])}`")
            w("")

    w("## What this does not establish")
    w("")
    w(
        "- One account, one region, one runtime, one execution per scenario. These "
        "are observations, not a statistical characterisation, and a scenario that "
        "survived once is not thereby safe."
    )
    w(
        "- The suspend is 90 seconds. A real execution suspended for weeks crosses "
        "platform changes this harness cannot stage, which is the case the whole "
        "question is about and the one still unmeasured."
    )
    w(
        "- Python only. The JS, Java and .NET SDKs have their own replay engines "
        "and may not behave the same way."
    )
    w(
        "- A clean failure here means the platform detected *this* violation. It "
        "is not evidence that it detects every violation."
    )
    w("")
    return "\n".join(lines)
