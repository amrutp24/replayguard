"""Normalized intermediate representation for durable handlers.

Every language frontend lowers source into this IR, and every rule operates on
it. That split is the whole architecture: adding a language means writing a
frontend, not reimplementing the rules, and a rule fix lands for every language
at once.

The IR is deliberately shallow. It is not a general-purpose AST — it captures
only what determinism rules need to reason about:

  * where the code is (durable context vs step body),
  * what it calls,
  * what it branches on,
  * what it writes to that it does not own.

Anything a rule doesn't need stays in the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Language(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVA = "java"


class Region(str, Enum):
    """Which side of the replay boundary a node sits on.

    The distinction drives almost every rule, because the two regions have
    *opposite* obligations:

    DURABLE   — code outside any step. Re-executes from the top on every replay,
                so it must be a pure function of handler inputs and completed
                step results. I/O, clocks, and randomness are violations here.

    STEP_BODY — code inside a step. Runs at most once and is checkpointed, so
                I/O is exactly what it's for. The hazard is the reverse: writes
                to state outside the step are silently dropped on replay,
                because the body doesn't re-run.

    UNKNOWN   — the frontend could not resolve the region. Rules must not report
                violations here; see `Confidence`. Reporting on unresolved
                regions is how a linter earns a reputation for false positives
                and gets switched off.
    """

    DURABLE = "durable"
    STEP_BODY = "step_body"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Location:
    file: str
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


@dataclass
class Call:
    """A function call, with its name resolved as far as the frontend can.

    `dotted` is the best-effort qualified name — `time.time`, `boto3.client`,
    `Math.random`. Frontends resolve import aliases into canonical names where
    they can (`import time as t; t.time()` -> `time.time`) because the symbol
    catalog is written against canonical names.
    """

    dotted: str
    loc: Location
    region: Region
    #: Names bound at import that this call resolved through, for diagnostics.
    resolved_via: str | None = None
    #: True when the receiver is a client bound to an external service, e.g. a
    #: module-level `boto3.resource(...).Table(...)`. Method names on such
    #: clients are unbounded, so they can't be catalogued by name.
    external_client: bool = False
    #: How to render this call in a message, when the catalog key is not what
    #: the developer wrote — `Date` is the lookup key, `new Date()` is the code.
    display: str | None = None
    #: Helper functions traversed to reach this call, outermost first. Empty
    #: when the call sits directly in the handler. A violation inside a helper
    #: is invisible at the call site, so the path is what makes it actionable.
    via: tuple[str, ...] = ()

    @property
    def shown(self) -> str:
        return self.display or self.dotted


@dataclass
class Branch:
    """A conditional whose outcome must be stable across replay."""

    loc: Location
    region: Region
    #: Dotted names appearing in the condition, used to trace nondeterminism.
    condition_symbols: list[str] = field(default_factory=list)
    #: Helper functions traversed to reach this branch, outermost first. Carried
    #: for the same reason as on Call and OuterWrite: a branch inside a helper is
    #: invisible at the call site.
    via: tuple[str, ...] = ()


@dataclass
class OuterWrite:
    """A write to a binding the enclosing function does not own.

    Inside a step body this is the silent-data-loss bug: the write lands on the
    first run and is skipped on replay, so the value reverts with no error.
    """

    target: str
    loc: Location
    region: Region
    #: True when the target is module-level rather than a captured closure var.
    is_global: bool = False
    #: Helper functions traversed to reach this write, outermost first.
    via: tuple[str, ...] = ()
    #: Identity of the step body containing the write, for the read-back check.
    step_id: int | None = None


@dataclass
class Read:
    """A name being read, and where from.

    RG003 needs this to tell a lost update from a harmless one. A value written
    in a step body and never read anywhere else cannot corrupt anything when the
    write is skipped on replay; one that is read elsewhere can.
    """

    name: str
    loc: Location
    region: Region
    #: Identity of the enclosing step body, or None in the durable region. Two
    #: different step bodies are "elsewhere" from each other: neither re-runs on
    #: replay, so a value written in one and read in another is still stale.
    step_id: int | None = None


@dataclass
class Step:
    """A durable operation — `context.step`, `wait_for_callback`, `invoke`.

    `name_is_static` matters more than it looks. Checkpoints are matched by
    name and order across replays, so a name built from a timestamp or a uuid
    cannot be matched on resume.
    """

    kind: str
    loc: Location
    name_literal: str | None = None
    name_is_static: bool = True
    #: Dotted names appearing in a computed step name. A name built from a loop
    #: index is fine; one built from a clock is not, and only the symbols can
    #: tell those apart.
    name_symbols: list[str] = field(default_factory=list)


@dataclass
class Handler:
    """One durable handler and everything the rules need to judge it."""

    name: str
    loc: Location
    language: Language
    calls: list[Call] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    outer_writes: list[OuterWrite] = field(default_factory=list)
    #: Every name read, with its region. Consumed only by RG003's read-back
    #: check; cheap to collect and it keeps the rule out of the frontends.
    reads: list[Read] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    #: Regions the frontend could not resolve, surfaced so coverage is honest
    #: rather than silently partial.
    unresolved: list[Location] = field(default_factory=list)


@dataclass
class Module:
    path: str
    language: Language
    handlers: list[Handler] = field(default_factory=list)
