---
name: carher-instance-config-override
description: >-
  针对 carher her 实例做单实例配置覆盖 / 灰度验证 / 批量推进 / 清理收尾的完整
  workflow。Use when the user wants to override a single field per-instance
  (e.g. memorySearch baseUrl, plugin config, model alias), roll it out to
  all ~200 instances gradually, or clean up overrides back to base-config
  only. Also covers "${ENV}" placeholder gotchas and hot-reload vs
  pod-restart-scope distinctions. **切 her 默认模型 / 换 primary model
  ("把 X 台切成 deepseek"、"默认模型改成和 Y 一样") 也走这里**——有专用脚本
  scripts/carher_her_model_switch.py，带 preflight 三道闸门和路径级 diff guard。
  **给 her 加 fallback / 兜底模型（"给每个模型加个 fallback 为 grok"、"挂了自动
  切别的模型"）同样走这里**——脚本 scripts/carher_her_model_fallback_add.py，
  且必读「openclaw 只有一条链、用户 /model 手选的模型 strict 永不兜底」这段，
  否则会承诺一件做不到的事。
---

# Carher 实例配置覆盖 & 批量灰度

## 三层配置合并关系

```
carher-base-config (ConfigMap, 全局)
  ├── shared-config.json5        ← memorySearch / tools / plugins 等默认值
  └── carher-config.json         ← $include shared-config.json5
            ↑ 引用
carher-<N>-user-config (ConfigMap, per-instance, 由 operator 生成)
  └── openclaw.json              ← $include carher-config.json
                                 ← 上面加 per-instance override（agents/models/channels 等）
```

**深合并语义**：user-config 的某字段覆盖 base-config 同路径的字段；其它字段继承。例如：

```json
{"$include": "./carher-config.json",
 "agents": {"defaults": {
   "memorySearch": {"remote": {"baseUrl": "http://litellm-proxy...", "apiKey": "sk-..."}}
 }}}
```

只覆盖 `memorySearch.remote.{baseUrl,apiKey}`；`memorySearch.model / sources / query / experimental` 继续从 base-config 继承。

## 两种生效方式（关键差异）

| 字段位置 | 挂载方式 | 变更生效 |
|---|---|---|
| **user-config**（`carher-<N>-user-config`）| operator 通过 init-container + sidecar `config-reloader` 把 ConfigMap → emptyDir `/data/.openclaw/openclaw.json` | ✅ **热 reload**，~60-120s 自动生效，**不用重启 pod** |
| **base-config**（`carher-base-config`）| 直接 subPath 挂载到 pod 里的 `shared-config.json5` / `carher-config.json` | ❌ **subPath 限制**：改了 ConfigMap pod 看不到新内容，**必须 rollout restart 才生效** |

详见 [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md) 里的 subPath 陷阱详解。

## `${ENV_VAR}` 占位符的坑

- **Base-config 里**：bot 会在运行时把 `"apiKey": "${LITELLM_API_KEY}"` 替换成 pod env 里 `LITELLM_API_KEY` 的实际值
- **User-config 里**：bot **不做** env 替换，`${LITELLM_API_KEY}` 会**原样保留** → 调用上游时报 401
- **结论**：user-config override 必须写**字面 key**（从 `spec.litellmKey` 取），不能用 env 占位

🔴 **上面三条只对「K8s 的 carher-<N>-user-config」这个形状成立，别外推。**
188 上的 docker 实例（`hermestest-*`，openclaw 2026.6.10，overlay 是
`/Data/carher-runtime/deploy/carher-<N>/openclaw.runtime.json5`）**语义相反**：
`resolveConfigEnvVars` 对整个已解析 config 递归插值，`${VAR}` 放任意路径都生效，
未设值抛 `MissingEnvVarError` 而不是静默空串。见
[hermestest-web-search-provider](../hermestest-web-search-provider/SKILL.md) §2。

**判别器（别猜，2 秒跑完）**——写一个 `${某个已设 env}` 进去，然后实打一次：

```bash
docker exec -e HOME=/data <容器> openclaw infer web search --query "test"
# 200 + 正文非空 ⇒ 插值了；401 ⇒ 原样透传了字面 ${...}
```

