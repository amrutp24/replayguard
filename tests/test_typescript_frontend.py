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


def findings_for_source(body: str, extra: str = ""):
    """Check a handler body written inline, with `extra` at module level.

    Interprocedural cases need a helper *and* a handler in one file, which the
    two fixtures cannot express without becoming a grab bag.
    """
    src = (
        "import { withDurableExecution, DurableContext } "
        "from '@aws/durable-execution-sdk-js';\n"
        f"{extra}\n"
        "const handler = async (event: any, context: DurableContext) => {\n"
        f"{body}\n"
        "};\n"
        "export const lambdaHandler = withDurableExecution(handler);\n"
    )
    module = typescript_frontend.parse_source(src, "h.ts")
    assert module.handlers, "handler not detected"
    return rules.check(module.handlers[0])


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


# -- interprocedural analysis ------------------------------------------------


def test_io_in_a_helper_called_from_the_durable_region_is_flagged():
    """The handler that only calls helpers is the common shape, not the rare one.

    Analysis that stopped at the handler body gave this file a clean bill of
    health while `fetch` re-ran on every replay.
    """
    findings = findings_for_source(
        "  const config = await readConfig();\n  return config;",
        extra=(
            "async function readConfig() {\n"
            "  return await fetch('https://x/config');\n"
            "}"
        ),
    )
    hits = [f for f in findings if f.rule == "RG002"]
    assert hits, findings
    assert "readConfig()" in hits[0].rationale, hits[0].rationale


def test_helper_path_is_named_in_the_rationale_outermost_first():
    """The path is the whole value of the finding.

    Pointing at a line inside a helper without saying how the handler reaches
    it leaves the reader to grep for callers.
    """
    findings = findings_for_source(
        "  return await outer();",
        extra=(
            "async function inner() { return Date.now(); }\n"
            "async function outer() { return await inner(); }"
        ),
    )
    hits = [f for f in findings if f.rule == "RG001"]
    assert hits, findings
    assert "outer() -> inner()" in hits[0].rationale, hits[0].rationale


def test_helper_called_only_inside_a_step_body_is_not_flagged():
    """The region travels with the call, so a helper is judged by its caller.

    I/O in a helper reached only from inside a step is exactly what steps are
    for. Flagging it would make the feature unusable.
    """
    findings = findings_for_source(
        "  return await context.step('cfg', async () => await readConfig());",
        extra=(
            "async function readConfig() {\n"
            "  return await fetch('https://x/config');\n"
            "}"
        ),
    )
    assert findings == [], findings


@pytest.mark.parametrize(
    "declaration",
    [
        "async function readConfig() { return await fetch('https://x/c'); }",
        "const readConfig = async () => { return await fetch('https://x/c'); };",
    ],
    ids=["function-declaration", "arrow-in-declarator"],
)
def test_both_declaration_forms_resolve(declaration):
    """`const f = async () => {}` is at least as common in TS as `function f()`."""
    findings = findings_for_source("  return await readConfig();", extra=declaration)
    assert [f for f in findings if f.rule == "RG002"], findings


def test_direct_recursion_terminates():
    findings = findings_for_source(
        "  return await countdown(3);",
        extra=(
            "async function countdown(n: number) {\n"
            "  return n > 0 ? await countdown(n - 1) : Date.now();\n"
            "}"
        ),
    )
    assert [f for f in findings if f.rule == "RG001"], findings


def test_mutual_recursion_terminates():
    """Two helpers calling each other must not hang the checker.

    A guard keyed only on "currently walking" would still loop here on the
    second entry from the other side; the set is never popped for that reason.
    """
    findings = findings_for_source(
        "  return await ping();",
        extra=(
            "async function ping() { return await pong(); }\n"
            "async function pong() { return (await ping()) + Math.random(); }"
        ),
    )
    assert [f for f in findings if f.rule == "RG001"], findings


def test_helper_used_as_a_step_body_is_not_double_walked():
    """One violation, one finding.

    `writeIt` is both a step body by reference and a call from inside another
    step body. Without the (function, region) guard its outer write is reported
    twice at the same line, which reads as two separate bugs.
    """
    findings = findings_for_source(
        "  await context.step('a', writeIt);\n"
        "  await context.step('b', async () => { await writeIt(); });\n"
        # Read back outside the steps, so the lost write is a genuine one and
        # the test is measuring double-reporting rather than suppression.
        "  return AUDIT.length;",
        extra=(
            "const AUDIT: string[] = [];\n"
            "async function writeIt() { AUDIT.push('x'); }"
        ),
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert len(hits) == 1, hits


def test_locals_of_a_top_level_helper_are_not_outer_writes():
    """A top-level function is not a closure over its caller.

    Carrying the handler's scope into it made the helper's own accumulator look
    like a captured variable the step body must not write to -- a false
    positive on ordinary correct code.
    """
    findings = findings_for_source(
        "  let total = 0;\n"
        "  await context.step('sum', async () => { await tally([1, 2]); });",
        extra=(
            "async function tally(xs: number[]) {\n"
            "  let total = 0;\n"
            "  for (const x of xs) { total = total + x; }\n"
            "  return total;\n"
            "}"
        ),
    )
    assert findings == [], findings


def test_a_shadowing_local_is_not_resolved_to_the_top_level_function():
    """`const readConfig = event.cb` holds something we know nothing about.

    Following the same-named top-level declaration would report a function this
    call never reaches.
    """
    findings = findings_for_source(
        "  const readConfig = event.cb;\n  return await readConfig();",
        extra="async function readConfig() { return await fetch('https://x/c'); }",
    )
    assert findings == [], findings
