---
name: litellm-198-router-patch
description: >-
  在 198 母 router（ns litellm-product）上装 / 校验 / 回滚 **Router 内部猴补丁**，
  以及用「一死腿一活腿」的临时组验证换腿是否真的发生。含三步安装 SOP（漏 subPath
  volumeMount 会静默失效）、`/v1/responses` 与 `/v1/chat/completions` 的重试行为差异、
  以及会制造假绿的四个测量坑（响应缓存 / WA 单 session 钉死 / 单线程 stub 假延迟 /
  脏时间窗）。Use when the user mentions 198 母 router/litellm-product 装补丁、
  should_retry_this_error、cooldown、死号没下线、no healthy deployments、
  换腿/failover 没生效、dead_deployment_retry、或要验证母 router 的重试行为。
---

# 198 母 router 补丁与换腿验证

> 作用域：**只有 198（ns `litellm-product`）**。
> 阿里云那套（ns `carher`）结构完全不同 —— 那边写 CustomLogger hook 看
> [[litellm-hook-dev]]，**别把这里的三步 SOP 拿去阿里云用**。

## 0. 先分清你要装的是哪一种

| | CustomLogger hook | **Router 内部猴补丁**（本 skill） |
|---|---|---|
| 干什么 | 观测 / 改请求体 / 打点 | 改**路由决策本身**：重试、拉黑、挑腿 |
| 挂法 | 继承 `CustomLogger`，实例名进 callbacks | `import` 时猴补丁 `Router.xxx` / 模块函数 |
| 风险 | 低 | 高 —— 挂错点会静默改变所有请求的路由 |
| 必带 | — | **源码指纹守卫** + 整个 install 包 try/except |

判断：你要改的东西在 `litellm/router.py` 或 `litellm/router_utils/` 里，就是后者。

## 1. 装一个补丁是**三步**，漏任何一步都是静默失效

母 router 的每个补丁文件在 Deployment 里都有**自己独立的 `subPath` volumeMount**
> 🔴 **2026-10-20 起：下面写的 `litellm-callbacks` / `litellm-config` 是 base CM，三条车道（prod/gray/guarded-old）都不挂它们**
> （三车道挂 `litellm-product-<lane>-callbacks-<hash>`，从 Deployment volumes 反查）。
> `litellm-198-router-patch-install.sh` 的 install / rollback 现在**先核这两个 CM 是不是目标 Deployment 真挂着的**，
> 不是就退出 2 并列出它实际挂的 CM（10-20 在 198 上验过：默认值对 prod 拒绝、给真 CM 名放行）。
> 三车道新增 callback 用 `scripts/litellm-198-lane-callback-add.py`，换已有的用 `litellm-gray-rollout/scripts/lane-callback-swap.sh`。
> 线上现在有哪些补丁、挂在哪 ⇒ [[litellm-198-patch-layer-map]]；每个补丁接管了哪个上游函数 ⇒ `cli-anything-litellm patches targets`。

（2026-09-07 时 39 条）。只往 `litellm-callbacks` CM 里加 data，**容器里根本不会出现这个文件，
而且不会报任何错** —— callbacks 里那行 import 失败会被吞掉，你会以为装上了。

```
1) litellm-callbacks CM      加一条 data: <mod>.py
2) Deployment volumeMounts   加 {name: callbacks, mountPath: /app/<mod>.py, subPath: <mod>.py}
3) litellm-config CM         config.yaml 的 litellm_settings.callbacks 加 "  - <mod>.<实例名>"
```

**198 禁止 `kubectl apply`** —— 仓库 manifest 陈旧，apply 会回退 image + 内嵌 CM
（[[feedback_manifest_prod_drift_apply_overwrites]]）。只能 `patch` / `set image` / `rollout restart`。

三步都封在脚本里：

```bash
scripts/litellm-198-router-patch-install.sh install \
    k8s/litellm-callbacks/dead_deployment_retry.py \
    dead_deployment_retry.dead_deployment_retry
scripts/litellm-198-router-patch-install.sh verify   dead_deployment_retry.py
scripts/litellm-198-router-patch-install.sh rollback dead_deployment_retry.py \
    dead_deployment_retry.dead_deployment_retry
```