⚠️ 不能用 `openclaw config get <路径>` 代替：它把 secret 打成
`__OPENCLAW_REDACTED__`，插没插值看不出来。

```bash
# 取某实例的 litellmKey
KEY=$(kubectl get her her-${ID} -n carher -o jsonpath='{.spec.litellmKey}')
```

## 灰度三段式

### Phase 0：预验证（纯读，0 影响）

```bash
# 1. 确认目标 key env 被 operator 注入到 pod
kubectl exec <some-pod> -n carher -c carher -- env | grep LITELLM_API_KEY
# 2. 挑一个样本实例，用其 key 直打目标 upstream 确认 200
KEY=$(kubectl get her her-10001 -n carher -o jsonpath='{.spec.litellmKey}')
kubectl run ck --image=curlimages/curl:latest --restart=Never -n carher --quiet --rm -i --command -- \
  curl -sS -w "\n%{http_code}\n" -X POST <url> \
  -H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" -d '<payload>'
# 3. 评估 upstream 容量：预估全量流量峰值，LiteLLM/依赖服务资源是否扛得住
kubectl top pod -n carher -l app=litellm-proxy
```

### Phase 1：单实例灰度（建议挑自己常用的，如 carher-1000）

```bash
# 备份当前 user-config
kubectl get cm carher-1000-user-config -n carher -o yaml > /tmp/carher-1000-user-config.bak.yaml

# 读当前 openclaw.json
kubectl get cm carher-1000-user-config -n carher -o jsonpath='{.data.openclaw\.json}' > /tmp/c.json

# 用 python 注入 override
# 注意 heredoc 用 'PY'（带单引号）避免 bash 展开 $ 变量
python3 <<'PY'
import json
cfg = json.load(open('/tmp/c.json'))
cfg.setdefault('agents', {}).setdefault('defaults', {})['<FIELD>'] = { ... }  # ← 替换 <FIELD>
json.dump(cfg, open('/tmp/c-new.json','w'), indent=2, ensure_ascii=False)
PY

# apply
kubectl create cm carher-1000-user-config -n carher \
  --from-file=openclaw.json=/tmp/c-new.json \
  --dry-run=client -o yaml | kubectl apply -f -

# 等 sidecar reload（kubelet 同步 + reloader 5s 轮询，总计 ~60-120s）
sleep 120

# 验证 pod 里真的生效了
POD=$(kubectl get pod -n carher --no-headers | grep "^carher-1000-" | awk '{print $1}' | head -1)
kubectl exec $POD -n carher -c carher -- python3 -c "
import json; d=json.load(open('/data/.openclaw/openclaw.json'))
print(d.get('agents',{}).get('defaults',{}).get('<FIELD>'))
"
```

### Phase 2-3：批量推进（灵活节奏）

**一次性拉数据（快）+ 并行 apply（快）**，避免 per-instance kubectl get 慢：

```bash
mkdir -p /tmp/rollout && cd /tmp/rollout
kubectl get her -n carher -o json > all_hers.json
kubectl get cm -n carher -o json > all_cms.json

python3 <<'PY'
import json
hers = json.load(open('all_hers.json'))['items']
cms  = json.load(open('all_cms.json'))['items']
id_to_key = {str(h['spec'].get('userId')): h['spec'].get('litellmKey','')
             for h in hers if h['spec'].get('litellmKey')}
cm_map = {cm['metadata']['name']: cm['data'].get('openclaw.json','')
          for cm in cms if cm['metadata']['name'].endswith('-user-config')}

# 选目标 ID 集合：奇数、特定模型、特定 group、或全量
target_ids = sorted([uid for uid in id_to_key if <CONDITION>], key=int)
for uid in target_ids:
    cfg = json.loads(cm_map.get(f'carher-{uid}-user-config',''))
    cfg.setdefault('agents',{}).setdefault('defaults',{})['<FIELD>'] = { ... }
    json.dump(cfg, open(f'new-{uid}.json','w'), indent=2, ensure_ascii=False)
with open('ids.txt','w') as f: f.write('\n'.join(target_ids)+'\n')
PY

# 并行 apply（每秒 ~10 个）
cat ids.txt | xargs -P 8 -I {} bash -c '
  ID=$1
  kubectl create cm carher-${ID}-user-config -n carher \
    --from-file=openclaw.json=/tmp/rollout/new-${ID}.json \
    --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1 \
    && echo "OK $ID" || echo "FAIL $ID"
' _ {} | tee apply.log
grep -c "^OK " apply.log
```

