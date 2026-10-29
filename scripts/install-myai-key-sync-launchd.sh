#!/usr/bin/env bash
# Install an hourly launchd job that keeps 243 myai's LiteLLM key in step with 198's live master key.
# 243 password is read from the login keychain (service "carher-host243", account "cltx"), never written to disk.
#   security add-generic-password -U -s carher-host243 -a cltx -w '<password>'   # one-time, done by the installer if HOST243_PW is set
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
LABEL="com.carher.myai-key-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$REPO_DIR/.tmp/myai-key-sync"
RUNNER="$LOG_DIR/run.sh"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
if [ -n "${HOST243_PW:-}" ]; then
  security add-generic-password -U -s carher-host243 -a cltx -w "$HOST243_PW"
fi
security find-generic-password -s carher-host243 -a cltx -w >/dev/null || {
  echo "keychain 里没有 carher-host243：先 HOST243_PW='...' $0" >&2; exit 2; }

cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
HOST243_PW=\$(security find-generic-password -s carher-host243 -a cltx -w) || exit 2
export HOST243_PW
echo "== \$(date -u +%FT%TZ)"
"$SCRIPT_DIR/myai-198-key-sync.sh" apply
EOF
chmod 700 "$RUNNER"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$RUNNER</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.log</string>
  <key>ThrottleInterval</key><integer>60</integer>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "installed: $PLIST"
echo "log: $LOG_DIR/launchd.log"
