---
name: add-litellm-model
description: >-
  把一个新模型端到端接入 carher（阿里云 LiteLLM，ns `carher`）：live
  `cm/litellm-config` 外科式加条目 → 让存量 carher-* virtual key 调得到
  （**198 桥模型走 access group `pro198`，零 key 写入**；wangsu/local 等直连
  provider 才需逐把扩白名单）→ base-config 模型清单 → operator/admin 镜像
  滚动让 her 的 openclaw.json 暴露新 alias。Use when 用户说"接入新模型"/
  "加 gpt-X"/"加 deepseek-Y"/"网宿/快汇通道新增模型"/"把 198 上的某个模型
  接到阿里云/her 上"/"让所有 her 都能用 X"。涵盖前置（堡垒机隧道）、定价
  决策与对账、ordering 原则、零中断要点、量具会骗人的十几种形状。
---

# 接入新模型到 LiteLLM + 全量 her

新模型上线是一条多步骤流水线，错序会让用户调不到或重启 pod。本 skill 锁
死正确顺序，并把上次踩过的坑点固化下来。

## 何时用本 skill

- 网宿（cheliantianxia*）、快汇（kuaihuiai.com）、或 OpenRouter 通道新增了
  上游模型，要把它接到 carher 来
- **把 198 prod LiteLLM 上已有的模型接到阿里云**（走公网 `cc.auto-link.com.cn/pro`
  当 provider）—— 这条路已脚本化，见步骤 2A
- 给所有 her 添加可选模型（不只是某一个 her）
- 需要存量 carher-* virtual key 都能调新模型

不用本 skill 的场景：

- **ChatGPT Pro 池新模型变体**（如 GPT-5.6 Sol/Terra/Luna）→ `chatgpt-pool-model-variant`（有独立流水线，且存在 `db_model: True` + `mode: responses` 导致 "No connected db" 的致命陷阱）
- 单个 key 切换 provider/aliases → `litellm-key-provider-swap`
- **只给全部 her 加一个「对外名」并映射到已有组**（`her-pro`/`her-flash` 这种）→
  `her-public-model-name`（不碰 base-config / operator，纯 CM+key 两步 + 回归矩阵）
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
| **live `cm/litellm-config`（ns `carher`）** | **唯一通路**：`model_list` 加条目（`custom_openai/<id>` + base + key env + 双份 cost 字段）。⛔ 不是仓库那份 `k8s/litellm-proxy.yaml` —— 它已比 live 落后 600 余行，apply 会推别人的挂起改动。仓库文件只当参考，改完可另行同步但**不许拿它 apply** |
| `backend/litellm_ops.py` | `_BASE_MODELS` 加新 `model_name`（影响新生成 key 默认 allowlist）。⚠️ **桥模型这里应该加的是 `pro198` 这个组标签**，加一次管所有 198 桥模型；不加则**将来新建**的实例拿不到组标签（这是 access group 机制唯一的漂移点）|
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

**双写里只有 `litellm_params` 那份真计费**（2026-09-05 Astra 实证）：只写
`model_info` 时写入返 200、`/model/info` 也读得到，但计费仍走默认值——和漏
`base_model` 一样是零费/错费指纹。所以：

- cache 类字段同样双写：`cache_read_input_token_cost`、
  `cache_creation_input_token_cost`，以及用得到的阶梯位
  （如 Astra 的 `cache_read_input_token_cost_above_272k_tokens`
  / `..._above_272k_tokens_priority`）。
- **用不到的阶梯位留空，别填 `0.0`**——字面零会按免费计。
- DB 改完必 `rollout restart` proxy，否则只有恰好重载过的副本生效。
- 判据是**逐行读回来数** ok/bad，不是写入的 200：
  `scripts/litellm-astra-cache-price-patch.py --verify`（Astra 形状，可照抄改
  `MODEL_NAME`/`PRICES` 复用到别的模型组）。

### `model_info.id` 必须显式设置（适用于 ConfigMap 和 admin API `/model/new`）

**绝对不要省略 `model_info.id`！** LiteLLM 默认自动生成 UUID（如 `29904ae5-eb92-4941-bf1e-57574bf1be18`），导致：
- 人眼无法识别条目归属（`/model/info` 全是 UUID）
- 自动化脚本按 ID 匹配失败
- SpendLogs 追踪无法按来源分组
- 后续 `/model/delete` 必须先查 UUID 才能操作

