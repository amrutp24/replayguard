"""Version drift: does an SDK upgrade endanger executions already in flight?

The other half of the replay contract. `replayguard check` asks whether your
handler is deterministic. This asks whether the code that *resumes* an execution
still matches the code that *suspended* it -- which is a different question, and
one a determinism check cannot answer. A handler can be perfectly deterministic
and still be corrupted by a deploy that lands while it is suspended.

Offline and stdlib-only: it compares two SDK versions on disk. No AWS account.
"""
