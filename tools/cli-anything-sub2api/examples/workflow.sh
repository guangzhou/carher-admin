#!/usr/bin/env bash
set -euo pipefail

# Inspect, update, and verify an upstream account through cli-anything-sub2api.
# Credentials are read from the environment: SUB2API_BASE_URL and one of
# SUB2API_ADMIN_API_KEY or SUB2API_JWT.

ACCOUNT_ID="${1:?usage: workflow.sh <account-id>}"

cli-anything-sub2api --json status
cli-anything-sub2api --json accounts get "$ACCOUNT_ID"
cli-anything-sub2api --dry-run accounts update "$ACCOUNT_ID" --data '{"concurrency":10}'
cli-anything-sub2api accounts update "$ACCOUNT_ID" --data '{"concurrency":10}'
cli-anything-sub2api --json accounts get "$ACCOUNT_ID"
