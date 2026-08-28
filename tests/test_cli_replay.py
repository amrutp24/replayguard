"""The `replayguard replay` subcommand.

The dynamic harness itself is covered by `test_dynamic.py`. This covers the way
anyone actually reaches it -- the command line -- which was the only entirely
untested entry point in the tool. Its exit codes are the contract CI depends on:
0 clean, 1 diverged, 2 the harness could not answer.

The distinction between 1 and 2 is the important one. Collapsing "your handler
is nondeterministic" into "replayguard could not run" would make a broken import
look like a passing build.
"""

import textwrap

import pytest

pytest.importorskip("aws_durable_execution_sdk_python")
pytest.importorskip("aws_durable_execution_sdk_python_testing")

from replayguard.cli import main  # noqa: E402

DETERMINISTIC = """
from aws_durable_execution_sdk_python import DurableContext, durable_execution

@durable_execution
def handler(event, context: DurableContext):
    return context.step(lambda _: "ok", name="only-step")
"""

DIVERGENT = """
import time
from aws_durable_execution_sdk_python import DurableContext, durable_execution

@durable_execution
def handler(event, context: DurableContext):
    return context.step(lambda _: 1, name=f"op-{time.time()}")
"""


@pytest.fixture
def handler_dir(tmp_path, monkeypatch):
    """The command imports from the working directory, so tests must live in one."""

    def write(name: str, source: str) -> str:
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(source))
        return f"{name}:handler"

    monkeypatch.chdir(tmp_path)
    return write


# -- the three exit codes ----------------------------------------------------


def test_deterministic_handler_exits_zero(handler_dir, capsys):
    target = handler_dir("clean_wf", DETERMINISTIC)
    assert main(["replay", target]) == 0


def test_divergent_handler_exits_one(handler_dir, capsys):
    """A clock-named step: the perturbed run journals a different name."""
    target = handler_dir("dirty_wf", DIVERGENT)
    assert main(["replay", target]) == 1
    assert "name changed" in capsys.readouterr().out


def test_clean_run_does_not_claim_proof(handler_dir, capsys):
    """Exit 0 means "no divergence under this perturbation", not "deterministic".

    The report has to say so, because a green CI check is exactly where someone
    would otherwise read a guarantee that was never made.
    """
    handler_dir("quiet_wf", DETERMINISTIC)
    main(["replay", "quiet_wf:handler"])
    out = capsys.readouterr().out.lower()
    assert "not proof" in out or "does not prove" in out


# -- the ways it can fail to answer, all exit 2 ------------------------------


def test_target_without_a_colon_is_rejected(capsys):
    assert main(["replay", "app.orders"]) == 2
    assert "module:handler" in capsys.readouterr().err


def test_unimportable_module_exits_two(handler_dir, capsys):
    handler_dir("present", DETERMINISTIC)
    assert main(["replay", "absent:handler"]) == 2
    assert "could not import" in capsys.readouterr().err


def test_missing_attribute_exits_two(handler_dir, capsys):
    handler_dir("present", DETERMINISTIC)
    assert main(["replay", "present:no_such_handler"]) == 2
    assert "could not import" in capsys.readouterr().err


def test_malformed_event_json_exits_two(handler_dir, capsys):
    target = handler_dir("clean_wf", DETERMINISTIC)
    assert main(["replay", target, "--event", "{not json}"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_event_payload_reaches_the_handler(handler_dir, capsys):
    """--event is parsed and passed through, not merely validated.

    The handler needs a field only the event can supply, so it completes when
    the payload arrives and raises when it does not. Asserting both directions
    is what distinguishes "the event was delivered" from "the event was parsed
    and dropped", which an exit code alone cannot tell apart.
    """
    target = handler_dir(
        "echo_wf",
        """
        from aws_durable_execution_sdk_python import DurableContext, durable_execution

        @durable_execution
        def handler(event, context: DurableContext):
            return context.step(lambda _: 1, name=f"op-{event['label']}")
        """,
    )
    assert main(["replay", target, "--event", '{"label": "abc"}']) == 0
    capsys.readouterr()

    # Default event is {}, so the same handler cannot find its field.
    assert main(["replay", target]) == 2
    assert "label" in capsys.readouterr().out
