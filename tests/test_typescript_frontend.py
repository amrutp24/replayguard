from pathlib import Path

import pytest

from replayguard import rules
from replayguard.findings import Severity
from replayguard.frontends import typescript_frontend

FIXTURES = Path(__file__).parent / "fixtures" / "typescript"

pytest.importorskip("tree_sitter_typescript", reason="TypeScript extra not installed")


def findings_for(name: str):
    module = typescript_frontend.parse_file(str(FIXTURES / name))
    assert module.handlers, f"no durable handler found in {name}"
    out = []
    for handler in module.handlers:
        out.extend(rules.check(handler))
    return out


@pytest.fixture(scope="module")
def bad():
    return findings_for("bad_handler.ts")


@pytest.fixture(scope="module")
def good():
    return findings_for("good_handler.ts")


def test_good_handler_is_clean(good):
    assert good == [], "\n".join(f"{f.rule} {f.loc} {f.message}" for f in good)


def test_bad_handler_triggers_every_rule(bad):
    assert {f.rule for f in bad} == {
        "RG001",
        "RG002",
        "RG003",
        "RG004",
        "RG005",
        "RG900",
    }


def test_rg001_catches_clock_random_identity(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG001")
    assert "Date.now" in msgs
    assert "Math.random" in msgs
    assert "crypto.randomUUID" in msgs


def test_rg001_catches_zero_arg_new_date(bad):
    """`new Date()` reads the clock; `new Date(iso)` does not.

    Reported as the developer wrote it rather than as the catalog key, so the
    message points at recognisable source.
    """
    assert any(f.rule == "RG001" and "`new Date()`" in f.message for f in bad)


def test_step_name_expression_is_analysed_in_durable_region(bad):
    """A clock call inside a step name violates RG001 as well as RG005.

    The name expression really does evaluate outside the step. Both frontends
    must agree on this — they disagreed until it was caught by diffing their
    output on equivalent fixtures.
    """
    line = next(f.loc.line for f in bad if f.rule == "RG005")
    assert any(f.rule == "RG001" and f.loc.line == line for f in bad)


def test_rg002_catches_fetch_and_sdk_send(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG002")
    assert "fetch" in msgs
    assert "send" in msgs


def test_rg003_catches_all_three_write_shapes(bad):
    targets = {f.message for f in bad if f.rule == "RG003"}
    joined = " ".join(targets)
    assert "receipts" in joined, "captured array mutation"
    assert "AUDIT" in joined, "module-level mutation"
    assert "lastReceipt" in joined, "bare assignment through to outer scope"


def test_rg003_bare_assignment_is_js_specific(bad):
    """In Python this would be a local binding; in JS it writes through.

    The rule is language-agnostic — the frontend encodes the difference.
    """
    hits = [f for f in bad if f.rule == "RG003" and "lastReceipt" in f.message]
    assert hits
    assert hits[0].severity is Severity.ERROR


def test_rg005_flags_clock_derived_template_name(bad):
    hits = [f for f in bad if f.rule == "RG005"]
    assert hits
    assert "Date.now" in hits[0].message


def test_rg005_allows_index_template_name(good):
    assert not [f for f in good if f.rule == "RG005"]


def test_step_name_and_callback_order_is_normalized():
    """JS is step(name, fn); Python is step(fn, name=). Same IR either way."""
    module = typescript_frontend.parse_file(str(FIXTURES / "good_handler.ts"))
    steps = module.handlers[0].steps
    names = [s.name_literal for s in steps if s.name_literal]
    assert "save-order" in names
    assert "pick-shift" in names
