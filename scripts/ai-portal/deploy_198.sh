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
# 「重置我的 key」:0 = 真换(11-04 用户定:「要改成真换,不要演练,好多人在后面等着呢」);1 = 只演练
ROTATE="${ROTATE_DRY_RUN-0}"
case "$ROTATE" in 0|1) ;; *) echo "ROTATE_DRY_RUN 只能是 0 或 1" >&2; exit 1 ;; esac
# 申请 key:0 = 真写表让 poller 发(内部同事);外部人另有 APPLY_ALLOW_EXTERNAL 默认关,走审核
APPLYDR="${APPLY_DRY_RUN-0}"
case "$APPLYDR" in 0|1) ;; *) echo "APPLY_DRY_RUN 只能是 0 或 1" >&2; exit 1 ;; esac
case "$PILOT" in *[!A-Za-z0-9_,]*) echo "PILOT_UNION_IDS 只能是 on_xxx,on_yyy" >&2; exit 1 ;; esac
ADMIN_IDS="${ADMIN_UNION_IDS:-on_0577e767b8995740ebbf83f06d9f8bd1}"   # 管理后台白名单(默认只有刘国现)
case "$ADMIN_IDS" in *[!A-Za-z0-9_,]*) echo "ADMIN_UNION_IDS 只能是 on_xxx,on_yyy" >&2; exit 1 ;; esac

# 0) 本地门禁:测试 + e2e 不过就停
# 门禁测试**不能继承部署开关**:server.py 会从环境变量读配置,ROTATE_DRY_RUN=0 漏进测试里的子进程
# ⇒ 它没配 master key 就按设计拒绝启动 ⇒ 门禁假红、部署被挡(11-04 真踩到)。用 env -u 剥掉。
# 🔴 11-05 两个**真让门禁装绿**的坑,一起修在这儿:
#
# 坑一:`pytest <父目录> <子目录>` 会静默丢测试,退出码还是 0。实测(本机 pytest 9.1.1):
#     pytest scripts/ai-portal                                  → 386 条
#     pytest scripts/ai-portal scripts/ai-portal/installer      → **0 条**,rc=0
#     pytest scripts/ai-portal scripts/feedback scripts/account-store → **0 条**,rc=0
#   也就是说原来那条三参数的门禁命令**一条测试都没跑过**,一直"全绿"。
#   ⇒ 只传互不嵌套的目录,再用条数门禁兜底(数字写小一点,只拦"整批被丢",不拦正常增删)。
#
# 坑二:`env -u X \` 下一行是**注释**时,反斜杠续的是注释行,`env` 就只剩自己 ⇒ 变成"打印环境变量"
#   然后退出 0,后面的 pytest 变成另一条**没有 env -u 前缀**的命令(实测:LEAK=1 照样可见)。
#   而 env -u 在这里是必需的:server.py 从环境变量读配置,ROTATE_DRY_RUN=0 漏进测试子进程
#   ⇒ 它没配 master key 就按设计拒绝启动 ⇒ 门禁假红、部署被挡(11-04 真踩到)。
#   ⇒ 不用续行:把要剥的变量定义成一个数组,每条命令都显式带上。
GATE_ENV=(env -u ROTATE_DRY_RUN -u APPLY_DRY_RUN -u PILOT_UNION_IDS)
GATE_DIRS=(scripts/ai-portal scripts/feedback)      # 互不嵌套;installer 已在 scripts/ai-portal 里
# 阳性自检:env -u 真的生效了吗(续行坑会让它静默失效)
ROTATE_DRY_RUN=0 "${GATE_ENV[@]}" python3 -c 'import os,sys; sys.exit(1 if "ROTATE_DRY_RUN" in os.environ else 0)' \
  || { echo "FATAL: 门禁的 env -u 没生效(多半是续行/注释把命令拆断了)" >&2; exit 1; }
