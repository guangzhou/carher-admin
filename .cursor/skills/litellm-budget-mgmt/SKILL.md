---
name: litellm-budget-mgmt
description: >-
  Manage daily/periodic budget limits for LiteLLM virtual keys (claude-code-*,
  cursor-*, carher-* etc). Use when the user mentions "限额" / "预算" / "budget"
  / "每日额度" / "max_budget" / "budget_duration", or wants to set, modify,
  remove, or query spending limits on LiteLLM keys.
---

# LiteLLM Key 预算管理

通过 LiteLLM `/key/update` API 热更新 key 的预算限制，**无需重启服务**；批量/排除式调整也支持直接 SQL UPDATE（DB 改完 60s 内 router cache 失效自动生效，无需重启 proxy）。

## 适用环境矩阵

本 skill 三套环境共用一套语义，但**入口和操作方式不一样**：

| 环境 | namespace / 入口 | 用户 | 操作 推荐方式 |
|------|-----------------|------|--------------|
| **aliyun bot** | `carher` (litellm.carher.net) | `carher-*` Her bot keys | `/key/update` API（见下方"操作流程"）|
| **198 prod** | `litellm-product` (cc.auto-link.com.cn/pro) | `claude-code-*` + `cursor-*` 内部 IDE 用户 | **直 SQL UPDATE**（见"198 prod 批量 SQL 速查"）|
| **198 dev** | `litellm-dev` (cc.auto-link.com.cn/dev) | dev 测试 key | 同 198 prod |

**为什么 198 不用 /key/update**：批量 + 白名单排除 + "max_budget > N AND key_alias NOT IN (...)" 这种范围 update，SQL 一条搞定；走 API 要先列 key 再逐个 POST，N+1 慢且没事务。**只调几个特定 key** 才走 API。

## 198 prod 批量 SQL 速查（2026-05-18 实战路径）

**场景**：调整 prod claude-code-\* 或 cursor-\* key 的每日额度，可能要"全员降到 X，但白名单 N 人保留原值"。

```bash
scripts/jms ssh AIYJY-litellm '
DB_POD=litellm-db-0
DB_URL=$(kubectl -n litellm-product exec deploy/litellm-proxy -- env 2>/dev/null | grep DATABASE_URL)
PG_PW=$(echo "$DB_URL" | sed -E "s|.*://[^:]+:([^@]+)@.*|\1|")

# STEP 1: 预览（必做）—— SQL 单引号一定要转义对！见下方坑 1
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
SELECT key_alias, max_budget, budget_duration, ROUND(spend::numeric,2) AS spend
  FROM \"LiteLLM_VerificationToken\"
 WHERE key_alias LIKE '"'"'claude-code-%'"'"'
   AND max_budget > 70
   AND key_alias NOT IN ('"'"'claude-code-buyitian'"'"', '"'"'claude-code-biancaoming-x36t'"'"')
 ORDER BY max_budget DESC;"

# STEP 2: UPDATE（RETURNING 让你看到改了什么）
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
UPDATE \"LiteLLM_VerificationToken\"
   SET max_budget = 70, updated_at = NOW()
 WHERE key_alias LIKE '"'"'claude-code-%'"'"'
   AND max_budget > 70
   AND key_alias NOT IN ('"'"'claude-code-buyitian'"'"', '"'"'claude-code-biancaoming-x36t'"'"')
RETURNING key_alias, max_budget, ROUND(spend::numeric,2) AS spend;"

# STEP 3: 终态分布
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
SELECT max_budget, COUNT(*) FROM \"LiteLLM_VerificationToken\"
 WHERE key_alias LIKE '"'"'claude-code-%'"'"' GROUP BY max_budget ORDER BY max_budget DESC;"'
```

### 坑 1：bash + kubectl exec + psql 嵌套引号 — `"..."` 在 SQL 里是**列名引用** ⚠️

PostgreSQL 用双引号引列名（`"LiteLLM_VerificationToken"`），用**单引号**引字符串（`'claude-code-buyitian'`）。把字符串写成双引号 → PG 解析为列名 → `ERROR: column "..." does not exist` → **UPDATE 完全没生效但脚本不报错**（kubectl exec 还回 exit 0，因为打印的 ERROR 走 stdout）。

bash 单层包裹 `'...'` 内不能直接写单引号，必须用 `'"'"'`（关、加双引号转义的单引号、重开）四字符序列。完整示例：

```
key_alias NOT IN ('"'"'name1'"'"', '"'"'name2'"'"')
       ↑     ↑     ↑      ↑     ↑     ↑      ↑     ↑
       SQL 单引号开始    SQL 单引号结束（中间用 bash 转义序列）
```

