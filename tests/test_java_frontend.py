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


# -- interprocedural analysis -----------------------------------------------
#
# Analysis used to stop at the handler method, so anything a private helper did
# was invisible. A helper does not change the replay obligation -- whatever
# region the caller is in, the callee runs in -- so these check that the walk
# follows same-file calls, carries the region across, and terminates.


def findings_for_source(source: str):
    module = java_frontend.parse_source(source, "H.java")
    assert module.handlers, "no durable handler found"
    return list(rules.check(module.handlers[0]))


def _handler(body: str, extra: str = "") -> str:
    return (
        "import java.nio.file.Files;\n"
        "import java.nio.file.Path;\n"
        "import java.time.Instant;\n"
        "public class H extends DurableHandler<In, Out> {\n"
        "    public String handleRequest(In input, DurableContext context) {\n"
        f"{body}\n"
        "    }\n"
        f"{extra}\n"
        "}\n"
    )


def test_io_in_private_helper_called_from_durable_region_is_flagged():
    """The bug that motivated this: a helper hid a real RG002.

    `Files.readString` two frames below the handler is exactly as broken as one
    written inline, and the checker reported nothing at all.
    """
    findings = findings_for_source(
        _handler(
            "        String config = readConfig();\n        return config;",
            "    private String readConfig() throws Exception {\n"
            "        return Files.readString(Path.of(\"/tmp/c.json\"));\n"
            "    }",
        )
    )
    hits = [f for f in findings if f.rule == "RG002"]
    assert hits, findings
    assert "Files.readString" in hits[0].message
    # The call site says nothing about the violation, so the message has to say
    # which helper to open.
    assert "readConfig()" in hits[0].rationale


def test_helper_path_is_reported_through_several_frames():
    findings = findings_for_source(
        _handler(
            "        return deeper();",
            "    private String deeper() { return level2(); }\n"
            "    private String level2() { return Instant.now().toString(); }",
        )
    )
    hits = [f for f in findings if f.rule == "RG001"]
    assert hits
    assert "deeper() -> level2()" in hits[0].rationale


def test_this_qualified_helper_call_resolves():
    findings = findings_for_source(
        _handler(
            "        return this.readClock();",
            "    private String readClock() { return Instant.now().toString(); }",
        )
    )
    assert [f.rule for f in findings if f.rule == "RG001"] == ["RG001"]


def test_call_on_another_object_is_not_followed():
    """Only the implicit receiver and `this.` resolve.

    `other.readClock()` needs the receiver's runtime type. Guessing would either
    invent violations in code this file cannot see or pin them on the wrong
    method, so the walk stops -- a known limitation, deliberately chosen.
    """
    findings = findings_for_source(
        _handler(
            "        H other = new H();\n        return other.readClock();",
            "    private String readClock() { return Instant.now().toString(); }",
        )
    )
    assert not [f for f in findings if f.rule == "RG001"], findings


def test_helper_called_only_from_a_step_body_is_not_flagged():
    """Region carries across the call, and inside a step I/O is the point.

    GoodHandler's `render()` covers the same ground; this states it directly,
    because a checker that follows helpers but loses the region would fire on
    every correct handler that delegates its step work.
    """
    findings = findings_for_source(
        _handler(
            "        return context.step(\"read\", String.class, stepCtx -> loadIt());",
            "    private String loadIt() throws Exception {\n"
            "        return Files.readString(Path.of(\"/tmp/c.json\"));\n"
            "    }",
        )
    )
    assert not [f for f in findings if f.rule in {"RG001", "RG002"}], findings


def test_outer_write_inside_a_helper_reports_the_helper_path():
    findings = findings_for_source(
        "public class H extends DurableHandler<In, Out> {\n"
        "    private String lastReceipt;\n"
        "    public String handleRequest(In input, DurableContext context) {\n"
        "        return context.step(\"s\", String.class, stepCtx -> record(\"r\"));\n"
        "    }\n"
        "    private String record(String r) {\n"
        "        String lastReceipt = r;\n"
        "        this.lastReceipt = r;\n"
        "        return r;\n"
        "    }\n"
        "}\n"
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert len(hits) == 1, findings
    assert "lastReceipt" in hits[0].message
    assert "record()" in hits[0].rationale


def test_direct_recursion_terminates():
    findings = findings_for_source(
        _handler(
            "        return spin();",
            "    private String spin() { return spin() + Instant.now(); }",
        )
    )
    assert len([f for f in findings if f.rule == "RG001"]) == 1, findings


def test_mutual_recursion_terminates():
    findings = findings_for_source(
        _handler(
            "        return a();",
            "    private String a() { return b() + Instant.now(); }\n"
            "    private String b() { return a(); }",
        )
    )
    assert len([f for f in findings if f.rule == "RG001"]) == 1, findings


def test_helper_chain_depth_is_capped():
    chain = "\n".join(
        f"    private String f{i}() {{ return f{i + 1}(); }}" for i in range(8)
    )
    findings = findings_for_source(
        _handler(
            "        return f0();",
            chain + "\n    private String f8() { return Instant.now().toString(); }",
        )
    )
    assert not [f for f in findings if f.rule == "RG001"], (
        "a chain deeper than the cap must stop, not report"
    )


def test_method_reference_step_body_is_not_double_walked():
    """`this::shared` stays unresolved even though `shared` is now walkable.

    Resolving it would need the target's parameter shape to line up with the
    step callback, and the note is honest about the gap. What must not happen is
    the method being walked once as a helper and again as a step body, which
    would report the same line twice under two different regions.
    """
    findings = findings_for_source(
        _handler(
            "        String a = shared();\n"
            "        context.step(\"s\", String.class, this::shared);\n"
            "        return a;",
            "    private String shared() { return Instant.now().toString(); }",
        )
    )
    assert len([f for f in findings if f.rule == "RG001"]) == 1, findings
    assert [f for f in findings if f.rule == "RG900"], "the gap is still reported"


def test_good_handler_helper_stays_clean(good):
    """GoodHandler's `render()` is called from inside a step. Following it must
    not turn the project's most important invariant -- zero findings on correct
    code -- into a false positive."""
    assert good == []
