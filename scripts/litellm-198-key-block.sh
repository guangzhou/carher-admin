#!/usr/bin/env bash
# litellm-198-key-block.sh
#
# 在 198（ns litellm-product 前面那层 nginx）按 **API key** 封禁滥用流量，
# 以及找出「只知道 key hash」时的真实客户端 IP / key 明文。
#
# 为什么不按 IP 封（2026-09-07 实证，三个坑）：
#   1. 198 的外部流量路径是
#        客户端 → 443 网关 58.241.5.230 → 内网前置代理 10.68.13.97 → 198:80 nginx → litellm
#      198 看到的 TCP 源地址**恒为 10.68.13.97**。iptables 按客户端 IP 封是空转；
#      唯一能封的 10.68.13.97 = 掐断全部用户。
#   2. nginx 按 $http_x_forwarded_for 封可行，但用户**大量共用一个出口 IP**（共享 NAT），
#      一封误伤一片。
#   3. **IP 会轮换**：那台 Codex Desktop 一小时内 58.242.232.152 → 220.180.208.237
#      （中间约 5 分钟静默）。IP 规则此时双向失效：放过真凶 + 继续封无辜的旧 IP。
#   → 结论：按 key 封，与来源 IP 无关。
#
# 用法（在本地 Mac 跑，脚本自己 ssh 进 198）：
#   ./litellm-198-key-block.sh probe   [--seconds 30] [--hash <sha256>] [--uri-grep <str>]
#   ./litellm-198-key-block.sh list
#   ./litellm-198-key-block.sh block   <sk-key> [--reason "文字"]
#   ./litellm-198-key-block.sh verify  <sk-key> [--control-key <活key>]
#   ./litellm-198-key-block.sh stats   [--minutes 15] [--since HH:MM]
#   ./litellm-198-key-block.sh unblock <sk-key | 标签(如 jwt-class)>
#   ./litellm-198-key-block.sh block-jwt              # 封「拿 OAuth/JWT 当 key」这一整类
#   ./litellm-198-key-block.sh verify-jwt [--control-key <活key>]
#   ./litellm-198-key-block.sh block-tail <key尾4~12位> [--reason "文字"]
#   ./litellm-198-key-block.sh watch   --hash <sha256> [--minutes 60]
#
# 三种封法怎么选：
#   - 对方一直用**同一把** sk- key，且抓到了明文 → `block <key>`（精确匹配，首选）
#   - 抓不到明文（**突发型**：打一分钟就停，probe 赶不上）→ `block-tail <尾巴>`
#     （库里 `key_name` 存着 `sk-...<尾4位>`；带碰撞门；是**临时刀**）
#     同时 `watch --hash <h>` 后台守着，抓到明文后换成 `block <明文>` 再 `unblock tail-<尾巴>`
#   - 对方的凭据**会换**（JWT/OAuth token 短命，每次都是新 hash）→ 按**形状**封，见 `block-jwt`
#
# 典型闭环（只有 LiteLLM UI 上那串 hash 时）：
#   1) probe --hash <hash>    # 抓包拿 key 明文 + 真源 IP + UA + URI
#   2) block <key>            # 备份 → 插规则 → nginx -t → reload → 自检
#   3) verify <key> --control-key <同IP某个活key>   # 四格回归 + litellm 侧归零
#   4) stats                  # 逐分钟看拦截量；别把几分钟静默当收工（客户端会退避/换 IP）
#
# 硬纪律：
#   - 198 禁 kubectl apply（manifest 陈旧会回退 image + 内嵌 CM）——本脚本只碰 nginx。
#   - nginx 只 `-s reload`，**禁 restart**；每次改动前 cp -a 备份到 /root/。
#   - 判 403 是本规则拦的还是上游 litellm 回的：zkreq 日志第 6 列 $upstream_response_time，
#     nginx 自返为 "-"，上游回的有耗时。不切这一刀会把上游 403 当成自己的误伤。
#   - tcpdump 的 -s 必须 >= 2000，1400 会把 Authorization 头截掉（看起来"没人用这把 key"）。
#
# 实现约定：所有远端 payload 都用**带引号的 heredoc**，参数走位置参数传进去。
# 早期版本用不带引号的 heredoc，本地 shell 会把 nginx 的 $http_authorization
# 当自己的变量吃掉（set -u 下直接 unbound variable）。别改回去。

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DIRECT_HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"
JMS_ASSET="${LITELLM_198_JMS_ASSET:-AIYJY-litellm}"
JMS_BIN="${JMS_BIN:-$SCRIPT_DIR/jms}"
CONF="${LITELLM_198_NGINX_CONF:-/etc/nginx/sites-enabled/cc.auto-link.com.cn.conf}"
ZKLOG="${LITELLM_198_ZKLOG:-/var/log/nginx/zkreq.log}"
NS="${LITELLM_198_NS:-litellm-product}"
IFACE="${LITELLM_198_IFACE:-any}"

