"""The rendered handler must be valid Python before anything is deployed.

A syntax error in a generated handler does not fail locally. It fails after the
zip is built, the function is created, an execution is started and the harness
has waited out a 90-second suspend -- and it fails as an import error that looks
like a platform problem rather than a typo. Compiling every variant here costs
milliseconds and removes a whole class of wasted AWS runs.
"""

from __future__ import annotations

import ast

import pytest

from replayguard.probe.handler_template import BODIES, render


@pytest.mark.parametrize("body", sorted(BODIES))
def test_every_variant_renders_valid_python(body):
    ast.parse(render(body, wait_seconds=42))


@pytest.mark.parametrize("body", sorted(BODIES))
def test_every_variant_defines_the_handler(body):
    tree = ast.parse(render(body, wait_seconds=42))
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    assert "handler" in names
    assert "record" in names


def test_wait_seconds_is_substituted():
    source = render("baseline", wait_seconds=137)
    assert "137" in source
    assert "__WAIT__" not in source


def test_no_placeholders_survive_rendering():
    for body in BODIES:
        source = render(body, wait_seconds=10)
        assert "__BODY__" not in source
        assert "__SDK_VERSION_EXPR__" not in source


def test_unknown_body_is_rejected_by_name(tmp_path):
    with pytest.raises(KeyError, match="unknown body"):
        render("no_such_variant", wait_seconds=10)


def test_control_and_renamed_differ_only_in_the_step_name():
    """The mutation under test must be the *only* difference.

    If the renamed variant differed from the baseline in any other way, a
    failure could not be attributed to the rename, and the scenario would prove
    nothing. This pins that.
    """
    a = render("baseline", wait_seconds=30)
    b = render("step_renamed", wait_seconds=30)

    differing = [
        (x, y)
        for x, y in zip(a.splitlines(), b.splitlines(), strict=True)
        if x != y
    ]
    assert len(differing) == 1
    assert "step_one" in differing[0][0]
    assert "step_one_renamed" in differing[0][1]


def test_side_effect_uses_an_atomic_add_not_a_put():
    """A PUT would make a re-executed step indistinguishable from a single run."""
    source = render("baseline", wait_seconds=10)
    assert "ADD hits :one" in source
    assert "put_item" not in source


def test_probe_is_read_outside_any_step():
    """D10 depends on this being module scope.

    If the environment read moved inside a step it would be checkpointed, the
    scenario would trivially pass, and the result would be meaningless.
    """
    tree = ast.parse(render("baseline", wait_seconds=10))
    module_level_assignments = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "DRIFT_PROBE" in module_level_assignments