**命名规范**：用唯一前缀避开内置 model_prices 表。ConfigMap 模型用 `wangsu/<MODEL>`；DB-registered ChatGPT 端点用 `chatgpt-acct-N-gpt-5.x`。2026-06-09 因漏传此字段，一次性清理了 15 个 UUID 条目。

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

### 步骤 1：改 litellm-proxy ConfigMap + 重启

⛔ **禁 `kubectl apply -f k8s/litellm-proxy.yaml`**（2026-09-09 起硬规矩）。仓库那份
已比 live **落后 600 余行**，apply 会把别人挂起的改动一并推上生产。而且这套 proxy
上**常年有别的会话在并发写同一个 CM**（09-09 一天内实测 163→165 条，`resourceVersion`
在两小时里变了两次）。所以只有一条合法路径：**读 live → 外科式改 data → `kubectl patch cm`**。

```bash
# (a) 落盘备份 + 记下 resourceVersion 当并发基线
kubectl -n carher get cm litellm-config -o json > /tmp/cm-$(date +%s).json
kubectl -n carher get cm litellm-config \
  -o jsonpath='{.metadata.resourceVersion}'; echo

# (b) splice 新条目进 model_list 末尾（`litellm_settings:` 之前），只改 data 这一个 key
kubectl -n carher patch cm litellm-config --type=merge --patch-file=/tmp/patch.json

# (c) 回读必须与写入内容逐字节相等；resourceVersion 变了就先 diff 清楚别人加了什么
```

然后 `kubectl -n carher rollout restart deployment/litellm-proxy`
（⛔ 同样禁 apply deployment，只 `set image` / `patch` / `rollout restart`）。

⚠️ **预期 ~25 分钟，不是 3 分钟**：`terminationGracePeriodSeconds: 600` + nodeAffinity
限 3 节点 + hostPort 4000 + `replicas 2 / maxSurge 1` ⇒ 新 pod 必须等老 pod 完全终止。
`rollout status --timeout=180s` 必然超时、日志报 `exceeded its progress deadline`——
**这是正常输出，不许当失败去回滚**。真判据是两条：全程 ready ≥ 1，且**容器内
`/app/config.yaml` 的 sha256 == 新 CM 内容的 sha256**（每个 pod 都要数）。
只看 `rollout status` 会漏掉「pod 起来了但挂的是旧 CM」这一档。

```bash
# 唯一可信的收敛判据
kubectl -n carher exec <pod> -c litellm -- sha256sum /app/config.yaml
```

验证 `/v1/models` 见到新名字 —— ⚠️ **出现 ≠ 健康**（`/v1/models` 只读 config，
别名列出来了照样可能 400/无腿），必须继续打一发带唯一 nonce 的真实推理。

⚠️ 别用 `kubectl exec deployment/... -- python <<'PY'` 跑验证脚本：多层 heredoc
quoting 会被吞掉，**python 根本没跑就 exit 0**，读起来像"跑完了没问题"。
本地 `kubectl -n carher port-forward svc/litellm-proxy 14000:4000` + 本地 python。

### 步骤 2：让存量 carher-* key 能调到它

**先分诊，两条路差一个数量级的工作量：**

| 新模型是什么 | 走哪条 | key 写入次数 |
|---|---|---|
| **级联到 198 的桥模型**（`api_base: https://cc.auto-link.com.cn/pro/v1`）| access group `pro198` | **0** |
| 阿里云自己直连的（wangsu / local / official / chatgpt-acct-*）| 逐把扩白名单 | 375 |

⚠️ 「配好 provider 就能用、不用开权限」这个直觉在这套 fleet 上**是错的**：
375 把 `carher-*` key 的 `models` **全部非空**（`models==[]` 无限制的 0 把，psql 实测）。
CM 里加完 model_group 之后，her 的 key 打它必得
`403 key_model_access_denied: This key can only access models=[...]`。

#### 2A. 桥模型 —— 打组标签，零 key 写入（2026-09-09 起）

CM 条目加一行 `model_info.access_groups: ["pro198"]` 即可：375 把 key 的 `models` 里
已经全部含 `pro198` 这一个 token。`_check_model_access_helper` 会先查
`llm_router.get_model_access_groups(...)`，命中即放行。

整条流水线（198 侧扩桥 key + CM splice + rollout + 全套验收）已脚本化：

