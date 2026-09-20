"""Build and deploy the function under test.

The SDK is bundled into the deployment package rather than assumed present.
That is what makes an SDK-drift scenario possible at all: the harness controls
which version the function runs, so it can change it underneath a suspended
execution and watch what happens. The wheels come from the same cache the
offline `diff` uses, so `diff 1.7.0 2.0.0` and a live 1.7.0-to-2.0.0 scenario
are demonstrably talking about the same two artifacts.

Whether the managed runtime *also* provides an SDK is a separate question with
its own scenario (D00), because if it does, a bundled copy may or may not win.
"""

from __future__ import annotations

import io
import shutil
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from replayguard.drift.fetch import fetch
from replayguard.probe.handler_template import render

DEFAULT_RUNTIME = "python3.13"
DEFAULT_SDK = "1.7.0"
SDK_PACKAGE = "aws-durable-execution-sdk-python"


def build_zip(
    *,
    body: str,
    wait_seconds: int,
    sdk_version: str | None,
    cache_dir: Path | None = None,
) -> bytes:
    """A deployment package: the handler, plus a pinned SDK unless told not to.

    `sdk_version=None` bundles nothing, which is how D00 asks whether the
    managed runtime supplies its own.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "handler.py",
            render(body, wait_seconds=wait_seconds, report_sdk_version=sdk_version is not None),
        )
        if sdk_version is not None:
            package_dir = fetch(SDK_PACKAGE, sdk_version, cache_dir=cache_dir)
            root = package_dir.parent
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                # dist-info carries the version metadata, which is worth keeping:
                # it is how `pip show` inside the sandbox would answer, and a
                # scenario that swaps SDKs should swap that too.
                if rel.endswith((".pyc", ".zip")) or "__pycache__" in rel:
                    continue
                z.write(path, rel)
    return buf.getvalue()


def deploy(
    infra,
    name: str,
    *,
    body: str = "baseline",
    wait_seconds: int = 90,
    sdk_version: str | None = DEFAULT_SDK,
    runtime: str = DEFAULT_RUNTIME,
    memory: int = 512,
    probe: str = "initial",
    publish: bool = True,
    cache_dir: Path | None = None,
) -> dict:
    """Create or update the function, returning the create/update response."""
    code = build_zip(
        body=body, wait_seconds=wait_seconds, sdk_version=sdk_version, cache_dir=cache_dir
    )
    env = {
        "RD_TABLE": infra.table,
        "RD_VARIANT": body,
        "RD_PROBE": probe,
    }

    try:
        infra.lam.get_function(FunctionName=name)
        exists = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        exists = False

    if not exists:
        resp = infra.lam.create_function(
            FunctionName=name,
            Runtime=runtime,
            Role=infra.role_arn,
            Handler="handler.handler",
            Code={"ZipFile": code},
            Architectures=["arm64"],
            Timeout=120,
            MemorySize=memory,
            Publish=publish,
            Environment={"Variables": env},
            DurableConfig={"ExecutionTimeout": 3600, "RetentionPeriodInDays": 1},
        )
    else:
        infra.lam.update_function_code(FunctionName=name, ZipFile=code, Publish=False)
        _wait_updated(infra, name)
        infra.lam.update_function_configuration(
            FunctionName=name,
            Runtime=runtime,
            MemorySize=memory,
            Environment={"Variables": env},
        )
        _wait_updated(infra, name)
        resp = (
            infra.lam.publish_version(FunctionName=name)
            if publish
            else infra.lam.get_function_configuration(FunctionName=name)
        )

    infra.lam.get_waiter("function_active_v2").wait(FunctionName=name)
    _wait_updated(infra, name)
    return resp


def _wait_updated(infra, name: str, attempts: int = 40) -> None:
    """Block until the function is not mid-update.

    Lambda rejects a second change while one is in flight, and a scenario that
    mutates a function twice in quick succession hits this reliably.
    """
    for _ in range(attempts):
        cfg = infra.lam.get_function_configuration(FunctionName=name)
        if cfg.get("LastUpdateStatus") != "InProgress":
            return
        time.sleep(1.5)


def set_alias(infra, name: str, alias: str, version: str) -> str:
    """Point `alias` at `version`, creating it if needed. Returns the alias ARN."""
    try:
        resp = infra.lam.update_alias(
            FunctionName=name, Name=alias, FunctionVersion=version
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        resp = infra.lam.create_alias(
            FunctionName=name, Name=alias, FunctionVersion=version
        )
    return resp["AliasArn"]


def clear_cache(cache_dir: Path | None = None) -> None:
    from replayguard.drift.fetch import DEFAULT_CACHE

    shutil.rmtree(Path(cache_dir or DEFAULT_CACHE), ignore_errors=True)
