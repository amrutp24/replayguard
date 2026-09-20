"""Account-side resources the live harness needs, and their teardown.

Everything created here is prefixed `rd-`, so a stray `aws lambda list-functions`
makes it obvious what came from this tool and what did not.

Teardown is not optional and not best-effort. `destroy()` verifies with a direct
get rather than trusting the delete call, because Lambda's list APIs lag deletes
by a noticeable margin and "it is not in the list" is not evidence that it is
gone. The one resource worth being strict about is the IAM role: a leftover
function is inert, a leftover role is standing permissions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "the live harness needs boto3: pip install 'replayguard[live]'"
    ) from exc


PREFIX = "rd-"
ROLE_NAME = f"{PREFIX}harness-role"
TABLE_NAME = f"{PREFIX}side-effects"

#: Checkpointing needs lambda:CheckpointDurableExecution, which only the durable
#: policy grants. With the basic execution role alone the failure surfaces
#: *after* the function has already suspended, which reads like a bug in the
#: handler rather than a missing permission.
MANAGED_POLICIES = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy",
)

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

#: The handler's side-effect counter. Scoped to the one table by name so the
#: role cannot touch anything else in the account.
INLINE_POLICY_NAME = f"{PREFIX}side-effect-table"


@dataclass
class Infra:
    session: boto3.Session
    region: str
    account: str
    role_arn: str
    table: str

    @property
    def lam(self):
        return self.session.client("lambda")

    @property
    def ddb(self):
        return self.session.client("dynamodb")

    @property
    def logs(self):
        return self.session.client("logs")


def connect(region: str, profile: str | None = None) -> tuple[boto3.Session, str]:
    session = boto3.Session(region_name=region, profile_name=profile)
    account = session.client("sts").get_caller_identity()["Account"]
    return session, account


def provision(region: str, profile: str | None = None, *, quiet: bool = False) -> Infra:
    session, account = connect(region, profile)
    say = (lambda *a: None) if quiet else print

    say(f"account {account}  region {region}")

    table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{TABLE_NAME}"
    role_arn = _ensure_role(session, table_arn, say)
    _ensure_table(session, say)

    return Infra(
        session=session,
        region=region,
        account=account,
        role_arn=role_arn,
        table=TABLE_NAME,
    )


def _ensure_role(session, table_arn: str, say) -> str:
    iam = session.client("iam")
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        say(f"  role {ROLE_NAME} exists")
        return role["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise

    arn = iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(TRUST),
        Description="replayguard drift probe (delete with: replayguard probe --destroy)",
    )["Role"]["Arn"]
    for policy in MANAGED_POLICIES:
        iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy)
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
                        "Resource": table_arn,
                    }
                ],
            }
        ),
    )
    say(f"  created role {ROLE_NAME}")
    # IAM is eventually consistent and Lambda rejects a role it cannot yet see.
    # The failure looks like a permissions bug rather than a race, which costs
    # more time to diagnose than the wait costs to take.
    say("  waiting 15s for IAM propagation ...")
    time.sleep(15)
    return arn


def _ensure_table(session, say) -> None:
    ddb = session.client("dynamodb")
    try:
        ddb.describe_table(TableName=TABLE_NAME)
        say(f"  table {TABLE_NAME} exists")
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    ddb.create_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "label", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "label", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    say(f"  created table {TABLE_NAME}")


def destroy(region: str, profile: str | None = None) -> bool:
    """Delete everything this tool created. Returns True if the account is clean."""
    session, account = connect(region, profile)
    lam = session.client("lambda")
    iam = session.client("iam")
    ddb = session.client("dynamodb")
    logs = session.client("logs")
    clean = True

    print(f"account {account}  region {region}")

    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        functions += [
            f["FunctionName"]
            for f in page["Functions"]
            if f["FunctionName"].startswith(PREFIX)
        ]

    for name in functions:
        try:
            lam.delete_function(FunctionName=name)
            print(f"  deleted function {name}")
        except ClientError as exc:
            print(f"  ! function {name}: {exc.response['Error']['Code']}")
            clean = False
        try:
            logs.delete_log_group(logGroupName=f"/aws/lambda/{name}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                print(f"  ! log group for {name}: {exc.response['Error']['Code']}")

    try:
        ddb.delete_table(TableName=TABLE_NAME)
        print(f"  deleted table {TABLE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"  ! table: {exc.response['Error']['Code']}")
            clean = False

    try:
        for p in iam.list_attached_role_policies(RoleName=ROLE_NAME).get(
            "AttachedPolicies", []
        ):
            iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=p["PolicyArn"])
        for p in iam.list_role_policies(RoleName=ROLE_NAME).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=p)
        iam.delete_role(RoleName=ROLE_NAME)
        print(f"  deleted role {ROLE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            print(f"  ! role: {exc.response['Error']['Code']}")
            clean = False

    clean = _verify_gone(lam, iam, functions) and clean
    print("  verified: account is clean" if clean else "  ! something survived teardown")
    return clean


#: How long to let a delete propagate before calling a resource a survivor.
#: An earlier 16s was too short and reported three functions as leftovers that
#: a direct get confirmed were already gone. A teardown check that cries wolf
#: gets ignored, which costs more than the extra wait.
_DELETE_PROPAGATION_S = 90


def _verify_gone(lam, iam, functions: list[str]) -> bool:
    """Confirm by direct get, not by absence from a list.

    Lambda's list APIs keep showing deleted functions for a while, so a teardown
    that trusts the list can report success over resources that are still there.
    That is the one failure this project cannot tolerate -- it is the promise
    made to anyone who deploys this into a personal account.

    The opposite error matters too, and cost a false alarm to learn: a
    `get_function` can keep answering for a while after the delete is accepted.
    Reporting a survivor that is not there is still a wrong result, so this
    waits properly and says how long it waited.
    """
    ok = True
    for name in functions:
        deadline = time.time() + _DELETE_PROPAGATION_S
        started = time.time()
        while time.time() < deadline:
            try:
                lam.get_function(FunctionName=name)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    break
            time.sleep(3)
        else:
            print(
                f"  ! function {name} still answering get_function after "
                f"{_DELETE_PROPAGATION_S}s - check manually"
            )
            ok = False
            continue
        waited = time.time() - started
        if waited > 10:
            print(f"    ({name} took {waited:.0f}s to stop answering)")

    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"  ! role {ROLE_NAME} still present after teardown")
        ok = False
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            ok = False
    return ok