die() { echo "FATAL: $*" >&2; exit 2; }

# 远端执行：stdin 收一段 bash，剩余参数作为该段 bash 的 $1 $2 ...
# direct ssh 优先，rc=255（连不上）时退到 jms。
remote() {
  local payload argstr=""
  payload=$(cat)
  if [ "$#" -gt 0 ]; then argstr=$(printf ' %q' "$@"); fi
  set +e
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=no \
      "$DIRECT_HOST" "sudo -n bash -s --$argstr" <<<"$payload"
  local rc=$?
  set -e
  if [ "$rc" -ne 255 ]; then return "$rc"; fi
  echo "direct ssh unavailable; fallback: jms $JMS_ASSET" >&2
  [ -x "$JMS_BIN" ] || die "jms not executable: $JMS_BIN"
  "$JMS_BIN" ssh "$JMS_ASSET" "sudo -n bash -s --$argstr" <<<"$payload"
}

# key 白名单校验：只允许 sk- 开头的 base64url 字符集。
# 这同时保证 key 塞进 nginx 正则 / shell 时没有元字符需要转义。
validate_key() {
  local k="$1"
  [[ "$k" =~ ^sk-[A-Za-z0-9_-]+$ ]] || die "key 形状不合法（只允许 ^sk-[A-Za-z0-9_-]+\$）: $k"
}

# LiteLLM 的 key hash = key 明文的裸 sha256（2026-09-07 实证）。
# 拿 hash 可以**验证**候选 key；反推不出明文，明文只能抓包。
key_hash() {
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  else
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  fi
}

usage() {
  sed -n '2,54p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

# ---------------------------------------------------------------- probe
# 抓 198:80 的明文回源（443 在网关终结，所以到 198 是明文，头能直接读），
# 把同一个包里的 Authorization / X-Real-IP / User-Agent / URI 配对聚合。
cmd_probe() {
  local seconds=30 want_hash="" uri_grep=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --seconds)  seconds="$2"; shift 2;;
      --hash)     want_hash="$2"; shift 2;;
      --uri-grep) uri_grep="$2"; shift 2;;
      *) die "probe: 未知参数 $1";;
    esac
  done
  # 注意 ${IFACE} 必须带花括号：紧跟全角逗号时 bash 会把多字节逗号吃进变量名，
  # set -u 下直接报 `IFACE，: unbound variable`。中文文案里的变量一律带花括号。
  echo "== probe: 抓 ${seconds}s，interface=${IFACE}，snaplen=2000（<2000 会截断 Authorization）"
  [ -n "$want_hash" ] && echo "== 只报 sha256 == $want_hash 的 key"
  remote "$seconds" "$IFACE" "$ZKLOG" "$want_hash" "$uri_grep" <<'REMOTE'
set -euo pipefail
SECONDS_ARG="$1"; IFACE="$2"; ZKLOG="$3"; WANT_HASH="$4"; URI_GREP="$5"
RAW=$(mktemp /tmp/litellm-198-key-probe.XXXXXX)
trap 'rm -f "$RAW"' EXIT
echo "log 末尾时间戳 (抓包前): $(tail -1 "$ZKLOG" | cut -d'|' -f1)"
timeout "$SECONDS_ARG" tcpdump -i "$IFACE" -A -s 2000 -nn 'tcp dst port 80' > "$RAW" 2>/dev/null || true
echo "log 末尾时间戳 (抓包后): $(tail -1 "$ZKLOG" | cut -d'|' -f1)"
echo "抓到原始行数: $(wc -l < "$RAW")"
python3 - "$RAW" "$WANT_HASH" "$URI_GREP" <<'PY'
import sys, re, hashlib, collections
raw, want_hash, uri_grep = sys.argv[1], sys.argv[2], sys.argv[3]
PKT = re.compile(r"^\d\d:\d\d:\d\d\.\d+ ")
agg = collections.Counter()
blk = []

def flush(block):
    b = "".join(block)
    if uri_grep and uri_grep not in b:
        return
    keys = re.findall(r"[Aa]uthorization:\s*Bearer\s+(\S+)", b)
    if not keys:
        return
    key = keys[0]
    h = hashlib.sha256(key.encode()).hexdigest()
    if want_hash and h != want_hash:
        return
    ip = re.search(r"[Xx]-[Rr]eal-[Ii][Pp]:\s*([\d.]+)", b)
    xff = re.search(r"[Xx]-[Ff]orwarded-[Ff]or:\s*([^\r\n]+)", b)
    ua = re.search(r"[Uu]ser-[Aa]gent:\s*([^\r\n]{0,60})", b)
    uri = re.search(r"^(?:GET|POST|PUT|DELETE|PATCH)\s+(\S+)", b, re.M)
    agg[(ip.group(1) if ip else (xff.group(1).strip() if xff else "?"),
         key, h[:12],
         ua.group(1).strip() if ua else "?",
         uri.group(1) if uri else "?")] += 1