**检测方法**：UPDATE 后用 SELECT 验证 max_budget 确实变了。WHERE 条件如果用列名当字符串，SELECT 会出意外结果（如全表命中或全空）。

### 坑 2：SQL UPDATE 不触发 LiteLLM router 缓存失效

直 SQL 写 DB 后，LiteLLM proxy 内存里的 key 对象**60s 内还是旧值**（router 缓存）。生产可接受（用户 retry 一下就好），但要立即生效得 `kubectl rollout restart deployment/litellm-proxy` 或调用 `/cache/flushall`。**预算上调不急**（用户暂时无感）、**预算下调要等 60s 才严格生效**（窗口期可能放过几个超额请求）。

### 坑 3：DELETE / TRUNCATE 之类的写操作要先 BEGIN

PG psql 命令行 default autocommit，单条 UPDATE 就是单条事务。对 ≥ 1000 行的批量 UPDATE，**先 EXPLAIN 看影响行数**再执行，避免 WHERE 写错把全表干了。

## 关键概念

| 字段 | 含义 | 示例值 |
|------|------|--------|
| `max_budget` | 单周期预算上限（美元） | `100.0` |
| `budget_duration` | 预算重置周期 | `1d`（每天）、`7d`（每周）、`30d`（每月） |
| `budget_reset_at` | 下次重置时间（自动设置） | `2026-04-17T00:00:00+00:00` |
| `spend` | 当前周期已消费金额 | `42.5` |

- `max_budget` + `budget_duration` 配合 = **周期性预算**（如每天 $100）
- 只设 `max_budget` 不设 `budget_duration` = **总预算**（用完即止，不重置）
- 超额后 LiteLLM 自动拒绝请求，到 `budget_reset_at` 时 spend 归零自动恢复

## Key 别名命名规则与默认限额

| 前缀 | 用途 | 默认每日限额 |
|------|------|-------------|
| `carher-*` | Her bot 实例（aliyun carher namespace） | **$100/天**（2026-05-23 确立为强制默认） |
| `claude-code-*` | Claude Code / CLI 开发者账户 | $100/天（prod 2026-05-18 起降为 $70/天） |
| `cursor-*` | Cursor IDE 开发者账户 | $100/天 |

> 🟢 **2026-05-23 — carher-\* 默认 $100/天 确立为强制策略**：新建 her 实例（Admin API batch-import 自动生成 `carher-{uid}` virtual key）和**任何手动新建 carher-\* key 都必须立刻设 $100/day + budget_duration=1d**——admin API 当前不会自动 set，要在 [[add-instances]] 创建后调用本 skill 的"操作流程"或 `scripts/litellm-key-budget.py`。3 个特批高额度保留不动：carher-2 ($300), carher-11 ($200), carher-94 ($150)。当时 234 个 carher-\* key 6 个无限额已补齐。

> 🟡 **2026-05-18**：prod 把 claude-code-\* 默认从 $100/天 → **$70/天**（337 个 key 批量降）。4 个白名单保留高额度：buyitian ($1200), biancaoming-x36t ($600), linsen-rg9t ($500), liuguoxian-50gj ($500)。cursor-\* 保持 $100/天。当前 prod 上限合计 ~$26k/天。

## 前置

1. 集群连通性：`kubectl get nodes`。若 `connection refused`，按
   `k8s-via-bastion` skill 启动 kubectl 隧道：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

2. Port-forward LiteLLM：

```bash
kubectl port-forward -n carher svc/litellm-proxy 4000:4000 &
```

3. 获取 Master Key：

```bash
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
```

## 操作流程

### 1. 查询当前 key 的预算状态

拉取所有 key 的 spend 信息并按前缀过滤：

```bash
curl -s "http://127.0.0.1:4000/spend/keys?limit=600" \
  -H "Authorization: Bearer $MASTER_KEY" -o /tmp/spend_keys.json

python3 << 'PY'
import json
with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)
for r in rows:
    a = r.get("key_alias") or ""
    if a.startswith("claude-code-") or a.startswith("cursor-"):
        print(f'{a}: spend={r.get("spend",0)}, max_budget={r.get("max_budget")}, budget_duration={r.get("budget_duration")}')
PY
```

查看单个 key 详情：

```bash
curl -s "http://127.0.0.1:4000/key/info?key=<token>" \
  -H "Authorization: Bearer $MASTER_KEY" | jq '.info | {key_alias, max_budget, budget_duration, spend, budget_reset_at}'
```

### 2. 批量设置每日预算

**aliyun carher-\* 默认场景优先用脚本** `scripts/litellm-key-budget.py`（自动 port-forward + 拿 master key + idempotent，2026-05-23 起 carher-\* 默认 $100/天的强制 enforcer）：

