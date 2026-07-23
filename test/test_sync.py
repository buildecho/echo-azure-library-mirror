"""Unit tests for the library-mirror orchestrator (pure logic, no network).

Run: pytest test/    (from the repo root)
The docker-backed end-to-end test lives in test/local-e2e.sh.
"""

import os

import pytest

from config import Config, ConfigError
from mirror_npm import _is_duplicate_publish
from mirror_pypi import (
    _is_duplicate_upload,
    _pep503_name,
    _pick_latest,
    _sorted_versions,
    _versions_from_filenames,
)
from packages import _parse_spec, parse_packages
from shell import _http_get, _redact


# --------------------------------------------------------------------------
# spec parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,name,version",
    [
        ("lodash", "lodash", None),
        ("react@18.2.0", "react", "18.2.0"),
        ("@types/node", "@types/node", None),
        ("@babel/core@7.24.0", "@babel/core", "7.24.0"),
    ],
)
def test_parse_spec_npm(line, name, version):
    r = _parse_spec(line, "npm")
    assert (r.name, r.version) == (name, version)


@pytest.mark.parametrize(
    "line,name,version",
    [
        ("requests", "requests", None),
        ("urllib3==2.2.2", "urllib3", "2.2.2"),
        ("typing_extensions", "typing_extensions", None),
    ],
)
def test_parse_spec_pypi(line, name, version):
    r = _parse_spec(line, "pypi")
    assert (r.name, r.version) == (name, version)


def test_parse_packages_skips_comments_and_blanks():
    text = "lodash@1.0.0\n\n# a comment\n  \nleft-pad  # trailing\n"
    pkgs = parse_packages(text, "npm")
    assert [(p.name, p.version) for p in pkgs] == [("lodash", "1.0.0"), ("left-pad", None)]


# --------------------------------------------------------------------------
# version extraction from filenames (PEP 503 aware)
# --------------------------------------------------------------------------


def test_versions_from_filenames_scopes_to_package():
    files = [
        "requests-2.32.3-py3-none-any.whl",
        "requests-2.32.3.tar.gz",
        "requests-2.31.0.tar.gz",
        "typing_extensions-4.12.2-py3-none-any.whl",  # different package
    ]
    assert _versions_from_filenames("requests", files) == {"2.31.0", "2.32.3"}


def test_versions_from_filenames_name_with_separators():
    files = [
        "zope.interface-6.0-cp311-cp311-manylinux_2_17_x86_64.whl",
        "zope.interface-6.0.tar.gz",
        "zope_interface-5.5.2.tar.gz",  # underscore spelling of same name
    ]
    assert _versions_from_filenames("zope.interface", files) == {"6.0", "5.5.2"}


def test_versions_from_filenames_hyphenated_wheel_distribution():
    # PEP 427 mandates hyphens be normalized to underscores in a wheel's
    # distribution segment, but not every wheel on PyPI is compliant — the
    # name-boundary match must still work when it isn't.
    files = ["typing-extensions-4.12.2-py3-none-any.whl"]  # non-compliant: raw hyphen
    assert _versions_from_filenames("typing-extensions", files) == {"4.12.2"}


# --------------------------------------------------------------------------
# version sorting (PEP 440)
# --------------------------------------------------------------------------


def test_sorted_versions_semver_order():
    assert _sorted_versions({"9.0", "10.0", "1.2.3", "1.10.0", "1.2"}) == [
        "1.2",
        "1.2.3",
        "1.10.0",
        "9.0",
        "10.0",
    ]


def test_pick_latest_prefers_stable_over_higher_prerelease():
    # A newer prerelease outranks an older stable by raw PEP 440 ordering, but
    # "latest" should mean the latest *stable* release, like pip's default.
    assert _pick_latest(_sorted_versions({"4.9.0", "5.0.0a1"})) == "4.9.0"


def test_pick_latest_falls_back_to_prerelease_if_no_stable_exists():
    assert _pick_latest(_sorted_versions({"1.0.0a1", "1.0.0a2"})) == "1.0.0a2"


