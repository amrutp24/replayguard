"""One command that proves the tool works locally.

    python scripts/verify.py

Runs every gate that must pass before anything is published. `make` is not
available on the maintainer's machine, so this is a plain Python script rather
than a Makefile -- it works anywhere Python does.

Stages, in the order a failure is most likely to be informative:

  1. import        -- the package is importable at all
  2. lint          -- ruff, source and tests
  3. tests         -- pytest with a coverage floor
  4. cli           -- the installed console script, exit codes, every format
  5. canary        -- the good fixtures must produce exactly zero findings

Stage 5 is the one that matters most. A linter that fires on correct code gets
uninstalled, so a false positive is a worse failure than a missed bug, and it
gets its own gate rather than hiding among the unit tests.

Exit code is 0 only if every stage passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

GOOD_FIXTURES = [
    FIXTURES / "python" / "good_handler.py",
    FIXTURES / "typescript" / "good_handler.ts",
    FIXTURES / "java" / "GoodHandler.java",
    FIXTURES / "rust" / "good_handler.rs",
]

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Stage:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def report(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {name}")
        if detail:
            for line in detail.strip().splitlines()[:15]:
                print(f"{DIM}         {line}{RESET}")
        if not ok:
            self.failures.append(name)
        return ok


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, **kw
    )


def stage_import(s: Stage) -> None:
    print("\n1. import")
    proc = run([sys.executable, "-c", "import replayguard, replayguard.cli"])
    s.report(
        "package imports",
        proc.returncode == 0,
        proc.stderr if proc.returncode else "",
    )


def stage_lint(s: Stage) -> None:
    print("\n2. lint")
    proc = run([sys.executable, "-m", "ruff", "check", "src", "tests", "experiments"])
    if proc.returncode == 127 or "No module named" in proc.stderr:
        s.report("ruff installed", False, "pip install ruff")
        return
    s.report("ruff", proc.returncode == 0, proc.stdout or proc.stderr)


def stage_tests(s: Stage) -> None:
    print("\n3. tests")
    proc = run(
        [sys.executable, "-m", "pytest", "-q", "--cov=replayguard", "--cov-report=term"]
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-4:])
    s.report("pytest + coverage floor", proc.returncode == 0, tail)


def stage_cli(s: Stage) -> None:
    """Exercise the CLI the way a user would, not via imports."""
    print("\n4. cli")
    bad = FIXTURES / "python" / "bad_handler.py"
    good = FIXTURES / "python" / "good_handler.py"
    cli = [sys.executable, "-m", "replayguard.cli"]

    checks = [
        ("findings exit 1", cli + ["check", str(bad)], 1),
        ("clean exits 0", cli + ["check", str(good)], 0),
        ("--fail-on never exits 0", cli + ["check", str(bad), "--fail-on", "never"], 0),
        ("missing path exits 2", cli + ["check", str(ROOT / "no-such-dir")], 2),
    ]
    for name, cmd, expected in checks:
        proc = run(cmd)
        s.report(
            name,
            proc.returncode == expected,
            f"expected {expected}, got {proc.returncode}\n{proc.stderr}",
        )

    for fmt, validate in (
        ("json", lambda d: "findings" in d),
        ("sarif", lambda d: d.get("version") == "2.1.0"),
    ):
        proc = run(cli + ["check", str(FIXTURES), "--format", fmt])
        try:
            ok = validate(json.loads(proc.stdout))
            detail = ""
        except (json.JSONDecodeError, AttributeError) as exc:
            ok, detail = False, str(exc)
        s.report(f"--format {fmt} parses", ok, detail)

    # Output must survive a cp1252 console; a stray em-dash renders as garbage.
    proc = run(cli + ["check", str(FIXTURES), "--explain"])
    try:
        proc.stdout.encode("cp1252")
        ok, detail = True, ""
    except UnicodeEncodeError as exc:
        ok, detail = False, f"non-ASCII in output: {exc}"
    s.report("output is cp1252-safe", ok, detail)


def stage_canary(s: Stage) -> None:
    """Correct handlers must produce zero findings, in every language.

    This is the gate that decides whether the tool is usable. A false positive
    gets it switched off, after which it catches nothing at all.
    """
    print("\n5. canary (no false positives)")
    for fixture in GOOD_FIXTURES:
        if not fixture.exists():
            s.report(f"{fixture.name} present", False, "fixture missing")
            continue
        proc = run(
            [
                sys.executable,
                "-m",
                "replayguard.cli",
                "check",
                str(fixture),
                "--format",
                "json",
                "--fail-on",
                "never",
            ]
        )
        try:
            findings = json.loads(proc.stdout)["findings"]
        except (json.JSONDecodeError, KeyError):
            s.report(f"{fixture.name} analysed", False, proc.stderr or proc.stdout)
            continue
        detail = "\n".join(
            f"{f['rule']} line {f['line']}: {f['message']}" for f in findings
        )
        s.report(f"{fixture.name} is clean", not findings, detail)


def main() -> int:
    print(f"replayguard verification  ({ROOT})")
    s = Stage()
    stage_import(s)
    stage_lint(s)
    stage_tests(s)
    stage_cli(s)
    stage_canary(s)

    print("\n" + "=" * 60)
    if s.failures:
        print(f"{RED}FAILED{RESET}: {len(s.failures)} stage(s)")
        for f in s.failures:
            print(f"  - {f}")
        print("=" * 60)
        return 1
    print(f"{GREEN}ALL CHECKS PASSED{RESET} - safe to publish")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
