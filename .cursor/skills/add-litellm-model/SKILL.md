---
name: add-litellm-model
description: >-
  把一个新模型从网宿/快汇/OpenRouter 等 provider 端到端接入 carher：
  litellm-proxy ConfigMap → 存量 carher-* virtual key allowlist 扩张 →
  base-config wangsu provider 模型清单 → operator/admin 镜像滚动让 her
  的 openclaw.json 暴露新 alias。Use when 用户说"接入新模型"/"加 gpt-X"/
  "加 deepseek-Y"/"网宿/快汇通道新增模型"，或要把某个已有 provider 的
  额外模型暴露给所有 her 实例。涵盖前置（堡垒机隧道）、定价决策、ordering
  原则、零中断要点、常见踩坑。
---

# 接入新模型到 LiteLLM + 全量 her

新模型上线是一条多步骤流水线，错序会让用户调不到或重启 pod。本 skill 锁
死正确顺序，并把上次踩过的坑点固化下来。

## 何时用本 skill

- 网宿（cheliantianxia*）、快汇（kuaihuiai.com）、或 OpenRouter 通道新增了
  上游模型，要把它接到 carher 来
- 给所有 her 添加可选模型（不只是某一个 her）
- 需要存量 carher-* virtual key 都能调新模型

不用本 skill 的场景：

- 单个 key 切换 provider/aliases → `litellm-key-provider-swap`
- 新建一个 her → `add-instances`

---

## 前置

```bash
# kubectl 隧道（按 k8s-via-bastion）
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes >/dev/null

# Master key 必须从 secret 取（旧 SKILL 文档里的硬编码值会过期）
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
```

### Provider env vars 的来源（**记住，不在 litellm-secrets**）

litellm-proxy Deployment 用 `envFrom` 同时挂两个 secret：

| Secret | 内容 |
|---|---|
| `litellm-secrets` | `LITELLM_MASTER_KEY` / `DATABASE_URL` / `KUAIHUI_API_KEY`（仅 aliyun） |
| `carher-env-keys` | **`WANGSU_API_KEY` / `WANGSU_DIRECT_API_KEY` / `WANGSU_DIRECT5_API_KEY` / `WANGSU_DIRECT6_API_KEY` / `WANGSU_EMBEDDING_KEY` / `OPENROUTER_API_KEY` / `EMBEDDING_*` / `LITELLM_API_KEY`** |

```bash
# 看 / 改 wangsu key（aliyun + 198 prod + 198 dev 三套**共享同一把** WANGSU_API_KEY）
kubectl get secret carher-env-keys -n <ns> -o jsonpath='{.data.WANGSU_API_KEY}' | base64 -d; echo
kubectl patch secret carher-env-keys -n <ns> --type=json \
  -p='[{"op":"replace","path":"/data/WANGSU_API_KEY","value":"'$(echo -n "<NEW_KEY>" | base64 -w0)'"}]'
```

不要去 `litellm-secrets` 找 `WANGSU_API_KEY` —— 不在那。

### Pre-flight key probe（key 轮换 / 新通道首次接入必跑）

网宿 cheliantianxia1 用 IP 白名单：`58.241.5.230`（198 出口）+ `47.84.112.136`（aliyun build server）。**直接从 build server curl 网宿验证 key 已激活**，再动 LiteLLM secret，否则轮换后才发现 key 没生效就 401 风暴：

```bash
scripts/jms ssh k8s-work-227 'curl -sS -X POST \
  -H "Authorization: Bearer <NEW_WANGSU_KEY>" -H "Content-Type: application/json" \
  https://aigateway.edgecloudapp.com/v1/23dcb2866d219047ae6edd6a2724dbc2/cheliantianxia1/chat/completions \
  -d "{\"model\":\"<EXISTING_MODEL>\",\"messages\":[{\"role\":\"user\",\"content\":\"PONG only\"}],\"max_tokens\":20}" \
  -w "\nHTTP=%{http_code}\n" --max-time 60' | tail -3
```

任何新模型也要在这一步逐一探针，HTTP 200 才能进入下一步。glm-5.1 / gemini-3.5-flash 等 reasoning 模型若 max_tokens=20 太小会出现 `content="" reasoning_tokens=N`，仍属 200 通过。


---

## 决策清单（开工前对齐）

