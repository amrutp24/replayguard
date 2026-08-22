from pathlib import Path

import pytest

from replayguard import rules
from replayguard.findings import Severity
from replayguard.frontends import python_frontend
from replayguard.ir import Region

FIXTURES = Path(__file__).parent / "fixtures" / "python"

PRELUDE = "from aws_durable_execution_sdk_python import durable_execution\n"


def findings_for(name: str):
    module = python_frontend.parse_file(str(FIXTURES / name))
    assert module.handlers, f"no durable handler found in {name}"
    out = []
    for handler in module.handlers:
        out.extend(rules.check(handler))
    return out


def handler_for(body: str, extra: str = ""):
    """Parse a handler from source, for tests that need an exact call graph.

    The fixtures are whole realistic files; these cases need one call shape at
    a time, and a fixture per shape would bury the point being made.
    """
    src = f"{PRELUDE}{extra}\n\n@durable_execution\ndef handler(event, context):\n{body}\n"
    module = python_frontend.parse_source(src, "h.py")
    assert module.handlers, "handler not detected"
    return module.handlers[0]


def source_findings(body: str, extra: str = ""):
    return rules.check(handler_for(body, extra))


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


# -- interprocedural analysis ------------------------------------------------
#
# Analysis that stops at the handler body passes the shape that most real bugs
# actually take: the handler reads clean because the violation lives one `def`
# away. These tests pin both halves -- that the walk finds it, and that it does
# not manufacture findings by walking helpers in the wrong region.


def test_io_in_module_level_helper_is_flagged():
    """The motivating case: `open()` one call away from the handler."""
    findings = source_findings(
        "    config = read_config()\n    return config",
        extra="def read_config():\n    return open('/tmp/c.json').read()",
    )
    hits = [f for f in findings if f.rule == "RG002"]
    assert hits, findings


def test_helper_finding_names_the_route_from_the_handler():
    """A finding the reader cannot locate from the call site is not actionable."""
    findings = source_findings(
        "    return read_config()",
        extra="def read_config():\n    return open('/tmp/c.json').read()",
    )
    hits = [f for f in findings if f.rule == "RG002"]
    assert hits
    assert "read_config()" in hits[0].rationale, hits[0].rationale


def test_via_path_lists_helpers_outermost_first():
    """Two levels down, the path has to read in call order to be followable."""
    handler = handler_for(
        "    return outer()",
        extra=(
            "import time\n"
            "def outer():\n"
            "    return inner()\n"
            "def inner():\n"
            "    return time.time()"
        ),
    )
    clock = [c for c in handler.calls if c.dotted == "time.time"]
    assert len(clock) == 1, clock
    assert clock[0].via == ("outer", "inner")


def test_helper_called_only_from_a_step_body_is_not_flagged():
    """I/O in a helper is fine when every caller is a step. The region belongs
    to the call site, not to the definition."""
    findings = source_findings(
        "    return context.step(lambda _: save_it(), name='save')",
        extra="def save_it():\n    return open('/tmp/c.json').read()",
    )
    assert not [f for f in findings if f.rule in {"RG001", "RG002"}], findings


def test_same_helper_is_judged_separately_in_each_region():
    """Called from both sides, the durable call is a violation and the step
    call is not -- one helper, two verdicts."""
    handler = handler_for(
        "    early = now()\n"
        "    late = context.step(lambda _: now(), name='now')\n"
        "    return early, late",
        extra="import time\ndef now():\n    return time.time()",
    )
    clock = [c for c in handler.calls if c.dotted == "time.time"]
    assert {c.region for c in clock} == {Region.DURABLE, Region.STEP_BODY}

    hits = [f for f in rules.check(handler) if f.rule == "RG001"]
    assert len(hits) == 1, hits
    assert "now()" in hits[0].rationale


def test_step_body_helper_is_not_walked_twice():
    """A nested def used as a step body is walked once, at its `step()` site.

    Walking it again from a bare call would replay its legitimate in-step I/O
    into the durable region -- the false positive this tool can least afford.
    """
    handler = handler_for(
        "    def save(_):\n"
        "        return table.put_item(Item={'id': 1})\n"
        "\n"
        "    save(None)\n"
        "    return context.step(save, name='save')",
        extra="import boto3\ntable = boto3.resource('dynamodb').Table('t')",
    )
    puts = [c for c in handler.calls if c.dotted.endswith("put_item")]
    assert len(puts) == 1, f"step body walked {len(puts)} times"
    assert puts[0].region is Region.STEP_BODY
    assert not [f for f in rules.check(handler) if f.rule == "RG002"]


def test_module_level_helper_is_not_treated_as_a_closure():
    """A module-level helper cannot see the handler's locals, so its own
    parameters must not read as captured state.

    Without a fresh scope stack the parameter write below becomes an RG003, and
    every handler that delegates to a helper starts crying wolf.
    """
    findings = source_findings(
        # AUDIT is read back outside the step, so it is a genuine lost update and
        # serves as the positive control. `bucket` is the helper's own parameter
        # and must never be reported.
        "    context.step(lambda _: record({}), name='record')\n"
        "    return len(AUDIT)",
        extra="AUDIT = []\ndef record(bucket):\n    bucket['done'] = True\n    AUDIT.append(1)",
    )
    targets = [f.message for f in findings if f.rule == "RG003"]
    assert any("AUDIT" in m for m in targets), findings
    assert not any("bucket" in m for m in targets), findings


def test_direct_recursion_terminates():
    findings = source_findings(
        "    return countdown(3)",
        extra=(
            "import time\n"
            "def countdown(n):\n"
            "    if n <= 0:\n"
            "        return time.time()\n"
            "    return countdown(n - 1)"
        ),
    )
    assert [f for f in findings if f.rule == "RG001"], findings


def test_mutual_recursion_terminates():
    """Two helpers calling each other must not loop the walker."""
    findings = source_findings(
        "    return ping(1)",
        extra=(
            "import time\n"
            "def ping(n):\n"
            "    return pong(n)\n"
            "def pong(n):\n"
            "    return time.time() if n else ping(n)"
        ),
    )
    assert [f for f in findings if f.rule == "RG001"], findings


def test_recursive_handler_does_not_re_walk_itself():
    """The handler is a module-level function too, so it resolves as its own
    callee. Re-entering it would double every finding under a bogus path."""
    handler = handler_for(
        "    if event:\n"
        "        return handler(None, context)\n"
        "    return time.time()",
        extra="import time",
    )
    clock = [c for c in handler.calls if c.dotted == "time.time"]
    assert len(clock) == 1, clock
    assert clock[0].via == ()
