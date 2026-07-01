# T2 canary 总结 — LiteLLM v1.89.4 router 层 reactive cooldown

> 测试时间：2026-06-27 / 2026-06-28（198 `litellm-product` ns）
> 验证方法：Plan B 升级版 — 独立 `chatgpt-acct-canary-N` deployment_id 隔离 Redis cooldown key 命名空间
> 结论：**reactive cooldown 在 v1.89.4 vanilla + capacity-patch 上可工作，但要走 v2 路径（删 `allowed_fails`）**

---

## TL;DR

1. v1.89.4 router 的 cooldown decision tree 有 **v2 / v1 legacy 双路径**，由 `_is_allowed_fails_set_on_router` 决定
2. **prod 当前 `allowed_fails=3 cooldown_time=60` → 走 v1 legacy 路径**，要 `updated_fails > allowed_fails` 严格大于才 cooldown（4 次失败才触发）— **当前 reactive cooldown 实际上被禁用**
3. T2 在 canary 上 **删除 `allowed_fails`** → router 走 v2 路径 → **第 1 次 429 立即** 调 `_set_cooldown_deployments` → 写 Redis `deployment:<id>:cooldown` key
4. canary 通过 `model_info.id = chatgpt-acct-canary-N` 命名空间隔离 → prod Redis cooldown set 0 污染 ✅

---

## 验证矩阵

| 检查项 | 结果 | 证据 |
|---|---|---|
| canary CM 应用成功 | ✅ | `kubectl rollout status` 0 exit + `/v1/model/info` 返 2 entries |
| 删 allowed_fails 后 CM 实际生效 | ✅ | `router_settings.allowed_fails: None`，pod 重启后从 in-memory 也 None |
| canary-68 上游真撞顶 → 429 | ✅ | upstream 真返 `usage_limit_reached`，capacity-patch 升格为 `litellm.RateLimitError` |
| 第 1 次 429 触发 cooldown | ✅ | call 1 之后 Redis 出现 `deployment:chatgpt-acct-canary-68:cooldown` |
| 后续路由避开 cooldown deployment | ✅ | call 2 全部到 canary-49 |
| prod `/model/info` 看到 canary 入口？ | ❌ | False（store_model_in_db=False + 独立 CM） |
| prod Redis 出现 canary 触发的 cooldown？ | ✅ | `deployment:chatgpt-acct-canary-*:cooldown` 命名空间与 prod `deployment:chatgpt-acct-N:cooldown` 不交叉 |
| prod cooldown set 与 baseline 差异 | ✅ | 完全一致（baseline 空 / 当前空） |

---

## 根因诊断（三段式）

**假设 A**：T1 dev POC 里 `allowed_fails=1 cooldown_time=3600` → 1 次失败 cooldown
**证伪条件**：T2 复现 1 次 429 触发 cooldown key
**实际数据**：T2 第一次跑 6 个 fail 0 cooldown key — **假设 A 错**

**新假设 B**：`allowed_fails` 显式设置改变了 cooldown 决策路径
**证伪条件**：源码里看是否有 `_is_allowed_fails_set_on_router` 类分支
**实际数据**：找到 `_should_cooldown_deployment` 第一行就是

```python
if (
    litellm_router_instance.allowed_fails_policy is None
    and _is_allowed_fails_set_on_router(litellm_router_instance) is False
):
    # v2: 429 → return True 直接
    ...
else:
    return should_cooldown_based_on_allowed_fails_policy(...)  # v1 legacy
```

`should_cooldown_based_on_allowed_fails_policy` 关键：

```python
current_fails = litellm_router_instance.failed_calls.get_cache(key=deployment) or 0
updated_fails = current_fails + 1
if updated_fails > allowed_fails:  # 严格大于
    return True
else:
    litellm_router_instance.failed_calls.set_cache(...)
return False
```

**假设 B 成立**：设 `allowed_fails=1` → 第 1 次失败 `updated_fails=1`，`1 > 1 = False` 不 cooldown。

---

## prod 当前状态的含义

prod CM `router_settings.allowed_fails=3 cooldown_time=60`：

- 撞顶 acct 的 429 反向打回 router，**只在 `current_fails+1 > 3` 即第 4 次失败** 才进 cooldown 60s
- 60s 后 cooldown 解除，router 又把同一撞顶 acct 拉回服务列表
- 之间 188 上 cron `quota-rebalance.py` 每 5min 一次跑 `/codex/usage` 主动探查并 `/model/delete` — **这才是当前真正干活的快速摘除链路**
- LiteLLM 内置 reactive cooldown 在当前 prod 配置下基本不动，只有偶发突发失败时短暂作用

