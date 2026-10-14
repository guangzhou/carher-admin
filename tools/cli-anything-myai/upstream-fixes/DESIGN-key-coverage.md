# 按 key 补齐漏掉的用量 + 对账（2026-10-14）

## 现状（198 生产库实测）

- 30 天上游花费按用户分类：
  - 用户表里有的用户：$680,401
  - **key 有 user_id 但用户表里没有这个人**（11 个 id）：$40,341
  - **key 没有 user_id**：$18,151（19 把 key，绝大部分是 `carher-hangzhou`）
- 按天汇总表（`LiteLLM_DailyUserSpend`）里的 user_id 和 key 的 user_id 没有冲突（不一致的 172 行合计 $0），所以**按 key 补漏不会和按人统计的重复**。
- 另有 $695 落在已删除的 key 上：汇总表里有 api_key，但 key 表里已经没有这把 key。

## 上游能直接给的数据

`GET /user/daily/activity?start_date=D&end_date=D&page_size=1000`（**不带 user_id**）按页返回全局汇总：
- `breakdown.api_keys[<sha>]`：每把 key 的 metrics 和 metadata（`key_alias`、`user_id`、`team_id`、`key_exists`）。
  10-12 两页共 406 把 key，加总 **136,113 请求 / $28,550.79，与全局总数完全一致**。
- `breakdown.models[<m>].api_key_breakdown[<sha>]`：每个模型下每把 key 的 metrics，所以能做到 **key × 模型** 粒度。
- 它按 UTC 日界算，和 myai 已有的 daily activity 回退路径一致（都是 UTC 日界），不引入新的口径差异。

## 方案

在 `collect_backend` 的末尾（按人统计之后、身份归类之前）加一步 **补漏**（只对 primary，her 不动）：

1. 拉一次上面这个全局按天汇总，按 key × 模型展开成 usage 行。
2. **只保留**"按人统计覆盖不到"的 key：
   - key 的 `user_id` 为空，或者
   - key 的 `user_id` 不在本次的 `account_users`（即用户表）里。
   按人统计已经覆盖的 key 一律跳过，所以不会重复计算。
3. 补漏行的 `user_id` 规则：
   - key 有 user_id（孤儿）：就用这个 user_id，身份目录能认出来就带上名字和邮箱；
   - key 没有 user_id：用 `key:<key_alias>`（没有别名就用 `key:<sha 前 12 位>`），显示名就是 key_alias。所以 `carher-hangzhou` 会作为一行独立的"用户"显示出来。
   - `attribution_source='key_rollup'`，`billing_eligible=False`。**补漏只负责显示，不自动计费**；计费仍然走已有的 key→组织映射（`_apply_token_attribution`），这条路径对补漏行同样生效。
4. 这一步只在 **daily activity 路径**（长窗口，或者日志扫描判了不完整之后的回退）里执行。
   日志扫描路径本来就把所有 user（包括 unattributed）都带上了，不需要补。
5. 补漏失败（上游出错）时不影响已有结果，只记日志。

## 对账检查

- 每次同步发布前，把本次 primary 的总请求数和全局总数比一次，记成日志加指标。
- 差距超过 1% 写 warning：`usage coverage gap backend=primary day=… scanned=… upstream=… gap_pct=…`。
- 这一版只做告警，不拦发布（拦了会让整个看板停更）。

## 开关

`USAGE_SYNC_KEY_ROLLUP_ENABLED`（默认 true）。出问题时改 ConfigMap 关掉，不用回滚镜像。

## 不在这次范围

- 不改 198 的用户表（建议 2，需要单独授权）。
- 不改计费规则（billing_eligible 的判定）。
