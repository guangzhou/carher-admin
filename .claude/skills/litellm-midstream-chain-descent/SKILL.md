---
name: litellm-midstream-chain-descent
description: >-
  修 / 验 198（ns litellm-product）**中流失败后兜底链不往下走**的病：重发全砸在
  链子第 1 跳、后面几层一次没试、最后把上游原始错误抛给前端用户。真因是
  `run_async_fallback` 的 `**kwargs` 收参让 `attempted_targets` 去重表落在副本上，
  每次重发都重新命中第 1 个目标。含带阳性对照的 mock 回归夹具（`descend` / `guard`
  两个子命令）、链表必须是"一行五目标"形状 B 的理由、以及指纹守卫必须按后果分两级
  （读不到源码只告警、缺类才硬拒装 —— 混为一谈会让坏尺子关停 4/4 生产副本）。
  ⚠️ 本篇的补丁只在 ≤1.103.2；1.105.1 上同组上限由 same_group_failover_cap 提供（文首对照表）。
  Use when the user mentions 兜底链不下降 / fallback 只试第一层 / 重发全落同一组 /
  attempted_targets / astra 链子 / midstream_fallback_loop 改动后回归 /
  中流失败重试次数对不上 / 指纹守卫拒装 / 同组换号上限 / same_group_failover_cap / 过载没兜住 / 没 fallback 到可用模型。
---

# 198 中流失败的链子逐层下降

> 🔴 **版本边界（10-20）**：下面写的 `midstream_fallback_loop.py` 只在 **≤1.103.2** 上有（现在只剩保险丝
> guarded-old）。**prod/gray 是 1.105.1，这个补丁已下线**：中流多跳和 `attempted_targets` 去重上游自带了，
> 但上游**没有**「同组换号 ≤3」，同组换号和跨组兜底共用 `max_fallbacks`（默认 5）⇒ 10-20 过载时 5 次全换号、跨组 0 跳。
> 1.105.1 上的对应物：
>
> | 本篇讲的 | 1.105.1 上是谁 |
> |---|---|
> | `_MAX_SAME_GROUP_RETRIES = 3` | `k8s/litellm-callbacks/same_group_failover_cap.py`（包 `Router._maybe_run_weighted_failover`，env `SAME_GROUP_FAILOVER_CAP`） |
> | `_MAX_MIDSTREAM_FAILOVERS = 10` | env `ROUTER_MAX_FALLBACKS=10`（DB / `/config/update` 都不生效，只认 env） |
> | §3 mock 夹具 `descend` | `scripts/litellm-198-failover-contract-check.py`（按车道真 callbacks+env、三车道一起、自带 vanilla 对照） |
> | §5 装载锚点 `patched Router._aresponses_streaming_iterator` | `same_group_failover_cap: installed`；运行期证据 `handing over to fallbacks chain` |
> | 生产侧「兜住没有」 | `cli-anything-litellm db failure-rate <组...>`（按客户端请求的组归类，`rescued` 列 = 被别组救下） |
>
> §1 的真因分析、§2 链表形状 B、§4 守卫分两级、§5「sha 对 ≠ 装上」这些**原理仍然成立**，可以照读。
> 下线这类补丁前先列行为清单，见 [[litellm-198-version-upgrade]]「下线补丁 = 删行为」。

> 作用域：**198，ns `litellm-product`**，补丁文件 `k8s/litellm-callbacks/midstream_fallback_loop.py`。
> 装 / 回滚 Router 猴补丁的三步 SOP 看 [[litellm-198-router-patch]]；
> 改 `router_settings.fallbacks` 那一行本身看 [[litellm-198-fallback-chain]]；
> 四形状升格（怎么把 HTTP 200 里的失败认出来）看 [[litellm-midstream-failover-defense]]。
> 本篇只管**升格之后**：重发到底落在哪一层。

## 0. 症状与它的双胞胎

症状：中流失败被接住了（日志有 `midstream_fallback_loop: attempt N/10`），
但 `N` 发全部落在**同一个组**，链子后面几层一次没试，最后抛错给前端。