```bash
# 预演，什么都不写，顺带把定价从 198 抄过来
./scripts/aliyun-pro198-add-model.py --public-name <对外名> --upstream-name <198上的名>

# 真做（需要 198 master key 去放宽桥 key 的 models）
MK198=$(...) ./scripts/aliyun-pro198-add-model.py --mode apply --public-name ... --upstream-name ...

# 只验一个已上线的
./scripts/aliyun-pro198-add-model.py --mode verify --public-name ... --upstream-name ...
```

⛔ **绝不许往阿里云 CM 里引入含 `*` 的 `model_name`**：live 现在 0 条通配。一旦有一条，
`pro198` 就变成 wildcard-route access group，`_check_model_access_group` 在非 premium 下
会让**所有 key 写入直接 403 enterprise**（实测 `premium_user=False`、无 `LITELLM_LICENSE`）。
这也是当初否掉 `pro198/*` 通配桥的第二个理由（第一个是**一条 entry 只能带一份定价**，
grok in `2.0e-06` vs gemini `7.5e-07` 差 2.7 倍）。

#### 2B. 非桥模型 —— 仍需逐把扩白名单

**用 `scripts/litellm-198-key-allowlist.py`，别手搓**（它已内置 read-merge-write、
跳过 `models==[]`、backup/restore）。`LITELLM_BASE` 指到阿里云本地 port-forward。

```bash
kubectl -n carher port-forward svc/litellm-proxy 14000:4000 &
LITELLM_BASE=http://127.0.0.1:14000 LITELLM_MASTER_KEY=$MK \
  ./scripts/litellm-198-key-allowlist.py --add-model <名> \
    --prefix carher- --backup /tmp/bak.json --apply
```

⚠️ **默认 `--prefix` 是 `cursor-,claude-`，不加 `--prefix carher-` 会规划出 0 把**
（第一次跑必踩，且它安静地报"0 keys planned"，读起来像"已经都有了"）。

四条铁律：
- **`models == []` 表示"不限制"**，往里写白名单是**吊销**，必须跳过。⛔ 禁 `/key/bulk_update`（会把所有 key 同质化，丢掉各自的非默认条目）。
- **串行，禁并发** —— 09-08 实测并发批量写 key 会静默整片互相覆盖（632 把写完只剩 115 把生效，**且双方全返 200**）。
- **收敛只认 psql**，不认脚本自己的 readback（走同一条 API）。一遍不收敛就重跑到收敛，不许拿单遍 `applied_ok` 结案：
  ```bash
  NS=carher KUBECTL=kubectl FAMS=carher- ACCESS_GROUPS=pro198 GRANTED=1 \
    bash scripts/litellm-198-key-allowlist-verify.sh <名>
  ```
  ⛔ **参数名不是 `GROUPS`**（2026-09-16 前的文档写的是它，照抄会假红）：`GROUPS` 是
  bash 内建只读数组（调用者的 gid），`GROUPS=pro198` 被静默丢弃、读回 `"0"`
  ⇒ 组标签那条腿永远匹配不到，任何 `pro198` 桥模型都会报出几百条不存在的缺失。
  同轮还补了 `models` 含 `'*'` 的判定（实测那是全放行，不是一个名字）。
- **撤权限必须带 `--include-blocked`**：blocked 只挡认证、不清白名单，解封会静默复活。
- ⚠️ `/key/list` 的 `size` 上限 100（>100 直接 422）；198 的 `key_alias` 是**全局唯一**的，改名前先确认没被别的 key 占了（09-09 `aliyun-carher-bridge` 就已被一把无关的 key 占用，只能改叫 `aliyun-carher-pro198`）。
- ⚠️ 拿 her 的**真 raw key** 验收，别拿 `/spend/keys` 返的 `token`（那是 sha256 哈希，当 Bearer 必 401 —— 一个和"权限没开"长得一模一样的错）：
  ```bash
  kubectl -n carher get cm carher-<uid>-user-config \
    -o go-template='{{index .data "openclaw.json"}}'   # .models.providers.litellm.apiKey
  ```
  ⚠️ 这里**别用 `-o jsonpath='{.data.openclaw\.json}'`**：它对某些 cm 静默返空串（同一条命令对别的 cm 好用），下游直接 JSONDecodeError。

#### 验收姿势（两条路都适用）

