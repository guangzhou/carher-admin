#!/usr/bin/env bash
# AI 自助门户部署到 198:https://chat.auto-link.com.cn/ai/   (照 scripts/feedback/deploy_198.sh 的套路,幂等可重跑)
#
#   后端     /opt/carher-ai-portal/ai-portal/{server.py,static/index.html}
#            /opt/carher-ai-portal/feedback/feedback_server.py   ← server.py 按 HERE.parent/feedback/ 加载它,
#            所以目录形状必须和仓库一致(不能用 /opt/carher-feedback/ 那份:它是旧版,没有 build_fields)
#            systemd carher-ai-portal.service,监听 127.0.0.1:8793
#   安装器   /var/www/ai-installer/{install.sh,install.ps1,pkg/*}
#            nginx:/ai/i/{cursor,codex,claude}.sh → install.sh;.ps1 → install.ps1;/ai/i/pkg/ → pkg/
#   数据     /var/lib/carher-ai-portal/ai_portal.db
#   配置     /etc/carher-ai-portal.env (root 600)
#            FEISHU_APP_SECRET 每次从 k8s open-webui Deployment 现读(先核对 FEISHU_CLIENT_ID=carher-236)
#            SESSION_SECRET / INTERNAL_SECRET 只在 198 上生成;文件里已有就沿用(换了会把所有人踢下线)
#            PILOT_UNION_IDS = 灰度名单(默认只有刘国现);PILOT_UNION_IDS= 置空跑一遍 = 对所有人开放
#   nginx    chat.auto-link.com.cn.conf 里 `# >>> carher-ai-portal >>>` 一段(只加一次),备份 /etc/nginx/backup-ai-portal/
#   回滚     删 nginx 那一段 → nginx -t → reload;systemctl disable --now carher-ai-portal
#
# 用法: bash scripts/ai-portal/deploy_198.sh            (先本地测试 + 重建安装器产物,再推)
#       SKIP_BUILD=1 bash scripts/ai-portal/deploy_198.sh   (产物已是最新时跳过重建)
# 🔴 不跑 release_all.sh / publish_198.sh,不碰 /oneclick/ 和飞书文档。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PILOT="${PILOT_UNION_IDS-on_0577e767b8995740ebbf83f06d9f8bd1}"   # 刘国现(carher-236 开发商下的 union_id)
case "$PILOT" in *[!A-Za-z0-9_,]*) echo "PILOT_UNION_IDS 只能是 on_xxx,on_yyy" >&2; exit 1 ;; esac

# 0) 本地门禁:测试 + e2e 不过就停
python3 -m pytest scripts/ai-portal scripts/feedback scripts/ai-portal/installer -q -p no:cacheprovider
python3 .work/ai-portal/e2e/e2e.py

# 1) 安装器产物(要带「从 env CARHER_KEY 读 key」)
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  bash codex-oneclick/build.sh
  bash claude-oneclick/build.sh
  sh scripts/zk-cursor-web/package_team_setup.sh
fi
PKGS=(codex-oneclick/codex-mac.command codex-oneclick/codex-windows.bat
      claude-oneclick/claude-mac.command claude-oneclick/claude-windows.bat
      scripts/zk-cursor-web/cursor-mac.zip scripts/zk-cursor-web/cursor-windows.zip)
for f in "${PKGS[@]:0:4}"; do grep -q CARHER_KEY "$f" || { echo "FATAL: $f 里没有 CARHER_KEY,先重建" >&2; exit 1; }; done
# 🔴 zip 里用 grep -c 不用 grep -q:-q 命中就退出 ⇒ unzip 吃 SIGPIPE,set -o pipefail 下整条流水线判失败(好包被说成坏包)
for f in "${PKGS[@]:4:2}"; do
  [ "$(unzip -p "$f" | grep -ac CARHER_KEY)" -gt 0 ] || { echo "FATAL: $f 里没有 CARHER_KEY,先重建" >&2; exit 1; }
done

# 2) 打包上传(一个 tar,少几次 jms 往返)
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/ai-portal/static" "$STAGE/feedback" "$STAGE/installer/pkg"
cp scripts/ai-portal/server.py "$STAGE/ai-portal/"
cp scripts/ai-portal/static/index.html "$STAGE/ai-portal/static/"
cp scripts/feedback/feedback_server.py "$STAGE/feedback/"
cp scripts/ai-portal/installer/install.sh scripts/ai-portal/installer/install.ps1 "$STAGE/installer/"
cp "${PKGS[@]}" "$STAGE/installer/pkg/"
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf "$STAGE/ap.tgz" ai-portal feedback installer
J=scripts/jms
H=AIYJY-litellm
$J scp "$STAGE/ap.tgz" $H:/tmp/ap.tgz

