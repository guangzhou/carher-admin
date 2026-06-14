---
name: carher-her-reply-failure-triage
description: |
  K8s carher her bot 用户报"Something went wrong while processing your request" / 不回复 / 慢 /
  半天没反应 等"reply 失败"症状的诊断决策树。本 skill 整合 2026-05-12/13/15 和 H75 升级调查的多类根因 +
  快速分诊 + 修复路径,避免每次都从零排查。Use when user 在飞书报"机器人坏了"/"不说话"/
  "看到 ⚠️ Something went wrong" 等消息,或要 audit 集群 her 健康度。
metadata:
  openclaw:
    emoji: "🚨"
---

# her bot reply 失败诊断决策树

升级或 rollout 之后出现不回复时，先加载 `carher-upgrade-flow`。本 skill 负责 reply 失败分诊；如果症状来自 `/hermes`、`/openclaw`、Dify Creator、Feishu home channel 或长消息压测，要把对应专项验收补齐，不要只修模型调用。

## Post-Upgrade First Checks

- 先确认消息是否到达目标 pod；没有到达就是 Feishu/channel/WS，不是模型。
- `无法识别飞书 chat_id` 先走 `carher-feishu-bench-regression` 的 exact-chat 注册流程。
- active engine 为 Hermes 时，必须确认 Hermes Feishu WS connected；没有 WS 时可先恢复 OpenClaw 保障服务。
- Dify 相关 403/1010 先查 internal URL：`dify-nginx` for workflow API, `dify-bootstrap` for lifecycle。
- `/dify` 或 Dify 登录入口失败先查配置，不要先等日志：Deployment env、`/data/.openclaw/workflow/dify-config.json`、`bot_id`、lifecycle URL、workspace/api/lifecycle token、bootstrap shared nonce marker；验收必须包含 issue-login 和 `/v1/exchange`。
- Hermes 报 `Unknown provider 'litellm'` 时修 Hermes runtime config，不切 Claude 绕过。当前目标是 `provider: litellm`、K8s 内网 LiteLLM URL、`transport: chat_completions`。
- title/footer/card 显示异常必须查对应 card/title patch 或 footer parser。普通文本回复成功不代表 card/footer 路径正确。
- group-at 相关问题要拆成两条链路：能不能回复看 inbound gate，footer 显示看 card/footer parser；两者都要验证。
- H75 升级后 `CrashLoopBackOff` 或 `PostStartHookError` 先查 Deployment hardening：base config、required secret env、writable mounts、initContainers；不要直接归因模型或源码。
- 如果用户要求“不要改源码”，只做 runtime/deployment/config 修复，并明确残留源码问题。

## "修复了吗" Closure Rule

Do not answer "已修复" from a patch/apply result alone. Close a no-reply incident only after the latest post-fix evidence proves the user-visible path.

Required closure evidence:

- Deployment/Pod is on the intended image/profile/env after rollout, not before rollout.
- Active engine is restored to `openclaw` unless the user explicitly wants Hermes left active.
- Recent logs have zero matching failure signatures for the original symptom, for example owner-block, `无法识别飞书 chat_id`, `No adapter available for feishu`, `ModuleNotFoundError`, LiteLLM/provider `NoneType`, or Cloudflare `403/1010`.
- If Feishu self-test is possible, a fresh marker gets a reply from the target `app_id`.
- If self-test is not possible because the operator is outside the exact chat, report `not_self_tested/current_operator_not_in_chat` and list the K8s/runtime gates that did pass.
- If the issue class affected one upgraded Her, scan all already-upgraded Her for the same signature before saying the fleet is clean.

## H75 Post-Upgrade Startup Failure Fast Path

Use this path when an upgraded Her is `1/2`, `CrashLoopBackOff`, `PostStartHookError`, or user-visible no-reply is caused by container restarts.

### 1. Old base config causes schema failure

**Signature**:

```text
Invalid config at /data/.openclaw/openclaw.json
agents.defaults: Unrecognized key: "llm"
```

**Root cause**: the Deployment still mounts old `carher-base-config`; H75 needs `carher-base-config-h75`. Because user `openclaw.json` can `$include` the mounted `carher-config.json`, the failure can look like a PVC/user config issue even when the bad input is the Deployment base ConfigMap.

**Fix**:

```bash
kubectl -n carher patch deploy "carher-$HER_ID" --type strategic -p \
  '{"spec":{"template":{"spec":{"volumes":[{"name":"base-config","configMap":{"name":"carher-base-config-h75","defaultMode":420}}]}}}}'
kubectl -n carher rollout status deploy/"carher-$HER_ID" --timeout=600s
```

Then scan the fleet for any H75 Deployment still mounting `carher-base-config`.

### 2. Missing H75 runtime secret env

**Signatures**:

```text
SecretRefResolutionError: Environment variable "CARHER_GATEWAY_TOKEN" is missing or empty
required secret env CARHER_PROD_KEY is missing
```

**Root cause**: partial H75 env hardening. H75 requires gateway/ACP/Dify token envs and `CARHER_PROD_KEY`. For current ACK H75, `CARHER_PROD_KEY` should equal that instance's `LITELLM_API_KEY`.

**Fix**:

- Add `CARHER_GATEWAY_TOKEN` from `carher-h75-runtime-secrets/CARHER_GATEWAY_TOKEN`.
- Add `ANTHROPIC_AUTH_TOKEN` from `carher-h75-acp-secrets/ANTHROPIC_AUTH_TOKEN`.
- Add `CARHER_DIFY_BOOTSTRAP_TOKEN` from `carher-dify-bootstrap-token/token`.
- Set `CARHER_REQUIRED_SECRET_ENVS=CARHER_PROD_KEY`.
- Set `CARHER_PROD_KEY` to the same value as `LITELLM_API_KEY`.

Re-read the rendered Deployment and pod env after rollout before closing.

### 3. H75 writable mount is still read-only

**Signature**:

```text
cp: cannot create directory '/data/.agents/skills/...': Read-only file system
```

or a similar write failure under:

```text
/data/.agents/skills
/data/.openclaw/skills
/data/.openclaw/local
/data/.openclaw/runtime-plugins
/data/.openclaw/extensions
/opt/data/.hermes/skills
/opt/data/skills
```

**Root cause**: old Deployment template has `volumeMounts[].readOnly=true`. Kubernetes strategic merge may not remove that field from array items even if the local patch object omits it.

