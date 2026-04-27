---
name: litellm-budget-mgmt
description: >-
  Manage daily/periodic budget limits for LiteLLM virtual keys (claude-code-*,
  cursor-*, carher-* etc). Use when the user mentions "限额" / "预算" / "budget"
  / "每日额度" / "max_budget" / "budget_duration", or wants to set, modify,
  remove, or query spending limits on LiteLLM keys.
---

# LiteLLM Key 预算管理

通过 LiteLLM `/key/update` API 热更新 key 的预算限制，**无需重启服务**。

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
| `carher-*` | Her bot 实例（慎改，改前确认业务影响） | $150/天 |
| `claude-code-*` | Claude Code / CLI 开发者账户 | $100/天 |
| `cursor-*` | Cursor IDE 开发者账户 | $100/天 |

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

用 Python 脚本通过 `/key/update` 逐个更新。`/key/update` 是热更新，立即生效，不需要重启。

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
