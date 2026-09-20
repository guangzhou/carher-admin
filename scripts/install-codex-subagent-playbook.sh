#!/usr/bin/env bash
set -euo pipefail

agent_dir="${CODEX_HOME:-$HOME/.codex}/agents"
target="$agent_dir/luna-worker.toml"
mode="check"
force=0

usage() { echo "Usage: $0 [--check|--install|--one-click] [--force]"; }
for arg in "$@"; do
  case "$arg" in
    --check|--install) mode="${arg#--}" ;;
    --one-click) mode="install"; force=1 ;;
    --force) force=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

read -r -d '' desired <<'EOF' || true
name = "luna_worker"
description = "Fast worker for clear, narrowly scoped, and repeatable tasks."
developer_instructions = """
Handle the assigned task strictly within its stated scope.
Work independently and use appropriate tools when needed.
Verify the result when practical.
Do not make unrelated changes.
Return a concise summary containing the result, relevant file paths, verification performed, and any important caveats.
"""
model = "gpt-5.6-luna"
model_reasoning_effort = "max"
sandbox_mode = "workspace-write"
EOF

if [[ -f "$target" ]]; then
  echo "Existing config: $target"
  if cmp -s <(printf '%s\n' "$desired") "$target"; then
    echo "Status: already matches the company worker baseline."
    exit 0
  fi
  echo "Status: drift detected; existing content will not be overwritten automatically."
  if [[ "$mode" != install || "$force" != 1 ]]; then
    echo "Action: rerun with --install --force to replace it (a backup will be created)."
    exit 1
  fi
  backup="$target.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$target" "$backup"
  echo "Backup: $backup"
elif [[ "$mode" == check ]]; then
  echo "Missing config: $target"
  echo "Action: rerun with --install to create it."
  exit 1
fi

if [[ "$mode" == install ]]; then
  mkdir -p "$agent_dir"
  printf '%s\n' "$desired" > "$target"
  chmod 600 "$target"
  python3 - "$target" <<'PY'
import sys, tomllib
from pathlib import Path
p = Path(sys.argv[1])
data = tomllib.loads(p.read_text())
required = {"name": "luna_worker", "model": "gpt-5.6-luna", "model_reasoning_effort": "max"}
missing = [k for k, v in required.items() if data.get(k) != v]
if missing:
    raise SystemExit(f"configuration mismatch: {missing}")
print(f"Validated TOML: {p}")
PY
  echo "Installed: $target"
fi
