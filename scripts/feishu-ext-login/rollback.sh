#!/bin/bash
# carher-ext-login 回滚。幂等,可重复跑。写之前先回读。
#   ./rollback.sh list            列出当前状态(只读)
#   ./rollback.sh L1              摘掉 guest(ext-login 名单 + DM 准入名单)⇒ 功能对他立即失效
#   ./rollback.sh L2              L1 + 停用插件(插件目录改名 .disabled,下次 gateway 起来就不加载)
#   ./rollback.sh L3              L2 + 删插件目录、名单、密钥、审计(回到 11-04 之前的形状)
#   ./rollback.sh enable          把 .disabled 改回来(L2 的反操作)
#   ./rollback.sh portal-pilot    把门户 PILOT_UNION_IDS 里的 guest union_id 摘掉(198 侧)
# 注意:L2/L3 需要 gateway 重启后才真正卸载内存里的钩子;L1 是立即生效的(每条消息现读名单文件)。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GUEST=ou_050cf464b2a27d6da3bbd8a9d2eafaa4
GUEST_UNION=on_8ee47ff881fb5ab35289f5ca90c2dd2c
MODE="${1:-list}"

pod() { "$HERE/podsh.sh" "$1" "${2:-240}"; }

case "$MODE" in
list)
  cat > /tmp/_rb_list.sh <<'EOF'
D=/data/.openclaw/carher-ext-login
echo "== ext-login allow =="; cat $D/allow.json 2>&1
echo "== secret =="; [ -f $D/internal-secret ] && echo "present ($(stat -c%s $D/internal-secret) bytes, $(stat -c%a $D/internal-secret))" || echo absent
echo "== DM store =="; cat /data/.openclaw/credentials/feishu-allowFrom.json 2>&1; echo
echo "== DM store backups =="; ls -1 /data/.openclaw/credentials/feishu-allowFrom.json.bak.* 2>/dev/null || echo none
echo "== plugin dir =="; ls -d /data/.openclaw/extensions/carher-ext-login* 2>/dev/null || echo absent
echo "== audit lines =="; wc -l $D/audit.jsonl 2>/dev/null || echo 0
EOF
  pod /tmp/_rb_list.sh; rm -f /tmp/_rb_list.sh ;;

L1)
  cat > /tmp/_rb_l1.sh <<EOF
set -e
node -e '
const fs=require("fs"), g=process.argv[1];
for (const [P,key] of [["/data/.openclaw/carher-ext-login/allow.json","allow"],
                       ["/data/.openclaw/credentials/feishu-allowFrom.json","allowFrom"]]) {
  let o; try { o=JSON.parse(fs.readFileSync(P,"utf8")); } catch { console.log("SKIP(unreadable):",P); continue; }
  const cur=Array.isArray(o[key])?o[key]:[];
  const next=cur.filter(x=>x!==g);
  if(next.length===cur.length){ console.log("NOT_PRESENT, no write:",P); continue; }
  o[key]=next; fs.writeFileSync(P, JSON.stringify(o));
  console.log("REMOVED from",P,"=>",JSON.stringify(next));
}
' "$GUEST"
echo "--- readback ---"
cat /data/.openclaw/carher-ext-login/allow.json 2>&1; echo
cat /data/.openclaw/credentials/feishu-allowFrom.json; echo
EOF
  pod /tmp/_rb_l1.sh; rm -f /tmp/_rb_l1.sh ;;

L2)
  "$0" L1
  cat > /tmp/_rb_l2.sh <<'EOF'
set -e
S=/data/.openclaw/extensions/carher-ext-login
if [ -d "$S" ]; then mv "$S" "$S.disabled"; echo "DISABLED -> $S.disabled"; else echo "NOT_PRESENT: $S"; fi
ls -d /data/.openclaw/extensions/carher-ext-login* 2>/dev/null || echo "none left"
EOF
  pod /tmp/_rb_l2.sh; rm -f /tmp/_rb_l2.sh ;;

enable)
  cat > /tmp/_rb_en.sh <<'EOF'
set -e
S=/data/.openclaw/extensions/carher-ext-login
if [ -d "$S.disabled" ]; then mv "$S.disabled" "$S"; echo "ENABLED -> $S"; else echo "NO_DISABLED_DIR"; fi
ls -d /data/.openclaw/extensions/carher-ext-login* 2>/dev/null
EOF
  pod /tmp/_rb_en.sh; rm -f /tmp/_rb_en.sh ;;

L3)
  "$0" L2
  cat > /tmp/_rb_l3.sh <<'EOF'
set -e
rm -rf /data/.openclaw/extensions/carher-ext-login /data/.openclaw/extensions/carher-ext-login.disabled
rm -rf /data/.openclaw/carher-ext-login
echo "PURGED plugin dir + state dir"
B=$(ls -1t /data/.openclaw/credentials/feishu-allowFrom.json.bak.* 2>/dev/null | head -1)
if [ -n "$B" ]; then cp "$B" /data/.openclaw/credentials/feishu-allowFrom.json; echo "RESTORED DM store from $B"; fi
cat /data/.openclaw/credentials/feishu-allowFrom.json; echo
ls -d /data/.openclaw/extensions/carher-ext-login* /data/.openclaw/carher-ext-login 2>/dev/null || echo "none left"
EOF
  pod /tmp/_rb_l3.sh; rm -f /tmp/_rb_l3.sh ;;

portal-pilot)
  cd /Users/Liuguoxian/codes/carher-admin/.claude/skills/owui-ops && . ./scripts/_198.sh
  r198 "set -e
CUR=\$(sudo -n sed -n 's/^PILOT_UNION_IDS=//p' /etc/carher-ai-portal.env | tr -d '\r\n')
NEW=\$(echo \"\$CUR\" | tr ',' '\n' | grep -v '^${GUEST_UNION}\$' | paste -sd, -)
if [ \"\$CUR\" = \"\$NEW\" ]; then echo 'NOT_PRESENT, no write'; exit 0; fi
sudo -n sed -i \"s|^PILOT_UNION_IDS=.*|PILOT_UNION_IDS=\$NEW|\" /etc/carher-ai-portal.env
sudo -n systemctl restart carher-ai-portal; sleep 2; systemctl is-active carher-ai-portal
sudo -n sed -n 's/^PILOT_UNION_IDS=//p' /etc/carher-ai-portal.env | tr ',' '\n' | sed -E 's/(on_.{6}).*/\1…/'" ;;

*) echo "unknown mode: $MODE"; exit 2 ;;
esac
