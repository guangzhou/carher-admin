#!/usr/bin/env bash
# 第 2 步：把 bge-m3 的上游从 OpenRouter 换成硅基流动 Pro，OpenRouter 降为兜底。
#
# 前提：第 1 步已 PASS（SILICONFLOW_API_KEY 已在服务中的副本 env 里）。
#
# ⚠️ 为什么不是「同组加第二条腿」：
#   routing_strategy = simple-shuffle（DB router_settings 实测）。
#   同组内多个 deployment 是**随机负载均衡**，不是主备。把 siliconflow 和
#   OpenRouter 放一个组 ⇒ 流量 50/50 劈开，OpenRouter 继续吃一半。
#   所以必须：组 `BAAI/bge-m3` 的那条腿**换成** siliconflow（组内仍只 1 条腿），
#   OpenRouter 挪到一个**新组** `openrouter-bge-m3`，再用 fallback 链串起来。
#
# 改三处：
#   A. CM config.yaml：`BAAI/bge-m3` 的 litellm_params 换成 siliconflow Pro
#   B. CM config.yaml：新增组 `openrouter-bge-m3`（= 原来那条 OpenRouter 条目）
#   C. DB router_settings.fallbacks：加 BAAI/bge-m3 -> openrouter-bge-m3
#                                     加 bge-m3     -> BAAI/bge-m3
#      （当前 107 条 fallback 里没有任何 bge 条目 = OpenRouter 挂了 404 到底）
#
# `bge-m3` 组（22 把 key，DB 行 converge/bge-m3）上游是加密列，本脚本不改它的
# 上游，它通过 C 的 fallback 链落到新的 siliconflow 腿上。
#
# 240 把 key 一把都不碰。
set -euo pipefail

NS=litellm-product
CM=litellm-product-proxy-config-ffb212f72856
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK="$HOME/bge-sf-step2-$TS"

if kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then K=kubectl
elif sudo kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then K="sudo kubectl"
else echo "FATAL: kubectl 读不到集群"; exit 1; fi
echo "== 使用: $K"

mkdir -p "$BK"; chmod 700 "$BK"
echo "== 备份目录: $BK"

# ───────────────────── 0. 前置闸门 ─────────────────────
echo "== 闸门 0: 服务中的副本必须都有 SILICONFLOW_API_KEY"
PODS=$($K -n "$NS" get endpoints litellm-proxy \
        -o jsonpath='{range .subsets[*].addresses[*]}{.targetRef.name}{"\n"}{end}' \
      | grep -v '^$' | sort -u)
[ -n "$PODS" ] || { echo "FATAL: 取不到 endpoints"; exit 1; }
for P in $PODS; do
  L=$($K -n "$NS" exec "$P" -- python3 -c \
      'import os;print(len(os.environ.get("SILICONFLOW_API_KEY") or ""))' 2>/dev/null || echo 0)
  echo "   $P env len=$L"
  [ "$L" -ge 20 ] || { echo "FATAL: $P 缺 SILICONFLOW_API_KEY，先跑第 1 步"; exit 1; }
done

# ───────────────────── 1. 备份 ─────────────────────
$K -n "$NS" get cm "$CM" -o yaml > "$BK/cm.before.yaml"
$K -n "$NS" get cm "$CM" -o jsonpath='{.data.config\.yaml}' > "$BK/config.before.yaml"
$K -n "$NS" get deploy litellm-proxy -o yaml > "$BK/deploy.before.yaml"
$K -n "$NS" exec litellm-db-0 -- psql -U litellm -d litellm -tAc \
  "SELECT param_value FROM \"LiteLLM_Config\" WHERE param_name = 'router_settings';" \
  > "$BK/router_settings.before.json"
echo "   config.before:          $(wc -c < "$BK/config.before.yaml") bytes  sha=$(sha256sum "$BK/config.before.yaml" | cut -c1-12)"
echo "   router_settings.before: $(wc -c < "$BK/router_settings.before.json") bytes"
python3 -c "import json;print('   fallbacks 条数:',len(json.load(open('$BK/router_settings.before.json')).get('fallbacks',[])))"

# ───────────────────── 2. 生成新 config ─────────────────────
echo "== 生成新 config.yaml"
python3 - "$BK" <<'PY'
import sys, yaml
bk = sys.argv[1]
d = yaml.safe_load(open(bk + "/config.before.yaml"))
ml = d["model_list"]

GROUP = "BAAI/bge-m3"
NEWGROUP = "openrouter-bge-m3"

