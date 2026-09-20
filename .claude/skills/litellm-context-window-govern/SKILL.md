---
name: litellm-context-window-govern
description: >-
  给 LiteLLM 上的模型「量出真实上下文窗口、把它落成入口闸门」的完整流程。
  适用场景：要新设/修改某模型的上下文窗口；长会话被 400 ContextWindowExceeded 或被上游
  500 拒；怀疑宣称窗口（官方页/第三方目录/内置价格表）与实际能力不符；新模型上线要定窗口。
  覆盖：max_input_tokens 的真实语义、enable_pre_call_checks 开关、承载面发现集
  （prod CM / canary CM / acct CM / 仓库 manifest / her 侧 config_gen）、阶梯二分测真实窗口、
  外科式改 CM 脚本模板、阿里云 rollout 的慢节奏、逐副本回归。
  典型产出：把「宣称 1,000,000、实测 922,000」这类偏差收敛掉。
metadata:
  requires:
    bins: ["kubectl", "ssh", "python3"]
  related_skills:
    - litellm-ops
    - add-litellm-model
    - litellm-chatgpt-provider-prefix-fix
    - carher-k8s-zero-downtime-rollout
  related_memories:
    - feedback_aliyun_litellm_max_input_tokens_is_metadata_not_gate
    - feedback_synthetic_red_is_as_untrusted_as_synthetic_green
    - reference_chatgpt_sub_to_api_two_routes
    - feedback_shared_cm_needs_all_lane_rollout_pool_hides_drift
    - feedback_manifest_prod_drift_apply_overwrites
---

# LiteLLM 上下文窗口治理：量真实值 → 落成闸门

## 0. 先搞清这个字段到底管什么（错一次全盘错）

| 事实 | 判据 |
|---|---|
| `model_info.max_input_tokens` 只管**输入**，不是总窗口 | 报错原文 `Max Input Tokens=N, Got=M`，M 是 prompt_tokens |
| **没有 `router_settings.enable_pre_call_checks: true` 时它只是元数据**，一个请求都不拦 | 设小值后发超限请求，若被转发到上游、由上游报错 ⇒ 闸门是关的 |
| 不配这个字段时，LiteLLM 会按 `litellm_params.model` 的 slug 去**内置价格表**静默继承一个值 | `litellm.get_model_info('<slug>')['max_input_tokens']` |
| **DB/CM 里是 `<none>` ≠ 继承到了合理值** | 198 实测：`chatgpt-gpt-5.6-*` 132 行 DB 全 `<none>`，运行时却读到 **10,000,000**；而同样 `<none>` 的 astra 6 行读到 922,000。**只能以 `/model/info` 为判据** |
| `max_input_tokens: 10000000` 不是能力声明，是**故意写到够不着 = 把闸门关掉** | 同 pod 同载荷 A/B：922000 那条 400/1.6s 本地拒；10000000 那条不拒、出网超时 |
| 官方文档的 `context window` 是**总窗口**（输入+输出），不能直接填进 `max_input_tokens` | 一般 `max_input_tokens ≈ context_window − max_output_tokens` |

**⚠️ 读配置的假判据**：`python3 -c "import litellm.proxy.proxy_server as ps; ps.llm_router"` 在**新起的进程里恒为 None**
（不是在跑的那个 app）。想知道闸门开没开，只有两条合法路：读 pod 内 `/app/config.yaml`，或行为实测。

## 1. 第 0 步永远是阳性对照

新写的探针、久没用的探针，**先在答案已知的样本上复现已知答案**。这条流程里两个必做的对照：

1. **量具对照**：拿一个确定能过的尺寸（比如已知窗口的 90%）先打一发，确认 200 + `prompt_tokens` 符合预期。
2. **参数对照**：确认你打算用来做判据的参数**真的端到端生效**。
   - 血的教训：ChatGPT acct 这条路（`chatgpt.com/backend-api/codex`）**`max_tokens` 会被删掉**。
     对照做法：`max_tokens=16` + "写 500 词"，若 `completion_tokens=3853 / finish_reason=stop` ⇒ 参数没生效。
   - 此时所有"请求 12.8 万输出 → HTTP 200"式的结论**全是假绿**，参数压根没传上去，不构成任何判据。
   - 见 [[reference_chatgpt_sub_to_api_two_routes]]：codex 端点对 `max_output_tokens` 是**必删**，网页端点是**收下但忽略**。

## 2. 承载面发现集（漏一层就是白改）

同一个"窗口"在 CarHer 这套里最多躺在 5 个地方，动手前逐个确认在不在你的改动范围内：