**节奏选择**（按风险偏好）：

| 方案 | 批量大小 | 间隔 | 总时长（~200 台）|
|---|---|---|---|
| 保守 | 1 台串行 | 每台验证后再下一台 | 3-4 小时 |
| 稳健 | 10 台/批 | 批间 60s | ~40 分钟 |
| 激进 | 20 台/批 | 批间 60s | ~18 分钟（今天实测）|

user-config 热 reload 不走 rollout，所以严格说**不受 K8s 控制面压力影响**；但若 hot-reload 后下游（如 LiteLLM）流量激增，仍建议分批观察。

## 清理回退（从 override 回到 base-config 唯一源）

**前置条件**：base-config 必须已经包含正确的最终配置（否则删掉 override 后 pod 热 reload 会回退到 base-config 的旧值）。

如果 base-config 也要改，注意它 **subPath 挂载不热 reload**，必须 **rollout restart** 新 pod 才能读到新 base-config。

**正确时序**：

```
1. 改 base-config ConfigMap + apply（pod 看不到，但 kubelet 已同步）
2. rollout restart 目标 deployment（分批）
3. 等新 pod Ready，确认新 pod 里 shared-config.json5 已是新内容：
     kubectl exec <new-pod> -c carher -- grep baseUrl /data/.openclaw/shared-config.json5
4. 从 user-config 删除 override 字段 + apply
5. sidecar ~60s 热 reload，bot 从 base-config 继承（已是新内容）→ 稳态
```

**反向时序会 revert**：先删 override 再 rollout，drainage 时旧 pod 瞬间从 override 退到 base-config 的**旧**内容。

## 批量清理 override 脚本模板

```bash
# 生成 "删除 override 字段" 版的 new openclaw.json
python3 <<'PY'
import json
cms = json.load(open('/tmp/rollout/all_cms.json'))['items']
cm_map = {cm['metadata']['name']: cm['data'].get('openclaw.json','')
          for cm in cms if cm['metadata']['name'].endswith('-user-config')}
for name, raw in cm_map.items():
    if not raw: continue
    d = json.loads(raw)
    ad = d.get('agents', {}).get('defaults', {})
    if '<FIELD>' in ad: del ad['<FIELD>']
    uid = name.replace('carher-','').replace('-user-config','')
    json.dump(d, open(f'/tmp/rollout/clean-{uid}.json','w'), indent=2, ensure_ascii=False)
PY
# 分批 apply（和 Phase 2-3 同样的 xargs 模板）
```

## 案例：memorySearch 全量切换到 LiteLLM (2026-04-21)

- **目标**：把所有 her 实例的 memorySearch 从"直连 OpenRouter"切到"走 LiteLLM"（为了 spend 统计）
- **Phase 1**：carher-1000 单实例 override，约 2 分钟
- **Phase 2**（激进）：98 个奇数 ID 并行 8 路 apply，**10 秒**完成；等 sidecar reload 120s
- **Phase 3**（激进）：98 个偶数 ID（排除 1000），**18 秒**完成
- **Phase 4**：全量 197 rollout restart（10 批 × 20）+ 清理 override，**18 分钟**
- **结果**：0 次 5xx（除滚动窗口的 3 次 rollout 抖动），用户 0 感知
- **commit**：`2aefc16 feat(base-config): route memorySearch through LiteLLM proxy by default`

## 专项：切 her 的默认模型（有专用脚本，别手搓）

`scripts/carher_her_model_switch.py`（本仓库）。**改默认模型走不了 CRD**——`spec.model` 有 enum 白名单，自建模型进不去，只能 user-config override。副作用：`kubectl get her` 的 MODEL 列会和实际运行模型不一致，这是正常的。

四个子命令，**全部 dry-run 优先，`--apply` 才写**：