N=$("${GATE_ENV[@]}" python3 -m pytest "${GATE_DIRS[@]}" -q -p no:cacheprovider --collect-only 2>/dev/null \
      | tail -1 | grep -oE '^[0-9]+')
[ "${N:-0}" -ge 380 ] || { echo "FATAL: 门禁只收到 ${N:-0} 条测试(应 ≥380)⇒ 收集被静默丢了,先查 pytest 参数" >&2; exit 1; }
echo "门禁收集到 $N 条测试"
"${GATE_ENV[@]}" python3 -m pytest "${GATE_DIRS[@]}" -q -p no:cacheprovider
# 账户库那套要 cryptography,解释器可能和上面不同 ⇒ 单独一跑,缺依赖就明说而不是当它不存在
if python3 -c 'import cryptography' 2>/dev/null; then
  "${GATE_ENV[@]}" python3 -m pytest scripts/account-store -q -p no:cacheprovider
else
  echo "!! 本机 python3 没有 cryptography ⇒ 跳过 scripts/account-store 的 71 条(门户部署不依赖它)" >&2
fi
"${GATE_ENV[@]}" python3 .work/ai-portal/e2e/e2e.py
# nginx 加固段的离线验收(它改的是生产 nginx,不能只靠部署时 nginx -t)
"${GATE_ENV[@]}" python3 scripts/ai-portal/test_nginx_harden.py

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
mkdir -p "$STAGE/ai-portal/static" "$STAGE/ai-portal/admin" "$STAGE/feedback" "$STAGE/installer/pkg"
cp scripts/ai-portal/server.py scripts/ai-portal/admin_console.py "$STAGE/ai-portal/"
# 管理后台页面:不放 static/(那里的文件不过闸门),由 admin_console.py 过完闸门才出
cp scripts/ai-portal/admin/admin.html "$STAGE/ai-portal/admin/"
# 公司映射(tenant_key → 公司名):外部人申请要按它判「是不是合作公司」。🔴 漏拷 = 服务端读不到 ⇒ 失败关闭
# ⇒ **所有外部人申请全 403**(内部同事不受影响,所以不容易被发现)。只有公司标识,不含个人 id / key。
[ -s scripts/ai-portal/tenant_map.json ] || { echo "FATAL: 缺 scripts/ai-portal/tenant_map.json(外部人申请会全部被拒)" >&2; exit 1; }
python3 -c 'import json,sys;d=json.load(open("scripts/ai-portal/tenant_map.json"));n=sum(1 for k,v in d.items() if k!="_meta" and isinstance(v,dict) and v.get("org"));sys.exit(0 if n>0 else 1)' \
  || { echo "FATAL: tenant_map.json 里一个能放行的公司都没有" >&2; exit 1; }
