#!/usr/bin/env bash
# 意见反馈页部署到 198:https://chat.auto-link.com.cn/feedback/
#
#   静态页   /var/www/feedback/index.html
#   小服务   /opt/carher-feedback/feedback_server.py  ← systemd carher-feedback.service,监听 127.0.0.1:8792
#   配置     /etc/carher-feedback.env (root 600)。FEISHU_APP_SECRET 每次部署时从 k8s 的 open-webui Deployment 现读,不进 git
#   nginx    chat.auto-link.com.cn.conf 里 `# >>> carher-feedback >>>` 一段:/feedback/ 静态、/feedback/api/ 反代小服务
#            其余路径仍反代 Open WebUI。备份在 /etc/nginx/backup-feedback/
#   回滚     删 nginx 那一段 → nginx -t → reload;systemctl disable --now carher-feedback
#
# 用法: bash scripts/feedback/deploy_198.sh        (重复跑只更新文件 + 重启服务,nginx 段已在就不动)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
python3 -m pytest scripts/feedback -q -p no:cacheprovider
J=scripts/jms
H=AIYJY-litellm
$J scp scripts/feedback/feedback_server.py $H:/tmp/fb_server.py
$J scp scripts/feedback/index.html $H:/tmp/fb_index.html
cat > .work/fb_remote.sh <<'REMOTE'
set -euo pipefail
S="sudo -n"
# 1) 文件
$S mkdir -p /opt/carher-feedback /var/www/feedback
$S install -m 0755 /tmp/fb_server.py /opt/carher-feedback/feedback_server.py
$S install -m 0644 /tmp/fb_index.html /var/www/feedback/index.html
rm -f /tmp/fb_server.py /tmp/fb_index.html
# 2) 配置:密钥现读 k8s(open-webui 用的同一个飞书应用),只写 root 可读文件
DB=$(ls -d /Data/rancher/storage/*_open-webui_open-webui-data)/webui.db
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
print("FEISHU_APP_ID="+val("FEISHU_CLIENT_ID")); print("FEISHU_APP_SECRET="+val("FEISHU_CLIENT_SECRET"))
' > /tmp/fb.env.part
{ cat /tmp/fb.env.part; echo "OWUI_DB=$DB"; echo "OWUI_URL=http://127.0.0.1:30880"; echo "LISTEN=127.0.0.1:8792"; } | $S tee /etc/carher-feedback.env >/dev/null
rm -f /tmp/fb.env.part
$S chmod 600 /etc/carher-feedback.env
# 3) systemd
$S tee /etc/systemd/system/carher-feedback.service >/dev/null <<'UNIT'
[Unit]
Description=CarHer 意见反馈小服务 (chat.auto-link.com.cn/feedback/api)
After=network-online.target
[Service]
EnvironmentFile=/etc/carher-feedback.env
ExecStart=/usr/bin/python3 /opt/carher-feedback/feedback_server.py
Restart=always
RestartSec=3
DynamicUser=no
NoNewPrivileges=yes
ProtectSystem=full
[Install]
WantedBy=multi-user.target
UNIT
$S systemctl daemon-reload
$S systemctl enable --now carher-feedback >/dev/null 2>&1
$S systemctl restart carher-feedback
sleep 2
curl -fsS -m 5 http://127.0.0.1:8792/feedback/api/healthz
echo
# 4) nginx(只加一次)
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
if ! grep -q '>>> carher-feedback >>>' "$CONF"; then
  TS=$(date +%Y%m%d-%H%M%S); $S mkdir -p /etc/nginx/backup-feedback
  $S cp -a "$CONF" /etc/nginx/backup-feedback/chat.auto-link.com.cn.conf.$TS
  $S python3 - "$CONF" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read()
blk='''    # >>> carher-feedback >>> 意见反馈页(静态)+ 小服务(127.0.0.1:8792)。回滚=删这一段
    location = /feedback { return 301 /feedback/; }
    location ^~ /feedback/api/ {
        proxy_pass http://127.0.0.1:8792;
        proxy_set_header Host $host;
        proxy_set_header Cookie $http_cookie;
        client_max_body_size 25M;
        proxy_read_timeout 120s;
    }
    location ^~ /feedback/ {
        alias /var/www/feedback/;
        index index.html;
        limit_except GET HEAD { deny all; }
        add_header Cache-Control "no-cache" always;
    }
    # <<< carher-feedback <<<
'''
a='    location / {\n        proxy_pass http://open_webui_upstream;'
assert s.count(a)==1
open(p,'w').write(s.replace(a,blk+a))
PY
  if ! $S nginx -t; then $S cp -a /etc/nginx/backup-feedback/chat.auto-link.com.cn.conf.$TS "$CONF"; echo ROLLED-BACK; exit 1; fi
  $S systemctl reload nginx
  echo "nginx 已加 carher-feedback 段,备份 /etc/nginx/backup-feedback/chat.auto-link.com.cn.conf.$TS"
fi
REMOTE
$J scp .work/fb_remote.sh $H:/tmp/fb_remote.sh
$J ssh $H "bash /tmp/fb_remote.sh; rm -f /tmp/fb_remote.sh"
rm -f .work/fb_remote.sh
# 5) 外网验收
code=$(curl -s -o /dev/null -w '%{http_code}' https://chat.auto-link.com.cn/feedback/)
me=$(curl -s -o /dev/null -w '%{http_code}' https://chat.auto-link.com.cn/feedback/api/me)
openchat=$(curl -s -o /dev/null -w '%{http_code}' https://chat.auto-link.com.cn/api/version)
echo "页面 $code(应 200) · 未登录 /me $me(应 401) · openchat $openchat(应 200)"
[ "$code" = 200 ] && [ "$me" = 401 ] && [ "$openchat" = 200 ]