# --------------------------------------------------------------------------
# credential redaction
# --------------------------------------------------------------------------


def test_redact_masks_url_userinfo():
    s = "pip download x --index-url https://user:s3kr3t@pypi.echohq.com/simple"
    out = _redact(s)
    assert "s3kr3t" not in out and "user" not in out
    assert "pypi.echohq.com/simple" in out


def test_redact_masks_empty_user_url():
    # Echo pip auth is token-only: scheme://:<token>@host (empty username).
    s = "pip download x --index-url https://:s3kr3t-token@pypi.echohq.com/simple"
    out = _redact(s)
    assert "s3kr3t-token" not in out
    assert "pypi.echohq.com/simple" in out


# --------------------------------------------------------------------------
# twine duplicate-upload detection
# --------------------------------------------------------------------------


def test_is_duplicate_upload_true_for_known_conflict():
    out = "HTTPError: 400 Bad Request from https://example/\n    File already exists."
    assert _is_duplicate_upload(out) is True


def test_is_duplicate_upload_false_for_unrelated_conflict_wording():
    # Must not fire on a loose "conflict"/"duplicate" mention alone — that was
    # the actual bug: an unrelated failure could be misread as a harmless
    # duplicate and reported as published even though nothing was uploaded.
    out = "ERROR: dependency conflict detected while resolving build requirements"
    assert _is_duplicate_upload(out) is False


def test_is_duplicate_upload_false_without_a_conflict_status_code():
    out = "already exists somewhere else, unrelated to this upload"
    assert _is_duplicate_upload(out) is False


# --------------------------------------------------------------------------
# npm duplicate-publish detection
# --------------------------------------------------------------------------


def test_is_duplicate_publish_true_for_known_conflict():
    out = "npm error code E409\nnpm error 409 Conflict - cannot publish over the previously published version"
    assert _is_duplicate_publish(out) is True


def test_is_duplicate_publish_false_for_bare_409_substring():
    # The actual bug: any output containing "409" (e.g. a coincidental digit
    # sequence or an unrelated error) was misread as a harmless duplicate.
    out = "npm error network request failed, retrying (409ms elapsed)"
    assert _is_duplicate_publish(out) is False


def test_is_duplicate_publish_false_without_conflict_status():
    out = "npm error cannot publish over this package for an unrelated reason"
    assert _is_duplicate_publish(out) is False


# --------------------------------------------------------------------------
# _http_get network-error handling
# --------------------------------------------------------------------------


def test_http_get_handles_connection_errors_without_raising(monkeypatch):
    import urllib.error

    import shell

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(shell.urllib.request, "urlopen", boom)
    status, body = _http_get("https://example.invalid/simple/pkg/")
    assert status == 0
    assert body == b""


# --------------------------------------------------------------------------
# pep503 normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,norm",
    [
        ("Typing_Extensions", "typing-extensions"),
        ("zope.interface", "zope-interface"),
        ("Flask", "flask"),
    ],
)
def test_pep503_name(raw, norm):
    assert _pep503_name(raw) == norm


# --------------------------------------------------------------------------
# Config.from_env
# --------------------------------------------------------------------------


@pytest.fixture
def base_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("INPUT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INPUT_ECHO_KEY", "t")
    monkeypatch.setenv("INPUT_AZURE_TOKEN", "pat")
    return monkeypatch


def test_config_all_resolves_two_ecosystems(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "all")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    base_env.setenv("INPUT_PYPI_PACKAGES", "requests")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    base_env.setenv("INPUT_AZURE_PYPI_UPLOAD", "https://x/pypi/upload/")
    base_env.setenv("INPUT_AZURE_PYPI_INDEX", "https://x/pypi/simple/")
    cfg = Config.from_env()
    assert cfg.ecosystems == ["npm", "pypi"]


