#!/usr/bin/env bash
# Manage a local Feishu -> Codex bridge on macOS/Linux.
# Secrets stay in lark-channel-bridge's encrypted keystore; this script never
# accepts or prints an App Secret.

set -euo pipefail

SCRIPT_NAME="local-feishu-codex-bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
USER_HOME="${HOME:?HOME is required}"
PROFILE="${LARK_CHANNEL_PROFILE:-codex}"
WORKSPACE="${LARK_CHANNEL_WORKSPACE:-$(pwd)}"
APP_ID="${LARK_CHANNEL_APP_ID:-}"
BRIDGE_VERSION="${LARK_CHANNEL_BRIDGE_VERSION:-0.7.1}"
LARK_HOME="${LARK_CHANNEL_HOME:-$USER_HOME/.lark-channel}"
SKIP_LARK_CHECK="${LARK_CHANNEL_SKIP_CHECK:-0}"

usage() {
  cat <<'EOF'
Usage:
  scripts/local-feishu-codex-bridge.sh install [--version VERSION]
  scripts/local-feishu-codex-bridge.sh bootstrap --app-id cli_xxx [--profile codex] [--workspace PATH]
  scripts/local-feishu-codex-bridge.sh start|stop|restart|status|ps|logs|doctor
  scripts/local-feishu-codex-bridge.sh recent [--limit N] [--cwd PATH] [--search TEXT]
  scripts/local-feishu-codex-bridge.sh allow-user ou_xxx
  scripts/local-feishu-codex-bridge.sh allow-chat oc_xxx

Environment overrides:
  LARK_CHANNEL_PROFILE        Profile name (default: codex)
  LARK_CHANNEL_WORKSPACE      Codex workspace (default: current directory)
  LARK_CHANNEL_APP_ID         Existing Feishu app ID
  LARK_CHANNEL_BRIDGE_VERSION Bridge version (default: 0.7.1)
  LARK_CHANNEL_HOME           Bridge state directory (default: ~/.lark-channel)
  LARK_CHANNEL_SKIP_CHECK=1   Skip lark-cli pre-flight during bootstrap

bootstrap is interactive once: it starts the bridge in the foreground so the
App Secret can be entered without appearing in shell history. Press Ctrl-C
after the bot reports connected; the script then installs the launchd/systemd
service and starts it in the background.
EOF
}

die() {
  printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2
  exit 1
}