cur = [e for e in ml if e.get("model_name") == GROUP]
if len(cur) != 1:
    print("FATAL: 组 %s 的 deployment 数 = %d，预期 1。停手。" % (GROUP, len(cur)))
    raise SystemExit(1)
old = cur[0]
if old["litellm_params"].get("model") != "openrouter/BAAI/bge-m3":
    print("FATAL: 现有上游不是 openrouter/BAAI/bge-m3，实际 =",
          old["litellm_params"].get("model"), "停手。")
    raise SystemExit(1)
if any(e.get("model_name") == NEWGROUP for e in ml):
    print("FATAL: 组 %s 已存在，本脚本不幂等，停手。" % NEWGROUP)
    raise SystemExit(1)

import copy
# B. OpenRouter 原条目整体挪进新组（上游参数逐字节保留，只改 model_name 和 id）
orr = copy.deepcopy(old)
orr["model_name"] = NEWGROUP
orr["model_info"]["id"] = "openrouter/bge-m3-fallback"

# A. 原组那条腿换成 siliconflow Pro（¥0.07/M ÷ 6.71 = $1.043e-8 /token）
SF_COST = 1.043e-08
old["litellm_params"] = {
    "model": "openai/Pro/BAAI/bge-m3",
    "api_key": "os.environ/SILICONFLOW_API_KEY",
    "api_base": "https://api.siliconflow.cn/v1",
    "input_cost_per_token": SF_COST,
    "output_cost_per_token": 0,
}
old["model_info"] = {
    "id": "siliconflow/bge-m3-pro",
    "mode": "embedding",
    "input_cost_per_token": SF_COST,
    "output_cost_per_token": 0,
}
ml.insert(ml.index(old) + 1, orr)

out = bk + "/config.after.yaml"
with open(out, "w") as f:
    yaml.dump(d, f, allow_unicode=True, sort_keys=True, default_flow_style=False, width=4096)

chk = yaml.safe_load(open(out))
g = {}
for e in chk["model_list"]: g.setdefault(e["model_name"], []).append(e)
assert len(g[GROUP]) == 1, "%s 应仍只有 1 条腿（simple-shuffle 会劈流量）" % GROUP
assert len(g[NEWGROUP]) == 1, "%s 应有 1 条腿" % NEWGROUP
assert g[GROUP][0]["litellm_params"]["model"] == "openai/Pro/BAAI/bge-m3"
assert g[NEWGROUP][0]["litellm_params"]["model"] == "openrouter/BAAI/bge-m3"
print("   组 %-18s -> %s (1 条腿)" % (GROUP, g[GROUP][0]["litellm_params"]["model"]))
print("   组 %-18s -> %s (1 条腿，新建)" % (NEWGROUP, g[NEWGROUP][0]["litellm_params"]["model"]))
print("   model_list 条数: %d -> %d" % (len(yaml.safe_load(open(bk+"/config.before.yaml"))["model_list"]), len(chk["model_list"])))
PY

AFTER="$BK/config.after.yaml"
[ -s "$AFTER" ] || { echo "FATAL: 没生成 config.after.yaml"; exit 1; }

echo "== 闸门 1: 只允许触及 BAAI/bge-m3 + 新增 openrouter-bge-m3"
python3 - "$BK" <<'PY'
import sys, yaml
bk = sys.argv[1]
a = yaml.safe_load(open(bk + "/config.before.yaml"))
b = yaml.safe_load(open(bk + "/config.after.yaml"))
for key in ("general_settings", "litellm_settings", "router_settings"):
    if a.get(key) != b.get(key):
        print("FATAL: %s 被改动，本次只应改 model_list。停手。" % key); raise SystemExit(1)
ga = {}; gb = {}
for e in a["model_list"]: ga.setdefault(e["model_name"], []).append(e)
for e in b["model_list"]: gb.setdefault(e["model_name"], []).append(e)
added = set(gb) - set(ga); lost = set(ga) - set(gb)
if added != {"openrouter-bge-m3"} or lost:
    print("FATAL: 组集合变化异常。新增:", added, "丢失:", lost); raise SystemExit(1)
changed = [g for g in ga if ga[g] != gb[g]]
if changed != ["BAAI/bge-m3"]:
    print("FATAL: 被改动的组 =", changed, "，预期只有 ['BAAI/bge-m3']。停手。"); raise SystemExit(1)
print("   PASS: 仅 BAAI/bge-m3 改动 + 新增 openrouter-bge-m3，其余 %d 组逐字节一致" % (len(ga)-1))
PY