for line in open(raw, errors="replace"):
    if PKT.match(line):
        flush(blk); blk = []
    blk.append(line)
flush(blk)

if not agg:
    print("!! 0 命中。可能原因：(a) 客户端正在退避/换 IP 的静默窗口——别当收工，隔几分钟重抓；")
    print("   (b) --hash 写错；(c) 请求头分散在多个包里。先不带 --hash 跑一遍看有没有流量。")
    sys.exit(0)
print()
print("%6s  %-16s  %-14s  %-30s  %s" % ("次数", "客户端IP(XFF)", "key hash前12", "User-Agent", "URI"))
for (ip, key, h12, ua, uri), n in agg.most_common(25):
    print("%6d  %-16s  %-14s  %-30s  %s" % (n, ip, h12, ua[:30], uri[:60]))
print()
print("key 明文（拿去 block）：")
for key in sorted({k for (_, k, _, _, _) in agg}):
    print("  %s   sha256=%s" % (key, hashlib.sha256(key.encode()).hexdigest()))
PY
REMOTE
}

# ---------------------------------------------------------------- list
cmd_list() {
  remote "$CONF" <<'REMOTE'
set -euo pipefail
CONF="$1"
echo "== $CONF 里已装的 key 封禁块 =="
grep -n 'litellm-198-key-block' "$CONF" || echo "(无 managed 封禁块)"
echo
echo "== 兜底：managed 块**之外**的手工 if 规则（应为空，否则是脚本管不到的遗留） =="
# managed 块内部当然有这些 if，不能算遗留 —— 用 BEGIN/END 状态机排掉。
awk '/# >>> litellm-198-key-block .* BEGIN/ {inblk=1}
     /# <<< litellm-198-key-block .* END/   {inblk=0; next}
     !inblk && /http_authorization|arg_api_key|http_x_forwarded_for/ {print FNR": "$0; found=1}
     END {if (!found) print "(无)"}' "$CONF"
REMOTE
}

# ---------------------------------------------------------------- block
cmd_block() {
  local key="${1:-}"; shift || true
  [ -n "$key" ] || usage
  validate_key "$key"
  local reason="revoked key abuse"
  while [ $# -gt 0 ]; do
    case "$1" in
      --reason) reason="$2"; shift 2;;
      *) die "block: 未知参数 $1";;
    esac
  done
  local h; h=$(key_hash "$key")
  echo "== block key=$key"
  echo "== sha256=$h"
  remote "$CONF" "$key" "${h:0:12}" "$reason" <<'REMOTE'
set -euo pipefail
CONF="$1"; KEY="$2"; H12="$3"; REASON="$4"
TS=$(date +%Y%m%d-%H%M%S)
BAK="/root/nginx-backup-cc.conf.$TS"
cp -a "$CONF" "$BAK"
echo "备份: $BAK"

python3 - "$CONF" "$KEY" "$H12" "$REASON" "$(date +%F)" <<'PY'
import sys, re
conf, key, h12, reason, today = sys.argv[1:6]
s = open(conf).read()

BEGIN = "    # >>> litellm-198-key-block %s BEGIN\n" % h12
END   = "    # <<< litellm-198-key-block %s END\n" % h12

# 1) 幂等 + 就地重装：先删掉本 key 已有的 managed 块
if BEGIN in s and END in s:
    i, j = s.index(BEGIN), s.index(END) + len(END)
    s = s[:i] + s[j:]
    print("已存在 managed 块 -> 就地重装")

# 2) 收养手工遗留：删掉任何提到这把 key 的 if 块 + 紧邻其上的注释行。
#    key 出现在 **if 的条件里**（$http_authorization ~ "sk-..."），不在 {} 里 ——
#    早期版本把 key 写进 [^}]* 那段，永远匹配不上，于是手工规则和 managed 块并存。
pat = re.compile(r"(?:[ \t]*#[^\n]*\n)*[ \t]*if \([^)]*%s[^)]*\)[ \t]*\{[^{}]*\}\n"
                 % re.escape(key))
n_adopt = len(pat.findall(s))
if n_adopt:
    s = pat.sub("", s)
    print("收养并替换手工 if 规则 %d 处（含其上注释）" % n_adopt)

marker = "    server_name cc.auto-link.com.cn;\n"
if s.count(marker) != 1:
    sys.exit("FATAL: server_name 锚点不唯一（%d 次），拒绝改" % s.count(marker))