| 层 | 位置 | 是闸门吗 |
|---|---|---|
| LiteLLM 入口（真正生效的那层） | 阿里云 ns `carher` CM `litellm-config` / 198 是 `LiteLLM_ProxyModelTable` | 开了 pre_call_checks 就是 |
| LiteLLM canary | CM `litellm-config-canary` | 同上，**必须单独确认改不改** |
| acct 层（上游池自己的 litellm） | CM `chatgpt-pool-config`（7 个 `chatgpt-acct-*` 共用） | **不是**——那层没有 router_settings，纯元数据 |
| 仓库 manifest | `k8s/litellm-proxy.yaml` | 不是，且**永远不许 apply**（陈旧会回退 image + 内嵌 CM） |
| her 客户端 | `backend/config_gen.py` 的 `contextWindow` | 不是，另一条承载面，通常由他人控制 |

```bash
# 发现集清点模板
kubectl -n carher get cm litellm-config        -o jsonpath='{.data.config\.yaml}' | grep -c '<model-slug>'
kubectl -n carher get cm litellm-config-canary -o jsonpath='{.data.config\.yaml}' | grep -c '<model-slug>'
kubectl -n carher get cm chatgpt-pool-config   -o jsonpath='{.data.config\.yaml}' | grep -n -A6 '<model-slug>'
grep -rn 'contextWindow' backend/config_gen.py
```

## 3. 量真实窗口：直连单腿 + 阶梯 + 二分

必须**直连某一个 acct pod**（`http://chatgpt-acct-<n>.carher.svc:4000`），不能走 router
—— router 每次挑腿不同，边界会被不同腿的差异污染。

```bash
# 在 litellm-proxy pod 里跑（有 CHATGPT_POOL_KEY），或在 acct pod 里跑（用 self + LITELLM_MASTER_KEY）
python3 scripts/ladder_direct.py 226 chatgpt-gpt-5.6-luna 900000,920000,950000
python3 scripts/ladder_direct.py self chatgpt-gpt-5.6-luna 921000,922000,923000
```

- 填充物用 `"hello "×N`，1 token/词，`prompt_tokens ≈ N + 系统开销`（acct 侧约 1,638）。
- 边界要**可重复**：同一个尺寸打两次，一次过一次拒 = 尺子坏了，不是窗口。
- 失败原文认这句：`ChatgptException - Your input exceeds the context window of this model`。
- 填充物可能踩内容策略（阿里云 astra 上 `"hello"×N` 和随机自然句**都**被判 `ContentPolicyViolationError`）
  —— 换填充物后现象不变，说明**与填充物无关**，别把它当根因。

**2026-09-05 实测结果（可当已知答案做阳性对照）**：
gpt-5.6-sol / terra / luna 与 gpt-6-astra 的真实输入上限**都是 922,000**
（`prompt_tokens` 921,638 恒过、922,638 恒拒），= 官方 1,050,000 总窗 − 128,000 输出预留，
与 litellm 内置表 `922000` 吻合。**官方从没发布过 922,000 这个数**，别说成官方口径。

## 4. 改：外科式，脚本自带门

用 `scripts/patch_window.py`（行级替换 / 行级新增两种模式）。它强制做四件事，缺一不改：

1. **备份**当前 CM 到 `/tmp/<cm>-<ts>.yaml` 并打印 sha256
2. 只允许"同一行右值替换"或"在指定行下新增"，**断言旧值等于预期**（旧值不对 = 你的前提错了，立刻停）
3. **结构门**：改后重新 YAML 解析，`model_list` 条数不变，除目标字段外每条 deployment 逐字段相等
4. `kubectl patch --type=merge --patch-file` 后**回读比对**，打印各值计数

```bash
python3 scripts/patch_window.py            # dry-run，先看 diff +N/-N 和结构门
python3 scripts/patch_window.py --apply
```

> 禁 `kubectl apply -f k8s/litellm-proxy.yaml` —— 仓库 manifest 与现网漂移严重，apply 会连 image
> 和内嵌 CM 一起回退。仓库文件只同步改、只作记录。

## 5. rollout：阿里云这套很慢，别误判成失败

`litellm-proxy` 是 2 副本 + hostPort + 节点亲和，`maxSurge:0 / maxUnavailable:1`：

- 全过程 **12~20 分钟**，中途必有一个 pod 长时间 `Init:0/2` / `Pending`
- `kubectl rollout status` 经常先报 **`exceeded its progress deadline`** —— 这是进度期限，不是失败，
  继续 `get po` 轮询到两个新 pod 都 `1/1 Running` 为止