cp scripts/ai-portal/tenant_map.json "$STAGE/ai-portal/"
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
RDR="$2"
ADR="$3"
ADMIN_IDS="$4"
case "$ADMIN_IDS" in *[!A-Za-z0-9_,]*|"") echo "远端没拿到 ADMIN_UNION_IDS" >&2; exit 1 ;; esac
W=$(mktemp -d); trap 'rm -rf "$W" /tmp/ap.tgz' EXIT
tar -C "$W" -xzf /tmp/ap.tgz
/usr/bin/python3 -m py_compile "$W/ai-portal/server.py" "$W/ai-portal/admin_console.py" "$W/feedback/feedback_server.py"
# 1) 文件
$S mkdir -p /opt/carher-ai-portal/ai-portal/static /opt/carher-ai-portal/ai-portal/admin /opt/carher-ai-portal/feedback /var/www/ai-installer/pkg
$S install -d -m 0700 /var/lib/carher-ai-portal
$S install -m 0644 "$W/ai-portal/server.py" /opt/carher-ai-portal/ai-portal/server.py
$S install -m 0644 "$W/ai-portal/admin_console.py" /opt/carher-ai-portal/ai-portal/admin_console.py
$S install -m 0644 "$W/ai-portal/admin/admin.html" /opt/carher-ai-portal/ai-portal/admin/admin.html
$S install -m 0644 "$W/ai-portal/tenant_map.json" /opt/carher-ai-portal/ai-portal/tenant_map.json
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
# 「重置我的 key」要 198 网关的 master key。和 FEISHU_APP_SECRET 一样**每次现读**,不进 git、不沿用旧值:
#   master key 10-28 轮换过一次(secret 名从 litellm-secrets 变成 -r2),沿用旧值 = 轮换后重置静默全失败。
#   secret 名跟着 deploy/litellm-proxy 的 envFrom 走(同 scripts/litellm_198_secret.py 的 SHELL_RESOLVE)。
MK="$($S sh -c 'for s in $(kubectl -n litellm-product get deploy litellm-proxy -o jsonpath="{range .spec.template.spec.containers[0].envFrom[*]}{.secretRef.name}{\"\n\"}{end}"); do v=$(kubectl -n litellm-product get secret "$s" -o jsonpath="{.data.LITELLM_MASTER_KEY}" 2>/dev/null); [ -n "$v" ] && { printf %s "$v" | base64 -d; break; }; done')"
ASIP="$($S kubectl -n account-store get svc account-store -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
[ -n "$ASIP" ] || echo "!! 读不到 account-store Service ⇒ 管理后台会 503" >&2
$S test -s /etc/account-store/clients/adminweb.token || echo "!! 缺 /etc/account-store/clients/adminweb.token(先跑 scripts/account-store/deploy_198.sh)⇒ 管理后台会 503" >&2
[ -n "$MK" ] || echo "!! 读不到 198 网关 master key ⇒ 「重置我的 key」会自动关闭(页面退回「请提反馈」)" >&2
# 🔴 这段跑在 **198 上**:本机的 ROTATE_DRY_RUN 传不过来,只能靠参数 $2(11-04 踩到:本机设了 0,
#    远端读自己的环境变量拿不到 ⇒ 永远落到默认值,部署全绿、却一直是演练模式)
case "$RDR" in 0|1) ;; *) echo "远端没拿到 ROTATE_DRY_RUN(应为 0/1,实际「$RDR」)" >&2; exit 1 ;; esac
case "$ADR" in 0|1) ;; *) echo "远端没拿到 APPLY_DRY_RUN(应为 0/1,实际「$ADR」)" >&2; exit 1 ;; esac
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
  echo "APPLY_DRY_RUN=$ADR"
  echo "APPLY_ALLOW_EXTERNAL=1"   # 11-04 用户定:外部人登录飞书后就能申请;要收紧改 0
  [ -n "$MK" ] && echo "LITELLM_MASTER_KEY=$MK"
  echo "ROTATE_DRY_RUN=$RDR"
  # 管理后台 /ai/admin/:白名单默认只有刘国现;账户库地址现读 Service ClusterIP;token 文件 root 0600
  # (由 scripts/account-store/deploy_198.sh 生成)。缺任一项 ⇒ 后台 503,门户其余功能不受影响
  echo "ADMIN_UNION_IDS=${ADMIN_IDS}"
  [ -n "$ASIP" ] && echo "ACCOUNT_STORE_URL=http://$ASIP"
  echo "ACCOUNT_STORE_TOKEN_FILE=/etc/account-store/clients/adminweb.token"
  # 11-05 申请走账户库(POST /v1/accounts):portal client = read + reveal:self + issue。缺 ⇒ 申请 503(不退回写表)
  echo "ACCOUNT_STORE_ISSUE_TOKEN_FILE=/etc/account-store/clients/portal.token"
} > "$W/new.env"
unset SS IS MK
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
# all = 「三个都装」那条命令的网址(/ai/i/all.sh)。四个名字都 alias 到同一份 install.sh / install.ps1,
# 装哪个以安装码换回来的 tool 为准 ⇒ 加名字不改脚本。
sh = "".join(one.format(t=t, e=e) for t in ("cursor", "codex", "claude", "all") for e in ("sh", "ps1"))
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
$J ssh $H "bash /tmp/ap_remote.sh '$PILOT' '$ROTATE' '$APPLYDR' '$ADMIN_IDS'; rc=\$?; rm -f /tmp/ap_remote.sh; exit \$rc"