rule = BEGIN + """    # %s  原因：%s
    # 已吊销的 key %s（sha256 %s...）在被死循环重打。
    # 按 key 封而不按 IP 封：用户共用出口 IP（误伤一片）且 IP 会轮换（放过真凶）。
    # 两条都要：有客户端把 key 塞在 query 里（?api_key=）。
    # 装/卸都走 scripts/litellm-198-key-block.sh，别手改（手改的块脚本管不到）。
    if ($http_authorization ~ "%s") {
        return 403 "blocked: revoked api key\\n";
    }
    if ($arg_api_key = "%s") {
        return 403 "blocked: revoked api key\\n";
    }
""" % (today, reason, key, h12, key, key) + END

s = s.replace(marker, marker + "\n" + rule, 1)
open(conf, "w").write(s)
print("已插入 managed 封禁块 %s" % h12)
PY

nginx -t
nginx -s reload
sleep 1
echo "== nginx reloaded（未 restart）"

echo "== 自检：带该 key 打 /pro/v1/models"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: cc.auto-link.com.cn' \
        -H "Authorization: Bearer $KEY" http://127.0.0.1/pro/v1/models)
echo "  期望 403，实得 $CODE"
[ "$CODE" = "403" ] || { echo "!! 自检失败，请查 $CONF"; exit 1; }
REMOTE
  echo "== 建议接着跑: $0 verify $key --control-key <同IP某个活key>"
}

# ---------------------------------------------------------------- block-jwt
# 封「拿 OAuth/JWT 当 virtual key」这一整类，而不是封某个 token。
# 为什么不封 token 本身：每个 JWT 都是短命的 —— 2026-09-07 查库，7 天里 73 个不同 token、
# 162 行**全部 failure、0 成功**，封住一个 hash 换个 token 就是新 hash。
# 为什么整类封是零损失：198 的 config.yaml 没有任何 jwt 配置（`enable_jwt_auth` 没开），
# JWT 结构性不可能通过鉴权，这类请求本来 100% 拿 401。
cmd_block_jwt() {
  echo "== 安装 jwt-class 封禁块（三条件 AND）"
  remote "$CONF" "$(date +%F)" <<'REMOTE'
set -euo pipefail
CONF="$1"; TODAY="$2"
TS=$(date +%Y%m%d-%H%M%S)
BAK="/root/nginx-backup-cc.conf.$TS"
cp -a "$CONF" "$BAK"
echo "备份: $BAK"

python3 - "$CONF" "$TODAY" <<'PY'
import sys
conf, today = sys.argv[1], sys.argv[2]
s = open(conf).read()
BEGIN = "    # >>> litellm-198-key-block jwt-class BEGIN\n"
END   = "    # <<< litellm-198-key-block jwt-class END\n"
if BEGIN in s and END in s:
    i, j = s.index(BEGIN), s.index(END) + len(END)
    s = s[:i] + s[j:]
    print("已存在 jwt-class 块 -> 就地重装")

marker = "    server_name cc.auto-link.com.cn;\n"
if s.count(marker) != 1:
    sys.exit("FATAL: server_name 锚点不唯一（%d 次），拒绝改" % s.count(marker))

rule = BEGIN + """    # %s  拿 ChatGPT OAuth token(JWT)当 Bearer 打 /pro/v1/* 的客户端（多为 Codex Desktop
    # 用 OAuth 登录、没配 sk- key）。LiteLLM 未开 enable_jwt_auth，这类请求 100%% 401
    # （全历史 162 行 / 73 个不同 token / 0 成功），所以在这里返 403 是零损失。
    # 封 token 本身没有意义：每个 JWT 都短命，换一个就是新 hash。
    #
    # 三个条件必须同时满足，第 ③ 条是为了**不误伤管理 UI** ——
    # UI 前端包里确实有 /v1/models、/v1/chat/completions、/v1/responses（实测 grep 到），
    # 只按 path+JWT 两条件会把 UI 的模型下拉/Test Key 打瘸。
    # 浏览器一定发 Sec-Fetch-*，原生客户端一定不发，用它把两者分开。
    set $blk_jwt "";
    if ($http_authorization ~ "^Bearer eyJ")  { set $blk_jwt "${blk_jwt}A"; }   # ① 是 JWT
    if ($request_uri ~ "^/(pro|dev|stg)/v1/") { set $blk_jwt "${blk_jwt}B"; }   # ② 打推理面
    if ($http_sec_fetch_mode = "")            { set $blk_jwt "${blk_jwt}C"; }   # ③ 不是浏览器
    if ($blk_jwt = "ABC") {
        return 403 "blocked: OAuth/JWT token is not a LiteLLM virtual key; configure your sk-... key\\n";
    }
""" % today + END

s = s.replace(marker, marker + "\n" + rule, 1)
open(conf, "w").write(s)
print("已插入 managed 块 jwt-class")
PY

nginx -t
nginx -s reload
sleep 1
echo "== nginx reloaded（未 restart）"
REMOTE
  echo "== 建议接着跑: $0 verify-jwt --control-key <某个活key>"
}

