"""Dynamic replay-divergence checking.

Static analysis reasons about what code *might* do. This runs the handler and
measures what it *did*, twice, under two different worlds -- so it catches
nondeterminism it has no rule for: inside a library, in an iteration order, in
a value tainted many hops back.
"""

from .diverge import Divergence, Report, assert_deterministic, check_handler
from .journal import Entry, Journal

__all__ = [
    "Divergence",
    "Entry",
    "Journal",
    "Report",
    "assert_deterministic",
    "check_handler",
]
