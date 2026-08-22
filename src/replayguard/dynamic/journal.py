"""Operation journals — what a durable execution actually did.

A durable execution is matched to its checkpoints by operation *name and order*.
So the journal, not the return value, is the thing that has to be stable: two
executions of the same handler over the same input must request the same
operations in the same sequence, or replay cannot line them up.

This captures that sequence in a comparable form. Deliberately excluded:

  * **operation results** — a step exists precisely so its result can be
    nondeterministic and then checkpointed. Diffing results would flag every
    correct handler.
  * **timestamps and durations** — wall-clock by definition.
  * **attempt counts** — a retry is not a determinism failure.

What remains is the shape of the execution, which is exactly what replay
depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entry:
    """One operation, reduced to what replay actually matches on."""

    #: Operation kind - step, wait, callback, invoke, context.
    kind: str
    #: The customer-provided name. None when the overload takes no name.
    name: str | None
    #: Depth in the operation tree; child contexts nest.
    depth: int = 0

    def render(self) -> str:
        pad = "  " * self.depth
        return f"{pad}{self.kind}({self.name if self.name is not None else '<unnamed>'})"


@dataclass
class Journal:
    """The ordered operations of one execution."""

    entries: list[Entry] = field(default_factory=list)
    #: Set when the handler raised. A run that fails on one pass and not the
    #: other is itself a divergence worth reporting.
    error: str | None = None

    def __len__(self) -> int:
        return len(self.entries)

    def signature(self) -> tuple:
        """The comparable form: kinds, names, and nesting in order."""
        return tuple((e.kind, e.name, e.depth) for e in self.entries)

    def render(self) -> str:
        return "\n".join(e.render() for e in self.entries) or "(no operations)"


def _kind_of(operation: object) -> str:
    """`StepOperation` -> `step`, `WaitOperation` -> `wait`."""
    name = type(operation).__name__
    if name.endswith("Operation"):
        name = name[: -len("Operation")]
    return name.lower() or "operation"


def from_result(result: object) -> Journal:
    """Build a journal from a testing-SDK run result.

    Reads through `get_all_operations()` and the `child_operations` tree rather
    than assuming a flat list, because `runInChildContext` and `parallel` nest --
    and a change in nesting is a divergence even when the flat order matches.
    """
    journal = Journal()

    error = getattr(result, "error", None)
    if error:
        journal.error = str(error)

    def walk(operations: object, depth: int) -> None:
        for op in operations or ():
            journal.entries.append(
                Entry(
                    kind=_kind_of(op),
                    name=getattr(op, "name", None),
                    depth=depth,
                )
            )
            walk(getattr(op, "child_operations", None), depth + 1)

    getter = getattr(result, "get_all_operations", None)
    walk(getter() if callable(getter) else (), 0)
    return journal