`install` 会先把三个对象备份到 198 `/root/ddr-backup/<名字>.<时间戳>.yaml`。
`rollback` 按 `subPath` 反查 volumeMount 下标 —— **永远别写死下标**，别人加了补丁就错位了。

### 验收判据（`verify` 干的事）

**逐副本**，不是抽一个：共用 CM 的多副本只重启一个 = 另一个静默跑旧代码
（[[feedback_shared_cm_needs_all_lane_rollout_pool_hides_drift]]）。每个副本都要满足：

- 容器内 `sha256sum /app/<mod>.py` == 本地文件 sha
- 日志里 `<mod>: installed` ≥ 1 行
- 日志里 `<mod>.*mismatch` == 0 行
- 日志里**该补丁自己的拒装字样** == 0 行（每道守卫的 `return` 分支各一条）

四条缺一就回滚。

🔴 **第 1 条单独成立 = 假绿。** 文件 sha 只证明**文件装载了**，
不证明**补丁装上了** —— 守卫拒装时 sha 照样对得上，而 Router 方法根本没被替换，
两者在 sha 这把尺子下**完全同形**。2026-09-29 我只核了 sha 就报"已上线"，
那一刻 4/4 副本的补丁全被自己的守卫拒了
（[[feedback_guard_must_warn_not_refuse_when_its_ruler_breaks]]）。
所以**第 2、4 条是不可省的**：一条证明装上了，一条证明没被自己的守卫挡掉。

### 补丁文件本身的三条硬要求

1. **保存原函数引用，不命中判别条件就原样调原函数** → 对其他所有情况逐字节不变
2. **源码指纹守卫**：`inspect.getsource` 原方法，比对一个只在该版本存在的特征串
   （升级 litellm 时这是唯一的安全网）。⚠️ **但要按后果分两级，别一律拒装**：
   - 缺了补丁就跑不起来的（import 不到某个类、方法体不是拷的那份）→ 拒装 + error log
   - 只是"读出来的文本不符预期"→ **`warning` + 继续装**，下次换镜像人工 re-diff

   理由：`inspect.getsource` 读不到是**量具坏了**，不等于**上游改了行为**。
   上游真改了，拒装是安全降级；量具坏了却拒装，是把能用的补丁关掉。
   2026-09-29 我把两者混为一谈，坏尺子让 **4/4 生产副本全部拒装**、线上退回无补丁约 8 分钟
   ⇒ [[feedback_guard_must_warn_not_refuse_when_its_ruler_breaks]]。
   **每道守卫上生产后必须逐副本 grep 确认它没误触发**（见下方验收判据第 3 条）。
3. 整个 `_install()` 包在 try/except 里 —— **补丁装不上，绝不能拖垮 proxy 启动**

范例：`k8s/litellm-callbacks/dead_deployment_retry.py`。

## 2. 验证换腿：死腿装置

### 为什么不能用现成的东西当死腿

- 拿真死号复现 → 要先把生产账号停掉，代价大且不可控
- 拿健康叶子发一个不存在的模型名 → **复现不出来**：它回的是
  `Invalid model name passed in model=...`，形状不对，**尺子是坏的**

所以用一个字节级可控的 stub：`:4000` 永远回 `400 + "There are no healthy deployments for this model"`，
`:4001` 永远回正常响应（chat 和 responses 两种形状按 path 分）。全程不启动任何真实账号、零上游费用。

```bash
scripts/litellm-198-dead-arm-rig.sh up                    # stub + 临时组 carher-deadtest + 24h 专用 key
scripts/litellm-198-dead-arm-rig.sh probe both 8 2        # 路径 responses|chat|both，组数，每组枪数
scripts/litellm-198-dead-arm-rig.sh status
scripts/litellm-198-dead-arm-rig.sh down                  # ⚠ 跑完必须执行，它挂在生产 router 上
```

stub 用**母 router 自己的镜像**（节点本地已有，`IfNotPresent` 不触发任何 pull，
所以不违反「K8s 镜像必须走 ACR VPC」—— 根本没有 pull 动作）。

