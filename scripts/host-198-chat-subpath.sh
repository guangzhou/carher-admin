#!/bin/bash
# 198 nginx：把服务挂到 https://chat.auto-link.com.cn/<name>/ 子路径（不用找 IT，DNS/证书/回源都是现成的）
#
#   scripts/host-198-chat-subpath.sh check  <name>                                   # 只读：名字能不能用
#   scripts/host-198-chat-subpath.sh add    <name> 127.0.0.1:30999 [--keep-prefix] [--ws] [--body 100m] [--timeout 600] [--dry-run]
#   scripts/host-198-chat-subpath.sh remove <name>
#   scripts/host-198-chat-subpath.sh list
#   scripts/host-198-chat-subpath.sh selftest                                        # 改完本脚本必跑：临时服务端到端 add→取到→remove
#
# 链路：用户 → https chat.auto-link.com.cn（IT 入口 58.241.5.230 终结 SSL）→ 198:80 nginx
#       → chat.auto-link.com.cn.conf 里按 location 分流：/<name>/ → 你的服务，其余 → Open WebUI
# 转发方式（二选一）：
#   默认         去掉前缀：浏览器 /<name>/a/b → 服务收到 /a/b。服务不用改，但前端如果写死了 /api、/assets
#                这类根路径，会打到 Open WebUI 上（像 myai 那样得 sub_filter 改写）⇒ 前端最好用相对路径
#   --keep-prefix 原样转发：服务收到 /<name>/a/b。服务自己支持 base path（如 --root-path /<name>）时用这个，最稳
# 🔴 Open WebUI 是单页应用，任何不存在的路径都回 200 + Open WebUI 首页 ⇒ 状态码 200 不代表挂上了，
#    自检看的是「返回的不是 Open WebUI 页面」。
# 🔴 只往 chat 的 conf 里插一段带标记的 location（`# >>> chat-subpath:<name> >>>`），不动别的段；
#    插在 `location / { proxy_pass http://open_webui_upstream;` 前面，oneclick/feedback/myai 的部署脚本也认这个锚点。
# 🔴 备份只放 /etc/nginx/backups/（include sites-enabled/* 无后缀过滤，.bak 放进去会被加载）。只 reload，禁 restart。
# 退出码：0=OK；1=参数/前置检查不过；2=nginx -t 失败（已回滚）；3=上线后自检失败；4=ssh 失败
set -uo pipefail
H198=${H198:-cltx@10.68.13.198}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes "$H198")
SITE=chat.auto-link.com.cn
CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf

die(){ echo "STOP: $*" >&2; exit "${2:-1}"; }
usage(){ sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 1; }
remote(){ "${SSH[@]}" "$@" 2> >(grep -v 'port forwarding' >&2); }

# Open WebUI 自己用的顶级路径 + chat 上已有的段 —— 占了会把 Open WebUI 搞坏
RESERVED=" api static assets _app oauth ws socket.io cache health ollama openai auth c s admin workspace
 notes channels home playground manifest.json favicon.png opensearch.xml robots.txt audio images files
 oneclick keyprobe feedback myai "

CMD=${1:-}; NAME=${2:-}
[ -n "$CMD" ] || usage
if [ "$CMD" != list ] && [ "$CMD" != selftest ]; then
  [ -n "$NAME" ] || usage
  [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{1,30}$ ]] || die "名字只要小写字母/数字/-，2~31 位，收到: ${NAME}"
fi
BEGIN="# >>> chat-subpath:${NAME} >>>"
END="# <<< chat-subpath:${NAME} <<<"

check(){
  case "$RESERVED" in *" ${NAME} "*) echo "STOP /${NAME} 是 Open WebUI 或已有段占用的路径，换个名字"; return 1;; esac
  local used
  used=$(remote "sudo -n grep -nE 'location[^/]*/${NAME}(/| |\\{|\$)' ${CONF}")
  if [ -n "$used" ]; then echo "STOP chat conf 里已有 /${NAME}："; echo "$used" | sed 's/^/     /'; return 1; fi
  echo "OK   /${NAME} 可用（https://${SITE}/${NAME}/）"
}

render(){ # $1=upstream $2=keep(0/1) $3=ws $4=body $5=timeout
  local up=$1 keep=$2 ws=$3 body=$4 to=$5
  echo "    ${BEGIN} ${up}（$(date '+%F') ${OPERATOR:-$USER}，scripts/host-198-chat-subpath.sh）。回滚=remove ${NAME}"
  echo "    location = /${NAME} { absolute_redirect off; return 302 /${NAME}/; }"
  echo "    location ^~ /${NAME}/ {"
  if [ "$keep" = 1 ]; then
    echo "        proxy_pass http://${up};"
  else
    echo "        proxy_pass http://${up}/;"
    echo "        # 服务回的根路径跳转 / cookie 补回前缀；替换串必须写全 https 域名（写相对路径 nginx 会补成 http://，对外 80 不通）"
    echo "        proxy_redirect ~^/(.*)\$ https://${SITE}/${NAME}/\$1;"
    echo "        proxy_cookie_path / /${NAME}/;"
  fi
  cat <<EOF
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix /${NAME};
EOF
  if [ "$ws" = 1 ]; then
    echo "        proxy_set_header Upgrade \$http_upgrade;"
    echo "        proxy_set_header Connection \"upgrade\";"
  fi
  cat <<EOF
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout ${to}s;
        proxy_send_timeout ${to}s;
        client_max_body_size ${body};
    }
    ${END}
