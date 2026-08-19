"""Findings and severities.

Severity is about *consequence on replay*, not about how confident the checker
is — confidence is tracked separately. A violation that silently corrupts state
is an ERROR even when detection is heuristic, and a stylistic nit stays a
WARNING even when detection is certain. Conflating the two axes is how linters
end up with everything at the same level.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .ir import Location


class Severity(str, Enum):
    #: Will corrupt state or produce wrong results on replay.
    ERROR = "error"
    #: Probably wrong, or correct only under assumptions worth stating.
    WARNING = "warning"
    #: Informational — coverage gaps, unresolved regions.
    NOTE = "note"

    @property
    def rank(self) -> int:
        return {"error": 3, "warning": 2, "note": 1}[self.value]


class Confidence(str, Enum):
    """How sure the checker is that this is a real violation.

    Kept orthogonal to severity so that `--fail-on error` and
    `--min-confidence high` are independent knobs. A CI gate usually wants high
    confidence at any severity; a code review wants everything.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


@dataclass
class Finding:
    rule: str
    message: str
    loc: Location
    severity: Severity
    confidence: Confidence
    #: Why this matters on replay — shown in verbose output, not every line.
    rationale: str = ""
    #: Concrete remediation, ideally the shape of the fix.
    fix: str = ""

    def sort_key(self) -> tuple:
        return (
            -self.severity.rank,
            -self.confidence.rank,
            self.loc.file,
            self.loc.line,
            self.loc.col,
            self.rule,
        )