这个状态是 `~/.claude/memory/project_litellm_198_cooldown_tune.md` 记录的"`cooldown_time 300→60s + allowed_fails 1→3` 缓解 encrypted_content_affinity 钉死 stateful Codex 会话"的副作用 — **当时为 sticky 会话妥协，关掉了 reactive cooldown**。

---

## 推广到 prod 的两条路径

| 方案 | 描述 | 风险 |
|---|---|---|
| A. 维持现状 | prod 继续走 cron quota-rebalance 主导，reactive cooldown 备用 | 0 改动；但 quota 撞顶后 0-5min 滞后期内仍走撞顶 acct |
| B. 改 v2 路径 | 删 `allowed_fails` + 改 `cooldown_time=3600`；reactive cooldown 1 次 429 立即生效 | **可能破坏 sticky Codex 会话**（同一会话第二次请求被路由到 cooldown 列表外的不同 acct → encrypted_content 不匹配） |

方案 B 启动前需要先：
1. 量化当前 prod 中 stateful Codex 会话的占比和 encrypted_content_affinity 命中率
2. canary 上加 sticky 行为回归（同 key 连发 3 次，所有请求是否同 deployment）
3. 1h 真用户压测对照 5xx/4xx 率不退化

**T2 不推进 promote 决策**。本文档仅证明 reactive cooldown **能** 生效；是否 promote、按什么阈值、怎么平衡 affinity 副作用 — 留给用户决策。

---

## canary endpoint（手动测）

```bash
# 198 上从 jms 跳进去
ssh AIYJY-litellm

# port-forward 到 198 本地 4001（jms tunnel 自动转发）
kubectl -n litellm-product port-forward svc/litellm-proxy-canary 4001:4000 &

# 5 个 sk-canary-rc-* 测试 key 在
cat /root/litellm-canary/keys/sk-canary-rc-keys.txt

# 手动 curl
SK=$(awk -F= '/sk-canary-rc-001=/{print $2}' /root/litellm-canary/keys/sk-canary-rc-keys.txt)
curl -sS http://localhost:4001/v1/responses \
  -H "Authorization: Bearer $SK" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chatgpt-canary-gpt-5.5",
    "input": [{"role":"user","content":[{"type":"input_text","text":"hello"}]}],
    "max_output_tokens": 64,
    "store": false
  }'
```

cursor / codex 接入同方法。**canary 只在集群内可达，外部不暴露**。

---

## 资源 / Teardown

| 资源 | 名 | 命名空间 |
|---|---|---|
| Deployment + Service | `litellm-proxy-canary` | `litellm-product` |
| ConfigMap | `litellm-config-canary` | `litellm-product` |
| Master Key Secret | `litellm-canary-master-key` | `litellm-product` |
| 测试 key 5 个 | `sk-canary-rc-001..005` | DB 共享（但 `STORE_MODEL_IN_DB=False` 下不入 ProxyModelTable） |
| Redis cooldown key | `deployment:chatgpt-acct-canary-*:cooldown` | 共享 Redis |
| 上游 chatgpt-acct deploy scale | acct-49 / acct-68 `replicas=1` | `litellm-product` |

Teardown：

```bash
scripts/litellm-canary-reactive-cooldown-config.py --teardown
```

**默认保留 canary 给用户继续手动测**。明确不再需要时再 teardown。

---

## 相关 memory

- [[project_litellm_198_cooldown_tune]] — 当前 prod `allowed_fails=3` 来历
- [[feedback_litellm_callback_print_invisible_in_workers]] — debug 必用 `verbose_router_logger`
- [[feedback_litellm_models_column_text_array]] — DB schema 跨集群差异
- [[feedback_litellm_router_alias_inflation]] — `/model/info` 行数 ≠ 真 deployment 数
- [[project_litellm_198_pro_capacity_patch]] — chatgpt 200+capacity 升格成 429 是 reactive cooldown 能识别 quota 撞顶的前提

---

## 待办（不在 T2 范围）

1. 量化 prod 当前 sticky Codex 会话占比（决定方案 B 风险）
2. canary 加 sticky 回归（同 key 连发 N 次的 affinity 行为）
3. 写一份对照 spike test：A 路径 cron-only vs B 路径 reactive+cron 的撞顶恢复时延
