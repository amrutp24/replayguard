"""Regressions found by running against AWS's own durable-functions repos.

Every test here corresponds to a false positive or missed detection that the
fixtures did not catch, because the fixtures were written by the same person who
wrote the frontends. Real code written by other people found five bugs the
fixtures could not.
"""

import pytest

from replayguard import rules
from replayguard.frontends import python_frontend

pytest.importorskip("tree_sitter")

from replayguard.frontends import java_frontend, typescript_frontend  # noqa: E402

TS_PRELUDE = (
    "import { withDurableExecution, DurableContext } "
    "from '@aws/durable-execution-sdk-js';\n"
)


def ts_findings(body: str, extra: str = "") -> list:
    src = (
        f"{TS_PRELUDE}{extra}\n"
        "const handler = async (event: any, context: DurableContext) => {\n"
        f"{body}\n"
        "};\n"
        "export const lambdaHandler = withDurableExecution(handler);\n"
    )
    module = typescript_frontend.parse_source(src, "h.ts")
    assert module.handlers, "handler not detected"
    return rules.check(module.handlers[0])


def py_findings(body: str, extra: str = "") -> list:
    src = (
        "from aws_durable_execution_sdk_python import durable_execution\n"
        f"{extra}\n"
        "@durable_execution\n"
        "def handler(event, context):\n"
        f"{body}\n"
    )
    module = python_frontend.parse_source(src, "h.py")
    assert module.handlers, "handler not detected"
    return rules.check(module.handlers[0])


# -- 1. TypeScript: client taint must not reach response data ---------------


def test_array_method_on_sdk_response_is_not_io():
    """`output.content.find(...)` is Array.prototype.find, not a network call.

    Found in sample-ai-workflows: taint propagated from an SDK client through a
    step result into ordinary response data, so every method call on it read as
    external I/O.
    """
    findings = ts_findings(
        "  const response = await context.step('converse', async () => bedrock.send(cmd));\n"
        "  const output = response.output.message;\n"
        "  const textBlock = output.content.find((b) => 'text' in b);\n"
        "  return textBlock;",
        extra="const bedrock = new BedrockRuntimeClient({});",
    )
    assert not [f for f in findings if f.rule == "RG002"], findings


# -- 2. Python: step body passed as a keyword argument ----------------------


def test_callback_submitter_passed_as_keyword_is_a_step_body():
    """`wait_for_callback(submitter=fn)` -- AWS's own sample uses this form.

    Only args[0] was checked, so the body was analysed in the durable region and
    every legitimate in-step call was reported.
    """
    findings = py_findings(
        "    def register(callback_id):\n"
        "        table = dynamodb.Table('t')\n"
        "        table.put_item(Item={'id': callback_id})\n"
        "\n"
        "    return context.wait_for_callback(submitter=register, name='cb')",
        extra="import boto3\ndynamodb = boto3.resource('dynamodb')",
    )
    assert not [f for f in findings if f.rule in {"RG001", "RG002"}], findings


# -- 3. All: the durable surface is wider than `step` -----------------------


@pytest.mark.parametrize(
    "method", ["stepAsync", "withRetry", "map", "runInChildContext", "waitForCondition"]
)
def test_async_and_variant_operations_are_recognised(method):
    """`stepAsync` and friends are durable operations too.

    Found in the Java SDK examples: an unrecognised operation is worse than a
    missing rule, because its body is then analysed in the wrong region.
    """
    findings = ts_findings(
        f"  await context.{method}('op', async () => {{ return Date.now(); }});",
    )
    assert not [f for f in findings if f.rule == "RG001"], f"{method}: {findings}"


def test_java_step_async_body_is_a_step_body():
    src = """
public class H extends DurableHandler<In, Out> {
    public String handleRequest(In input, DurableContext context) {
        var f = context.stepAsync("async-op", String.class, stepCtx -> {
            return String.valueOf(System.nanoTime());
        });
        return "ok";
    }
}
"""
    module = java_frontend.parse_source(src, "H.java")
    findings = rules.check(module.handlers[0])
    assert not [f for f in findings if f.rule == "RG001"], findings


