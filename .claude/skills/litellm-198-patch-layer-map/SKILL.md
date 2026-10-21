---
name: litellm-198-patch-layer-map
description: >-
  198 LiteLLM（ns litellm-product）**线上补丁层全景图**：我们在上游 litellm 上一共改了几层、每层在哪、
  怎么一条命令现量（`scripts/litellm-198-patch-layer-map.py`），以及读 198 上 litellm 源码的正确姿势
  （`/root/litellm-fork` 读 tag 不 checkout）。四层 = callbacks CM / sitecustomize / 整文件覆盖件 / 烘焙进镜像。
  Use when 用户说「线上 litellm 被我们改了哪些地方 / 有哪些补丁 / 补丁到底装上没有 / 看看 198 litellm 源码 /
  熟悉 litellm 代码和补丁 / 某个上游函数被谁 patch 了 / 三条车道补丁一样吗 / 保险丝和生产差在哪 /
  升级前先摸清补丁面」。装/换单个补丁走 [[litellm-198-router-patch]] 或 lane 脚本，整版本升级走
  [[litellm-198-version-upgrade]]，写新 hook 走 [[litellm-hook-dev]]；本 skill 只回答「现在是什么样」。
  读上游源码 / 看每个补丁接管了哪个上游函数 / 查生产库，走 [[cli-anything-litellm]]（`src` / `patches` / `db`）。
---

# 198 LiteLLM 补丁层全景

**一句话**：线上 litellm ≠ 上游 tag。差异分四层，任何一层单看都读不出全貌；判据只认
「容器里的 site-packages 和上游 tag 逐文件 md5 比」+「容器里真实 import」，不认 CM 名、helm 快照、git、记忆。

```bash
# 先现量版本（脚本会再核一遍，--tag 与线上不一致直接拒绝出数）
scripts/litellm-198-patch-layer-map.py --tag v1.105.1                       # lane=prod
scripts/litellm-198-patch-layer-map.py --lane gray --tag v1.105.1
scripts/litellm-198-patch-layer-map.py --lane guarded-old --tag v1.103.2
scripts/litellm-198-patch-layer-map.py --tag v1.105.1 --probe-chain --json   # 外加替换链实测
```

只读：ssh key + `sudo -n`，在 pod 里 `md5sum`/`python -c`，在 198 `/tmp/plm-*` 落临时文件；不改任何对象。
退出码：0 正常 / 1 有 callback import 失败或容器里多出上游没有的文件 / 2 环境错误。

## 四层分别在哪

| 层 | 在哪 | 怎么生效 | 怎么量 | 静默失效的样子 |
|---|---|---|---|---|
| ① callbacks | `litellm-product-<lane>-callbacks-<hash>` CM，每个 .py 一条 subPath 挂 `/app/<mod>.py`；`config.yaml` 的 `litellm_settings.callbacks` 列名 | import 时注册 CustomLogger / 猴补丁 Router、迭代器等 | 容器里逐个 `importlib.import_module` | ImportError 被吞；守卫拒装但 import 成功 |
| ② sitecustomize | 同一个 callbacks CM 的 `sitecustomize.py`，挂到 `<site-packages>/sitecustomize.py` | Python 启动自动加载，**不在 callbacks 列表里** | 启动日志 `[sitecustomize] … patched`（4 个：spendlog 清洗 / 批量救援 / prisma 传输误判 / UI spendlog 渲染） | 列表里找不到它，很容易以为没有 |
| ③ 整文件覆盖件 | `snapshot-*-patch` CM，subPath 单文件盖在 `litellm/` 包里 | 直接替换上游文件 | md5 与上游不同 **且** Deployment 里有落在包内的 subPath mount | 版本换了覆盖件没重做 ⇒ 静默回退上游改动 |
| ④ 烘焙进镜像 | `litellm-gray-rollout/patches/v<ver>/` 的脚本，Dockerfile 里就地锚点替换 | 镜像里的文件本身就是改过的 | md5 与上游不同、**没有** mount 盖着 | 不在任何 CM 里，只看 CM/helm 永远看不到 |

