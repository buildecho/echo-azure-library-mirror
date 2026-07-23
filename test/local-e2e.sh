#!/usr/bin/env bash
#
# End-to-end test of library-mirror with zero credentials.
#
# Stands up local stand-ins for Echo (source) and Azure Artifacts (destination):
#   - two Verdaccio registries for npm (echo proxies the public npm registry)
#   - two pypiserver instances for PyPI (echo seeded with a real package)
# then runs the mirror and asserts: packages land at the destination, a second
# run skips them (idempotent), and the artifacts are byte-for-byte identical.
#
# Requires: docker, python3, node/npm. Run from anywhere:  test/local-e2e.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STATE="$(mktemp -d)"
VENV="$STATE/venv"

NPM_ECHO=4873 NPM_AZURE=4874 PY_ECHO=8081 PY_AZURE=8082
NAMES=(lm-vd-echo lm-vd-azure lm-pypi-echo lm-pypi-azure)

cleanup() {
  docker rm -f "${NAMES[@]}" >/dev/null 2>&1 || true
  rm -rf "$STATE"
}
trap cleanup EXIT

fail() { echo "❌ $*"; exit 1; }
ok()   { echo "✅ $*"; }

echo "== preparing =="
docker rm -f "${NAMES[@]}" >/dev/null 2>&1 || true
mkdir -p "$STATE/vd-echo" "$STATE/vd-azure" "$STATE/pypi-echo" "$STATE/pypi-azure"

