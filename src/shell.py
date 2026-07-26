"""Low-level helpers: subprocess execution, credential redaction, HTTP, URLs."""

from __future__ import annotations

import base64
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

_CRED_RE = re.compile(r"(://)[^:/@\s]*:[^@/\s]+(@)")


def _redact(s: str) -> str:
    """Mask userinfo credentials embedded in URLs before logging."""
    return _CRED_RE.sub(r"\1***:***\2", s)


def _emit(text: str, limit: int = 15) -> None:
    """Print command output (redacted), truncated so a run stays readable."""
    lines = _redact(text.strip()).splitlines()
    for ln in lines[:limit]:
        print("      " + ln)
    if len(lines) > limit:
        print(f"      … ({len(lines) - limit} more line(s) suppressed)")


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    # Quiet on success: subprocess output is captured for the caller to parse,
    # but only surfaced (command + truncated output) when the command fails.
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print(f"    $ {_redact(' '.join(cmd))}")
        _emit(proc.stderr or proc.stdout)
        if check:
            raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    return proc


def _http_get(url: str, auth_header: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError:
        # No HTTP response at all — timeout, DNS failure, connection refused,
        # TLS error. Callers already treat any non-200/404 status as "unknown/
        # not present" and fail safe, so surface this the same way rather than
        # letting it crash the whole run mid-mirror.
        return 0, b""


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def _normalize_registry(url: str) -> str:
    return url.rstrip("/") + "/" if url else url


def _host_path(registry_url: str) -> str:
    """`https://host/a/b/` -> `host/a/b/` for .npmrc auth scoping."""
    no_scheme = registry_url.split("://", 1)[-1]
    return no_scheme if no_scheme.endswith("/") else no_scheme + "/"
