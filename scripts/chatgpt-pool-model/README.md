# 给 198 池加一个 chatgpt-backend 模型（codex-auto-review 等）并接 Codex 客户端

> ## 🔴 2026-09-23：本篇三处已被实测推翻，先读这段
>
> 新的权威流程是 skill **`chatgpt-pool-codex-slug-onboard`**（repo `.claude/skills/`）
> + 两个脚本 `scripts/chatgpt-pool-codex-slug-survey.py` /
> `scripts/chatgpt-pool-add-codex-model.py`。本篇保留是因为 `codex-auto-review`
> 那条「专用 key + Codex CLI 客户端配置」的内容仍然有效（§3）。
>
> **① Step 2 说「198 prod 的 `router_settings` 在 CM `litellm-config` 里、DB 无
> router_settings 行、`/config/update` 写 DB 不生效」——已作废。** 现在
> `STORE_MODEL_IN_DB=True`，权威源是 Postgres `LiteLLM_Config` 里
> `param_name='router_settings'` 那一行，改 CM 是 no-op；它每 ~30s 热加载，**不需要 rollout**。
> ⛔ 而 `/config/update` 不是"不生效"，是**危险**：传部分 `router_settings` 会静默抹掉
> `model_group_alias`（24 → `{}`），下次重启才引爆。只准
> `jsonb_set(param_value,'{fallbacks}',...)`。
> ⛔ 顺带：本篇 Step 2 让你 `apply` CM 后 `rollout restart deploy/litellm-proxy` ——
> **`litellm-proxy` 禁 `apply`**（仓库 manifest 陈旧，会回退 image + 内嵌 CM），
> 且生产车道现在不叫 `litellm-proxy`（判据 = Svc `litellm-proxy-nodeport` 的 selector，
> 标签在 **Pod** 上，`get deploy -l` 返回空是假红）。
>
> **② Step 4 / `fanout-pool-model.sh` 会对每个 acct `rollout restart`。**
> 重启在服务的号**可能永久打死它且回退救不回**（acct-237）。
> 只要 slug 已经在 acct pod 的 `config.yaml` 里，**建组根本不需要重启任何 pod**——
> 用 `chatgpt-pool-add-codex-model.py`（只写外层 router 行，一个 pod 都不碰）。
> 那份脚本还会先 `/model/delete` 再 `/model/new`；新脚本幂等跳过已存在的 id，不删。
>
> **③ Step 5 那条裸 SQL `array_append` 只写白名单、不写 alias。**
> 白名单只发**准入**不发路由：裸名不是真实组又没有全局 alias 时，只加白名单**必然 400**。
> 用 `scripts/litellm-198-key-allowlist.py` 在**同一条命令**里写 `--add-model` + `--alias`
> （还带金丝雀、backup、逐 key 回读）。
>
> **④ 另有一条相邻的旧结论也被推翻**（在 `~/.claude/skills/chatgpt-pool-model-variant` 里，
> 那份**不在本仓 git 里**，我改不了它，只能在这里标注）：
> 「`db_model:True` + `mode:responses` 会触发 `No connected db.`，必须用 CM 条目」。
> 实测 `/model/info` 上 `chatgpt-gpt-6-sol`/`-luna`/`-5.6-sol`/`-astra` 共 **188 行全部
> `db_model=True` + `mode=responses` 且都在服务**。真因是漏了 `litellm_params.api_key`。

> 面向下次直接执行的操作手册。以 `codex-auto-review`（Codex Guardian 审查专用模型）为样板，
> 同法可加任何 Codex 后端 slug（gpt-5.7、下一代 codex 变体等）。
>
> 配套脚本：`fanout-pool-model.sh`（同目录，全池铺开 = rollout + 注册）。
> 深度背景/踩坑：memory `[[reference_codex_auto_review_model_oauth_served]]`。
> 面向 carher bot 的产品级变体上线（config_gen 双写 + operator）走另一条：skill `chatgpt-pool-model-variant`
> （⚠️ **只存在于 `~/.claude/skills/`，不在本仓 git 里** —— 能加载，但不受本仓版本控制、
> review 不到、换机器就没有）。

---

## 0. 这是什么模型

`codex-auto-review` = OpenAI Codex 内部**代码审查/风险评估专用小模型**（Guardian/Smart-Approvals 用；
2026-04 起替换硬编码 gpt-5.4）。跟 `gpt-5.5`/`gpt-5.6-sol` **同一条 Codex OAuth 订阅通道**
（只走 `/v1/responses`，plain slug 透传别加后缀），不是 metered platform API。便宜低延迟，
**不是通用聊天模型**——单独成组，别混进 gpt-5.5 主组让日常流量路由到它。