**Fix**:

- Re-read the rendered Deployment.
- Replace the target container's `volumeMounts` with JSON Patch, explicitly setting all H75 writable mounts to `readOnly:false`.
- Roll out and verify the latest pod reaches `2/2 Running`.

Fleet scan:

```bash
kubectl -n carher get deploy -l app=carher-user -o json \
  | jq -r '.items[] | .metadata.name as $d |
    (.spec.template.spec.containers[] | select(.name=="carher") | .volumeMounts[]? |
    select((.name|test("^h75-(agent-skills|openclaw-local|runtime-plugins|openclaw-extensions|openclaw-skills|hermes-skills|hermes-opt-skills)$")) and (.readOnly==true)) |
    $d + " " + .name + " " + .mountPath)'
```

## 用户症状到根因的快速映射

收到 her "Something went wrong while processing your request. Please try again, or use /new..." → 这是 OpenClaw `agent-runner.runtime` 的 `GENERIC_EXTERNAL_RUN_FAILURE_TEXT`,**多类根因都会触发同一句兜底**。要分诊不能只看消息文字。

另一种症状："The model did not produce a response before the model idle timeout. Please try again, or increase models.providers.<id>.timeoutSeconds for slow local or self-hosted providers" —— 这是 OpenClaw 对 *客户端 turn timeout* 的 user-facing 模板（借鉴 Codex 的字段命名）。看到这文案**不要**去查 Codex 客户端配置，按下面 (7) 分诊。

```
用户报 her bot reply 失败
        │
        ├── (1) 飞书侧消息真到 bot 了吗?
        │       └── 看 her pod log "received message from ..." 时间戳
        │           ├── 没收到 → 飞书 → bot WebSocket 断了 (her bot pod 重启 / cloudflare-tunnel 异常)
        │           └── 收到了 → 进 (1b)
        │
        ├── (1b) log 里有没有 "channel reload still deferred" ?
        │       └── grep "reload still deferred" (每 30s 打一次)
        │           ├── 有,且持续数小时 + N reply(ies) active → 根因 F (hung reply 泄漏)
        │           └── 没有 → 进 (2)
        │
        ├── (2) bot dispatch 后 LiteLLM 怎么回?
        │       └── 看 LiteLLM SpendLogs (key_alias=carher-NNN, 最近)
        │           ├── 调用根本没 record (dur=0s, response={}) → 进 (3) 鉴权问题
        │           ├── 调用成功但 reply='NO_REPLY' → 进 (5) 模型行为 / discussion mode 残留
        │           ├── HTTP 400 'Budget has been exceeded' → 进 (6) budget 超额
        │           ├── 上游慢 / timeout / dur > 60s → 进 (7) reindex 死循环 / 上游故障
        │           └── timeout 窗口内 SpendLogs **0 条** + 文案是 "model idle timeout" → 进 (8) 根因 G prework cold scan stall
        │
        └── (3) 鉴权失败具体 error
                ├── 'Key not found in database' → LITELLM_API_KEY env 配错 (per-instance 漏覆盖)
                ├── 'model not in models[]' → user-config alias mismatch (#A 节)
                └── 401 Unauthorized → key 真失效 / Phase E 白名单清错了
```

## 已知根因 + 修复

### H. H75 group-at inbound gate / footer 双路径不一致

**症状 1：group-at 已开启但群里非 owner `@` 没回复**

常见于 H75 b600887 镜像。Redis 或提示文案显示 `group-at`，但 OpenClaw Lark inbound gate 读不到 Redis mode 后默认 `owner-at`，日志会出现：

```text
message in group oc_... mentioned bot but sender ou_... is not owner
```

**诊断**:

- 从 K8s/目标 pod 侧查，不用本地 macOS 验证脚本语义。
- 查当前 pod 最近 5-20 分钟日志是否有 `mentioned bot but sender ... is not owner`。
- 查 Deployment 是否有 `FEISHU_GROUP_POLICY=open`、`FEISHU_ALLOW_ALL_USERS=true`、`REDIS_URL`。
- 查 Redis `group:mode:<chat_id>:<app_id>` 是否为 `group-at`，并确认该 chat 在 `group:tracked:<app_id>`。

**不改源码时的修复**:

- 用目标 pod 自己的 Feishu app credentials 拉 exact home chat 成员，写入 Deployment `FEISHU_OWNER_OPEN_IDS`。
- 对日志里已经出现 owner-block 的非 home 群，至少把 exact blocked sender 加入 `FEISHU_OWNER_OPEN_IDS`；能拉群成员时再镜像该群当前成员。
- Redis 里把对应 `group:mode:<chat_id>:<app_id>` 设置为 `group-at`，并 `SADD group:tracked:<app_id> <chat_id>`。
- Rollout 后验证新 pod `2/2 Running`，最近 5 分钟 owner-block 为 0。
- 修完一个实例后，必须扫所有已升级目标的同类 owner-block 日志并一次性修复，不要等用户逐个报。

**症状 2：功能已是 group-at，但 footer 仍显示 `🔒主人@`**

这不是模型或 gate 功能失败，而是 card/footer 读取路径不同。H75 中 `feishu-her` 文本 footer 和 `@larksuite/openclaw-lark` streaming card footer 不是同一条代码路径；后者可能直接解析 Redis bulk response。

**诊断**:

- 用目标 pod 跑同款 footer/card Redis parser，看它读到的是 `group-at` 还是空。
- 同时用 `redis-cli GET group:mode:<chat_id>:<app_id>` 对比真实 Redis value。
- 如果 Redis value 中有中文 `context`，旧 footer parser 可能因字节长度/字符串长度不一致而超时读空，最终默认显示 `🔒主人@`。

**不改源码时的修复**:

- 将该 app 的 `group:mode:*:<app_id>` 中 `group-at` values 规范成 ASCII-only JSON，例如：

```json
{"mode":"group-at","context":"group-at runtime state for footer/gate parsers; ascii-only","set_by":"codex-upgrade-flow"}
```

- 重新运行 footer parser probe，期望看到 `group-at`，新卡片 footer 应为 `👥群@`。
- 旧飞书卡片不会 retroactively 改 footer；必须用新回复/新卡片验证。

### F. hung active reply 泄漏 → channel reload 永久 deferred