# 4b) 加固段(internal + magic)—— **幂等 reconcile**,每次部署都对齐
# 为什么单独一段:11-04 这两段是手工加到 198 上的,**没进本脚本** ⇒ 谁要是重走一遍 4) 的首次插入
# 路径(换 conf / 删过那一段),加固就静默消失,而部署照样全绿。这里按内容比对,缺了就补、旧了就换。
#
# 🔴 11-05 实测修掉的真 bug:原来 internal 段写的是 `allow 127.0.0.1; deny all;`,
#    但 198 的 nginx 前面还有一层前置代理 ⇒ 这个 location **没有** realip 指令时,
#    $remote_addr 永远是前置代理那一个(实测 10.68.13.97),127.0.0.1 这条规则**一次都不可能命中**。
#    证据:机器人 pod 里带正确密钥打这个接口 → nginx 自己的 403 页(error log: "access forbidden by
#    rule, client: 10.68.13.97"),请求根本没到门户进程;同一时刻在 198 本机打 127.0.0.1:8793 → 401。
#    ⇒ 外部人发「登录」拿不到链接(机器人侧 audit 里 11-04 14:08 还成功过一次,18:26 加固后就断了)。
#    修法:这个 location 也带上 realip(信任前置代理的 X-Real-IP —— 实测它会**覆盖**客户端伪造的值),
#    再按**机器人 pod 的真实出口 IP** 放行。门户侧另有 INTERNAL_SECRET + 按人/全局两道签发限流。
BOT_EGRESS="${BOT_EGRESS_IP:-47.96.23.21}"   # her-236(机器人)所在集群的出口 IP,11-05 在 pod 里实测
case "$BOT_EGRESS" in *[!0-9.]*) echo "BOT_EGRESS_IP 只能是 IPv4" >&2; exit 1 ;; esac
cat > "$STAGE/harden.sh" <<REMOTE2
set -euo pipefail
S="sudo -n"
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
BOT_EGRESS="\$1"
TS=\$(date +%Y%m%d-%H%M%S); \$S mkdir -p /etc/nginx/backup-ai-portal
\$S cp -a "\$CONF" /etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.harden.\$TS
\$S python3 - "\$CONF" "\$BOT_EGRESS" <<'PY'
import re, sys
p, bot = sys.argv[1], sys.argv[2]
s = open(p).read()
PROXY = "10.68.13.97"   # 前置代理;只信它的 X-Real-IP(11-04/11-05 实测:伪造值会被它覆盖)

internal = """    # >>> carher-ai-portal:internal >>> 签发一次性登录链接的内部接口。只有机器人该调。
    # 11-04 红队 P0-3:这个接口原来公网可达,只靠 INTERNAL_SECRET 挡着,而拿到它就能给**任意** union_id
    #   签出会话 ⇒ 全部 key 可读。
    # 🔴 11-05:原来写 `allow 127.0.0.1` —— 这个 location 没有 realip ⇒ $remote_addr 恒为前置代理,
    #   该规则**永不命中**,机器人带对密钥也被 nginx 403(实测),外部人因此登不进来。
    #   现在:带上 realip 再按机器人出口 IP 放行。回滚=删这一段。
    location ^~ /ai/api/internal/ {
        set_real_ip_from %s;
        real_ip_header X-Real-IP;
        allow 127.0.0.1;
        allow ::1;
        allow %s;
        deny all;
        proxy_pass http://127.0.0.1:8793;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 64k;
    }
    # <<< carher-ai-portal:internal <<<
""" % (PROXY, bot)