| 决策点 | 推荐 | 备选 |
|---|---|---|
| **定价** | 上游官方满价（如 OpenAI $5/$30、DeepSeek 满价） | 网宿/快汇实际报价（如知道） |
| **alias 风格** | 短、不带点号，类似 `gpt55`/`ds-pro` | 带点号 `gpt-5.5` |
| **fallback 链** | 暂不配（单通道） | 接 OpenRouter 同型号备份 |
| **存量 key allowlist** | 全部更新（否则老用户调不到） | 只 patch 新建逻辑 |
| **base-config drift** | 外科 `kubectl patch` 单个 key | 整 yaml `kubectl apply`（注意附带未上线改动） |

满价定价的理由：促销价会到期，按满价记账更稳，spend tracking 不会出现
"促销结束后单价漂移"。

---

## 涉及文件清单

| 文件 | 改什么 |
|---|---|
| `k8s/litellm-proxy.yaml` | `model_list` 加 `wangsu-<新模型>` 条目（`custom_openai/<id>` + cheliantianxia1 base + `WANGSU_API_KEY` + 双份 cost 字段） |
| `backend/litellm_ops.py` | `_BASE_MODELS` 加新 `model_name`（影响新生成 key 默认 allowlist） |
| `backend/config_gen.py` | litellm provider 分支 `models` map 加 `litellm/wangsu-<新模型>: alias` + `providers.litellm.models` 列表加 id/name/cost 元数据 |
| `operator-go/internal/controller/config_gen.go` | 同 Go 侧（**必须双写，operator 是真正写每个 her ConfigMap 的那一端**） |
| `k8s/base-config.yaml` | wangsu provider 的 `models` 数组加新 id（可选，仅影响 provider=wangsu 的 her UI selector） |
| `backend/tests/test_config_gen.py` | 加 alias / provider models 断言 |
| `operator-go/internal/controller/config_gen_test.go` | 同上，注意 `len(models)` 计数也要对应 +N |
| `docs/litellm-ops-skill/litellm-ops/SKILL.md` | 模型路由表 + Key 创建样例 |

`backend/config_gen.py` 和 `operator-go/.../config_gen.go` 是同一逻辑的双
份实现，**漏一处 = 重启后丢失**（admin 走 Python，operator 走 Go）。

### LiteLLM cost 字段双写

LiteLLM v1.82.6 有个已知 bug：`model_info.id` 与 bundled model_prices 冲突
时 `register_model()` 不注入 cost。**必须**把 `input_cost_per_token`/
`output_cost_per_token` 同时写进 `litellm_params` 和 `model_info`。

```yaml
- model_name: wangsu-<NEW_MODEL>
  litellm_params:
    model: custom_openai/<NEW_MODEL>
    api_key: os.environ/WANGSU_API_KEY
    api_base: https://aigateway.edgecloudapp.com/v1/<TENANT_ID>/cheliantianxia1
    input_cost_per_token: <USD_PER_TOKEN>
    output_cost_per_token: <USD_PER_TOKEN>
  model_info:
    id: wangsu/<NEW_MODEL>            # 唯一 id 避开 bedrock_converse 等内置
    input_cost_per_token: <USD_PER_TOKEN>
    output_cost_per_token: <USD_PER_TOKEN>
```

---

## 上线顺序（必须按此顺序，不能错）

> **核心原则**：proxy 先开放新模型 → key 先 allow → her 再暴露。
> 反过来做会出现 her 暴露 alias 但 key 调用 401。

### 步骤 1：apply litellm-proxy ConfigMap + 重启

```bash
kubectl diff -f k8s/litellm-proxy.yaml | head -100      # 看 diff 干净
kubectl apply -f k8s/litellm-proxy.yaml

# 关键：apply 不会自动重启 pod，model_list 是启动时读
kubectl rollout restart deployment/litellm-proxy -n carher
kubectl rollout status deployment/litellm-proxy -n carher --timeout=180s

# 验证：/v1/models 能看到新 3 个
kubectl exec deployment/litellm-proxy -n carher -- env MK="$MK" python -c "
import urllib.request, json, os
mk = os.environ['MK']
req = urllib.request.Request('http://localhost:4000/v1/models', headers={'Authorization':f'Bearer {mk}'})
ids = sorted(m['id'] for m in json.loads(urllib.request.urlopen(req, timeout=10).read())['data'])
print('total:', len(ids), 'new:', [i for i in ids if 'wangsu-<NEW_KEYWORD>' in i])
"
```