### 怎么读数

| stub 命中 | 客户端 | 结论 |
|---|---|---|
| dead 次数 **==** 失败枪数 | 大量失败 | 一枪打一次，**零重试** —— 坏 |
| dead 次数很少（1~2） | 全成功 | 拉黑 + 换腿生效 —— 好 |
| dead=0 **且** good=0 | 全成功 | **全是缓存，这轮数据作废** |

2026-09-07 实测：装补丁前 `/v1/responses` 32 枪 **6 成功 / 26 失败**（dead 命中 26 = 零重试）；
装补丁后 **32/32**，dead 只命中 1 次（第一枪拉黑 60s，其余全走活腿）。

## 3. ⚠️ `/v1/responses` 和 `/v1/chat/completions` 行为不同

**同一套装置、同一批腿、干净窗口**：

- `/v1/chat/completions` → 死腿 400 **会**换腿，32/32 成功
- `/v1/responses` → 死腿 400 **一枪不重试**，直接失败

**生产 acct 流量走的是 `/v1/responses`（`call_type=aresponses`）—— 坏的那条。**
拿 chat 测这类问题会拿到假绿。

原因：`Router.aresponses` 不是普通方法，是构造时赋的
`factory_function(litellm.aresponses, call_type="aresponses")`（router.py:1217）
→ `_aresponses_with_streaming_fallbacks` → `_ageneric_api_call_with_fallbacks`
→ `async_function_with_fallbacks` → `async_function_with_retries` → `should_retry_this_error`。
所以钩 `should_retry_this_error` 两条路都覆盖得到，但**验证必须两条路各跑一遍**。

同族坑：429 mid-stream 换号循环也只装了 `/v1/responses` 一半
（[[litellm-midstream-failover-defense]]）。**在 198 上，「这个功能装了没」要按端点分别问。**

### 范例 2：`same_group_failover_cap`（10-20，1.105.1）

包 `Router._maybe_run_weighted_failover`：本请求排除表 `meta["_failover_excluded_ids"]` ≥ N 就返回 None
（= 上游「不适用」的返回值，common_utils 随即走兜底链），只读不写排除表，其它情况原样调原函数。
配 env `ROUTER_MAX_FALLBACKS=10`（`max_fallbacks` 不在 `update_settings` 白名单，只认 env）。

它示范了这一类补丁的验法，比第 2 节的死腿装置更省事：**不起 stub、不碰上游**，在车道 pod 里起新进程，
按 `/app/config.yaml` 顺序 import 全部 callbacks（= 线上补丁面）+ pod 自己的 env，建真 `Router`，
把 `litellm.aresponses` 换成假上游，走真 `Router.aresponses(stream=True)` 数落点。
同 pod 再 `python3 -S` 跑 vanilla 当对照。成品：`scripts/litellm-198-failover-contract-check.py`。

两个夹具坑（第一版都踩了）：
- 假流对象必须继承 `BaseResponsesAPIStreamingIterator` **且带 `_hidden_params`**，否则 1.103.2 外面再包一层
  `HiddenParamsAsyncIteratorWrapper`，`isinstance` 失败，中流兜底不接 ⇒ 把好车道量成红。
- `litellm.aresponses` 要在**建 Router 之前**换：Router 构造时就把它绑成 `original_generic_function`。

离线读「一个请求最多打多少次上游」用 [[cli-anything-litellm]] 的 `router-experiments/01-router-exp7.py` 套路
（真 Router + `mock_response=<异常>`）：这个补丁让全 500 从 96 次降到 72 次；**只抬上限不加 cap 会涨到 150 次**。

装法：新增 callback 走 `scripts/litellm-198-lane-callback-add.py install ... --env K=V`（同一个 patch 加 env，rollback 一起还原），
装完**立刻把 live 回填进车道快照**，否则下次 helm upgrade 静默冲掉（见 [[litellm-198-version-upgrade]] §6）。

## 4. 四个会制造假绿/假红的测量坑

