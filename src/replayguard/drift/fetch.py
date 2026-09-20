"""Get a named SDK version onto disk, without installing it.

Installing would be the obvious approach and is the wrong one: the whole point
is to hold two versions of the same distribution side by side, which pip will
not do inside one environment. A wheel is a zip, so this unpacks two of them
into a cache and hands back two directories.

Stdlib only. A tool whose job is to warn about dependency drift should not
bring a dependency tree of its own.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/{package}/json"

DEFAULT_CACHE = Path.home() / ".cache" / "replayguard-drift"


class FetchError(RuntimeError):
    pass


def releases(package: str, *, timeout: float = 30.0) -> dict[str, str]:
    """Map version -> upload date (YYYY-MM-DD) for every release."""
    url = PYPI_JSON.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{package}: PyPI returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"{package}: could not reach PyPI ({exc})") from exc

    out: dict[str, str] = {}
    for version, files in (data.get("releases") or {}).items():
        if not files:
            continue
        out[version] = (files[0].get("upload_time") or "")[:10]
    return out


def _wheel_url(package: str, version: str, *, timeout: float = 30.0) -> str:
    url = PYPI_JSON.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{package}: PyPI returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"{package}: could not reach PyPI ({exc})") from exc

    files = (data.get("releases") or {}).get(version)
    if not files:
        known = sorted(data.get("releases") or {})
        raise FetchError(
            f"{package} has no release {version}. Known: {', '.join(known[-8:])}"
        )

    # Prefer a pure-python wheel. Falling back to any wheel is fine for reading
    # source; a platform wheel of a pure-python SDK is unusual but harmless.
    for f in files:
        if f.get("packagetype") == "bdist_wheel" and f.get("filename", "").endswith(
            "-py3-none-any.whl"
        ):
            return f["url"]
    for f in files:
        if f.get("packagetype") == "bdist_wheel":
            return f["url"]
    raise FetchError(f"{package} {version} has no wheel; cannot read it without a build")


def fetch(
    package: str,
    version: str,
    *,
    cache_dir: Path | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download and unpack `package==version`, returning its package directory.

    Cached by version, so a repeated diff costs nothing after the first run.
    """
    cache_dir = Path(cache_dir or DEFAULT_CACHE)
    dest = cache_dir / package / version
    package_dir = _package_dir(dest) if dest.is_dir() else None
    if package_dir is not None:
        return package_dir

    url = _wheel_url(package, version, timeout=timeout)
    dest.mkdir(parents=True, exist_ok=True)
    wheel = dest / "wheel.zip"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            wheel.write_bytes(resp.read())
        with zipfile.ZipFile(wheel) as z:
            z.extractall(dest)
    except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile) as exc:
        raise FetchError(f"{package} {version}: download failed ({exc})") from exc
    finally:
        wheel.unlink(missing_ok=True)

    package_dir = _package_dir(dest)
    if package_dir is None:
        raise FetchError(f"{package} {version}: no importable package inside the wheel")
    return package_dir


def _package_dir(root: Path) -> Path | None:
    """The one directory in an unpacked wheel that is the package itself."""
    candidates = [
        p
        for p in sorted(root.iterdir())
        if p.is_dir()
        and not p.name.endswith((".dist-info", ".data"))
        and (p / "__init__.py").exists()
    ]
    return candidates[0] if candidates else None