harden = """    # >>> carher-ai-portal:harden >>> 11-04 P2-3:一次性登录 token 在 query 里,这条路径不记访问日志;
    # realip:只信前置代理 %s 的 X-Real-IP(它会覆盖客户端伪造的值,11-04 实测)。回滚=删这一段
    location = /ai/auth/magic {
        access_log off;
        set_real_ip_from %s;
        real_ip_header X-Real-IP;
        proxy_pass http://127.0.0.1:8793;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Cookie $http_cookie;
        client_max_body_size 64k;
        proxy_read_timeout 60s;
    }
    # <<< carher-ai-portal:harden <<<
""" % (PROXY, PROXY)

def reconcile(src, tag, block):
    """按 >>> tag >>> … <<< tag <<< 整段替换;没有就插在 `location ^~ /ai/ {` 之前。"""
    beg, end = ">>> carher-ai-portal:%s >>>" % tag, "<<< carher-ai-portal:%s <<<" % tag
    pat = re.compile(r"[ \t]*#[^\n]*" + re.escape(beg) + r".*?" + re.escape(end) + r"[^\n]*\n",
                     re.S)
    hits = pat.findall(src)
    if len(hits) > 1:
        raise SystemExit("FATAL: %s 段出现 %d 次,人工看一眼再说" % (tag, len(hits)))
    if hits:
        return (src.replace(hits[0], block), "updated" if hits[0] != block else "same")
    anchor = "    location ^~ /ai/ {"
    if src.count(anchor) != 1:
        raise SystemExit("FATAL: 找不到唯一的 `location ^~ /ai/ {` 锚点,拒绝瞎插")
    return (src.replace(anchor, block + anchor), "inserted")

out = s
notes = []
for tag, blk in (("internal", internal), ("harden", harden)):
    out, how = reconcile(out, tag, blk)
    notes.append("%s=%s" % (tag, how))
if out != s:
    open(p, "w").write(out)
print("harden reconcile:", " ".join(notes))
PY
if ! \$S nginx -t 2>/dev/null; then
  \$S cp -a /etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.harden.\$TS "\$CONF"
  echo "HARDEN ROLLED-BACK(nginx -t 不过)"; \$S nginx -t; exit 1
fi
\$S systemctl reload nginx
# 回读:放行名单里必须有机器人出口 IP,且这个 location 带了 realip(两样缺一个都等于没修)
\$S sed -n '/>>> carher-ai-portal:internal/,/<<< carher-ai-portal:internal/p' "\$CONF" \
  | grep -q "allow \$BOT_EGRESS;" || { echo "FATAL: 回读没看到 allow \$BOT_EGRESS"; exit 1; }
\$S sed -n '/>>> carher-ai-portal:internal/,/<<< carher-ai-portal:internal/p' "\$CONF" \
  | grep -q "real_ip_header X-Real-IP;" || { echo "FATAL: internal 段没有 realip,allow 规则不会命中"; exit 1; }
echo "nginx 加固段已对齐(备份 .harden.\$TS)"
REMOTE2
$J scp "$STAGE/harden.sh" $H:/tmp/ap_harden.sh
$J ssh $H "bash /tmp/ap_harden.sh '$BOT_EGRESS'; rc=\$?; rm -f /tmp/ap_harden.sh; exit \$rc"

