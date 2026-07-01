# quota-rebalance.py 设计与运维笔记

ChatGPT acct 池（198 prod K3s + 188 docker + 187 docker）的上游配额探测 + LiteLLM
路由自动 pause/resume 的实现细节、故障模式、运维入口。

跟脚本本身一一对应：`scripts/quota-rebalance.py`（repo 版本可能落后 188 现场版，
现场为准）。

## 全景

```
 (5min cron on 188)
   ↓
quota-rebalance.py
   ↓
读 /home/cltx/.chatgpt-quota/state/state.json
   ↓
for acct in POOL_ACCOUNTS:
    should_probe(acct, state) ──→ 决定要不要探
        ↓
    parse_account_*(acct) ──→ 拿 token (本地/187/198 K3s)
        ↓
    fetch_usage(tok, aid) ──→ HTTPS GET chatgpt.com/backend-api/codex/usage
        ↓
    classify(usage) ──→ (tier, p_pct, w_pct, restore_at, ...)
        ↓
    diff state → trigger LiteLLM /model/delete or /model/new
        ↓
写回 state.json + 飞书边沿告警
```

没有 daemon、没有 MQ、没有 watcher。每 5min 重新跑一次单 .py，一份 state.json
是 source of truth。

## 触发与目录布局

188 (`JSZX-AI-03`) 上的 user crontab：

```
*/5 * * * * /usr/bin/env -S bash -c "set -a; source /home/cltx/.chatgpt-quota/env; set +a; \
            /usr/bin/python3 /home/cltx/quota-rebalance.py >> /home/cltx/.chatgpt-quota/cron.log 2>&1"
```

| 路径 | 内容 |
|---|---|
| `/home/cltx/quota-rebalance.py` | live 脚本（repo `scripts/quota-rebalance.py` 是落后副本）|
| `/home/cltx/.chatgpt-quota/env` | LITELLM_BASE / LITELLM_MK / SSH_* 等敏感 env |
| `/home/cltx/.chatgpt-quota/state/state.json` | 每个 acct 的当前 paused / pct / restore_at / ts |
| `/home/cltx/.chatgpt-quota/cron.log` | 全部历史运行轨迹（唯一日志）|
| `/home/cltx/quota-rebalance.py.bak-*` | 改 live 前的自动备份 |

## should_probe — 探测频率决策

state.json 里每个 acct 都有一个 `ts`（上次探的时间）。每轮先决定要不要重新探，
避免对 chatgpt.com 高频施压、被 CF 拦：

| 当前状态 | 行为 |
|---|---|
| `manual_offline=True` + 距上次 <6h | SKIP（防 token 死了反复探 401）|
| `manual_offline=True` + 距上次 ≥6h | PROBE（token 自愈试探）|
| `paused=True` + now < restore_at | SKIP（reset 没到不试）|
| `paused=True` + now ≥ restore_at | PROBE（看 quota 是否真 reset）|
| `online`，上次 5h% ≥ 80 | 每轮 PROBE（高位 close monitor）|
| `online`，上次 5h% ≥ 50 | PROBE iff ≥12min since last |
| `online`，上次 5h% < 50 | PROBE iff ≥25min since last |

`should_probe` 返回 `False` 时只更新 `ts` 不打上游，省 quota。

## 拿 token 的三条路径

`POOL_ACCOUNTS[acct]["location"]` 决定走哪条：

| location | 拿 token 的方法 |
|---|---|
| `188`（默认 / 本地）| `/Data/chatgpt-auth/{acct}/auth.json` 本地 `open` |
| `187`（已废弃 docker）| `ssh cltx@10.68.13.187 "cat /Data/chatgpt-auth/{acct}/auth.json"` |
| `198`（K3s litellm-product ns）| `ssh AIYJY-litellm "kubectl exec ${pod} -- cat /chatgpt-auth/auth.json"` |

`auth.json` 里取的两个字段：

- `access_token` — 调上游 /codex/usage 的 Bearer
- `account_id` — header `chatgpt-account-id`（fallback：从 `id_token` JWT claims
  里的 `chatgpt_account_id` 取）