**症状**: her 正常收到消息（`received message from`）、WS Connected、feishu-ws-ready True，但完全不 dispatch，用户发消息无任何回应。pod 没有 restart，LiteLLM SpendLogs 只有 cron job 定时条目（ct=8 每 30min），无真实用户对话记录。

**诊断**:
```bash
# 关键信号:每 30s 打一次,有则即确认
kubectl logs -n carher carher-NNN-<hash> -c carher --tail=20 | grep "reload still deferred"
# 健康: 无输出
# 异常: "channel reload still deferred after 68000000ms with 21 reply(ies) active"
```

**根因**: OpenClaw 内部 N 个 reply 进入永久 hung active 状态（具体触发条件待 upstream 确认，疑与某次上游超时 / 大批量请求并发有关），channel reload 被这些未完成的 reply 永远 defer，新消息的 dispatch 队列无法推进。

**集群扫描**（并发检查所有 pod，2–3 分钟跑完）:
```bash
kubectl get pod -n carher --no-headers -o custom-columns=NAME:.metadata.name | grep "^carher-" | \
  xargs -P 30 -I{} sh -c '
    result=$(kubectl logs -n carher {} -c carher --tail=10 2>/dev/null | grep "reload still deferred")
    if [ -n "$result" ]; then echo "{}: $result" | head -1; fi
  '
# 无输出 = 全集群健康
# 有输出 = 逐个 rollout restart
```

**修复**: 重启 pod 清掉 hung reply 状态。delivery-queue 里的旧文件重启后会尝试 recovery，失败项（如 invalid group_id）不影响新消息。
```bash
kubectl rollout restart deployment/carher-NNN -n carher
kubectl rollout status deployment/carher-NNN -n carher
# 重启后验证: grep "reload still deferred" 消失 + "ws client ready" 出现
```

**注意**:
- 有多个持续 hung reply 但 LiteLLM 的 cron 仍工作（SpendLogs 每 30min 固定条目），不要被 LiteLLM 正常的假象迷惑
- 重启前先确认 delivery-queue 里没有今天的 pending 文件（May 13 之前的可以丢）

参考 case: 2026-05-15 her-191（赵凌云的her，cli_a952363106389cbb），21 reply hung 19 小时，rollout restart 恢复。集群扫描 223 个 pod 无其他命中。

### A. user-config alias 跟 LiteLLM key 白名单不一致

**症状**: 用户 `@某模型` (比如 `@opus4.7`) → bot 几秒回 "Something went wrong" → 重试还是错

**诊断**:
```bash
# 看具体 her 的 SpendLogs
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT to_char((sl.\"startTime\" AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Shanghai','HH24:MI:SS') AS bjt,
       sl.model AS upstream, EXTRACT(EPOCH FROM (sl.\"endTime\"-sl.\"startTime\"))::int AS dur,
       sl.completion_tokens AS ct
FROM \"LiteLLM_SpendLogs\" sl JOIN \"LiteLLM_VerificationToken\" vt ON sl.api_key=vt.token
WHERE vt.key_alias='carher-NNN' AND sl.\"startTime\" > NOW() - INTERVAL '30 min'
ORDER BY sl.\"startTime\" DESC LIMIT 10;"
# 标志: dur=0 + ct=0 + 频繁 → key 鉴权拒绝

# 验证 key 白名单 vs user-config alias
kubectl get cm carher-NNN-user-config -n carher -o jsonpath='{.data.openclaw\.json}' \
  | python3 -c "import json,sys;d=json.load(sys.stdin);
for k,v in d.get('agents',{}).get('defaults',{}).get('models',{}).items():
    print(k, '->', v.get('alias'))"
```

**根因**: 比如 user-config 配了 `litellm/anthropic.claude-opus-4-7 → @opus4.7`,但 carher-* key 白名单经过 Phase E 重构后只有 `claude-opus-4-7` (无前缀)。

**修复**: 改 user-config 把 alias key 跟 LiteLLM 白名单对齐 + restart pod。脚本模板参考 `/tmp/fix-opus47-alias.py` (2026-05-13 用过)。

**集群级 audit** (215 个 user-config 一致性检查):
```python
import json, subprocess
result = subprocess.run(['kubectl','get','cm','-n','carher','-o','json'], capture_output=True, text=True)
cms = [c for c in json.loads(result.stdout)['items']
       if c['metadata']['name'].startswith('carher-') and c['metadata']['name'].endswith('-user-config')]
inconsistent = []
for cm in cms:
    cfg = json.loads(cm['data'].get('openclaw.json','{}'))
    for k,v in cfg.get('agents',{}).get('defaults',{}).get('models',{}).items():
        # 检查 user-config 的 model 是否在 carher-* key 白名单约定里
        # carher 约定: 无前缀 (claude-opus-4-7), 不该用 anthropic. 前缀
        if k.startswith('litellm/anthropic.claude-'):
            inconsistent.append((cm['metadata']['name'], k, v.get('alias')))
print(f'inconsistent: {len(inconsistent)}')
```

### B. per-instance LITELLM_API_KEY env 漏覆盖

**症状**: her 启动后 100% 调 LiteLLM 都 401。错误日志里看到 `Received API Key = sk-carher-litellm-...e6f00707, Key Hash (Token)=... Unable to find token in cache or LiteLLM_VerificationTokenTable`。

**根因**: deployment env 没显式设 `LITELLM_API_KEY`,fallback 用了 `carher-env-keys` secret 里的**默认占位 token** `sk-carher-litellm-...`,但这个 token 不存在于 LiteLLM DB。

**确认**:
```bash
kubectl get deploy carher-NNN -n carher -o json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for e in d['spec']['template']['spec']['containers'][0].get('env',[]):
    if 'LITELLM' in e['name']:
        print(e['name'],'=',e.get('value','<from valueFrom>')[:25])"
# 健康: LITELLM_API_KEY=sk-XXX (per-instance plaintext key, 跟 carher-NNN key alias 对应)
# 异常: 没有这个 env entry,或值是 sk-carher-litellm-...e6f00707 (默认占位)
```

**修复**:
```bash
# 1. 创建/找到该 her 的 LiteLLM virtual key
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -sX POST "http://127.0.0.1:4000/key/generate" -H "Authorization: Bearer $MK" \
  -d '{"key_alias":"carher-NNN","user_id":"carher-NNN","models":[...]}'
# 抓 plaintext key 'sk-XXX'

# 2. patch deploy 加 env
kubectl patch deploy carher-NNN -n carher --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/env/-",
   "value":{"name":"LITELLM_API_KEY","value":"sk-XXX"}}]'
kubectl rollout status deploy/carher-NNN -n carher
```