info() {
  printf '[%s] %s\n' "$SCRIPT_NAME" "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

bridge_bin() {
  command -v lark-channel-bridge 2>/dev/null || true
}

require_bridge() {
  local bin
  bin="$(bridge_bin)"
  [[ -n "$bin" ]] || die "lark-channel-bridge is not installed; run: $0 install"
  printf '%s\n' "$bin"
}

validate_workspace() {
  [[ -d "$WORKSPACE" ]] || die "workspace is not a directory: $WORKSPACE"
  WORKSPACE="$(cd "$WORKSPACE" && pwd -P)"
}

validate_app_id() {
  [[ "$APP_ID" =~ ^cli_[A-Za-z0-9]+$ ]] || die "--app-id must look like cli_xxx"
}

profile_exists() {
  [[ -f "$LARK_HOME/config.json" ]] || return 1
  node - "$LARK_HOME/config.json" "$PROFILE" <<'NODE'
const fs = require("fs");
const [configPath, profile] = process.argv.slice(2);
try {
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  process.exit(config.profiles && config.profiles[profile] ? 0 : 1);
} catch (_) {
  process.exit(1);
}
NODE
}

install_bridge() {
  require_command npm
  info "installing lark-channel-bridge@${BRIDGE_VERSION} globally"
  npm install --global "lark-channel-bridge@${BRIDGE_VERSION}"
}

bootstrap_bridge() {
  require_command node
  validate_app_id
  validate_workspace
  local bin
  bin="$(require_bridge)"
  local args=(run --profile "$PROFILE" --agent codex --app-id "$APP_ID" --workspace "$WORKSPACE")
  [[ "$SKIP_LARK_CHECK" == "1" ]] && args+=(--skip-check-lark-cli)

  info "starting interactive bootstrap for profile=${PROFILE}, workspace=${WORKSPACE}"
  info "enter the App Secret only when the bridge prompts; it is stored in the encrypted keystore"
  set +e
  "$bin" "${args[@]}"
  local rc=$?
  set -e
  if [[ "$rc" != "0" && "$rc" != "130" && "$rc" != "143" ]]; then
    die "bootstrap exited with code ${rc}; service was not installed"
  fi

  info "installing and starting the OS-managed service"
  "$bin" start --profile "$PROFILE"
}

set_access_entry() {
  local kind="$1"
  local value="$2"
  [[ -n "$value" ]] || die "empty access entry"
  [[ -f "$LARK_HOME/config.json" ]] || die "missing $LARK_HOME/config.json; run bootstrap first"

  node - "$LARK_HOME/config.json" "$PROFILE" "$kind" "$value" <<'NODE'
const fs = require("fs");
const [configPath, profileName, kind, value] = process.argv.slice(2);
const key = kind === "user" ? "allowedUsers" : "allowedChats";
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
const profile = config.profiles && config.profiles[profileName];
if (!profile) throw new Error(`profile not found: ${profileName}`);
profile.access ||= {};
const entries = Array.isArray(profile.access[key]) ? profile.access[key] : [];
if (!entries.includes(value)) entries.push(value);
profile.access[key] = entries;
const tempPath = `${configPath}.tmp.${process.pid}`;
fs.writeFileSync(tempPath, `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 });
fs.chmodSync(tempPath, 0o600);
fs.renameSync(tempPath, configPath);
console.log(`allow-${kind} updated for profile ${profileName}`);
NODE

  info "restarting the profile to apply the access list"
  local bin
  bin="$(require_bridge)"
  if "$bin" status --profile "$PROFILE" >/dev/null 2>&1; then
    "$bin" restart --profile "$PROFILE"
  fi
}

show_logs() {
  local log_dir="$LARK_HOME/profiles/$PROFILE/logs"
  [[ -d "$log_dir" ]] || die "log directory not found: $log_dir"
  local latest
  latest="$(find "$log_dir" -maxdepth 1 -type f -name 'bridge-*.jsonl' -print | sort | tail -1)"
  [[ -n "$latest" ]] || die "no bridge log found in $log_dir"
  tail -80 "$latest"
}

doctor() {
  local bin
  bin="$(require_bridge)"
  require_command node
  require_command npm
  require_command codex
  info "bridge=$("$bin" --version 2>/dev/null || true)"
  info "node=$(node --version)"
  info "npm=$(npm --version)"
  info "profile=${PROFILE} workspace=${WORKSPACE} state=${LARK_HOME}"
  if profile_exists; then
    node - "$LARK_HOME/config.json" "$PROFILE" <<'NODE'
const fs = require("fs");
const [configPath, profileName] = process.argv.slice(2);
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
const profile = config.profiles[profileName];
const appId = profile.accounts?.app?.id || "";
const maskedApp = appId ? `${appId.slice(0, 8)}...` : "missing";
const users = profile.access?.allowedUsers || [];
const chats = profile.access?.allowedChats || [];
console.log(`profile-agent=${profile.agentKind || "unknown"}`);
console.log(`app-id=${maskedApp}`);
console.log(`allowed-users=${users.length} allowed-chats=${chats.length}`);
console.log(`workspace=${profile.workspaces?.default || "missing"}`);
NODE
  else
    info "profile is not initialized; run bootstrap"
  fi
  "$bin" status --profile "$PROFILE" || true
  if [[ -d "$LARK_HOME/profiles/$PROFILE/logs" ]]; then
    info "recent bridge signals:"
    show_logs | grep -E '"event":"(connected|profile-online|enter|skip-not-allowed-user)"|owner_refresh_failed|runtime error|failed to spawn' | tail -20 || true
  fi
}

COMMAND="${1:-status}"
if [[ "$COMMAND" == "-h" || "$COMMAND" == "--help" ]]; then
  usage
  exit 0
fi
shift || true
while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || die "--profile needs a value"
      PROFILE="$2"; shift 2
      ;;
    --workspace)
      [[ $# -ge 2 ]] || die "--workspace needs a value"
      WORKSPACE="$2"; shift 2
      ;;
    --app-id)
      [[ $# -ge 2 ]] || die "--app-id needs a value"
      APP_ID="$2"; shift 2
      ;;
    --version)
      [[ $# -ge 2 ]] || die "--version needs a value"
      BRIDGE_VERSION="$2"; shift 2
      ;;
    --skip-check-lark-cli)
      SKIP_LARK_CHECK=1; shift
      ;;
    -h|--help)
      usage; exit 0
      ;;
    --)
      shift; break
      ;;
    *)
      break
      ;;
  esac
done

case "$COMMAND" in
  install)
    install_bridge
    ;;
  bootstrap|init)
    bootstrap_bridge
    ;;
  start|stop|restart|status)
    bin="$(require_bridge)"
    "$bin" "$COMMAND" --profile "$PROFILE"
    ;;
  ps)
    bin="$(require_bridge)"
    "$bin" ps
    ;;
  logs)
    show_logs
    ;;
  allow-user)
    value="${1:-}"
    [[ "$value" =~ ^ou_[A-Za-z0-9]+$ ]] || die "usage: $0 allow-user ou_xxx"
    set_access_entry user "$value"
    ;;
  allow-chat)
    value="${1:-}"
    [[ "$value" =~ ^oc_[A-Za-z0-9]+$ ]] || die "usage: $0 allow-chat oc_xxx"
    set_access_entry chat "$value"
    ;;
  doctor)
    doctor
    ;;
  recent|work-items|tasks)
    require_command python3
    exec python3 "$SCRIPT_DIR/codex-recent-work-items.py" "$@"
    ;;
  help)
    usage
    ;;
  *)
    usage >&2
    die "unknown command: $COMMAND"
    ;;
esac