`parse_auth_json` 还会解 `id_token` JWT 的 payload 拿 `chatgpt_subscription_active_until`
（订阅到期日，只用于 quota.sh 视图展示）。**id_token 可能是占位符（例如 "x"）或
缺失**——这种情况要 graceful skip claim 解析、不要 IndexError（2026-06-16 acct-2
踩坑）。

### 198 那条 ssh 跳跃为什么重要

188→198 这一跳是脚本里**最容易抖动**的，因为：

- 路径长：188 → 公司 jumpserver 隧道 → 198 sshd → K3s API server → kubectl exec
  → cat
- 任何一跳 RST/timeout 整个 ssh 都失败
- 早期实现：每个 198 acct 都重新握手一次 ssh（24 acct × 1-2s 握手 = 30-50s
  纯握手开销）

2026-06-16 强化后：

```python
ssh_args = [
    "ssh",
    "-o", "ConnectTimeout=5",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/cm-quota-198-%r@%h:%p",
    "-o", "ControlPersist=10m",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    f"{SSH_198_USER}@{SSH_198_HOST}",
    kc_cmd,
]
```

- **ControlMaster**：同一轮 cron 内 24 个 198 acct 共享一次 ssh 握手
- **远端 fast-fail**：kc_cmd 里先 `kubectl get pod -l app=...` 取 POD，空则
  `exit 42`，python 端识别 returncode==42 立刻 raise 不进重试
- **重试 ×2**：仅对真 TimeoutExpired / 非 42 非零 returncode 重试，间隔 0.5s 递增

效果：整轮 cron 249s → 44s（5.6×）。

## fetch_usage — 上游探针

```
GET https://chatgpt.com/backend-api/codex/usage
Headers:
  Authorization: Bearer {access_token}
  chatgpt-account-id: {account_id}
  Originator: codex_cli_rs
  User-Agent: codex_cli_rs/0.30.0 (Linux; x86_64)
```

返回的关键字段：

```json
{
  "rate_limit": {
    "primary_window":   { "used_percent": 0,   "reset_after_seconds": 18000 },
    "secondary_window": { "used_percent": 100, "reset_after_seconds": 144089 }
  },
  "additional_rate_limits": [
    { "limit_name": "GPT-5.3-Codex-Spark", "rate_limit": {...} }
  ],
  "rate_limit_reached_type": {...}
}
```

- `primary_window` = **5h codex 配额**
- `secondary_window` = **7d codex 配额**
- 这个端点**只**反映 codex 轨道。**gpt-5（非 codex）走另一条配额轨道**——它撞顶
  的话 LiteLLM 路由会拿到 429 + `usage_limit_reached / plan_type=pro`，但 quota.sh
  的 5h%/7d% 不会反映。
- 401 走 3 次重试（区分 CF 瞬态 vs token 真死）；其它 HTTPError 不重试。

## classify — 分类阈值

```python
if w_pct >= 95:  return "OFFLINE-WEEK"
if p_pct >= 95:  return "OFFLINE-5H"
if p_pct >= 50 or w_pct >= 50:  return "SLOW"
return "HEALTHY"
```

> 注：188 现场版阈值已升到 99（更保守，少 cooldown）；repo 副本可能仍是 95，
> 以现场为准。

`should_offline = tier in ("OFFLINE-5H", "OFFLINE-WEEK")` —— 这就是触发
`pause_acct()` 的开关。

## LiteLLM 路由变更

### pause（应该下线）

```
POST /model/delete  body={"id": "chatgpt-acct-N-gpt-5.5"}
POST /model/delete  body={"id": "chatgpt-acct-N-gpt-5.4"}
POST /model/delete  body={"id": "chatgpt-acct-N-gpt-5.3-codex"}
```

三条 entries 是 LiteLLM `LiteLLM_ProxyModelTable` 里 acct-N 的全部 chatgpt model
group 成员。删完后 router in-memory cache 几十秒收敛，新请求不再路由到 acct-N。

