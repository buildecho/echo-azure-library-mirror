#!/usr/bin/env python3
"""Mirror Echo-vetted npm / PyPI packages into an Azure Artifacts feed.

The sync pulls each requested package version from Echo's registry and
re-publishes it into the destination Azure Artifacts feed. Pulling through
Echo is the vetting gate: a malicious version is either absent from Echo's
index or blocked at download, so it can never be published downstream.

Everything runs on credentials the caller controls — Echo never sees the
Azure PAT and Azure never sees the Echo key.

Module layout:
    config.py       inputs → Config
    packages.py     package-list parsing
    results.py      Summary + Blocked/PushError
    shell.py        subprocess / redaction / http / url helpers
    mirror_base.py  Mirror (retry loop + hooks)
    mirror_npm.py   NpmMirror
    mirror_pypi.py  PypiMirror (+ PyPI index/version helpers)
    sync.py         this file — orchestration only
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from config import Config, ConfigError
from mirror_base import Mirror
from mirror_npm import NpmMirror
from mirror_pypi import PypiMirror
from packages import Requested, parse_packages
from results import Blocked, PushError, Summary


def _run_ecosystem(
    cfg: Config, ecosystem: str, label: str, packages: list[Requested], workdir: Path, summary: Summary
) -> None:
    print(f"\n### {ecosystem}: {len(packages)} package(s) from {label}")

    mirror: Mirror = (
        NpmMirror(cfg, workdir) if ecosystem == "npm" else PypiMirror(cfg, workdir)
    )
    mirror.setup_auth()

    for pkg in packages:
        versions = mirror.resolve_versions(pkg)
        if not versions:
            ref = f"{ecosystem}:{pkg.name}"
            print(f"\n-> {ref}\n   could not resolve any version; recording as unresolved")
            summary.unresolved.append(ref)
            continue
        for version in versions:
            ref = f"{ecosystem}:{pkg.name}@{version}"
            print(f"\n-> {ref}")
            if mirror.exists_at_dest(pkg.name, version):
                print("   already at destination; skipping")
                summary.skipped.append(ref)
                continue

            dl = workdir / "dl" / _safe(ref)
            dl.mkdir(parents=True, exist_ok=True)
            try:
                artifacts = mirror.pull(pkg.name, version, dl)
            except Blocked as e:
                print(f"   blocked/unavailable: {e}")
                summary.blocked.append(ref)
                continue

            if cfg.dry_run:
                print(f"   [dry-run] would publish {len(artifacts)} file(s)")
                summary.dry_run.append(ref)
                continue

            try:
                mirror.push(artifacts)
            except PushError as e:
                print(f"::error::push failed for {ref}: {e}")
                summary.failed.append(ref)
                continue

            print("   published")
            summary.published.append(ref)


def main() -> int:
    try:
        cfg = Config.from_env()
        # Resolve, parse, and validate every active ecosystem's package list
        # up front, so a bad *-packages-file path or an inline list that
        # parses to nothing (comments/blank lines only — and note inline
        # silently wins over *-packages-file) fails fast and cleanly instead
        # of surfacing mid-run, or worse, running the whole job to a hollow
        # "success" that mirrored zero packages.
        sources: dict[str, tuple[str, list[Requested]]] = {}
        for eco in cfg.ecosystems:
            label, text = cfg.packages_source(eco)
            packages = parse_packages(text, eco)
            if not packages:
                raise ConfigError(
                    f"{eco} package list from {label} has no packages after parsing "
                    "(comments/blank lines only?)"
                )
            sources[eco] = (label, packages)
    except ConfigError as e:
        print(f"::error::{e}")
        return 2

    print(f"Mirroring ecosystem(s): {', '.join(cfg.ecosystems)}")
    summary = Summary()
    # Each ecosystem gets its own workdir so npm/pypi auth files never collide.
    for ecosystem in cfg.ecosystems:
        label, packages = sources[ecosystem]
        with tempfile.TemporaryDirectory() as tmp:
            _run_ecosystem(cfg, ecosystem, label, packages, Path(tmp), summary)

    summary.report()
    if summary.failed or summary.unresolved:
        return 1
    if cfg.strict and summary.blocked:
        return 1
    return 0


def _safe(ref: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in ref)


if __name__ == "__main__":
    sys.exit(main())