冒烟一次（max_tokens=4 验证连通即可，reasoning 模型可能返回空 content
但 HTTP 200 就算通）。

### 步骤 2：扩张存量 carher-* key allowlist

**不要用 `/key/bulk_update`** —— 它把所有传入 key 同质化为同一份 models
列表，会丢失各 key 已有的非默认条目（如 `anthropic.claude-opus-4-7`）。
正确姿势：拉每个 key 的当前 models，逐个 union 后 `/key/update`。

```bash
kubectl exec deployment/litellm-proxy -n carher -- env MK="$MK" python <<'PY' 2>&1 | tail -30
import urllib.request, json, os
mk = os.environ['MK']
hdr = {'Authorization': f'Bearer {mk}', 'Content-Type': 'application/json'}
NEW = ['wangsu-<M1>','wangsu-<M2>','wangsu-<M3>']

# 一把拉所有 key（含 token + alias + models）
req = urllib.request.Request('http://localhost:4000/spend/keys?limit=2000', headers={'Authorization':f'Bearer {mk}'})
all_keys = json.loads(urllib.request.urlopen(req, timeout=30).read())
carher = [k for k in all_keys if (k.get('key_alias') or '').startswith('carher-')]
# 关键：models == [] 在 LiteLLM 表示"不限制" → 强加 3 个反而会限制它，**必须跳过**
targets = [k for k in carher if k.get('models')]
print(f'targets: {len(targets)} / total: {len(carher)} / skipped (empty allowlist): {len(carher)-len(targets)}')

ok = fail = 0
for k in targets:
    cur = list(k.get('models') or [])
    new_models = list(dict.fromkeys(cur + NEW))   # dedupe, preserve order
    body = json.dumps({'key': k['token'], 'models': new_models}).encode()
    req = urllib.request.Request('http://localhost:4000/key/update', data=body, headers=hdr)
    try:
        urllib.request.urlopen(req, timeout=10).read(); ok += 1
    except Exception as e:
        fail += 1; print(f'fail {k["key_alias"]}: {e}')
print(f'DONE: ok={ok} fail={fail}')
PY
```

**关于空 allowlist 的 LiteLLM 语义**：`models = []` = "不限制模型，all
allowed"；`models = [...]` = "白名单"。盲目 set 会从全开变成 3 个白名单。
所以脚本必须 `if k.get('models')` 过滤。

### 步骤 3a：base-config carher-config.json patch（外科手术）

仓库里 `k8s/base-config.yaml` 经常领先 live ConfigMap（如挂起的
`truncateAfterCompaction: true`）。整 yaml `kubectl apply` 会**顺带**推
未授权的改动。改用单 key patch：

```bash
# 拉 live carher-config.json，splice 3 个新模型，patch 回去
kubectl get cm carher-base-config -n carher -o jsonpath='{.data.carher-config\.json}' > /tmp/live.json

python3 <<'PY'
import json
with open('/tmp/live.json') as f: cfg = json.load(f)
ws = cfg['models']['providers']['wangsu']
NEW = [
    {"id": "<M1>", "name": "<NAME1>", "api": "openai-completions", ...},
    # ...
]
existing = {m['id'] for m in ws['models']}
for m in NEW:
    if m['id'] not in existing:
        ws['models'].append(m)
with open('/tmp/new.json','w') as f: json.dump(cfg, f, indent=2, ensure_ascii=False)
PY

python3 -c "
import json
with open('/tmp/new.json') as f: v = f.read()
with open('/tmp/cm-patch.json','w') as f: json.dump({'data': {'carher-config.json': v}}, f)
"
kubectl patch cm carher-base-config -n carher --type=merge --patch-file=/tmp/cm-patch.json

# 验证 shared-config.json5 没被动
kubectl get cm carher-base-config -n carher -o jsonpath='{.data.shared-config\.json5}' | grep truncateAfterCompaction
# 应该没有输出（即仍是挂起状态，不在 cluster 上生效）
```

### 步骤 3b：build + roll operator/admin（让 her UI 看到新 alias）

**不走 GitHub Actions** —— `.github/workflows/build-deploy.yml` 只构建
`her/carher` 主程序，admin/operator 是手动 nerdctl 在 `k8s-work-227`
上构建。

