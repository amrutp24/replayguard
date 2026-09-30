"""The offline renderer and the command line.

Both were at zero coverage while the rules engine behind them sat near 100,
which is the shape of gap worth distrusting: the part that decides what is true
was tested and the part that decides what the user is told was not.

The CLI tests use a fake PyPI so they stay offline and deterministic. Network
access in a unit suite turns an outage into a test failure and makes the suite
useless in exactly the situation where you want to trust it.
"""

from __future__ import annotations

import json

import pytest

from replayguard import cli, drift_cli  # noqa: F401
from replayguard.drift.report import counts, to_json, to_text
from replayguard.drift.rules import Detection, Finding, Severity


def finding(**overrides) -> Finding:
    base = {
        "rule": "RD003",
        "title": "Exception reparented",
        "severity": Severity.HIGH,
        "detection": Detection.SILENT,
        "subject": "exceptions.py::CallbackError",
        "detail": "CallbackError lost ExecutionError as a base.",
        "in_flight": True,
    }
    base.update(overrides)
    return Finding(**base)


# --------------------------------------------------------------------------
# report.to_text
# --------------------------------------------------------------------------


def test_in_flight_findings_get_their_own_section_first():
    out = to_text(
        [
            finding(),
            finding(
                rule="RD001",
                detection=Detection.BUILD,
                in_flight=False,
                subject="helpers.py::gone",
                severity=Severity.MEDIUM,
            ),
        ],
        package="pkg",
        old="1.0.0",
        new="2.0.0",
    )
    assert out.index("AFFECTS EXECUTIONS ALREADY IN FLIGHT") < out.index("BUILD-TIME ONLY")
    assert "CallbackError" in out


def test_an_empty_result_says_what_it_does_not_prove():
    """A clean report must not read as a guarantee."""
    out = to_text([], package="pkg", old="1.0.0", new="1.0.1")
    assert "No drift risk found" in out
    assert "not a guarantee" in out


def test_silent_findings_are_labelled_silent():
    out = to_text([finding()], package="pkg", old="1", new="2")
    assert "SILENT" in out


def test_text_output_is_ascii_only():
    """The console here is cp1252; a stray glyph turns a report into a crash."""
    out = to_text([finding()], package="pkg", old="1.0.0", new="2.0.0")
    out.encode("ascii")


def test_long_detail_is_wrapped_not_truncated():
    detail = " ".join(["word"] * 60)
    out = to_text([finding(detail=detail)], package="p", old="1", new="2")
    assert "word word" in out
    assert out.count("\n") > 5
    # Every word survives the wrap.
    assert out.count("word") == 60


# --------------------------------------------------------------------------
# report.counts / to_json
# --------------------------------------------------------------------------


def test_counts_separates_in_flight_from_silent():
    c = counts(
        [
            finding(),  # in-flight + silent
            finding(detection=Detection.BUILD),  # in-flight, build
            finding(in_flight=False, detection=Detection.BUILD),
        ]
    )
    assert c == {
        "total": 3,
        "in_flight": 2,
        "silent": 1,
        "in_flight_and_silent": 1,
        "high": 3,
    }


def test_json_round_trips_and_keeps_severity_as_a_string():
    payload = json.loads(
        to_json([finding()], package="pkg", old="1.0.0", new="2.0.0", meta={"x": 1})
    )
    assert payload["package"] == "pkg"
    assert payload["from_version"] == "1.0.0"
    assert payload["meta"]["x"] == 1
    assert payload["findings"][0]["severity"] == "high"
    assert payload["findings"][0]["in_flight"] is True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.fixture
def fake_sdk(tmp_path, monkeypatch):
    """Two versions of a tiny package on disk, standing in for PyPI."""

    def make(version: str, callback_base: str) -> None:
        root = tmp_path / version / "pkg"
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf8")
        (root / "exceptions.py").write_text(
            "class ExecutionError(Exception):\n    pass\n"
            "class DurableOperationError(Exception):\n    pass\n"
            f"class CallbackError({callback_base}):\n    pass\n",
            encoding="utf8",
        )

    make("1.0.0", "ExecutionError")
    make("2.0.0", "DurableOperationError")

    def fake_fetch(package, version, *, cache_dir=None, timeout=60.0):
        return tmp_path / version / "pkg"

    def fake_releases(package, *, timeout=30.0):
        return {"1.0.0": "2026-01-01", "2.0.0": "2026-02-01"}

    monkeypatch.setattr("replayguard.drift.fetch.fetch", fake_fetch)
    monkeypatch.setattr("replayguard.drift.fetch.releases", fake_releases)
    return tmp_path


