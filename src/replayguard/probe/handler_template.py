"""Source template for the durable function under test.

This is not imported. It is read as text, substituted, and zipped into a Lambda
deployment package, so it must stand alone and may only import what the runtime
and the bundled SDK provide.

The design constraint that shapes it: after a scenario runs, we need to know
whether a completed step's body *ran again*. A return value cannot tell us that,
because replay returns the checkpointed value whether or not the body executed.
So each step performs a real, external, atomic side effect -- a DynamoDB counter
increment -- and the count afterwards is the evidence. One means the checkpoint
was honoured. Two means the side effect was re-executed, which is the silent
corruption everyone is worried about and nobody has measured.

Placeholders in __UPPER__ are substituted by deploy.py.
"""

TEMPLATE = '''
import json
import os

import boto3

from aws_durable_execution_sdk_python import (
    DurableContext,
    StepContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import Duration

TABLE = os.environ["RD_TABLE"]
VARIANT = os.environ.get("RD_VARIANT", "unset")

# Read at module scope on purpose. This is outside any step, so it is re-read on
# every replay -- which is exactly the thing the determinism guidance tells you
# not to do, and scenario D10 exists to find out what the platform does when the
# value changes between the first invocation and the replay.
DRIFT_PROBE = os.environ.get("RD_PROBE", "unset")

_ddb = boto3.client("dynamodb")


def _bump(run_id, label):
    """Atomically count one execution of a step body.

    ADD is used rather than PUT so that a second execution is visible as a
    count of 2 rather than overwriting the first and looking identical to it.
    """
    _ddb.update_item(
        TableName=TABLE,
        Key={"run_id": {"S": run_id}, "label": {"S": label}},
        UpdateExpression="ADD hits :one SET variant = :v, probe = :p",
        ExpressionAttributeValues={
            ":one": {"N": "1"},
            ":v": {"S": VARIANT},
            ":p": {"S": DRIFT_PROBE},
        },
    )


@durable_step
def record(step_ctx: StepContext, run_id: str, label: str) -> dict:
    _bump(run_id, label)
    return {"label": label, "variant": VARIANT}


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    run_id = event["run_id"]

    __BODY__

    return {
        "ok": True,
        "variant": VARIANT,
        "probe": DRIFT_PROBE,
        "sdk": __SDK_VERSION_EXPR__,
    }
'''

#: The baseline body: a step, a suspend long enough for the harness to change
#: something underneath it, then a second step. Every code-drift variant is a
#: mutation of these three lines.
BODY_BASELINE = '''
    context.step(record(run_id, "first"), name="step_one")
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
'''

#: D02 -- the completed step keeps its code and loses its name. Every guide says
#: not to do this. None of them says what happens.
BODY_STEP_RENAMED = '''
    context.step(record(run_id, "first"), name="step_one_renamed")
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
'''

#: D03 -- a new step appears *before* one that has already checkpointed, so the
#: replay reaches a step the history does not have at that position.
BODY_STEP_INSERTED = '''
    context.step(record(run_id, "inserted"), name="step_zero")
    context.step(record(run_id, "first"), name="step_one")
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
'''

#: D04 -- a completed step is deleted. The history has a checkpoint the code no
#: longer accounts for.
BODY_STEP_REMOVED = '''
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
'''

#: D05 -- same steps, swapped order.
BODY_STEP_REORDERED = '''
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
    context.step(record(run_id, "first"), name="step_one")
'''

#: D06 -- the control for all of the above. Code outside any step changes, the
#: steps and their names do not. This *should* be safe; a failure here would be
#: the most interesting result in the matrix, because it would mean even a pure
#: refactor is unsafe mid-flight.
BODY_NONSTEP_REFACTOR = '''
    _unused_local = {"refactored": True, "harmless": [1, 2, 3]}
    _also_unused = ",".join(str(n) for n in _unused_local["harmless"])
    context.step(record(run_id, "first"), name="step_one")
    context.wait(duration=Duration.from_seconds(__WAIT__), name="pause")
    context.step(record(run_id, "second"), name="step_two")
'''

BODIES = {
    "baseline": BODY_BASELINE,
    "step_renamed": BODY_STEP_RENAMED,
    "step_inserted": BODY_STEP_INSERTED,
    "step_removed": BODY_STEP_REMOVED,
    "step_reordered": BODY_STEP_REORDERED,
    "nonstep_refactor": BODY_NONSTEP_REFACTOR,
}


def render(body: str, *, wait_seconds: int, report_sdk_version: bool = True) -> str:
    """Produce deployable handler source."""
    if body not in BODIES:
        raise KeyError(f"unknown body {body!r}; have {', '.join(sorted(BODIES))}")

    # Reporting the SDK version from inside the function is how the harness
    # learns which SDK actually ran, rather than which one it believes it
    # bundled. Those differ if the managed runtime supplies its own, which is
    # itself one of the things being measured.
    sdk_expr = (
        "__import__('aws_durable_execution_sdk_python').__version__"
        if report_sdk_version
        else "'unreported'"
    )

    return TEMPLATE.replace(
        "__BODY__", BODIES[body].replace("__WAIT__", str(wait_seconds)).strip()
    ).replace("__SDK_VERSION_EXPR__", sdk_expr)