参考 case: 2026-05-13 carher-1000 (国现的 her 阿里云) 就是这个根因 + 顺便从老模板切到 litellm-template。

### C. budget 超额 (设计意图,不修)

**症状**: 大消费 her 下午突然报 "Something went wrong",日志里 `400 Budget has been exceeded! Current cost: 721.5311, Max budget: 100.0`

**根因**: 每个 carher-* key `max_budget = $100/day`,`budget_duration = "1d"`,跨过 1 天才 reset。her 主人疯狂用导致用完。

**这不是 bug 是设计**。不要随便涨上限。

**用户应对**:
- 让用户等到第二天 (UTC 0:00 reset)
- 或用户切到不需 LiteLLM 的工具 (像 lark-cli 直调)
- 或主人主动跟 ops 确认是否真要涨上限 (如果是 IT/管理员等真高负荷岗位)

参考 case: 2026-05-13 carher-35 (用户报 "Something went wrong" 但实际是 budget exceeded)

### D. /new /reset slash 命令不回复

详见独立 skill: `carher-slash-command-noreply-debug`

这是 K8s carher 集群已知 bug,根因还在排查 (feishu-her plugin / openclaw-lark plugin / image patches 都验证过不是)。**workaround 是删 active session jsonl + restart pod**。

### E. reindex 死循环导致超时 / OOM

详见独立 skill:
- `her-memory-reindex-rescue` (单实例)
- `carher-memory-orphan-tmp-cluster-cleanup` (集群级)

**症状**: bot 启动后正常 5-10 分钟 → 突然慢 30s+/timeout → 偶尔 OOMKilled

**确认**:
```bash
POD=carher-NNN-...
kubectl exec $POD -n carher -c carher -- ls /data/.openclaw/memory/
# 如果有 main.sqlite.tmp-* 文件 → reindex 死循环
```

**根因**: bge-m3 上游 (OpenRouter) 抖动 → embedding API 超时 → memory module 反复 spawn 新 tmp。

**治标**: 删孤儿 tmp + 重启
**治本**: 已上线 bge-m3 → wangsu-text-embedding-v3 fallback (2026-05-13)。OpenRouter 故障时 LiteLLM 自动切。详见 `carher-bge-m3-embedding-fallback` skill。

## 全集群 audit 模板 (一键扫所有问题)

```bash
# 0. hung reply deferred 检测 (Python 并发版,避免 xargs 命令行过长)
python3 - <<'PYEOF'
import subprocess, concurrent.futures, re
r = subprocess.run(['kubectl','get','pod','-n','carher','--no-headers',
                    '-o','custom-columns=NAME:.metadata.name'], capture_output=True, text=True)
pat = re.compile(r'^carher-\d+-')  # 排除 carher-admin / carher-operator
pods = [l.strip() for l in r.stdout.split('\n') if pat.match(l.strip())]
print(f"扫描 {len(pods)} 个 her pod...")

def check(pod):
    try:
        r = subprocess.run(['kubectl','logs','-n','carher',pod,'-c','carher','--tail=15'],
                           capture_output=True, text=True, timeout=20)
        for line in r.stdout.split('\n'):
            if 'reload still deferred' in line:
                return (pod, line.strip()[:120])
    except: pass
    return None

with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
    hits = [r for r in ex.map(check, pods) if r]
print(f"hung 命中 {len(hits)}")
for pod, msg in hits: print(f"  {pod}: {msg}")
PYEOF

# ⚠️ 不要用 `xargs -P 30 -I{}`: 224+ pod 名拼起来超过 ARG_MAX
# (`xargs: command line cannot be assembled, too long`),会一条都不跑

# 1. 当前哪些 her 在频繁报 LiteLLM 错误（真实 chat 失败,排除 embedding 误报）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT vt.key_alias, count(*) AS errs,
       max(sl.\"startTime\")::time(0) AS last,
       max(sl.metadata->'error_information'->>'error_class') AS err_class
FROM \"LiteLLM_SpendLogs\" sl JOIN \"LiteLLM_VerificationToken\" vt ON sl.api_key=vt.token
WHERE sl.\"startTime\" > NOW() - INTERVAL '6 hours'
  AND vt.key_alias LIKE 'carher-%'
  AND sl.completion_tokens = 0
  AND sl.model NOT LIKE '%bge-m3%'
  AND sl.model NOT LIKE '%embedding%'
  AND CAST(sl.response AS TEXT) NOT LIKE '%\"embedding\"%'
GROUP BY vt.key_alias HAVING count(*) > 3 ORDER BY errs DESC LIMIT 20;"

# ⚠️ 关键过滤：response NOT LIKE '%embedding%' 不能省
# `openrouter/BAAI/bge-m3` / `wangsu-text-embedding-v3` 的 ct=0 是正常的,model 名 NOT LIKE
# '%embedding%' 抓不住 bge-m3（命名不匹配），response 体里有 "embedding": [...] 才是真信号

# 1b. 快速查 budget 超额（不用绕 SpendLogs）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT key_alias, spend::numeric(10,2), max_budget,
       ROUND((spend/max_budget*100)::numeric,1) AS pct, budget_reset_at
FROM \"LiteLLM_VerificationToken\"
WHERE key_alias LIKE 'carher-%' AND max_budget IS NOT NULL
  AND spend / max_budget > 0.8
ORDER BY pct DESC LIMIT 15;"
# pct ≥ 100 = 当前正在 BudgetExceededError,等下次 budget_reset_at 自动恢复

# 2. 集群孤儿 tmp 文件 (reindex 死循环检测)
scripts/jms ssh k8s-work-227 "find /Data -maxdepth 4 -path '*/memory/main.sqlite.tmp-*' -type f -printf '%TY-%Tm-%Td %s %p\n' 2>/dev/null | sort"

# 3. her pod 状态 (last restart, OOMKilled 等)
kubectl get pod -n carher -l 'app.kubernetes.io/component=carher-her' \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[*].restartCount,REASON:.status.containerStatuses[*].lastState.terminated.reason | head -30
```

### SpendLogs 查"不回复"的两个隐性陷阱

