"""Drive one scenario end to end and record what the platform actually did.

The shape of every scenario is the same:

    deploy -> start -> wait until it has genuinely suspended -> change something
    underneath it -> wait for a terminal status -> read the evidence

Two details do most of the work.

**Suspension is detected from the execution history, not from the status.**
`GetDurableExecution` reports RUNNING for a suspended execution -- there is no
SUSPENDED status -- so mutating on a status poll would race the checkpoint and
sometimes change the code before the first step had committed. The harness waits
for a `WaitStarted` event, which is the platform saying the checkpoint is
written and the handler has gone away.

**The side-effect counter is the finding, not the status.** A scenario that ends
FAILED is a clean failure and comparatively good news. The bad outcome is
SUCCEEDED with a step body that ran twice, because that is corruption the
execution reports as success. Counting external writes is the only way to tell
those apart, and it is why the handler writes to DynamoDB at all.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from replayguard.probe import deploy as deploy_mod
from replayguard.probe.infra import PREFIX, Infra

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED"}


@dataclass
class Observation:
    """What one scenario run produced. This is the publishable unit."""

    scenario: str
    question: str
    advice: str
    mutation: str
    run_id: str
    execution_arn: str = ""
    #: Function version the execution was pinned to, as the platform reports it.
    pinned_version: str = ""
    status: str = ""
    error: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    #: label -> number of times that step body actually executed.
    side_effects: dict[str, int] = field(default_factory=dict)
    #: Which build the handler was running when it *finished*. The handler
    #: reports its own variant, probe and SDK version in its return value, so
    #: this is the execution's own account of the code that completed it.
    final_build: dict[str, str] = field(default_factory=dict)
    #: The build the scenario deployed before starting the execution.
    initial_build: dict[str, str] = field(default_factory=dict)
    #: Ordered EventType stream, so a failure can be located in the lifecycle.
    events: list[str] = field(default_factory=list)
    suspended_after_s: float = 0.0
    total_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def mutation_reached(self) -> bool | None:
        """Did the change actually land on the execution that resumed?

        This exists because a whole matrix was once invalidated by not asking
        it. Every code-drift scenario reported a confident "survived" while the
        executions were pinned to an immutable version the mutation could not
        touch -- a green result that measured nothing.

        The handler reports its own build in its return value, so comparing what
        finished against what started is the execution's own evidence that the
        new code ran. `None` means the question could not be answered, which is
        different from `False` and must not be reported as a clean run.
        """
        if not self.initial_build or not self.final_build:
            return None
        if self.mutation.strip().lower() == "nothing":
            # A control has nothing to deliver; "reached" is not meaningful.
            return None
        return any(
            self.final_build.get(k) != v
            for k, v in self.initial_build.items()
            if k in self.final_build
        )

    @property
    def verdict(self) -> str:
        """A one-word reading of the outcome, for the matrix.

        Deliberately blunt, and deliberately *not* generous. 'clean-failure' is
        a good result: the platform refused and said so. 'silent-reexecution' is
        the one worth publishing a warning about.

        'not-delivered' outranks every other reading. A scenario whose mutation
        never reached the execution has measured nothing, and saying anything
        else about it -- especially 'survived' -- is the exact mistake that cost
        this project a full run.
        """
        duplicated = {k: v for k, v in self.side_effects.items() if v > 1}
        if self.mutation_reached is False:
            return "not-delivered"
        if self.status == "SUCCEEDED" and duplicated:
            return "silent-reexecution"
        if self.status == "SUCCEEDED":
            return "survived"
        if self.status in {"FAILED", "TIMED_OUT"} and duplicated:
            return "failure-with-reexecution"
        if self.status == "FAILED":
            return "clean-failure"
        if self.status == "TIMED_OUT":
            return "stuck"
        if not self.status:
            return "inconclusive"
        return self.status.lower()


def run_scenario(
    infra: Infra,
    scenario,
    *,
    wait_seconds: int = 90,
    timeout_s: int = 900,
    cache_dir=None,
    verbose: bool = True,
) -> Observation:
    name = f"{PREFIX}{scenario.id}"
    run_id = f"{scenario.id}-{uuid.uuid4().hex[:8]}"
    say = print if verbose else (lambda *a, **k: None)

    obs = Observation(
        scenario=scenario.id,
        question=scenario.question,
        advice=scenario.advice,
        mutation=scenario.mutation,
        run_id=run_id,
    )

    say(f"\n[{scenario.id}] {scenario.question}")

    started = time.time()
    try:
        # 1. Deploy the pre-drift function.
        created = deploy_mod.deploy(
            infra,
            name,
            wait_seconds=wait_seconds,
            cache_dir=cache_dir,
            **scenario.initial,
        )
        obs.initial_build = {
            "variant": scenario.initial.get("body", "baseline"),
            "probe": scenario.initial.get("probe", "initial"),
            "sdk": str(scenario.initial.get("sdk_version", deploy_mod.DEFAULT_SDK)),
        }
        version = created.get("Version", "$LATEST")
        qualifier = version
        if scenario.use_alias:
            deploy_mod.set_alias(infra, name, scenario.use_alias, version)
            qualifier = scenario.use_alias
        say(f"  deployed version {version}, invoking via {qualifier}")

        # 2. Start it. Event invocation: a durable execution that suspends for
        #    90s would otherwise hold the caller open for the whole wait.
        resp = infra.lam.invoke(
            FunctionName=name,
            Qualifier=qualifier,
            InvocationType="Event",
            DurableExecutionName=run_id,
            Payload=json.dumps({"run_id": run_id}).encode(),
        )
        arn = resp.get("DurableExecutionArn", "")
        if not arn:
            arn = _find_execution_arn(infra, name, run_id)
        obs.execution_arn = arn
        if not arn:
            obs.notes.append("no DurableExecutionArn returned or discoverable")
            return obs

        # 3. Wait until it has actually suspended.
        if not _await_suspension(infra, arn, timeout_s=timeout_s):
            obs.notes.append(
                "never observed a WaitStarted event; mutation was not applied, "
                "so this run says nothing about the scenario"
            )
            obs.status = _status(infra, arn)
            obs.events = _event_types(infra, arn)
            return obs
        obs.suspended_after_s = round(time.time() - started, 1)
        say(f"  suspended after {obs.suspended_after_s}s; applying mutation")

        # 4. Change something underneath it.
        scenario.mutate(infra, name, wait_seconds=wait_seconds, cache_dir=cache_dir)
        say(f"  mutated: {scenario.mutation}")

        # 5. Wait for a terminal status.
        obs.status = _await_terminal(infra, arn, timeout_s=timeout_s)
    except ClientError as exc:
        obs.notes.append(
            f"aws error: {exc.response['Error']['Code']}: "
            f"{exc.response['Error'].get('Message', '')[:400]}"
        )
    finally:
        obs.total_s = round(time.time() - started, 1)
        if obs.execution_arn:
            _collect(infra, obs)
        obs.side_effects = _side_effects(infra, run_id)

    reached = {True: "yes", False: "NO", None: "n/a"}[obs.mutation_reached]
    say(
        f"  status={obs.status or 'unknown'}  side effects={obs.side_effects}  "
        f"mutation reached={reached}  -> {obs.verdict}"
    )
    return obs


def _status(infra, arn: str) -> str:
    try:
        return infra.lam.get_durable_execution(DurableExecutionArn=arn).get("Status", "")
    except ClientError:
        return ""


def _event_types(infra, arn: str) -> list[str]:
    try:
        events = infra.lam.get_durable_execution_history(
            DurableExecutionArn=arn, MaxItems=200
        ).get("Events", [])
    except ClientError:
        return []
    return [e.get("EventType", "?") for e in events]


def _await_suspension(infra, arn: str, *, timeout_s: int) -> bool:
    """Block until a WaitStarted event appears, or the execution ends early."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        events = _event_types(infra, arn)
        if "WaitStarted" in events:
            return True
        if _status(infra, arn) in TERMINAL:
            # It finished before it ever suspended. Nothing to mutate against.
            return False
        time.sleep(3)
    return False