cat > "$STATE/vd-echo/config.yaml" <<'YAML'
storage: /verdaccio/storage
uplinks: { npmjs: { url: https://registry.npmjs.org/ } }
packages:
  '**': { access: $all, publish: $all, unpublish: $all, proxy: npmjs }
log: { type: stdout, format: pretty, level: warn }
YAML
cat > "$STATE/vd-azure/config.yaml" <<'YAML'
storage: /verdaccio/storage
uplinks: {}
packages:
  '**': { access: $all, publish: $all, unpublish: $all }
log: { type: stdout, format: pretty, level: warn }
YAML
chmod -R 777 "$STATE"

echo "== python venv (mirror deps) =="
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$ROOT/library-mirror/requirements.txt"

echo "== seed PyPI 'echo' with a real package =="
"$VENV/bin/pip" download six==1.16.0 --no-deps -d "$STATE/pypi-echo" >/dev/null
"$VENV/bin/pip" download six==1.16.0 --no-deps --no-binary :all: -d "$STATE/pypi-echo" >/dev/null

echo "== starting registries =="
docker run -d --name lm-vd-echo   -p $NPM_ECHO:4873  -v "$STATE/vd-echo/config.yaml:/verdaccio/conf/config.yaml"  verdaccio/verdaccio:6 >/dev/null
docker run -d --name lm-vd-azure  -p $NPM_AZURE:4873 -v "$STATE/vd-azure/config.yaml:/verdaccio/conf/config.yaml" verdaccio/verdaccio:6 >/dev/null
docker run -d --name lm-pypi-echo  -p $PY_ECHO:8080  -v "$STATE/pypi-echo:/data/packages"  pypiserver/pypiserver:latest run -P . -a . --disable-fallback /data/packages >/dev/null
docker run -d --name lm-pypi-azure -p $PY_AZURE:8080 -v "$STATE/pypi-azure:/data/packages" pypiserver/pypiserver:latest run -P . -a . --disable-fallback /data/packages >/dev/null

echo "== waiting for readiness =="
for url in "http://localhost:$NPM_ECHO/-/ping" "http://localhost:$NPM_AZURE/-/ping" \
           "http://localhost:$PY_ECHO/" "http://localhost:$PY_AZURE/"; do
  for i in $(seq 1 30); do
    curl -sf -o /dev/null "$url" && break
    [ "$i" = 30 ] && fail "timed out waiting for $url"
    sleep 1
  done
done
ok "all registries up"

run_mirror() {
  PIP_TRUSTED_HOST=localhost \
  INPUT_ECOSYSTEM=all \
  INPUT_NPM_PACKAGES=$'lodash@4.17.21\nleft-pad@1.3.0' \
  INPUT_PYPI_PACKAGES='six==1.16.0' \
  INPUT_ECHO_NPM_REGISTRY="http://localhost:$NPM_ECHO/" \
  INPUT_ECHO_PYPI_INDEX="http://localhost:$PY_ECHO/simple/" \
  INPUT_AZURE_NPM_REGISTRY="http://localhost:$NPM_AZURE/" \
  INPUT_AZURE_PYPI_UPLOAD="http://localhost:$PY_AZURE/" \
  INPUT_AZURE_PYPI_INDEX="http://localhost:$PY_AZURE/simple/" \
  INPUT_ECHO_USERNAME=echo INPUT_ECHO_KEY=dummy INPUT_AZURE_TOKEN=dummy \
    "$VENV/bin/python" "$ROOT/library-mirror/sync.py"
}

echo; echo "== FIRST RUN =="
run_mirror | tee "$STATE/run1.log"
grep -q "published  : 3" "$STATE/run1.log" || fail "expected 3 published on first run"
ok "first run published 3"

echo; echo "== verifying destination =="
curl -sf "http://localhost:$NPM_AZURE/lodash"   | grep -q '4.17.21' || fail "lodash missing at destination"
curl -sf "http://localhost:$NPM_AZURE/left-pad" | grep -q '1.3.0'   || fail "left-pad missing at destination"
curl -sf "http://localhost:$PY_AZURE/simple/six/" | grep -q '1.16.0' || fail "six missing at destination"
ok "all three packages present at destination"

echo; echo "== byte-for-byte (npm lodash) =="
curl -sf "http://localhost:$NPM_ECHO/lodash/-/lodash-4.17.21.tgz"  -o "$STATE/e.tgz"
curl -sf "http://localhost:$NPM_AZURE/lodash/-/lodash-4.17.21.tgz" -o "$STATE/a.tgz"
[ "$(shasum -a256 "$STATE/e.tgz" | cut -d' ' -f1)" = "$(shasum -a256 "$STATE/a.tgz" | cut -d' ' -f1)" ] \
  || fail "lodash tarball differs between source and destination"
ok "lodash tarball identical source->destination"

echo; echo "== SECOND RUN (idempotency) =="
run_mirror | tee "$STATE/run2.log"
grep -q "skipped    : 3" "$STATE/run2.log" || fail "expected 3 skipped on second run"
grep -q "published  : 0" "$STATE/run2.log" || fail "expected 0 published on second run"
ok "second run skipped all 3 (idempotent)"

echo; echo "== DRY RUN (a package not yet at the destination) =="
PIP_TRUSTED_HOST=localhost \
INPUT_ECOSYSTEM=npm \
INPUT_NPM_PACKAGES='is-odd@3.0.1' \
INPUT_ECHO_NPM_REGISTRY="http://localhost:$NPM_ECHO/" \
INPUT_AZURE_NPM_REGISTRY="http://localhost:$NPM_AZURE/" \
INPUT_ECHO_USERNAME=echo INPUT_ECHO_KEY=dummy INPUT_AZURE_TOKEN=dummy \
INPUT_DRY_RUN=true \
  "$VENV/bin/python" "$ROOT/library-mirror/sync.py" | tee "$STATE/dryrun.log"
grep -q "dry-run    : 1" "$STATE/dryrun.log" || fail "expected 1 dry-run entry"
grep -q "published  : 0" "$STATE/dryrun.log" || fail "dry-run must not count as published"
curl -sf "http://localhost:$NPM_AZURE/is-odd" >/dev/null 2>&1 \
  && fail "dry-run must not have actually published to the destination"
ok "dry-run pulled but did not publish, and was not counted as published"

echo; echo "🎉 all end-to-end checks passed"
