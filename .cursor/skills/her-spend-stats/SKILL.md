---
name: her-spend-stats
description: >-
  统计 K8s 上所有 Her 实例的每日消费情况（LiteLLM spend）。
  Use when the user asks to check, view, or compare spend/cost/消费/费用 for her instances,
  mentions "每天消费" / "每日费用" / "消费统计" / "spend统计" / "哪个her花最多",
  or wants a breakdown by model/day/instance.
---

# Her 实例每日消费统计

数据来源：**LiteLLM Proxy**（权威，按 `carher-{uid}` 虚拟 key 跟踪每笔请求的 cost）。

仅 `provider=litellm` 的实例有独立 key，其余实例（wangsu/openrouter/anthropic）不经过 LiteLLM，无法从此处获取消费数据。

---

## 前置：连接集群

```bash
kubectl get nodes 2>&1 | grep -v "^E" | head -3
```

若报 `connection refused`，按 `k8s-via-bastion` skill 启动 proxy：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

---

## 方式 1：Admin API 快速汇总（推荐）

Admin API 内部已聚合 LiteLLM spend 数据，无需额外 port-forward。

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

curl -s https://admin.carher.net/api/litellm/spend \
  -H "X-API-Key: $API_KEY" -o /tmp/admin_spend.json

python3 << 'PY'
import json
with open("/tmp/admin_spend.json") as f:
    data = json.load(f)
rows = sorted(data, key=lambda r: r.get("spend", 0), reverse=True)
print(f"{'ID':<6} {'名称':<16} {'模型':<8} {'消费(USD)':<12} {'今日(USD)':<12}")
print("-" * 60)
for r in rows:
    print(f"{r.get('uid',''):<6} {r.get('name',''):<16} {r.get('model',''):<8} "
          f"${r.get('spend',0):<11.4f} ${r.get('today_spend',0):<11.4f}")
PY
```

---

## 方式 2：LiteLLM API 精细查询（按日期范围）

### 2a. 获取 Master Key & Port-Forward

```bash
export MASTER_KEY=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

kubectl port-forward -n carher svc/litellm-proxy 4000:4000 &>/tmp/litellm-pf.log &
sleep 3
```

### 2b. 查每个 key 的累计 spend

```bash
curl -s "http://127.0.0.1:4000/spend/keys?limit=100000" \
  -H "Authorization: Bearer $MASTER_KEY" -o /tmp/spend_keys.json

python3 << 'PY'
import json
with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)
carher = [r for r in rows if (r.get("key_alias") or "").startswith("carher-")]
carher.sort(key=lambda r: r.get("spend", 0), reverse=True)
print(f"{'Key Alias':<16} {'消费(USD)':<12} {'预算(USD)':<12} {'重置周期':<10} {'下次重置'}")
print("-" * 70)
for r in carher:
    alias = r.get("key_alias", "")
    spend = r.get("spend", 0)
    budget = r.get("max_budget") or "-"
    duration = r.get("budget_duration") or "-"
    reset_at = (r.get("budget_reset_at") or "-")[:10]
    print(f"{alias:<16} ${spend:<11.4f} {str(budget):<12} {duration:<10} {reset_at}")
print(f"\n共 {len(carher)} 个 carher 实例")
PY
```

### 2c. 按日期范围查询 SpendLogs（每天明细）

```bash
# 查询最近 N 天的 spend log（按 key_alias + 日期分组）
python3 << 'PY'
import json, os, urllib.request
from datetime import datetime, timedelta, timezone

MASTER_KEY = os.environ["MASTER_KEY"]
BASE_URL = "http://127.0.0.1:4000"
DAYS = 7  # 查最近 7 天

# 分页拉取全部 spend logs（每页 1000 条，直到没有更多数据）
logs = []
page = 1
while True:
    req = urllib.request.Request(
        f"{BASE_URL}/spend/logs?limit=1000&page={page}",
        headers={"Authorization": f"Bearer {MASTER_KEY}"}
    )
    batch = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if not batch:
        break
    logs.extend(batch)
    if len(batch) < 1000:
        break
    page += 1
print(f"共拉取 {len(logs)} 条 spend logs")

# 过滤 carher-* 并按 (alias, date) 分组
daily = {}
for log in logs:
    alias = log.get("user_id") or log.get("key_alias") or ""
    if not alias.startswith("carher-"):
        continue
    ts = log.get("startTime") or log.get("created_at") or ""
    day = ts[:10]  # YYYY-MM-DD
    cost = log.get("spend") or 0
    key = (alias, day)
    daily[key] = daily.get(key, 0) + cost

# 输出（按消费降序）
sorted_items = sorted(daily.items(), key=lambda x: x[1], reverse=True)
print(f"{'Key Alias':<16} {'日期':<12} {'当日消费(USD)'}")
print("-" * 45)
for (alias, day), cost in sorted_items:
    print(f"{alias:<16} {day:<12} ${cost:.4f}")
PY
```

### 2d. 直接查 PostgreSQL（最精细，支持任意聚合）

```bash
DB_URL=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.DATABASE_URL}' | base64 -d)