# 5) 外网验收
U=https://chat.auto-link.com.cn
fail=0
chk() { if [ "$2" = "$3" ]; then echo "OK   $1 = $2"; else echo "FAIL $1 = $2(应 $3)"; fail=1; fi; }
chk "健康检查 /ai/healthz" "$(curl -s -m 10 $U/ai/healthz)" '{"ok": true}'
# 回读 198 上真正生效的值,别信「我传了」:上一版传不过去,部署照样全绿
chk "重置模式 ROTATE_DRY_RUN" "$($J ssh $H "sudo -n grep -E '^ROTATE_DRY_RUN=' /etc/carher-ai-portal.env | cut -d= -f2" 2>/dev/null | tr -d '\r' | tail -1)" "$ROTATE"
chk "申请模式 APPLY_DRY_RUN" "$($J ssh $H "sudo -n grep -E '^APPLY_DRY_RUN=' /etc/carher-ai-portal.env | cut -d= -f2" 2>/dev/null | tr -d '\r' | tail -1)" "$APPLYDR"
chk "公司映射已上线(能放行的公司数>0)" "$($J ssh $H "sudo -n python3 -c 'import json;d=json.load(open(\"/opt/carher-ai-portal/ai-portal/tenant_map.json\"));print(int(sum(1 for k,v in d.items() if k!=\"_meta\" and isinstance(v,dict) and v.get(\"org\"))>0))'" 2>/dev/null | tr -d '\r' | tail -1)" 1
chk "外部人可申请 APPLY_ALLOW_EXTERNAL" "$($J ssh $H "sudo -n grep -E '^APPLY_ALLOW_EXTERNAL=' /etc/carher-ai-portal.env | cut -d= -f2" 2>/dev/null | tr -d '\r' | tail -1)" 1
chk "灰度名单 PILOT_UNION_IDS" "$($J ssh $H "sudo -n grep -E '^PILOT_UNION_IDS=' /etc/carher-ai-portal.env | cut -d= -f2" 2>/dev/null | tr -d '\r' | tail -1)" "$PILOT"
chk "网关 master key 已配" "$($J ssh $H "sudo -n grep -cE '^LITELLM_MASTER_KEY=.{20,}' /etc/carher-ai-portal.env" 2>/dev/null | tr -d '\r' | tail -1)" 1
chk "未登录 /ai/api/me" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/api/me)" 401
chk "未登录 /ai/api/admin/accounts" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'X-Requested-With: ai-admin' $U/ai/api/admin/accounts)" 401
chk "未登录 /ai/admin/ 跳登录" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/admin/)" 302
chk "管理后台页不在静态目录" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/admin/admin.html)" 404
chk "门户已接上账户库(日志无「账户库未接入」)" "$($J ssh $H "sudo -n journalctl -u carher-ai-portal --since '-2min' --no-pager | grep -c 账户库未接入" 2>/dev/null | tr -d '\r' | tail -1)" 0
chk "页面 /ai/" "$(curl -s -o /dev/null -w '%{http_code}' -m 10 $U/ai/)" 200
loc="$(curl -s -o /dev/null -w '%{redirect_url}' -m 10 "$U/ai/auth/login?redirect=/ai/")"
case "$loc" in
  https://accounts.feishu.cn/*client_id=cli_aa8505afb0f89cb2*redirect_uri=https%3A%2F%2Fchat.auto-link.com.cn%2Fai%2Fauth%2Fcallback*)
    chk "登录跳飞书(client_id=carher-236 + 回调)" ok ok ;;
  *) chk "登录跳飞书(client_id=carher-236 + 回调)" "$loc" "accounts.feishu.cn?client_id=cli_aa85…&redirect_uri=…/ai/auth/callback" ;;
esac
mkdir -p "$STAGE/dl/pkg"
# 🔴 all 也要验:上面 nginx 那段是「只加一次」,11-04 之前已经加过的机器上**没有** all.sh / all.ps1
#    这两条 location ⇒ 网页默认那条「三个都装」命令会 404。这里验出 diff 就是这个原因,
#    要手动往 `# >>> carher-ai-portal >>>` 段里补两行(照 cursor.sh 那行改名),nginx -t 再 reload。
for t in cursor codex claude all; do
  curl -fsS -m 30 "$U/ai/i/$t.sh" -o "$STAGE/dl/sh_$t" || true; curl -fsS -m 30 "$U/ai/i/$t.ps1" -o "$STAGE/dl/ps_$t" || true
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
