"""Does a custom runtime actually participate in durable execution?

`durable_runtime_probe.py` established that Lambda accepts `DurableConfig` on
`provided.al2023`. Accepting the configuration is not the same as honouring it:
the runtime still has to be handed the durable payload at invoke time, or a Rust
SDK has nothing to work with.

The protocol, read out of the Python SDK, is plain JSON in the invoke body --
no headers, no privileged runtime hook:

    in   {"DurableExecutionArn": ..., "CheckpointToken": ...,
          "InitialExecutionState": {...}, "UpdatedOperationIds": [...]}
    out  {"Status": "SUCCEEDED" | "FAILED" | "PENDING" | "RETRY",
          "Result": ..., "Error": {...}}

`PENDING` is how a handler suspends.

So the question is answerable without a Rust toolchain: deploy a
`provided.al2023` function whose bootstrap is a shell script, return a valid
durable response, and echo the received event back inside `Result`. Echoing
beats reading CloudWatch -- the invoke response comes back directly and does not
depend on the execution role holding log permissions.

  * `DurableExecutionArn` present -> a custom runtime is a first-class durable
    participant, and a Rust SDK is an engineering problem.
  * absent                        -> `DurableConfig` is cosmetic on custom
    runtimes and the idea closes.

Creates one function and one published version, deletes both, and re-verifies
deletion rather than trusting the delete call: Lambda's list APIs are eventually
consistent and will show a deleted function for a while afterwards.

    python durable_invoke_probe.py --region us-east-1
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

NAME = "durable-invoke-probe"
LOG_GROUP = f"/aws/lambda/{NAME}"

#: Polls the Runtime API, echoes the raw event back inside a *valid* durable
#: response, and repeats. Written as a shell script because the question is what
#: the runtime is handed, which is language-independent -- no cross-compilation
#: toolchain required to answer it.
BOOTSTRAP = """#!/bin/sh
set -eu
API="http://${AWS_LAMBDA_RUNTIME_API}/2018-06-01/runtime"
while true; do
  HEADERS="$(mktemp)"
  EVENT="$(curl -sS -LD "$HEADERS" "$API/invocation/next")"
  REQ_ID="$(grep -i '^lambda-runtime-aws-request-id:' "$HEADERS" | tr -d '\\r\\n' | cut -d' ' -f2)"
  echo "PROBE_EVENT_BEGIN${EVENT}PROBE_EVENT_END"
  B64="$(printf '%s' "$EVENT" | base64 | tr -d '\\n')"
  curl -sS -X POST "$API/invocation/$REQ_ID/response" \\
       -d "{\\"Status\\":\\"SUCCEEDED\\",\\"Result\\":\\"$B64\\"}" >/dev/null
