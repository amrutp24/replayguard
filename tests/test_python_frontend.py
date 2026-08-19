from pathlib import Path

import pytest

from replayguard import rules
from replayguard.findings import Severity
from replayguard.frontends import python_frontend

FIXTURES = Path(__file__).parent / "fixtures" / "python"


def findings_for(name: str):
    module = python_frontend.parse_file(str(FIXTURES / name))
    assert module.handlers, f"no durable handler found in {name}"
    out = []
    for handler in module.handlers:
        out.extend(rules.check(handler))
    return out


@pytest.fixture(scope="module")
def bad():
    return findings_for("bad_handler.py")


@pytest.fixture(scope="module")
def good():
    return findings_for("good_handler.py")


def rule_ids(findings):
    return {f.rule for f in findings}


# -- the correctness that matters most --------------------------------------


def test_good_handler_is_clean(good):
    """A correct handler must produce zero findings.

    This is the test that decides whether the tool survives contact with users.
    A checker that cries wolf on working code gets switched off, and then it
    catches nothing at all.
    """
    assert good == [], "\n".join(f"{f.rule} {f.loc} {f.message}" for f in good)


def test_bad_handler_triggers_every_rule(bad):
    assert rule_ids(bad) == {"RG001", "RG002", "RG003", "RG004", "RG005", "RG900"}


# -- individual rules --------------------------------------------------------


def test_rg001_catches_clock_random_and_identity(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG001")
    assert "time.time" in msgs
    assert "uuid.uuid4" in msgs
    assert "random.random" in msgs


def test_rg001_resolves_from_imports(bad):
    """`from datetime import datetime` must resolve to `datetime.datetime.now`.

    Alias resolution is the difference between real analysis and grepping for
    `datetime.now`. A regex-based checker misses this entire class.
    """
    msgs = " ".join(f.message for f in bad if f.rule == "RG001")
    assert "datetime.datetime.now" in msgs


def test_rg002_catches_http_and_aws_sdk(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG002")
    assert "requests.get" in msgs
    assert any("put_item" in m or "boto3" in m for m in msgs.split())


def test_rg003_catches_captured_list_mutation(bad):
    """The AWS-documented silent-loss bug: `receipts.append()` inside a step."""
    targets = {f.message for f in bad if f.rule == "RG003"}
    assert any("receipts" in t for t in targets)


def test_rg003_catches_module_level_mutation(bad):
    targets = " ".join(f.message for f in bad if f.rule == "RG003")
    assert "AUDIT" in targets


def test_rg003_findings_are_errors(bad):
    for f in bad:
        if f.rule == "RG003":
            assert f.severity is Severity.ERROR


def test_rg005_flags_clock_derived_step_name(bad):
    hits = [f for f in bad if f.rule == "RG005"]
    assert hits, "expected a dynamic step name finding"
    assert "time.time" in hits[0].message


def test_rg005_allows_loop_index_names(good):
    """`f"item-{index}"` is the pattern AWS recommends and must not fire."""
    assert not [f for f in good if f.rule == "RG005"]


def test_rg900_reports_unresolved_step_body(bad):
    hits = [f for f in bad if f.rule == "RG900"]
    assert hits
    assert hits[0].severity is Severity.NOTE


# -- region resolution -------------------------------------------------------


def test_io_inside_step_body_is_not_reported(good):
    """I/O inside a step is the entire point of a step; it must never fire."""
    assert not [f for f in good if f.rule in {"RG001", "RG002"}]


def test_every_finding_has_rationale_and_fix(bad):
    for f in bad:
        assert f.rationale, f"{f.rule} has no rationale"
        assert f.fix, f"{f.rule} has no fix"