# ---------------------------------------------------------------- verify-jwt
# 四格：原生客户端带 JWT 打推理面必 403；**同一个 JWT 加上浏览器头必须放行**（UI 不误伤）；
# JWT 打 /pro/ui/ 必须放行；活 sk- key 必须 200。
cmd_verify_jwt() {
  local control=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --control-key) control="$2"; validate_key "$2"; shift 2;;
      *) die "verify-jwt: 未知参数 $1";;
    esac
  done
  remote "$control" <<'REMOTE'
set -euo pipefail
CONTROL="$1"
H='Host: cc.auto-link.com.cn'
# 一个语法合法但签名无效的 JWT，只用来触发形状匹配，不携带任何真实凭据
J='eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJwcm9iZSJ9.c2lnbmF0dXJlLXBsYWNlaG9sZGVy'
row() { printf '  %-34s -> %s\n' "$1" "$2"; }
hit() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
echo "== jwt-class 四格回归"
row "JWT + /pro/v1/models（原生客户端）" \
    "$(hit -H "$H" -H "Authorization: Bearer $J" http://127.0.0.1/pro/v1/models)  期望 403"
row "同一 JWT + 浏览器头（UI 判据）" \
    "$(hit -H "$H" -H "Authorization: Bearer $J" -H 'Sec-Fetch-Mode: cors' -H 'Sec-Fetch-Site: same-origin' http://127.0.0.1/pro/v1/models)  期望非 403"
row "JWT + /pro/ui/（UI 判据）" \
    "$(hit -H "$H" -H "Authorization: Bearer $J" http://127.0.0.1/pro/ui/)  期望非 403"
if [ -n "$CONTROL" ]; then
  row "对照活 sk- key（零误伤判据）" \
      "$(hit -H "$H" -H "Authorization: Bearer $CONTROL" http://127.0.0.1/pro/v1/models)  期望 200"
else
  echo "  !! 没给 --control-key：**误伤这一格没测**，别宣布零误伤"
fi
REMOTE
}