def _await_terminal(infra, arn: str, *, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        last = _status(infra, arn)
        if last in TERMINAL:
            return last
        time.sleep(5)
    # Not the same as FAILED, and recorded differently: an execution the harness
    # stopped watching is 'stuck' from the operator's point of view, which is
    # itself one of the outcomes worth reporting.
    return last or "TIMED_OUT"


def _collect(infra, obs: Observation) -> None:
    try:
        got = infra.lam.get_durable_execution(DurableExecutionArn=obs.execution_arn)
    except ClientError as exc:
        obs.notes.append(f"could not read execution: {exc.response['Error']['Code']}")
        return

    obs.status = got.get("Status", obs.status)
    obs.pinned_version = str(got.get("Version", ""))
    if got.get("Error"):
        err = got["Error"]
        obs.error = err if isinstance(err, dict) else {"raw": str(err)}
    raw = got.get("Result")
    if raw:
        try:
            obs.result = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            obs.result = str(raw)[:500]
        # The handler reports its own variant, probe and SDK version. That is
        # the execution's own account of which build completed it, and is what
        # makes "did the mutation actually land" answerable from the record
        # rather than by someone remembering to check.
        if isinstance(obs.result, dict):
            obs.final_build = {
                k: str(obs.result[k])
                for k in ("variant", "probe", "sdk")
                if k in obs.result
            }
    obs.events = _event_types(infra, obs.execution_arn)


def _find_execution_arn(infra, name: str, run_id: str) -> str:
    """Fall back to listing when Invoke did not hand back an ARN."""
    try:
        page = infra.lam.list_durable_executions_by_function(
            FunctionName=name, DurableExecutionName=run_id, MaxItems=5
        )
    except ClientError:
        return ""
    for ex in page.get("DurableExecutions", []):
        if ex.get("DurableExecutionName") == run_id:
            return ex.get("DurableExecutionArn", "")
    return ""


def _side_effects(infra, run_id: str) -> dict[str, int]:
    """How many times each step body actually executed."""
    try:
        resp = infra.ddb.query(
            TableName=infra.table,
            KeyConditionExpression="run_id = :r",
            ExpressionAttributeValues={":r": {"S": run_id}},
        )
    except ClientError:
        return {}
    return {
        item["label"]["S"]: int(item.get("hits", {}).get("N", "0"))
        for item in resp.get("Items", [])
    }


def to_json(observations: list[Observation]) -> str:
    return json.dumps(
        [
            asdict(o) | {"verdict": o.verdict, "mutation_reached": o.mutation_reached}
            for o in observations
        ],
        indent=2,
        default=str,
    )


def run_matrix(
    scenarios,
    *,
    region: str,
    profile: str | None = None,
    wait_seconds: int = 90,
    timeout_s: int = 900,
    cache_dir=None,
    json_out: str | None = None,
    keep: bool = False,
) -> list[Observation]:
    """Provision, run every scenario, report, and tear down.

    Lives here rather than in the CLI because it drives AWS, and the CLI's job
    is to parse arguments and delegate. That also keeps the argument-parsing
    paths unit-testable while this one is exercised only against a real account.

    The teardown is in a `finally` so that an interrupt halfway through a
    45-minute run still leaves the account clean.
    """
    from pathlib import Path

    from replayguard.probe.infra import destroy, provision

    print(
        "This creates real AWS resources (Lambda functions, an IAM role, a "
        "DynamoDB table), all prefixed 'rd-'. Expected spend is well under $0.10."
    )
    print(f"Running {len(scenarios)} scenario(s) with a {wait_seconds}s suspend each.\n")

    infra = provision(region, profile)
    observations: list[Observation] = []
    try:
        for scenario in scenarios:
            observations.append(
                run_scenario(
                    infra,
                    scenario,
                    wait_seconds=wait_seconds,
                    timeout_s=timeout_s,
                    cache_dir=cache_dir,
                )
            )
    finally:
        if observations:
            if json_out:
                Path(json_out).write_text(to_json(observations), encoding="utf8")
                print(f"\nwrote {json_out}")
            print_matrix(observations)
        if not keep:
            print()
            destroy(region, profile)
        else:
            print("\n--keep: resources left. Run `replayguard probe --destroy`.")
    return observations


def print_matrix(observations: list[Observation]) -> None:
    print("\n" + "=" * 78)
    print(f"{'scenario':<22} {'status':<11} {'verdict':<24} side effects")
    print("-" * 78)
    for o in observations:
        effects = ", ".join(f"{k}={v}" for k, v in sorted(o.side_effects.items())) or "-"
        print(f"{o.scenario:<22} {o.status or '?':<11} {o.verdict:<24} {effects}")
    print("=" * 78)
