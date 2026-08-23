"""Rust frontend tests.

Rust is the fourth language and the fourth outer-write model. It matters most
here because the SDK this targets says its determinism rules are documented,
not enforced -- so this is the only thing standing between a Rust durable
handler and a silent replay bug.
"""

from pathlib import Path

import pytest

from replayguard import rules
from replayguard.ir import Region

pytest.importorskip("tree_sitter_rust", reason="Rust extra not installed")

from replayguard.frontends import rust_frontend  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "rust"

PRELUDE = (
    "use chrono::Utc;\n"
    "use durable_lambda_core::context::DurableContext;\n"
    "use std::sync::{Arc, Mutex};\n"
    "use uuid::Uuid;\n"
)


def findings_for(name: str):
    module = rust_frontend.parse_file(str(FIXTURES / name))
    assert module.handlers, f"no durable handler found in {name}"
    out = []
    for handler in module.handlers:
        out.extend(rules.check(handler))
    return out


def source_findings(body: str, extra: str = ""):
    src = (
        f"{PRELUDE}{extra}\n"
        "#[durable_execution]\n"
        "async fn handler(event: Value, mut ctx: DurableContext) -> Result<Value, Error> {\n"
        f"{body}\n"
        "}\n"
    )
    module = rust_frontend.parse_source(src, "h.rs")
    assert module.handlers, "handler not detected"
    return rules.check(module.handlers[0])


@pytest.fixture(scope="module")
def bad():
    return findings_for("bad_handler.rs")


@pytest.fixture(scope="module")
def good():
    return findings_for("good_handler.rs")


def test_good_handler_is_clean(good):
    assert good == [], "\n".join(f"{f.rule} {f.loc} {f.message}" for f in good)


def test_bad_handler_triggers_the_five_rules(bad):
    assert {f.rule for f in bad} == {"RG001", "RG002", "RG003", "RG004", "RG005"}


# -- handler detection -------------------------------------------------------


@pytest.mark.parametrize(
    "context_type", ["DurableContext", "BuilderContext", "ClosureContext", "TraitContext"]
)
def test_all_four_api_styles_are_detected(context_type):
    """The SDK ships four API styles; the context parameter is the one signal
    common to all of them, so detection keys off that rather than the attribute
    or the trait impl."""
    src = (
        f"{PRELUDE}async fn handler(event: Value, mut ctx: {context_type})"
        " -> Result<Value, Error> { Ok(Utc::now()) }\n"
    )
    module = rust_frontend.parse_source(src, "h.rs")
    assert len(module.handlers) == 1
    assert [f.rule for f in rules.check(module.handlers[0])] == ["RG001"]


def test_closure_handler_is_detected():
    """The builder style passes the handler as a closure, not a fn item."""
    src = (
        f"{PRELUDE}fn main() {{ durable_lambda_builder::handler("
        "|event: Value, mut ctx: BuilderContext| async move { Ok(Utc::now()) }); }\n"
    )
    module = rust_frontend.parse_source(src, "h.rs")
    assert module.handlers


def test_plain_function_is_ignored():
    """No durable context means no replay obligation and nothing to report."""
    src = f"{PRELUDE}fn helper() -> i64 {{ Utc::now().timestamp() }}\n"
    assert rust_frontend.parse_source(src, "h.rs").handlers == []


# -- catalog resolution ------------------------------------------------------


def test_crate_prefix_is_canonicalised(bad):
    """`chrono::Utc::now` must match the catalog's `Utc::now`, and the message
    must still show what the developer actually wrote."""
    hits = [f for f in bad if "chrono::Utc::now" in f.message]
    assert hits, [f.message for f in bad]


def test_std_and_bare_paths_both_resolve(bad):
    msgs = " ".join(f.message for f in bad)
    assert "SystemTime::now" in msgs
    assert "Uuid::new_v4" in msgs
    assert "std::fs::read_to_string" in msgs


# -- Rust-specific outer-write model ----------------------------------------


def test_arc_mutex_write_in_step_is_reported(bad):
    """Rust's remaining RG003 shape.

    A `&mut` capture will not compile under the SDK's `Send + 'static` bound, so
    the JavaScript shape is impossible. Interior mutability through a shared
    handle is what is left, and it is lost on replay just the same.
    """
    hits = [f for f in bad if f.rule == "RG003"]
    assert hits
    assert "receipts" in hits[0].message