脚本把 ③④ 合在一把尺子里抓（都落在 site-packages），再按 mount 反查分开。

## 现状快照（2026-10-20 晚实测，数字会过时，以脚本现量为准）

| lane | 版本 | ① callbacks（含内置 prometheus） | ③ 覆盖件 | ④ 烘焙文件 |
|---|---|---|---|---|
| prod `litellm-proxy`（4 副本，helm rev21） | 1.105.1 | 37 个，36 import OK | 3 | 5 |
| gray `litellm-proxy-gray`（2，helm rev26） | 1.105.1 | 38 个，37 import OK | 3 | 5 |
| guarded-old（2，保险丝） | **1.103.2** | 38 个（含 `midstream_fallback_loop`，该版本应在） | 3 | 2 |

10-20 当天的变化（都已进 helm values 和 git `8a85fc8`）：
- prod/gray 加了 `same_group_failover_cap`（包 `Router._maybe_run_weighted_failover`，同组已排除 3 条腿就交给兜底链）
  + env `ROUTER_MAX_FALLBACKS=10`；摘掉了 `anthropic_passthrough_pingfix`、`log_truncate`；DB 连接池 3、key 缓存 3000。
  行为验收不靠补丁地图，靠 `scripts/litellm-198-failover-contract-check.py`（见 [[litellm-198-version-upgrade]] §4）。
- guarded-old 仍是老的 `midstream_fallback_loop`，兜底契约同样过。
- 三车道 callbacks 和 git 是否一致：`scripts/litellm-198-callbacks-drift.py`；补丁归属三车道比对：`cli-anything-litellm patches compare`。

③ 覆盖件（三条都有，按版本各自重做）：`litellm_core_utils/streaming_handler.py`、
`proxy/pass_through_endpoints/streaming_handler.py`、`…/llm_provider_handlers/anthropic_passthrough_logging_handler.py`。

④ 烘焙：4 个脚本 → 5 个文件（1.105.1）

| 脚本 | 改的文件 | 干什么 |
|---|---|---|
| `litellm-patch-capacity-ratelimit.py` | `litellm_core_utils/exception_mapping_utils.py` | 400 + "Selected model is at capacity" 升格 RateLimitError，触发冷却/兜底 |
| `litellm-patch-sse-lifecycle.py` | `responses/streaming_iterator.py` | Responses SSE 生命周期补齐（468 行 diff，最大的一块） |
| `litellm-patch-stream-alias-pricing.py` | `litellm_core_utils/litellm_logging.py` + `proxy/proxy_server.py` | #44341 回移：流式经 alias 按落点计价，给客户端的 chunk 用拷贝改 model |
| `litellm-patch-key-alias-auth-compat.py` | `proxy/auth/auth_checks.py` | 回退 #43049：key aliases 不再收窄鉴权（只回退 auth，路由仍用 aliases） |

guarded-old 的 1.103.2 配方只有前两个——#43049 是 1.104 才引入的，#44341 是 1.103.3 才有 ⇒ **回退到保险丝时别名流式计价按旧行为**。

`midstream_fallback_loop` 只在 prod/gray 摘掉了（1.105 上游已有多跳中流兜底，指纹仍命中会覆盖上游新实现，见 [[litellm-198-version-upgrade]] §0）。

## 多个补丁 patch 同一个上游符号

```bash
python3 scripts/litellm-callback-upstream-audit.py …   # 只比两个 tag；撞点要自己按 patches 聚合
```

10-20 聚合 37 个文件、47 个被替换符号，两个撞点：

- `BaseResponsesAPIStreamingIterator._process_chunk` ← `codex_compaction_v2` / `streaming_output_backfill` / `deepseek_id_prefix`
- `proxy_server._format_streaming_sse_chunk` ← `codex_compaction_v2` / `deepseek_id_prefix`

每个都存 `_orig` 再包一层、各带幂等哨兵（`_ccv2_patched`、`_output_backfill_patched`）⇒ 应当是**叠起来**。
`--probe-chain` 读 `__closure__` 实测（不信 import 成功）：