# ---------------------------------------------------------------- db helper
# 远端 payload 里可 source 的一段：定义 db_q "<SQL>"。
# 口令从 litellm pod 的 DATABASE_URL 里拆，psql 在 litellm-db pod 里跑。
DB_HELPER=$(cat <<'EOF'
_POD=$(kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
_U=$(kubectl -n litellm-product exec "$_POD" -- printenv DATABASE_URL)
_USER=$(echo "$_U" | sed -E 's#^postgresql://([^:]+):.*#\1#')
_PASS=$(echo "$_U" | sed -E 's#^postgresql://[^:]+:([^@]+)@.*#\1#')
_DBPOD=$(kubectl -n litellm-product get pods -l app=litellm-db -o jsonpath='{.items[0].metadata.name}')
db_q() { kubectl -n litellm-product exec "$_DBPOD" -- env PGPASSWORD="$_PASS" \
           psql -U "$_USER" -d litellm -At -F'|' -c "$1"; }
EOF
)

# ---------------------------------------------------------------- block-tail
# 只知道 hash、抓不到明文时的封法：按 key **尾几位**封。
# LiteLLM 的 `LiteLLM_VerificationToken.key_name` 存成 `sk-...<尾4位>`，
# 所以尾巴是能从库里读到的，而明文永远读不到（只存 sha256）。
#
# ⚠️ 这是有碰撞风险的封法，必须过碰撞门：库里以该尾巴结尾的 key **只能有 1 把**，
#    否则拒绝安装。**将来**新建的 key 仍有约 1/14.7M 的概率撞上（1866 把 key ≈ 0.013%），
#    所以这是**临时刀**：一旦 watch 抓到明文，就 `block <明文>` 然后 `unblock tail-<尾巴>`
#    换成精确匹配（先加后撤，别一把换）。
cmd_block_tail() {
  local tail="${1:-}"; shift || true
  [ -n "$tail" ] || usage
  [[ "$tail" =~ ^[A-Za-z0-9_-]{4,12}$ ]] || die "尾巴形状不合法（4~12 位 base64url）: $tail"
  local reason="expired/revoked key still retrying"
  while [ $# -gt 0 ]; do
    case "$1" in
      --reason) reason="$2"; shift 2;;
      *) die "block-tail: 未知参数 $1";;
    esac
  done
  echo "== block-tail $tail"
  remote "$CONF" "$tail" "$reason" "$(date +%F)" "$DB_HELPER" <<'REMOTE'
set -euo pipefail
CONF="$1"; TAIL="$2"; REASON="$3"; TODAY="$4"
eval "$5"

echo "== 碰撞门：库里 key_name 以 $TAIL 结尾的 key"
N=$(db_q "select count(*) from \"LiteLLM_VerificationToken\" where key_name like '%$TAIL'")
TOTAL=$(db_q "select count(*) from \"LiteLLM_VerificationToken\"")
echo "   命中 $N 把 / 全库 $TOTAL 把"
db_q "select token, key_name, coalesce(key_alias,'-'), expires from \"LiteLLM_VerificationToken\" where key_name like '%$TAIL'"
if [ "$N" != "1" ]; then
  echo "!! 碰撞门不通过（需恰好 1 把，实得 $N）——拒绝安装，会误伤别人的 key"
  exit 3
fi

TS=$(date +%Y%m%d-%H%M%S)
BAK="/root/nginx-backup-cc.conf.$TS"
cp -a "$CONF" "$BAK"
echo "备份: $BAK"

python3 - "$CONF" "$TAIL" "$REASON" "$TODAY" "$TOTAL" <<'PY'
import sys
conf, tail, reason, today, total = sys.argv[1:6]
s = open(conf).read()
BEGIN = "    # >>> litellm-198-key-block tail-%s BEGIN\n" % tail
END   = "    # <<< litellm-198-key-block tail-%s END\n" % tail
if BEGIN in s and END in s:
    i, j = s.index(BEGIN), s.index(END) + len(END)
    s = s[:i] + s[j:]
    print("已存在 tail-%s 块 -> 就地重装" % tail)

marker = "    server_name cc.auto-link.com.cn;\n"
if s.count(marker) != 1:
    sys.exit("FATAL: server_name 锚点不唯一（%d 次），拒绝改" % s.count(marker))

rule = BEGIN + """    # %s  原因：%s
    # 按 key **尾 %d 位**封，因为抓不到明文：LiteLLM 只存 sha256，
    # 能读到的只有 key_name = `sk-...%s`。安装时过了碰撞门（全库 %s 把 key 里只有 1 把以此结尾）。
    # ⚠️ 临时刀：将来新建的 key 仍可能撞上（~1/14.7M/把）。
    #    watch 抓到明文后请 `block <明文>` 再 `unblock tail-%s`，换成精确匹配。
    if ($http_authorization ~ "^Bearer sk-[A-Za-z0-9_-]*%s$") {
        return 403 "blocked: expired api key\\n";
    }
    if ($arg_api_key ~ "^sk-[A-Za-z0-9_-]*%s$") {
        return 403 "blocked: expired api key\\n";
    }
""" % (today, reason, len(tail), tail, total, tail, tail, tail) + END

s = s.replace(marker, marker + "\n" + rule, 1)
open(conf, "w").write(s)
print("已插入 managed 块 tail-%s" % tail)
PY

nginx -t
nginx -s reload
sleep 1
echo "== nginx reloaded（未 restart）"

echo "== 自检：阳性（尾巴相同的合成 key）/ 阴性（差一位）"
H='Host: cc.auto-link.com.cn'
POS=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" -H "Authorization: Bearer sk-selftest0000$TAIL" http://127.0.0.1/pro/v1/models)
NEG=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" -H "Authorization: Bearer sk-selftest0000${TAIL}z" http://127.0.0.1/pro/v1/models)
echo "  阳性(应 403): $POS    阴性(应非 403): $NEG"
[ "$POS" = "403" ] || { echo "!! 阳性对照没亮，规则没生效"; exit 1; }
[ "$NEG" != "403" ] || { echo "!! 阴性对照也被封了，正则过宽"; exit 1; }
REMOTE
}

# ---------------------------------------------------------------- watch
# 突发型流量（打一分钟就停）用 probe 抓不到。watch 在 198 后台常驻一段时间，
# 只要该 hash 的 key 再出现一次，就把**明文**记到 /root/key-watch-<h12>.log。
cmd_watch() {
  local want_hash="" minutes=60
  while [ $# -gt 0 ]; do
    case "$1" in
      --hash)    want_hash="$2"; shift 2;;
      --minutes) minutes="$2"; shift 2;;
      *) die "watch: 未知参数 $1";;
    esac
  done
  [ -n "$want_hash" ] || die "watch 必须给 --hash"
  echo "== 在 198 后台守 ${minutes} 分钟，等该 hash 再出现一次就记下明文"
  remote "$want_hash" "$minutes" "$IFACE" <<'REMOTE'