```bash
TAG="v$(date +%Y%m%d)-$(git log -1 --format=%h)"

scripts/jms ssh k8s-work-227 'bash -s' <<EOF
set -e
cd /root/carher-admin
git checkout main && git pull --ff-only

# carher-admin
nerdctl --namespace k8s.io build \
  -t cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:${TAG} \
  -f Dockerfile .
nerdctl --namespace k8s.io push \
  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:${TAG}

# carher-operator
cd operator-go
nerdctl --namespace k8s.io build \
  -t cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:${TAG} \
  -f Dockerfile .
nerdctl --namespace k8s.io push \
  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:${TAG}
EOF

# 容器名注意：operator 的容器叫 `operator`，admin 的容器叫 `admin`
# （不是默认的 `manager` / `<deploy-name>`）
kubectl set image deployment/carher-operator operator=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:${TAG} -n carher
kubectl rollout status deployment/carher-operator -n carher --timeout=180s

kubectl set image deployment/carher-admin admin=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:${TAG} -n carher
kubectl rollout status deployment/carher-admin -n carher --timeout=180s
```

operator 滚完后会全量 reconcile 每个 HerInstance，**通过 sidecar
config-reloader 实现热加载，0 pod 重启**，最小用户感知。

### 验证（抽样）

```bash
# 抽一个 her 看 openclaw.json 里 alias 已注入
kubectl get cm carher-99-user-config -n carher -o jsonpath='{.data.openclaw\.json}' \
  | python3 -c "
import sys, json
m = json.loads(sys.stdin.read())['agents']['defaults']['models']
print({k: v.get('alias') for k,v in m.items() if 'wangsu-' in k})
"
```

---

## contextWindow 对齐（每次新增模型后必查）

新增模型时，顺手检查**存量模型**的 `contextWindow` 是否与官方规格一致。
历史遗留：早期所有模型统一写 `200000`，后来官方纷纷升到 1M，旧值变成了错误的上限。

### 官方 contextWindow 参考（截至 2026-05）

| 模型 | 官方 contextWindow | maxTokens（输出上限）|
|---|---|---|
| claude-opus-4-6 / claude-sonnet-4-6 | **1 000 000** | 128K / 64K |
| gpt-5.4 / gpt-5.5 | **1 000 000** | 128K |
| gemini-3.1-pro-preview | **1 000 000** | 65 536 |
| anthropic.claude-opus-4-7 | **1 000 000** | 128K |
| deepseek-v4-pro / deepseek-v4-flash | **1 000 000** | 384K |
| minimax-m2.7 | 200 000（官方限制） | 128K |
| glm-5 | 128 000（官方限制） | 32K |
| gpt-5.3-codex | 200 000（官方限制） | 128K |

后三个模型**有意保留低值**，不应改到 1M。

### 需要同步修改的 3 个地方

```
backend/config_gen.py               → providers.litellm.models 列表里每个 id 的 contextWindow
operator-go/.../config_gen.go       → 同上（Go 侧，漏改 = operator rollout 后 her 仍用旧值）
k8s/base-config.yaml                → wangsu 和 openrouter provider 两个 section 各自的 models 数组
```

base-config.yaml 改完用外科手术 patch（同上文步骤 3a），不要 `kubectl apply` 整文件。
改完需要走完整 build + rollout（步骤 3b），否则 her 的 openclaw.json 不更新。

---

## 网宿 cheliantianxia1 同步：三套环境一键流程

> **触发场景**：网宿运营给你新的 cheliantianxia1 spec yaml（含 `tokens.value` 新值 + 模型清单变更），需要把变更同步到 **aliyun carher / 198 prod / 198 dev** 三套 LiteLLM。
>
> **作用域**：本 skill 上面的"上线顺序 5 步"主要服务 aliyun carher 全栈（含 her）。本节专门针对**网宿 cheliantianxia1 渠道**的批量更新（key 轮换 + 模型增删），三套环境共用同一把 WANGSU_API_KEY，必须同步轮换。

### 推荐顺序（由小到大爆炸半径）

```
198 dev → 198 prod → aliyun carher
```

dev 最先，prod 双副本零中断兜底，aliyun 最后再走完整 her 暴露链路（步骤 3a/3b/2 = 步骤 6-15）。

### Phase -1：Pre-flight（必跑）

见上方"Pre-flight key probe"。三个目标，全 200 才进入下一步：
1. 新 WANGSU_API_KEY + 一个**已存在**模型（验证 key 生效）
2. 新 WANGSU_API_KEY + 每个**新增**模型（验证模型在网宿端已开）
3. spec 没列出但仍在我们 ConfigMap 里的模型（验证不会因 spec 漏写导致丢通道）