EOF
}

add(){
  local up=${1:-}; shift || true
  local keep=0 ws=0 body=32m to=300 dry=0
  while [ $# -gt 0 ]; do case "$1" in
    --dry-run) dry=1;;
    --keep-prefix) keep=1;;
    --ws) ws=1;;
    --body) body=${2:?}; shift;;
    --timeout) to=${2:?}; shift;;
    *) die "未知参数 $1";; esac; shift; done
  [[ "$up" =~ ^(127\.0\.0\.1|10\.[0-9]+\.[0-9]+\.[0-9]+):[0-9]{2,5}$ ]] \
    || die "上游写成 127.0.0.1:<端口> 或 10.x.x.x:<端口>，收到: ${up:-空}"
  [[ "$body" =~ ^[0-9]+[kKmMgG]?$ ]] || die "--body 形如 32m"
  [[ "$to" =~ ^[0-9]+$ ]] || die "--timeout 是秒数"

  echo "== 前置检查"; check || exit 1
  echo "== 要插入的段"; render "$up" "$keep" "$ws" "$body" "$to"
  [ "$dry" = 1 ] && { echo "DRY-RUN 未改动 198"; return 0; }
  local b64; b64=$(render "$up" "$keep" "$ws" "$body" "$to" | base64 | tr -d '\n')

  echo "== 198 上安装"
  remote "CONF='${CONF}' UP='${up}' NAME='${NAME}' B64='${b64}' bash -s" <<'REMOTE'
set -uo pipefail
S="sudo -n"; BK=/etc/nginx/backups; TS=$(date +%Y%m%dT%H%M%S)
fail(){ echo "STOP: $*"; exit "${2:-1}"; }
code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${UP}/")
[ "${code}" = 000 ] && fail "上游 http://${UP} 从 198 连不上（000）⇒ 先把服务起来/确认端口"
echo "OK   上游 http://${UP} 可达（回 ${code}）"
$S mkdir -p "${BK}"; BAK="${BK}/chat.auto-link.com.cn.conf.${TS}.pre-${NAME}"
$S cp -a "${CONF}" "${BAK}" && echo "OK   备份 ${BAK}"
echo "${B64}" | base64 -d > /tmp/chat-subpath-blk.$$
$S python3 - "${CONF}" /tmp/chat-subpath-blk.$$ <<'PY' || { rm -f /tmp/chat-subpath-blk.$$; fail "插入失败（锚点不唯一），未改动"; }
import sys
p, b = sys.argv[1], sys.argv[2]
s = open(p).read(); blk = open(b).read()
a = '    location / {\n        proxy_pass http://open_webui_upstream;'
assert s.count(a) == 1, f"anchor count={s.count(a)}"
open(p, 'w').write(s.replace(a, blk + a))
PY
rm -f /tmp/chat-subpath-blk.$$
if ! $S nginx -t 2>&1; then
  $S cp -a "${BAK}" "${CONF}"
  $S nginx -t >/dev/null 2>&1 && echo "已回滚到 ${BAK}" || echo "🔴 回滚后 nginx -t 仍失败 ⇒ 不是本次改动的问题，别 reload，喊人"
  fail "nginx -t 失败，未 reload" 2
fi
$S nginx -s reload && echo "OK   nginx reload"
REMOTE
  local rc=$?
  [ $rc = 255 ] && die "ssh 失败" 4
  [ $rc = 0 ] || exit $rc

  verify "$up"
}

