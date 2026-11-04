#!/usr/bin/env bash
# 在 198 上跑(刘国现执行;本脚本没有被执行过)。照 deploy_198.sh 的套路:只加一次 + 备份 + nginx -t 失败自动还原。
#   ① P2-3:location = /ai/auth/magic 关 access_log(GET ?t=<一次性登录 token> 现在会进 /var/log/nginx/access.log)
#   ② /ai/ 段打开 realip,只信前置代理 10.68.13.97 给的 X-Real-IP
#      (现在门户看到的所有人 IP 都是 10.68.13.97 ⇒ 每 IP 限流 = 全局限流,redeem/reveal 审计里的 IP 没用)
# 用法(本机仓库根):
#   scripts/jms scp .work/ai-portal/harden/nginx_harden_198.sh AIYJY-litellm:/tmp/ap_nginx_harden.sh
#   scripts/jms ssh AIYJY-litellm "bash /tmp/ap_nginx_harden.sh; rc=\$?; rm -f /tmp/ap_nginx_harden.sh; exit \$rc"
# 回滚:cp 备份回去 → nginx -t → systemctl reload nginx(备份路径脚本会打印)
set -euo pipefail
S="sudo -n"
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
if $S grep -q '>>> carher-ai-portal:harden >>>' "$CONF"; then echo "已经加过,跳过"; exit 0; fi
TS=$(date +%Y%m%d-%H%M%S); $S mkdir -p /etc/nginx/backup-ai-portal
BK=/etc/nginx/backup-ai-portal/chat.auto-link.com.cn.conf.$TS
$S cp -a "$CONF" "$BK"
$S python3 - "$CONF" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
anchor = '    location ^~ /ai/ {\n        proxy_pass http://127.0.0.1:8793;\n'
assert s.count(anchor) == 1, "锚点不唯一/不存在:%d" % s.count(anchor)
realip = ('        set_real_ip_from 10.68.13.97;\n'
          '        real_ip_header X-Real-IP;\n')
magic = ('    # >>> carher-ai-portal:harden >>> 11-04 P2-3:一次性登录 token 在 query 里,这条路径不记访问日志;\n'
         '    # realip:只信前置代理 10.68.13.97 的 X-Real-IP(它会覆盖客户端伪造的值,11-04 实测)。回滚=删这一段 + 下面两行 realip\n'
         '    location = /ai/auth/magic {\n'
         '        access_log off;\n' + realip +
         '        proxy_pass http://127.0.0.1:8793;\n'
         '        proxy_set_header Host $host;\n'
         '        proxy_set_header X-Real-IP $remote_addr;\n'
         '        proxy_set_header Cookie $http_cookie;\n'
         '        client_max_body_size 64k;\n'
         '        proxy_read_timeout 60s;\n'
         '    }\n'
         '    # <<< carher-ai-portal:harden <<<\n')
s = s.replace(anchor, magic + anchor + realip)
open(p, 'w').write(s)
PY
if ! $S nginx -t; then $S cp -a "$BK" "$CONF"; echo "ROLLED-BACK(nginx -t 没过,已还原)"; exit 1; fi
$S systemctl reload nginx
echo "已加,备份 $BK"
# 验收(只打计数,不打 token):
curl -s -o /dev/null -w 'magic GET=%{http_code}\n' "http://127.0.0.1/ai/auth/magic?t=PROBEPROBEPROBEPROBEPROBEPROBEPROBEPROBE123" -H 'Host: chat.auto-link.com.cn'
sleep 1
echo "access.log 里 PROBE 行数(应为 0):$($S grep -c 'PROBEPROBE' /var/log/nginx/access.log || true)"
