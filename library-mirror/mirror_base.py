"""Mirror base class: the pull-with-retry loop and the per-ecosystem hooks."""

from __future__ import annotations

import time
from pathlib import Path

from config import Config
from packages import Requested
from results import Blocked


class Mirror:
    """One ecosystem's pull→push implementation.

    Subclasses implement setup_auth, resolve_versions, exists_at_dest,
    _pull_once and push. The base provides the retry loop around _pull_once.
    """

    def __init__(self, cfg: Config, workdir: Path):
        self.cfg = cfg
        self.workdir = workdir

    def setup_auth(self) -> None: ...

    def resolve_versions(self, pkg: Requested) -> list[str]: ...

    def exists_at_dest(self, name: str, version: str) -> bool: ...

    def pull(self, name: str, version: str, dest: Path) -> list[Path]:
        """Pull with retry so a live-vetted (cold) version succeeds on a later
        attempt. Raise Blocked if it never becomes available."""
        last = ""
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                return self._pull_once(name, version, dest)
            except Blocked as e:
                last = str(e)
                if attempt < self.cfg.max_retries:
                    delay = self.cfg.retry_base_seconds * (2 ** (attempt - 1))
                    print(f"    pull attempt {attempt} failed ({e}); retrying in {delay:.0f}s")
                    time.sleep(delay)
        raise Blocked(last or "unavailable after retries")

    def _pull_once(self, name: str, version: str, dest: Path) -> list[Path]: ...

    def push(self, artifacts: list[Path]) -> None: ...