set -euo pipefail
WANT="$1"; MIN="$2"; IFACE="$3"
H12=${WANT:0:12}
OUT="/root/key-watch-$H12.log"
RUNNER="/root/key-watch-$H12.sh"
cat > "$RUNNER" <<'INNER'
#!/bin/bash
WANT="$1"; MIN="$2"; IFACE="$3"; OUT="$4"
RAW=$(mktemp /tmp/key-watch.XXXXXX)
trap 'rm -f "$RAW"' EXIT
timeout $((MIN*60)) tcpdump -i "$IFACE" -A -s 2000 -nn 'tcp dst port 80' > "$RAW" 2>/dev/null || true
python3 - "$RAW" "$WANT" >> "$OUT" 2>&1 <<'PY'
import sys, re, hashlib, datetime
raw, want = sys.argv[1], sys.argv[2]
PKT = re.compile(r"^\d\d:\d\d:\d\d\.\d+ ")
seen = {}
blk = []
def flush(b):
    b = "".join(b)
    m = re.search(r"[Aa]uthorization:\s*Bearer\s+(\S+)", b)
    if not m: return
    tok = m.group(1)
    if hashlib.sha256(tok.encode()).hexdigest() != want: return
    ip  = re.search(r"[Xx]-[Rr]eal-[Ii][Pp]:\s*([\d.]+)", b)
    ua  = re.search(r"[Uu]ser-[Aa]gent:\s*([^\r\n]{0,60})", b)
    uri = re.search(r"^(?:GET|POST|PUT|DELETE|PATCH)\s+(\S+)", b, re.M)
    k = (tok, ip.group(1) if ip else "?", ua.group(1).strip() if ua else "?",
         uri.group(1) if uri else "?")
    seen[k] = seen.get(k, 0) + 1
for line in open(raw, errors="replace"):
    if PKT.match(line): flush(blk); blk = []
    blk.append(line)
flush(blk)
ts = datetime.datetime.now().isoformat(timespec="seconds")
if not seen:
    print("%s  窗口内 0 命中（不代表它停了，可能只是没赶上突发）" % ts)
for (tok, ip, ua, uri), n in seen.items():
    print("%s  命中 %d 次  明文=%s  IP=%s  UA=%s  URI=%s" % (ts, n, tok, ip, ua, uri))
PY
INNER
chmod +x "$RUNNER"
pkill -f "key-watch-$H12.sh" 2>/dev/null || true
nohup "$RUNNER" "$WANT" "$MIN" "$IFACE" "$OUT" >/dev/null 2>&1 &
sleep 1
echo "已起后台 watcher，pid=$(pgrep -f "key-watch-$H12.sh" | head -1)"
echo "结果文件: $OUT （抓够 ${MIN}min 后才落盘）"
echo "查看: ssh 198 'sudo cat $OUT'"
REMOTE
}

# ---------------------------------------------------------------- unblock
cmd_unblock() {
  local target="${1:-}"
  [ -n "$target" ] || usage
  # 参数可以是 sk- key（换算成 hash 前 12 位当标签），也可以直接给标签（如 jwt-class）
  local label
  if [[ "$target" == sk-* ]]; then
    validate_key "$target"
    local h; h=$(key_hash "$target"); label="${h:0:12}"
  else
    [[ "$target" =~ ^[A-Za-z0-9_-]+$ ]] || die "标签形状不合法: $target"
    label="$target"
  fi
  remote "$CONF" "$label" <<'REMOTE'
set -euo pipefail
CONF="$1"; H12="$2"
TS=$(date +%Y%m%d-%H%M%S)
BAK="/root/nginx-backup-cc.conf.$TS"
cp -a "$CONF" "$BAK"
echo "备份: $BAK"
python3 - "$CONF" "$H12" <<'PY'
import sys
conf, h12 = sys.argv[1], sys.argv[2]
s = open(conf).read()
BEGIN = "    # >>> litellm-198-key-block %s BEGIN\n" % h12
END   = "    # <<< litellm-198-key-block %s END\n" % h12
if BEGIN not in s or END not in s:
    sys.exit("没找到 managed 块 %s（可能是手工规则，需人工删）" % h12)
i, j = s.index(BEGIN), s.index(END) + len(END)
s = s[:i] + s[j:]
open(conf, "w").write(s)
print("已移除 managed 块 %s" % h12)
PY
nginx -t
nginx -s reload
echo "== nginx reloaded（未 restart）"
REMOTE
}

# ---------------------------------------------------------------- verify
# 四格回归：死 key 必 403；伪造别的 XFF 仍 403（证明与 IP 无关）；
# 活 key 必 200（证明零误伤）；不带 key 仍 401（证明没碰到正常鉴权路径）。
# 外加 litellm 侧该 hash 归零（证明确实没打进去，不只是我探针看着绿）。
cmd_verify() {
  local key="${1:-}"; shift || true
  [ -n "$key" ] || usage
  validate_key "$key"
  local control=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --control-key) control="$2"; validate_key "$2"; shift 2;;
      *) die "verify: 未知参数 $1";;
    esac
  done
  local h; h=$(key_hash "$key")
  remote "$key" "$control" "$h" "$NS" <<'REMOTE'