def test_diff_reports_the_reparenting_and_exits_zero(fake_sdk, capsys):
    code = cli.main(["drift", "1.0.0", "2.0.0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "CallbackError" in out
    assert "RD003" in out


def test_diff_fail_on_inflight_exits_one(fake_sdk, capsys):
    """The CI gate. If this returns 0 the flag is decorative."""
    assert cli.main(["drift", "1.0.0", "2.0.0", "--fail-on-inflight"]) == 1


def test_diff_fail_on_inflight_exits_zero_when_nothing_is_in_flight(fake_sdk, capsys):
    assert cli.main(["drift", "1.0.0", "1.0.0", "--fail-on-inflight"]) == 0


def test_diff_writes_json_when_asked(fake_sdk, tmp_path, capsys):
    out_path = tmp_path / "findings.json"
    cli.main(["drift", "1.0.0", "2.0.0", "--json", str(out_path)])
    payload = json.loads(out_path.read_text(encoding="utf8"))
    assert payload["from_version"] == "1.0.0"
    assert any(f["rule"] == "RD003" for f in payload["findings"])


def test_releases_lists_versions_by_date(fake_sdk, capsys):
    assert cli.main(["drift", "--releases"]) == 0
    out = capsys.readouterr().out
    assert "2026-01-01" in out and "1.0.0" in out


def test_sweep_reports_how_many_upgrades_carry_risk(fake_sdk, capsys):
    assert cli.main(["drift", "--sweep"]) == 0
    out = capsys.readouterr().out
    assert "1 of 1 upgrades carry" in out


def test_scenarios_command_lists_every_scenario(capsys):
    from replayguard.probe.scenarios import SCENARIOS

    assert cli.main(["probe", "--list"]) == 0
    out = capsys.readouterr().out
    for s in SCENARIOS:
        assert s.id in out


def test_report_command_renders_from_json(tmp_path, capsys):
    src = tmp_path / "live.json"
    src.write_text(
        json.dumps(
            [
                {
                    "scenario": "d02-step-renamed",
                    "question": "What happens?",
                    "advice": "AWS says do not.",
                    "mutation": "renamed a step",
                    "status": "FAILED",
                    "verdict": "clean-failure",
                    "side_effects": {"first": 1},
                    "events": ["ExecutionFailed"],
                    "error": {"ErrorMessage": "boom"},
                    "notes": [],
                }
            ]
        ),
        encoding="utf8",
    )
    out_path = tmp_path / "FINDINGS.md"
    assert cli.main(["probe", "--report", str(src), "--out", str(out_path)]) == 0
    text = out_path.read_text(encoding="utf8")
    assert "d02-step-renamed" in text
    assert "boom" in text


def test_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


# --------------------------------------------------------------------------
# The merged subcommands' own entry points.
#
# These paths decide what a user is told before any AWS call happens, and an
# untested entry point in front of a well-tested engine is the gap that hid the
# worst bug in this project's history. Everything here runs offline.
# --------------------------------------------------------------------------


def test_drift_without_versions_explains_itself(capsys):
    """Two positional args are optional because --sweep exists; say so."""
    assert cli.main(["drift"]) == 2
    err = capsys.readouterr().err
    assert "two versions" in err
    assert "--sweep" in err


def test_drift_releases_lists_by_date(fake_sdk, capsys):
    assert cli.main(["drift", "--releases"]) == 0
    out = capsys.readouterr().out
    assert "2026-01-01" in out and "1.0.0" in out


def test_drift_sweep_counts_risky_upgrades(fake_sdk, capsys):
    assert cli.main(["drift", "--sweep"]) == 0
    assert "1 of 1 upgrades carry" in capsys.readouterr().out


def test_drift_sweep_writes_json(fake_sdk, tmp_path):
    out = tmp_path / "sweep.json"
    cli.main(["drift", "--sweep", "--json", str(out)])
    payload = json.loads(out.read_text(encoding="utf8"))
    assert payload["steps"][0]["from"] == "1.0.0"


def test_drift_reports_a_bad_version_without_traceback(monkeypatch, capsys):
    from replayguard.drift.fetch import FetchError

    def boom(*a, **k):
        raise FetchError("no release 9.9.9")

    monkeypatch.setattr("replayguard.drift.fetch.fetch", boom)
    assert cli.main(["drift", "1.0.0", "9.9.9"]) == 2
    assert "no release 9.9.9" in capsys.readouterr().err


def test_probe_list_names_every_scenario(capsys):
    from replayguard.probe.scenarios import SCENARIOS

    assert cli.main(["probe", "--list"]) == 0
    out = capsys.readouterr().out
    for s in SCENARIOS:
        assert s.id in out


def test_probe_report_renders_without_touching_aws(tmp_path, capsys):
    """--report must work from a JSON file alone, with no credentials."""
    src = tmp_path / "live.json"
    src.write_text(
        json.dumps(
            [
                {
                    "scenario": "d05-step-reordered",
                    "question": "What happens?",
                    "advice": "Do not reorder.",
                    "mutation": "swapped step_one and step_two",
                    "status": "SUCCEEDED",
                    "verdict": "silent-reexecution",
                    "side_effects": {"first": 2},
                    "events": ["ExecutionSucceeded"],
                    "error": {},
                    "notes": [],
                }
            ]
        ),
        encoding="utf8",
    )
    out_path = tmp_path / "F.md"
    assert cli.main(["probe", "--report", str(src), "--out", str(out_path)]) == 0
    text = out_path.read_text(encoding="utf8")
    assert "silent-corruption" in text, "both failure modes must survive rendering"


def test_probe_report_to_stdout(tmp_path, capsys):
    src = tmp_path / "live.json"
    src.write_text(json.dumps([]), encoding="utf8")
    assert cli.main(["probe", "--report", str(src)]) == 0
    assert "Of 0 scenarios" in capsys.readouterr().out


def test_unknown_scenario_id_is_named(monkeypatch, capsys):
    """Fail on a typo before provisioning anything."""
    import replayguard.probe.infra as infra

    monkeypatch.setattr(infra, "provision", lambda *a, **k: None)
    assert cli.main(["probe", "nope-not-a-scenario"]) == 2
    assert "nope-not-a-scenario" in capsys.readouterr().err


def test_probe_destroy_delegates_and_maps_exit_code(monkeypatch):
    """--destroy must report a failed teardown as a non-zero exit.

    A teardown that fails silently is the one bug this project cannot ship:
    it leaves billable resources in someone's account.
    """
    import replayguard.probe.infra as infra

    monkeypatch.setattr(infra, "destroy", lambda region, profile: True)
    assert cli.main(["probe", "--destroy"]) == 0

    monkeypatch.setattr(infra, "destroy", lambda region, profile: False)
    assert cli.main(["probe", "--destroy"]) == 1


def test_probe_passes_its_arguments_through_to_the_runner(monkeypatch):
    """The CLI's remaining job is delegation; check it delegates faithfully."""
    import replayguard.probe.runner as runner

    seen = {}

    def fake_run_matrix(scenarios, **kwargs):
        seen["ids"] = [s.id for s in scenarios]
        seen.update(kwargs)
        return []

    monkeypatch.setattr(runner, "run_matrix", fake_run_matrix)
    code = cli.main(
        [
            "probe",
            "d01-control",
            "--region", "eu-west-1",
            "--wait", "30",
            "--keep",
        ]
    )
    assert code == 0
    assert seen["ids"] == ["d01-control"]
    assert seen["region"] == "eu-west-1"
    assert seen["wait_seconds"] == 30
    assert seen["keep"] is True


# --------------------------------------------------------------------------
# Shipped bug in 0.2.0: `probe --list` and `probe --report` crashed on a plain
# `pip install replayguard`, because scenarios.py imported the deploy module at
# top level and that imports botocore. Every test here passed, because the dev
# environment has boto3. The only way to test "works without the extra" is to
# take the extra away.
# --------------------------------------------------------------------------


@pytest.fixture
def no_boto(monkeypatch):
    """Make `import boto3` / `import botocore` raise, as on a bare install.

    A None entry in sys.modules makes the import machinery raise ImportError.
    The probe modules are evicted too so the test sees a fresh import chain
    rather than one cached from an earlier test that had boto available.
    """
    import sys

    for name in list(sys.modules):
        if name.startswith("replayguard.probe"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    for name in ("boto3", "botocore", "botocore.exceptions"):
        monkeypatch.setitem(sys.modules, name, None)


def test_probe_list_works_without_the_live_extra(no_boto, capsys):
    assert cli.main(["probe", "--list"]) == 0
    assert "d01-control" in capsys.readouterr().out


def test_probe_report_works_without_the_live_extra(no_boto, tmp_path, capsys):
    src = tmp_path / "live.json"
    src.write_text(json.dumps([]), encoding="utf8")
    assert cli.main(["probe", "--report", str(src)]) == 0
    assert "Of 0 scenarios" in capsys.readouterr().out


def test_the_live_path_still_reports_the_missing_extra_cleanly(no_boto, capsys):
    """Without boto, running the probe must say so and exit 2, not traceback."""
    assert cli.main(["probe", "--destroy"]) == 2
    assert "boto3" in capsys.readouterr().err