先确认它在 acct 的 catalog 里（token 有效的号）：
```bash
# 在某个 acct pod 内
python3 -c "import json,urllib.request as u;d=json.load(open('/chatgpt-auth/auth.json'));\
at=d.get('access_token');acc=d.get('account_id','');\
r=u.urlopen(u.Request('https://chatgpt.com/backend-api/codex/models?client_version=1.0.0',\
headers={'Authorization':'Bearer '+at,'chatgpt-account-id':acc,'originator':'pi'}));\
print([m['slug'] for m in json.load(r)['models']])"
# 期望含 codex-auto-review；client_version 太低会返回空 []
```

---

## 1. 上线四步（改代码前先读全）

**SLUG=codex-auto-review，MODEL_NAME=chatgpt-codex-auto-review，ALIAS=codex-auto-review。**

### Step 1 — acct pod 共享 CM `chatgpt-pool-config` 加模型
所有 acct pod 共享这一份 config，vanilla litellm **不热载**，改完靠 rollout 生效（Step 4 做）。
```python
# 在 198 kube host 上跑（surgical 插入，别 yaml round-trip 重排整个文件）
import subprocess,os
NS="litellm-product";CM="chatgpt-pool-config";OUT=os.path.expanduser("~/pool-cm.yaml")
cur=subprocess.check_output(["kubectl","-n",NS,"get","cm",CM,"-o","jsonpath={.data.config\\.yaml}"],text=True)
NEW="- model_name: chatgpt-codex-auto-review\n  litellm_params:\n    model: chatgpt/codex-auto-review\n  model_info:\n    mode: responses\n"
i=cur.index("litellm_settings:");open(OUT,"w").write(cur[:i]+NEW+cur[i:])
subprocess.run(f"kubectl -n {NS} create cm {CM} --from-file=config.yaml={OUT} --dry-run=client -o yaml | kubectl -n {NS} apply -f -",shell=True)
```

### Step 2 — litellm CM `litellm-config` 加 group_alias
⚠️ **198 prod 的 `router_settings` 在 CM `litellm-config` 里、DB `LiteLLM_Config` 无 router_settings 行**
→ 走 `/config/update` 写 DB **不生效**（实测写完仍旧值）。正解改 CM + rollout litellm-proxy。
别 clobber 现有别名（surgical 插一行即可）：
```bash
# model_group_alias 块下加一行（4 空格缩进）：
#     codex-auto-review: chatgpt-codex-auto-review
# 然后 kubectl create cm litellm-config --from-file=... --dry-run|apply
kubectl -n litellm-product rollout restart deploy/litellm-proxy
kubectl -n litellm-product rollout status deploy/litellm-proxy --timeout=180s
```
> alias 改写发生在 ACL 之前 → key allowlist 只需含**改写前**名 `codex-auto-review`。

### Step 3 — 固化进 `quota-rebalance.py`
运行副本在 **188 `/home/cltx/quota-rebalance.py`**（每 5min cron），加：
```python
CHATGPT_MODELS_REVIEW = [
    {"model_name": "chatgpt-codex-auto-review", "litellm_model": "openai/chatgpt-codex-auto-review"},
]
def models_for(acct):
    return CHATGPT_MODELS + CHATGPT_MODELS_56 + CHATGPT_MODELS_REVIEW
```
**不加这步 → pause/resume 一轮后 entry 被静默摘**（同 5.6 教训）。repo 副本 `scripts/quota-rebalance.py`
已同步，但两边已分叉——188 是运行真相，改要改 188。

### Step 4 — 全池 rollout + 注册（用脚本）
```bash
# 传脚本到 198 用 base64 走参数（jms stdin 会被隧道抖动丢），写 $HOME 别写 /tmp（root 残留占位）
B64=$(base64 < scripts/chatgpt-pool-model/fanout-pool-model.sh | tr -d '\n')
jms ssh AIYJY-litellm "echo $B64 | base64 -d > \$HOME/fanout-pool-model.sh && \
  setsid nohup env SLUG=codex-auto-review bash \$HOME/fanout-pool-model.sh > \$HOME/fanout.log 2>&1 </dev/null & disown"
# 监控（读本地/远端日志，别穿隧道盯屏）
jms ssh AIYJY-litellm 'tail -3 $HOME/fanout.log; grep -c "ok register" $HOME/fanout.log'
```
脚本对每个 replicas>=1 的 acct **串行** rollout + 注册。⚠️必须串行：acct deploy strategy=**Recreate**
（RWO PVC 单挂载）+ ~1GB/pod，并发 40+ 会 2x 内存 OOM 节点；串行任意时刻只 1 号下线（群里还有 46 个兜）。

