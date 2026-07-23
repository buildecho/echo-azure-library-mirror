#!/usr/bin/env bash
#
# Deletes the feed created by azure-e2e-setup.sh. Meant to run with
# `if: always()` so the feed is cleaned up whether the test passed or failed.
#
# Requires: curl. Env:
#   AZURE_DEVOPS_ORG      Azure DevOps organization name
#   AZURE_DEVOPS_PROJECT  Azure DevOps project the feed was created inside
#   AZURE_ARTIFACTS_PAT   Same PAT used to create the feed
#   FEED_ID               id output by azure-e2e-setup.sh
set -euo pipefail

: "${AZURE_DEVOPS_ORG:?AZURE_DEVOPS_ORG is required}"
: "${AZURE_DEVOPS_PROJECT:?AZURE_DEVOPS_PROJECT is required}"
: "${AZURE_ARTIFACTS_PAT:?AZURE_ARTIFACTS_PAT is required}"

if [ -z "${FEED_ID:-}" ]; then
  echo "no FEED_ID set; nothing to delete (setup likely failed before creating one)"
  exit 0
fi

API_BASE="https://feeds.dev.azure.com/$AZURE_DEVOPS_ORG/$AZURE_DEVOPS_PROJECT/_apis/packaging/feeds"
echo "== deleting feed $FEED_ID =="
curl -sf -u "echo:$AZURE_ARTIFACTS_PAT" -X DELETE "$API_BASE/$FEED_ID?api-version=7.1" >/dev/null
echo "✅ deleted feed $FEED_ID"