# 按 key_alias + 天 汇总最近 7 天
kubectl exec litellm-db-0 -n carher -- psql "$DB_URL" -c "
SELECT
  user_id AS key_alias,
  DATE(\"startTime\") AS day,
  ROUND(SUM(spend)::numeric, 4) AS cost_usd,
  COUNT(*) AS requests
FROM \"LiteLLM_SpendLogs\"
WHERE user_id LIKE 'carher-%'
  AND \"startTime\" >= NOW() - INTERVAL '7 days'
GROUP BY user_id, DATE(\"startTime\")
ORDER BY day DESC, cost_usd DESC;
"

# 按实例汇总（总消费排行）
kubectl exec litellm-db-0 -n carher -- psql "$DB_URL" -c "
SELECT
  user_id AS key_alias,
  ROUND(SUM(spend)::numeric, 4) AS total_usd,
  COUNT(*) AS requests,
  MAX(\"startTime\") AS last_seen
FROM \"LiteLLM_SpendLogs\"
WHERE user_id LIKE 'carher-%'
GROUP BY user_id
ORDER BY total_usd DESC;
"

# 按天汇总全部 carher 实例（查平台整体每天费用）
kubectl exec litellm-db-0 -n carher -- psql "$DB_URL" -c "
SELECT
  DATE(\"startTime\") AS day,
  ROUND(SUM(spend)::numeric, 4) AS total_usd,
  COUNT(DISTINCT user_id) AS active_instances,
  COUNT(*) AS requests
FROM \"LiteLLM_SpendLogs\"
WHERE user_id LIKE 'carher-%'
  AND \"startTime\" >= NOW() - INTERVAL '30 days'
GROUP BY DATE(\"startTime\")
ORDER BY day DESC;
"
```

---

## 方式 3：关联 CRD 数据（得到实例名称）

LiteLLM 只知道 `carher-{uid}`，关联 CRD 得到人名和模型。

```bash
# 1. 获取 CRD 基础信息
kubectl get her -n carher -o json > /tmp/her_crd.json

python3 << 'PY'
import json
with open("/tmp/her_crd.json") as f:
    data = json.load(f)
mapping = {}
for item in data["items"]:
    uid = item["spec"]["userId"]
    mapping[f"carher-{uid}"] = {
        "name": item["spec"].get("name", ""),
        "model": item["spec"].get("model", ""),
        "provider": item["spec"].get("provider", ""),
        "phase": item.get("status", {}).get("phase", ""),
    }
with open("/tmp/her_mapping.json", "w") as f:
    json.dump(mapping, f)
print(f"共 {len(mapping)} 个 her 实例已写入 /tmp/her_mapping.json")
PY

# 2. 与 spend 数据合并输出
python3 << 'PY'
import json

with open("/tmp/her_mapping.json") as f:
    mapping = json.load(f)
with open("/tmp/spend_keys.json") as f:
    spend_rows = json.load(f)

carher = {r["key_alias"]: r for r in spend_rows
          if (r.get("key_alias") or "").startswith("carher-")}

print(f"{'ID':<6} {'名称':<16} {'模型':<8} {'状态':<10} {'消费(USD)':<12} {'预算':<8}")
print("-" * 65)
all_aliases = set(mapping) | set(carher)
rows = []
for alias in all_aliases:
    info = mapping.get(alias, {})
    spend_info = carher.get(alias, {})
    uid = alias.replace("carher-", "")
    rows.append((
        uid,
        info.get("name", "?"),
        info.get("model", "?"),
        info.get("phase", "?"),
        spend_info.get("spend", 0),
        spend_info.get("max_budget") or "-",
    ))
rows.sort(key=lambda r: r[4], reverse=True)
for uid, name, model, phase, spend, budget in rows:
    print(f"{uid:<6} {name:<16} {model:<8} {phase:<10} ${spend:<11.4f} {str(budget)}")
PY
```

---

## 汇总模板

向用户汇报时使用如下格式：

```
Her 消费统计（最近 7 天 / 截至 {today}）

排名  ID    名称             模型      今日($)    累计($)    状态
----  ----  ---------------  -------  ---------  ---------  -------
 1    1042  张三的her         opus     12.3456    89.1234    Running
 2    1005  李四的her         gpt       8.7890    67.4321    Running
 3    1018  王五的her         sonnet    0.0000    45.6789    Paused
...
共 117 个 litellm 实例，今日总消费 $XX.XX，近 7 天总消费 $XXX.XX
```

注意：
- 消费 = 0 的实例可能是今日未使用，或 `provider != litellm`
- `Paused` 实例无消费，可作为对照
- 如需按模型/天/实例钻取，使用方式 2d（PostgreSQL 直查）

---

## 常见问题

| 问题 | 原因 | 处理 |
|------|------|------|
| 某实例不在 spend 列表 | provider 不是 litellm | 确认 CRD `spec.provider` |
| spend 数据与预期差距大 | LiteLLM 按 token 估算 cost，不含税费 | 以平台账单为准 |
| `/spend/logs` 数据不全 | 单页 1000 条但已分页拉取 | 检查分页循环是否正常退出，或改用 DB 直查 |
| port-forward 中断 | 连接超时 | 重新运行 port-forward 命令 |
