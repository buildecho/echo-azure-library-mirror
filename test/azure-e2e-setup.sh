#!/usr/bin/env bash
#
# Creates a throwaway Azure Artifacts feed for the real-Azure end-to-end test.
# The action itself runs against this feed as a separate `uses: ./`
# workflow step — this script only provisions the feed and hands back where it is.
#
# Requires: curl, jq. Env:
#   AZURE_DEVOPS_ORG      Azure DevOps organization name
#   AZURE_DEVOPS_PROJECT  Azure DevOps project the feed is created inside
#   AZURE_ARTIFACTS_PAT   PAT scoped to Packaging (Read, write, & manage) —
#                         broader than the Read & write scope end users need,
#                         since this also creates the feed itself
#
# Writes feed-name / feed-id / npm-registry / pypi-upload / pypi-index to
# $GITHUB_OUTPUT (falls back to stdout as KEY=VALUE for local/manual runs).
set -euo pipefail

: "${AZURE_DEVOPS_ORG:?AZURE_DEVOPS_ORG is required}"
: "${AZURE_DEVOPS_PROJECT:?AZURE_DEVOPS_PROJECT is required}"
: "${AZURE_ARTIFACTS_PAT:?AZURE_ARTIFACTS_PAT is required}"

API_BASE="https://feeds.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_apis/packaging/feeds"
FEED_NAME="${FEED_NAME:-lm-e2e-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-$$}}"

# Prints the response body on 2xx; on failure, prints "HTTP <status>: <body>"
# to stderr and returns non-zero — unlike `curl -f`, which discards the body
# and leaves no way to tell an auth/scope problem from a bad org/project name.
az_curl() {
  local resp status body
  resp="$(curl -s -w '\n%{http_code}' -u "echo:$AZURE_ARTIFACTS_PAT" "$@")"
  status="${resp##*$'\n'}"
  body="${resp%$'\n'"$status"}"
  if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    echo "Azure API request failed (HTTP $status): $body" >&2
    return 1
  fi
  printf '%s' "$body"
}

echo "== creating feed $FEED_NAME in $AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT ==" >&2
CREATE_RESP="$(az_curl -X POST "$API_BASE?api-version=7.1" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$FEED_NAME\"}")"
FEED_ID="$(echo "$CREATE_RESP" | jq -r '.id // empty')"
[ -n "$FEED_ID" ] || { echo "feed creation returned no id: $CREATE_RESP" >&2; exit 1; }
echo "✅ feed created (id $FEED_ID)" >&2

NPM_REGISTRY="https://pkgs.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_packaging/$FEED_NAME/npm/registry/"
PYPI_UPLOAD="https://pkgs.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_packaging/$FEED_NAME/pypi/upload/"
PYPI_INDEX="https://pkgs.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_packaging/$FEED_NAME/pypi/simple/"

echo "== waiting for feed to be queryable ==" >&2
LAST_ERR=""
for i in $(seq 1 30); do
  if LAST_ERR="$(az_curl "$API_BASE/$FEED_ID?api-version=7.1" 2>&1 >/dev/null)"; then
    break
  fi
  if [ "$i" = 30 ]; then
    echo "timed out waiting for feed $FEED_NAME to become queryable; last error: $LAST_ERR" >&2
    exit 1
  fi
  sleep 2
done
echo "✅ feed is queryable" >&2

OUT="${GITHUB_OUTPUT:-/dev/stdout}"
{
  echo "feed-name=$FEED_NAME"
  echo "feed-id=$FEED_ID"
  echo "npm-registry=$NPM_REGISTRY"
  echo "pypi-upload=$PYPI_UPLOAD"
  echo "pypi-index=$PYPI_INDEX"
} >> "$OUT"