# 外网真实线路自检。reload 后旧 worker 还会用旧配置跑一阵 ⇒ 最多重试 60s 再判失败
verify(){
  local up=$1 i c body_t owui
  echo "== 外网自检（真实线路）"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 5
    body_t=$(curl -s -m 20 -w '\n%{http_code}' "https://${SITE}/${NAME}/")
    c=$(echo "$body_t" | tail -1)
    echo "$body_t" | grep -q '<title>Open WebUI' || break
    echo "    第 ${i} 次仍是 Open WebUI 页面（旧 worker 未退），再等 5s"
  done
  echo "    https://${SITE}/${NAME}/ → ${c}"
  # 不带斜杠的要在新 worker 生效后再测（放循环前测会被旧 worker 回成 Open WebUI 的 200）
  local c0; c0=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "https://${SITE}/${NAME}")
  echo "    https://${SITE}/${NAME}  → ${c0}（应 302 到带斜杠）"
  [ "$c0" = 302 ] || echo "WARN 不带斜杠的入口不是 302，用户少打斜杠会落到 Open WebUI"
  owui=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "https://${SITE}/api/version")
  echo "    对照 Open WebUI /api/version → ${owui}（应 200）"
  if [ "$owui" != 200 ]; then
    echo "🔴 Open WebUI 对照不是 200 ⇒ 自动回滚"; remove; die "已回滚 ${NAME}，Open WebUI 再测一次确认" 3
  fi
  echo "$body_t" | grep -q '<title>Open WebUI' && die "60s 后返回的仍是 Open WebUI 页面 ⇒ 段没生效，跑 list 看" 3
  case "${c}" in 000|502|504) die "外网 ${c} ⇒ 上游有问题，看 198 /var/log/nginx/error.log（段已装上，要撤用 remove）" 3;; esac
  echo "DONE https://${SITE}/${NAME}/ → ${up}"
}

# 端到端回归：198 上起临时 http.server → add → 外网取到内容 → remove → conf 与改前逐字节一致。改完本脚本必跑
selftest(){
  local port=18999 marker="csp-selftest-$RANDOM$RANDOM" before after
  NAME=zz-csp-selftest; BEGIN="# >>> chat-subpath:${NAME} >>>"; END="# <<< chat-subpath:${NAME} <<<"
  before=$(remote "sudo -n sha256sum ${CONF}" | awk '{print $1}')
  # 🔴 别用 pkill -f '<pattern>'：在 ssh 'cmd' 里它会匹配到远端 shell 自己的命令行，把本次 ssh 一起杀掉 ⇒ 后续清理不执行。用 pid 文件
  # 🔴 `a && b && nohup srv &` 里 & 作用于整条 && 链，子 shell 攥着 ssh 的 stdout ⇒ ssh 永不返回（10-29 卡死过一次）。
  #    后台进程必须单独一句、所有 fd 重定向（含 stdin </dev/null）
  remote "D=/tmp/${NAME}; mkdir -p \$D; echo '<title>${marker}</title>' > \$D/index.html
cd \$D; setsid python3 -m http.server ${port} --bind 127.0.0.1 </dev/null >/dev/null 2>&1 &
echo \$! > /tmp/${NAME}.pid; sleep 1"
  local rc=0
  add "127.0.0.1:${port}" || rc=$?
  if [ $rc = 0 ]; then
    curl -s -m 10 "https://${SITE}/${NAME}/" | grep -q "${marker}" && echo "OK   外网取到临时服务内容" || { echo "STOP 外网没取到 marker"; rc=3; }
    remove || rc=$?
  fi
  remote "kill \$(cat /tmp/${NAME}.pid) 2>/dev/null; rm -rf /tmp/${NAME} /tmp/${NAME}.pid"
  after=$(remote "sudo -n sha256sum ${CONF}" | awk '{print $1}')
  [ "$before" = "$after" ] && echo "OK   conf 与改前逐字节一致" || { echo "STOP conf sha 变了 ${before} → ${after}"; rc=3; }
  [ $rc = 0 ] && echo "SELFTEST PASS" || echo "SELFTEST FAIL rc=${rc}"
  return $rc
}

remove(){
  remote "CONF='${CONF}' NAME='${NAME}' bash -s" <<'REMOTE'
set -uo pipefail
S="sudo -n"; BK=/etc/nginx/backups; TS=$(date +%Y%m%dT%H%M%S)
$S grep -qF "# >>> chat-subpath:${NAME} >>>" "${CONF}" || { echo "STOP: 没有 chat-subpath:${NAME} 段（不是本脚本加的就手动处理）"; exit 1; }
$S mkdir -p "${BK}"; BAK="${BK}/chat.auto-link.com.cn.conf.${TS}.pre-remove-${NAME}"; $S cp -a "${CONF}" "${BAK}"
$S python3 - "${CONF}" "${NAME}" <<'PY'
import sys, re
p, n = sys.argv[1], sys.argv[2]
s = open(p).read()
pat = re.compile(r'^    # >>> chat-subpath:' + re.escape(n) + r' >>>.*?^    # <<< chat-subpath:' + re.escape(n) + r' <<<\n', re.S | re.M)
s2, k = pat.subn('', s)
assert k == 1, f"matched {k}"
open(p, 'w').write(s2)
PY
$S nginx -t 2>&1 || { $S cp -a "${BAK}" "${CONF}"; echo "STOP: nginx -t 失败，已恢复"; exit 2; }
$S nginx -s reload && echo "DONE 已删除 /${NAME}（备份 ${BAK}）"
REMOTE
}

list(){
  remote "sudo -n grep -nE '# >>> |location (=|\\^~) ' ${CONF}"
}

case "$CMD" in
  check) check;;
  add) shift 2; add "$@";;
  remove) remove;;
  list) list;;
  selftest) selftest;;
  *) usage;;
esac
