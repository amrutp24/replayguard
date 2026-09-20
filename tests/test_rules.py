"""Rule behaviour, against synthetic packages written here.

Every rule gets a pair: one tree that must fire it and one that must not. The
negative half is the point. A detector that has only ever been shown failing
input cannot distinguish "clean" from "broken", and the headline this project
publishes -- that one release in eleven was safe for an in-flight execution --
is only worth anything if a silent report means silence rather than a bug.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from replayguard.drift.rules import Detection, Severity, compare
from replayguard.drift.surface import extract


def build(tmp_path: Path, name: str, modules: dict[str, str]) -> Path:
    root = tmp_path / name / "pkg"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf8")
    for module, source in modules.items():
        path = root / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf8")
    return root


def findings_for(tmp_path, old_modules, new_modules):
    old = extract(build(tmp_path, "old", old_modules), "1.0.0")
    new = extract(build(tmp_path, "new", new_modules), "2.0.0")
    return compare(old, new)


def rules_fired(findings) -> set[str]:
    return {f.rule for f in findings}


# --------------------------------------------------------------------------
# The canary: identical input must produce nothing at all.
# --------------------------------------------------------------------------


def test_identical_packages_produce_no_findings(tmp_path):
    modules = {
        "state.py": "class CheckpointedResult:\n    def result(self):\n        return 1\n",
        "exceptions.py": "class StepError(Exception):\n    pass\n",
    }
    assert findings_for(tmp_path, modules, modules) == []


def test_comment_and_docstring_edits_are_not_drift(tmp_path):
    """The rule that keeps RD005 credible.

    A release that only rewords documentation must be indistinguishable from a
    release that changes nothing. Without this, every version bump fires the
    replay-path rules and the reader learns to skip them.
    """
    old = {
        "state.py": '''
            """Old wording."""
            def replay(state):
                # an explanatory comment
                return state
        '''
    }
    new = {
        "state.py": '''
            """Completely rewritten prose, several sentences, new examples."""
            def replay(state):
                # a different and much longer explanatory comment
                return state
        '''
    }
    assert findings_for(tmp_path, old, new) == []


def test_reformatting_is_not_drift(tmp_path):
    old = {"state.py": "def replay(a,b):\n    return (a,b)\n"}
    new = {
        "state.py": """
            def replay(
                a,
                b,
            ):
                return (
                    a,
                    b,
                )
        """
    }
    assert findings_for(tmp_path, old, new) == []


# --------------------------------------------------------------------------
# RD002 / RD003 -- the exception rules, which are why this tool exists.
# --------------------------------------------------------------------------


def test_rd002_removed_exception_is_in_flight(tmp_path):
    old = {"exceptions.py": "class CallableRuntimeError(Exception):\n    pass\n"}
    new = {"exceptions.py": "class StepError(Exception):\n    pass\n"}

    findings = findings_for(tmp_path, old, new)
    rd002 = [f for f in findings if f.rule == "RD002"]

    assert len(rd002) == 1
    assert rd002[0].subject.endswith("CallableRuntimeError")
    assert rd002[0].in_flight is True
    assert rd002[0].severity is Severity.HIGH


def test_rd003_reparenting_is_silent_and_in_flight(tmp_path):
    """The exact shape of the real CallbackError change."""
    old = {
        "exceptions.py": """
            class ExecutionError(Exception):
                pass
            class CallbackError(ExecutionError):
                pass
        """
    }
    new = {
        "exceptions.py": """
            class ExecutionError(Exception):
                pass
            class DurableOperationError(Exception):
                pass
            class CallbackError(DurableOperationError):
                pass
        """
    }

    findings = findings_for(tmp_path, old, new)
    rd003 = [f for f in findings if f.rule == "RD003"]

    assert len(rd003) == 1
    assert rd003[0].detection is Detection.SILENT
    assert rd003[0].in_flight is True
    assert "ExecutionError" in rd003[0].detail


def test_rd003_ignores_a_base_added_without_losing_one(tmp_path):
    """Widening a hierarchy is not drift.

    Gaining a base class cannot stop an existing `except` clause matching, so it
    must not fire. Only a *lost* ancestor changes what gets caught.
    """
    old = {
        "exceptions.py": """
            class Base(Exception):
                pass
            class Thing(Base):
                pass
        """
    }
    new = {
        "exceptions.py": """
            class Base(Exception):
                pass
            class Extra(Exception):
                pass
            class Thing(Base, Extra):
                pass
        """
    }
    assert [f for f in findings_for(tmp_path, old, new) if f.rule == "RD003"] == []


def test_rd003_follows_a_transitive_ancestor(tmp_path):
    """Losing a grandparent counts, because `except Grandparent:` stops matching."""
    old = {
        "exceptions.py": """
            class Root(Exception):
                pass
            class Middle(Root):
                pass
            class Leaf(Middle):
                pass
        """
    }
    new = {
        "exceptions.py": """
            class Root(Exception):
                pass
            class Other(Exception):
                pass
            class Middle(Other):
                pass
            class Leaf(Middle):
                pass
        """
    }
    rd003 = [f for f in findings_for(tmp_path, old, new) if f.rule == "RD003"]
    subjects = {f.subject.rsplit("::", 1)[-1] for f in rd003}
    assert "Leaf" in subjects, "a lost grandparent must be reported on the leaf too"


# --------------------------------------------------------------------------
# RD001 / RD004 -- build-time findings.
# --------------------------------------------------------------------------


def test_rd001_removed_function_is_build_time_not_in_flight(tmp_path):
    old = {"helpers.py": "def helper(a):\n    return a\n"}
    new = {"helpers.py": "def other(a):\n    return a\n"}

    rd001 = [f for f in findings_for(tmp_path, old, new) if f.rule == "RD001"]
    assert len(rd001) == 1
    assert rd001[0].detection is Detection.BUILD
    assert rd001[0].in_flight is False


def test_rd001_folds_methods_into_their_removed_class(tmp_path):
    old = {
        "helpers.py": """
            class Gone:
                def a(self):
                    pass
                def b(self):
                    pass
                def c(self):
                    pass
        """
    }
    new = {"helpers.py": "class Kept:\n    pass\n"}

    rd001 = [f for f in findings_for(tmp_path, old, new) if f.rule == "RD001"]
    assert len(rd001) == 1, "the class, not the class plus each of its methods"
    assert rd001[0].subject.endswith("Gone")


def test_rd004_signature_change_on_replay_path_is_in_flight(tmp_path):
    old = {"state.py": "def replay(state, ctx):\n    return state\n"}
    new = {"state.py": "def replay(state, ctx, checkpointed_result):\n    return state\n"}

    rd004 = [f for f in findings_for(tmp_path, old, new) if f.rule == "RD004"]
    assert len(rd004) == 1
    assert rd004[0].in_flight is True
    assert "checkpointed_result" in rd004[0].detail


def test_rd004_signature_change_off_replay_path_is_not_in_flight(tmp_path):
    old = {"helpers.py": "def fmt(a):\n    return a\n"}
    new = {"helpers.py": "def fmt(a, b):\n    return a\n"}

    rd004 = [f for f in findings_for(tmp_path, old, new) if f.rule == "RD004"]
    assert len(rd004) == 1
    assert rd004[0].in_flight is False
    assert rd004[0].severity is Severity.MEDIUM


# --------------------------------------------------------------------------
# RD005 / RD006 -- module-level replay-path rules.
# --------------------------------------------------------------------------


def test_rd006_fires_on_format_modules_and_rd005_on_other_replay_modules(tmp_path):
    old = {
        "serdes.py": "def dumps(v):\n    return str(v)\n",
        "operation/step.py": "def run(f):\n    return f()\n",
    }
    new = {
        "serdes.py": "def dumps(v):\n    return repr(v)\n",
        "operation/step.py": "def run(f):\n    return f() or None\n",
    }

    findings = findings_for(tmp_path, old, new)
    by_rule = {f.rule: f for f in findings}

    assert by_rule["RD006"].subject == "serdes.py"
    assert by_rule["RD006"].severity is Severity.HIGH
    assert by_rule["RD005"].subject == "operation/step.py"
    assert all(f.detection is Detection.SILENT for f in findings if f.rule in {"RD005", "RD006"})


def test_replay_path_rules_ignore_modules_off_the_path(tmp_path):
    old = {"logger.py": "def log(m):\n    print(m)\n"}
    new = {"logger.py": "def log(m):\n    print(m.upper())\n"}

    assert rules_fired(findings_for(tmp_path, old, new)) & {"RD005", "RD006"} == set()


# --------------------------------------------------------------------------
# Ordering -- the report is only useful if the dangerous cell comes first.
# --------------------------------------------------------------------------


def test_in_flight_silent_findings_sort_first(tmp_path):
    old = {
        "helpers.py": "def gone(a):\n    return a\n",
        "exceptions.py": """
            class Base(Exception):
                pass
            class Thing(Base):
                pass
        """,
    }
    new = {
        "helpers.py": "",
        "exceptions.py": """
            class Base(Exception):
                pass
            class Other(Exception):
                pass
            class Thing(Other):
                pass
        """,
    }

    findings = findings_for(tmp_path, old, new)
    assert findings[0].in_flight is True
    assert findings[0].detection is Detection.SILENT
    assert findings[-1].in_flight is False


@pytest.mark.parametrize("private", ["_helper", "__secret"])
def test_private_symbols_are_ignored(tmp_path, private):
    old = {"helpers.py": f"def {private}(a):\n    return a\n"}
    new = {"helpers.py": ""}
    assert findings_for(tmp_path, old, new) == []