```bash
S=/tmp/carher_her_model_switch.py     # 传到 k8s-work-226 上跑
python3 $S preflight --uids 25,26,71 --reference 1000
python3 $S switch    --uids 25,26,71 --reference 1000 --expect-old litellm/chatgpt-gpt-5.5   # dry run
python3 $S switch    --uids 25,26,71 --reference 1000 --expect-old litellm/chatgpt-gpt-5.5 --apply
sleep 140
python3 $S verify    --uids 25,26,71 --reference 1000
# 出事回滚（stamp 由 switch 打印出来）
python3 $S rollback  --uids 25,26,71 --stamp 20260806T101500Z --expect-old litellm/chatgpt-gpt-5.5 --apply
```

传脚本上 226：exec 通道常坏死，用 gzip+base64 分片追加，`gunzip` 成功即校验通过（别用 `jms scp`，也别 `cat f > f` 往返——会给文件尾加 `\n`）。

**核心手法是"照抄参照实例"**，不是自己拼配置：`--reference 1000` 直接复制 carher-1000 的 `agents.defaults.model` 块。原有别名只增不改，用户仍能 `/model gpt` 自己切回去。

### preflight 的三道闸门（都是拿生产事故换来的）

| 闸门 | 不过会怎样 |
|---|---|
| **group 至少 2 个 deployment** | 单点自建盒子一挂，litellm 报 `No fallback model group found` 直抛 500 → her `surface_error` → 用户看到「⚠️ Something went wrong」。2026-08-05 就是这么打挂 9 台的 |
| **key allowlist 含该模型** | 切完直接 401。**顺序必须 key 先于 config** |
| **pod 真正挂载的 base-config 里有 catalog 条目** | her 会按厂商官方规格发 `maxTokens`（如 384000），自建 sglang 盒子把 输入+max_tokens 一起算进 `max_model_len` → 每轮必 400，且伪装成"上下文爆了+压缩失败" |

第三条有个额外陷阱：**哪台挂哪份 base-config 必须实查，不许按 ID 段推断**。carher-25/26/71/72/73/74/170/178/275 名字上都不像 h75 系，实测**全挂 `carher-base-config-h75`**。脚本用 pod 的 `spec.volumes` 实查，别自己猜。

```bash
kubectl -n carher get pod <pod> -o json | jq '.spec.volumes[]|select(.configMap)|.configMap.name'
```

### switch 的路径级 diff guard

每台打印叶子级 diff，并断言三件事，任一不满足直接中止**整批**：无删除、变更全在 `/agents/defaults/model*` 下、旧 primary == `--expect-old`。正常输出应是每台恰好 2 处变更：

```
[  25] changed=2 removed=0
      /agents/defaults/model/primary: 'litellm/chatgpt-gpt-5.5' -> 'litellm/local-deepseek-v4-flash-chat'
      /agents/defaults/models/litellm/local-deepseek-v4-flash-chat/alias: '<absent>' -> 'ds'
```

备份文件名带 UTC 时间戳（`backup-<uid>-<stamp>.json`），**别用固定路径**——固定 `/tmp` 路径会让你跑到上一轮甚至别人留下的陈旧文件。rollback 也带 guard：备份里的 primary 必须等于 `--expect-old` 才恢复。

### 验收标准

- `verify` 读的是 **pod 内 `/data/.openclaw/openclaw.json`**，不是 ConfigMap——CM 写了不等于生效
- pod `restarts` / `started` 不变 → 证明是热 reload，没重启
- **smoke 必须带 tools 且流式**。裸 `{"messages":[...]}` 是假绿信号：openclaw 每轮都带 tools，只测无 tools 的形状等于没测（2026-08-05 就是这么假绿放行的）
- **合成 smoke 全绿仍不等于能服务**。切生产前先让参照实例吃一段真实对话，判据是 pod 内 `/tmp/openclaw/openclaw-<date>.log` 里的 `reply completed, card finalized`，且 `surface_error` / `Something went wrong` / `context overflow` 均为 0

### 案例：9 台阿里云实例切自建 deepseek（2026-08-05 失败 → 2026-08-06 成功）

第一次（08-05）三个坑连环踩：`mode:responses` 给 tools 注入 `strict=None` → 新 group 无兜底撞上盒子整体 500 → `maxTokens: 384000` 吃光 393216 上下文。**全部回滚**，用户两次来问"为什么不回复"。