cat > "$STAGE/remote.sh" <<'REMOTE'
set -euo pipefail
S="sudo -n"
PILOT="$1"
W=$(mktemp -d); trap 'rm -rf "$W" /tmp/ap.tgz' EXIT
tar -C "$W" -xzf /tmp/ap.tgz
/usr/bin/python3 -m py_compile "$W/ai-portal/server.py" "$W/feedback/feedback_server.py"
# 1) 文件
$S mkdir -p /opt/carher-ai-portal/ai-portal/static /opt/carher-ai-portal/feedback /var/www/ai-installer/pkg
$S install -d -m 0700 /var/lib/carher-ai-portal
$S install -m 0644 "$W/ai-portal/server.py" /opt/carher-ai-portal/ai-portal/server.py
$S install -m 0644 "$W/ai-portal/static/index.html" /opt/carher-ai-portal/ai-portal/static/index.html
$S install -m 0644 "$W/feedback/feedback_server.py" /opt/carher-ai-portal/feedback/feedback_server.py
$S install -m 0644 "$W/installer/install.sh" "$W/installer/install.ps1" /var/www/ai-installer/
for f in "$W"/installer/pkg/*; do $S install -m 0644 "$f" /var/www/ai-installer/pkg/; done
# 2) 配置:飞书密钥现读 k8s;核对 app id,不是 carher-236 就停
kubectl -n open-webui get deploy open-webui -o json | python3 -c '
import json,sys,subprocess,base64
c=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
env={x["name"]:x for x in c["env"]}
def val(n):
    x=env[n]
    if "value" in x: return x["value"]
    r=x["valueFrom"]["secretKeyRef"]
    s=json.loads(subprocess.check_output(["kubectl","-n","open-webui","get","secret",r["name"],"-o","json"]))
    return base64.b64decode(s["data"][r["key"]]).decode()
cid=val("FEISHU_CLIENT_ID")
if cid!="cli_aa8505afb0f89cb2":
    sys.exit("STOP: open-webui 的 FEISHU_CLIENT_ID=%s,不是 carher-236(cli_aa8505afb0f89cb2)" % cid)
sec=val("FEISHU_CLIENT_SECRET")
assert sec and "\n" not in sec
print("FEISHU_APP_ID="+cid); print("FEISHU_APP_SECRET="+sec)
' > "$W/fs.env"
# 已有的两个密钥沿用;没有才在本机生成。两个必须不同(server.py 也会拒绝启动)
old() { $S sh -c "test -f /etc/carher-ai-portal.env && grep -E '^$1=' /etc/carher-ai-portal.env | tail -1 | cut -d= -f2-" || true; }
SS="$(old SESSION_SECRET)"; IS="$(old INTERNAL_SECRET)"
[ ${#SS} -ge 32 ] || SS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
[ ${#IS} -ge 32 ] && [ "$IS" != "$SS" ] || IS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
[ "$IS" != "$SS" ]
{
  cat "$W/fs.env"
  echo "SESSION_SECRET=$SS"
  echo "INTERNAL_SECRET=$IS"
  echo "PILOT_UNION_IDS=$PILOT"
  echo "LISTEN=127.0.0.1:8793"
  echo "PUBLIC_ORIGIN=https://chat.auto-link.com.cn"
  echo "BASE_PATH=/ai"
  echo "STATIC_DIR=/opt/carher-ai-portal/ai-portal/static"
  echo "DB_PATH=/var/lib/carher-ai-portal/ai_portal.db"
  echo "TRUST_PROXY=1"
  echo "APPLY_DRY_RUN=1"
} > "$W/new.env"
unset SS IS
$S install -m 0600 -o root -g root "$W/new.env" /etc/carher-ai-portal.env
rm -f "$W/fs.env" "$W/new.env"
# 3) systemd
$S tee /etc/systemd/system/carher-ai-portal.service >/dev/null <<'UNIT'
[Unit]
Description=CarHer AI 自助门户 (chat.auto-link.com.cn/ai)
After=network-online.target
[Service]
EnvironmentFile=/etc/carher-ai-portal.env
ExecStart=/usr/bin/python3 /opt/carher-ai-portal/ai-portal/server.py
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/carher-ai-portal
[Install]
WantedBy=multi-user.target
UNIT
$S systemctl daemon-reload
$S systemctl enable carher-ai-portal >/dev/null 2>&1
$S systemctl restart carher-ai-portal
for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS -m 3 http://127.0.0.1:8793/ai/healthz >/dev/null 2>&1 && break; sleep 1; done
curl -fsS -m 5 http://127.0.0.1:8793/ai/healthz || { $S journalctl -u carher-ai-portal -n 30 --no-pager; exit 1; }
echo
# 4) nginx(只加一次)
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
if ! grep -q '>>> carher-ai-portal >>>' "$CONF"; then
  TS=$(date +%Y%m%d-%H%M%S); $S mkdir -p /etc/nginx/backup-ai-portal
  $S cp -a "$CONF" /etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.$TS
  $S python3 - "$CONF" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read()
# 精确匹配的 location 不继承同级设置 ⇒ charset / 缓存逐个写进去(PS5 的 irm 没 charset 会把中文解成乱码)
one = ('    location = /ai/i/{t}.{e} {{ alias /var/www/ai-installer/install.{e}; '
       'types {{ }} default_type "text/plain; charset=utf-8"; '
       'add_header Cache-Control "no-cache" always; limit_except GET HEAD {{ deny all; }} }}\n')
sh = "".join(one.format(t=t, e=e) for t in ("cursor", "codex", "claude") for e in ("sh", "ps1"))
blk='''    # >>> carher-ai-portal >>> AI 自助门户(127.0.0.1:8793)+ 一行命令安装器(/var/www/ai-installer)。回滚=删这一段
    location = /ai { return 301 /ai/; }
''' + sh + '''    location ^~ /ai/i/pkg/ {
        alias /var/www/ai-installer/pkg/;
        types { application/zip zip; }
        default_type application/octet-stream;
        limit_except GET HEAD { deny all; }
        add_header Cache-Control "no-cache" always;
    }
    location ^~ /ai/i/ { return 404; }
    location ^~ /ai/ {
        proxy_pass http://127.0.0.1:8793;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Cookie $http_cookie;
        client_max_body_size 2M;
        proxy_read_timeout 60s;
    }
    # <<< carher-ai-portal <<<
'''
a='    location / {\n        proxy_pass http://open_webui_upstream;'
assert s.count(a)==1
open(p,'w').write(s.replace(a,blk+a))
PY
  if ! $S nginx -t; then $S cp -a /etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.$TS "$CONF"; echo ROLLED-BACK; exit 1; fi
  $S systemctl reload nginx
  echo "nginx 已加 carher-ai-portal 段,备份 /etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.$TS"
fi
REMOTE
$J scp "$STAGE/remote.sh" $H:/tmp/ap_remote.sh
$J ssh $H "bash /tmp/ap_remote.sh '$PILOT'; rc=\$?; rm -f /tmp/ap_remote.sh; exit \$rc"

# 5) 外网验收
U=https://chat.auto-link.com.cn
fail=0
chk() { if [ "$2" = "$3" ]; then echo "OK   $1 = $2"; else echo "FAIL $1 = $2(应 $3)"; fail=1; fi; }
chk "健康检查 /ai/healthz" "$(curl -s -m 10 $U/ai/healthz)" '{"ok": true}'
chk "未登录 /ai/api/me" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/api/me)" 401
chk "页面 /ai/" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/)" 200
loc="$(curl -s -o /dev/null -w '%{redirect_url}' -m 10 "$U/ai/auth/login?redirect=/ai/")"
case "$loc" in
  https://accounts.feishu.cn/*client_id=cli_aa8505afb0f89cb2*redirect_uri=https%3A%2F%2Fchat.auto-link.com.cn%2Fai%2Fauth%2Fcallback*)
    chk "登录跳飞书(client_id=carher-236 + 回调)" ok ok ;;
  *) chk "登录跳飞书(client_id=carher-236 + 回调)" "$loc" "accounts.feishu.cn?client_id=cli_aa85…&redirect_uri=…/ai/auth/callback" ;;
esac
mkdir -p "$STAGE/dl/pkg"
for t in cursor codex claude; do
  curl -fsS -m 30 "$U/ai/i/$t.sh" -o "$STAGE/dl/sh_$t"; curl -fsS -m 30 "$U/ai/i/$t.ps1" -o "$STAGE/dl/ps_$t"
  cmp -s "$STAGE/dl/sh_$t" scripts/ai-portal/installer/install.sh && chk "/ai/i/$t.sh 逐字节" ok ok || chk "/ai/i/$t.sh 逐字节" diff ok
  cmp -s "$STAGE/dl/ps_$t" scripts/ai-portal/installer/install.ps1 && chk "/ai/i/$t.ps1 逐字节" ok ok || chk "/ai/i/$t.ps1 逐字节" diff ok
done
chk "/ai/i/codex.ps1 Content-Type" "$(curl -s -o /dev/null -w '%{content_type}' -m 10 $U/ai/i/codex.ps1)" "text/plain; charset=utf-8"
for f in "${PKGS[@]}"; do
  b=$(basename "$f"); curl -fsS -m 60 "$U/ai/i/pkg/$b" -o "$STAGE/dl/pkg/$b" || true
  cmp -s "$STAGE/dl/pkg/$b" "$f" && chk "pkg/$b 逐字节" ok ok || chk "pkg/$b 逐字节" diff ok
done
chk "未知路径 /ai/i/x.sh" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/i/x.sh)" 404
chk "openchat /api/version" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/api/version)" 200
chk "反馈页 /feedback/" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/feedback/)" 200
chk "一键页 /oneclick/" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/oneclick/)" 200
[ $fail = 0 ] && echo "✅ 外网验收全过" || { echo "❌ 外网验收有失败项"; exit 1; }