**陷阱 1：bge-m3 误报**
- `sl.model NOT LIKE '%embedding%'` 看似合理，但 `openrouter/BAAI/bge-m3` 不含 "embedding" → 漏过滤
- bge-m3 调用的 `completion_tokens = 0` 是 embedding 的正常值（不是失败信号）
- **必须加** `CAST(sl.response AS TEXT) NOT LIKE '%"embedding"%'` 才能干净

**陷阱 2：失败条目的 `call_type` 是 NULL**
- 真实失败（鉴权拒、budget 拒、模型不在白名单）走不到 dispatch 分类阶段
- 用 `call_type IN ('completion','acompletion')` 过滤会把这些失败全过滤掉 → 看似"没有失败"
- 正确做法：不过滤 call_type，用 `completion_tokens = 0` + response 内容辨别

**结构化 error 提取**：`metadata->'error_information'->>'error_message'` / `->>'error_class'` 比从 200KB JSON 字符串 substring 高效得多。常见 `error_class`：
- `BudgetExceededError` → 根因 C
- `AuthenticationError` → 根因 A/B
- `MidStreamFallbackError` → 上游 timeout（瞬态）
- `RateLimitError` → 限流

## 已知根因 vs 处置 quick reference

| 用户症状 | 检查点 | 根因 | 处置 |
|---|---|---|---|
| her 正常收消息 + 不 dispatch + WS Connected + log 有 "reload still deferred" | grep "reload still deferred" | F hung reply 泄漏 | rollout restart |
| 任何 her 报错 + dur=0 + ct=0 | LiteLLM SpendLogs | A user-config alias mismatch / B key env 漏配 / C budget 超 | 看 error message 细分 |
| 单一 her 启动后 100% 报错 | her pod env LITELLM_API_KEY | B per-instance key 漏 | patch deploy 加 env |
| 单一 her 大消费 + 下午突然不行 | error 含 'Budget has been exceeded' | C budget 设计 | 等明天 / 或确认涨 |
| @某新 model 第一次就报错 | user-config alias key 形式 | A 命名跟 LiteLLM 不一致 | 改 user-config + restart |
| /new /reset 没回复 | feishu log 'system command dispatched (delivered=false)' | D OpenClaw upstream bug | workaround: 删 active session jsonl |
| 多个 her 慢 + NAS 容量警告 | 集群孤儿 tmp 扫描 | E reindex 死循环 | 走 cluster cleanup runbook |
| **文案"model idle timeout / increase models.providers.timeoutSeconds"** | **先看 LiteLLM proxy access log（podIP）确证请求是否进 proxy**，再看 SpendLogs / DB 体积 | **G prework cold scan stall（cache bloat）或 H stream consumer hang（trajectory accumulation, 假说）** | **跳 her-memory-reindex-rescue 判断 8/9**（先 access log 区分 B/C，再决定 GC 还是 archive session） |
| `/hermes` 或 `/openclaw` 能触发但 1-7 分钟才可用 | 比较 S3 `hermestest-75` 与 ACK 的 image digest、entrypoint hash、env、mount、启动日志 | J H75 dual-engine switch slow：重复静态同步 + ACK 部署缓存/profile 差异 | 先做 deployment-layer prewarm/fast-cache；源码未授权时不要改 runtime |
| H75/Dify workflow 工具报 403 / health/run 失败 | `her-workflow-dify-creator health`，再测 internal `dify-nginx` workflow API 和 `dify-bootstrap` lifecycle | K Dify runtime URL 混用：公网 Cloudflare URL 被写进 bot 控制面或 workflow API | 备份并修 `workflow/dify-config.json.dify_base_url` 与 `lifecycle_base_url`，同时固化 Deployment env/profile |
| H75/Dify `run` 单次 500，但 internal URL 正确 | `kubectl logs -n dify deploy/dify-api --since=5m` | Dify API/DB transient，例如更新 `api_tokens.last_used_at` 时 Postgres 连接被关闭 | 先重试一次；若重试和 health 通过，不改 Her。若持续 500，再修 Dify API/DB |

## G. prework cold scan stall（embedding_cache 过大）

**症状**: 用户报"超时 / 半天不回复 / 看到 'model idle timeout' 提示"。`embedded run failover decision: ... reason=timeout from=litellm/... next=none`。客户端等够 turn timeout（120-130s）后吐 surface_error。pod **不会 restart**（不是 OOM），但回复体感差到不可用。

> ⚠️ **2026-05-16 复现教训**：carher-30 上午做完 GC + VACUUM 后同日 20:33 还是 timeout，复盘发现是 **SpendLogs 0 条但 access log 有 200 OK 3s** —— 请求其实发了，是 carher 端 stream consumer 卡死（trajectory 5.2 MB 累积）。所以根因 G 必须先用 access log 反证，再决定走 G（cache bloat）还是 H（trajectory 假说，见下）。

**诊断快查**:

1. 是不是 Codex / 客户端的问题？**不是**。"models.providers.<id>.timeoutSeconds" 文案是 OpenClaw 借鉴 Codex 的字段命名做 user-facing 兜底，与 Codex 客户端配置无关
2. **请求是否真的进了 LiteLLM proxy**？用 podIP 在 access log 反证（SpendLogs 不可靠）：
   ```bash
   POD=$(kubectl -n carher get pod --no-headers | grep "^carher-${HID}-" | head -1 | awk '{print $1}')
   POD_IP=$(kubectl -n carher get pod "$POD" -o jsonpath='{.status.podIP}')
   kubectl -n carher logs -l app=litellm-proxy --since=2h --tail=-1 \
     | grep "$POD_IP" | grep -E "POST /(v1/)?chat/completions"
   ```
   - access log **0 条** → 请求**真没发出去**，进 3（量 DB 体积，判断 G）
   - access log **有 200 OK 1-5s** → 请求发了 LLM 也正常返回，**跳根因 H**（trajectory accumulation，stream consumer hang）
   - access log 有但耗时 ≥ 60s → 是 LLM 真慢，走 (7) 上游故障
3. main.sqlite 是否过大？
   ```bash
   kubectl -n carher exec "$POD" -c carher -- ls -lh /data/.openclaw/memory/main.sqlite
   ```
   - ≥ 500 MB → 高度可疑，跳 `her-memory-reindex-rescue` 判断 8 做完整确证
   - ≥ 1 GB → 直接进 Phase G 修复
   - < 500 MB → 跳根因 H

