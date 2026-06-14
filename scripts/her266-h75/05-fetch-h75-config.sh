#!/usr/bin/env bash
set -euo pipefail

JMS="${JMS:-./scripts/jms}"
S3_ASSET="${S3_ASSET:-JSZX-AI-03}"
BUILD_NODE="${BUILD_NODE:-k8s-work-227}"
REMOTE_ROOT="${REMOTE_ROOT:-/root/her266-h75-rollout}"
H75_CONFIG_ID="${H75_CONFIG_ID:-_cicd-pinned-config-57197a0}"
REMOTE_CONFIG_DIR="$REMOTE_ROOT/h75-config"
LOCAL_DIR="${LOCAL_DIR:-/tmp/her266-h75-config}"

mkdir -p "$LOCAL_DIR"
"$JMS" scp "$S3_ASSET:/Data/carher-runtime/deploy/$H75_CONFIG_ID/base.json5" "$LOCAL_DIR/base.json5"
"$JMS" scp "$S3_ASSET:/Data/carher-runtime/deploy/$H75_CONFIG_ID/docker.json5" "$LOCAL_DIR/docker.json5"
"$JMS" ssh "$BUILD_NODE" "mkdir -p '$REMOTE_CONFIG_DIR'"
"$JMS" scp "$LOCAL_DIR/base.json5" "$BUILD_NODE:$REMOTE_CONFIG_DIR/base.json5"
"$JMS" scp "$LOCAL_DIR/docker.json5" "$BUILD_NODE:$REMOTE_CONFIG_DIR/docker.json5"

echo "copied H75 config $H75_CONFIG_ID to $BUILD_NODE:$REMOTE_CONFIG_DIR"
