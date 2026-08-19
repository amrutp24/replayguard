"""Deliberately broken durable handler — every rule should fire at least once.

Modeled on the failure cases AWS documents plus the shape of a real
human-in-the-loop pipeline handler.
"""

import random
import time
import uuid
from datetime import datetime

import boto3
import requests
from aws_durable_execution_sdk_python import DurableContext, durable_execution

table = boto3.resource("dynamodb").Table("orders")
AUDIT = []


@durable_execution
def lambda_handler(event, context: DurableContext):
    run_id = event["run_id"]

    # RG001 — clock outside a step
    started_at = time.time()

    # RG001 — identity outside a step; downstream steps get a different id on replay
    correlation_id = uuid.uuid4()

    # RG001 — clock via a from-import alias, which a regex checker would miss
    stamp = datetime.now()

    # RG001 — random outside a step
    jitter = random.random()

    # RG002 — external I/O outside a step, re-executed on every replay
    profile = requests.get(f"https://example.internal/profile/{run_id}")

    # RG002 — AWS SDK call outside a step
    table.put_item(Item={"id": run_id, "started": started_at})

    # RG004 — branch on a nondeterministic value; replay may take the other path
    if datetime.now().hour < 12:
        shift = "morning"
    else:
        shift = "afternoon"

    receipts = []

    # RG003 — step body mutating a captured list; silently lost on replay
    def save(_):
        receipt = table.put_item(Item={"id": run_id})
        receipts.append(receipt)
        AUDIT.append(run_id)
        return "saved"

    context.step(save, name="save-order")

    # RG003 — same bug via a lambda and a subscript write
    state = {}

    context.step(
        lambda _: state.__setitem__("done", True),
        name="mark-done",
    )

    # RG005 — dynamically constructed step name cannot be matched on resume
    context.step(lambda _: "work", name=f"process-{time.time()}")

    # RG900 — step body passed by reference, so its region is unresolved
    context.step(_helper, name="helper")

    return {"run_id": run_id, "shift": shift, "jitter": jitter, "profile": profile}


def _helper(_):
    return "helper"