# -- 4. TypeScript: await + generic type argument ---------------------------


def test_await_generic_call_is_recognised():
    """`await context.waitForCallback<T>(...)` parses with the await_expression
    as the call's `function` field, wrapping the member expression.

    Found in the AutonomousCodingAgent sample. Without unwrapping, the whole
    operation went unrecognised and its body was analysed in the durable region.
    """
    findings = ts_findings(
        "  const r = await context.waitForCallback<string>('agent', async (id) => {\n"
        "    await agentClient.send(cmd);\n"
        "  }, { timeout: { hours: 8 } });\n"
        "  return r;",
        extra="const agentClient = new AgentClient({});",
    )
    assert not [f for f in findings if f.rule == "RG002"], findings


# -- 5. Coverage notes must not drown real findings -------------------------


def test_data_arguments_are_not_reported_as_coverage_gaps():
    """`context.invoke(name, payload)` passes data, not an unresolved callable.

    An over-eager RG900 produced 194 notes against 13 real findings on AWS's
    repos, which made the count meaningless.
    """
    findings = py_findings(
        "    payload = {'a': 1}\n"
        "    return context.invoke('other-fn', payload)",
    )
    assert not [f for f in findings if f.rule == "RG900"], findings


def test_genuine_unresolved_body_is_still_reported():
    """The tightening must not silence real coverage gaps."""
    findings = py_findings(
        "    return context.step(external_helper, name='x')",
        extra="from helpers import external_helper",
    )
    assert [f for f in findings if f.rule == "RG900"], findings


# -- 6. Child contexts are contexts -----------------------------------------


def test_child_context_steps_are_recognised():
    """`runInChildContext(async (childContext) => ...)` names its context freely.

    A fixed list of four receiver names missed `childContext`, so the child's
    steps went unrecognised and their bodies were analysed in the durable
    region. Found in the JS SDK conformance tests.
    """
    findings = ts_findings(
        "  return await context.runInChildContext(async (childContext: DurableContext) => {\n"
        "    return await childContext.step(async () => await ddb.send(cmd));\n"
        "  });",
        extra="const ddb = new DynamoDBClient({});",
    )
    assert not [f for f in findings if f.rule == "RG002"], findings


# -- 7. Unnamed operations must not have their body walked twice ------------


def test_unnamed_operation_body_is_visited_once():
    """`context.step(fn)` with no name makes positional[0] the body itself.

    Visiting it as a name expression as well walked the body twice, once in the
    durable region, so every legitimate in-step call was reported.
    """
    src = (
        f"{TS_PRELUDE}const ddb = new DynamoDBClient({{}});\n"
        "const handler = async (event: any, context: DurableContext) => {\n"
        "  return await context.step(async () => await ddb.send(cmd));\n"
        "};\n"
        "export const lambdaHandler = withDurableExecution(handler);\n"
    )
    handler = typescript_frontend.parse_source(src, "h.ts").handlers[0]

    assert len(handler.steps) == 1, handler.steps
    sends = [c for c in handler.calls if c.dotted == "ddb.send"]
    assert len(sends) == 1, f"body visited {len(sends)} times"
    assert not rules.check(handler)


# -- 8. Deferred step bodies (@durable_step) --------------------------------


def test_durable_step_decorated_helper_runs_inside_a_step():
    """`@durable_step` makes calling the function return a step descriptor.

    `context.step(validate(event), name="v")` therefore runs the body inside the
    step, not at the call site. Following it as an ordinary durable-region call
    reported every clock and SDK call in the body -- five false positives
    against AWS's own FSI payment sample.
    """
    findings = py_findings(
        "    return context.step(validate(event), name='validate')",
        extra=(
            "import datetime\n"
            "from aws_durable_execution_sdk_python import durable_step\n"
            "@durable_step\n"
            "def validate(step_context, event):\n"
            "    return datetime.datetime.now().isoformat()"
        ),
    )
    assert not [f for f in findings if f.rule in {"RG001", "RG002"}], findings


