"""Classify the difference between two SDK surfaces as drift risk.

The ordering principle here is *when you find out*, not how large the change is.
A removed function is a big change and a harmless one: the import fails, you see
it before you deploy, you fix it. An exception class that quietly stops being
raised is a small change that reaches production intact, because the `except`
clause that no longer matches is still valid Python.

So findings carry two axes. `severity` is how much damage is available, and
`detection` is whether anything tells you. The cell worth reading first is
HIGH + SILENT, and the report sorts for it.

Nothing in this module talks to AWS. The comparison is between two trees of
source, so it runs offline, in CI, before an upgrade rather than after one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from replayguard.drift.surface import FORMAT_MODULES, REPLAY_PATH_MODULES, Surface


class Detection(str, Enum):
    """When you find out."""

    #: Import or attribute error. You cannot deploy this without seeing it.
    BUILD = "build"
    #: Valid code, changed behaviour, no diagnostic. This is the dangerous one.
    SILENT = "silent"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Finding:
    rule: str
    title: str
    severity: Severity
    detection: Detection
    subject: str
    detail: str
    #: True when this can change the outcome of an execution that was already
    #: suspended when the version changed -- as opposed to only affecting code
    #: you are about to write. This is the column that makes the report about
    #: durable execution rather than about packaging.
    in_flight: bool

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        sev = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}[self.severity]
        return (
            0 if self.in_flight else 1,
            0 if self.detection is Detection.SILENT else 1,
            sev,
            self.subject,
        )


RULES: dict[str, str] = {
    "RD001": "Public symbol removed",
    "RD002": "Exception class removed",
    "RD003": "Exception reparented",
    "RD004": "Public signature changed",
    "RD005": "Replay-path internals changed",
    "RD006": "Checkpoint format module changed",
}


def _ancestors(name: str, table: dict[str, tuple[str, ...]]) -> set[str]:
    """Every base class reachable from `name`, by name.

    The table only holds classes defined in this SDK, so a chain that leaves the
    package (into `Exception`) simply stops. That is the right behaviour: an
    `except Exception` clause matches regardless and is not drift.
    """
    seen: set[str] = set()
    stack = list(table.get(name, ()))
    while stack:
        base = stack.pop()
        if base in seen:
            continue
        seen.add(base)
        stack.extend(table.get(base, ()))
    return seen


def compare(old: Surface, new: Surface) -> list[Finding]:
    """Findings for upgrading from `old` to `new`."""
    findings: list[Finding] = []

    findings += _removed_symbols(old, new)
    findings += _changed_signatures(old, new)
    findings += _exception_changes(old, new)
    findings += _module_changes(old, new)

    return sorted(findings, key=lambda f: f.sort_key)


def _removed_symbols(old: Surface, new: Surface) -> list[Finding]:
    out: list[Finding] = []

    # A class that is gone takes its methods with it. Reporting each method as
    # its own finding triples the noise and says nothing the class-level finding
    # does not, so members of a removed class are folded into it.
    removed_classes = {
        sym.name
        for key, sym in old.symbols.items()
        if sym.kind == "class" and key not in new.symbols
    }

    for key, sym in old.symbols.items():
        if key in new.symbols:
            continue
        # Exception removals are RD002's job and carry a different detection
        # story; reporting them twice would double-count the worst findings.
        if sym.kind == "class" and sym.name in old.exceptions:
            continue
        if sym.kind == "method" and sym.name.split(".", 1)[0] in removed_classes:
            continue
        out.append(
            Finding(
                rule="RD001",
                title=RULES["RD001"],
                severity=Severity.MEDIUM,
                detection=Detection.BUILD,
                subject=key,
                detail=(
                    f"{sym.kind} {sym.name} existed in {old.version} and is gone in "
                    f"{new.version}. Code referencing it fails at import."
                ),
                # A removed symbol cannot alter an execution that is already
                # suspended: if the handler referenced it, the handler no longer
                # loads at all, which is a deploy failure and not a replay one.
                in_flight=False,
            )
        )
    return out


def _changed_signatures(old: Surface, new: Surface) -> list[Finding]:
    out: list[Finding] = []
    for key, sym in old.symbols.items():
        other = new.symbols.get(key)
        if other is None or sym.params is None or other.params is None:
            continue
        if sym.params == other.params:
            continue

        added = [p for p in other.params if p not in sym.params]
        dropped = [p for p in sym.params if p not in other.params]

        # A parameter added to something on the replay path is the signal that
        # the engine now needs information it did not need before -- the 2.0
        # `replay(..., checkpointed_result)` change is the example. That is a
        # semantic change to replay, not a packaging detail.
        on_replay = sym.on_replay_path
        out.append(
            Finding(
                rule="RD004",
                title=RULES["RD004"],
                severity=Severity.HIGH if on_replay else Severity.MEDIUM,
                detection=Detection.BUILD,
                subject=key,
                detail=(
                    f"{sym.signature()} -> {other.signature()}"
                    + (f"; added {', '.join(added)}" if added else "")
                    + (f"; dropped {', '.join(dropped)}" if dropped else "")
                    + (
                        ". On the replay path: the engine's contract changed, not "
                        "just its callers."
                        if on_replay
                        else ""
                    )
                ),
                in_flight=on_replay,
            )
        )
    return out


def _exception_changes(old: Surface, new: Surface) -> list[Finding]:
    """The rules that justify this tool existing.

    Both findings here are invisible to a type checker, a linter, and a test
    suite that does not deliberately fail a step. An `except SomeError:` naming
    a class that no longer gets raised is well-formed code that silently stops
    handling the case it was written for.
    """
    out: list[Finding] = []

    old_bases = {n: e.bases for n, e in old.exceptions.items()}
    new_bases = {n: e.bases for n, e in new.exceptions.items()}

    for name, exc in old.exceptions.items():
        if name in new.exceptions:
            continue
        out.append(
            Finding(
                rule="RD002",
                title=RULES["RD002"],
                severity=Severity.HIGH,
                detection=Detection.BUILD,
                subject=f"{exc.module}::{name}",
                detail=(
                    f"{name} no longer exists in {new.version}. An `except {name}:` "
                    f"fails at import -- but a handler that caught it was catching "
                    f"a real failure mode, and whatever now replaces it is "
                    f"uncaught until the handler is rewritten. On replay this "
                    f"surfaces where a checkpointed failure is re-raised."
                ),
                in_flight=True,
            )
        )

    for name in old.exceptions:
        if name not in new.exceptions:
            continue
        before = _ancestors(name, old_bases)
        after = _ancestors(name, new_bases)
        lost = before - after
        if not lost:
            continue
        out.append(
            Finding(
                rule="RD003",
                title=RULES["RD003"],
                severity=Severity.HIGH,
                detection=Detection.SILENT,
                subject=f"{new.exceptions[name].module}::{name}",
                detail=(
                    f"{name} was a subclass of {', '.join(sorted(lost))} in "
                    f"{old.version} and is not in {new.version} "
                    f"(now: {', '.join(new_bases.get(name, ())) or 'no local base'}). "
                    f"`except {sorted(lost)[0]}:` still compiles and still runs and "
                    f"no longer catches {name}."
                ),
                in_flight=True,
            )
        )

    return out


def _module_changes(old: Surface, new: Surface) -> list[Finding]:
    """Modules on the replay path whose *code* changed.

    Deliberately coarse. This rule does not try to understand the change -- it
    reports that the code deciding how a checkpoint is replayed is not the code
    that wrote the checkpoints you already have. That is the fact a pinned
    dependency does not fix, because the other half of the contract lives in the
    platform's checkpoint store rather than in your bundle.

    Compared by structural fingerprint, so a reformat, a new comment or a
    reworded docstring does not fire it. Without that this rule would be noise
    on every release and would be ignored on the release that mattered.
    """
    out: list[Finding] = []
    for module, fp in old.fingerprints.items():
        other = new.fingerprints.get(module)
        if other is None or other == fp:
            continue

        fmt = module.endswith(FORMAT_MODULES)
        if not module.endswith(REPLAY_PATH_MODULES):
            continue

        out.append(
            Finding(
                rule="RD006" if fmt else "RD005",
                title=RULES["RD006"] if fmt else RULES["RD005"],
                severity=Severity.HIGH if fmt else Severity.MEDIUM,
                detection=Detection.SILENT,
                subject=module,
                detail=(
                    "serialization or checkpoint state handling changed; an "
                    "execution that checkpointed under the old version will be "
                    "replayed by this code"
                    if fmt
                    else "replay-path module changed with no public API change"
                ),
                in_flight=True,
            )
        )
    return out
