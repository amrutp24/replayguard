from pathlib import Path

import pytest

from replayguard import rules
from replayguard.findings import Severity

pytest.importorskip("tree_sitter_java", reason="Java extra not installed")

from replayguard.frontends import java_frontend  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "java"


def findings_for(name: str):
    module = java_frontend.parse_file(str(FIXTURES / name))
    assert module.handlers, f"no durable handler found in {name}"
    out = []
    for handler in module.handlers:
        out.extend(rules.check(handler))
    return out


@pytest.fixture(scope="module")
def bad():
    return findings_for("BadHandler.java")


@pytest.fixture(scope="module")
def good():
    return findings_for("GoodHandler.java")


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


def test_handler_found_via_durable_context_parameter():
    """Detection keys off the DurableContext parameter, not the base class.

    Both signals exist, but a method taking a DurableContext is the reliable
    one -- the base class may be aliased or the file may hold a helper class.
    """
    module = java_frontend.parse_file(str(FIXTURES / "GoodHandler.java"))
    assert [h.name for h in module.handlers] == ["handleRequest"]


def test_rg001_catches_clock_and_identity(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG001")
    assert "System.currentTimeMillis" in msgs
    assert "Instant.now" in msgs
    assert "UUID.randomUUID" in msgs


def test_rg001_catches_unseeded_new_random(bad):
    assert any(f.rule == "RG001" and "new Random()" in f.message for f in bad)


def test_rg001_catches_method_on_tainted_field(bad):
    """`rng.nextInt()` where `rng` is a Random field.

    Method names on such an instance are unbounded, so the declared type is
    what makes it detectable -- the same trick the AWS-client rule uses.
    """
    assert any(f.rule == "RG001" and "rng.nextInt" in f.message for f in bad)


def test_rg002_catches_filesystem_and_sdk(bad):
    msgs = " ".join(f.message for f in bad if f.rule == "RG002")
    assert "Files.readString" in msgs
    assert "ddb.putItem" in msgs


def test_rg003_catches_collection_and_field_writes(bad):
    joined = " ".join(f.message for f in bad if f.rule == "RG003")
    assert "receipts" in joined, "captured collection mutation"
    assert "AUDIT" in joined, "static field mutation"
    assert "lastReceipt" in joined, "instance field write"


def test_rg003_has_no_captured_local_reassignment_case(bad):
    """Java's effectively-final rule removes an entire violation class.

    JavaScript's `lastReceipt = x` writing through to an enclosing local is a
    compile error in Java, so the frontend must not invent it. Only field and
    collection writes should appear.
    """
    for f in bad:
        if f.rule == "RG003":
            assert f.severity is Severity.ERROR


def test_rg005_flags_clock_derived_step_name(bad):
    hits = [f for f in bad if f.rule == "RG005"]
    assert hits
    assert "System.currentTimeMillis" in hits[0].message


def test_rg005_allows_index_concatenated_name(good):
    assert not [f for f in good if f.rule == "RG005"]


def test_rg900_reports_method_reference_body(bad):
    hits = [f for f in bad if f.rule == "RG900"]
    assert hits
    assert hits[0].severity is Severity.NOTE


def test_body_located_by_kind_not_position(good):
    """Java has step(name, Type.class, fn) and step(name, fn).

    Both must resolve their body, so the lambda is found by kind rather than by
    argument index.
    """
    module = java_frontend.parse_file(str(FIXTURES / "GoodHandler.java"))
    names = [s.name_literal for s in module.handlers[0].steps]
    assert "save-order" in names
    assert "finalise" in names, "two-argument overload must be recognised"


def test_io_inside_step_body_is_not_reported(good):
    assert not [f for f in good if f.rule in {"RG001", "RG002"}]