# ───────────────────── 3. 写 CM ─────────────────────
echo "== 写 ConfigMap"
$K -n "$NS" create configmap "$CM" --from-file=config.yaml="$AFTER" \
  --dry-run=client -o yaml > "$BK/cm.after.yaml"
$K -n "$NS" apply -f "$BK/cm.after.yaml" >/dev/null
$K -n "$NS" get cm "$CM" -o jsonpath='{.data.config\.yaml}' > "$BK/config.readback.yaml"
if cmp -s "$AFTER" "$BK/config.readback.yaml"; then
  echo "   PASS: 回读 $(wc -c < "$BK/config.readback.yaml") bytes，与预期逐字节一致"
else
  echo "FATAL: 回读不一致，立即回滚 CM"
  $K -n "$NS" apply -f "$BK/cm.before.yaml" >/dev/null
  echo "   已回滚"; exit 1
fi

# ───────────────────── 4. DB fallback 链 ─────────────────────
echo "== 加 fallback 链（read-merge-write）"
python3 - "$BK" <<'PY'
import sys, json
bk = sys.argv[1]
d = json.load(open(bk + "/router_settings.before.json"))
fb = d.get("fallbacks", [])
n0 = len(fb)
if any("bge" in json.dumps(x).lower() for x in fb):
    print("FATAL: fallbacks 里已有 bge 条目，本脚本不幂等，停手。"); raise SystemExit(1)
# 两端同权重同归一化（实测 cos 0.9999），兜底不污染向量空间。
fb.append({"BAAI/bge-m3": ["openrouter-bge-m3"]})
fb.append({"bge-m3": ["BAAI/bge-m3"]})
d["fallbacks"] = fb
json.dump(d, open(bk + "/router_settings.after.json", "w"), ensure_ascii=False)
print("   fallbacks: %d -> %d" % (n0, len(fb)))
for x in fb[-2:]: print("     +", x)
PY
python3 -c "import json;json.load(open('$BK/router_settings.after.json'))" \
  || { echo "FATAL: after JSON 不合法"; exit 1; }

$K -n "$NS" exec -i litellm-db-0 -- sh -c "cat > /tmp/rs-$TS.json" < "$BK/router_settings.after.json"
$K -n "$NS" exec litellm-db-0 -- python3 -c "
import json; json.load(open('/tmp/rs-$TS.json')); print('   pod 内 JSON 合法')" 2>/dev/null \
  || echo "   (pod 内无 python3，跳过二次校验)"
$K -n "$NS" exec -i litellm-db-0 -- psql -U litellm -d litellm -v ON_ERROR_STOP=1 >/dev/null <<SQL
\set rs \`cat /tmp/rs-$TS.json\`
UPDATE "LiteLLM_Config" SET param_value = :'rs'::jsonb WHERE param_name = 'router_settings';
SQL
$K -n "$NS" exec litellm-db-0 -- rm -f "/tmp/rs-$TS.json"

$K -n "$NS" exec litellm-db-0 -- psql -U litellm -d litellm -tAc \
  "SELECT param_value FROM \"LiteLLM_Config\" WHERE param_name='router_settings';" \
  > "$BK/router_settings.readback.json"
python3 - "$BK" <<'PY'
import sys, json
bk = sys.argv[1]
a = json.load(open(bk + "/router_settings.after.json"))
b = json.load(open(bk + "/router_settings.readback.json"))
if a == b:
    print("   PASS: DB 回读一致，fallbacks %d 条" % len(b.get("fallbacks", [])))
else:
    print("FATAL: DB 回读不一致。after keys=%d readback keys=%d" % (len(a), len(b)))
    print("  fallbacks after=%d readback=%d" % (len(a.get("fallbacks",[])), len(b.get("fallbacks",[]))))
    raise SystemExit(1)
PY

# ───────────────────── 5. 滚动 ─────────────────────
echo "== 滚动重启 litellm-proxy（零 delete pod）"
$K -n "$NS" rollout restart deploy/litellm-proxy
$K -n "$NS" rollout status deploy/litellm-proxy --timeout=600s
echo "   $($K -n "$NS" get deploy litellm-proxy -o jsonpath='updated={.status.updatedReplicas} ready={.status.readyReplicas} replicas={.spec.replicas}')"

echo
echo "== 第 2 步写入完成。备份 $BK"
echo "   回滚: $K -n $NS apply -f $BK/cm.before.yaml"
echo "         DB: 把 router_settings.before.json 同法写回"
echo "         然后 rollout restart deploy/litellm-proxy"
echo "   ⚠️ 落点还没验，接着跑 verify 脚本"
