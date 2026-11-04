#!/bin/bash
# 「谁在门外等登录」—— 列出被 DM 名单拒掉的人,并把放行做成一条命令。
#
# 为什么需要:外部人要拿 key,得先私聊机器人发一句「登录」。但机器人的 DM 策略是 allowlist,
# **不在名单的人连插件都碰不到**(实测:lark 插件 inbound 流水线第 5 步 policy gate 就 return 了,
# before_dispatch 是第 9 步)⇒ 他发了「登录」机器人一声不响,他以为坏了,我也不知道他来过。
# 11-04 一天就有 3 个不同的人这样被拒(日志 `sender ou_… not in DM allowlist`)。
#
# 这个脚本只做两件事,都**不改变放行策略**:
#   list    把日志里被拒的人聚合出来(谁、几次、最后一次什么时候)—— 看见"门外有谁"
#   allow   把某个 open_id 加进两份名单(DM 准入 + ext-login 签发),回读确认
#
# 🔴 不做自动放行:放行 = 允许一个陌生飞书号私聊生产机器人并拿到登录链接,这是人该拍的。
#    也**不要**把 dmPolicy 改成 open —— 那等于对全飞书开放私聊(fail-open),
#    而且 gate 放过之后消息会进模型,不只是走我们这个确定性插件。
#
# 用法:
#   bash scripts/feishu-ext-login/waiting.sh list
#   bash scripts/feishu-ext-login/waiting.sh allow ou_xxxxxxxx [ou_yyyy …]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CMD="${1:-list}"
shift || true

case "$CMD" in
list)
  cat > /tmp/_wait_list.sh <<'REMOTE'
set -e
python3 - <<'PY'
import glob, json, os, re
# 两份名单:config 里的 allowFrom + pairing store;再加上 ext-login 自己的签发名单
cfg = json.load(open("/data/.openclaw/openclaw.json"))
dm = set(x.lower() for x in (cfg.get("channels", {}).get("feishu", {}).get("allowFrom") or []))
try:
    dm |= set(x.lower() for x in (json.load(
        open("/data/.openclaw/credentials/feishu-allowFrom.json")).get("allowFrom") or []))
except Exception:
    pass
try:
    issue = set(x.lower() for x in (json.load(
        open("/data/.openclaw/carher-ext-login/allow.json")).get("allow") or []))
except Exception:
    issue = set()

RE = re.compile(r"sender (ou_[a-z0-9]+) not in DM allowlist")
hits = {}
for f in sorted(glob.glob("/data/.openclaw/logs/openclaw-*.log")):
    for line in open(f, errors="replace"):
        m = RE.search(line)
        if not m:
            continue
        who = m.group(1)
        t = ""
        try:
            t = json.loads(line).get("time", "")
        except Exception:
            pass
        r = hits.setdefault(who, {"n": 0, "last": ""})
        r["n"] += 1
        if t > r["last"]:
            r["last"] = t

print("DM 准入名单 %d 人;ext-login 签发名单 %d 人" % (len(dm), len(issue)))


def is_external(open_id):
    """内部人 / 外部人:拿 bot token 查通讯录 —— 外部租户必 41050(本仓已验),内部人能查到。
    查不动(网络/权限异常)就回 None,**不猜**。"""
    import urllib.request, urllib.error
    aid, sec = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if not aid or not sec:
        return None
    try:
        base = "https://open.feishu.cn/open-apis"
        req = urllib.request.Request(base + "/auth/v3/tenant_access_token/internal",
                                     json.dumps({"app_id": aid, "app_secret": sec}).encode(),
                                     {"Content-Type": "application/json"})
        tok = json.load(urllib.request.urlopen(req, timeout=10)).get("tenant_access_token")
        if not tok:
            return None
        req2 = urllib.request.Request(
            "%s/contact/v3/users/%s?user_id_type=open_id" % (base, open_id),
            headers={"Authorization": "Bearer " + tok})
        # 🔴 外部人这条查询飞书回 **HTTP 400** + body 里 code=41050 ⇒ urlopen 直接抛 HTTPError,
        #    不接住的话就永远读不到那个 41050,所有人都成了"查不动"(第一版实测全是 ?)。
        #    错误信封要当数据读。
        try:
            d = json.load(urllib.request.urlopen(req2, timeout=10))
        except urllib.error.HTTPError as he:
            try:
                d = json.load(he)
            except Exception:
                return None
        if d.get("code") == 0:
            return False, (d.get("data", {}).get("user", {}) or {}).get("name") or ""
        if d.get("code") == 41050:      # no user authority = 外部租户的人(本仓已验)
            return True, ""
        return None
    except Exception:
        return None