它和另外两件事**同形**，先排掉再往下走：

| 看着一样 | 真身 | 怎么分 |
|---|---|---|
| 链表里就没配那几层 | 配置问题 | 查 DB（**不是 yaml**，见 §2） |
| 补丁根本没装上 | 装载问题 | 逐副本 grep 装载行 + 拒装行（§4） |
| 装了、链表也对，但只砸第一层 | **本篇** | 重发落点全是同一个组名 |

## 1. 真因：`**kwargs` 收参让去重表落在副本上

LiteLLM 用 `attempted_targets`（`router_utils.fallback_event_handlers.AttemptedFallbackTargets`）
记住"这一发已经试过哪几个目标"，`run_async_fallback` 在开头读它、结尾写回：

```python
# fallback_event_handlers.py:384 附近
carried_targets: Final = kwargs.get("attempted_targets")
...
if attempt_key in attempted: continue      # 去重：试过的目标跳过
```

但它的签名是
`run_async_fallback(*args, litellm_router, fallback_model_group, original_model_group, **kwargs)`
—— **`**kwargs` 收进来的是一个新 dict**，`common_utils` 里的 `input_kwargs` 也是新的。
所以它写回的 `kwargs["attempted_targets"]`、`kwargs["model"]` 全落在**副本**上，
我们循环里的 `initial_kwargs` 一个都收不到。下一发重新读原始组名 → 查表 → 又拿到第 1 个目标。

**修法**：我们自己在 `initial_kwargs` 上放一个**持久的** `AttemptedFallbackTargets()`，
让它跨轮活着，`run_async_fallback` 每轮把试过的目标记进去，下一轮自然跳过。

```python
if attempts >= _MAX_SAME_GROUP_RETRIES and not isinstance(
    initial_kwargs.get("attempted_targets"), _AttemptedFallbackTargets):
    initial_kwargs["attempted_targets"] = _AttemptedFallbackTargets()
```

**时机是 `>=` 不是 `>`。** 晚注入一发的话：第 4 发时跨组闸门已经开了，
但去重表刚建成、还是空的 ⇒ `run_async_fallback` 重挑第 1 个目标 ⇒ 同层白打 4 发。

两个常量的分工（**不要**指望"差值自动表达行为"，那只是设计意图，2026-09-28 记过一次，
实际没生效，真正让链子下降的是上面这段注入）：

| 常量 | 值 | 管什么 |
|---|---|---|
| `_MAX_SAME_GROUP_RETRIES` | 3 | 前 3 发花在同组换号（`_cross_group = attempts > 它`）|
| `_MAX_MIDSTREAM_FAILOVERS` | 10 | 一发请求总重发上限 |

## 2. 链表必须是"一行五目标"的形状 B

```
形状 B（对）：  astra → [6-sol, 6-luna, 5.6-sol, sa-grok-4.20, openrouter-deepseek-v4.1-flash]
形状 A（错）：  astra → [6-sol]，  6-sol → [6-luna]，  6-luna → [5.6-sol] ...
```

**形状 A 在去重表下会退化成只重试 1 次** —— 链式单跳每层只有一个目标，
被去重表标记后该层就没有候选了。所以链子要几层，就在**同一行**里写几个目标。

⚠️ **判生效值只认 DB**（`LiteLLM_Config` 表 `param_name='router_settings'`）。
`config.yaml` 里有另一份且可以完全矛盾，DB **整键覆盖**它，
只有 DB 值是空列表时才让位 ⇒ [[feedback_db_router_settings_overlays_config_yaml]]。
2026-09-29 实测：DB 里 astra 已是上面那五个目标，yaml 里还是单目标旧残留 ——
**改 yaml 是改了不生效的那份**，别为了"看起来一致"去动 config CM，那是白担风险。

## 3. 回归：`scripts/litellm-midstream-chain-descent-mock.py`