> ⚠️ **PATCH rpm=0 / blocked=true 都不管用**——v1.85 是 post-selection limiter，
> 不影响路由选择阶段。Cooldown 必须走 /model/delete 物理移除（参考
> `feedback_litellm_rpm0_blocked_dont_block_routing.md`）。

### resume（应该上线）

```
POST /model/new
body={
  "model_name": "chatgpt-gpt-5.5",
  "litellm_params": {
    "model": "openai/chatgpt-gpt-5.5",
    "api_base": "http://chatgpt-acct-N.litellm-product.svc.cluster.local:4000",
    "api_key": "x",  # provider-side ignore
    ...
  },
  "model_info": {"id": "chatgpt-acct-N-gpt-5.5"}
}
```

**`model_info.id` 必须显式给**，不传 LiteLLM 会自动生成 UUID 不可读不可批量
处理（参考 `feedback_litellm_model_new_must_pass_id.md`）。

## state.json 一行字段

```json
"acct-2": {
  "primary_pct": 0,
  "weekly_pct": 100,
  "paused": true,
  "manual_offline": false,
  "restore_at": 1781743197,
  "primary_resets_at": 1781616963,
  "weekly_resets_at": 1781743052,
  "subscription_active_until": "2026-07-09T04:27:30+00:00",
  "subscription_checked_at": 1781595096,
  "consecutive_401": 0,
  "consecutive_probe_err": 0,
  "tier": "OFFLINE-WEEK",
  "cause": "wk=100%>=95",
  "ts": 1781599108,
  // TOKEN_INVALID 自愈：
  "repair_attempts": 0,
  "last_repair_at": 0,
  "repair_frozen": false
}
```

**两个独立维度**：

- `paused` — quota 撞顶导致的"暂时下线"，restore_at 到了自动 resume
- `manual_offline` — token 死/网络坏导致的"严重下线"，每 6h 探 1 次自愈

视图里 `take=yes` 的判定：`not paused and not manual_offline`。

## 自愈链路

| 触发 | 自愈路径 |
|---|---|
| `paused=True` + now ≥ restore_at | 下一轮 PROBE → HEALTHY → `/model/new` 加回 |
| `manual_offline=True` + 距上次探 ≥ 6h | 下一轮 PROBE → 非 401 即召回 |
| `tier=TOKEN_INVALID` + 距上次 repair ≥12h + 累计 <5 次 | 自动 re-OAuth 链路（188 现场版有此模块）：`re-oauth.sh GEN_ONLY=1` → ssh AIYJY-litellm + `kubectl cp` + rollout restart |
| `consecutive_probe_err ≥ 3` | 飞书边沿告警（不阻流量）|

`repair_frozen=True` 表示 5 次自动 re-OAuth 都失败，进人工修复队列。

## 故障定位入口（按"看到的现象"）

### 现象 A：quota.sh 视图里 acct 显示 0% 但实际上游 100%
（也就是 2026-06-16 acct-24 那种 BLEEDING）

```bash
# 1. 直接探上游
jms ssh AIYJY-litellm 'POD=$(kubectl -n litellm-product get pod -l app=chatgpt-acct-N -o jsonpath={.items[0].metadata.name})
kubectl -n litellm-product exec $POD -- python3 -c "
import json, urllib.request
a=json.load(open(\"/chatgpt-auth/auth.json\"))
req=urllib.request.Request(\"https://chatgpt.com/backend-api/codex/usage\",
  headers={\"Authorization\":f\"Bearer {a[\\\"access_token\\\"]}\",
           \"chatgpt-account-id\":a[\"account_id\"],
           \"Originator\":\"codex_cli_rs\",
           \"User-Agent\":\"codex_cli_rs/0.30.0 (Linux; x86_64)\"})
r=json.loads(urllib.request.urlopen(req,timeout=15).read())
rl=r[\"rate_limit\"]
print(\"5h:\", rl[\"primary_window\"][\"used_percent\"])
print(\"7d:\", rl[\"secondary_window\"][\"used_percent\"])"'

# 2. 看 cron.log 看那个 acct 最近几次 probe 是否在 error
jms ssh JSZX-AI-03 'grep "acct-N:" ~/.chatgpt-quota/cron.log | tail -10'

# 3. 看 state.json ts 是否 stale
./scripts/chatgpt-acct-quota.sh --json | python3 -c "
import json, sys, time
d=json.load(sys.stdin)
r=d['acct-N']
print('age:', time.time()-r['ts'], 's')"
```

