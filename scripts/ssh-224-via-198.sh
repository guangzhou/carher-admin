#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JMS="${JMS:-$ROOT_DIR/scripts/jms}"
JUMP_ASSET="${AIYJY_198_ASSET:-AIYJY-litellm}"
TARGET_HOST="${AIYJY_224_HOST:-10.68.13.224}"
TARGET_USER="${AIYJY_224_USER:-cltx}"
DEFAULT_SSH_PORT="${AIYJY_224_LOCAL_SSH_PORT:-2224}"
DEFAULT_VNC_PORT="${AIYJY_224_LOCAL_VNC_PORT:-5901}"

PROXY_PID=""
PROXY_LOG=""

usage() {
  cat <<'EOF'
Usage:
  scripts/ssh-224-via-198.sh ssh
  scripts/ssh-224-via-198.sh cmd '<remote command>'
  scripts/ssh-224-via-198.sh check
  scripts/ssh-224-via-198.sh vnc

What it does:
  Local machine -> JumpServer asset AIYJY-litellm (198) -> 10.68.13.224:22

No password is stored. SSH will prompt for the 224 user's password.

Environment overrides:
  AIYJY_198_ASSET=AIYJY-litellm
  AIYJY_224_HOST=10.68.13.224
  AIYJY_224_USER=cltx
  AIYJY_224_LOCAL_SSH_PORT=2224
  AIYJY_224_LOCAL_VNC_PORT=5901
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing command: $1" >&2
    exit 127
  }
}

is_port_open() {
  nc -z -w 1 127.0.0.1 "$1" >/dev/null 2>&1
}

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

choose_port() {
  local wanted="$1"
  if is_port_open "$wanted"; then
    free_port
  else
    echo "$wanted"
  fi
}

cleanup() {
  if [[ -n "${PROXY_PID:-}" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null || true
  fi
  if [[ -n "${PROXY_LOG:-}" && -f "$PROXY_LOG" ]]; then
    rm -f "$PROXY_LOG"
  fi
}
trap cleanup EXIT INT TERM

wait_for_port() {
  local port="$1"
  local i
  for i in $(seq 1 80); do
    if is_port_open "$port"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

start_proxy() {
  SSH_PROXY_PORT="$(choose_port "$DEFAULT_SSH_PORT")"
  PROXY_LOG="$(mktemp -t ssh-224-via-198.XXXXXX.log)"

  "$JMS" proxy "$JUMP_ASSET" "$SSH_PROXY_PORT" "$TARGET_HOST" 22 \
    >"$PROXY_LOG" 2>&1 &
  PROXY_PID="$!"

  if ! wait_for_port "$SSH_PROXY_PORT"; then
    echo "failed to open local proxy port $SSH_PROXY_PORT" >&2
    echo "--- proxy log ---" >&2
    cat "$PROXY_LOG" >&2 || true
    exit 1
  fi
}

run_ssh() {
  local -a extra remote cmd
  extra=()
  remote=()
  while [[ "$#" -gt 0 ]]; do
    if [[ "$1" == "--" ]]; then
      shift
      remote=("$@")
      break
    fi
    extra+=("$1")
    shift
  done
  cmd=(
    ssh
    -p "$SSH_PROXY_PORT"
    -o "HostKeyAlias=$TARGET_HOST-via-$JUMP_ASSET"
    -o StrictHostKeyChecking=accept-new
    -o ServerAliveInterval=30
    -o ExitOnForwardFailure=yes
  )
  if [[ "${#extra[@]}" -gt 0 ]]; then
    cmd+=("${extra[@]}")
  fi
  cmd+=("$TARGET_USER@127.0.0.1")
  if [[ "${#remote[@]}" -gt 0 ]]; then
    cmd+=("${remote[@]}")
  fi
  "${cmd[@]}"
}

cmd="${1:-}"
case "$cmd" in
  ssh)
    shift
    need_cmd nc
    need_cmd python3
    start_proxy
    echo "Connecting to $TARGET_USER@$TARGET_HOST via $JUMP_ASSET (local port $SSH_PROXY_PORT)..." >&2
    run_ssh -- "$@"
    ;;

  cmd)
    shift
    if [[ "$#" -eq 0 ]]; then
      echo "cmd requires a remote command" >&2
      usage >&2
      exit 2
    fi
    need_cmd nc
    need_cmd python3
    start_proxy
    run_ssh -- "$@"
    ;;

  check)
    shift
    need_cmd nc
    need_cmd python3
    start_proxy
    echo "proxy_ok local=127.0.0.1:$SSH_PROXY_PORT target=$JUMP_ASSET->$TARGET_HOST:22"
    if run_ssh -o BatchMode=yes -o ConnectTimeout=5 -- 'hostname; whoami' >/tmp/ssh-224-via-198.check.$$ 2>&1; then
      cat /tmp/ssh-224-via-198.check.$$
      rm -f /tmp/ssh-224-via-198.check.$$
      echo "ssh_login_ok"
    else
      out="$(cat /tmp/ssh-224-via-198.check.$$ 2>/dev/null || true)"
      rm -f /tmp/ssh-224-via-198.check.$$
      if [[ "$out" == *"Permission denied"* ]]; then
        echo "ssh_port_ok_password_required"
      else
        echo "$out" >&2
        exit 1
      fi
    fi
    ;;

  vnc)
    shift
    need_cmd nc
    need_cmd python3
    start_proxy
    VNC_LOCAL_PORT="$(choose_port "$DEFAULT_VNC_PORT")"
    local_cmd=()
    if command -v open >/dev/null 2>&1; then
      local_cmd=(-o PermitLocalCommand=yes -o "LocalCommand=open vnc://127.0.0.1:$VNC_LOCAL_PORT")
    fi
    echo "Opening VNC tunnel: vnc://127.0.0.1:$VNC_LOCAL_PORT" >&2
    echo "Keep this command running; Ctrl-C closes the VNC path." >&2
    run_ssh -N -L "$VNC_LOCAL_PORT:127.0.0.1:5901" "${local_cmd[@]}" --
    ;;

  ""|-h|--help|help)
    usage
    ;;

  *)
    echo "unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
