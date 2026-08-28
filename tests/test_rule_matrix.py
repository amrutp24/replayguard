"""Every rule against every language, in both directions.

The per-frontend test files each prove their own language works. Nothing proved
that a *rule* works everywhere -- and RG002, RG004 and RG005 have never fired on
third-party code, which left "does that rule even work in Java?" unanswered.
This closes it: one minimal violation per rule per language, plus a correct
handler per language that must produce nothing at all.

The negative half matters more than the positive half. A rule that fires on
correct code gets the whole tool switched off, after which it catches nothing.
"""

import pytest

from replayguard import rules
from replayguard.frontends import python_frontend

pytest.importorskip("tree_sitter", reason="parsers not installed")

from replayguard.frontends import (  # noqa: E402
    java_frontend,
    rust_frontend,
    typescript_frontend,
)

LANGUAGES = ["python", "typescript", "java", "rust"]

# A case is (source, parse function, filename). Each builder wraps a body in the
# smallest handler its frontend will recognise, so the snippet under test stays
# readable next to the three others that mean the same thing.


def _py(body: str, extra: str = "") -> tuple:
    return (
        "from aws_durable_execution_sdk_python import durable_execution\n"
        f"{extra}\n@durable_execution\ndef handler(event, context):\n{body}\n",
        python_frontend.parse_source,
        "h.py",
    )


def _ts(body: str, extra: str = "") -> tuple:
    return (
        "import { withDurableExecution, DurableContext } "
        "from '@aws/durable-execution-sdk-js';\n"
        f"{extra}\n"
        "const handler = async (event: any, context: DurableContext) => {\n"
        f"{body}\n}};\n"
        "export const lambdaHandler = withDurableExecution(handler);\n",
        typescript_frontend.parse_source,
        "h.ts",
    )


def _java(body: str, extra: str = "") -> tuple:
    return (
        "import java.util.*;\nimport java.time.Instant;\nimport java.nio.file.*;\n"
        f"{extra}\n"
        "public class H extends DurableHandler<In, Out> {\n"
        "  public String handleRequest(In input, DurableContext context) {\n"
        f"{body}\n  }}\n}}\n",
        java_frontend.parse_source,
        "H.java",
    )


def _rust(body: str, extra: str = "") -> tuple:
    return (
        "use chrono::Utc;\n"
        "use durable_lambda_core::context::DurableContext;\n"
        "use std::sync::{Arc, Mutex};\n"
        f"{extra}\n#[durable_execution]\n"
        "async fn handler(event: Value, mut ctx: DurableContext)"
        " -> Result<Value, Error> {\n"
        f"{body}\n}}\n",
        rust_frontend.parse_source,
        "h.rs",
    )


def rules_fired(case: tuple) -> set:
    source, parse, path = case
    module = parse(source, path)
    assert module.handlers, f"no durable handler detected in {path}"
    return {f.rule for handler in module.handlers for f in rules.check(handler)}


# -- one minimal violation per rule, per language ----------------------------

