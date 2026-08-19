"""A correct durable handler — the checker must report nothing on this file.

Structurally close to the bad fixture on purpose: same work, same shape, but
every nondeterministic operation is checkpointed. If a rule fires here it is a
false positive, which for a linter is the failure that gets it uninstalled.
"""

import time
import uuid

import boto3
from aws_durable_execution_sdk_python import DurableContext, durable_execution

table = boto3.resource("dynamodb").Table("orders")


@durable_execution
def lambda_handler(event, context: DurableContext):
    run_id = event["run_id"]

    # Clock and identity are checkpointed, so replay reuses the original values.
    started_at = context.step(lambda _: time.time(), name="started-at")
    correlation_id = context.step(lambda _: str(uuid.uuid4()), name="correlation-id")

    # The decision is checkpointed, so control flow is stable across replay.
    shift = context.step(
        lambda _: "morning" if time.localtime().tm_hour < 12 else "afternoon",
        name="pick-shift",
    )

    # Values come back through return values and are assigned in the handler,
    # where replay rebuilds them from checkpointed results.
    receipts = []
    receipt_id = context.step(
        lambda _: table.put_item(Item={"id": run_id, "started": started_at}),
        name="save-order",
    )
    receipts.append(receipt_id)

    # Branching on checkpointed data is fine.
    if shift == "morning":
        context.step(lambda _: "morning-work", name="morning-work")
    else:
        context.step(lambda _: "afternoon-work", name="afternoon-work")

    # Loop suffixes derived from a stable index, not from a clock.
    for index, item in enumerate(event.get("items", [])):
        context.step(lambda _, i=item: str(i), name=f"item-{index}")

    return {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "receipts": receipts,
    }
