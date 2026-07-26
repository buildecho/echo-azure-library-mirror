"""Runtime configuration parsed from the action's INPUT_* environment vars."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from shell import _normalize_registry

ECHO_NPM_REGISTRY = "https://npm.echohq.com"
ECHO_PYPI_INDEX = "https://pypi.echohq.com/simple"


class ConfigError(Exception):
    """A required input is missing or invalid."""


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    val = os.environ.get(name, default).strip()
    if required and not val:
        raise ConfigError(f"missing required input: {name}")
    return val


def _env_int(name: str, default: str, *, min_value: int | None = None) -> int:
    val = _env(name, required=False, default=default)
    try:
        parsed = int(val)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {val!r}") from None
    if min_value is not None and parsed < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {parsed}")
    return parsed


def _env_float(name: str, default: str, *, min_value: float | None = None) -> float:
    val = _env(name, required=False, default=default)
    try:
        parsed = float(val)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {val!r}") from None
    if not math.isfinite(parsed):
        raise ConfigError(f"{name} must be a finite number, got {val!r}")
    if min_value is not None and parsed < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {parsed}")
    return parsed


@dataclass
class Config:
    ecosystems: list[str]  # subset of {"npm", "pypi"}, in run order
    npm_packages_file: Path | None
    pypi_packages_file: Path | None
    npm_packages_inline: str | None
    pypi_packages_inline: str | None
    echo_key: str  # token-only; no username
    # Echo source endpoints (default to prod; overridable for staging/tests)
    echo_npm_registry: str
    echo_npm_tarball_host: str  # host serving dist.tarball (prod: packages.echohq.com)
    echo_pypi_index: str
    # npm target
    azure_npm_registry: str
    # pypi targets
    azure_pypi_upload: str
    azure_pypi_index: str
    azure_token: str
    versions: str  # "all" | "latest"
    max_retries: int
    retry_base_seconds: float
    dry_run: bool
    strict: bool

    def packages_source(self, ecosystem: str) -> tuple[str, str]:
        """Return (label, text) for an ecosystem's package list.

        An inline `*-packages` input takes precedence over its `*-packages-file`.
        from_env guarantees one of the two is set for every active ecosystem.
        """
        if ecosystem == "npm":
            inline, path = self.npm_packages_inline, self.npm_packages_file
        else:
            inline, path = self.pypi_packages_inline, self.pypi_packages_file
        if inline:
            return (f"<{ecosystem}-packages input>", inline)
        assert path is not None
        if not path.exists():
            raise ConfigError(f"packages file not found: {path}")
        return (str(path), path.read_text())

    @classmethod
    def from_env(cls) -> "Config":
        # "npm" | "pypi" | "all" (also accepts a comma list, e.g. "npm,pypi").
        # "all" rather than "both" so adding a third ecosystem later doesn't
        # change what an existing "all" run does.
        raw = _env("INPUT_ECOSYSTEM").lower()
        if raw == "all":
            ecosystems = ["npm", "pypi"]
        else:
            # dict.fromkeys dedupes while preserving order (e.g. "npm,npm"
            # would otherwise run the npm mirror twice in one job).
            ecosystems = list(dict.fromkeys(e.strip() for e in raw.split(",") if e.strip()))
        if not ecosystems or any(e not in ("npm", "pypi") for e in ecosystems):
            raise ConfigError(
                f"ecosystem must be 'npm', 'pypi', or 'all', got {raw!r}"
            )
        npm_on = "npm" in ecosystems
        pypi_on = "pypi" in ecosystems

        versions = _env("INPUT_VERSIONS", required=False, default="all").lower()
        if versions not in ("all", "latest"):
            raise ConfigError(f"versions must be 'all' or 'latest', got {versions!r}")

        # A package list can be supplied inline (`*-packages`) or as a file
        # (`*-packages-file`); each active ecosystem needs exactly one source.
        npm_inline = _env("INPUT_NPM_PACKAGES", required=False)
        pypi_inline = _env("INPUT_PYPI_PACKAGES", required=False)
        npm_pkgs = _env("INPUT_NPM_PACKAGES_FILE", required=False)
        pypi_pkgs = _env("INPUT_PYPI_PACKAGES_FILE", required=False)
        if npm_on and not (npm_inline or npm_pkgs):
            raise ConfigError("npm is active but neither npm-packages nor npm-packages-file was set")
        if pypi_on and not (pypi_inline or pypi_pkgs):
            raise ConfigError("pypi is active but neither pypi-packages nor pypi-packages-file was set")

        # Only require the destination inputs relevant to the active ecosystem(s).
        azure_npm_registry = _env("INPUT_AZURE_NPM_REGISTRY", required=npm_on)
        azure_pypi_upload = _env("INPUT_AZURE_PYPI_UPLOAD", required=pypi_on)
        azure_pypi_index = _env("INPUT_AZURE_PYPI_INDEX", required=pypi_on)

        return cls(
            ecosystems=ecosystems,
            npm_packages_file=Path(npm_pkgs) if npm_pkgs else None,
            pypi_packages_file=Path(pypi_pkgs) if pypi_pkgs else None,
            npm_packages_inline=npm_inline or None,
            pypi_packages_inline=pypi_inline or None,
            echo_npm_registry=_normalize_registry(
                _env("INPUT_ECHO_NPM_REGISTRY", required=False, default=ECHO_NPM_REGISTRY)
            ),
            echo_npm_tarball_host=_env(
                "INPUT_ECHO_NPM_TARBALL_HOST", required=False, default="packages.echohq.com"
            ),
            echo_pypi_index=_normalize_registry(
                _env("INPUT_ECHO_PYPI_INDEX", required=False, default=ECHO_PYPI_INDEX)
            ),
            echo_key=_env("INPUT_ECHO_KEY"),
            azure_npm_registry=_normalize_registry(azure_npm_registry),
            azure_pypi_upload=azure_pypi_upload,
            azure_pypi_index=_normalize_registry(azure_pypi_index),
            azure_token=_env("INPUT_AZURE_TOKEN"),
            versions=versions,
            max_retries=_env_int("INPUT_MAX_RETRIES", default="4", min_value=1),
            retry_base_seconds=_env_float("INPUT_RETRY_BASE_SECONDS", default="5", min_value=0),
            dry_run=_env("INPUT_DRY_RUN", required=False, default="false").lower() == "true",
            strict=_env("INPUT_STRICT", required=False, default="false").lower() == "true",
        )