```
deepseek_id_prefix → streaming_output_backfill → codex_compaction_v2 → vanilla   ⇒ 4 层，链底是上游原函数
```

叠的顺序 = `config.yaml` 里 callbacks 的顺序。**调 callbacks 顺序会改变这条链**；新写一个 patch `_process_chunk`
的补丁，必须也存 `_orig` 包一层，直接赋值会把前面几层全盖掉，且 import/sha/rollout 全绿。

## 读 198 上的 litellm 源码

| | |
|---|---|
| 仓 | 198 `/root/litellm-fork`（root 0700：`cd` 进不去，一律 `sudo -n git -C /root/litellm-fork …`） |
| 读法 | `git show <tag>:<path>` 或 `git archive <tag> litellm \| tar -x -C <dir>`，**不 checkout**（工作区有别人的未提交改动） |
| fetch | `https://www.github.com/BerriAI/litellm.git`（`.git/config` 里 `insteadOf` 指向的 kkgithub 证书坏） |
| 稳定分支 | 已 fetch 成 `up/stable-1.105.x`、`up/stable-1.106.x`；有没有新回移：`git rev-list --count v1.106.0..up/stable-1.106.x` |
| 线上真实代码 | 不是 tag，是 tag + ③④；要看「线上这个函数长什么样」就 `kubectl exec … cat <pkg>/<path>`，再和 tag `diff -u` |

本机 `~/codes/litellm` 停在 5 月，别用它判线上行为。读源码直接用
`cli-anything-litellm src find <符号>` / `src show <path:line>`（自动取线上版本的 tag，见 [[cli-anything-litellm]]）。

## 坑（都踩过）

1. **base CM 不是线上那份**。`litellm-callbacks` / `litellm-config` 三车道都**不挂**（10-20 只有 13 个 chatgpt-acct 挂
   `litellm-callbacks`）。三车道挂的是带 hash 的派生 CM，名字从 Deployment `volumes` 反查。
   ⇒ `scripts/litellm-198-router-patch-install.sh` 默认 `CB_CM=litellm-callbacks CFG_CM=litellm-config`，
   **对三车道直接跑 = 改错对象且全绿**。三车道新增 callback 用 `scripts/litellm-198-lane-callback-add.py`，
   换已有的用 `litellm-gray-rollout/scripts/lane-callback-swap.sh`（[[project_198_callback_git_live_drift_2026_10_03]]）。
2. **CM 名 ≠ 内容**：同一份内容不同 lane 名字不同，升级后名字也会换（[[feedback_derived_cm_name_is_not_content_sha_after_upgrade]]）。
3. **import OK ≠ 补丁装上**：猴补丁守卫拒装时 import 照样成功。要么看该补丁自己的 `installed`/拒装日志
   （[[litellm-198-router-patch]] 验收四条），要么像 `--probe-chain` 那样直接看方法被没被换掉。
4. **新起 python 进程读 `litellm.callbacks` = 0**：那是空进程，不是 proxy 运行时，读数无效。
5. **版本先现量**：记忆写过 guarded-old=1.100.1，实测 1.103.2（[[feedback_current_version_in_memory_goes_stale]]）。
   脚本对 `--tag` 和 pod 内 `importlib.metadata.version("litellm")` 不一致直接退出。
6. 「上游有、容器里没有」的 1 个文件是 guardrail benchmark 测试件，镜像构建裁掉的，不是补丁。
7. 容器里逐文件 md5 约 2700 个 .py，`kubectl exec` 一次跑完；别把 155MB 的包拷回本地。

## 扩展这个脚本

- 新的撞点：改 `CHAIN_PROBE`（模块、类、方法），或照它写第二个探针。
- 新 lane：加进 `LANES`（Deployment 名；selector 自动从 Deployment 读）。
- 改完对三条 lane 各跑一遍，guarded-old 必须用它自己的版本 tag——这也是版本门禁的阳性对照：
  传错 tag 必须拒绝出数。