### Phase 1+2：198 dev 与 198 prod（同一脚本，仅 namespace 不同）

198 manifest 格式坑：`30-cm-litellm-config.yaml` 实际是 **JSON 文件，`data["config.yaml"]` 是 yaml 字符串**。`kubectl apply` 旧 manifest 会因 stale resourceVersion 报 conflict（见 [[kubectl_apply_stale_resourceversion]] 类型经验）。**用 `kubectl get -o json` 拉 live → Python 解析 + splice → kubectl replace** 是最稳的姿势：

```bash
scripts/jms ssh AIYJY-litellm "bash -s" <<'REMOTE'
set -euo pipefail
NS=litellm-dev   # 或 litellm-product
TS=$(date +%Y%m%d-%H%M%S)
DIR=/root/litellm-dev   # 或 /root/litellm-product-manifests

# (a) Backup live cm
kubectl -n $NS get cm litellm-config -o yaml > $DIR/30-cm-litellm-config.yaml.bak-$TS

# (b) Splice 新 model_list 条目
python3 <<'PY'
import yaml, json, subprocess, os
ns=os.environ.get("NS")
out = subprocess.check_output(["kubectl","-n",ns,"get","cm","litellm-config","-o","json"])
cm = json.loads(out)
cfg = yaml.safe_load(cm["data"]["config.yaml"])

NEW = [
  {"model_name":"wangsu-<NEW1>",
   "litellm_params":{"model":"custom_openai/<NEW1>","api_key":"os.environ/WANGSU_API_KEY",
     "api_base":"https://aigateway.edgecloudapp.com/v1/23dcb2866d219047ae6edd6a2724dbc2/cheliantianxia1",
     "input_cost_per_token":<INPUT>,"output_cost_per_token":<OUTPUT>},
   "model_info":{"id":"wangsu/<NEW1>","input_cost_per_token":<INPUT>,"output_cost_per_token":<OUTPUT>}},
  # ... 其他新增同样格式
]
existing = {m.get("model_name") for m in cfg.get("model_list", [])}
to_add = [m for m in NEW if m["model_name"] not in existing]

ml = cfg["model_list"]
# 在最后一个 wangsu cheliantianxia1 条目后插入（保持 grouping）
insert_at = max((i+1 for i,m in enumerate(ml)
                 if m.get("model_name","").startswith("wangsu-")
                 and "cheliantianxia1" in str(m.get("litellm_params",{}).get("api_base",""))),
                default=len(ml))
for m in reversed(to_add):
    ml.insert(insert_at, m)

# Strip server-managed metadata 防 replace 冲突
for k in ("resourceVersion","creationTimestamp","uid","managedFields"):
    cm["metadata"].pop(k, None)
cm["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
cm["data"]["config.yaml"] = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True)
with open("/tmp/cm-new.yaml","w") as f:
    yaml.safe_dump(cm, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
print("to_add:", [m["model_name"] for m in to_add])
PY

# (c) Replace + rotate secret + restart
NS=$NS kubectl -n $NS replace -f /tmp/cm-new.yaml
kubectl -n $NS patch secret carher-env-keys --type=json \
  -p='[{"op":"replace","path":"/data/WANGSU_API_KEY","value":"'$(echo -n "<NEW_WANGSU_KEY>" | base64 -w0)'"}]'
kubectl -n $NS rollout restart deployment/litellm-proxy
kubectl -n $NS rollout status deployment/litellm-proxy --timeout=300s
REMOTE
```

注意：**168 dev / 168 prod 的 `model_group_alias`** 把 `wangsu-gpt-5.4` / `wangsu-gpt-5.5` 等映射到 chatgpt-* 上（ChatGPT Pro 池），不是真的去 wangsu。这个跟本次同步无关，dev 即使没显式 model_list 条目也能通过 alias 工作。

### Phase 3：aliyun carher LiteLLM proxy

走本 skill 上面的"上线顺序步骤 1"标准流程（编辑 `k8s/litellm-proxy.yaml` → `kubectl apply` → restart）。**额外**做 secret 轮换 `kubectl patch secret carher-env-keys` 跟 198 同样。

