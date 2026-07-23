#!/usr/bin/env bash
#
# Downloads the libraries the mirror was supposed to have published to the
# feed created by azure-e2e-setup.sh, using the real npm/pip clients — not
# just a metadata check — and confirms the expected artifacts show up.
#
# Requires: curl, python3, node/npm. Env:
#   AZURE_ARTIFACTS_PAT   Same PAT the mirror step pushed with
#   NPM_REGISTRY          npm registry URL output by azure-e2e-setup.sh
#   PYPI_INDEX            PyPI simple index URL output by azure-e2e-setup.sh
set -euo pipefail

: "${AZURE_ARTIFACTS_PAT:?AZURE_ARTIFACTS_PAT is required}"
: "${NPM_REGISTRY:?NPM_REGISTRY is required}"
: "${PYPI_INDEX:?PYPI_INDEX is required}"

STATE="$(mktemp -d)"
trap 'rm -rf "$STATE"' EXIT

fail() { echo "❌ $*"; exit 1; }
ok()   { echo "✅ $*"; }

AZURE_HOST="$(python3 -c "import sys,urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).netloc)" "$NPM_REGISTRY")"
AZURE_PW="$(printf '%s' "$AZURE_ARTIFACTS_PAT" | base64 | tr -d '\n')"
cat > "$STATE/.npmrc" <<EOF
//$AZURE_HOST/:username=echo
//$AZURE_HOST/:_password=$AZURE_PW
//$AZURE_HOST/:email=mirror@echohq.com
//$AZURE_HOST/:always-auth=true
EOF

echo "== downloading npm packages from the feed =="
for spec in "lodash@4.17.21" "left-pad@1.3.0"; do
  npm pack "$spec" --registry="$NPM_REGISTRY" --userconfig="$STATE/.npmrc" \
    --pack-destination="$STATE" >/dev/null \
    || fail "npm pack failed for $spec (not in the feed, or auth rejected)"
done
[ -f "$STATE/lodash-4.17.21.tgz" ]  || fail "lodash tarball missing after npm pack"
[ -f "$STATE/left-pad-1.3.0.tgz" ] || fail "left-pad tarball missing after npm pack"
ok "npm packages downloaded from the feed (lodash@4.17.21, left-pad@1.3.0)"

echo; echo "== downloading pypi package from the feed =="
PYPI_INDEX_AUTH="$(python3 -c "
import sys, urllib.parse
scheme, rest = sys.argv[1].split('://', 1)
print(f'{scheme}://echo:{urllib.parse.quote(sys.argv[2], safe=\"\")}@{rest}')
" "$PYPI_INDEX" "$AZURE_ARTIFACTS_PAT")"
mkdir -p "$STATE/pypi"
python3 -m pip download "six==1.16.0" --no-deps --index-url "$PYPI_INDEX_AUTH" -d "$STATE/pypi" >/dev/null \
  || fail "pip download failed for six==1.16.0 (not in the feed, or auth rejected)"
compgen -G "$STATE/pypi/six-1.16.0*" >/dev/null || fail "six artifact missing after pip download"
ok "pypi package downloaded from the feed (six==1.16.0)"

echo; echo "🎉 all expected libraries verified in the feed"