```bash
# 对照必须是改动前**线上在跑**的那版
git show 32a66b1:k8s/litellm-callbacks/midstream_fallback_loop.py > /tmp/prepatch.py   # sha 8bf25d663a10

POD=$(ssh cltx@10.68.13.198 "sudo kubectl -n litellm-product get po -l app=litellm-proxy \
      --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'")
# 两个文件拷进 pod，然后
kubectl -n litellm-product exec $POD -- python3 /tmp/chain_mock.py descend \
    --module /app/midstream_fallback_loop.py --control /tmp/prepatch.py
kubectl -n litellm-product exec $POD -- python3 /tmp/chain_mock.py guard \
    --module /app/midstream_fallback_loop.py
```

**为什么必须在 pod 里跑**：本地 Mac 没有 litellm。
**为什么 `--control` 不给就拒跑**：只有新版本的读数说明不了任何事 ——
夹具把 `fallbacks` 喂错、或者压根没触发 `MidStreamFallbackError`，
两种情况都会产出看着漂亮的数字（[[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]）。

夹具自己会报废两种坏对照：**对照 == 被测**（新版已上生产，`/app/` 那份就是新版），
以及**两边 `_MAX_MIDSTREAM_FAILOVERS` 不同**。第二条是 2026-09-29 真踩到的：
错取了抬预算前的 `a62bac8`（上限 5），对照跑出"5 发"而不是线上真实的"10 发" ——
**对照的绝对数字变小，读上去像"病没那么严重"**，而且完全无声。

### 2026-09-29 实测读数（判据就是这张表）

| 活着的层 | 对照 `8bf25d663a10` | 新版 `69280bb117f9` |
|---|---|---|
| 全层皆死 | 10 发**全落** `6-sol`，抛错 | 3+1+1+1+1 = 7 发，抛错 |
| 只第 5 层活 | 10 发全落 `6-sol`，抛错 | 7 发，**成功** |
| 只第 2 层活 | 10 发全落 `6-sol`，抛错 | 4 发，**成功** |

⚠️ **这只是夹具里的 Router 行为**，不是生产 worker 的行为。
别指望 LiteLLM 自带的强制开关：`mock_testing_fallbacks` 在 198 上是硬 400
（[[feedback_198_mock_fallback_forcers_ignored]]），**生产侧"兜住没有"只能等真实流量**，
用下面 §3.5 那把只读尺子去读。

### 夹具的两个实现约束（照抄，别自己简化）

1. **一个场景一个子进程，`python3 -S` 启动再 `site.main()`**。
   `-S` 跳过 `sitecustomize.py`（它 import 时就把补丁装上了），拿到 vanilla Router；
   `site.main()` 把 `-S` 一起关掉的 site-packages 搜索路径找回来。
   同进程内**无法**卸载已装的补丁（`mappingproxy` 不可写 + 原方法引用已被覆盖）。
2. **钩点必须是 `r._ageneric_api_call_with_fallbacks_helper`**。
   钩在它上游（我第一版钩早了）会让"落点"那一列全是 `?` ——
   那时 LiteLLM 还没挑 deployment，**尺子答不出"同层重试 vs 换层"这个问题，等于没有尺子**。

## 3.5 生产侧：`scripts/litellm-midstream-chain-descent-prod-audit.py`

夹具答的是"如果坏了我看得出来吗"，这把答的是"窗口内真实流量上发生了什么"。
**两把都要跑，缺一把就报不了"生产上已验证"。** 全程只读，不写任何配置。

```bash
python3 scripts/litellm-midstream-chain-descent-prod-audit.py --group gpt-6-astra --hours 3
```

它出五段：A0 逐副本装载态 / A 面日志数每条流重发几发 / A2 按 trace 看有没有抛给用户 /
B 面 SpendLogs 看跨组真落在哪层 / 两面互校。

### 2026-09-29 首次生产实测（~10.5h 窗口，4 副本）

| 读数 | 值 |
|---|---|
| 有中流失败并重发的流 | 107 条（235 发） |
| 同组重发（第 1~3 发） | 204 发 |
| **跨组重发（第 4 发起）** | **31 发** ← 病灶未修时这些全砸第一层 |
| 打满 10 发耗尽抛错 | **0** |
| 能归属到 astra 流的用户可见报错 | **0** |

SpendLogs 侧 2h 内 111 条跨组落点**全 success**，含 `gpt-6-astra → chatgpt-gpt-6-sol`。
最深那条流跑了 3 发同组 + 4 发跨组 —— 修复前这 4 发会全落第一层。
**用户的目标（前端不吃报错）在生产上成立。**

### 这把尺子的三个坑（都是首跑真踩的）

1. 🔴 **归属只能按 trace id，不许按组名 grep 整行。** 我 `grep astra` 数出"32 条
   astra 报错"，实际是别组的长报错串里嵌了 `astra` 子串（`fallbacks` 字典会被整个
   打进 message，里面列着所有组名）。按 trace 重查是 **0**。组名 grep 在这里恒定虚高，
   且高得很像真的。
2. 🔴 **A 面（日志滑窗 2~4 分钟）和 B 面（SpendLogs 数小时）差着数量级。**
   首跑 A 面跨组 = 0、B 面 = 7，看着自相矛盾，其实是窗口不对齐 ——
   只拿 A 面会把**已经修好的系统判成没修**。脚本里的 `cross_check()` 就是为此而加，
   此时唯一合法结论是"这几分钟没赶上"，不是"链子不下降"。
3. 🔴 **刚起的 pod 和 Terminating 的 pod 必须排除。** 首跑正撞上生产滚更（不是我干的，
   我全程只读），一个 95 秒的新副本还没打出装载行，被判"判不了"；同时刻 `sha256sum`
   证明它装得好好的。脚本现在按 `startTime` 卡 180s 并跳过 `deletionTimestamp` 非空的。
   **滚更窗口里的 0 是 rollout 窗口，不是回归**（[[feedback_rolling_restart_window_is_not_a_code_regression]]）。

另外：`metadata::text LIKE '%<trace>%'` 在 25 万行 SpendLogs 上全表扫、120s 超时，
**必须先用 `startTime` 收窗口**再 LIKE。

## 4. 指纹守卫必须按后果分两级

`midstream_fallback_loop.py` 现在有两道守卫，**行为故意不一样**：

| 守卫 | 缺了会怎样 | 行为 |
|---|---|---|
| `AttemptedFallbackTargets` import 不到 | 注入代码 `NameError`，补丁跑不起来 | **硬拒装** |
| `Router` 方法体源码指纹对不上 | 方法体不是拷的那份，装了会改错行为 | **硬拒装** |
| `run_async_fallback` 源码里找不到 `kwargs.get("attempted_targets")` | 只是**文本**读不到 | **`warning` + 继续装** |

第三条为什么只告警：**`inspect.getsource` 读不到 ≠ 上游改了行为**。
上游真改了，降级是安全的（链子退回单层，和补丁前一样）；
量具坏了却拒装，是**把能用的补丁关掉**，比不加守卫更差。

2026-09-29 我第一版把这两件事混为一谈，结果 **4/4 生产副本全部拒装**，
线上退回完全不打补丁，连补丁之前就有的"同组重挑 3 次"都没了，持续约 8 分钟。
详见 [[feedback_guard_must_warn_not_refuse_when_its_ruler_breaks]]。

### ⚠️ 未结案：这条告警在生产里恒亮，真因没查出来

v2 上线后，4 个副本**各 2 条** `source does not show ...` 告警，稳定复现。
而同一容器里 `exec` 起干净进程**怎么测都能读到**：冷 import、热 import、
带不带 `sitecustomize`，`common_utils.__globals__` 里那个引用也是同一对象、也能匹配。

已排除：全盘只一份 `fallback_event_handlers.py`；needle 确实是双引号写法；
函数无 `__wrapped__`；`__module__`/`__qualname__` 与路径一致。
**未排除**：uvicorn 多 worker 下 `inspect` / `linecache` 的行为。
**没查出真因，不下结论**（[[feedback_fresh_in_pod_process_is_not_the_proxy_process]] 同族）。
它现在不影响功能，但日志里会一直有 —— **别把它当新故障重新排查一遍**。

## 5. 上线验收：文件 sha 对得上 ≠ 补丁装上了

这是本轮最贵的一课。我第一版报"已上线"时只核了文件 sha，
而那一刻 4/4 副本的补丁全被自己的守卫拒了。逐副本必须三条都满足：

```bash
# 每个副本分别跑，不许抽样（共用 CM 只重启一个 = 另一个静默跑旧代码）
sudo kubectl -n litellm-product exec <pod> -- sha256sum /app/midstream_fallback_loop.py
sudo kubectl -n litellm-product logs <pod> | grep -c 'patched Router._aresponses_streaming_iterator'  # ≥1
sudo kubectl -n litellm-product logs <pod> | grep -cE 'NOT patching|install failed, skipped|import failed, skip'  # ==0
```

第 3 条是这次新加的 —— 少了它，拒装与装好**完全同形**。
判收敛时排除 `Terminating` 副本，否则是假红。

🔴 **锚点必须从源码抄，别凭印象写。** 本 skill 第一版第 2 条写的是
`grep 'midstream_fallback_loop.*installed'` —— 源码里**没有 `installed` 这个词**，
真实字样是 `patched Router._aresponses_streaming_iterator`。照抄那条会恒 0，
**把装好的补丁读成没装**，而"没装"正是我上一轮真踩过的故障，
于是这把坏尺子会精准地伪造出一个我最容易相信的结论。
写 grep 锚点前先 `grep -n 'verbose_router_logger' k8s/litellm-callbacks/*.py` 核一遍。

⚠️ **pod 跑过几小时后这两条会双双变 0** —— 装载行是启动期打的，日志窗口只留
2~4 分钟（滑窗），启动段早滑掉了。**那不等于没装**。窗口滑掉之后改用
"补丁自己运行期才会打的行"当装载证据，本补丁是
`grep -c 'mid-stream failover attempt'`（>0 即补丁在跑，这行只存在于我们的补丁里）。

**别碰 `cm/litellm-callbacks`**（那个可变的共用 CM）：13 个 `chatgpt-acct-*` Deployment
挂着它，其中有在服务的，**重启在服务的 acct 号可能永久打死且回滚救不回来**。
本补丁走的是不可变 CM：新建 `litellm-stable-callbacks-<sha12>` + 一次原子 `patch`
把卷的 `configMap.name` 换掉。验"我没碰共用 CM"的证据 = 它的 `resourceVersion` 没变。

## 6. 回滚

```bash
# 把 callbacks 卷（下标 1，**别写死，按 subPath 反查**）指回改动前那份
sudo kubectl -n litellm-product patch deploy litellm-proxy --type=json \
  -p '[{"op":"replace","path":"/spec/template/spec/volumes/1/configMap/name",
        "value":"litellm-stable-callbacks-e0d82a3a9435"}]'
sudo kubectl -n litellm-product rollout status deploy/litellm-proxy --timeout=15m
```

改动前的卷定义备份在 198 `/tmp/rollback_volumes.json`。

## 相关

- [[project_198_astra_chain_descent_fixed_2026_09_29]] — 本轮全记录
- [[topic_ruler_failure_shapes]] — 窗口差数量级 / 锚点过期，两条都在里面
- [[feedback_rolling_restart_window_is_not_a_code_regression]]
- [[litellm-198-router-patch]] — 装 / 验 / 回滚 Router 猴补丁的三步 SOP
- [[litellm-198-fallback-chain]] — 改 `router_settings.fallbacks` 那一行
- [[litellm-midstream-failover-defense]] — 升格：怎么把 HTTP 200 里的失败认出来
- [[feedback_guard_must_warn_not_refuse_when_its_ruler_breaks]]
- [[feedback_db_router_settings_overlays_config_yaml]]
- [[feedback_198_mock_fallback_forcers_ignored]] — 合成不出"兜住了"
- [[feedback_fallback_reached_is_not_fallback_held]] — 走到 ≠ 兜住