### Step 5 — 给所有 cursor key 授权（模型按产品名被客户端直接调用时才需要）
`codex-auto-review` 会被 cursor/codex 客户端**按产品名直接调用**，而 key 的 allowlist（`LiteLLM_VerificationToken.models`）
是 `models` 数组白名单（1.89 严格）。alias 改写在 ACL 之前 → allowlist 要含**改写前名** `codex-auto-review`。
**镜像已有产品名（如 `gpt-5.6-terra`）的访问面**，一条原子 SQL 批量补齐（只碰有 terra 的 key，抽样确认全是 `cursor-*`）：
```sql
UPDATE "LiteLLM_VerificationToken"
SET models = array_append(models, 'codex-auto-review')
WHERE 'gpt-5.6-terra' = ANY(models) AND NOT ('codex-auto-review' = ANY(models));
```
2026-07-23 实跑 `UPDATE 474`（475 把 terra key 全覆盖；另 131 把空 allowlist=全模型的 key 本就可用无需动）。
改完 rollout litellm-proxy 刷新内存 key 缓存（授权类变更即使有缓存延迟也自愈，非安全问题）。
> SQL 走 `kubectl cp` 进 `litellm-db-0` 执行（见 [[feedback_litellm_db_sql_via_kubectl_cp]]）；DB 用户/库都是 `litellm`。


---

## 2. 验证在循环池里（不只是注册）
```bash
# 连打几发看 x-litellm-model-id 是否落不同 acct（轮询）
for i in 1 2 3 4 5 6; do
  curl -s -D- -o /dev/null -X POST https://cc.auto-link.com.cn/pro/v1/responses \
    -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
    -d '{"model":"codex-auto-review","input":[{"role":"user","content":[{"type":"input_text","text":"hi"}]}],"stream":true}' \
    | grep -i x-litellm-model-id
done
# 期望每次 chatgpt-acct-<不同N>-codex-auto-review = HTTP 200
```
> `model_group/info` 显示 `deployments:1` 是按 provider 名 `openai` 聚合的显示 quirk，不是真实端点数；
> 以路由头 `x-litellm-model-id` 每次落不同 acct 为准。

---

## 3. Codex CLI 客户端怎么用

Codex CLI 打这个池必须用自定义 provider（`~/.codex/config.toml`）：
```toml
model = "gpt-5.5"                 # 主模型
model_provider = "carher_dev"
[model_providers.carher_dev]
base_url = "https://cc.auto-link.com.cn/pro/v1"
env_key = "CARHER_DEV_KEY"
wire_api = "responses"           # 必须；Codex 只发 /v1/responses
supports_websockets = false      # ⚠️ 必须 false！
```
⚠️ **`supports_websockets` 不设 false**：Codex 0.145 默认先试 `wss://.../v1/responses` → LiteLLM **405**
→ 回退 HTTPS 时**丢 key → 401**。

**显式用审查模型**（`codex review` 命令默认用配置的 `model`，*不是* codex-auto-review）：
```bash
codex review -c model=codex-auto-review --uncommitted
codex review -c model=codex-auto-review --base main
```
另一入口：Guardian 审批（`approvals_reviewer=guardian_subagent`）后台自动调 codex-auto-review，与 `codex review` 命令是两回事。

key 需含 `codex-auto-review`（改写前名）+ 主模型 in allowlist。已建专用 key `codex-client-pool`。

---

## 4. 踩坑速查

| 现象 | 根因 / 修法 |
|---|---|
| acct 重启后 pod 0/1 卡住，日志 `refresh token failed 401` | 该号 refresh token 死（僵尸号），脚本 skip register 是对的；交 quota-rebalance pause，或重 onboard |
| smoke 该号 500→400 | entry 挂到没有该模型/未 ready 的 pod（Step 4 timeout 门禁防这个） |
| `/config/update` 改 alias 不生效 | 198 router_settings 在 CM 不在 DB，改 CM + rollout |
| 传脚本到 198 变空文件 | jms stdin 丢 / `/tmp` root 残留占位；用 base64 参数 + 写 `$HOME` |
| codex CLI 405 / 401 | provider 缺 `wire_api="responses"` + `supports_websockets=false` |
| `codex review` 没用审查模型 | 它用配置的 `model`；`-c model=codex-auto-review` 强制 |