1. **LiteLLM 响应缓存是开的** —— body 完全相同的请求根本不出门。
   每一枪的 content 必须唯一（脚本里加了 `#i-sessionid`）。
2. **不给 `x-litellm-session-id`，WA 会退化成 key 级钉子** —— 整轮全钉在同一条腿上。
   我第一次的"阳性对照"6/6 全绿就是这么来的：那一个 session 恰好钉在活腿。
   必须撒到 ≥8 个不同 session。session id 只能用 ASCII（中文会 `UnicodeEncodeError` latin-1）。
3. **stub 必须 `ThreadingHTTPServer`** —— 单线程 + HTTP/1.1 keep-alive 会堵住并发连接，
   造出 30~60s 的假延迟，看着像"上游慢"，其实是量具自己坏了。
4. **时间窗必须干净**：读 stub 计数用探针发起前一刻抓的 `--since-time=$T0`，
   不要用 `--since=3m` 这种相对窗 —— 会把上一轮的计数吃进来，得到无法解释的数。
   两轮之间 `sleep 65` 让 cooldown（60s）过期，否则上一轮的拉黑污染这一轮。

还有两个非测量的坑：

- `kubectl exec` **不带 `-i` 不转发 stdin** —— heredoc 喂 python 会静默收到空输入。
- litellm 容器里**没有 `curl`**，探针只能用 python `urllib.request`。

## 5. 别拿被人工干预过的指标给补丁记功

SpendLogs 里 marker 400 在 09-06 14:00 UTC 就归零了 —— 那是六个过期号被人工摘掉的时刻
（[[project_expired_acct_86_118_retired_from_pool_2026_09_06]]），**不是补丁的功劳**。
所以"降到接近 0"这条验收标准在补丁上线时已经无法区分有没有用。
真正的证据只有第 2 节那组同装置对照（6/32 → 32/32）。

上线后的观察指标应该是：真客户端 400 数量不变（没误伤）、
`No deployments available` 429 没暴涨（没引发 cooldown 风暴）、P95 没恶化。

## 6. 自动摘号：**叶子的 key 不是母 router 的 master key**

`dead_deployment_retry` 09-07 加了自动摘号：同一条腿连续 10 次 marker 400 →
实探叶子 → 确认死了 → `POST /model/delete`。三层闸门见
`docs/dead-deployment-retry-plan.md` 第七节。这里只记最容易踩的那一脚。

**198 的叶子 `chatgpt-acct-N` 认的是 secret `chatgpt-pool-master-key`，不是母 router 那把。**
拿错 key 去探叶子，健康叶子会回 **400 `No connected db.`**（叶子不连 DB，
`/v1/models`、`/models`、`/model/info`、`/model_group/info` 全走这条），
和死号的 400 长得一模一样。09-07 阳性对照就是这么翻车的：

```
母 router key  → 6/6 健康叶子 /v1/models = 400 No connected db.   ← 尺子坏的
pool key       → 3/3 健康叶子 /v1/models = 200，12 个 model       ← 判据成立
```

所以任何"探叶子"的动作，**token 必须取该腿 `litellm_params.api_key`**
（母 router 进程内是解密后的明文；`/model/info` 会把它剥掉，从外部拿不到）。
只有调母 router 自己的 `/model/delete` 才用 `LITELLM_MASTER_KEY`。

### 推论：探测结果必须有三档，不能两档

```
200 + data 非空  → alive    判瞬态，计数清零
200 + data 空    → dead     可以删
其它一切         → unknown  不判死也不判活，计数**不清零**
```

少了 `unknown` 这一档，两个方向都是坏的：把 401/400 归进 alive ⇒ **每次都清零，
永远升级不到阈值，功能等于没装，而日志还谎称"瞬态"**；归进 dead ⇒ 误删健康号。
装置里 4003 端口就是专门复现这个形状的（`/v1/models` 也回 400）。

### 阈值为什么是 10 而不是别的数

