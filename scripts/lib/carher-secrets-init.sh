#!/usr/bin/env bash
# Create the local, gitignored secrets file used by the ops scripts.
#
# Usage:
#   scripts/lib/carher-secrets-init.sh            # interactive prompts
#   SUDO_PW='...' scripts/lib/carher-secrets-init.sh --from-env
#
# Writes .carher-secrets.json at the repo root with mode 600. That path is in
# .gitignore; never commit it, and never paste these values into a chat or a log.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO_ROOT/.carher-secrets.json"

# Refuse to run if the path is somehow tracked -- that would mean .gitignore was
# edited and the next commit would leak the file.
if git -C "$REPO_ROOT" ls-files --error-unmatch .carher-secrets.json >/dev/null 2>&1; then
  echo "REFUSING: .carher-secrets.json is TRACKED by git." >&2
  echo "  Run: git rm --cached .carher-secrets.json" >&2
  echo "  and confirm .gitignore lists it, then re-run this script." >&2
  exit 1
fi

if [[ -f "$OUT" ]]; then
  echo "$OUT already exists; not overwriting."
  echo "Edit it by hand, or delete it and re-run."
  exit 0
fi

read_secret() {  # name, prompt
  local __name="$1" __prompt="$2" __val=""
  if [[ "${2:-}" == "" ]]; then return 1; fi
  if [[ "${FROM_ENV:-0}" == "1" ]]; then
    __val="${!__name:-}"
  else
    # -s so the value never appears on screen or in scrollback
    read -r -s -p "$__prompt: " __val
    echo
  fi
  printf '%s' "$__val"
}

FROM_ENV=0
[[ "${1:-}" == "--from-env" ]] && FROM_ENV=1

SUDO_PW="$(read_secret SUDO_PW 'sudo password for 10.68.13.198 / .225')"
if [[ -z "$SUDO_PW" ]]; then
  echo "SUDO_PW is required." >&2
  exit 1
fi

# Optional extras; blank is fine.
LITELLM_MASTER_KEY="$(read_secret LITELLM_MASTER_KEY 'LiteLLM master key (optional, blank to skip)' || true)"
ZK_KEY="$(read_secret ZK_KEY 'LiteLLM virtual key for test harnesses (optional, blank to skip)' || true)"

umask 077
python3 - "$OUT" "$SUDO_PW" "$LITELLM_MASTER_KEY" "$ZK_KEY" <<'PY'
import json, sys
out, sudo_pw, mk, zk = sys.argv[1:5]
d = {"SUDO_PW": sudo_pw}
if mk: d["LITELLM_MASTER_KEY"] = mk
if zk: d["ZK_KEY"] = zk
with open(out, "w", encoding="utf-8") as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
chmod 600 "$OUT"

echo "wrote $OUT (mode $(stat -f '%Lp' "$OUT" 2>/dev/null || stat -c '%a' "$OUT"))"
echo "verify with: python3 scripts/lib/carher_secrets.py"
