#!/bin/bash
# carher-ext-login 安装 / 重装(幂等,可重复跑)。
#
# 🔴 什么时候需要重跑:**pod 被重建之后**(operator reconcile / 节点漂移 / 换镜像)。
#    原因:openclaw.json 挂在 emptyDir(merged-config)上,由 config-reloader 从
#    operator 生成的 ConfigMap 重新合成 ⇒ 我们加的 plugins.entries 那一行会消失,
#    而 gateway 启动时**只加载显式启用的非 bundled 插件**
#    (dist/gateway-startup-plugin-ids-COmsQTCi.js:50)⇒ 插件静默不加载,「登录」不再回链接。
#    插件代码 / 名单 / 密钥在 PVC 上,**不会丢**,只丢这一行 config。
#    容器重启(kubelet 重启 carher 容器)**不需要**重跑:emptyDir 不随容器重启清空(已实测)。
#
# 判据:跑完必须看到 gateway 日志里 `[carher-ext-login] registered N before_dispatch listener(s)`。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GUEST="${EXT_LOGIN_GUEST:-ou_050cf464b2a27d6da3bbd8a9d2eafaa4}"
DO_RESTART="${1:-}"

# 1) 插件文件
TGZ=$(mktemp); tar czf "$TGZ" -C "$HERE/plugin" carher-ext-login
B64=$(base64 < "$TGZ" | tr -d '\n'); rm -f "$TGZ"
cat > /tmp/_apply_files.sh <<EOF
set -e
D=/data/.openclaw/extensions/carher-ext-login
echo '$B64' | base64 -d | tar xzf - -C /data/.openclaw/extensions
chown -R 0:0 "\$D"; chmod -R go-w "\$D"; chmod 755 "\$D"
mkdir -p /data/.openclaw/carher-ext-login; chown 0:0 /data/.openclaw/carher-ext-login; chmod 700 /data/.openclaw/carher-ext-login
echo "FILES_OK sha256=\$(sha256sum "\$D/index.js" | cut -d' ' -f1)"
EOF
"$HERE/podsh.sh" /tmp/_apply_files.sh; rm -f /tmp/_apply_files.sh

# 2) 显式启用(gateway 启动闸)+ 名单 + DM 准入;全部先回读再写
cat > /tmp/_apply_cfg.sh <<EOF
set -e
node -e '
const fs=require("fs"), g=process.argv[1];
const P="/data/.openclaw/openclaw.json";
const o=JSON.parse(fs.readFileSync(P,"utf8"));
o.plugins=o.plugins||{}; o.plugins.entries=o.plugins.entries||{};
if(o.plugins.entries["carher-ext-login"]?.enabled===true){ console.log("ENTRY_ALREADY_PRESENT"); }
else { o.plugins.entries["carher-ext-login"]={enabled:true}; fs.writeFileSync(P, JSON.stringify(o,null,2)); console.log("ENTRY_ADDED"); }
const A="/data/.openclaw/carher-ext-login/allow.json";
let a={allow:[]}; try{ const t=JSON.parse(fs.readFileSync(A,"utf8")); if(Array.isArray(t.allow)) a=t; }catch{}
if(a.allow.includes(g)) console.log("ALLOW_ALREADY_PRESENT");
else { a.allow.push(g); fs.writeFileSync(A, JSON.stringify(a)); console.log("ALLOW_ADDED"); }
const S="/data/.openclaw/credentials/feishu-allowFrom.json";
const s=JSON.parse(fs.readFileSync(S,"utf8")); s.allowFrom=Array.isArray(s.allowFrom)?s.allowFrom:[];
if(s.allowFrom.includes(g)) console.log("DM_ALREADY_PRESENT");
else { s.allowFrom.push(g); fs.writeFileSync(S, JSON.stringify(s)); console.log("DM_ADDED"); }
' "$GUEST"
chmod 600 /data/.openclaw/carher-ext-login/allow.json
echo "--- readback ---"
cat /data/.openclaw/carher-ext-login/allow.json; echo
cat /data/.openclaw/credentials/feishu-allowFrom.json; echo
node -e 'const c=JSON.parse(require("fs").readFileSync("/data/.openclaw/openclaw.json","utf8"));
console.log("entry =", JSON.stringify(c.plugins?.entries?.["carher-ext-login"]??null), "| dmPolicy =", c.channels?.feishu?.dmPolicy)'
# 密钥不在这个脚本里补:它要从 198 的 /etc/carher-ai-portal.env 取 INTERNAL_SECRET,
# 写到 /data/.openclaw/carher-ext-login/internal-secret(0600 root)。缺了就只报警不猜。
[ -f /data/.openclaw/carher-ext-login/internal-secret ] && echo "SECRET present" \
  || echo "🔴 SECRET MISSING — 从 198 的 /etc/carher-ai-portal.env 取 INTERNAL_SECRET 写进 /data/.openclaw/carher-ext-login/internal-secret (chmod 600)"
cd /data/.openclaw && timeout 180 /opt/openclaw/bin/openclaw config validate 2>&1 | head -2
EOF
"$HERE/podsh.sh" /tmp/_apply_cfg.sh; rm -f /tmp/_apply_cfg.sh

if [ "$DO_RESTART" = "--restart" ]; then
  echo "=== 重启 gateway(只重启 her-236 的 carher 容器进程) ==="
  cd /Users/Liuguoxian/codes/carher-admin
  scripts/jms ssh k8s-01 --timeout 240 'P=$(kubectl get pod -n carher -l user-id=236 -o name|head -1); kubectl exec -n carher $P -c carher -- sh -c "pkill -TERM -f \"openclaw/dist/index.js gateway run\"" || true' < /dev/null || true
  echo "等待就绪…"
  for i in 1 2 3 4 5 6 7 8; do
    OUT=$(scripts/jms ssh k8s-01 --timeout 240 'kubectl get pod -n carher -l user-id=236 -o jsonpath="ready={.items[0].status.containerStatuses[0].ready}"; echo; kubectl get herinstance her-236 -n carher -o jsonpath="ws={.status.feishuWS}"; echo' < /dev/null 2>&1 | tail -2)
    echo "[$i] $(echo $OUT)"; echo "$OUT" | grep -q 'ws=Connected' && break
  done
  cat > /tmp/_apply_chk.sh <<'EOF'
L=$(ls -t /tmp/openclaw/openclaw-*.log 2>/dev/null | head -1)
[ -z "$L" ] && { echo "🔴 还没有 live log,再等等"; exit 1; }
grep -o 'carher-ext-login\] registered[^"]\{0,90\}' "$L" | tail -1
grep -o 'engine-swap\] registered[^"]\{0,60\}' "$L" | tail -1   # 阳性对照
EOF
  "$HERE/podsh.sh" /tmp/_apply_chk.sh || true; rm -f /tmp/_apply_chk.sh
else
  echo
  echo "⚠️  文件和 config 已就位,但 gateway 没重启 ⇒ 内存里还没有这个钩子。"
  echo "    要让它生效:$0 --restart"
fi
