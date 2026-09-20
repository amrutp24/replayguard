"""Fetching SDK versions from PyPI, with the network faked.

This module was almost entirely uncovered after the merge because every other
test monkeypatches `fetch` wholesale. That is fine for testing the callers and
useless for testing this: the error handling here is most of the code, and it
is the part a user meets on a bad day -- a typo'd version, a PyPI outage, a
wheel with nothing importable in it.

Every test below fakes `urllib.request.urlopen`, so the suite stays offline. A
unit suite that needs the network fails during exactly the outage where you want
to trust it.
"""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile

import pytest

from replayguard.drift import fetch as fetch_mod


def _wheel_bytes(package_dir: str = "pkg", version: str = "1.0.0") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{package_dir}/__init__.py", "")
        z.writestr(f"{package_dir}/mod.py", "def f():\n    return 1\n")
        z.writestr(f"{package_dir}-{version}.dist-info/METADATA", "Name: pkg\n")
    return buf.getvalue()


class _Resp(io.BytesIO):
    """Minimal stand-in for what urlopen returns, usable as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _index(versions: dict[str, str], *, wheel_name: str = "pkg-{v}-py3-none-any.whl"):
    return {
        "releases": {
            v: [
                {
                    "packagetype": "bdist_wheel",
                    "filename": wheel_name.format(v=v),
                    "url": f"https://example.invalid/{v}.whl",
                    "upload_time": f"{date}T00:00:00",
                }
            ]
            for v, date in versions.items()
        }
    }


@pytest.fixture
def fake_pypi(monkeypatch):
    """Serve a JSON index for the API URL and a wheel for anything else."""
    state = {"index": _index({"1.0.0": "2026-01-01", "2.0.0": "2026-02-01"})}

    def fake_urlopen(url, timeout=None):
        u = url if isinstance(url, str) else url.full_url
        if u.endswith("/json"):
            return _Resp(json.dumps(state["index"]).encode())
        return _Resp(_wheel_bytes())

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", fake_urlopen)
    return state


def test_releases_maps_version_to_upload_date(fake_pypi):
    assert fetch_mod.releases("pkg") == {"1.0.0": "2026-01-01", "2.0.0": "2026-02-01"}


def test_fetch_unpacks_and_returns_the_package_directory(fake_pypi, tmp_path):
    got = fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path)
    assert got.is_dir()
    assert (got / "__init__.py").exists()
    assert (got / "mod.py").read_text(encoding="utf8").startswith("def f()")


def test_the_wheel_archive_is_not_left_behind(fake_pypi, tmp_path):
    got = fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path)
    assert not (got.parent / "wheel.zip").exists()


def test_a_second_fetch_is_served_from_cache(fake_pypi, tmp_path, monkeypatch):
    first = fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path)

    def explode(*a, **k):
        raise AssertionError("cached fetch must not hit the network")

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", explode)
    assert fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path) == first


def test_an_unknown_version_names_the_versions_that_do_exist(fake_pypi, tmp_path):
    with pytest.raises(fetch_mod.FetchError) as exc:
        fetch_mod.fetch("pkg", "9.9.9", cache_dir=tmp_path)
    msg = str(exc.value)
    assert "9.9.9" in msg
    assert "1.0.0" in msg, "tell the user what they could have asked for"


def test_an_http_error_is_reported_not_raised_raw(monkeypatch, tmp_path):
    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", boom)
    with pytest.raises(fetch_mod.FetchError, match="404"):
        fetch_mod.releases("nope")


def test_an_unreachable_index_is_reported_as_such(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", boom)
    with pytest.raises(fetch_mod.FetchError, match="could not reach PyPI"):
        fetch_mod.releases("pkg")


def test_a_release_with_no_wheel_is_refused_clearly(monkeypatch, tmp_path):
    index = {
        "releases": {
            "1.0.0": [
                {
                    "packagetype": "sdist",
                    "filename": "pkg-1.0.0.tar.gz",
                    "url": "https://example.invalid/pkg.tar.gz",
                    "upload_time": "2026-01-01T00:00:00",
                }
            ]
        }
    }
    monkeypatch.setattr(
        fetch_mod.urllib.request,
        "urlopen",
        lambda url, timeout=None: _Resp(json.dumps(index).encode()),
    )
    with pytest.raises(fetch_mod.FetchError, match="no wheel"):
        fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path)


def test_a_wheel_with_no_importable_package_is_reported(monkeypatch, tmp_path):
    """This is the real 0.0.1a0 case, and why the sweep reports its skips."""
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("pkg-0.0.1.dist-info/METADATA", "Name: pkg\n")

    def serve(url, timeout=None):
        u = url if isinstance(url, str) else url.full_url
        if u.endswith("/json"):
            return _Resp(json.dumps(_index({"0.0.1": "2025-10-01"})).encode())
        return _Resp(empty.getvalue())

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", serve)
    with pytest.raises(fetch_mod.FetchError, match="no importable package"):
        fetch_mod.fetch("pkg", "0.0.1", cache_dir=tmp_path)


def test_a_non_pure_wheel_is_still_usable(monkeypatch, tmp_path):
    """Prefer py3-none-any, but do not refuse a platform wheel of pure source."""

    def serve(url, timeout=None):
        u = url if isinstance(url, str) else url.full_url
        if u.endswith("/json"):
            idx = _index({"1.0.0": "2026-01-01"}, wheel_name="pkg-{v}-cp313-win_amd64.whl")
            return _Resp(json.dumps(idx).encode())
        return _Resp(_wheel_bytes())

    monkeypatch.setattr(fetch_mod.urllib.request, "urlopen", serve)
    assert fetch_mod.fetch("pkg", "1.0.0", cache_dir=tmp_path).is_dir()


def test_releases_skips_versions_with_no_files(monkeypatch):
    monkeypatch.setattr(
        fetch_mod.urllib.request,
        "urlopen",
        lambda url, timeout=None: _Resp(
            json.dumps({"releases": {"1.0.0": [], "2.0.0": [
                {"packagetype": "bdist_wheel", "filename": "p.whl",
                 "url": "u", "upload_time": "2026-02-01T00:00:00"}
            ]}}).encode()
        ),
    )
    assert fetch_mod.releases("pkg") == {"2.0.0": "2026-02-01"}
