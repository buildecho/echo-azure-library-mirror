"""npm mirror: pull with `npm pack`, publish with `npm publish`."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from config import Config
from mirror_base import Mirror
from packages import Requested
from results import Blocked, PushError
from shell import _host_path, run

_CONFLICT_STATUS_RE = re.compile(r"\be?409\b", re.IGNORECASE)


def _is_duplicate_publish(output: str) -> bool:
    """True only for npm's "cannot publish over an existing version" conflict
    — not any failure that happens to mention a similar word. Requires both
    a real conflict-shaped status code AND the specific wording npm/Azure use
    for this case, so an unrelated failure (e.g. one whose text incidentally
    contains "409") can't be misread as a harmless duplicate and reported as
    published when nothing actually reached the registry.
    """
    lowered = output.lower()
    has_phrase = "cannot publish over" in lowered or "already exist" in lowered
    return has_phrase and bool(_CONFLICT_STATUS_RE.search(lowered))


class NpmMirror(Mirror):
    def __init__(self, cfg: Config, workdir: Path):
        super().__init__(cfg, workdir)
        self.npmrc = workdir / ".npmrc"
        self.echo_registry = cfg.echo_npm_registry

    def setup_auth(self) -> None:
        echo_host = _host_path(self.echo_registry)
        azure_host = _host_path(self.cfg.azure_npm_registry)
        azure_pw = base64.b64encode(self.cfg.azure_token.encode()).decode()
        # Echo: token-only (bearer via _authToken). Auth both the metadata host
        # and the tarball host — `dist.tarball` points at a separate host
        # (packages.echohq.com in prod), so `npm pack` fetches it from there.
        echo_lines = "".join(
            f"//{h}/:_authToken={self.cfg.echo_key}\n"
            for h in dict.fromkeys([echo_host.rstrip("/"), self.cfg.echo_npm_tarball_host])
        )
        # Azure: PAT via legacy basic. Both registries' auth live in one .npmrc;
        # --registry picks the target per command.
        self.npmrc.write_text(
            f"registry={self.echo_registry}\n"
            f"{echo_lines}"
            f"//{echo_host}:always-auth=true\n"
            f"//{azure_host}:username=echo\n"
            f"//{azure_host}:_password={azure_pw}\n"
            f"//{azure_host}:email=mirror@echohq.com\n"
            f"//{azure_host}:always-auth=true\n"
        )
        self.npmrc.chmod(0o600)

    def _npm(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "npm_config_userconfig": str(self.npmrc)}
        return run(["npm", *args], cwd=self.workdir, env=env, check=check)

    def resolve_versions(self, pkg: Requested) -> list[str]:
        if pkg.version:
            return [pkg.version]
        if self.cfg.versions == "latest":
            # The `latest` dist-tag, not the highest semver — a package can
            # have newer prereleases that aren't its actual latest release.
            proc = self._npm(
                ["view", pkg.name, "dist-tags.latest", f"--registry={self.echo_registry}"],
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                print(f"    could not resolve latest tag for {pkg.name}; skipping")
                return []
            return [proc.stdout.strip()]
        proc = self._npm(
            ["view", pkg.name, "versions", "--json", f"--registry={self.echo_registry}"],
            check=False,
        )
        if proc.returncode != 0:
            print(f"    could not list versions for {pkg.name}; skipping")
            return []
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            print(f"    unexpected (non-JSON) output listing versions for {pkg.name}; skipping")
            return []
        return data if isinstance(data, list) else [data]

    def exists_at_dest(self, name: str, version: str) -> bool:
        proc = self._npm(
            ["view", f"{name}@{version}", "version", "--json",
             f"--registry={self.cfg.azure_npm_registry}"],
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def _pull_once(self, name: str, version: str, dest: Path) -> list[Path]:
        proc = self._npm(
            ["pack", f"{name}@{version}", "--json", f"--registry={self.echo_registry}",
             f"--pack-destination={dest}"],
            check=False,
        )
        if proc.returncode != 0:
            raise Blocked(f"npm pack failed for {name}@{version}")
        try:
            meta = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise Blocked(f"npm pack returned unexpected (non-JSON) output for {name}@{version}")
        # `npm pack --json` returns a list of pack entries on some npm versions
        # and a single keyed object (like `npm publish --json`) on others.
        entries = meta if isinstance(meta, list) else [meta]
        artifacts = [dest / entry["filename"] for entry in entries if entry.get("filename")]
        if not artifacts:
            raise Blocked(f"npm pack produced no tarball for {name}@{version}")
        return artifacts

    def push(self, artifacts: list[Path]) -> None:
        for tgz in artifacts:
            proc = self._npm(
                # --provenance=false: some packages embed publishConfig.provenance,
                # which makes npm attempt a Sigstore attestation (needs id-token
                # write and an attestation-capable registry). A mirror is not the
                # original builder and Azure doesn't accept attestations, so force off.
                ["publish", str(tgz), f"--registry={self.cfg.azure_npm_registry}",
                 "--provenance=false"],
                check=False,
            )
            if proc.returncode != 0:
                # Azure returns 409 for an already-published version — idempotent,
                # ignore and move on to any remaining artifacts. Check both streams:
                # npm doesn't consistently put this on stderr across versions.
                combined = f"{proc.stdout}\n{proc.stderr}"
                if _is_duplicate_publish(combined):
                    print("      already published at destination; treating as success")
                    continue
                raise PushError(f"npm publish failed: {proc.stderr.strip()}")
