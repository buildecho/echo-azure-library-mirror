"""Per-run result tracking and control-flow exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Summary:
    published: list[str] = field(default_factory=list)
    dry_run: list[str] = field(default_factory=list)  # resolved+pulled but not pushed
    skipped: list[str] = field(default_factory=list)  # already at destination
    blocked: list[str] = field(default_factory=list)  # not vetted / unavailable at Echo
    failed: list[str] = field(default_factory=list)  # push errors etc.
    unresolved: list[str] = field(default_factory=list)  # couldn't even list versions

    def report(self) -> None:
        print("\n" + "=" * 60)
        print("Mirror summary")
        print("=" * 60)
        print(f"  published  : {len(self.published)}")
        if self.dry_run:
            print(f"  dry-run    : {len(self.dry_run)} (pulled only — not pushed)")
        print(f"  skipped    : {len(self.skipped)} (already at destination)")
        print(f"  blocked    : {len(self.blocked)} (unavailable / not vetted at Echo)")
        print(f"  unresolved : {len(self.unresolved)} (couldn't list versions at Echo)")
        print(f"  failed     : {len(self.failed)}")
        for label, items in (
            ("blocked", self.blocked),
            ("unresolved", self.unresolved),
            ("failed", self.failed),
        ):
            for item in items:
                print(f"    [{label}] {item}")


class Blocked(Exception):
    """Echo did not serve the version (absent from index or vetting-blocked)."""


class PushError(Exception):
    """The destination rejected the publish for a non-idempotent reason."""
