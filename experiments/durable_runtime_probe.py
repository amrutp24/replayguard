"""Does AWS Lambda accept DurableConfig on a custom runtime?

This decides whether a Rust durable execution SDK is buildable at all. The
durable execution APIs are public and `DurableConfig` carries no runtime
constraint in the service model -- but the model permitting something is not the
service accepting it, and that gap is exactly what this probe measures.

Three cells, because a bare rejection would be ambiguous:

  A  python3.13      + DurableConfig   Is durable execution available here at all?
  B  provided.al2023 + DurableConfig   THE QUESTION.
  C  provided.al2023 without           Does custom-runtime creation work at all?

Only the A/B/C comparison is interpretable. If A fails, the account or region
lacks the feature and B tells you nothing about runtime gating.

Uses botocore directly: AWS CLI v2.24.x predates durable functions and has no
--durable-config flag.

    python durable_runtime_probe.py                # discover a role, clean up
    python durable_runtime_probe.py --keep         # leave the functions behind
    ROLE_ARN=arn:aws:iam::...:role/x python durable_runtime_probe.py
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile

import boto3
from botocore.exceptions import ClientError

PREFIX = "durable-probe"
EXECUTION_TIMEOUT = 3600
RETENTION_DAYS = 1


def build_zip(kind: str) -> bytes:
    """Minimal deployable package. Never invoked -- only the package must be valid."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if kind == "provided":
            # provided.al2023 requires an executable file named `bootstrap`.
            info = zipfile.ZipInfo("bootstrap")
            info.external_attr = 0o755 << 16
            z.writestr(info, "#!/bin/sh\nwhile true; do sleep 1; done\n")
        else:
            z.writestr("handler.py", "def handler(event, context):\n    return {}\n")
    return buf.getvalue()


def discover_role(session):
    """Find any role a Lambda function could assume, newest first."""
    iam = session.client("iam")
    candidates = []
    try:
        for page in iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                doc = str(role.get("AssumeRolePolicyDocument", ""))
                if "lambda.amazonaws.com" in doc:
                    candidates.append((role["CreateDate"], role["Arn"]))
    except ClientError as exc:
        print(f"  ! cannot list roles ({exc.response['Error']['Code']})")
        return None
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]


def attempt(client, name, runtime, role, durable):
    """Returns (verdict, detail). Never raises -- the errors are the data."""
    is_provided = runtime.startswith("provided")
    kwargs = {
        "FunctionName": name,
        "Runtime": runtime,
        "Role": role,
        "Handler": "bootstrap" if is_provided else "handler.handler",
        "Code": {"ZipFile": build_zip("provided" if is_provided else "py")},
        "Architectures": ["arm64"],
        "Timeout": 30,
        "Publish": True,
    }
    if durable:
        kwargs["DurableConfig"] = {
            "ExecutionTimeout": EXECUTION_TIMEOUT,
            "RetentionPeriodInDays": RETENTION_DAYS,
        }
    try:
        resp = client.create_function(**kwargs)
        return "ACCEPTED", resp.get("FunctionArn", "")
    except ClientError as exc:
        err = exc.response["Error"]
        return f"REJECTED[{err['Code']}]", err.get("Message", "")[:400]
    except Exception as exc:
        # Parameter validation happens client-side and raises a plain exception.
        return "REJECTED[ClientSideValidation]", str(exc)[:400]


def cleanup(client, names):
    for n in names:
        try:
            client.delete_function(FunctionName=n)
            print(f"  deleted {n}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                print(f"  ! could not delete {n}: {exc.response['Error']['Code']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not delete the functions")
    ap.add_argument("--region", default=None)
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    region = session.region_name
    if not region:
        print("No region set. Use --region or set AWS_REGION.", file=sys.stderr)
        return 2

    try:
        ident = session.client("sts").get_caller_identity()
    except Exception as exc:
        print(f"No usable credentials: {exc}", file=sys.stderr)
        return 2

    client = session.client("lambda")
    print(f"account {ident['Account']}  region {region}")

    members = client.meta.service_model.operation_model("CreateFunction").input_shape.members
    if "DurableConfig" not in members:
        print("\nInstalled botocore has no DurableConfig on CreateFunction.")
        print("Upgrade first:  pip install -U botocore boto3")
        return 2

    role = os.environ.get("ROLE_ARN") or discover_role(session)
    if not role:
        print("\nNo Lambda-assumable IAM role found, and ROLE_ARN is not set.")
        print("Create one, or pass ROLE_ARN=arn:aws:iam::<acct>:role/<name>.")
        return 2
    print(f"role    {role}\n")

    cells = [
        ("A", PREFIX + "-managed-durable", "python3.13", True,
         "control: is durable execution available in this account/region?"),
        ("B", PREFIX + "-custom-durable", "provided.al2023", True,
         "THE QUESTION: custom runtime + DurableConfig"),
        ("C", PREFIX + "-custom-plain", "provided.al2023", False,
         "control: does custom-runtime creation work at all?"),
    ]

    created = []
    results = {}
    for cell, name, runtime, durable, why in cells:
        print(f"[{cell}] {runtime:<16} durable={str(durable):<5}  {why}")
        verdict, detail = attempt(client, name, runtime, role, durable)
        results[cell] = verdict
        print(f"     -> {verdict}")
        if detail and not verdict.startswith("ACCEPTED"):
            print(f"        {detail}")
        if verdict.startswith("ACCEPTED"):
            created.append(name)
        print()

    print("=" * 72)
    accepted_a = results["A"].startswith("ACCEPTED")
    accepted_b = results["B"].startswith("ACCEPTED")
    accepted_c = results["C"].startswith("ACCEPTED")

    if accepted_b:
        print("VERDICT: custom runtimes ACCEPT DurableConfig.")
        print("A Rust durable execution SDK is buildable. Next question is")
        print("whether the invoke/suspend path works, not just creation.")
    elif not accepted_a:
        print("VERDICT: INCONCLUSIVE -- the managed-runtime control also failed.")
        print("Durable execution looks unavailable in this account/region, so")
        print("cell B says nothing about runtime gating. Resolve A first.")
    elif not accepted_c:
        print("VERDICT: INCONCLUSIVE -- plain custom-runtime creation failed.")
        print("Something unrelated to durability is wrong (package, role, arch).")
    else:
        print("VERDICT: custom runtimes are GATED OUT.")
        print("Durable works on a managed runtime (A) and custom-runtime creation")
        print("works (C), but the combination is rejected. A Rust SDK is blocked")
        print("on AWS, not on implementation effort.")
    print("=" * 72)

    if created and not args.keep:
        print("\ncleanup:")
        cleanup(client, created)
    elif created:
        print(f"\nkept: {', '.join(created)}  (delete them when done)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
