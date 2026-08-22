"""Command line interface.

    replayguard check src/                    # human-readable
    replayguard check src/ --explain          # with rationale and fix
    replayguard check src/ --format sarif -o r.sarif
    replayguard rules                         # what it checks and why
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

from . import report, rules
from .findings import Confidence, Finding, Severity
from .frontends import python_frontend

_PY_EXT = {".py"}
_TS_EXT = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs"}
_JAVA_EXT = {".java"}

_SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    "cdk.out",
    ".terraform",
}


def _discover(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1]
                if ext in _PY_EXT or ext in _TS_EXT or ext in _JAVA_EXT:
                    out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _analyse(path: str) -> tuple[list[Finding], str | None]:
    """Returns (findings, error). A parse failure is reported, never swallowed."""
    ext = os.path.splitext(path)[1]
    try:
        if ext in _PY_EXT:
            module = python_frontend.parse_file(path)
        elif ext in _JAVA_EXT:
            from .frontends import java_frontend

            module = java_frontend.parse_file(path)
        else:
            from .frontends import typescript_frontend

            module = typescript_frontend.parse_file(path)
    except SyntaxError as exc:
        return [], f"{path}: could not parse ({exc.msg})"
    except RuntimeError as exc:
        return [], f"{path}: {exc}"

    findings: list[Finding] = []
    for handler in module.handlers:
        findings.extend(rules.check(handler))
    return findings, None


def _cmd_check(args: argparse.Namespace) -> int:
    files = _discover(args.paths)
    if not files:
        print(
            "replayguard: no Python, TypeScript, or Java files found",
            file=sys.stderr,
        )
        return 2

    all_findings: list[Finding] = []
    errors: list[str] = []
    for path in files:
        found, err = _analyse(path)
        all_findings.extend(found)
        if err:
            errors.append(err)

    min_conf = Confidence(args.min_confidence).rank
    all_findings = [f for f in all_findings if f.confidence.rank >= min_conf]

    # Coverage notes are suppressed by default. On real repositories they
    # outnumbered actual violations roughly 7:1 and buried them. They are never
    # silently dropped -- the count is always reported -- so the tool still
    # cannot imply a clean bill of health it has not earned.
    suppressed = 0
    if not args.show_coverage_gaps:
        keep = [f for f in all_findings if f.severity is not Severity.NOTE]
        suppressed = len(all_findings) - len(keep)
        all_findings = keep

    all_findings.sort(key=lambda f: f.sort_key())

    renderer = report.FORMATS[args.format]
    out = (
        renderer(all_findings, args.root, verbose=args.explain)
        if args.format == "text"
        else renderer(all_findings, args.root)
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"replayguard: wrote {args.format} to {args.output}", file=sys.stderr)
    else:
        print(out)

    # stderr, not stdout: --format json/sarif must stay machine-parseable, and
    # this notice corrupted both when it was printed alongside the document.
    if suppressed:
        print(
            f"replayguard: {suppressed} region(s) could not be resolved and "
            "were not analysed. Re-run with --show-coverage-gaps to see them.",
            file=sys.stderr,
        )

    for err in errors:
        print(f"replayguard: {err}", file=sys.stderr)

    if args.fail_on == "never":
        return 0
    threshold = Severity(args.fail_on).rank
    return 1 if any(f.severity.rank >= threshold for f in all_findings) else 0


def _cmd_rules(args: argparse.Namespace) -> int:
    import inspect

    for fn in rules._RULES:
        doc = inspect.getdoc(fn) or ""
        head, _, body = doc.partition("\n\n")
        print(head)
        if args.explain and body:
            print("\n".join(f"    {line}" for line in body.strip().splitlines()))
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replayguard",
        description="Determinism checker for AWS Lambda durable functions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="analyse files or directories")
    check.add_argument("paths", nargs="+")
    check.add_argument("--format", choices=sorted(report.FORMATS), default="text")
    check.add_argument("-o", "--output", help="write to a file instead of stdout")
    check.add_argument("--explain", action="store_true", help="include why and fix")
    check.add_argument(
        "--fail-on",
        choices=["error", "warning", "note", "never"],
        default="error",
        help="exit non-zero at or above this severity (default: error)",
    )
    check.add_argument(
        "--min-confidence",
        choices=["high", "medium", "low"],
        default="low",
        help="suppress findings below this confidence (default: low)",
    )
    check.add_argument(
        "--show-coverage-gaps",
        action="store_true",
        help="include RG900 notes for code whose region could not be resolved",
    )
    check.add_argument("--root", default=os.getcwd(), help="base for relative paths")
    check.set_defaults(func=_cmd_check)

    rules_cmd = sub.add_parser("rules", help="list the rules")
    rules_cmd.add_argument("--explain", action="store_true")
    rules_cmd.set_defaults(func=_cmd_rules)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
