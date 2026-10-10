#!/usr/bin/env bash
# 第 3 步：把 DB 里的 `bge-m3` 组（22 把 key，含 carher-1/2/13/14）的上游
#          从 openrouter 换成硅基流动 Pro。
#
# 为什么单独一步：这一条是 db_model=true，上游在 LiteLLM_ProxyModelTable 的
# 加密列里（model / api_base / api_key 三列都加密），不能改 CM，也不能明文 UPDATE。
# 必须走 /model/update API。
#
# 🔴 两个已知地雷：
#   1. /model/update 会清掉未传字段（memory: litellm_model_update_endpoint_clobbers_fields）
#      ⇒ 必须 read-merge-write，把 /model/info 读到的字段全量回传。
#      这一条上挂着 22 个 team 的 access_via_team_ids，丢了就是 22 个 team 集体失权。
#   2. ProxyModelTable 写入需要重启才重路由
#      （memory: proxymodeltable_write_reroutes_deployment_needs_restart_regress）
set -euo pipefail

NS=litellm-product
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK="$HOME/bge-sf-step3-$TS"
MODEL_ID="converge/bge-m3"

if kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then K=kubectl
elif sudo kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then K="sudo kubectl"
else echo "FATAL: kubectl 读不到集群"; exit 1; fi

mkdir -p "$BK"; chmod 700 "$BK"
echo "== 备份目录: $BK"
MK=$($K -n "$NS" get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# ───────── 1. 读当前完整形状 + 备份 ─────────
echo "== 读 /model/info 当前形状"
curl -s "http://127.0.0.1:30402/model/info" -H "Authorization: Bearer $MK" \
  > "$BK/model_info.before.json"
$K -n "$NS" exec litellm-db-0 -- psql -U litellm -d litellm -tAc \
  "SELECT row_to_json(t) FROM (SELECT * FROM \"LiteLLM_ProxyModelTable\"
    WHERE model_id = '$MODEL_ID') t;" > "$BK/dbrow.before.json"
echo "   model_info: $(wc -c < "$BK/model_info.before.json") bytes"
echo "   dbrow:      $(wc -c < "$BK/dbrow.before.json") bytes"

python3 - "$BK" <<'PY'
import sys, json
bk = sys.argv[1]
rows = json.load(open(bk + "/model_info.before.json")).get("data", [])
cur = [r for r in rows if r.get("model_info", {}).get("id") == "converge/bge-m3"]
if len(cur) != 1:
    print("FATAL: converge/bge-m3 条数 =", len(cur), "预期 1，停手"); raise SystemExit(1)
r = cur[0]
lp = r.get("litellm_params", {}) or {}
mi = r.get("model_info", {}) or {}
if lp.get("model") != "openrouter/BAAI/bge-m3":
    print("FATAL: 当前上游不是 openrouter/BAAI/bge-m3，实际 =", lp.get("model"), "停手")
    raise SystemExit(1)
teams = mi.get("access_via_team_ids") or []
print("   当前上游:", lp.get("model"))
print("   model_name:", r.get("model_name"))
print("   db_model:", mi.get("db_model"))
print("   access_via_team_ids: %d 个 team（绝不能丢）" % len(teams))
json.dump(r, open(bk + "/entry.before.json", "w"), ensure_ascii=False, indent=2)
PY

# ───────── 2. 构造 merge 后的 payload ─────────
echo "== 构造 read-merge-write payload"
python3 - "$BK" <<'PY'
import sys, json
bk = sys.argv[1]
r = json.load(open(bk + "/entry.before.json"))
lp = dict(r.get("litellm_params", {}) or {})
mi = dict(r.get("model_info", {}) or {})

SF_COST = 1.043e-08   # ¥0.07/M ÷ 6.71 CNY/USD
# 只改上游三项 + 价格，其余 litellm_params 字段逐一保留
lp["model"] = "openai/Pro/BAAI/bge-m3"
lp["api_base"] = "https://api.siliconflow.cn/v1"
lp["api_key"] = "os.environ/SILICONFLOW_API_KEY"
lp["input_cost_per_token"] = SF_COST
lp["output_cost_per_token"] = 0.0
mi["input_cost_per_token"] = SF_COST
mi["output_cost_per_token"] = 0.0

