#!/usr/bin/env bash
# litellm-me.sh —— 个人自助查询 198 LiteLLM：我有哪些模型 / 我今天用了多少 / 我的 key 什么状态
#
#   export LITELLM_KEY=sk-xxxx          # 你自己的 virtual key
#   ./scripts/litellm-me.sh models      # 我能调哪些模型
#   ./scripts/litellm-me.sh today       # 今天（UTC 日）用量，按模型拆
#   ./scripts/litellm-me.sh yesterday   # 昨天（UTC 日）
#   ./scripts/litellm-me.sh usage 7     # 最近 7 天每日用量
#   ./scripts/litellm-me.sh key         # 这把 key 的预算/到期/白名单/映射
#   ./scripts/litellm-me.sh whoami      # 我是谁、我名下有几把 key、累计 spend
#   ./scripts/litellm-me.sh raw <path>  # 直接打任意只读路径，原样吐 JSON
#
# 对应文档：docs/198-litellm-personal-api.md
set -euo pipefail

BASE="${LITELLM_BASE:-https://cc.auto-link.com.cn/pro}"
KEY="${LITELLM_KEY:-}"
[ -n "$KEY" ] || { echo "需要 export LITELLM_KEY=sk-...（自己的 virtual key）" >&2; exit 2; }

get() { # get <path> -> JSON 到 stdout；非 200 打印 body 并返回 1（带一次重试，偶发 nginx 5xx）
  local out code body try=0
  while :; do
    out=$(curl -sS -m 60 -w $'\n<<%{http_code}>>' "$BASE$1" -H "Authorization: Bearer $KEY")
    code=${out##*<<}; code=${code%%>>*}
    [ "$code" = 200 ] && break
    try=$((try+1)); [ "$try" -ge 2 ] && break
    sleep 2
  done
  body=${out%$'\n'<<*}
  if [ "$code" != "200" ]; then echo "HTTP $code  GET $1" >&2; echo "$body" >&2; return 1; fi
  printf '%s' "$body"
}

# UTC 日期（LiteLLM 日聚合按 UTC 分桶，不是北京时间，见文档「时区」一节）
utcday() { date -u -v-"${1:-0}"d +%F 2>/dev/null || date -u -d "${1:-0} days ago" +%F; }

cmd=${1:-help}
case "$cmd" in

models)
  DATA=$(get /v1/models) || exit 1
  DATA="$DATA" python3 <<'PY'
import os, json
ids = [m["id"] for m in json.loads(os.environ["DATA"])["data"]]
print(f"可调用模型 {len(ids)} 个：")
for i in sorted(ids):
    print("  ", i)
PY
  ;;

key)
  DATA=$(get /key/info) || exit 1
  DATA="$DATA" python3 <<'PY'
import os, json
i = json.loads(os.environ["DATA"])["info"]
print("key_alias   :", i.get("key_alias"))
print("key_name    :", i.get("key_name"))
print("spend       :", round(i.get("spend") or 0, 4), "/", i.get("max_budget") or "无上限",
      "（重置周期:", i.get("budget_duration") or "不重置", "）")
print("expires     :", i.get("expires") or "永不过期")
print("blocked     :", i.get("blocked"))
print("rpm / tpm   :", i.get("rpm_limit"), "/", i.get("tpm_limit"))
m = i.get("models") or []
print("白名单      :", f"{len(m)} 个模型" if m else "空 = 不限制（全放行）")
a = i.get("aliases") or {}
print("per-key 映射:", len(a), "条")
for k, v in sorted(a.items()):
    print("    ", k, "->", v)
PY
  ;;

whoami)
  DATA=$(get /user/info) || exit 1
  DATA="$DATA" python3 <<'PY'
import os, json
d = json.loads(os.environ["DATA"])
u = d.get("user_info") or {}
md = u.get("metadata") or {}
print("user_id    :", d.get("user_id"))
print("姓名 / 邮箱:", md.get("display_name") or u.get("user_alias"), "/", u.get("user_email"))
print("部门 / 团队:", md.get("department"), "/", md.get("team_alias") or u.get("team_id"))
print("累计 spend :", round(u.get("spend") or 0, 4))
keys = d.get("keys") or []
print(f"名下 key   : {len(keys)}")
for k in keys:
    print("    ", k.get("key_alias"), "| spend", round(k.get("spend") or 0, 4),
          "| 预算", k.get("max_budget") or "-", "| 过期", k.get("expires") or "-")
PY
  ;;

today|yesterday)
  off=0; [ "$cmd" = yesterday ] && off=1
  d=$(utcday "$off")
  echo "== $d (UTC 日 = 北京时间 $d 08:00 ~ 次日 08:00)"
  DATA=$(get "/user/daily/activity?start_date=$d&end_date=$d") || exit 1
  DATA="$DATA" python3 <<'PY'
import os, json
d = json.loads(os.environ["DATA"])
m = d.get("metadata") or {}
print("请求 {} 次（成功 {} / 失败 {}）  spend ${}".format(
    m.get("total_api_requests", 0), m.get("total_successful_requests", 0),
    m.get("total_failed_requests", 0), round(m.get("total_spend", 0), 4)))
print("tokens 输入 {:,} / 输出 {:,} / 缓存读 {:,}".format(
    m.get("total_prompt_tokens", 0), m.get("total_completion_tokens", 0),
    m.get("total_cache_read_input_tokens", 0)))
for r in d.get("results", []):
    mods = (r.get("breakdown") or {}).get("models") or {}
    if not mods:
        continue
    print("按模型：")
    for name, v in sorted(mods.items(), key=lambda kv: -kv[1]["metrics"]["spend"]):
        x = v["metrics"]
        print("  {:<46} req {:>5}  spend {:<10} in {:>10,}  out {:>8,}  cache {:>10,}".format(
            name[:46], x["api_requests"], round(x["spend"], 4),
            x["prompt_tokens"], x["completion_tokens"], x["cache_read_input_tokens"]))
PY
  ;;

usage)
  n=${2:-7}; s=$(utcday "$((n-1))"); e=$(utcday 0)
  echo "== $s ~ $e (UTC 日)"
  DATA=$(get "/user/daily/activity?start_date=$s&end_date=$e&page_size=100") || exit 1
  DATA="$DATA" python3 <<'PY'
import os, json
d = json.loads(os.environ["DATA"])
hdr = "{:<12}{:>8}{:>7}{:>12}{:>14}{:>11}{:>14}"
print(hdr.format("日期", "请求", "失败", "spend", "输入tok", "输出tok", "缓存读"))
for r in sorted(d.get("results", []), key=lambda r: r["date"]):
    x = r["metrics"]
    print(hdr.format(r["date"], x["api_requests"], x["failed_requests"], round(x["spend"], 4),
                     format(x["prompt_tokens"], ","), format(x["completion_tokens"], ","),
                     format(x["cache_read_input_tokens"], ",")))
m = d.get("metadata") or {}
print(hdr.format("合计", m.get("total_api_requests", 0), m.get("total_failed_requests", 0),
                 round(m.get("total_spend", 0), 4),
                 format(m.get("total_prompt_tokens", 0), ","),
                 format(m.get("total_completion_tokens", 0), ","),
                 format(m.get("total_cache_read_input_tokens", 0), ",")))
PY
  ;;

raw)
  get "${2:?用法: raw /some/path}"; echo
  ;;

*)
  sed -n '2,13p' "$0"
  ;;
esac