- acct 侧那层若只改了元数据（不是闸门），**不值得为它滚 7 条单副本的池腿**；
  写进 CM、留待下次自然重启即可，但要**明确告知"现在还没生效"**
  —— 用户要求"全部生效"时，用 `scripts/pace_restart_accts.sh`：一次只滚一个、
  节点 Disk/MemoryPressure + load 超阈值就**停下（不是跳过）**、每个滚完在容器内验
  `grep -c '<needle>' /app/config.yaml == EXPECT`。198 上 63 条里有若干长期 `0/1`，
  这些会等满 `--timeout=5m`，整轮 ~1.5h 属正常，别当卡死。

**paused Deployment 拦路**（`can't restart paused deployment`）：不许直接 resume 了就走，
resume 会一次性放出攒下的漂移（见 [[feedback_paused_deployment_absorbs_set_image_silently]]）。
先出数据再动手：

```bash
# 判据：deployment.spec.template 与「活跃 RS 的 template」逐字段比对（RS 那份多一个
# pod-template-hash 标签属正常）。全等 ⇒ 没攒任何漂移 ⇒ resume 安全
python3 scripts/tmpl_drift.py            # 输出 "✅ 模板逐字段相等"
```

全等后按 **resume → rollout restart → 验 needle → 立刻 `rollout pause` 恢复原状**
逐个做（paused 是别人有意设的状态，不许留在 resumed 上走人）。
2026-09-06 阿里云 122/124/125 就是这么过的，三个都 `needle=4` 且 `paused=true` 已复原。

### 198 是 DB 模式：改完**可能不用重启**

`LiteLLM_ProxyModelTable` 的 `model_info` 改完，proxy 会靠 DB 轮询自行收敛 ——
2026-09-06 实测 4 副本在 **90 秒内**全部从 1,050,000 变成 922,000，**一次 rollout 都没做**
（对宿主机零冲击）。所以顺序是：**先只读 `/model/info` 逐副本轮询等收敛，收敛不了再滚**。

```bash
# 审计：任何 zerokey 支撑的 deployment 若 max_input_tokens 为空 → 退出码 1
# （空值会被 LiteLLM 从内置价格表按 slug 静默继承成 1,050,000）
scripts/litellm-198-max-input-tokens-audit.sh              # 默认 zerokey-cursor%
scripts/litellm-198-max-input-tokens-audit.sh 'zerokey-%'  # 自定义 id 前缀
```

> ⛔ 这里以前写的是 `scripts/astra_modelinfo.sh` —— **那个文件从来不存在**
> （git 全历史 0 次），照着抄会直接 `No such file`。2026-09-20 改指到真实脚本。
> ⛔ 另外：`/model_group/info` 返回的 cap 是**组内最大值，不是闸门**，别拿它当判据。

DB 侧改动同样要外科式：`copy (...) to stdout` 备份目标行 + 断言行数、
`update ... where <字段>='<旧值>'` 带旧值条件（补新键时条件写 `is null`）、`UPDATE N` 与期望条数对上、回读，
并**显式回读证明不该动的行没动**（198 上 `cursor-*` 那 26 行的 `10000000` 是有意的，
碰它就是劈错路由，见 [[feedback_proxymodeltable_write_reroutes_deployment_needs_restart_regress]]）。

```bash
# 参数化版（带上面五道门，条件是 is null ⇒ 重跑幂等、UPDATE 0）
TARGETS="'chatgpt-gpt-5.6-sol','chatgpt-gpt-5.6-terra','chatgpt-gpt-5.6-luna'" EXPECT=132 NEW=922000 \
  bash scripts/db_patch_window_generic.sh          # dry-run
```

2026-09-06 用它给 198 的 5.6 三档 132 行补上 922,000：`UPDATE 132`，**100 秒内 4 副本自行收敛，零 rollout**；
双向回归 930k→400（日志原文 `Max Input Tokens=922000, Got=930008`）、920k→200（`prompt_tokens=921,631`）。

## 6. 回归：逐副本、真 key、带证伪腿

用 `scripts/regress_window.py <label>`，在**每一个** proxy pod 里各跑一遍（共用 CM 的多副本必须全查，
只查一个 = 另一个可能在跑旧配置）：

```bash
for P in $(kubectl -n carher get po -l app=litellm-proxy --no-headers -o name | cut -d/ -f2); do
  kubectl -n carher cp scripts/regress_window.py $P:/tmp/regress_window.py
  kubectl -n carher exec $P -- python3 /tmp/regress_window.py $P
done
```

必须全绿的五项：