# 两份名单不一致本身就是故障源 —— 但只对**外部人**才算问题:
# 内部人走网页飞书 OAuth 登录,根本不需要 ext-login 签发名单(他在 DM 名单里只是能跟机器人聊天)。
# 不做这层区分 ⇒ 每次都对"刘国现不在签发名单"报一次假警(第一版就是这样)。
only_dm = dm - issue - {"*"}
real = []
for x in sorted(only_dm):
    r = is_external(x)
    if r is None:
        real.append((x, "查不动,没法判内外部"))
    elif r[0]:
        real.append((x, "外部人 ⇒ 真的拿不到链接"))
if real:
    print("⚠️  能私聊但拿不到登录链接:")
    for x, why in real:
        print("     %s  (%s)" % (x, why))
only_issue = issue - dm
if only_issue:
    print("⚠️  在签发名单但进不来私聊(%d 人,等于白加):%s" % (len(only_issue), ", ".join(sorted(only_issue))))
if not hits:
    print("\n门外没人等(日志里没有被拒记录)。")
    raise SystemExit(0)
print("\n被挡在门外的人(发过「登录」但不在 DM 名单):")
for who, r in sorted(hits.items(), key=lambda kv: kv[1]["last"], reverse=True):
    state = "已在名单(日志是加之前的)" if who.lower() in dm else "**还在门外**"
    ext = is_external(who)
    tag = "?" if ext is None else ("外部人" if ext[0] else "内部人" + (" " + ext[1] if ext[1] else ""))
    print("  %s  被拒 %d 次  最后一次 %s  [%s]  %s" % (
        who, r["n"], r["last"][:19] or "?", tag, state))
print("\n放行:bash scripts/feishu-ext-login/waiting.sh allow <ou_…>  (一次可以给多个)")
PY
REMOTE
  "$HERE/podsh.sh" /tmp/_wait_list.sh; rm -f /tmp/_wait_list.sh
  ;;

allow)
  [ "$#" -ge 1 ] || { echo "用法: $0 allow ou_xxx [ou_yyy …]" >&2; exit 1; }
  for id in "$@"; do
    case "$id" in
      ou_*) : ;;
      *) echo "FATAL: '$id' 不像 open_id(应形如 ou_xxx);没有任何改动" >&2; exit 1 ;;
    esac
    case "$id" in
      *[!A-Za-z0-9_]*) echo "FATAL: '$id' 含非法字符;没有任何改动" >&2; exit 1 ;;
    esac
  done
  IDS="$*"
  cat > /tmp/_wait_allow.sh <<REMOTE
set -e
python3 - ${IDS} <<'PY'
import json, os, sys, shutil, time
ids = [a.strip().lower() for a in sys.argv[1:] if a.strip()]
stamp = time.strftime("%Y%m%d-%H%M%S")

def edit(path, key, container):
    """幂等地把 ids 并进 path 里的名单;改前备份,改后回读。"""
    try:
        doc = json.load(open(path))
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    cur = doc.get(key)
    cur = [str(x) for x in cur] if isinstance(cur, list) else []
    add = [i for i in ids if i not in [c.lower() for c in cur]]
    if not add:
        print("  %s:已经都在里面了(%d 人),没动" % (os.path.basename(path), len(cur)))
        return
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak." + stamp)
    doc[key] = cur + add
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    # 回读:不信"我写了",只信读回来的
    back = json.load(open(path)).get(key) or []
    missing = [i for i in ids if i not in [str(x).lower() for x in back]]
    if missing:
        raise SystemExit("FATAL: 回读少了 %s" % missing)
    print("  %s:加了 %d 人,现在 %d 人(备份 .bak.%s)" % (
        os.path.basename(path), len(add), len(back), stamp))

print("放行:", ", ".join(ids))
# 两份都要加:DM 准入(让他能私聊到机器人)+ ext-login(让插件肯给他签链接)。
# 只加一份 = 白加:只有 DM 没有 ext-login,他发「登录」会掉进模型;反之连消息都到不了插件。
edit("/data/.openclaw/credentials/feishu-allowFrom.json", "allowFrom", "DM 准入")
edit("/data/.openclaw/carher-ext-login/allow.json", "allow", "ext-login 签发")
PY
echo "--- 回读两份名单 ---"
python3 -c '
import json
for p,k in (("/data/.openclaw/credentials/feishu-allowFrom.json","allowFrom"),
            ("/data/.openclaw/carher-ext-login/allow.json","allow")):
    print(" ", p.split("/")[-1], "=", len(json.load(open(p)).get(k) or []), "人")
'
REMOTE
  "$HERE/podsh.sh" /tmp/_wait_allow.sh; rm -f /tmp/_wait_allow.sh
  echo
  echo "名单是热读的(gate 每条消息都重读 store,插件每次都重读 allow.json)⇒ 不用重启。"
  echo "让对方再私聊机器人发一句「登录」即可;没回就跑 $0 list 看他是否还在门外。"
  ;;

*)
  echo "用法: $0 list | $0 allow ou_xxx [ou_yyy …]" >&2; exit 1 ;;
esac