第二次（08-06）：前三项修复已在位（key allowlist、h75 catalog `320000/65536`、组内网宿备胎 w=1），只剩一步 override。结果 9/9 pod 生效、**0 重启**、9 把 key 带 tools 流式 smoke 9/9 200（0.4~1.8s，落 `local/deepseek-v4-flash-chat`）。

**留下的风险敞口**：该 group 至今**不在 `router_settings.fallbacks`**，只靠组内两成员 + `num_retries: 2`；组内 failover **没有实测数据**，是机制推断。要补测就建隔离探针组（死端口 w=999 + 真备胎 w=1）打一发，测完即删，别在在用 group 上做。

## 专项：给 her 加 fallback 模型（有专用脚本，别手搓）

`scripts/carher_her_model_fallback_add.py`。**先读下面这段再答应任何人**，否则你会承诺一件做不到的事。

### 硬语义：openclaw 只有一条链，且用户手选的模型永不兜底

判据是引擎自带文档，pod 里就有：`/opt/openclaw/lib/node_modules/openclaw/docs/concepts/model-failover.md`。

1. **只有一条链**。`agents.defaults.model.fallbacks` 是**一个列表**，绑在 configured default primary 上，**不是 per-model 字段**。`openclaw models list` 把它渲染成 `default` / `fallback#1` / `fallback#2` tag——只有这几行有 tag，其余模型一律只有 `configured`，**那就是"它们没有兜底"的现场证据**。（per-agent 例外：`agents.list[].model` 可以自带 `fallbacks`；`agents.defaults` 没有这个粒度。）
2. **用户 `/model` 选的模型是 strict**。`/model`、model picker、`session_status(model=…)`、`sessions.patch` 会写 `modelOverrideSource: "user"` ⇒ 失败直接报错，**不会**落到 `fallbacks`。能走链的只有：configured default primary、cron job primary、带显式 fallbacks 的 agent primary、auto fallback override。
3. **context overflow 不推进链**。窗口报大了，兜底那一跳直接撞 overflow 报错给用户，而不是继续往下一个 fallback 走。

所以有人说「给 X 实例的**每个**模型都加兜底」时，先分清落点——这一步不问清楚，后面全是白做：

| 落点 | 粒度 | 生效方式 |
|---|---|---|
| **her 侧**（`openclaw.json`）| 只覆盖「默认主模型那条路」，**覆盖不到用户手选的模型** | CM patch，**热 reload、0 重启** |
| **LiteLLM 侧**（`router_settings.fallbacks`）| 才是真正"每个模型"，但**按 model group 全局生效、无 per-key 粒度** | 改一个组 = 全公司共用该组的实例一起改 |

阿里云那份 `router_settings` 是 **YAML-only**：initContainer `wipe-db-config-rows` 每次启动 DELETE 掉 DB 里的 `router_settings` / `litellm_settings` 行 ⇒ 只能改 CM + rollout，**热改 DB 无效**（`general_settings` 故意不在 wipe 名单里）。

### 用法

```bash
S=scripts/carher_her_model_fallback_add.py
python3 $S preflight --uids 1000 --model grok-4.6
python3 $S add       --uids 1000 --model grok-4.6 --alias grok \
                     --context-window 200000 --max-tokens 64000        # dry run
python3 $S add       --uids 1000 --model grok-4.6 --alias grok \
                     --context-window 200000 --max-tokens 64000 --apply
python3 $S verify    --uids 1000 --model grok-4.6
python3 $S rollback  --uids 1000 --model grok-4.6 --backup-dir <add 打印的目录> --apply
```

改动是**三处纯加法**：catalog 加 entry + `agents.defaults.models` 加 alias + `fallbacks` **末尾追加**（不替换、不动表头）。`cost` 从 LiteLLM `/model/info` 的 `litellm_params` 按每 token 换算成 catalog 的每百万单位，不手抄。

### preflight 的两道闸门

| 闸门 | 不过会怎样 |
|---|---|
| **目标模型在 key allowlist 里**（`/model/info` 能看见）| 兜底那一跳 401，用户看到的还是报错，只是换了个理由 |
| **带 tools 的实探活，非流 + 流式各一发** | **配了 ≠ 能服务**。阿里云曾经 15 个 gpt 组的兜底目标是个 `mode:responses` 条目，对任何 tools 载荷 400，整张安全网是死的、且没人知道 |