1. `/model/info` 里目标行的 `max_input_tokens` 分布 = 期望值 × 期望条数（两副本一致）
2. **临时 scoped key**（不是 master key）打每个目标模型 → 200；`finally` 里删 key
3. **闸门放行腿**：略低于阈值（如 920,637）→ 200
4. **闸门拦截腿**：略高于阈值（如 923,000）→ **400 `ContextWindowExceededError`，< 1s，不出网**
   （只有 3 没有 4 = 没证明闸门在工作）
   - ⚠ **临时 key 的 `max_budget` 要够**：92 万那发放行腿在 198 上就吃掉 ~$12，
     $5 的 key 会让紧随其后的拦截腿返 **429**（"今日 key 额度已用完"）——那是判据被顶掉，
     不是闸门。要么把拦截腿排在放行腿**前面**，要么预算给到 200。
   - ⚠ **198 有报错脱敏**：前台只看得到 `API 异常 (req: xxxxxxxx)`，光凭 400 不能说是窗口闸门。
     必须去 pod 日志捞脱敏前原文：
     `kubectl -n litellm-product logs <pod> --since=10m | grep -E '<reqid>|Max Input Tokens'`
     认这句才算数：`Max Input Tokens=922000, Got=923013`。
5. 邻居模型冒烟（如 `chatgpt-gpt-5.5` / `claude-opus-4-7` / `wangsu-deepseek-v4-flash`）→ 200，
   证明没误伤别的行；**改前也测一遍存基线**，否则分不清"本来就坏"和"我搞坏的"

## 7. 开闸前的爆炸半径审计（第一次开 pre_call_checks 时必做）

开关是全局的。开之前把**所有** deployment 的生效值（含静默继承的）列出来，找有没有
「闸门值 < 真实能力」的行 —— 那种行一开闸就是误杀。

```bash
python3 scripts/precall_audit.py   # 在 proxy pod 内跑，读 /app/config.yaml + 内置表
```

2026-09-05 阿里云审计结论：138 条里最小的是 haiku 200,000 / glm-5 202,752 / gpt-5.3-codex 272,000，
都等于各自真实窗口，无误杀风险；另有 42 条算不出窗口 ⇒ 不受闸门约束。

## 8. 决策口径：填官方数还是填实测数

- 官方页写的是**总窗口**；`max_input_tokens` 填总窗口 ⇒ 闸门形同虚设，
  超出真实能力的请求照样出网、浪费一次上游调用再报错。
- 填**实测输入上限**（= 总窗 − 输出预留）⇒ 超限请求本地 0.7s 拒掉，不出网。
- 两者不冲突：实测值和官方值是自洽的，只是分母不同。**把差别讲清楚让用户选**，
  别自己把"官方数"和"实测数"混成一个说法。

## 常见坑速查

- 探针用 master key 而不是 scoped key ⇒ 没走用户真实的 key 路径，绿了也不算
- `/model/info` 读到 `None` ≠ 无限制，是"未声明、将从内置表静默继承"
- diff 的 `+` 行带前缀，断言写 `line.strip() == "..."` 会必假；要 `line[1:].strip()`
- 共用 CM 的多个消费者（7 个 acct 共用一份）：改一份影响全部，**逐个确认谁受影响**
- acct pod 里没有 `curl`（exit 127），探针用 python `urllib`
- 引号很多的 `kubectl exec ... python3 -c` 会报 `Operation not permitted`，改成上传脚本文件执行
- **`/tmp/<固定名>` 会撞别人的文件**（`Permission denied` / 跑到陈旧脚本）：用 `/tmp/lgx-$(date +%s)/`，
  且 `kubectl cp` 前要先 `exec -- mkdir -p` 那个目录，否则 `tar: can't change directory`

## 10. 在 pod 里 grep 的三个假阴性（每次都要带阳性对照）

在容器里找「某个字面量到底写在哪」时，下面三种写法会**静默返回 0 命中**，让你得出"哪儿都没有"的错误结论：

| 坏写法 | 为什么假阴性 |
|---|---|
| `ssh host "kubectl exec $P -- bash -lc \"grep -r X \$L\""` | `\$L` 是在**容器内**展开的，容器里没这个变量 ⇒ 变空串 ⇒ grep 去读 stdin |
| `grep -r X /app --include=*.py` | 容器里的 busybox grep 对 `--include` 支持不一致，实测对必然命中的词返回 0 |
| `grep -R X /app --exclude-dir=.venv` | busybox grep **不认 `--exclude-dir`**，整条命令的输出不可信 |

正确姿势：**字面路径 + 不带花哨开关 + 用第二个 grep 过滤**，并且每次都先跑一条
「必然命中」的阳性对照（例如先 grep 一个你已知存在于 `/app/config.yaml` 的词），
对照不亮就说明尺子坏了，这一轮的所有"0 命中"结论作废。
另外 `grep -r` **不跟随符号链接目录**（`/app/.venv` 那种），要找包内文件得直接给包路径。
见 [[topic_ruler_failure_shapes]]。
