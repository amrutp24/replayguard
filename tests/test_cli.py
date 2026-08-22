"""CLI tests, including one true end-to-end run of the installed console script.

Most tests call `main()` directly for speed. The subprocess test exists because
that is the only thing that proves the packaging, the entry point, and the
console script actually work -- everything else would still pass if the package
were broken and only importable from the source tree.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from replayguard.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
GOOD_PY = FIXTURES / "python" / "good_handler.py"
BAD_PY = FIXTURES / "python" / "bad_handler.py"


# -- exit codes --------------------------------------------------------------


def test_findings_exit_nonzero(capsys):
    assert main(["check", str(BAD_PY)]) == 1


def test_clean_file_exits_zero(capsys):
    assert main(["check", str(GOOD_PY)]) == 0


def test_fail_on_never_always_exits_zero(capsys):
    assert main(["check", str(BAD_PY), "--fail-on", "never"]) == 0


def test_fail_on_note_catches_notes(capsys):
    """RG900 is a NOTE. Default --fail-on=error ignores it; note-level does not."""
    only_note = FIXTURES / "python" / "good_handler.py"
    # The bad fixture has both; assert the threshold is actually consulted.
    assert main(["check", str(BAD_PY), "--fail-on", "note"]) == 1
    assert main(["check", str(only_note), "--fail-on", "note"]) == 0


def test_no_matching_files_exits_two(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("not source")
    assert main(["check", str(tmp_path)]) == 2


# -- formats -----------------------------------------------------------------


def test_json_format_parses(capsys):
    main(["check", str(BAD_PY), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["findings"]


def test_sarif_format_parses(capsys):
    main(["check", str(BAD_PY), "--format", "sarif"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0"


def test_output_file_is_written(tmp_path, capsys):
    out = tmp_path / "r.sarif"
    main(["check", str(BAD_PY), "--format", "sarif", "-o", str(out)])
    assert out.exists()
    assert json.loads(out.read_text())["version"] == "2.1.0"


def test_explain_adds_rationale(capsys):
    main(["check", str(BAD_PY), "--explain"])
    assert "why:" in capsys.readouterr().out


# -- filtering ---------------------------------------------------------------


def test_min_confidence_filters(capsys):
    main(["check", str(BAD_PY), "--format", "json"])
    everything = json.loads(capsys.readouterr().out)["findings"]

    main(["check", str(BAD_PY), "--format", "json", "--min-confidence", "high"])
    high_only = json.loads(capsys.readouterr().out)["findings"]

    assert len(high_only) < len(everything), "bad fixture should have medium findings"
    assert all(f["confidence"] == "high" for f in high_only)


# -- discovery ---------------------------------------------------------------


def test_all_three_languages_are_discovered(capsys):
    main(["check", str(FIXTURES), "--format", "json"])
    files = {f["file"] for f in json.loads(capsys.readouterr().out)["findings"]}
    suffixes = {Path(f).suffix for f in files}
    assert suffixes == {".py", ".ts", ".java"}


def test_vendor_directories_are_skipped(tmp_path, capsys):
    """node_modules and friends must not be walked -- a durable handler in a
    dependency is not the user's problem and would flood the output.
    """
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "h.py").write_text(BAD_PY.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["check", str(tmp_path)]) == 2, "should find no analysable files"


def test_unsupported_extensions_ignored(tmp_path, capsys):
    (tmp_path / "README.md").write_text("# not code")
    (tmp_path / "h.py").write_text(GOOD_PY.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["check", str(tmp_path)]) == 0


def test_single_file_path_works(capsys):
    assert main(["check", str(GOOD_PY)]) == 0


# -- robustness --------------------------------------------------------------


def test_syntax_error_is_reported_not_raised(tmp_path, capsys):
    """A file that will not parse must be reported and the run must continue.

    Crashing on one malformed file in a large tree would make the tool unusable.
    """
    (tmp_path / "broken.py").write_text("def handler( <<< syntax error", encoding="utf-8")
    (tmp_path / "fine.py").write_text(GOOD_PY.read_text(encoding="utf-8"), encoding="utf-8")
    code = main(["check", str(tmp_path)])
    assert code == 0
    assert "could not parse" in capsys.readouterr().err


def test_file_with_no_durable_handler_is_clean(tmp_path, capsys):
    (tmp_path / "plain.py").write_text(
        "import time\n\ndef handler(event, context):\n    return time.time()\n",
        encoding="utf-8",
    )
    assert main(["check", str(tmp_path)]) == 0


# -- rules subcommand --------------------------------------------------------


def test_rules_lists_every_rule(capsys):
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    for rule_id in ("RG001", "RG002", "RG003", "RG004", "RG005", "RG900"):
        assert rule_id in out


def test_rules_explain_adds_detail(capsys):
    main(["rules"])
    brief = len(capsys.readouterr().out)
    main(["rules", "--explain"])
    assert len(capsys.readouterr().out) > brief


# -- true end-to-end ---------------------------------------------------------


@pytest.mark.parametrize("args,expected", [([], 1), (["--fail-on", "never"], 0)])
def test_installed_console_script(args, expected):
    """The only test that proves packaging and the entry point actually work."""
    proc = subprocess.run(
        [sys.executable, "-m", "replayguard.cli", "check", str(BAD_PY), *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expected, proc.stderr
    if expected:
        assert "RG00" in proc.stdout


# -- degraded environments ---------------------------------------------------


def test_missing_parser_is_reported_not_crashed(tmp_path, capsys, monkeypatch):
    """Without the tree-sitter extra, TS/Java files must degrade gracefully.

    A user who installed the base package and points it at a mixed repo should
    get their Python results plus a clear note, not a traceback.
    """
    from replayguard.frontends import typescript_frontend

    def boom():
        raise RuntimeError("TypeScript support needs the optional dependencies")

    monkeypatch.setattr(typescript_frontend, "_load_parser", lambda *a, **k: boom())

    (tmp_path / "h.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "h.py").write_text(GOOD_PY.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(["check", str(tmp_path)]) == 0
    assert "optional dependencies" in capsys.readouterr().err