### 现象 B：批量 acct 都 SSH timeout

```bash
# 看是不是 188→198 这一跳的整体抖动
jms ssh JSZX-AI-03 'tail -100 ~/.chatgpt-quota/cron.log | grep "SSH to 198 timeout"'

# 看 ControlMaster socket
jms ssh JSZX-AI-03 'ls -la /tmp/cm-quota-198-*'
```

### 现象 C：手动止血（不等 cron）

```bash
# 1. /model/delete 把 acct-N 从路由移除
jms ssh AIYJY-litellm 'MK=$(kubectl -n litellm-product get secret litellm-secrets -o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d)
kubectl -n litellm-product port-forward svc/litellm-proxy 14000:4000 >/dev/null 2>&1 &
PF=$!; sleep 2
for id in chatgpt-acct-N-gpt-5.5 chatgpt-acct-N-gpt-5.4 chatgpt-acct-N-gpt-5.3-codex; do
  curl -s -X POST -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
    -d "{\"id\":\"$id\"}" http://localhost:14000/model/delete
done
kill $PF 2>/dev/null'

# 2. 同步 state.json：标 paused=True + restore_at
jms ssh JSZX-AI-03 'python3 - <<PY
import json, time
p="/home/cltx/.chatgpt-quota/state/state.json"
s=json.load(open(p))
now=int(time.time())
s["acct-N"].update({
  "primary_pct":100, "paused":True, "restore_at":now+18000,
  "primary_resets_at":now+18000, "tier":"OFFLINE-5H",
  "cause":"manual sync (reason)", "ts":now,
})
open(p,"w").write(json.dumps(s,indent=2))
PY'
```

接下来 quota-rebalance 会按 restore_at 自然 self-heal（探到 HEALTHY → /model/new
加回），不需要再人工介入。

## 已知设计缺陷与待办

1. **188 跑探 198 是历史包袱**：早期 acct 分布在 188/187 docker，198 K3s 是后来的，
   迁移成本（state、cron、TOKEN_INVALID 自愈 chromium 环境）阻止把 cron 挪到 198。
   2026-06-16 已通过 ssh 强化（ControlMaster + fast-fail + 重试）把痛点从分钟级
   降到秒级。

2. **/codex/usage 只反映 codex 轨道**：gpt-5（非 codex）撞顶时 quota.sh 视图
   会"看起来很干净"但 LiteLLM 路由实际持续 429。识别要看 LiteLLM 路由层
   `x-litellm-attempted-fallbacks` 或 Pod log 里的 429。

3. **POOL_ACCOUNTS 跟 K3s 现场漂移**：今天发现配置里有但现场没 Pod 的（acct-15、
   acct-32），以及现场有配置里没的（acct-12、acct-39、acct-40）。fast-fail 让
   前者不再阻塞 cron，但 hygiene 应清。

4. **repo 跟 188 live 版本漂移**：188 live 比 repo 多 acct-34~38、TOKEN_INVALID
   自愈模块、99% 阈值。每次改前必须先 `jms scp JSZX-AI-03:/home/cltx/quota-rebalance.py /tmp/qr-on-188.py`
   diff 一下。

## 相关 memory / skill

- `feedback_litellm_rpm0_blocked_dont_block_routing.md` — pause 必须 /model/delete
- `feedback_litellm_model_new_must_pass_id.md` — resume 必须显式 model_info.id
- `feedback_quota_rebalance_manual_offline_transient_401.md` — 瞬态 401 误判
- `feedback_aliyun_ip_blocked_chatgpt_web.md` — 阿里云 IP 调 chatgpt web 端点 CF 403
  （仅影响阿里云池，198 池不受影响）
- `chatgpt-acct-manual-cooldown` skill — 手动 cooldown 完整 SOP
- `chatgpt-acct-close-wait-restart` skill — chatgpt-acct Pod 死链巡检