- 每一发探针**带唯一 nonce 要求回显**：LiteLLM **响应缓存是开的**，body 逐字节相同的枪不会到达上游，"32/32 全绿而上游计数 0" 就是这么来的。
- `max_tokens` 别给 4：小上限会把 reasoning 模型截成空 content，读起来像坏了。给 ≥512。
- **阴性对照必做**：另取一把**没有**该权限的 key 打同一个名字，必须 403。没有这条，上面的绿不可信。
- psql 查 SpendLogs **双列**（`model_group` 是请求名、`model` 是落点），且 `completion_tokens ≠ 0`（同时证明 callback 层没被吞）。⚠️ 别用 `/spend/logs` 端点，实测它返 0 条而 psql 里有行。
- ⚠️ **对账公式**：`spend = 未命中缓存的输入×in + cached_tokens×cache_read + 计费输出×out`，
  其中 **`reasoning_tokens` 计费但通常不在 `completion_tokens` 列里**。所以两行
  (prompt, completion) 完全相同而 spend 不同是**正常的**，别当成定价配错了。
- ⚠️ mac 上**没有 `timeout` 和 `shuf`**：`timeout 30 kubectl ... | grep` 会整条空转、退出码 0、
  打出干净的"零命中"。用 `kubectl --request-timeout=30s`；随机抽样用 python 的 `random.shuffle`。

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

走本 skill 上面的"上线顺序步骤 1"标准流程（**读 live cm → 外科式 patch → rollout restart**，⛔ 禁 apply 仓库 yaml）。**额外**做 secret 轮换 `kubectl patch secret carher-env-keys` 跟 198 同样。

