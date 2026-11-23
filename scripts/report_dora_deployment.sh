#!/usr/bin/env bash
# Register one Pages deploy with Datadog DORA (RC1-682). Called by pages.yml
# after the live site passes its health gate, so an event means the site serves.
# Timestamps are nanoseconds. A failed POST fails the job on purpose: the site
# is live either way, but an unrecorded deploy silently undercounts.
#
# Usage: report_dora_deployment.sh <service> <started_at_epoch_seconds> [commit_sha]
# The commit defaults to GITHUB_SHA.
# Env:   DD_API_KEY, GITHUB_SERVER_URL, GITHUB_REPOSITORY
set -euo pipefail

service="$1"
started_at="$2"
commit_sha="${3:-$GITHUB_SHA}"
repo_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"

payload=$(printf '{"data":{"attributes":{"service":"%s","env":"prod","started_at":%s000000000,"finished_at":%s000000000,"git":{"commit_sha":"%s","repository_url":"%s"}}}}' \
  "$service" "$started_at" "$(date +%s)" "$commit_sha" "$repo_url")
code=$(curl -s -o dora-response.json -w '%{http_code}' -m 30 -X POST \
  -H "DD-API-KEY: $DD_API_KEY" -H "Content-Type: application/json" \
  -d "$payload" "https://api.datadoghq.com/api/v2/dora/deployment")
cat dora-response.json; echo
case "$code" in
  2*) echo "DORA deployment recorded for $service at $commit_sha (HTTP $code)";;
  *) echo "::error::DORA deployment POST for $service failed with HTTP $code — this release is out but unrecorded"; exit 1;;
esac
