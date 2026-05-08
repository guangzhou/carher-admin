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
