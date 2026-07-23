#!/usr/bin/env bash
#
# End-to-end test that a feed's PUBLIC UPSTREAM fallback works: a package that
# was never mirrored (never published to the feed) still resolves, because the
# feed proxies it from its public npm / PyPI upstream and saves a copy.
#
# This is the "non-synced libs shouldn't fail the build" half of the story —
# the counterpart to azure-e2e-{setup,verify}.sh, which proves the Echo-synced
# (published-to-feed) half.
#
# IMPORTANT — this test targets a PRE-PROVISIONED feed, it does not create one.
# A public upstream that actually resolves packages can only be created through
# the Azure DevOps portal ("Include packages from common public sources" /
# Feed settings > Upstream sources > Add Upstream > Public source). An upstream
# added via the Feeds REST API with a raw location URL reports status "ok" but
# never actually serves upstream packages (confirmed repeatedly). So a human
# creates the feed once in the portal; this test just consumes from it.
#
# Set up once (portal):
#   1. Artifacts > Create Feed, tick "Include packages from common public
#      sources" (npm + PyPI), in the AZURE_DEVOPS_PROJECT below.
#   2. Point AZURE_UPSTREAM_FEED at its name.
# The feed is NOT deleted by this test — it's a shared, reusable fixture.
#
# Requires: curl, python3, node/npm. Env:
#   AZURE_DEVOPS_ORG      Azure DevOps organization name
#   AZURE_DEVOPS_PROJECT  Azure DevOps project the feed lives in
#   AZURE_UPSTREAM_FEED   Name of the portal-created feed with public upstreams
#   AZURE_ARTIFACTS_PAT   PAT with Packaging read (Collaborator+ so first-time
#                         upstream saves are allowed)
set -euo pipefail

: "${AZURE_DEVOPS_ORG:?AZURE_DEVOPS_ORG is required}"
: "${AZURE_DEVOPS_PROJECT:?AZURE_DEVOPS_PROJECT is required}"
: "${AZURE_UPSTREAM_FEED:?AZURE_UPSTREAM_FEED is required (portal-created feed with public upstreams)}"
: "${AZURE_ARTIFACTS_PAT:?AZURE_ARTIFACTS_PAT is required}"

# Packages that are NOT synced anywhere in this test — so a successful install
# can only mean the feed pulled them from its public upstream.
NPM_PKG="left-pad@1.3.0"
NPM_TARBALL="left-pad-1.3.0.tgz"
PYPI_PKG="requests==2.32.3"

NPM_REGISTRY="https://pkgs.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_packaging/$AZURE_UPSTREAM_FEED/npm/registry/"
PYPI_INDEX="https://pkgs.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_packaging/$AZURE_UPSTREAM_FEED/pypi/simple/"

STATE="$(mktemp -d)"
trap 'rm -rf "$STATE"' EXIT

fail() { echo "❌ $*"; exit 1; }
ok()   { echo "✅ $*"; }

# Retry wrapper: an on-demand upstream fetch/save can take a moment on the very
# first request for a package, so give the install a few attempts before failing.
retry() {
  local n=0 max=6
  until "$@"; do
    n=$((n + 1))
    [ "$n" -ge "$max" ] && return 1
    echo "   attempt $n failed; retrying in $((n * 5))s..." >&2
    sleep $((n * 5))
  done
}

echo "== target feed: $AZURE_UPSTREAM_FEED ($AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT) =="

# --- npm: install a package that only exists upstream ---------------------
AZURE_HOST="$(python3 -c "import sys,urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).netloc)" "$NPM_REGISTRY")"
AZURE_PW="$(printf '%s' "$AZURE_ARTIFACTS_PAT" | base64 | tr -d '\n')"
cat > "$STATE/.npmrc" <<EOF
//$AZURE_HOST/:username=echo
//$AZURE_HOST/:_password=$AZURE_PW
//$AZURE_HOST/:email=mirror@echohq.com
//$AZURE_HOST/:always-auth=true
EOF

echo; echo "== npm: install $NPM_PKG (never synced -> must come from upstream) =="
mkdir -p "$STATE/npm"
npm_pull() {
  npm pack "$NPM_PKG" --registry="$NPM_REGISTRY" --userconfig="$STATE/.npmrc" \
    --pack-destination="$STATE/npm" >/dev/null 2>&1
}
retry npm_pull || fail "npm could not resolve $NPM_PKG from the feed's public upstream"
[ -f "$STATE/npm/$NPM_TARBALL" ] || fail "$NPM_TARBALL missing after npm pack"
ok "npm resolved $NPM_PKG via upstream fallback"

# --- pypi: install a package that only exists upstream --------------------
echo; echo "== pypi: install $PYPI_PKG (never synced -> must come from upstream) =="
PYPI_INDEX_AUTH="$(python3 -c "
import sys, urllib.parse
scheme, rest = sys.argv[1].split('://', 1)
print(f'{scheme}://echo:{urllib.parse.quote(sys.argv[2], safe=\"\")}@{rest}')
" "$PYPI_INDEX" "$AZURE_ARTIFACTS_PAT")"
mkdir -p "$STATE/pypi"
pypi_pull() {
  python3 -m pip download "$PYPI_PKG" --no-deps --index-url "$PYPI_INDEX_AUTH" -d "$STATE/pypi" >/dev/null 2>&1
}
retry pypi_pull || fail "pip could not resolve $PYPI_PKG from the feed's public upstream"
compgen -G "$STATE/pypi/requests-2.32.3*" >/dev/null || fail "requests artifact missing after pip download"
ok "pypi resolved $PYPI_PKG via upstream fallback"

echo; echo "🎉 upstream-fallback end-to-end checks passed"
