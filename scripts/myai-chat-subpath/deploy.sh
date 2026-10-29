#!/usr/bin/env bash
# myai 国内入口 https://chat.auto-link.com.cn/myai/ 的部署 / 校验 / 回滚。
#
# 链路: 58.241.5.230(IT,SSL 终结) → 198 nginx chat 站点 `# >>> myai-subpath >>>` 段 → 243:30810
#       → deploy/ai-token-dashboard-chat（k8s/myai-243-chat-subpath.yaml，同镜像同库，只改 APP_BASE_URL）
# myai.carher.net 那份全程不动 ⇒ 这是加法，两个入口可以并存。
#
# 用法:
#   HOST243_PW='...' scripts/myai-chat-subpath/deploy.sh apply      # 243 apply + 198 加 nginx 段 + Casdoor 加回调 + 外网验收
#   scripts/myai-chat-subpath/deploy.sh nginx                       # 只更新 198 上的 nginx 段（内容相同不动；不同整段替换）
#   scripts/myai-chat-subpath/deploy.sh verify                      # 只跑外网验收
#   HOST243_PW='...' scripts/myai-chat-subpath/deploy.sh rollback   # 三处全撤，myai.carher.net 不受影响
#
# 足迹 / 备份:
#   198  /etc/nginx/sites-enabled/chat.auto-link.com.cn.conf 加一段；改前备份 /etc/nginx/backup-myai/<ts>
#        （🔴 不能放 sites-enabled：include 是 *，备份会被一起加载）
#   198  Casdoor application admin/ai-token-dashboard 的 redirectUris 追加一条；改前整份 JSON 存 /etc/nginx/backup-myai/casdoor-app.<ts>.json
#   243  ns ai-token-dashboard 新增 deploy+svc ai-token-dashboard-chat（NodePort 30810）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
H198=cltx@10.68.13.198
CB="https://chat.auto-link.com.cn/myai/api/auth/callback"
ACTION="${1:-}"

r198(){ ssh -o BatchMode=yes -o ConnectTimeout=10 "$H198" "$@"; }