**根因**: PVC = NFS NAS（`alibabacloud-cnfs-nas`）→ sqlite 每页 4KB 读都是 NFS RPC round trip。`embedding_cache` 表（content-hash → embedding 缓存）随用户活跃天数线性增长，到 30000+ 行 / 600+ MB 时全表 cold scan 需要 30-40s + vec0 KNN cold 需要 10s + node:sqlite 同步调用 block event loop → 单 turn prework 累积 30-130s 后客户端 turn timeout 触发。

**修复**: 跳 `her-memory-reindex-rescue` skill 的 **Phase G**（GC embedding_cache + VACUUM 主库）。流程概览：

```
VACUUM INTO 备份 → 校验 integrity_check → DELETE WHERE updated_at < 7-30 天前 → VACUUM → 验证 cold scan 速度
```

**安全保证**: embedding_cache 是性能缓存，不是记忆本体。记忆本体在 `chunks` / `chunks_vec` / `chunks_fts*` 表，完全独立。删 cache 后果只是下次相同 hash 的文本要重新调一次 LiteLLM `/embeddings`（~1-3s + < $0.0001）。**不会丢失任何记忆。**

**参考案例**: carher-30（王丽花，2026-05-16 早上），main.sqlite 1.1 GB / embedding_cache 33787 行 / 698 MB，timeout 130s × 3 次 / 2h；VACUUM INTO 备份 903 MB / 83s。但**当晚 20:33 同样症状复现** → 该案例 G 治标没治本，真因可能落在 H 上。完整证据链见 `her-memory-reindex-rescue` skill 的"判断 9 / 模式 C"章节。

## H. stream consumer hang（session trajectory accumulation，**假说，2026-05-16 引入，待验证**）

**症状**: 跟 G 一模一样的"model idle timeout"用户文案，但 access log 看得到 200 OK 1-5s（LiteLLM 正常返回了），偏偏 carher 这边到 turn timeout 才报错。

**触发条件**:
- access log 有 200 OK 短 TTFB（不是 LiteLLM 慢）
- DB 体积 < 500 MB **或** 已经做过 G 但 timeout 复现
- 该 user 最近切过 model 或单 session trajectory.jsonl 体积异常大（5+ MB）

**假说**: carher 把上游 SSE chunk 喂进 `processOpenAICompletionsStream` / `sanitizeOpenAISdkSseResponse` / `buildGuardedModelFetch` 几层包装；遇到 trajectory 累积大 + 单 turn `context.compiled` 接近 trajectory-event-size-limit=262144 截断阈值时，迭代器在某种 chunk 模式下卡住，OpenClaw `streamWithIdleTimeout(120s)` 到点 surface_error。

**临时修复（最小侵入，零数据损失）**: archive 老 session + pod 重启起新 session。完整命令见 `her-memory-reindex-rescue` 判断 9。

**验证状态**: carher-30 fix 已部署 2026-05-16 22:00 前后，等用户下次自然使用确认。

## I. S3 实例 upstream SSE stream 卡死 + 无 failover profile（2026-05-17 carher-14 案例）

**症状**: S3 (内网 Docker, JSZX-AI-03) 上 hermestest-N 容器健康跑，但用户某次 turn **600s 干等**——日志反复打 `[diagnostic] stalled session ... activeWorkKind=model_call recovery=none age=137s → 257s → 587s`，最终 `embedded run failover decision: ... decision=surface_error reason=timeout from=anthropic/anthropic.claude-opus-4-7 profile=-`。

> **`profile=-` 不是 provider 名字** —— 是 failover profile 字段为空的占位符。**provider 真实身份在 env `ANTHROPIC_BASE_URL`**。carher-14 实测：`ANTHROPIC_BASE_URL=https://cc.auto-link.com.cn/pro` → 实际走 198 Pro LiteLLM（ChatGPT Pro 订阅账号代答）。从 `from=anthropic/...` 字段直接归因到"Anthropic / openrouter / wangsu"是诊断陷阱。

**根因证据链**（用来与 G / H 区分）：

| 假设 | 证伪条件 | 实际数据（carher-14） | 结论 |
|---|---|---|---|
| carher 内部 sqlite cold scan 卡 | stalled 全程会持续刷 `event_loop_delay` 警告 | **整 10 分钟只有 1 次** `event_loop_delay max=6492ms`（dispatch 后 23s 的 prework）；之后**无 block 警告** → 主线程 idle 在 socket 上等 | ❌ 排除 G |
| stream consumer hang（H） | 至少能看到部分 SSE chunk 写进 trajectory | `dispatch complete replies=0` 一个 chunk 都没收到；trajectory 也只有 user 消息 | ❌ 排除 H |
| openrouter / wangsu / etc 上游慢 | response 至少有进展但很慢 | 整 10 分钟无 fetch error / abort / retry / chunk；timeout 后才 surface_error | 命中：上游 SSE **从未返回任何 chunk**，TCP 不断也不超时 |
| 同实例独占 | 同机其他 hermestest-N 同时段 OK | hermestest-75 同窗口 08:42-08:44 也 stalled 163s | 共同因素，上游链路抖 |

**为什么 600s 才超时**：S3 实例 `docker.json5` 配 `agents.defaults.timeoutSeconds: 600`（K8s 默认 120s），加上 `thinkingDefault: "xhigh"` + opus-4-7 + ChatGPT Pro 链路 long-tail，体感比 K8s 差 5×。

**为什么没自动重试**：`grep failover|profile docker.json5 base.json5` 完全空 → **S3 实例没配 `failoverProfiles`**。OpenClaw 日志 `profile=-` 翻译过来就是"我想 failover 但没配 chain，只能 surface_error"。对比 K8s 集群走 LiteLLM proxy，proxy 层有 `fallbacks:` model 链兜底。

**修复路径**（按 cost 升序）：

| 方案 | 改动 | 副作用 | 体感降幅 |
|---|---|---|---|
| 短期 A. 等 600s timeout 自然抛 | 0 | 用户等 10 min | 0 |
| 短期 B. docker restart hermestest-N | 30-60s 下线 | 当前 turn 丢，新 turn 重试 | 间歇有效 |
| 中期 C. base.json5 加 `failoverProfile` chain（primary opus@120s → openrouter or-opus@120s → sonnet@60s）+ ConfigMap 同步 | 改 1 处配置 + restart 容器 | 600s → 60-180s | 大 |
| 长期 D. S3 实例改走 198 LiteLLM proxy（跟 K8s 一致），用 proxy 层 fallbacks: + chatgpt-pro 多账号扩容 | env 改 `ANTHROPIC_BASE_URL` → proxy；proxy 配 fallback | 跟 K8s 行为统一 | 最大 |

