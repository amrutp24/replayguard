"""Can a custom runtime suspend and be resumed against its checkpoints?

The last unproven step. Earlier probes established that Lambda accepts
`DurableConfig` on `provided.al2023`, and that such a function is handed a real
`DurableExecutionArn` and `CheckpointToken` at invoke time. Neither shows that
the suspend/resume cycle -- the entire point of durable execution -- reaches a
custom runtime.

Two findings shaped this design, both from failed earlier attempts:

  * Returning `{"Status": "PENDING"}` with nothing outstanding is rejected:
    *"Cannot return PENDING status with no pending operations."* The operation
    has to be checkpointed *before* the handler suspends, so checkpointing from
    outside afterwards cannot work -- and indeed the token is spent by then
    (*"Invalid checkpoint token"*).
  * `provided.al2023` ships curl 8.17 with `--aws-sigv4`, and Lambda injects
    credentials as environment variables. So a shell bootstrap can sign its own
    `CheckpointDurableExecution` call, and no Rust toolchain is needed to answer
    the question.

Sequence:

  1. invoke -> the function checkpoints a WAIT, then returns PENDING
  2. the timer fires -> the platform re-invokes it
  3. the second invoke carries the WAIT in InitialExecutionState, and the
     function returns SUCCEEDED

Two invocations, the second carrying state the first created, is the proof.

The bootstrap decides what to return by looking for the operation id in its
event rather than counting invocations in /tmp: a resume can land on a fresh
execution environment, where a counter would reset and it would suspend forever.

Needs a role with CloudWatch Logs permissions. Creates one function, one log
group and one version, deletes all of it, and verifies deletion with a direct
get -- Lambda's list APIs lag well behind deletes.

    python durable_suspend_resume_probe.py --region us-east-1
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

NAME = "durable-suspend-probe"
LOG_GROUP = f"/aws/lambda/{NAME}"
WAIT_OP_ID = "rg-wait-1"
WAIT_SECONDS = 5
ROLE_NAME = "durable-suspend-probe-role"

#: Rule 3 of SWA-001, hit live: AWSLambdaBasicExecutionRole alone is not enough.
#: Checkpointing needs lambda:CheckpointDurableExecution, which only the durable
#: policy grants, and the failure surfaces *after* the function has suspended.
MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy",
]

TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

#: Raw string throughout: the sed expressions and the JSON escapes both need
#: their backslashes to survive into the shell verbatim.
BOOTSTRAP = r"""#!/bin/sh
set -eu
API="http://${AWS_LAMBDA_RUNTIME_API}/2018-06-01/runtime"
REGION="${AWS_REGION:-us-east-1}"
while true; do
  H="$(mktemp)"
  EVENT="$(curl -sS -LD "$H" "$API/invocation/next")"
  RID="$(grep -i '^lambda-runtime-aws-request-id:' "$H" | tr -d '\r\n' | cut -d' ' -f2)"
  echo "RGEVENT $(printf '%s' "$EVENT" | base64 | tr -d '\n')"

  case "$EVENT" in
    *__OPID__*)
      # Resumed: our checkpointed WAIT is in the state we were handed, so finish.
      BODY='{"Status":"SUCCEEDED","Result":"\"resumed\""}'
      ;;
    *)
      # First pass. Lambda refuses PENDING with nothing outstanding, so the WAIT
      # must be checkpointed before suspending. curl signs this itself.
      ARN="$(printf '%s' "$EVENT" | sed -n 's/.*"DurableExecutionArn":"\([^"]*\)".*/\1/p')"
      TOK="$(printf '%s' "$EVENT" | sed -n 's/.*"CheckpointToken":"\([^"]*\)".*/\1/p')"
      AENC="$(printf '%s' "$ARN" | sed -e 's/:/%3A/g' -e 's|/|%2F|g')"
      CPB='{"CheckpointToken":"'"$TOK"'","Updates":[{"Id":"__OPID__","Type":"WAIT","Action":"START","Name":"resume-trigger","WaitOptions":{"WaitSeconds":__SECS__}}]}'
      # Credentials go in through a config file on stdin rather than on the
      # command line. argv is readable via /proc; a Lambda sandbox is
      # single-tenant so the real risk here is slight, but this is example code
      # and secrets-on-argv is a bad habit to hand to anyone who copies it.
      OUT="$(printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" \
        | curl -sS -X POST -K - \
        --aws-sigv4 "aws:amz:$REGION:lambda" \
        -H "x-amz-security-token: ${AWS_SESSION_TOKEN:-}" \
        -H "content-type: application/json" \
        -d "$CPB" \
        "https://lambda.$REGION.amazonaws.com/2025-12-01/durable-executions/$AENC/checkpoint" 2>&1 || true)"
      echo "RGCP $(printf '%s' "$OUT" | base64 | tr -d '\n')"
      BODY='{"Status":"PENDING"}'
      ;;
  esac
  curl -sS -X POST "$API/invocation/$RID/response" -d "$BODY" >/dev/null
done
""".replace("__OPID__", WAIT_OP_ID).replace("__SECS__", str(WAIT_SECONDS))


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("bootstrap")
        info.external_attr = 0o755 << 16
        z.writestr(info, BOOTSTRAP)
    return buf.getvalue()


def ensure_role(session) -> str | None:
    """Create a dedicated role with the durable policy attached.

    A temporary role of its own rather than modifying an existing one: the
    account's other Lambda roles belong to unrelated stacks, and attaching
    permissions to those would outlive this experiment.
    """
    iam = session.client("iam")
    delete_role(session, quiet=True)
    try:
        arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST),
            Description="Temporary role for replayguard durable suspend/resume probe",
        )["Role"]["Arn"]
        for policy in MANAGED_POLICIES:
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy)
        print(f"  created role {ROLE_NAME} with:")
        for policy in MANAGED_POLICIES:
            print(f"    {policy.rsplit('/', 1)[-1]}")
        # IAM is eventually consistent; Lambda will reject a role it cannot yet
        # see, and the failure looks like a permissions bug rather than a race.
        print("  waiting 15s for IAM propagation ...")
        time.sleep(15)
        return arn
    except ClientError as exc:
        print(f"  ! could not create role: {exc.response['Error']['Code']}")
        return None


def delete_role(session, quiet: bool = False) -> None:
    iam = session.client("iam")
    try:
        for p in iam.list_attached_role_policies(RoleName=ROLE_NAME).get(
            "AttachedPolicies", []
        ):
            iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=ROLE_NAME)
        if not quiet:
            print(f"  deleted role {ROLE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity" and not quiet:
            print(f"  ! could not delete role: {exc.response['Error']['Code']}")


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
            print(
                f"  {label} already absent"
                if code == "ResourceNotFoundException"
                else f"  ! could not delete {label}: {code}"
            )
    if not verify:
        return
    for _ in range(8):
        try:
            lam.get_function(FunctionName=NAME)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                print("  verified: function is gone")
                return
        time.sleep(2)
    print("  ! function still present after cleanup - check manually")


def _decode(b64: str) -> str | None:
    try:
        return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf8", "replace")
    except ValueError:
        return None


def collect(logs, since_ms: int, marker: str, want: int, deadline_s: int) -> list[str]:
    """Every `marker` line the function logged, decoded, oldest first."""
    seen: dict[str, str] = {}
    end = time.time() + deadline_s
    while True:
        try:
            streams = logs.describe_log_streams(
                logGroupName=LOG_GROUP, orderBy="LastEventTime", descending=True, limit=10
            ).get("logStreams", [])
        except ClientError:
            streams = []
        for stream in streams:
            token = None
            while True:
                kwargs = {
                    "logGroupName": LOG_GROUP,
                    "logStreamName": stream["logStreamName"],
                    "startTime": since_ms,
                    "startFromHead": True,
                }
                if token:
                    kwargs["nextToken"] = token
                try:
                    page = logs.get_log_events(**kwargs)
                except ClientError:
                    break
                for e in page.get("events", []):
                    msg = e.get("message", "")
                    if f"{marker} " not in msg:
                        continue
                    b64 = msg.split(f"{marker} ", 1)[1].strip()
                    decoded = _decode(b64)
                    if decoded is not None:
                        seen[f"{e.get('timestamp')}:{b64[:40]}"] = decoded
                nxt = page.get("nextForwardToken")
                if nxt == token or not page.get("events"):
                    break
                token = nxt
        if len(seen) >= want or time.time() >= end:
            break
        time.sleep(3)
    return [seen[k] for k in sorted(seen)]


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

    lam, logs = session.client("lambda"), session.client("logs")
    print(f"account {ident['Account']}  region {args.region}")

    print("\nprovisioning a temporary role ...")
    role = os.environ.get("ROLE_ARN") or ensure_role(session)
    if not role:
        print("Could not obtain a role; set ROLE_ARN.", file=sys.stderr)
        return 2
    print(f"  role {role}")

    cleanup(lam, logs, verify=False)

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
            Publish=True,
            DurableConfig={"ExecutionTimeout": 300, "RetentionPeriodInDays": 1},
        )
    except ClientError as exc:
        err = exc.response["Error"]
        print(f"  create failed: {err['Code']}: {err.get('Message', '')[:300]}")
        return 1

    version = created.get("Version", "$LATEST")
    print(f"  created, version {version}")
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME)

    since_ms = int(time.time() * 1000) - 5000
    qualified = f"{NAME}:{version}"

    print(f"\n[1] invoking {qualified} -- it should checkpoint a WAIT, then suspend")
    try:
        resp = lam.invoke(FunctionName=qualified, Payload=b'{"probe": true}')
        payload = resp["Payload"].read().decode("utf8", "replace")
        print(f"    status {resp['StatusCode']} functionError={resp.get('FunctionError')}")
        print(f"    payload {payload[:200]}")
    except ClientError as exc:
        err = exc.response["Error"]
        print(f"    invoke failed: {err['Code']}: {err.get('Message', '')[:200]}")

    print("\n[2] what did its own checkpoint call return?")
    for out in collect(logs, since_ms, "RGCP", want=1, deadline_s=90) or ["(nothing logged)"]:
        print(f"    {out[:300]}")

    print(f"\n[3] waiting for the {WAIT_SECONDS}s timer and a second invocation ...")
    raw_events = collect(logs, since_ms, "RGEVENT", want=2, deadline_s=180)
    events = []
    for raw in raw_events:
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    print(f"    invocations observed: {len(events)}")

    resumed_with_state = False
    for i, e in enumerate(events, 1):
        ops = (e.get("InitialExecutionState") or {}).get("Operations") or []
        ids = [o.get("Id") for o in ops if isinstance(o, dict)]
        updated = e.get("UpdatedOperationIds") or []
        print(f"    invoke {i}: operations={ids or '[]'}  updated={updated or '[]'}")
        if i > 1 and (WAIT_OP_ID in ids or WAIT_OP_ID in updated):
            resumed_with_state = True

    try:
        arn = events[0].get("DurableExecutionArn") if events else None
        if arn:
            got = lam.get_durable_execution(DurableExecutionArn=arn)
            print(f"    execution status: {got.get('Status')}  result={str(got.get('Result'))[:60]}")
    except ClientError as exc:
        print(f"    could not read execution: {exc.response['Error']['Code']}")

    print("\n" + "=" * 72)
    if resumed_with_state:
        print("VERDICT: a custom runtime SUSPENDS and RESUMES against its checkpoints.")
        print("It checkpointed a WAIT, returned PENDING, and the platform re-invoked")
        print("it after the timer fired with that operation in its state. The full")
        print("durable cycle works on provided.al2023.")
    elif len(events) > 1:
        print("VERDICT: re-invoked, but without the checkpointed operation in state.")
        print("Suspend and resume work; the replay contract needs more digging.")
    else:
        print("VERDICT: resume NOT observed. See the checkpoint response above --")
        print("if it was rejected, that is the reason, and this is inconclusive")
        print("rather than a negative result.")
    print("=" * 72)

    if not args.keep:
        # Role first. The function-verify loop below can take 16s, and the role
        # is the one resource that lingers if this process dies partway -- a
        # leftover function is inert, a leftover role is standing permissions.
        delete_role(session)
        cleanup(lam, logs)
    else:
        print(f"\nkept {NAME} and {ROLE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