不是拍的，也**不是**从 SpendLogs 反推的 —— 失败行里 `model_id` 基本是空串
（9960 行只有 182 行有值，`api_base` 0 行），`LiteLLM_ErrorLogs` 表 0 行从没启用过，
**历史上根本无法按腿复原连击次数**。安全性来自另一处：命中就拉黑 60s ⇒
**一个副本攒到 10 次至少要 10 分钟**，秒级抖动天然过不去。这个时间下限是实测的
（同装置 32 枪只打中死腿 1 次）。别再试图用窗口 diff 去"验证"这个阈值。

## 7. 流式格式桥补丁（chat↔responses）：`responses_tool_args_done_backfill`

不是路由补丁，但同样是猴补丁、同样挂在母 router 上，判断和验证方法记在这里。

**链路分清再下刀**：chat 客户端 → 母 router `/v1/chat/completions` → 母 router 以 **`POST /responses`** 打
`chatgpt-acct` 叶子（`model_info.mode=responses`）→ 母 router 把 responses SSE 转回 chat。
所以 responses→chat 的格式转换发生在**母 router**（1.103.2），叶子（1.90.2）的 chat 端点生产不走。
🔴 直接打叶子 `/v1/chat/completions` 复现出的问题 ≠ 生产问题；10-09 我据此误判「要改叶子、重启 62 号」。
判叶子走哪条路：叶子日志 `POST /responses` vs `POST /v1/chat/completions` 计数。

**补丁本身**：包 `OpenAiResponsesToChatCompletionStreamIterator.chunk_parser`，按流记录哪些 output_index 收到过
非空 delta；没收到的在 `*.done`/`output_item.done` 时合成一个 delta 事件交给原函数（同一张 index 映射、同一个流 id）。
fail-open（自身异常 ⇒ 原事件原样交给原函数），env `RESPONSES_TOOL_ARGS_BACKFILL_DISABLED=1` 关。上游 #45356 合并升级后删。

**验证三层，缺一层都可能假绿**：
1. pod 内离线：`k8s/litellm-callbacks/tests/test_responses_tool_args_done_backfill.py`（30 项，每项先用原函数做阳性对照）
2. 进程内 A/B：`litellm.Router`，deployment 字段**逐个照抄生产腿**（`openai/<名>` + `api_base` 叶子 + `model_info.mode=responses`），
   打健康叶子。🔴 用 `litellm.acompletion(model="openai/responses/...")` 自己拼会走另一条路，读出「参数拼两遍」的假红。
3. 真客户端：同容器 `openclaw agent --local`，隔离 `HOME` 下复制一份 runtime 配置只改 `carher-pro-chat.baseUrl`
   指 gray `:30405` / prod `:30402` 交替打。10-10 读数 gray 4/4 OK vs 未补丁 4/4 MALFORMED。

上游无 delta 是**随机**的（同模型 3~4/5），A/B 每侧至少 6 发，并保留「全 delta」那几发看逐字节不变。

现成尺子（别再手搓）：
- `scripts/litellm-198-toolcall-args-probe.py upstream`（叶子 pod 内，数上游每个调用有没有 delta；401/429 发不计数）
- `scripts/litellm-198-toolcall-args-probe.py lanes --a … --b …`（两个入口交替打，含 single/text/nostream 阴性对照）
- `scripts/litellm-198-toolcall-args-probe.py sql --day …`（全网「客户端回传参数为空」按天统计，单日窗口）
- `scripts/openclaw-real-turn-probe.sh <provider> <model> <baseUrl> [rounds]`（her 容器内真回合）

同一类病的入口症状写在 `carher-her-reply-failure-triage` 根因 M。

- 计划与全部事实/推测表：`docs/dead-deployment-retry-plan.md`
- [[project_dead_deployment_retry_patch_2026_09_07]]
- [[litellm-hook-dev]] — 阿里云 ns `carher` 的 CustomLogger hook 开发（**不是这套 SOP**）
- [[litellm-midstream-chain-descent]] — 中流失败后**链子不往下走**（重发全砸第 1 跳）；
  含守卫必须按后果分两级：读不到源码只告警，混为一谈会让坏尺子关停 4/4 副本
- [[topic_litellm_ops_index]]
- [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]] — 第 0 步阳性对照
- [[feedback_manifest_prod_drift_apply_overwrites]] — 禁 apply