# Casdoor 管理口令沿用 skill owui-casdoor-sso §3 的内置 admin（只在 198 本机 30882 上用）
casdoor_py='
import json,sys,urllib.request,http.cookiejar,time
mode,cb,bak=sys.argv[1],sys.argv[2],sys.argv[3]
cj=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
B="http://127.0.0.1:30882"
def call(p,body=None):
    req=urllib.request.Request(B+p,data=None if body is None else json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    r=json.load(op.open(req,timeout=15)); return r
r=call("/api/login",{"application":"app-built-in","organization":"built-in","username":"admin","password":"admin@admin","type":"login","signinMethod":"Password"})
assert r.get("status")=="ok", r
app=call("/api/get-application?id=admin/ai-token-dashboard")["data"]
uris=list(app.get("redirectUris") or [])
assert "https://myai.carher.net/api/auth/callback" in uris, ("原有回调不在，停止", uris)
if mode=="add":
    if cb in uris: print("casdoor: 已有", cb); sys.exit(0)
    open(bak,"w").write(json.dumps(app,ensure_ascii=False))
    app["redirectUris"]=uris+[cb]
elif mode=="del":
    if cb not in uris: print("casdoor: 本来就没有", cb); sys.exit(0)
    open(bak,"w").write(json.dumps(app,ensure_ascii=False))
    app["redirectUris"]=[u for u in uris if u!=cb]
r=call("/api/update-application?id=admin/ai-token-dashboard",app); assert r.get("status")=="ok", r
after=call("/api/get-application?id=admin/ai-token-dashboard")["data"]["redirectUris"]
print("casdoor redirectUris:", after)
assert (cb in after)==(mode=="add") and set(uris)-{cb} <= set(after)
'

nginx_add(){
  B64=$(base64 < "$ROOT/scripts/myai-chat-subpath/nginx-block.conf" | tr -d '\n')
  r198 "B64=$B64 bash -s" <<'REMOTE'
set -euo pipefail
S="sudo -n"; CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
TS=$(date +%Y%m%d-%H%M%S); $S mkdir -p /etc/nginx/backup-myai
BLK=$(mktemp); printf %s "$B64" | base64 -d > "$BLK"
# 段已在：内容相同就不动；不同就整段替换（重跑 apply = 更新）
$S cp -a "$CONF" /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS
st=$($S python3 - "$CONF" "$BLK" <<'PY'
import sys,re
p,b=sys.argv[1],sys.argv[2]; s=open(p).read(); blk=open(b).read()
pat=re.compile(r'    # >>> myai-subpath >>>.*?    # <<< myai-subpath <<<\n',re.S)
old=pat.findall(s)
if len(old)>1: sys.exit("段出现了不止一次，停止")
if old:
    if old[0]==blk: print("same"); sys.exit(0)
    open(p,'w').write(pat.sub(lambda m: blk, s)); print("replaced"); sys.exit(0)
a='    location / {\n        proxy_pass http://open_webui_upstream;'
assert s.count(a)==1, "锚点不唯一/不存在"
open(p,'w').write(s.replace(a,blk+a)); print("added")
PY
)
rm -f "$BLK"
if [ "$st" = same ]; then $S rm -f /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS; echo "nginx: myai-subpath 段已是最新"; exit 0; fi
[ "$st" = added ] || [ "$st" = replaced ] || { echo "nginx: 改写失败 ($st)"; exit 1; }
if ! $S nginx -t 2>&1; then $S cp -a /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS "$CONF"; echo "nginx -t 失败，已还原"; exit 1; fi
$S systemctl reload nginx
echo "nginx: myai-subpath 段 ${st}，备份 /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS"
REMOTE
}

nginx_del(){
  r198 bash -s <<'REMOTE'
set -euo pipefail
S="sudo -n"; CONF=/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf
grep -q '>>> myai-subpath >>>' "$CONF" || { echo "nginx: 本来就没有"; exit 0; }
TS=$(date +%Y%m%d-%H%M%S); $S mkdir -p /etc/nginx/backup-myai
$S cp -a "$CONF" /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS.pre-rollback
$S python3 - "$CONF" <<'PY'
import sys,re
p=sys.argv[1]; s=open(p).read()
n=re.subn(r'    # >>> myai-subpath >>>.*?    # <<< myai-subpath <<<\n','',s,flags=re.S)
assert n[1]==1; open(p,'w').write(n[0])
PY
if ! $S nginx -t 2>&1; then $S cp -a /etc/nginx/backup-myai/chat.auto-link.com.cn.conf.$TS.pre-rollback "$CONF"; echo "nginx -t 失败，已还原"; exit 1; fi
$S systemctl reload nginx; echo "nginx: 已删 myai-subpath 段"
REMOTE
}

casdoor(){  # $1 = add|del
  local B64; B64=$(printf %s "$casdoor_py" | base64 | tr -d '\n')
  r198 "B64=$B64 MODE=$1 CB=$CB bash -s" <<'REMOTE'
set -uo pipefail
sudo -n mkdir -p /etc/nginx/backup-myai
P=$(mktemp); B=$(mktemp)   # mktemp 是 600：备份里有 clientSecret
printf %s "$B64" | base64 -d > "$P"
rc=0; python3 "$P" "$MODE" "$CB" "$B" || rc=$?
[ -s "$B" ] && sudo -n mv "$B" "/etc/nginx/backup-myai/casdoor-app.$(date +%Y%m%d-%H%M%S).json"
rm -f "$P" "$B"
exit $rc
REMOTE
}

verify(){
  # 🔴 这里不能 set -e：grep 数到 0 个时 rc=1，配合 pipefail 会让验收在"残留=0"这条最该绿的地方静默退出
  set +e
  local U=https://chat.auto-link.com.cn fail=0 W
  W=$(mktemp -d)
  chk(){ if [ "$2" = "$3" ]; then echo "OK    $1 ($2)"; else echo "FAIL  $1 实际=$2 期望=$3"; fail=1; fi; }
  cnt(){ grep -oE "$1" "$2" | wc -l | tr -d ' '; }
  chk "首页 /myai/"            "$(curl -sS -m 20 -o $W/i.html -w '%{http_code}' $U/myai/)" 200
  chk "/myai → /myai/"         "$(curl -sS -m 20 -o /dev/null -w '%{http_code} %{redirect_url}' $U/myai)" "302 $U/myai/"
  js=$(grep -oE 'src="/myai/assets/app\.js[^"]*"' $W/i.html | head -1 | cut -d'"' -f2)
  chk "首页里的 app.js 已改写成 /myai/assets"  "$([ -n "$js" ] && echo yes || echo no)" yes
  if [ -n "$js" ]; then
    curl -sS -m 30 --compressed -o $W/a.js "$U$js"
    chk "app.js 里没有残留的 \"/api 和 \`/api" "$(cnt '["`]/api[/?"`]' $W/a.js)" 0
    chk "app.js 已改写成 /myai/api 的处数（2818c0d 实测 86）" "$(cnt '["`]/myai/api' $W/a.js)" 86
    chk "app.js 经 gzip 下发" "$(curl -sS -m 30 -o /dev/null -D - -H 'Accept-Encoding: gzip' "$U$js" | grep -ci '^content-encoding: gzip' | tr -d ' ')" 1
  fi
  rm -rf "$W"
  chk "/myai/api/health"       "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/myai/api/health)" 200
  chk "未登录 /myai/api/auth/me" "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/myai/api/auth/me)" 401
  # OIDC_SKIP_CASDOOR_PAGE=true ⇒ Location 直接是飞书，myai 自己的 redirect_uri 藏在两层里：
  #   飞书 state（base64）里是 Casdoor 的 authorize 参数；session cookie 里存着 authlib 的 _state_*.redirect_uri
  # 两处都解出来比，只 grep Location 会永远读不到（10-19 第一版验收就这么假红过）
  hdr=$(curl -sS -m 20 -o /dev/null -D - $U/myai/api/auth/sso/start | tr -d '\r')
  got=$(printf %s "$hdr" | python3 -c '
import sys,re,base64,json,urllib.parse as up
h=sys.stdin.read(); out=set()
m=re.search(r"(?im)^location: (.*)$",h)
if m:
    st=up.parse_qs(up.urlparse(m.group(1)).query).get("state",[""])[0]
    try:
        inner=base64.urlsafe_b64decode(st+"="*(-len(st)%4)).decode()
        out.update(up.parse_qs(inner.lstrip("?")).get("redirect_uri",[]))
    except Exception: pass
c=re.search(r"(?im)^set-cookie: ai_token_dashboard_session=([^.;]+)",h)
if c:
    s=c.group(1); d=json.loads(base64.urlsafe_b64decode(s+"="*(-len(s)%4)))
    out.update(v["data"]["redirect_uri"] for k,v in d.items() if k.startswith("_state_"))
print(" ".join(sorted(out)))')
  chk "SSO 的 redirect_uri（state + cookie 两处）" "$got" "$CB"
  chk "session cookie Path=/myai/" "$(printf %s "$hdr" | grep -i '^set-cookie:' | grep -ci 'path=/myai/' | tr -d ' ')" 1
  # 登录回调最后那一跳：后端回 "/?auth_callback=success"，必须变成 https 的 /myai/ 绝对地址。
  # 没有 code 时回调会报错页而不是跳转，所以这里只能看 nginx 配置里的替换串（桩测见 skill）
  chk "回调跳转改写成 https 绝对地址" "$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$H198" "grep -c 'proxy_redirect ~^/(.\*)\$ https://chat.auto-link.com.cn/myai/\$1;' /etc/nginx/sites-enabled/chat.auto-link.com.cn.conf")" 1
  # 旁路回归：同一 server 块里的其它服务
  chk "旁路 Open WebUI /api/version" "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/api/version)" 200
  chk "旁路 /oneclick/"        "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/oneclick/)" 200
  chk "旁路 /feedback/"        "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/feedback/)" 200
  chk "旁路 /keyprobe/"        "$(curl -sS -m 20 -o /dev/null -w '%{http_code}' $U/keyprobe/)" 200
  # 2026-10-29 起 myai.carher.net 已下线（主看板 + cloudflared 缩到 0），只认本入口；这里确认它没被意外拉起
  chk "老入口 myai.carher.net 已下线（主看板副本数）" "$(SSHPASS="${HOST243_PW:-}" sshpass -e ssh -o ConnectTimeout=15 -o ClearAllForwardings=yes cltx@10.68.13.243 'kubectl -n ai-token-dashboard get deploy ai-token-dashboard -o jsonpath={.spec.replicas}' 2>/dev/null || echo '?')" 0
  [ $fail = 0 ] && echo "=== ALL OK" || echo "=== 有 FAIL"
  return $fail
}

case "$ACTION" in
  apply)
    : "${HOST243_PW:?需要 HOST243_PW}"
    SSHPASS="$HOST243_PW" sshpass -e scp -q -o StrictHostKeyChecking=no "$ROOT/k8s/myai-243-chat-subpath.yaml" cltx@10.68.13.243:/tmp/myai-chat-lgx.yaml
    "$ROOT/scripts/host-243-run.sh" - <<'EOF'
k3s kubectl apply -f /tmp/myai-chat-lgx.yaml && rm -f /tmp/myai-chat-lgx.yaml
k3s kubectl -n ai-token-dashboard rollout status deploy/ai-token-dashboard-chat --timeout=300s
EOF
    r198 'curl -fsS -m 5 -o /dev/null http://10.68.13.243:30810/api/health && echo "198→243:30810 ok"'
    casdoor add
    nginx_add
    verify ;;
  nginx)  nginx_add && verify ;;   # 只更新 198 上那段 nginx（改了 nginx-block.conf 之后用）
  verify) verify ;;
  rollback)
    : "${HOST243_PW:?需要 HOST243_PW}"
    # 🔴 2026-10-29 起这是唯一入口，回滚 = myai 全站不可用。确需回滚先恢复老入口：
    #   243: kubectl -n ai-token-dashboard apply -f /Data/migration/myai-retire-carher-net-20261029T0707/deploys.yaml
    [ "${I_KNOW_THIS_IS_THE_ONLY_ENTRY:-}" = 1 ] || { echo "这是唯一入口，回滚会让 myai 全站不可用；确认请设 I_KNOW_THIS_IS_THE_ONLY_ENTRY=1" >&2; exit 2; }
    nginx_del
    casdoor del
    "$ROOT/scripts/host-243-run.sh" - <<'EOF'
k3s kubectl -n ai-token-dashboard delete deploy/ai-token-dashboard-chat svc/ai-token-dashboard-chat --ignore-not-found
EOF
    code=$(curl -sS -m 30 -o /dev/null -w '%{http_code}' https://myai.carher.net/api/health); echo "myai.carher.net health=$code" ;;
  *) echo "用法: $0 apply|verify|rollback" >&2; exit 2 ;;
esac