⚠️ **预期 rollout 时长 15-20 min**：aliyun litellm-proxy 用 `terminationGracePeriodSeconds: 600s` + `nodeAffinity` 限定 3 候选节点 + `hostPort: 4000` + `replicas: 2` + `maxSurge: 1`，**新 pod 必须等老 pod 完全终止才能调度上同一节点**。`deployment "litellm-proxy" exceeded its progress deadline` 报错是预期，不是失败 —— 看 `kubectl get pods` 双副本始终 ≥2 ready 即可，service 不中断。

### Phase 4 + 5：her 侧 + 验证

走 skill 上面"上线顺序步骤 2 / 3a / 3b"完整流程（仅 aliyun，198 不接 her）。

---

## 仓库提交规则


`carher-admin` **直接提交到 main**，不开 feature 分支不走 PR：

```bash
git add ...
git commit -m "feat(litellm): add <models> via <provider>"
git push origin main
```

仓库历史是线性的（看 `git log` 没有 merge commit）。

---

## 常见踩坑

| 症状 | 根因 | 解 |
|---|---|---|
| `/v1/models` 看不到新模型，但 ConfigMap 里有 | apply ConfigMap 不会自动重启 pod | `kubectl rollout restart deployment/litellm-proxy` |
| `kubectl exec ... curl ...: not found` | litellm-proxy 镜像没装 curl | 用 `python -c "import urllib.request"` 替代 |
| 401 Unauthorized hitting LiteLLM API | 用了 SKILL doc 里硬编码的旧 master key | 永远从 `kubectl get secret litellm-secrets` 取 |
| `kubectl set image deployment/X ...: unable to find container "manager"` | controller-runtime 模板默认 `manager`，但 carher 用 `operator`/`admin` | 先 `get deploy -o jsonpath='{...containers[*].name}'` 确认容器名 |
| spend 一直是 $0 | `model_info.id` 撞内置 model_prices 表 | `litellm_params` 和 `model_info` 双写 cost；id 用唯一前缀如 `wangsu/...` |
| 部分 carher-* key 一直调不到新模型 | 这些 key models 数组原本是 `[]`（无限制），脚本误把它们覆写为白名单 | 跳过 `models == []` 的 key，保持其无限制状态 |
| `kubectl apply k8s/base-config.yaml` 顺带推未授权改动 | 仓库 yaml 比 live ConfigMap 领先（有挂起的 fix） | 用 `kubectl patch --type=merge --patch-file=...` 单 key patch |
| `/api/ci/trigger-build` 不构建 admin | 那个 workflow 只 build `her/carher` | 走 `k8s-work-227` 手动 nerdctl 构建 |
| `kubectl exec deploy/litellm-proxy -- env MK=$MK python3 <<'PY' ... PY` 静默成功但 0 字节 stdout，0 个 key 被更新（"假成功"） | bash heredoc + python heredoc 多层 quoting，env / stdin 被 ssh / kubectl exec 的 wrapper 吞掉，python 根本没执行就 `exit 0` | 改用本地 `kubectl port-forward svc/litellm-proxy 14000:4000` + 本地 `python3 <<PY ... PY`；总跑完后**反查 LiteLLM `/spend/keys` 真实 allowlist 字段**确认，不要靠 exit code |
| aliyun litellm-proxy `rollout status` 报 `exceeded its progress deadline` | `terminationGracePeriodSeconds: 600s` + nodeAffinity 限 3 节点 + hostPort 4000 + 2 副本：新 pod 必须等老 pod 完全终止才能调度上同一节点；老 pod 走完整 600s grace 才被 SIGKILL | 不是失败，等就行（总 15-20 min）。判断：`kubectl get pods -l app=litellm-proxy` 双副本始终 ≥2 ready，service 不中断 |
| 网宿 cheliantianxia1 key 轮换后某些请求 401，但其他请求 200 | 三套环境（aliyun / 198 prod / 198 dev）共享同一把 `WANGSU_API_KEY`，只在一个 namespace 改了 secret 但其他没改 | 三套环境**同步**轮换 `kubectl patch secret carher-env-keys`（见上面"网宿 cheliantianxia1 同步"章节） |
| 不知道 `WANGSU_API_KEY` 在哪 / 改了 `litellm-secrets` 但不生效 | wangsu 系列 env 在 `carher-env-keys` 不在 `litellm-secrets`（litellm-proxy `envFrom` 同时挂两个） | 见上面"前置 → Provider env vars 的来源" |
