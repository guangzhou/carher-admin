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

三条缺一就回滚。

### 补丁文件本身的三条硬要求

1. **保存原函数引用，不命中判别条件就原样调原函数** → 对其他所有情况逐字节不变
2. **源码指纹守卫**：`inspect.getsource` 原方法，比对一个只在该版本存在的特征串；
   对不上就拒绝安装 + 打 error log + 保持原状（升级 litellm 时这是唯一的安全网）
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



- 计划与全部事实/推测表：`docs/dead-deployment-retry-plan.md`
- [[project_dead_deployment_retry_patch_2026_09_07]]
- [[litellm-hook-dev]] — 阿里云 ns `carher` 的 CustomLogger hook 开发（**不是这套 SOP**）
- [[topic_litellm_ops_index]]
- [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]] — 第 0 步阳性对照
- [[feedback_manifest_prod_drift_apply_overwrites]] — 禁 apply