VIOLATIONS = {
    # a clock read outside a step: replay gets a different answer
    "RG001": {
        "python": _py("    t = time.time()\n    return {'t': t}", "import time"),
        "typescript": _ts("  const t = Date.now();\n  return { t };"),
        "java": _java(
            "    long t = Instant.now().toEpochMilli();\n    return String.valueOf(t);"
        ),
        "rust": _rust('    let t = Utc::now();\n    Ok(json!({"t": t}))'),
    },
    # I/O outside a step: re-run on every replay, and unrecorded
    "RG002": {
        "python": _py("    c = open('/tmp/x').read()\n    return {'c': c}"),
        "typescript": _ts("  const r = await fetch('https://example/x');\n  return { r };"),
        "java": _java('    String c = Files.readString(Path.of("/tmp/x"));\n    return c;'),
        "rust": _rust(
            '    let c = std::fs::read_to_string("/tmp/x")?;\n    Ok(json!({"c": c}))'
        ),
    },
    # a step body writing state it does not own: the write is skipped on replay
    "RG003": {
        "python": _py(
            "    log = []\n"
            "    def w(_):\n        log.append(1)\n        return 1\n"
            "    context.step(w, name='w')\n"
            "    return {'n': len(log)}"
        ),
        "typescript": _ts(
            "  const log: number[] = [];\n"
            "  await context.step('w', async () => { log.push(1); });\n"
            "  return { n: log.length };"
        ),
        "java": _java(
            "    List<String> log = new ArrayList<>();\n"
            '    context.step("w", String.class, c -> { log.add("x"); return "x"; });\n'
            "    return String.valueOf(log.size());"
        ),
        # Rust can only express this through interior mutability: a borrowed
        # capture will not compile under the SDK's Send + 'static bound, so the
        # Arc/Mutex shape is the only form the bug can actually take here.
        "rust": _rust(
            "    let log = Arc::new(Mutex::new(Vec::new()));\n"
            "    let h = Arc::clone(&log);\n"
            '    ctx.step("w", move || { let h = Arc::clone(&h);\n'
            "        async move { h.lock().unwrap().push(1); Ok::<_, String>(1) } })"
            ".await?;\n"
            "    let n = log.lock().unwrap().len();\n"
            '    Ok(json!({"n": n}))'
        ),
    },
    # branching on something nondeterministic: replay can take the other branch
    "RG004": {
        "python": _py(
            "    if time.time() > 0:\n        pass\n    return {}", "import time"
        ),
        "typescript": _ts("  if (new Date().getHours() < 12) { }\n  return {};"),
        "java": _java('    if (Instant.now().toEpochMilli() > 0) { }\n    return "x";'),
        "rust": _rust("    if Utc::now().timestamp() > 0 { }\n    Ok(json!({}))"),
    },
    # an operation name built from a clock: replay looks up a name that is not
    # in the journal, so the step runs a second time
    "RG005": {
        "python": _py(
            "    return context.step(lambda _: 1, name=f'op-{time.time()}')",
            "import time",
        ),
        "typescript": _ts(
            "  await context.step(`op-${Date.now()}`, async () => 1);\n  return {};"
        ),
        "java": _java(
            '    context.step("op-" + Instant.now(), String.class, c -> "x");\n'
            '    return "x";'
        ),
        "rust": _rust(
            '    ctx.step(&format!("op-{}", Utc::now()), || async { Ok::<_, String>(1) })'
            ".await?;\n"
            "    Ok(json!({}))"
        ),
    },
}


@pytest.mark.parametrize("rule", sorted(VIOLATIONS))
@pytest.mark.parametrize("language", LANGUAGES)
def test_rule_fires_in_every_language(rule, language):
    """Capability, asserted per cell rather than per language.

    Three of these rules have never fired on code someone else wrote. That is
    because published handlers do not contain those mistakes -- but the only way
    to tell that apart from a broken rule is to hand it the mistake and watch.
    """
    assert rule in rules_fired(VIOLATIONS[rule][language])


# -- the same work, done correctly, in every language ------------------------

CLEAN = {
    "python": _py(
        "    t = context.step(lambda _: time.time(), name='t')\n"
        "    log = []\n"
        "    v = context.step(lambda _: 1, name='w')\n"
        "    log.append(v)\n"
        "    return {'t': t, 'n': len(log)}",
        "import time",
    ),
    "typescript": _ts(
        "  const t = await context.step('t', async () => Date.now());\n"
        "  const log: number[] = [];\n"
        "  const v = await context.step('w', async () => 1);\n"
        "  log.push(v);\n"
        "  return { t, n: log.length };"
    ),
    "java": _java(
        '    Long t = context.step("t", Long.class, c -> Instant.now().toEpochMilli());\n'
        "    List<String> log = new ArrayList<>();\n"
        '    String v = context.step("w", String.class, c -> "x");\n'
        "    log.add(v);\n"
        "    return String.valueOf(log.size());"
    ),
    "rust": _rust(
        '    let t = ctx.step("t", || async { Ok::<_, String>(Utc::now().to_rfc3339()) })'
        ".await?;\n"
        "    let mut log = Vec::new();\n"
        '    let v = ctx.step("w", || async { Ok::<_, String>(1) }).await?;\n'
        "    log.push(v);\n"
        '    Ok(json!({"t": t, "n": log.len()}))'
    ),
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_correct_handler_is_silent_in_every_language(language):
    """The half that decides whether anyone leaves the tool switched on.

    Every clock is checkpointed and every value comes back out of its step
    rather than being written from inside one -- the same work as the
    violations above, done the right way round. Anything reported is a false
    positive.
    """
    assert rules_fired(CLEAN[language]) == set()
