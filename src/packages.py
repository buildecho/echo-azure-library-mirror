"""Package-list parsing (shared by npm and PyPI)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Requested:
    name: str
    version: str | None  # pinned version, or None to resolve from Echo


def parse_packages(text: str, ecosystem: str) -> list[Requested]:
    """One package per line. Blank lines and `#` comments ignored.

    Accepts a bare name (resolve versions from Echo) or a pin:
      npm:  name  |  name@1.2.3  |  @scope/name  |  @scope/name@1.2.3
      pypi: name  |  name==1.2.3
    """
    out: list[Requested] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.append(_parse_spec(line, ecosystem))
    return out


def _parse_spec(line: str, ecosystem: str) -> Requested:
    if ecosystem == "npm":
        # Scoped names start with '@'; the version separator is the LAST '@'.
        at = line.rfind("@")
        if at > 0:  # >0 so a leading scope '@' isn't treated as separator
            return Requested(name=line[:at], version=line[at + 1 :] or None)
        return Requested(name=line, version=None)
    # pypi
    if "==" in line:
        name, _, ver = line.partition("==")
        return Requested(name=name.strip(), version=ver.strip() or None)
    return Requested(name=line, version=None)