⚠️ **rollout 前必查 provider key 漂移**（kuaihui 坑）：secret 里的值与 pod 正在跑的 env
可能不是同一把，一 rollout 会把别的线整体带炸。
`kubectl -n carher exec <现役pod> -c litellm -- env` 与 secret 解码值逐条 diff，漂移 = 0 才继续。

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
| `/v1/models` 看不到新模型，但 ConfigMap 里有 | 改 ConfigMap 不会自动重启 pod，`model_list` 是启动时读 | `kubectl -n carher rollout restart deployment/litellm-proxy`，判据是容器内 `/app/config.yaml` 的 sha256 |
| `kubectl exec ... curl ...: not found` | litellm-proxy 镜像没装 curl | 用 `python -c "import urllib.request"` 替代 |
| 401 Unauthorized hitting LiteLLM API | 用了 SKILL doc 里硬编码的旧 master key | 永远从 `kubectl get secret litellm-secrets` 取 |
| `kubectl set image deployment/X ...: unable to find container "manager"` | controller-runtime 模板默认 `manager`，但 carher 用 `operator`/`admin` | 先 `get deploy -o jsonpath='{...containers[*].name}'` 确认容器名 |
| spend 一直是 $0 | `model_info.id` 撞内置 model_prices 表 | `litellm_params` 和 `model_info` 双写 cost；id 用唯一前缀如 `wangsu/...` |
| 价格/cache 价写完返 200、`/model/info` 也读得到，但计费仍是默认值 | 只写进了 `model_info`；计费只读 `litellm_params` | 两个 block 都写 + `rollout restart` proxy + 逐行读回数 ok/bad（`scripts/litellm-astra-cache-price-patch.py --verify`） |
| cache 阶梯位填了 `0.0` 结果那段免费 | 字面零 = 免费，不是"未配置" | 用不到的阶梯位**不写**该字段 |
| 部分 carher-* key 一直调不到新模型 | 这些 key models 数组原本是 `[]`（无限制），脚本误把它们覆写为白名单 | 跳过 `models == []` 的 key，保持其无限制状态 |
| `kubectl apply k8s/base-config.yaml` 顺带推未授权改动 | 仓库 yaml 比 live ConfigMap 领先（有挂起的 fix） | 用 `kubectl patch --type=merge --patch-file=...` 单 key patch |
| `/api/ci/trigger-build` 不构建 admin | 那个 workflow 只 build `her/carher` | 走 `k8s-work-227` 手动 nerdctl 构建 |
| `kubectl exec deploy/litellm-proxy -- env MK=$MK python3 <<'PY' ... PY` 静默成功但 0 字节 stdout，0 个 key 被更新（"假成功"） | bash heredoc + python heredoc 多层 quoting，env / stdin 被 ssh / kubectl exec 的 wrapper 吞掉，python 根本没执行就 `exit 0` | 改用本地 `kubectl port-forward svc/litellm-proxy 14000:4000` + 本地 `python3 <<PY ... PY`；总跑完后**反查 LiteLLM `/spend/keys` 真实 allowlist 字段**确认，不要靠 exit code |
| aliyun litellm-proxy `rollout status` 报 `exceeded its progress deadline` | `terminationGracePeriodSeconds: 600s` + nodeAffinity 限 3 节点 + hostPort 4000 + 2 副本：新 pod 必须等老 pod 完全终止才能调度上同一节点；老 pod 走完整 600s grace 才被 SIGKILL | 不是失败，等就行（总 15-20 min）。判断：`kubectl get pods -l app=litellm-proxy` 双副本始终 ≥2 ready，service 不中断 |
| 网宿 cheliantianxia1 key 轮换后某些请求 401，但其他请求 200 | 三套环境（aliyun / 198 prod / 198 dev）共享同一把 `WANGSU_API_KEY`，只在一个 namespace 改了 secret 但其他没改 | 三套环境**同步**轮换 `kubectl patch secret carher-env-keys`（见上面"网宿 cheliantianxia1 同步"章节） |
| 不知道 `WANGSU_API_KEY` 在哪 / 改了 `litellm-secrets` 但不生效 | wangsu 系列 env 在 `carher-env-keys` 不在 `litellm-secrets`（litellm-proxy `envFrom` 同时挂两个） | 见上面"前置 → Provider env vars 的来源" |
| **所有 key 写入突然 403 `only available for LiteLLM Enterprise`** | CM 里被引入了含 `*` 的 `model_name` ⇒ `pro198` 变成 wildcard-route access group，`_check_model_access_group` 在 `premium_user=False` 下直接拒 | 把那条通配条目删掉。⛔ 用着 access group 就永远不许往这个 CM 引入通配 `model_name` |
| 新模型上完，`/v1/models` 有、master key 打得通，但 her 的 key 403 | 375 把 `carher-*` key 的 `models` **全部非空**，白名单里没这个名字 | 桥模型 ⇒ 打 `access_groups: ["pro198"]` 零 key 写入；非桥 ⇒ `litellm-198-key-allowlist.py --prefix carher-` |
| 允许清单脚本报 "0 keys planned"（读起来像"都已经有了"）| 默认 `--prefix` 是 `cursor-,claude-`，压根没扫 carher 家族 | 必须显式 `--prefix carher-` |
| 白名单里明明有这个模型名，key 打它照样 400 | 名字既不是真实 model_group、也没有 per-key alias 兜底 ⇒ 过了准入闸门后无法解析 | 清点"谁能用 X"别只数 `models`（会高估）；判据要打一发真流量 |
| 两行 SpendLogs 的 prompt/completion 完全相同但 spend 不同，怀疑定价配错 | `cached_tokens` 按 cache_read 计、`reasoning_tokens` 按 output 计但**不在 `completion_tokens` 列里** | 按 `未命中输入×in + cached×cache_read + 计费输出×out` 对账，能对到最后一位 |
| `timeout 30 kubectl ... \| grep X` 打出"零命中" | **mac 上没有 `timeout`**（GNU coreutils 才有）⇒ 管道左端啥也没产出，grep 读空 stdin，退出码还是 0 | `kubectl --request-timeout=30s`；随机抽样用 python `random.shuffle` 不用 `shuf`（同样不存在） |
| `kubectl get cm X -o jsonpath='{.data.openclaw\.json}'` 返空串（同命令对别的 cm 好用）| jsonpath 对某些对象静默返空 | 换 `-o go-template='{{index .data "openclaw.json"}}'` |
| `/key/list?size=2000` → 422 | `size` 上限是 100 | 分页，或用 `/spend/keys` |
| 改 198 key 的 `key_alias` → 400 | 198 的 `key_alias` **全局唯一**，想用的名字已被一把无关的 key 占了 | 先 `/key/info` 确认；换个名字，⛔ 别去动那把无关的 key |
| 撤了模型权限，验收 SQL 说"0 残留"，过一阵又能用了 | 验收把 `blocked` 的 key 过滤掉了；blocked 只挡认证、不清白名单，解封即复活 | 写入带 `--include-blocked`；验收按 blocked **分列**不过滤（`litellm-198-key-allowlist-verify.sh` 已内置） |
