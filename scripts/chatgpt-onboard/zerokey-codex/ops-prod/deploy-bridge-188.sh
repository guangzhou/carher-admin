#!/usr/bin/env bash
# deploy-bridge-188.sh — 在 188 把 zerokey-codex-responses-bridge 装成 systemd --user 常驻
#
# 幂等：重复跑 = 覆盖 unit + restart。零参数默认单 upstream 8123；
# 传 UPSTREAMS 覆盖为多桥轮询 failover。
#
# 用法（在 188 本机，或 ssh 进去跑）：
#   bash deploy-bridge-188.sh
#   UPSTREAMS='http://127.0.0.1:8123/v1,http://127.0.0.1:8124/v1' bash deploy-bridge-188.sh
#
# 前置：~/zk-bridge/zerokey-codex-responses-bridge.py 已存在（本脚本会自动 scp 不了，
#       需先把 repo bridge/zerokey-codex-responses-bridge.py 放到 ~/zk-bridge/）
set -euo pipefail

BRIDGE_DIR="${BRIDGE_DIR:-$HOME/zk-bridge}"
BRIDGE_PY="$BRIDGE_DIR/zerokey-codex-responses-bridge.py"
LISTEN="${BRIDGE_LISTEN:-0.0.0.0:8788}"
UPSTREAMS="${UPSTREAMS:-http://127.0.0.1:8123/v1}"
UP_AUTH="${BRIDGE_UPSTREAM_AUTH:-vscode}"
UP_MODEL="${BRIDGE_MODEL:-gpt-5-5}"
UNIT="$HOME/.config/systemd/user/zk-bridge.service"

echo "== preflight =="
if [[ ! -f "$BRIDGE_PY" ]]; then
  echo "FATAL: $BRIDGE_PY 不存在。先把 repo scripts/chatgpt-onboard/zerokey-codex/bridge/zerokey-codex-responses-bridge.py 拷到 $BRIDGE_DIR/" >&2
  exit 1
fi
python3 --version

echo "== write systemd --user unit =="
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=zerokey codex responses bridge
After=network.target

[Service]
Type=simple
Environment=BRIDGE_LISTEN=$LISTEN
Environment=BRIDGE_UPSTREAMS=$UPSTREAMS
Environment=BRIDGE_UPSTREAM_AUTH=$UP_AUTH
Environment=BRIDGE_MODEL=$UP_MODEL
Environment=BRIDGE_LOG=/tmp/zk_bridge.log
ExecStart=/usr/bin/python3 $BRIDGE_PY
Restart=always
RestartSec=5
StandardOutput=append:/tmp/zk_bridge.stdout
StandardError=append:/tmp/zk_bridge.stderr

[Install]
WantedBy=default.target
UNITEOF

echo "== enable-linger + enable + (re)start =="
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable zk-bridge.service
systemctl --user restart zk-bridge.service
sleep 2

echo "== verify =="
systemctl --user is-active zk-bridge.service
ss -tlnp 2>/dev/null | grep 8788 || netstat -tln | grep 8788
echo "--- health ---"
curl -sS --max-time 5 "http://127.0.0.1:${LISTEN##*:}/health"
echo
echo "--- responses smoke (build file) ---"
curl -sS --max-time 60 -X POST "http://127.0.0.1:${LISTEN##*:}/v1/responses" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$UP_MODEL"'","input":[{"role":"user","content":[{"type":"input_text","text":"Create a file at bridge_smoke.txt containing exactly: ok"}]}],"stream":false}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); o=d.get("output",[{}])[0]; print("tool:",o.get("name"),"| cmd contains apply_patch:", "apply_patch" in (o.get("arguments") or ""))'
echo "DONE: bridge active on $LISTEN -> $UPSTREAMS"
