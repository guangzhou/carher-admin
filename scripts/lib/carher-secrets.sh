#!/usr/bin/env bash
# Shell counterpart to scripts/lib/carher_secrets.py — source this, then use
# carher_secret NAME to read a value.
#
#   source "$(dirname "$0")/../../../lib/carher-secrets.sh"
#   K8S_PASS="$(carher_secret SUDO_PW)"          # aborts if missing
#   OPTIONAL="$(carher_secret FOO || true)"      # tolerate missing
#
# Resolution order matches the Python loader: environment, then $CARHER_SECRETS,
# then <repo>/.carher-secrets.json (gitignored), then ~/.config/carher/secrets.json.

carher_secret() {
  local name="$1"
  if [[ -z "$name" ]]; then
    echo "carher_secret: missing argument" >&2
    return 2
  fi
  # 1. environment wins
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  # 2-4. config files, first hit wins
  local _repo_root
  _repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  local f
  for f in "${CARHER_SECRETS:-}" \
           "$_repo_root/.carher-secrets.json" \
           "$HOME/.config/carher/secrets.json"; do
    [[ -n "$f" && -f "$f" ]] || continue
    local v
    v="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception:
    sys.exit(1)
v=d.get(sys.argv[2])
if v: sys.stdout.write(str(v))
' "$f" "$name" 2>/dev/null)" || true
    if [[ -n "$v" ]]; then
      printf '%s' "$v"
      return 0
    fi
  done
  echo "missing secret '$name'." >&2
  echo "  export $name='...'  or  run scripts/lib/carher-secrets-init.sh" >&2
  echo "  (.carher-secrets.json is gitignored and must never be committed.)" >&2
  return 1
}
