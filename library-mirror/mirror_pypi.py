"""PyPI mirror: pull with `pip download`, publish with `twine upload`.

Also holds the PEP 503 simple-index parsing and version-extraction helpers,
which are only needed on the PyPI side.
"""

from __future__ import annotations

import html.parser
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

from config import Config
from mirror_base import Mirror
from packages import Requested
from results import Blocked, PushError
from shell import _basic, _http_get, run


class _SimpleIndexParser(html.parser.HTMLParser):
    """Collect anchor filenames from a PEP 503 simple index page."""

    def __init__(self) -> None:
        super().__init__()
        self.files: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.files.append(v.rsplit("/", 1)[-1].split("#", 1)[0])


class PypiMirror(Mirror):
    def __init__(self, cfg: Config, workdir: Path):
        super().__init__(cfg, workdir)
        self.echo_index = cfg.echo_pypi_index
        # Token-only pip auth: empty username, url-encoded Echo key as password.
        scheme, rest = self.echo_index.split("://", 1)
        token = urllib.parse.quote(cfg.echo_key, safe="")
        self.echo_index_auth = f"{scheme}://:{token}@{rest}"

    def setup_auth(self) -> None:
        # twine reads TWINE_* from the environment at push time.
        pass

    def resolve_versions(self, pkg: Requested) -> list[str]:
        if pkg.version:
            return [pkg.version]
        files = self._list_index(self.echo_index_auth, pkg.name)
        vers = _sorted_versions(_versions_from_filenames(pkg.name, files))
        if not vers:
            print(f"    no versions found for {pkg.name} at Echo; skipping")
            return []
        return [_pick_latest(vers)] if self.cfg.versions == "latest" else vers

    def exists_at_dest(self, name: str, version: str) -> bool:
        auth = _basic("echo", self.cfg.azure_token)
        url = self.cfg.azure_pypi_index + _pep503_name(name) + "/"
        status, body = _http_get(url, auth)
        if status == 404:
            return False
        if status != 200:
            # Auth failure / 5xx / etc: don't parse the body as an index page —
            # assume "not present" so the mirror retries the push rather than
            # silently skipping a version it couldn't actually confirm.
            print(f"    warning: unexpected status {status} checking {name} at destination")
            return False
        files = _parse_simple(body.decode("utf-8", "replace"))
        return version in _versions_from_filenames(name, files)

    def _list_index(self, index_base: str, name: str) -> list[str]:
        url = index_base + _pep503_name(name) + "/"
        # index_base already carries credentials in the URL for Echo.
        status, body = _http_get(url)
        if status != 200:
            return []
        return _parse_simple(body.decode("utf-8", "replace"))

    def _pull_once(self, name: str, version: str, dest: Path) -> list[Path]:
        # A retry reuses the same `dest`. Clear it first so a partial/stale
        # file left by an earlier failed attempt can't get swept into this
        # attempt's result (and end up published as-is).
        for f in dest.iterdir():
            shutil.rmtree(f) if f.is_dir() else f.unlink()

        proc = run(
            [sys.executable, "-m", "pip", "download", f"{name}=={version}", "--no-deps",
             "--index-url", self.echo_index_auth, "-d", str(dest)],
            check=False,
        )
        if proc.returncode != 0:
            raise Blocked(f"pip download failed for {name}=={version}")
        got = list(dest.glob("*"))
        if not got:
            raise Blocked(f"no artifact downloaded for {name}=={version}")
        return got

    def push(self, artifacts: list[Path]) -> None:
        env = {
            **os.environ,
            "TWINE_USERNAME": "echo",
            "TWINE_PASSWORD": self.cfg.azure_token,
            "TWINE_NON_INTERACTIVE": "1",
        }
        # No --skip-existing: newer twine rejects it for generic repository URLs.
        # Idempotency comes from exists_at_dest; a conflict here is still safe.
        proc = run(
            [sys.executable, "-m", "twine", "upload", "--repository-url", self.cfg.azure_pypi_upload,
             *[str(a) for a in artifacts]],
            env=env, check=False,
        )
        if proc.returncode != 0:
            combined = f"{proc.stdout}\n{proc.stderr}"
            if _is_duplicate_upload(combined):
                print("      already present at destination; treating as success")
                return
            raise PushError(f"twine upload failed: {(proc.stderr or proc.stdout).strip()}")


_CONFLICT_STATUS_RE = re.compile(r"\b(400|403|409)\b")


def _is_duplicate_upload(output: str) -> bool:
    """True only for "this exact file/version already exists at the
    destination" — not any error that merely mentions a similar word.
    Requires both a real conflict-shaped status code AND the specific
    "already exist(s)" phrasing PyPI-compatible servers use for this case,
    so an unrelated failure that happens to say e.g. "dependency conflict"
    can't be misread as a harmless duplicate and reported as published.
    """
    lowered = output.lower()
    return "already exist" in lowered and bool(_CONFLICT_STATUS_RE.search(lowered))


def _sorted_versions(versions: set[str]) -> list[str]:
    """Sort versions ascending, PEP 440-aware when `packaging` is available."""
    try:
        from packaging.version import InvalidVersion, Version

        def key(v: str):
            try:
                return (0, Version(v))
            except InvalidVersion:
                return (1, v)  # unparseable sort last, lexically among themselves

        return sorted(versions, key=key)
    except ImportError:
        return sorted(versions)


def _pick_latest(versions: list[str]) -> str:
    """Pick the highest *stable* release, matching pip's own default behavior
    (prereleases are skipped unless nothing else is available). `versions`
    is ascending-sorted by `_sorted_versions`. PyPI has no dist-tag like npm's
    `latest`, so "highest stable" is the closest equivalent.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return versions[-1]
    for v in reversed(versions):
        try:
            if not Version(v).is_prerelease:
                return v
        except InvalidVersion:
            continue
    return versions[-1]  # only pre-releases / unparseable versions available


def _parse_simple(body: str) -> list[str]:
    p = _SimpleIndexParser()
    p.feed(body)
    return p.files


def _pep503_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _versions_from_filenames(name: str, files: list[str]) -> set[str]:
    """Extract version strings from a package's sdist/wheel filenames.

    Wheel:  {dist}-{version}(-{build})?-{py}-{abi}-{plat}.whl
    sdist:  {name}-{version}.{tar.gz|zip|tar.bz2}
    In both cases the name may itself contain separators (and, for
    non-compliant wheels, a literal hyphen rather than the spec-mandated
    underscore), so the name boundary is matched with PEP 503 normalization
    (`-`, `_`, `.` equivalent) rather than a plain split on `-`. The version
    is then captured verbatim from the original filename.
    """
    norm = _pep503_name(name)
    tokens = norm.split("-")
    name_pat = r"[-_.]+".join(re.escape(t) for t in tokens)
    name_re = re.compile(rf"^{name_pat}[-_.]+(?P<rest>.+)$", re.IGNORECASE)

    versions: set[str] = set()
    for f in files:
        if f.endswith(".whl"):
            m = name_re.match(f[:-4])
            # The field right after the name is always the version; anything
            # further (build/python/abi/platform tags) comes after another '-'.
            if m:
                versions.add(m.group("rest").split("-", 1)[0])
            continue
        for ext in (".tar.gz", ".zip", ".tar.bz2"):
            if f.endswith(ext):
                m = name_re.match(f[: -len(ext)])
                if m:
                    versions.add(m.group("rest"))
                break
    return versions
