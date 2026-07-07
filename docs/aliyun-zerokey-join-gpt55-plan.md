# zerokey 池并入 gpt-5.5 轮询 — 让 279 个 her 自动生效

## Context

阿里云已建好 8 个原生 zerokey 网页额度成员(`zerokey-pool` 组:aliyun-69/71/72/73/74/75/77/78),全部 serve 正常、直连 200。但**目前没有任何 her 在走** —— 279 个 her 全部发 `gpt-5.5`,经 `model_group_alias: gpt-5.5→chatgpt-gpt-5.5` 只轮询 9 个 codex acct。zerokey 池是"建好了但没接入"。

用户决策:**并入 gpt-5.5 一起轮询** —— 让 8 个 zerokey 成员加入 `chatgpt-gpt-5.5` 组,her 发 gpt-5.5 时 9 codex + 8 zerokey = 17 成员一起 least-busy 轮询,全局一处改动、所有 her 自动生效、产能翻倍。

## 摸底结论(全部证据验证,非假设)

1. **her 侧零改动即可生效**:her openclaw 配置 `chatgpt-gpt-5.5` = `"api": "openai-completions"`(chat completions),发到 `litellm-proxy.carher.svc:4000`。model_group_alias 已把 `gpt-5.5→chatgpt-gpt-5.5`。只要让 zerokey 成员进这个组,her 完全不用动。
2. **协议兼容(坑已排除)**:her→litellm 是 chat completions;litellm 按**每个成员各自 mode** 转发——acct 成员 `mode:responses` 转 responses 发 acct pod,zerokey 成员(不标 mode)直接 chat completions 发 zerokey serve(只吃 chat,不支持 responses,已实测 /v1/responses=404)。混组共存已被 acct-69 注册进 zerokey-pool 后 her 经池 200 验证。
3. **CM/DB 混合是已知雷但可控**:acct 9 成员 = CM 定义(db=False),zerokey 8 成员 = DB 定义(db=True)。同组名混 CM+DB 成员 → memory 记过"同 slug 路由抖动"。**但**:两边 `model_info.id` 各自唯一(`chatgpt-acct-N/chatgpt-gpt-5.5` vs `zerokey-pool-aliyun-N`),不撞;litellm 按 id 去重,实测能合并列 17 成员。风险点是升级/rollout 时 DB 成员的持久性,需验证。
4. **deployment_affinity 会影响分配**:`optional_pre_call_checks: [deployment_affinity]` ttl 600s,同签名请求钉单成员。真实 her 会话签名各异 → 自然分散到 17 成员;但同一 her 的连续对话会黏在同一成员(这对 prompt cache 命中其实是好事)。
5. **fallback 已就位**:`chatgpt-gpt-5.5 → deepseek-v4-flash`(撞限兜底),并入后 17 成员全挂才 fallback,更难触发。

## 方案(推荐:DB 重注册,零改 CM,零 rollout)

把 8 个 zerokey 成员从 `model_name: zerokey-pool` 改注册为 `model_name: chatgpt-gpt-5.5`,DB 热加,**不动 CM、不 rollout litellm**(STORE_MODEL_IN_DB 热生效)。

### 为什么选 DB 重注册而非改 CM
- **改 CM 要 rollout litellm**(2 副本滚动,cold start 90-120s,虽零中断但动线上主 proxy);DB `/model/new` 热加不重启,风险更低。
- CM 是 acct 池的 source of truth(`aliyun-batch-add-accts.sh` 维护),把 zerokey 混进 CM 会让两套 onboarding 流程纠缠。DB 侧保持 zerokey 独立管理,edge 更清晰。
- 回滚零成本:`/model/delete` 8 个 id 秒级摘除,her 自动回落纯 acct 池。

### 具体步骤
1. **改 8 个成员的 model_name**:对 aliyun-69/71/72/73/74/75/77/78,`/model/delete` 旧的(model_name=zerokey-pool)→ `/model/new` 新的(model_name=**chatgpt-gpt-5.5**,同 api_base/id/rpm=30)。保留 `zerokey-pool` 组名可选(留空或删)。
2. **验证组合并**:`/v1/model/info` 应见 `chatgpt-gpt-5.5` = 17 成员(9 CM + 8 DB)。
3. **灰度观察**:先只改 2-3 个 zerokey 成员进组(如 69/71/72),用真实 her 流量看 SpendLogs 是否有流量落到 `zerokey-pool-aliyun-*`、serve 是否稳、有无 5xx;稳一段再把剩余 5 个并入。
4. **监控生效**:`/spend/logs` 按 model_id 统计,确认 zerokey 成员开始分担流量;`kubectl logs zerokey-serve-N` 看真实 her 对话进来。

## 待验证/风险(动手时逐一确认)
- **DB 成员在 litellm rollout 后是否还在**(STORE_MODEL_IN_DB 应持久,但要实测一次 rollout 不丢)。
- **同组 responses+chat 混合成员,litellm 路由是否真的按成员 mode 分发**(acct-69 经 zerokey-pool 已验证;并入 chatgpt-gpt-5.5 后再验一次真 her 流量)。
- **计费**:zerokey 成员当前 input/output_cost=0(网页额度不计 token 费),混进 gpt-5.5 组后 SpendLogs 里这部分显示 0 成本,符合预期(web 额度本就不按 token 算)。
- **acct-70 仍挂起**(不在 8 个之列,不影响)。

## 后台查看生效的方法(交付给用户)
无 Web UI(只 ClusterIP + STORE_MODEL_IN_DB)。查生效:
- **成员列表**:`/v1/model/info` 过滤 model_name=chatgpt-gpt-5.5 → 应 17 个。
- **实际流量**:`/spend/logs` 或 litellm-db SpendLogs 表按 model(带 `zerokey-pool-aliyun-*` id 前缀)统计。
- **serve 侧**:`kubectl logs -n carher zerokey-serve-N` 看真实对话请求。
- (可选)后续可给 litellm 开 Web UI(加 UI env + Ingress),但需单独评估。

## 关键文件
- 无需改 repo 代码(纯 DB API 操作);可加一个 `scripts/aliyun-zerokey-join-pool.py`(delete+new 重注册 + --rollback)固化操作,类比 `prod-aliyun-her-zerokey.py`。
- 参照:`scripts/prod-aliyun-her-zerokey.py`(register 配方)、CM `chatgpt-gpt-5.5` 定义(k8s/litellm-proxy.yaml:749+)。

## Verify(端到端)
1. 改后 `/v1/model/info` chatgpt-gpt-5.5 = 17 成员。
2. flush affinity + 多签名请求打 gpt-5.5,`x-litellm-model-id` 应出现 zerokey-pool-aliyun-* 命中。
3. 真实 her(挑 1-2 个)发消息,SpendLogs 见 zerokey 成员流量,回复正常。
4. 线上安全:9 codex acct 全 Running、codex 路径仍 401(健康)、her 无 429 上升。