**与 G / H 的快速鉴别**：

| 信号 | G | H | I |
|---|---|---|---|
| 部署 | K8s | K8s | S3 Docker |
| LiteLLM access log（podIP / 同链路）有 200 OK | ❌ 0 条 | ✅ 200 OK 1-5s | ❌ 0 条（链路上游卡死） |
| `event_loop_delay` 警告 | 持续刷 | 持续刷 | **只 1 次**（prework）|
| main.sqlite 体积 | ≥ 500 MB | < 500 MB | 不相关 |
| turn timeout | 120-130s | 120-130s | **600s** |
| 修复 | GC + VACUUM | archive trajectory + 重启 | failoverProfile / 接 LiteLLM proxy |

## J. H75 dual-engine switch 慢：S3 快、ACK 慢（2026-06-01 carher-1000 / hermestest-75）

**症状**:
- `/hermes` 或 `/openclaw` 命令能被拦截，卡片也能发，但用户体感“切一次很久”。
- `/data/.engine/active` 已变化，但目标引擎几分钟后才真正回复。
- Kubernetes Pod 可能已经 `2/2 Running`，但目标 gateway/Feishu WS 还没 ready。

**正确计时点**:
- 开始：Feishu 日志 `command intercepted`，或发送 slash command 的时间。
- 退出：`process.exit(0)` / `process exit -> <engine>`。
- Hermes 可用：`connected to wss://msg-frontier.feishu.cn`。
- OpenClaw 可用：`http server listening` + `gateway ready` + `[ws] ws client ready`。
- 不要用 `/data/.engine/active` 或 K8s Ready 作为最终可用时间。

**S3/hermestest-75 参考基线**:
- `/data` 和 `/opt/data` 是本地 ext4。
- 镜像 entrypoint hash 与 ACK 一致，但 image digest、env、mount/profile 仍要逐项对齐。
- OpenClaw -> Hermes：script start -> Feishu WS connected 约 45s；process start -> Feishu WS connected 约 39s。
- Hermes -> OpenClaw：script start -> OpenClaw WS ready 约 30s；OpenClaw gateway init 约 18s。

**ACK/H75 根因链（最新结论）**:
1. `/entrypoint.sh` hash 一致不等于部署行为一致；ACK 与 S3 的 image digest、env、mount 和缓存介质都要对比。
2. ACK 旧路径把 `/data/.openclaw`、`/opt/data` 等状态放到 NAS/NFS 或每次重建的冷 `emptyDir`，重复小文件操作会被放大。
3. 每次切换重复做静态工作：ACP bootstrap、official lark skill sync、shared skill sync、runtime plugin refresh、Hermes patches、OpenClaw/Hermes gateway init。
4. `carher-1000` 旧数据：OpenClaw -> Hermes 约 148s，Hermes -> OpenClaw 约 143s。
5. 部署层 H75 fast-cache/prewarm 后，新 Hermes Pod 从 first log 到 Feishu WS connected 约 31s；这已经进入用户接受的 40s 档。

**部署层可修内容**:
- 给 H75 runtime profile 加 `emptyDir` 本地缓存和 initContainer prewarm：ACP adapters/wrappers、Claude state、lark-cli skills、shared skills、OpenClaw lark extension、runtime plugins。
- 在 main container 里禁掉重复静态工作：`CARHER_RUNTIME_PLUGINS_REFRESH=0`、`CARHER_LARK_CLI_SKILLS_BUNDLE_DIR=/opt/carher-runtime/empty/lark-cli-skills`、`CARHER_SHARED_SKILLS_BUNDLE_DIR=/opt/carher-runtime/empty/shared-skills`。
- 只有在 ACP wrappers/state 已经 prewarm 且 `/data/.claude`、`/data/.acpx` 已挂本地状态时，才允许 `CARHER_ACP_ENABLED=0`。
- 保留 `/carher-fastbin` wrapper 用来处理热路径文件操作；确认 `PATH` 用 `sh -c 'command -v cp'` 或 `/proc/1/environ`，不要用会重置 PATH 的 login shell。
- 直接 patch Deployment 可以应急，但长期应固化为 operator/runtime-profile 规则；不要让手工 patch 成为唯一事实来源。

**验收标准**:
- 两个方向都要测：`/hermes` 和 `/openclaw`。
- 计时从 command intercepted 到目标引擎真实 ready，不从 K8s `Ready` 或 active marker 算。
- 40s 是用户接受线；S3 parity 容忍到约 45s。
- 同一聊天里必须再做一条真实消息 smoke；不能只看日志。
- 如果最后一轮只测了 Hermes 启动路径，要明确写“反向 `/openclaw` 尚未复测”，不要把单向数据包装成全面通过。

**排查命令**:

```bash
POD=$(kubectl get pods -n carher -l user-id=$HER_ID -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n carher "$POD" -c carher -- sh -c '
  cat /data/.engine/active
  env | grep -E "CARHER_(RUNTIME_PLUGINS_REFRESH|LARK_CLI_SKILLS_BUNDLE_DIR|SHARED_SKILLS_BUNDLE_DIR|ACP_ENABLED)=" | sort
  mount | grep -E "(/data/.openclaw|/opt/data|/carher-fastbin)" | sed -E "s#(//[^/ ]+)#[redacted-host]#g"
  printf "PATH=%s\n" "$PATH"
  command -v cp; command -v rm; command -v chown; command -v chmod || true
'
kubectl logs -n carher "$POD" -c carher --since=10m --timestamps \
  | rg 'command intercepted|process.exit|seeding baked ACP|ACP toolchain ready|synced official lark-cli skills|syncing baked shared skills|reconciling image-managed|ran [0-9]+ hermes patch|ran [0-9]+ runtime glue|exec hermes|exec openclaw|connected to wss|ws client ready|File exists|Device or resource busy'
```