def test_config_dedupes_repeated_ecosystem(base_env):
    # "npm,npm" would otherwise run the npm mirror twice in one job.
    base_env.setenv("INPUT_ECOSYSTEM", "npm,npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    cfg = Config.from_env()
    assert cfg.ecosystems == ["npm"]


def test_config_inline_takes_precedence_over_file(base_env, tmp_path):
    f = tmp_path / "npm.txt"
    f.write_text("from-file@1.0.0\n")
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "inline-pkg@2.0.0")
    base_env.setenv("INPUT_NPM_PACKAGES_FILE", str(f))
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    cfg = Config.from_env()
    _, text = cfg.packages_source("npm")
    assert "inline-pkg" in text and "from-file" not in text


def test_config_file_used_when_no_inline(base_env, tmp_path):
    f = tmp_path / "npm.txt"
    f.write_text("from-file@1.0.0\n")
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES_FILE", str(f))
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    cfg = Config.from_env()
    label, text = cfg.packages_source("npm")
    assert label == str(f) and "from-file" in text


def test_config_missing_packages_file_raises_configerror(base_env, tmp_path):
    # from_env() only checks that a *-packages-file path was given, not that
    # it exists — that check happens in packages_source(), which main() must
    # call inside the same ConfigError-handling try block it uses for from_env.
    missing = tmp_path / "does-not-exist.txt"
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES_FILE", str(missing))
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    cfg = Config.from_env()
    with pytest.raises(ConfigError):
        cfg.packages_source("npm")


def test_config_rejects_bad_ecosystem(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "maven")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_requires_a_package_source(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_requires_destination(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    # no INPUT_AZURE_NPM_REGISTRY
    with pytest.raises(ConfigError):
        Config.from_env()


def _minimal_npm_env(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")


def test_config_rejects_non_numeric_max_retries(base_env):
    _minimal_npm_env(base_env)
    base_env.setenv("INPUT_MAX_RETRIES", "not-a-number")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_rejects_non_numeric_retry_base_seconds(base_env):
    _minimal_npm_env(base_env)
    base_env.setenv("INPUT_RETRY_BASE_SECONDS", "soon")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_rejects_zero_max_retries(base_env):
    # max-retries=0 would make the retry loop never attempt a pull at all.
    _minimal_npm_env(base_env)
    base_env.setenv("INPUT_MAX_RETRIES", "0")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_rejects_negative_retry_base_seconds(base_env):
    # time.sleep(negative) raises ValueError outside the ConfigError handler.
    _minimal_npm_env(base_env)
    base_env.setenv("INPUT_RETRY_BASE_SECONDS", "-1")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_rejects_non_finite_retry_base_seconds(base_env):
    # float("inf") parses fine but would block the retry loop indefinitely.
    _minimal_npm_env(base_env)
    base_env.setenv("INPUT_RETRY_BASE_SECONDS", "inf")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_echo_endpoints_overridable(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    base_env.setenv("INPUT_ECHO_NPM_REGISTRY", "http://localhost:4873")
    cfg = Config.from_env()
    assert cfg.echo_npm_registry == "http://localhost:4873/"  # normalized trailing slash


def test_config_defaults_to_prod_echo(base_env):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "lodash")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    cfg = Config.from_env()
    assert "npm.echohq.com" in cfg.echo_npm_registry


# --------------------------------------------------------------------------
# main(): a non-empty package list that parses to zero packages must not
# silently "succeed" — this also covers the case where a comment-only inline
# *-packages value would otherwise shadow a real *-packages-file.
# --------------------------------------------------------------------------


def test_main_rejects_comment_only_inline_packages(base_env, capsys):
    base_env.setenv("INPUT_ECOSYSTEM", "npm")
    base_env.setenv("INPUT_NPM_PACKAGES", "# just a comment\n\n  \n")
    base_env.setenv("INPUT_AZURE_NPM_REGISTRY", "https://x/npm/registry/")
    import sync

    assert sync.main() == 2
    assert "no packages after parsing" in capsys.readouterr().out
