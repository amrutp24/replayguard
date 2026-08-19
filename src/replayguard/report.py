"""Reporters.

Three formats, three audiences: `text` for a human at a terminal, `json` for
anything downstream, `sarif` so GitHub code scanning renders findings inline on
the pull request that introduced them. The last one matters most for a rule
class whose whole problem is that nobody notices it until months later.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .findings import Finding, Severity

_TOOL_URI = "https://github.com/amrutp24/replayguard"

_SARIF_LEVEL = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.NOTE: "note",
}


def _rel(path: str, root: str | None) -> str:
    if not root:
        return path
    try:
        import os

        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path


def text(findings: Sequence[Finding], root: str | None = None, verbose: bool = False) -> str:
    if not findings:
        return "replayguard: no determinism violations found."

    lines: list[str] = []
    current_file = None
    for f in findings:
        if f.loc.file != current_file:
            current_file = f.loc.file
            lines.append("")
            lines.append(_rel(current_file, root))
        lines.append(
            f"  {f.loc.line:>4}:{f.loc.col:<3} {f.severity.value:<7} "
            f"{f.rule}  {f.message}"
        )
        if verbose:
            if f.rationale:
                lines.append(f"         why: {f.rationale}")
            if f.fix:
                lines.append(f"         fix: {f.fix}")

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    summary = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
    lines.append("")
    lines.append(f"{len(findings)} finding(s): {summary}")
    if not verbose:
        lines.append("Run with --explain for the rationale and the fix.")
    return "\n".join(lines).lstrip("\n")


def as_json(findings: Sequence[Finding], root: str | None = None) -> str:
    return json.dumps(
        {
            "version": 1,
            "findings": [
                {
                    "rule": f.rule,
                    "message": f.message,
                    "severity": f.severity.value,
                    "confidence": f.confidence.value,
                    "file": _rel(f.loc.file, root),
                    "line": f.loc.line,
                    "column": f.loc.col,
                    "endLine": f.loc.end_line,
                    "endColumn": f.loc.end_col,
                    "rationale": f.rationale,
                    "fix": f.fix,
                }
                for f in findings
            ],
        },
        indent=2,
    )


def sarif(findings: Sequence[Finding], root: str | None = None) -> str:
    """SARIF 2.1.0, the format GitHub code scanning ingests."""
    rules_seen: dict[str, Finding] = {}
    for f in findings:
        rules_seen.setdefault(f.rule, f)

    return json.dumps(
        {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "replayguard",
                            "informationUri": _TOOL_URI,
                            "rules": [
                                {
                                    "id": rule_id,
                                    "shortDescription": {"text": sample.message},
                                    "fullDescription": {"text": sample.rationale},
                                    "help": {"text": sample.fix},
                                    "defaultConfiguration": {
                                        "level": _SARIF_LEVEL[sample.severity]
                                    },
                                }
                                for rule_id, sample in sorted(rules_seen.items())
                            ],
                        }
                    },
                    "results": [
                        {
                            "ruleId": f.rule,
                            "level": _SARIF_LEVEL[f.severity],
                            "message": {"text": f"{f.message}. {f.rationale}"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {
                                            "uri": _rel(f.loc.file, root)
                                        },
                                        "region": {
                                            "startLine": max(f.loc.line, 1),
                                            "startColumn": max(f.loc.col + 1, 1),
                                        },
                                    }
                                }
                            ],
                        }
                        for f in findings
                    ],
                }
            ],
        },
        indent=2,
    )


FORMATS = {"text": text, "json": as_json, "sarif": sarif}