payload = {
    "model_name": r["model_name"],
    "litellm_params": lp,
    "model_info": mi,          # 整块回传 => 22 个 team 授权和 mode/db_model 都保住
}
json.dump(payload, open(bk + "/payload.json", "w"), ensure_ascii=False, indent=2)

# 自检：payload 必须保住关键字段
assert payload["model_info"].get("id") == "converge/bge-m3"
assert payload["model_info"].get("mode") == "embedding"
nt = len(payload["model_info"].get("access_via_team_ids") or [])
assert nt == len(r["model_info"].get("access_via_team_ids") or []), "team 数变了"
print("   payload: model=%s" % lp["model"])
print("   payload: api_base=%s" % lp["api_base"])
print("   payload: model_info.id=%s mode=%s teams=%d" % (
    payload["model_info"]["id"], payload["model_info"]["mode"], nt))
print("   litellm_params 字段数: %d -> %d" % (len(r.get("litellm_params") or {}), len(lp)))
PY

# ───────── 3. 写 ─────────
echo "== 调 /model/update"
HTTP=$(curl -s -o "$BK/update.resp.json" -w '%{http_code}' \
  -X POST "http://127.0.0.1:30402/model/update" \
  -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
  --data-binary @"$BK/payload.json")
echo "   HTTP $HTTP"
head -c 400 "$BK/update.resp.json"; echo
[ "$HTTP" = "200" ] || { echo "FATAL: update 没返回 200，停手（未滚动，线上仍是旧上游）"; exit 1; }

# ───────── 4. 回读（写前就验，别等滚完）─────────
echo "== 回读 /model/info"
curl -s "http://127.0.0.1:30402/model/info" -H "Authorization: Bearer $MK" \
  > "$BK/model_info.after.json"
python3 - "$BK" <<'PY'
import sys, json
bk = sys.argv[1]
rows = json.load(open(bk + "/model_info.after.json")).get("data", [])
cur = [r for r in rows if r.get("model_info", {}).get("id") == "converge/bge-m3"]
if len(cur) != 1:
    print("FATAL: 回读 converge/bge-m3 条数 =", len(cur)); raise SystemExit(1)
r = cur[0]
lp = r.get("litellm_params", {}) or {}
mi = r.get("model_info", {}) or {}
before = json.load(open(bk + "/entry.before.json"))
bt = len(before["model_info"].get("access_via_team_ids") or [])
at = len(mi.get("access_via_team_ids") or [])
ok = True
print("   上游:", lp.get("model"), "| base:", lp.get("api_base"))
if lp.get("model") != "openai/Pro/BAAI/bge-m3": print("   FAIL: 上游没改成 siliconflow"); ok = False
print("   model_name:", r.get("model_name"))
if r.get("model_name") != before.get("model_name"): print("   FAIL: model_name 变了"); ok = False
print("   mode:", mi.get("mode"))
if mi.get("mode") != "embedding": print("   FAIL: mode 丢了 => embedding 路由会坏"); ok = False
print("   access_via_team_ids: %d (改前 %d)" % (at, bt))
if at != bt: print("   FAIL: team 授权数变了！%d -> %d" % (bt, at)); ok = False
# 其余 litellm_params 字段不许丢
lost = set(before.get("litellm_params") or {}) - set(lp)
if lost: print("   FAIL: litellm_params 丢字段:", lost); ok = False
raise SystemExit(0 if ok else 1)
PY
RC=$?
[ "$RC" = "0" ] || { echo "FATAL: 回读校验失败。回滚见 $BK/entry.before.json"; exit 1; }

# ───────── 5. 滚动（ProxyModelTable 写入需重启才重路由）─────────
echo "== 滚动重启 litellm-proxy（ProxyModelTable 写入需重启生效）"
$K -n "$NS" rollout restart deploy/litellm-proxy
$K -n "$NS" rollout status deploy/litellm-proxy --timeout=600s
echo "   $($K -n "$NS" get deploy litellm-proxy -o jsonpath='updated={.status.updatedReplicas} ready={.status.readyReplicas} replicas={.spec.replicas}')"

echo
echo "== 第 3 步完成。备份 $BK"
echo "   回滚：用 $BK/entry.before.json 的 litellm_params+model_info 原样 POST 回 /model/update"
