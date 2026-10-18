#!/usr/bin/env bash
# Create or update the GitHub pull_request webhook that points a repo at the public Decision Ledger.
# Usage: scripts/setup_webhook.sh <owner/repo>
# The secret is read from Secret Manager (dl-webhook-secret) and passed to gh on stdin:
# it is never printed, never written to disk and never placed on a command line.
set -euo pipefail

REPO="${1:?usage: $0 <owner/repo>}"
PROJECT="${PROJECT:-decision-ledger-cup}"
BASE_URL="${BASE_URL:-https://decision-ledger-cup.web.app}"
# Must match the webhook route in src/decision_ledger/api/routes.py.
WEBHOOK_PATH="${WEBHOOK_PATH:-/webhook/github}"
HOOK_URL="${BASE_URL%/}${WEBHOOK_PATH}"

command -v gh >/dev/null || { echo "gh CLI is required" >&2; exit 2; }
command -v gcloud >/dev/null || { echo "gcloud is required" >&2; exit 2; }

DL_SECRET="$(gcloud secrets versions access latest --secret dl-webhook-secret --project "$PROJECT")"
export DL_SECRET HOOK_URL

payload() {
  python3 -c 'import json, os
print(json.dumps({"name": "web", "active": True, "events": ["pull_request"],
  "config": {"url": os.environ["HOOK_URL"], "content_type": "json", "insecure_ssl": "0",
             "secret": os.environ["DL_SECRET"]}}))'
}

HOOK_ID="$(gh api "repos/${REPO}/hooks" --paginate --jq ".[] | select(.config.url == \"${HOOK_URL}\") | .id" | head -1)"
if [[ -n "$HOOK_ID" ]]; then
  payload | gh api -X PATCH "repos/${REPO}/hooks/${HOOK_ID}" --input - --jq '"updated hook \(.id) -> \(.config.url) events=\(.events | join(","))"'
else
  payload | gh api -X POST "repos/${REPO}/hooks" --input - --jq '"created hook \(.id) -> \(.config.url) events=\(.events | join(","))"'
fi
unset DL_SECRET
