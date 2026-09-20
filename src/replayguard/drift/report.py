"""Render findings.

Two formats. Text is for a terminal and leads with the finding that reaches
production: in-flight and silent, sorted first, because a report whose first
screen is forty renamed private helpers has buried its own point.

JSON is for committing. Every claim this project publishes should be traceable
to a machine-readable record of the run that produced it, and a table pasted
into an article is not that.

User-facing strings stay ASCII. The console this is developed against is cp1252
and a stray arrow glyph turns a report into a UnicodeEncodeError.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from replayguard.drift.rules import Detection, Finding, Severity


def _bar(text: str, char: str = "=") -> str:
    return char * max(8, min(78, len(text)))


def to_json(
    findings: list[Finding], *, package: str, old: str, new: str, meta: dict | None = None
) -> str:
    payload = {
        "package": package,
        "from_version": old,
        "to_version": new,
        "counts": counts(findings),
        "findings": [asdict(f) | {"severity": f.severity.value} for f in findings],
    }
    if meta:
        payload["meta"] = meta
    return json.dumps(payload, indent=2, sort_keys=False)


def counts(findings: list[Finding]) -> dict[str, int]:
    return {
        "total": len(findings),
        "in_flight": sum(1 for f in findings if f.in_flight),
        "silent": sum(1 for f in findings if f.detection is Detection.SILENT),
        "in_flight_and_silent": sum(
            1 for f in findings if f.in_flight and f.detection is Detection.SILENT
        ),
        "high": sum(1 for f in findings if f.severity is Severity.HIGH),
    }


def to_text(findings: list[Finding], *, package: str, old: str, new: str) -> str:
    head = f"{package}: {old} -> {new}"
    lines = [head, _bar(head), ""]

    if not findings:
        lines.append("No drift risk found on the replay path.")
        lines.append("")
        lines.append(
            "That is a result, not a guarantee: this compares source surfaces, so"
        )
        lines.append(
            "a behaviour change made entirely inside one function body with no"
        )
        lines.append("signature change is invisible to it.")
        return "\n".join(lines)

    c = counts(findings)
    lines.append(
        f"{c['total']} findings   {c['in_flight']} can affect an in-flight execution"
        f"   {c['silent']} are silent"
    )
    lines.append("")

    inflight = [f for f in findings if f.in_flight]
    rest = [f for f in findings if not f.in_flight]

    if inflight:
        lines.append("AFFECTS EXECUTIONS ALREADY IN FLIGHT")
        lines.append(_bar("AFFECTS EXECUTIONS ALREADY IN FLIGHT", "-"))
        lines.append("")
        lines += [_one(f) for f in inflight]

    if rest:
        lines.append("BUILD-TIME ONLY")
        lines.append(_bar("BUILD-TIME ONLY", "-"))
        lines.append("")
        lines += [_one(f) for f in rest]

    return "\n".join(lines)


def _one(f: Finding) -> str:
    flag = "SILENT" if f.detection is Detection.SILENT else "build"
    out = [f"  [{f.rule}] {f.severity.value.upper():6s} {flag:6s} {f.subject}"]
    for chunk in _wrap(f.detail, 72):
        out.append(f"      {chunk}")
    out.append("")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = text.split(), "", []
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out