def test_arc_clone_alias_resolves_to_the_outer_name():
    """`let r = Arc::clone(&outer)` inside a step rebinds the name locally.

    A plain scope check says "not outer" and misses the write entirely; this is
    the idiomatic way to share state into a Rust step, so without alias
    resolution RG003 would never fire on real code.
    """
    findings = source_findings(
        "    let shared = Arc::new(Mutex::new(Vec::new()));\n"
        "    let handle = Arc::clone(&shared);\n"
        "    ctx.step(\"s\", move || { let shared = Arc::clone(&handle);\n"
        "        async move { shared.lock().unwrap().push(1); Ok::<_, String>(1) } }).await?;\n"
        "    let n = shared.lock().unwrap().len();\n"
        "    Ok(json!({ \"n\": n }))",
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert hits, findings
    assert "shared" in hits[0].message


def test_plain_local_mutation_in_a_step_is_not_reported():
    """A `Vec` moved into the closure is the closure's own; mutating it cannot
    affect the caller, so reporting it would be noise."""
    findings = source_findings(
        "    ctx.step(\"s\", move || async move {\n"
        "        let mut local = Vec::new();\n"
        "        local.push(1);\n"
        "        Ok::<_, String>(local.len()) }).await?;\n"
        "    Ok(json!({}))",
    )
    assert not [f for f in findings if f.rule == "RG003"], findings


# -- macros ------------------------------------------------------------------


def test_clock_inside_a_format_macro_step_name_is_caught(bad):
    """tree-sitter leaves macro arguments as an unparsed token_tree, so there is
    no call node to find. `format!` step names are the common case, so the raw
    text is scanned instead."""
    hits = [f for f in bad if f.rule == "RG005"]
    assert hits
    assert "Utc::now" in hits[0].message


def test_index_derived_step_name_is_allowed(good):
    assert not [f for f in good if f.rule == "RG005"]


# -- regions -----------------------------------------------------------------


def test_io_inside_a_step_body_is_not_reported(good):
    assert not [f for f in good if f.rule in {"RG001", "RG002"}]


def test_await_and_try_wrappers_do_not_hide_the_call():
    """`ctx.step(..).await?` wraps the call in await_expression and
    try_expression; both must be seen through."""
    module = rust_frontend.parse_source(
        f"{PRELUDE}async fn handler(e: Value, mut ctx: DurableContext) -> Result<Value, Error> {{\n"
        "    let a = ctx.step(\"one\", || async { Ok::<_, String>(1) }).await?;\n"
        "    Ok(json!({}))\n}\n",
        "h.rs",
    )
    steps = module.handlers[0].steps
    assert [s.name_literal for s in steps] == ["one"]


def test_step_body_calls_are_in_step_region():
    module = rust_frontend.parse_source(
        f"{PRELUDE}async fn handler(e: Value, mut ctx: DurableContext) -> Result<Value, Error> {{\n"
        "    let a = ctx.step(\"one\", || async { Ok::<_, String>(Utc::now()) }).await?;\n"
        "    Ok(json!({}))\n}\n",
        "h.rs",
    )
    handler = module.handlers[0]
    clock = [c for c in handler.calls if "Utc::now" in c.dotted]
    assert clock and all(c.region is Region.STEP_BODY for c in clock)


# -- statics, loops, and match ----------------------------------------------


def test_static_mut_write_in_a_step_is_reported():
    """`static mut` is the other legal route to shared state in Rust.

    Unlike a moved-in local, a static is visible to every later invocation, so
    losing the write on replay leaves the caller reading a stale value.
    """
    findings = source_findings(
        "    ctx.step(\"s\", || async { COUNTER = 1; Ok::<_, String>(1) }).await?;\n"
        "    Ok(json!({ \"c\": COUNTER }))",
        extra="static mut COUNTER: u64 = 0;\n",
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert hits, findings
    assert "COUNTER" in hits[0].message


def test_compound_assignment_to_a_static_is_a_write():
    findings = source_findings(
        "    ctx.step(\"s\", || async { COUNTER += 1; Ok::<_, String>(1) }).await?;\n"
        "    Ok(json!({ \"c\": COUNTER }))",
        extra="static mut COUNTER: u64 = 0;\n",
    )
    assert [f for f in findings if f.rule == "RG003"], findings


def test_clock_in_a_while_condition_is_a_branch():
    findings = source_findings(
        "    while Utc::now().hour() < 12 { break; }\n    Ok(json!({}))",
    )
    assert [f for f in findings if f.rule == "RG004"], findings


def test_clock_in_a_match_scrutinee_is_a_branch():
    """`match` is the idiomatic Rust conditional and diverges the same way."""
    findings = source_findings(
        "    match Utc::now().hour() { 0..=11 => {}, _ => {} }\n    Ok(json!({}))",
    )
    assert [f for f in findings if f.rule == "RG004"], findings


def test_reads_are_recorded_for_the_read_back_check():
    """RG003 depends on knowing whether the value is consumed elsewhere."""
    module = rust_frontend.parse_source(
        f"{PRELUDE}async fn handler(e: Value, mut ctx: DurableContext) -> Result<Value, Error> {{\n"
        "    let total = 1;\n    Ok(json!({ \"t\": total }))\n}\n",
        "h.rs",
    )
    names = {r.name for r in module.handlers[0].reads}
    assert "total" in names


def test_write_only_shared_state_is_not_reported():
    """Never read back, so losing it on replay corrupts nothing."""
    findings = source_findings(
        "    let log = Arc::new(Mutex::new(Vec::new()));\n"
        "    let handle = Arc::clone(&log);\n"
        "    ctx.step(\"s\", move || { let log = Arc::clone(&handle);\n"
        "        async move { log.lock().unwrap().push(1); Ok::<_, String>(1) } }).await?;\n"
        "    Ok(json!({}))",
    )
    assert not [f for f in findings if f.rule == "RG003"], findings


def test_unknown_receiver_is_not_a_durable_operation():
    """`thing.step(..)` on something that is not a context must be ignored."""
    module = rust_frontend.parse_source(
        f"{PRELUDE}async fn handler(e: Value, mut ctx: DurableContext) -> Result<Value, Error> {{\n"
        "    let x = builder.step(\"one\", || async { Ok::<_, String>(1) });\n"
        "    Ok(json!({}))\n}\n",
        "h.rs",
    )
    assert module.handlers[0].steps == []