set -euo pipefail
KEY="$1"; CONTROL="$2"; HASH="$3"; NS="$4"
H='Host: cc.auto-link.com.cn'
U='http://127.0.0.1/pro/v1/models'
row() { printf '  %-26s -> %s\n' "$1" "$2"; }
hit() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
echo "== 四格回归"
row "死key"                "$(hit -H "$H" -H "Authorization: Bearer $KEY" "$U")  期望 403"
row "死key + 伪造别的XFF"  "$(hit -H "$H" -H 'X-Forwarded-For: 1.2.3.4' -H "Authorization: Bearer $KEY" "$U")  期望 403"
row "死key 走 ?api_key="   "$(hit -H "$H" "$U?api_key=$KEY")  期望 403"
row "不带key"              "$(hit -H "$H" "$U")  期望 401"
if [ -n "$CONTROL" ]; then
  row "对照活key(零误伤判据)" "$(hit -H "$H" -H "Authorization: Bearer $CONTROL" "$U")  期望 200"
else
  echo "  !! 没给 --control-key：**误伤这一格没测**，别宣布零误伤"
fi
echo
echo "== litellm 侧该 hash 最近 60s 出现次数（全部应为 0）"
for p in $(kubectl -n "$NS" get pods -l app=litellm-proxy -o name); do
  printf '  %-46s %s\n' "$p" "$(kubectl -n "$NS" logs "$p" --since=60s 2>/dev/null | grep -c "$HASH" || true)"
done
REMOTE
}

# ---------------------------------------------------------------- stats
# 逐分钟：本规则拦下的 403（第6列 "-"） vs 上游 litellm 回的 403（第6列有耗时） vs 401。
cmd_stats() {
  local minutes=15 since=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --minutes) minutes="$2"; shift 2;;
      --since)   since="$2"; shift 2;;
      *) die "stats: 未知参数 $1";;
    esac
  done
  remote "$ZKLOG" "$minutes" "$since" <<'REMOTE'
set -euo pipefail
ZKLOG="$1"; MINUTES="$2"; SINCE="$3"
echo "now=$(date +%H:%M:%S)   log 末尾=$(tail -1 "$ZKLOG" | cut -d'|' -f1)"
echo
echo "== 逐分钟（最近 $MINUTES 个有数据的分钟）：本规则403 / 上游403 / 401"
awk -F'|' '{
    m = substr($1, 1, 17)
    if ($4 == 403) { if ($6 == "-") mine[m]++; else up[m]++ }
    else if ($4 == 401) { un[m]++ }
    seen[m] = 1
  }
  END { for (m in seen) printf "%s  mine=%-6d upstream=%-5d 401=%-5d\n", m, mine[m], up[m], un[m] }' "$ZKLOG" \
  | sort | tail -"$MINUTES"
echo
if [ -z "$SINCE" ]; then
  echo "!! 没给 --since：下面这张 UA 表统计的是**整个日志文件**。"
  echo "   如果历史上装过按 IP 封的规则，那段时间同一出口 IP 后面的所有 UA 都会被记进来，"
  echo "   会把「这把 key 被多台机器共用」这个结论做假。要下这个结论必须带 --since HH:MM"
  echo "   把窗口限制在**只有 key 规则生效**的时段。"
else
  echo "== 窗口：$SINCE 之后（只应包含 key 规则生效的时段）"
fi
echo "== 本规则拦下的 403 按 UA（多个 UA = 这把 key 被多台机器共用）"
awk -F'|' -v since="$SINCE" '
  $4==403 && $6=="-" {
    if (since != "" && substr($1,13,5) < since) next
    print $7
  }' "$ZKLOG" | sort | uniq -c | sort -rn | head -10
echo
echo "== 提醒：per-minute 出现空档多是客户端退避或换 IP，不是它放弃了。"
REMOTE
}

case "${1:-}" in
  probe)      shift; cmd_probe "$@";;
  watch)      shift; cmd_watch "$@";;
  list)       shift; cmd_list "$@";;
  block)      shift; cmd_block "$@";;
  block-tail) shift; cmd_block_tail "$@";;
  block-jwt)  shift; cmd_block_jwt "$@";;
  unblock)    shift; cmd_unblock "$@";;
  verify)     shift; cmd_verify "$@";;
  verify-jwt) shift; cmd_verify_jwt "$@";;
  stats)      shift; cmd_stats "$@";;
  *) usage;;
esac