def test_undecorated_helper_is_still_followed():
    """The @durable_step handling must not disable interprocedural analysis."""
    findings = py_findings(
        "    return validate(event)",
        extra=(
            "import datetime\n"
            "def validate(event):\n"
            "    return datetime.datetime.now().isoformat()"
        ),
    )
    assert [f for f in findings if f.rule == "RG001"], findings


# -- 9. Lazy client initialisation is not a lost update ---------------------


def test_lazy_initialised_client_is_not_reported():
    """The memoised-singleton idiom, found in AWS's own AI workflow sample.

    Losing this write on replay costs a re-initialisation, not correctness: the
    next call rebuilds it. RG003's premise -- that the value is silently wrong
    -- does not hold, and firing here would hit almost every real handler.
    """
    findings = py_findings(
        "    return context.step(lambda _: get_client(), name='call')",
        extra=(
            "import boto3\n"
            "_client = None\n"
            "def get_client():\n"
            "    global _client\n"
            "    if _client is None:\n"
            "        _client = boto3.client('bedrock-runtime')\n"
            "    return _client"
        ),
    )
    assert not [f for f in findings if f.rule == "RG003"], findings


def test_unguarded_global_write_from_a_step_is_still_reported():
    """The guard must not silence a genuine lost update."""
    findings = py_findings(
        "    return context.step(lambda _: record(), name='rec')",
        extra=(
            "TOTAL = 0\n"
            "def record():\n"
            "    global TOTAL\n"
            "    TOTAL = TOTAL + 1\n"
            "    return TOTAL"
        ),
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert hits, findings
    assert "TOTAL" in hits[0].message


# -- 10. A stored closure runs at an unknown time ---------------------------


def test_closure_stored_then_invoked_in_a_step_is_not_durable_region():
    """The saga-compensation shape, from DurableLambdaCourse.

    The closure is *defined* at handler top level and pushed into an array, then
    *executed* from inside a step much later. Taking the definition site as the
    execution context reported every nondeterministic call it reaches -- five
    false positives in one file.
    """
    findings = ts_findings(
        "  const comps: any[] = [];\n"
        "  comps.push({ name: 'undo', fn: async () => { return Math.random(); } });\n"
        "  for (const c of comps) {\n"
        "    await context.step(c.name, async () => c.fn());\n"
        "  }",
    )
    assert not [f for f in findings if f.rule in {"RG001", "RG004"}], findings


def test_closure_invoked_at_its_definition_site_is_still_checked():
    """The fix must not blind the checker to callbacks that do run immediately."""
    findings = ts_findings(
        "  const ids = [1, 2].map(() => Math.random());\n  return ids;",
    )
    assert [f for f in findings if f.rule == "RG001"], findings


# -- 11. Increment is a mutation --------------------------------------------


def test_increment_in_a_step_body_is_an_outer_write():
    """`n++` is an update_expression, not an assignment_expression.

    Handling only assignments missed a counter incremented inside a step body and
    read back outside it into the handler's return value -- a genuine RG003 in
    the same file that produced four false positives.
    """
    findings = ts_findings(
        "  let attempts = 0;\n"
        "  await context.step('retry', async () => { attempts++; });\n"
        "  return { attempts };",
    )
    hits = [f for f in findings if f.rule == "RG003"]
    assert hits, findings
    assert "attempts" in hits[0].message


# -- 12. Unnamed operation overloads ----------------------------------------


def test_parallel_with_an_array_has_no_name():
    """`context.parallel([...])` is the unnamed overload.

    Argument 0 is an array of branch bodies, not a name. Binding it as the name
    scanned every branch for clocks and reported a span covering the whole call
    -- against a file whose author had named all 30 of its operations with string
    literals precisely to avoid this hazard.
    """
    findings = ts_findings(
        "  return await context.parallel([\n"
        "    async () => ({ at: new Date().toISOString() }),\n"
        "    async () => ({ at: new Date().toISOString() }),\n"
        "  ]);",
    )
    assert not [f for f in findings if f.rule == "RG005"], findings


def test_template_literal_name_is_still_checked():
    """A computed name built from a clock must still be caught."""
    findings = ts_findings(
        "  await context.step(`op-${Date.now()}`, async () => 1);",
    )
    assert [f for f in findings if f.rule == "RG005"], findings
