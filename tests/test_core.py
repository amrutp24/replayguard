"""Core model and cross-frontend invariants.

Two jobs: pin the small pieces the rules depend on (severity ordering, catalog
lookups, the display/dotted split), and assert the properties that must hold
identically in every frontend. The second kind has already found a real bug --
Python and TypeScript disagreed about a clock call inside a step name, and only
a cross-frontend comparison surfaced it.
"""

from pathlib import Path

import pytest

from replayguard import catalog, rules
from replayguard.findings import Confidence, Finding, Severity
from replayguard.frontends import python_frontend
from replayguard.ir import Call, Handler, Language, Location, Region

pytest.importorskip("tree_sitter", reason="parsers not installed")

from replayguard.frontends import java_frontend, typescript_frontend  # noqa: E402

# -- model -------------------------------------------------------------------


def test_location_renders_as_clickable_path():
    assert str(Location(file="a.py", line=4, col=2)) == "a.py:4:2"


def test_call_shown_prefers_display_over_catalog_key():
    """`Date` is the catalog key; `new Date()` is what the developer wrote."""
    plain = Call(dotted="Date.now", loc=Location("a.ts", 1, 0), region=Region.DURABLE)
    aliased = Call(
        dotted="Date",
        loc=Location("a.ts", 1, 0),
        region=Region.DURABLE,
        display="new Date()",
    )
    assert plain.shown == "Date.now"
    assert aliased.shown == "new Date()"


@pytest.mark.parametrize(
    "high,low",
    [
        (Severity.ERROR, Severity.WARNING),
        (Severity.WARNING, Severity.NOTE),
    ],
)
def test_severity_ordering(high, low):
    assert high.rank > low.rank


def test_confidence_ordering():
    assert Confidence.HIGH.rank > Confidence.MEDIUM.rank > Confidence.LOW.rank


def test_findings_sort_severity_then_confidence():
    """Severity and confidence are orthogonal axes and both drive ordering."""
    loc = Location("a.py", 1, 0)
    note = Finding("RG900", "m", loc, Severity.NOTE, Confidence.HIGH)
    err_low = Finding("RG002", "m", loc, Severity.ERROR, Confidence.MEDIUM)
    err_high = Finding("RG001", "m", loc, Severity.ERROR, Confidence.HIGH)

    ordered = sorted([note, err_low, err_high], key=lambda f: f.sort_key())
    assert [f.rule for f in ordered] == ["RG001", "RG002", "RG900"]


# -- catalog -----------------------------------------------------------------


def test_catalog_lookup_miss_returns_none():
    assert catalog.lookup(Language.PYTHON, "totally.unknown.symbol") is None


def test_categories_for_returns_a_copy():
    """Callers must not be able to mutate the shared catalog."""
    snapshot = catalog.categories_for(Language.PYTHON)
    snapshot["injected.symbol"] = catalog.Category.CLOCK
    assert catalog.lookup(Language.PYTHON, "injected.symbol") is None


@pytest.mark.parametrize(
    "language,dotted,expected",
    [
        (Language.PYTHON, "boto3.client", True),
        (Language.PYTHON, "json.loads", False),
        (Language.JAVA, "software.amazon.awssdk.services.s3.S3Client", True),
        (Language.JAVA, "java.util.List", False),
        (Language.TYPESCRIPT, "client.send", True),
        (Language.TYPESCRIPT, "JSON.parse", False),
    ],
)
def test_aws_sdk_detection_per_language(language, dotted, expected):
    assert catalog.is_aws_sdk_call(language, dotted) is expected


def test_every_language_has_a_catalog():
    for language in Language:
        assert catalog.categories_for(language), language


def test_every_category_has_a_reason():
    """The reason is user-facing -- it becomes the finding's rationale."""
    for category in catalog.Category:
        assert category.why
        category.why.encode("cp1252")  # must survive a Windows console


# -- rules -------------------------------------------------------------------


def test_unknown_region_never_produces_findings():
    """Rules must not report on code whose region could not be resolved.

    Reporting on unresolved regions is how a linter earns a false-positive
    reputation. RG900 flags the gap instead.
    """
    handler = Handler(
        name="h", loc=Location("a.py", 1, 0), language=Language.PYTHON
    )
    handler.calls.append(
        Call(
            dotted="time.time",
            loc=Location("a.py", 2, 0),
            region=Region.UNKNOWN,
        )
    )
    assert rules.check(handler) == []


def test_clean_handler_produces_nothing():
    handler = Handler(name="h", loc=Location("a.py", 1, 0), language=Language.PYTHON)
    assert rules.check(handler) == []


def test_check_returns_sorted_findings():
    module = python_frontend.parse_file(
        str(Path(__file__).parent / "fixtures" / "python" / "bad_handler.py")
    )
    found = rules.check(module.handlers[0])
    assert found == sorted(found, key=lambda f: f.sort_key())


# -- cross-frontend invariants ----------------------------------------------

_EMPTY = {
    Language.PYTHON: ("empty.py", ""),
    Language.TYPESCRIPT: ("empty.ts", ""),
    Language.JAVA: ("Empty.java", ""),
}

_NO_HANDLER = {
    Language.PYTHON: ("plain.py", "import time\n\ndef f():\n    return time.time()\n"),
    Language.TYPESCRIPT: ("plain.ts", "export const f = () => Date.now();\n"),
    Language.JAVA: (
        "Plain.java",
        "public class Plain { public long f() { return System.currentTimeMillis(); } }\n",
    ),
}

_PARSERS = {
    Language.PYTHON: python_frontend.parse_source,
    Language.TYPESCRIPT: typescript_frontend.parse_source,
    Language.JAVA: java_frontend.parse_source,
}


@pytest.mark.parametrize("language", list(Language))
def test_empty_file_yields_no_handlers(language):
    name, source = _EMPTY[language]
    assert _PARSERS[language](source, name).handlers == []


@pytest.mark.parametrize("language", list(Language))
def test_file_without_durable_handler_is_ignored(language):
    """Nondeterministic code in an ordinary function is not this tool's problem.

    Only durable handlers carry the replay obligation, so a plain function
    calling the clock must produce nothing at all.
    """
    name, source = _NO_HANDLER[language]
    module = _PARSERS[language](source, name)
    assert module.handlers == []


@pytest.mark.parametrize("language", list(Language))
def test_parsed_module_reports_its_language(language):
    name, source = _EMPTY[language]
    assert _PARSERS[language](source, name).language is language


def test_python_async_handler_is_detected():
    source = (
        "from aws_durable_execution_sdk_python import durable_execution\n"
        "import time\n\n"
        "@durable_execution\n"
        "async def handler(event, context):\n"
        "    return time.time()\n"
    )
    module = python_frontend.parse_source(source, "async_handler.py")
    assert len(module.handlers) == 1
    found = rules.check(module.handlers[0])
    assert any(f.rule == "RG001" for f in found), "async handlers must be checked too"


def test_multiple_handlers_in_one_file_are_all_analysed():
    source = (
        "from aws_durable_execution_sdk_python import durable_execution\n"
        "import time, random\n\n"
        "@durable_execution\n"
        "def first(event, context):\n"
        "    return time.time()\n\n"
        "@durable_execution\n"
        "def second(event, context):\n"
        "    return random.random()\n"
    )
    module = python_frontend.parse_source(source, "two.py")
    assert {h.name for h in module.handlers} == {"first", "second"}
    for handler in module.handlers:
        assert rules.check(handler), f"{handler.name} should have a finding"
