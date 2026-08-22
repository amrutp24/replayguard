"""Perturbation — making nondeterminism show itself.

Running a handler twice and diffing the journals catches almost nothing on its
own. Two runs a millisecond apart usually produce the same `datetime.now().hour`
and the same branch, so a genuinely broken handler looks stable. The bug is
latent, which is exactly why it survives into production and surfaces months
later on a resume.

So the second run is executed under a *different world*: the clock is moved,
the random source is reseeded, and identity generators return different values.
A handler that is a pure function of its inputs and its checkpointed results is
unaffected by any of that. One that is not changes its journal, and the change
is the evidence.

This is the part static analysis cannot do. It does not need to recognise the
source of nondeterminism -- a clock, a library that reads one internally, an
iteration order, a locale -- only that the execution shape moved when the world
did.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import random as _random
import time as _time
import uuid as _uuid
from collections.abc import Iterator

#: How far the clock jumps between runs. Large enough to cross the boundaries
#: real handlers branch on -- hour, business day, month -- because a jitter of
#: milliseconds would leave most latent bugs latent.
_CLOCK_SKEW_SECONDS = 13 * 60 * 60 + 7 * 60 + 11


class _ShiftedDatetime(_datetime.datetime):
    """A `datetime` whose `now`/`utcnow`/`today` are offset.

    Subclassing keeps `isinstance(x, datetime)` true and arithmetic intact, so
    handlers that do real date work still behave -- they just believe it is a
    different moment.
    """

    _offset = _datetime.timedelta(seconds=_CLOCK_SKEW_SECONDS)
    #: Bound at class-creation time, which happens at import -- before any
    #: patching. Calling `_datetime.datetime.now()` here instead would resolve
    #: through the patched module attribute back into this class and recurse
    #: until the stack blows.
    _real = _datetime.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - mirrors datetime.now
        return cls._real.now(tz) + cls._offset

    @classmethod
    def utcnow(cls):  # noqa: D102
        return cls._real.utcnow() + cls._offset

    @classmethod
    def today(cls):  # noqa: D102
        return cls._real.today() + cls._offset


@contextlib.contextmanager
def perturbed(seed: int = 0x5EED) -> Iterator[None]:
    """Run the block in a world with a different clock, entropy, and identity.

    Patches the source modules rather than each handler's namespace, so a
    handler reaching nondeterminism through a third-party library is perturbed
    too -- which is the whole point of doing this dynamically.
    """
    real_time, real_monotonic = _time.time, _time.monotonic
    real_datetime = _datetime.datetime
    real_uuid1, real_uuid4 = _uuid.uuid1, _uuid.uuid4
    real_state = _random.getstate()

    def shifted_time() -> float:
        return real_time() + _CLOCK_SKEW_SECONDS

    def shifted_monotonic() -> float:
        return real_monotonic() + _CLOCK_SKEW_SECONDS

    counter = {"n": 0}

    def stable_but_different_uuid():
        # Deterministic within the run so a *correct* handler is not made to
        # look flaky, but different from the baseline run so a handler that
        # leaks a uuid into an operation name diverges visibly.
        counter["n"] += 1
        return _uuid.UUID(int=(seed << 64) + counter["n"])

    _time.time = shifted_time
    _time.monotonic = shifted_monotonic
    _datetime.datetime = _ShiftedDatetime
    _uuid.uuid1 = stable_but_different_uuid
    _uuid.uuid4 = stable_but_different_uuid
    _random.seed(seed)
    try:
        yield
    finally:
        _time.time = real_time
        _time.monotonic = real_monotonic
        _datetime.datetime = real_datetime
        _uuid.uuid1 = real_uuid1
        _uuid.uuid4 = real_uuid4
        _random.setstate(real_state)


@contextlib.contextmanager
def baseline(seed: int = 0x1234) -> Iterator[None]:
    """The control run.

    Randomness is seeded here too. Without it, a handler using `random` diverges
    between *any* two runs and every handler looks broken -- the comparison has
    to isolate the perturbation as the only difference that matters.
    """
    real_state = _random.getstate()
    _random.seed(seed)
    try:
        yield
    finally:
        _random.setstate(real_state)