**Known pitfalls from the fix**:
- Mounting `emptyDir` at `/data/.openclaw/runtime-plugins` makes `rm -rf /data/.openclaw/runtime-plugins` fail with `Device or resource busy`; delete contents, not the mountpoint.
- `/bin/sh` 不支持 `trap ERR`；init script 不要照 bash 写。
- `set -e` 下不要写 `[ "$base" = "." ] || [ "$base" = ".." ] && continue`，会让 init 直接退出；用 `case "$base" in .|..) continue;; esac`。
- Do not blindly skip `rm -rf /opt/data/.hermes/skills/dogfood/lark-*`; those are symlinks created each boot. Skipping removal caused `ln: File exists` and container restarts.
- A stale CRD `status.message=CrashLoopBackOff` is not decisive; use current pod state, restart count, gateway logs, and Feishu WS.

## K. H75/Dify runtime URL 走公网导致 workflow helper 403（2026-06-01 carher-1000 / hermestest-14）

**症状**:
- `CARHER_DIFY_ENABLED=1`，`dify-bootstrap-init` 和 `her-workflow-dify-*` 都存在。
- `https://dify-k8s.carher.net/healthz` 和 in-cluster `dify-bootstrap` health 都正常。
- `her-workflow-dify-creator health` 失败，直接访问 generated lifecycle 返回 Cloudflare `403` / `1010`，或 `publish` / `new-key` 成功但 `run` 返回 Cloudflare `403` / `1010`。

**S3 参照**:
- `hermestest-14` 的 Dify API 走 S2 内网 `http://10.68.13.187:5680`，bot lifecycle 走 S2 内网 `http://10.68.13.187:5688/v1/lifecycle/carher-14`，health 返回 200。
- ACK Her pod 的 `dify_base_url` 是 workflow API 调用地址，必须走 internal `dify-nginx`；`lifecycle_base_url` 是 bot 控制面地址，必须走 internal `dify-bootstrap`。

**ACK 正确形态**:
- `CARHER_DIFY_BASE_URL=http://dify-nginx.dify.svc.cluster.local`
- `CARHER_DIFY_BOOTSTRAP_URL=http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot`
- `workflow/dify-config.json.lifecycle_base_url=http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/carher-<ID>`

**快速确证**:

```bash
POD=$(kubectl get pods -n carher -l user-id=$HER_ID -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n carher "$POD" -c carher -- sh -lc '
  /data/.openclaw/local/bin/her-workflow-dify-creator config
  /data/.openclaw/local/bin/her-workflow-dify-creator health
'
```

If generated config points at public `https://dify-k8s.carher.net/v1/lifecycle/<bot>` and internal `http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/<bot>/health` works with the same token, treat it as deployment/profile config drift. Back up `workflow/dify-config.json`, rewrite `lifecycle_base_url` to the internal URL, rerun `her-workflow-dify-creator health`, then fix the profile/bootstrap generation path before expanding rollout.

If `dify_setup_status=403` or workflow `run` returns Cloudflare `403` / `1010`, patch `dify_base_url` too: set live `workflow/dify-config.json.dify_base_url` and Deployment env `CARHER_DIFY_BASE_URL=http://dify-nginx.dify.svc.cluster.local`, roll the target, then require health `200/200` and a successful `her-workflow-dify-creator run`.

Dify Creator import trap: ACK/S3 Dify currently treats DSL `version: 0.3.1` as `202 pending + app_id=null`; this is a confirmation flow, not a worker queue. Call `POST /apps/imports/<import_id>/confirm` within 10 minutes, or use `version: 0.3.0` for immediate `200 completed + app_id`. `GET /apps/imports/<id>` returning `404` is expected here.

Dify API transient trap: if `run` returns HTTP 500 but config still uses internal `dify-nginx` and lifecycle health is OK, inspect `dify-api` logs before touching the Her. A `psycopg2.OperationalError: server closed the connection unexpectedly` during `api_tokens.last_used_at` is usually a stale DB connection; retry the run once, then repair Dify API/DB only if it persists.

## 已知调查时间线 (2026-05-12 / 13 / 15 / 16)

- 2026-05-12 全集群孤儿扫描 → 29 her / 49.6GB → 全清 + 215 重启
- 2026-05-12 carher-1000 (国现的) "Something went wrong" → 是 B (per-instance LITELLM_API_KEY 漏配,从老模板切 litellm-template + 创建 key)
- 2026-05-13 早 carher-35 报错 → 是 C (budget exceeded $721/$100,设计意图不修)
- 2026-05-13 中 carher-39/63/67/73/177 等报错 → 是 A (215/216 user-config opus4.7 alias 都配错,只有 carher-87 对) → 改 215 个 user-config + restart
- 2026-05-13 中 上线 bge-m3 fallback (E 治本)
- 2026-05-13 晚 复扫孤儿: 0 (fallback 见效)
- 2026-05-15 her-191 (赵凌云) "不回复" → 是 F (hung 21 reply 19h,WS Connected 正常) → rollout restart 恢复；集群 223 pod 扫描无其他命中
- 2026-05-16 早 carher-30 (王丽花) "model idle timeout" 偶发 (3 次/2h, 130s 后才报错) → 初判 G (main.sqlite 1.1 GB / embedding_cache 33787 行 698 MB, NFS cold scan 38s + vec0 KNN cold 10s, node:sqlite 同步 block event loop)，做 GC + VACUUM 1.1 GB → 208 MB
- **2026-05-16 20:33 同样 timeout 复现** → G 治标没治本。复盘发现该窗口 LiteLLM proxy access log 有 200 OK 3s TTFB → 请求其实进了 proxy，是 carher 端 stream consumer 卡死。**修正认知：诊断必须先用 podIP 反证 access log，不能只看 SpendLogs**。补做 H 临时修复（archive 5.2MB trajectory + 删 pod），fix 待王丽花下次自然使用验证

## 相关 skills

- `carher-bge-m3-embedding-fallback` — E 治本方案的实施记录
- `carher-memory-orphan-tmp-cluster-cleanup` — E 治标方案 (集群级清理)
- `her-memory-reindex-rescue` — E 单实例诊断 (模式 A) + **G prework cold scan stall 修复 (模式 B Phase G)**
- `carher-slash-command-noreply-debug` — D 已知 bug
- `litellm-budget-mgmt` — C budget 配置
- `litellm-key-mapping` — B 验证 key 是否在 LiteLLM DB
- `check-instance-status` — 单实例健康检查