```bash
# 给所有"无限额"的 carher-* 补 $100/day，已有限额的不动（适合新建 her 后做 enforcer）
scripts/litellm-key-budget.py --apply

# 精确指定（如 batch-import 刚创建的 ID）
scripts/litellm-key-budget.py --apply --key carher-234 --key carher-235

# 强制覆盖现有限额（不会动 3 个特批白名单：carher-2/11/94）
scripts/litellm-key-budget.py --apply --force

# 自定义额度（如某 her 临时调到 $200）
scripts/litellm-key-budget.py --apply --force --key carher-99 --budget 200

# 只查看当前状态
scripts/litellm-key-budget.py --inspect
```

**手动批量场景**（cursor-\*、claude-code-\*、或非 aliyun 环境）用 Python 脚本通过 `/key/update` 逐个更新。`/key/update` 是热更新，立即生效，不需要重启。

```bash
python3 << 'PYEOF'
import json, urllib.request

MASTER_KEY = "<master_key>"
BASE_URL = "http://127.0.0.1:4000"
TARGET_PREFIXES = ("claude-code-", "cursor-")  # 按需修改
MAX_BUDGET = 100.0      # 美元
BUDGET_DURATION = "1d"   # 每天重置

with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)

targets = [r for r in rows if (r.get("key_alias") or "").startswith(TARGET_PREFIXES)]
print(f"Updating {len(targets)} keys: max_budget={MAX_BUDGET}, budget_duration={BUDGET_DURATION}")

success = failed = 0
for i, r in enumerate(targets):
    payload = json.dumps({
        "key": r["token"],
        "max_budget": MAX_BUDGET,
        "budget_duration": BUDGET_DURATION
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/key/update", data=payload,
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        success += 1
    except Exception as e:
        failed += 1
        print(f"  FAIL: {r.get('key_alias')}: {e}")
    if (i+1) % 50 == 0:
        print(f"  Progress: {i+1}/{len(targets)}")

print(f"Done: {success} success, {failed} failed")
PYEOF
```

### 3. 重置所有目标 key 的 spend（关键！）

设置 `budget_duration` 后，LiteLLM 拿当前 `spend` 与 `max_budget` 比较。`spend` 是**历史累计值**，不会因为新设 `budget_duration` 而自动归零。

**必须重置所有目标 key 的 spend 为 0**，否则历史消费会算入新周期，导致：
- 累计 $89 的 key 只能再用 $11 就触发 $100 限额（实际今天才用了 $11）
- 用户看到 "Budget exceeded" 但完全不理解为什么

```bash
python3 << 'PY'
import json, urllib.request

MASTER_KEY = "<master_key>"
BASE_URL = "http://127.0.0.1:4000"

with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)

targets = [r for r in rows
           if (r.get("key_alias") or "").startswith(("claude-code-","cursor-"))
           and (r.get("spend") or 0) > 0]

print(f"Resetting spend for {len(targets)} keys...")
for alias, token in [(r["key_alias"], r["token"]) for r in targets]:
    payload = json.dumps({"key": token, "spend": 0.0}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/key/update", data=payload,
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  FAIL: {alias}: {e}")
print("Done")
PY
```

### 4. 验证

```bash
python3 << 'PY'
import json, urllib.request

MASTER_KEY = "<master_key>"
req = urllib.request.Request(
    "http://127.0.0.1:4000/spend/keys?limit=600",
    headers={"Authorization": f"Bearer {MASTER_KEY}"}
)
rows = json.loads(urllib.request.urlopen(req, timeout=30).read())

correct = blocked = total = 0
for r in rows:
    a = r.get("key_alias") or ""
    if a.startswith("claude-code-") or a.startswith("cursor-"):
        total += 1
        if r.get("max_budget") == 100.0 and r.get("budget_duration") == "1d":
            correct += 1
        if (r.get("spend") or 0) > (r.get("max_budget") or float("inf")):
            blocked += 1

print(f"Total: {total}, Correct: {correct}, Currently blocked: {blocked}")
PY
```

### 5. 移除预算限制

恢复为不限额度：

```python
payload = json.dumps({
    "key": "<token>",
    "max_budget": None,
    "budget_duration": None
}).encode()
```

批量移除参照步骤 2 的脚本结构，将 `MAX_BUDGET` 和 `BUDGET_DURATION` 改为 `None`。

## 注意事项

- `/key/update` 是**热更新**，立即生效，无需重启 LiteLLM proxy
- 首次设置预算后**必须重置所有目标 key 的 spend 为 0**（步骤 3），否则历史累计消费会算入新周期
- `carher-*` key 关联 bot 实例，修改前确认业务影响
- 脚本中 `MASTER_KEY` 不要硬编码到日志或聊天中，运行时从 Secret 获取
- 每次操作约 500ms/key，556 个 key 约需 4-5 分钟
