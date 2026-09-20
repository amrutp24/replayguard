"""Subcommands for the second half of the replay contract.

`replayguard check` and `replayguard replay` ask whether your handler is
deterministic. These two ask whether the code that resumes an execution still
matches the code that suspended it:

    replayguard drift 1.7.0 2.0.0     # will this SDK upgrade reach in-flight work?
    replayguard drift --sweep         # how often does an upgrade carry that risk?
    replayguard probe                 # what does the platform actually do? (live)

`drift` is offline and stdlib-only. `probe` needs boto3 and an AWS account and
creates billable resources, so it lives behind the `live` extra and says so
before it does anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_PACKAGE = "aws-durable-execution-sdk-python"


def add_parsers(sub) -> None:
    """Attach `drift` and `probe` to replayguard's subparsers."""
    d = sub.add_parser(
        "drift",
        help="compare two SDK versions for changes that can reach a suspended execution",
    )
    d.add_argument("old", nargs="?", help="version your in-flight executions run on")
    d.add_argument("new", nargs="?", help="version you are about to deploy")
    d.add_argument("--package", default=DEFAULT_PACKAGE)
    d.add_argument("--json", dest="json_out", metavar="PATH")
    d.add_argument(
        "--fail-on-inflight",
        action="store_true",
        help="exit 1 if anything can affect an execution already in flight (for CI)",
    )
    d.add_argument(
        "--sweep",
        action="store_true",
        help="diff every adjacent release pair instead of one upgrade",
    )
    d.add_argument("--releases", action="store_true", help="list published versions")
    d.add_argument("--cache-dir", default=None)
    d.set_defaults(func=_cmd_drift)

    p = sub.add_parser(
        "probe",
        help="live: suspend a real execution, change it underneath, record what happened",
    )
    p.add_argument("scenarios", nargs="*", help="scenario ids; default is all")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile", default=None)
    p.add_argument("--wait", type=int, default=90, help="suspend length, seconds")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--json", dest="json_out", metavar="PATH")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--list", action="store_true", help="list scenarios and exit")
    p.add_argument(
        "--destroy",
        action="store_true",
        help="delete every rd- resource, verify, and exit",
    )
    p.add_argument(
        "--report",
        metavar="JSON",
        help="render a findings document from a previous run's JSON and exit",
    )
    p.add_argument("--out", default=None, help="where --report writes its markdown")
    p.add_argument(
        "--keep",
        action="store_true",
        help="skip teardown (leaves billable resources in the account)",
    )
    p.set_defaults(func=_cmd_probe)


def _cmd_drift(args: argparse.Namespace) -> int:
    from .drift.fetch import FetchError, fetch, releases
    from .drift.report import counts, to_json, to_text
    from .drift.rules import compare
    from .drift.surface import extract

    if args.releases:
        try:
            rel = releases(args.package)
        except FetchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{args.package}: {len(rel)} releases")
        for version, date in sorted(rel.items(), key=lambda kv: kv[1]):
            print(f"  {date}  {version}")
        return 0

    if args.sweep:
        return _sweep(args, fetch, releases, extract, compare, counts, FetchError)

    if not args.old or not args.new:
        print(
            "error: drift needs two versions, or --sweep, or --releases",
            file=sys.stderr,
        )
        return 2

    try:
        old_dir = fetch(args.package, args.old, cache_dir=args.cache_dir)
        new_dir = fetch(args.package, args.new, cache_dir=args.cache_dir)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    old, new = extract(old_dir, args.old), extract(new_dir, args.new)
    findings = compare(old, new)
    print(to_text(findings, package=args.package, old=args.old, new=args.new))

    if args.json_out:
        Path(args.json_out).write_text(
            to_json(findings, package=args.package, old=args.old, new=args.new),
            encoding="utf8",
        )
        print(f"\nwrote {args.json_out}")

    if args.fail_on_inflight and counts(findings)["in_flight"]:
        return 1
    return 0


def _sweep(args, fetch, releases, extract, compare, counts, FetchError) -> int:
    try:
        rel = releases(args.package)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    order = [v for v, _ in sorted(rel.items(), key=lambda kv: kv[1])]
    print(f"{args.package}: {len(order)} releases, {len(order) - 1} upgrade steps\n")
    print(f"{'from':>12} -> {'to':<14} {'date':<12} {'total':>5} {'in-flight':>10}")
    print("-" * 62)

    rows, skipped = [], []
    for old_v, new_v in zip(order, order[1:], strict=False):
        try:
            old = extract(fetch(args.package, old_v, cache_dir=args.cache_dir), old_v)
            new = extract(fetch(args.package, new_v, cache_dir=args.cache_dir), new_v)
        except (FetchError, NotADirectoryError) as exc:
            skipped.append((old_v, new_v, str(exc)))
            continue
        c = counts(compare(old, new))
        rows.append({"from": old_v, "to": new_v, "to_date": rel[new_v], **c})
        print(
            f"{old_v:>12} -> {new_v:<14} {rel[new_v]:<12} "
            f"{c['total']:>5} {c['in_flight']:>10}"
        )

    if rows:
        risky = sum(1 for r in rows if r["in_flight"])
        print(
            f"\n{risky} of {len(rows)} upgrades carry at least one change that can "
            f"reach an execution already in flight."
        )
    for old_v, new_v, why in skipped:
        print(f"  (skipped {old_v} -> {new_v}: {why})")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"package": args.package, "steps": rows}, indent=2),
            encoding="utf8",
        )
        print(f"\nwrote {args.json_out}")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    if args.list:
        from .probe.scenarios import SCENARIOS

        print(f"{len(SCENARIOS)} scenarios\n")
        for s in SCENARIOS:
            print(f"  {s.id}\n      Q: {s.question}\n      does: {s.mutation}\n")
        return 0

    if args.report:
        import datetime

        from .probe.findings import load, render

        markdown = render(
            load(args.report),
            region=args.region,
            account_note="One account, one execution per scenario.",
            run_date=datetime.date.today().isoformat(),
        )
        if args.out:
            Path(args.out).write_text(markdown, encoding="utf8")
            print(f"wrote {args.out}")
        else:
            print(markdown)
        return 0

    try:
        from .probe.infra import destroy
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.destroy:
        return 0 if destroy(args.region, args.profile) else 1

    from .probe.runner import run_matrix
    from .probe.scenarios import select

    try:
        chosen = select(args.scenarios)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run_matrix(
        chosen,
        region=args.region,
        profile=args.profile,
        wait_seconds=args.wait,
        timeout_s=args.timeout,
        cache_dir=args.cache_dir,
        json_out=args.json_out,
        keep=args.keep,
    )
    return 0