探针**从目标实例自己的 pod、用它自己的 key** 打（`kubectl exec … python3 -`）——litellm 那个镜像**没有 curl**，而且从 her 侧打才是真实用户路径。

### contextWindow 不许猜

`/model/info` 的 `max_input_tokens` 是 null 时，脚本**强制要求你显式传 `--context-window`**，并且**要往小取**：报大了就撞上面第 3 条硬语义（overflow 不推进链），用户直接吃报错，比没兜底还差。

grok-4.6 那次取 `200000` 是**保守取值不是实测值**——阿里云 `/model/info` 报 null，我们全栈没有 grok-4.6 真实窗口的数据。这种数要么实测，要么就明说是保守取值，**禁止拿"看着差不多"的数写进 catalog 再当事实引用**。

### 验收只认引擎，不认文件

```bash
kubectl exec $POD -n carher -c carher -- sh -c \
  'cd /data && HOME=/data openclaw models list' | grep grok-4.6
# litellm/grok-4.6   text   195k   no   yes   fallback#2,configured,alias:grok
#                                              ^^^^^^^^^^ 这个 tag 才是判据
```

CM 写对了不代表 gateway 加载了。第二个量具是 pod 日志里的 reload 行，它会**逐条打印变更路径**，跟你改的三条路径逐一对上才算数：

```
[reload] config hot reload applied (agents.defaults.models.litellm/grok-4.6,
         agents.defaults.model.fallbacks, models.providers.litellm.models)
```

**没验的那条**：链上真的发生过一次 failover。合成探针触发不了它（要打死 primary），所以别说"兜底已验证"，只能说"配置已生效、目标实测可服务"。

### 案例：carher-1000 加 grok-4.6 兜底（2026-09-10）

用户原话是「每个模型都加一个 fallback 为 grok-4.6」。查完上面的硬语义后**回去问了落点**，用户选「只改 carher-1000 的 her 侧」。

- 她的 key 本来就有 `grok-4.6` 白名单 + alias `grok` ⇒ **零 key 写入**
- 改前实探 grok-4.6：带 tools 非流 200（真 `tool_calls`）+ 流式 19 帧 `[DONE]`
- 链：`litellm/deepseek-v4-flash` → **追加** `litellm/grok-4.6`
- CM patch 后 **15s** 生效（比上面那张表的 60-120s 快得多，kubelet 同步快时会远小于它——**别当 120s 是下限去干等**），pod 2/2、**0 重启**
- `models list` 出 `fallback#2`，reload 日志三条路径逐一对上；主模型 `gpt-5.6-terra` 带 tools 回归 200

顺带一条**别被名字骗**的事实：carher-1000 her 侧那 10 个模型 id 经 per-key alias 落到 8 个 LiteLLM 组，其中 `claude-opus-4-8` → `chatgpt-gpt-5.6-sol`、`claude-sonnet-5` → `chatgpt-gpt-5.6-terra`——**是 GPT 组不是真 Claude**。alias 重写发生在 proxy 层、**router 之前**（`litellm_pre_call_utils.py:1972` 直接改 `data["model"]`），所以 router 侧查 fallback 用的是**改写后**的组名，按 her 侧那个名字去 `router_settings` 里找是找不到的。

## 相关 skill

- 配置挂载细节 → [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md)
- rollout 机制 → [carher-k8s-zero-downtime-rollout](../carher-k8s-zero-downtime-rollout/SKILL.md)
- memorySearch 特定路径 → [carher-memorysearch-config](../carher-memorysearch-config/SKILL.md)
- LiteLLM 侧改 group / entry / key alias → [litellm-ops](../litellm-ops/SKILL.md)、[litellm-per-key-model-alias](../litellm-per-key-model-alias/SKILL.md)
- 切完 her 不回消息的排查 → [carher-her-reply-failure-triage](../carher-her-reply-failure-triage/SKILL.md)
- **188 上的 docker 实例形状**（`hermestest-*`，`${ENV}` 语义与这里相反）→ [hermestest-web-search-provider](../hermestest-web-search-provider/SKILL.md)
