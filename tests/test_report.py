"""Reporter tests.

These exist because the reporters were the untested half of the tool. The text
grouping bug below shipped once already and was caught by eye rather than by a
test, which is exactly the kind of regression that comes back.
"""

import json

import pytest

from replayguard import report
from replayguard.findings import Confidence, Finding, Severity
from replayguard.ir import Location


def finding(rule="RG001", file="a.py", line=10, sev=Severity.ERROR, col=0):
    return Finding(
        rule=rule,
        message=f"{rule} happened",
        loc=Location(file=file, line=line, col=col),
        severity=sev,
        confidence=Confidence.HIGH,
        rationale=f"{rule} rationale",
        fix=f"{rule} fix",
    )


# -- text --------------------------------------------------------------------


def test_empty_findings_reports_clean():
    assert "no determinism violations" in report.text([])


def test_file_header_appears_once_per_file():
    """Regression: findings sort by severity, so a file's rows are not
    contiguous. Printing in arrival order repeated the filename header once per
    severity band, which made the output look broken.
    """
    findings = [
        finding(rule="RG001", file="a.py", line=1, sev=Severity.ERROR),
        finding(rule="RG002", file="b.py", line=1, sev=Severity.ERROR),
        finding(rule="RG900", file="a.py", line=99, sev=Severity.NOTE),
    ]
    out = report.text(findings)
    assert out.count("a.py") == 1, out
    assert out.count("b.py") == 1, out


def test_rows_within_a_file_are_line_ordered():
    findings = [
        finding(rule="RG900", file="a.py", line=99, sev=Severity.NOTE),
        finding(rule="RG001", file="a.py", line=5, sev=Severity.ERROR),
        finding(rule="RG002", file="a.py", line=50, sev=Severity.ERROR),
    ]
    body = report.text(findings)
    lines = [ln for ln in body.splitlines() if ln.startswith("   ")]
    numbers = [int(ln.split(":")[0].strip()) for ln in lines]
    assert numbers == sorted(numbers), numbers


def test_explain_includes_rationale_and_fix():
    plain = report.text([finding()], verbose=False)
    verbose = report.text([finding()], verbose=True)
    # Assert on the finding's own text, not the word "rationale" -- the
    # non-verbose footer advertises --explain and contains that word itself.
    assert "RG001 rationale" not in plain
    assert "RG001 fix" not in plain
    assert "RG001 rationale" in verbose
    assert "RG001 fix" in verbose


def test_summary_counts_by_severity():
    findings = [
        finding(sev=Severity.ERROR),
        finding(sev=Severity.ERROR),
        finding(rule="RG900", sev=Severity.NOTE),
    ]
    out = report.text(findings)
    assert "3 finding(s)" in out
    assert "2 error" in out
    assert "1 note" in out


def test_text_output_is_ascii_safe():
    """The owner's console is cp1252; a stray em-dash renders as a replacement
    character. Reporter output must survive it.
    """
    out = report.text([finding()], verbose=True)
    out.encode("cp1252")  # raises UnicodeEncodeError if not representable


# -- json --------------------------------------------------------------------


def test_json_is_valid_and_complete():
    doc = json.loads(report.as_json([finding(), finding(rule="RG003")]))
    assert doc["version"] == 1
    assert len(doc["findings"]) == 2
    first = doc["findings"][0]
    for key in (
        "rule",
        "message",
        "severity",
        "confidence",
        "file",
        "line",
        "column",
        "rationale",
        "fix",
    ):
        assert key in first, key


def test_json_severity_and_confidence_are_strings():
    doc = json.loads(report.as_json([finding()]))
    assert doc["findings"][0]["severity"] == "error"
    assert doc["findings"][0]["confidence"] == "high"


# -- sarif -------------------------------------------------------------------


def test_sarif_is_valid_2_1_0():
    doc = json.loads(report.sarif([finding()]))
    assert doc["version"] == "2.1.0"
    assert "$schema" in doc
    assert len(doc["runs"]) == 1
    assert doc["runs"][0]["tool"]["driver"]["name"] == "replayguard"


def test_sarif_rules_are_deduplicated():
    """Three findings, two rules -> two rule descriptors, three results."""
    findings = [finding(rule="RG001"), finding(rule="RG001"), finding(rule="RG003")]
    run = json.loads(report.sarif(findings))["runs"][0]
    assert len(run["tool"]["driver"]["rules"]) == 2
    assert len(run["results"]) == 3


def test_sarif_columns_are_one_based():
    """SARIF regions are 1-based; the IR stores 0-based columns."""
    run = json.loads(report.sarif([finding(col=0)]))["runs"][0]
    region = run["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region["startColumn"] == 1
    assert region["startLine"] >= 1


@pytest.mark.parametrize(
    "severity,level",
    [(Severity.ERROR, "error"), (Severity.WARNING, "warning"), (Severity.NOTE, "note")],
)
def test_sarif_level_mapping(severity, level):
    run = json.loads(report.sarif([finding(sev=severity)]))["runs"][0]
    assert run["results"][0]["level"] == level


def test_sarif_every_result_has_a_location():
    findings = [finding(file="a.py"), finding(file="b.ts"), finding(file="C.java")]
    run = json.loads(report.sarif(findings))["runs"][0]
    for result in run["results"]:
        uri = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        assert uri


def test_paths_are_relative_to_root(tmp_path):
    f = finding(file=str(tmp_path / "src" / "h.py"))
    doc = json.loads(report.as_json([f], root=str(tmp_path)))
    assert doc["findings"][0]["file"] == "src/h.py"


def test_all_formats_registered():
    assert set(report.FORMATS) == {"text", "json", "sarif"}


def test_path_on_another_drive_falls_back_to_absolute():
    """os.path.relpath raises across Windows drives; the reporter must not."""
    f = finding(file="D:/elsewhere/h.py")
    out = report.as_json([f], root="C:/project")
    assert "h.py" in out