done
"""


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("bootstrap")
        info.external_attr = 0o755 << 16  # must be executable
        z.writestr(info, BOOTSTRAP)
    return buf.getvalue()


def discover_role(session) -> str | None:
    iam = session.client("iam")
    candidates = []
    try:
        for page in iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                if "lambda.amazonaws.com" in str(role.get("AssumeRolePolicyDocument", "")):
                    candidates.append((role["CreateDate"], role["Arn"]))
    except ClientError as exc:
        print(f"  ! cannot list roles ({exc.response['Error']['Code']})")
        return None
    return sorted(candidates, reverse=True)[0][1] if candidates else None


def cleanup(lam, logs, verify: bool = True) -> None:
    print("\ncleanup:")
    for label, fn in (
        ("function", lambda: lam.delete_function(FunctionName=NAME)),
        ("log group", lambda: logs.delete_log_group(logGroupName=LOG_GROUP)),
    ):
        try:
            fn()
            print(f"  deleted {label}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"  {label} already absent")
            else:
                print(f"  ! could not delete {label}: {code}")

    if not verify:
        return
    # Lambda's list APIs lag behind deletes, so confirm with a direct get and
    # give it a few seconds rather than reporting a false leftover.
    for _ in range(8):
        try:
            lam.get_function(FunctionName=NAME)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                print("  verified: function is gone")
                return
        time.sleep(2)
    print("  ! function still present after cleanup - check manually")


def read_log_events(logs, since_ms: int) -> list[str]:
    """Fallback: pull the echoed event out of CloudWatch."""
    out: list[str] = []
    for _ in range(8):
        try:
            streams = logs.describe_log_streams(
                logGroupName=LOG_GROUP, orderBy="LastEventTime", descending=True, limit=5
            ).get("logStreams", [])
        except ClientError:
            time.sleep(3)
            continue
        for stream in streams:
            try:
                events = logs.get_log_events(
                    logGroupName=LOG_GROUP,
                    logStreamName=stream["logStreamName"],
                    startTime=since_ms,
                    startFromHead=True,
                ).get("events", [])
            except ClientError:
                continue
            for e in events:
                msg = e.get("message", "")
                if "PROBE_EVENT_BEGIN" in msg:
                    out.append(
                        msg.split("PROBE_EVENT_BEGIN", 1)[1].split("PROBE_EVENT_END", 1)[0]
                    )
        if out:
            return out
        time.sleep(3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    try:
        ident = session.client("sts").get_caller_identity()
    except Exception as exc:
        print(f"No usable credentials: {exc}", file=sys.stderr)
        return 2

    lam = session.client("lambda")
    logs = session.client("logs")
    print(f"account {ident['Account']}  region {args.region}")

    role = os.environ.get("ROLE_ARN") or discover_role(session)
    if not role:
        print("No Lambda-assumable role found; set ROLE_ARN.", file=sys.stderr)
        return 2
    print(f"role    {role}")

    cleanup(lam, logs, verify=False)  # clear anything a previous run left

    print("\ncreating provided.al2023 function with DurableConfig ...")
    try:
        created = lam.create_function(
            FunctionName=NAME,
            Runtime="provided.al2023",
            Role=role,
            Handler="bootstrap",
            Code={"ZipFile": build_zip()},
            Architectures=["arm64"],
            Timeout=60,
            Publish=True,  # durable executions resume against a pinned version
            DurableConfig={"ExecutionTimeout": 900, "RetentionPeriodInDays": 1},
        )
    except ClientError as exc:
        err = exc.response["Error"]
        print(f"  create failed: {err['Code']}: {err.get('Message', '')[:300]}")
        return 1

    version = created.get("Version", "$LATEST")
    print(f"  created, version {version}")
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME)

    since_ms = int(time.time() * 1000) - 5000
    qualified = f"{NAME}:{version}" if version != "$LATEST" else NAME
    print(f"\ninvoking {qualified} ...")

    invoke_result: str | None = None
    try:
        resp = lam.invoke(FunctionName=qualified, Payload=b'{"probe": true}')
        invoke_result = resp["Payload"].read().decode("utf8", "replace")
        print(f"  status {resp['StatusCode']}  functionError={resp.get('FunctionError')}")
        print(f"  response {invoke_result[:200]}")
    except ClientError as exc:
        err = exc.response["Error"]
        print(f"  invoke failed: {err['Code']}: {err.get('Message', '')[:300]}")

    print("\nreading what the runtime was handed ...")
    events: list[str] = []
    if invoke_result:
        # Lambda unwraps the durable response and returns `Result` on its own,
        # so the payload is the bare base64 rather than the {"Status": ...}
        # envelope the runtime posted. Handle both, plus a JSON-quoted string.
        candidate = invoke_result.strip()
        try:
            outer = json.loads(candidate)
            if isinstance(outer, dict):
                candidate = outer.get("Result") or ""
            elif isinstance(outer, str):
                candidate = outer
        except json.JSONDecodeError:
            pass
        candidate = candidate.strip().strip('"')
        if candidate:
            try:
                padded = candidate + "=" * (-len(candidate) % 4)
                events.append(base64.b64decode(padded).decode("utf8", "replace"))
            except (ValueError, UnicodeDecodeError) as exc:
                print(f"  could not decode the echoed event ({exc}); trying logs")
    if not events:
        events = read_log_events(logs, since_ms)

    verdict_durable = False
    if not events:
        print("  no probe output captured")
    for raw in events:
        print(f"  event: {raw[:600]}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "DurableExecutionArn" in parsed:
            verdict_durable = True

    print("\n" + "=" * 72)
    if verdict_durable:
        print("VERDICT: the custom runtime RECEIVES durable execution context.")
        print("DurableExecutionArn and CheckpointToken arrive in the event payload,")
        print("so a Rust runtime can participate. The SDK becomes an engineering")
        print("problem rather than a research question.")
    elif events:
        print("VERDICT: invoked, but the runtime received a PLAIN event.")
        print("No DurableExecutionArn in the payload, so DurableConfig is accepted")
        print("but not honoured on provided.al2023.")
    else:
        print("VERDICT: INCONCLUSIVE -- no probe output was captured.")
    print("=" * 72)

    if not args.keep:
        cleanup(lam, logs)
    else:
        print(f"\nkept {NAME} (delete it when done)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
