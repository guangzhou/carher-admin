# 客户端链式增量 shim（`@cx-chain:v3`）下线 — 2026-09-01

一句话：**服务端那半已经不在线上了，客户端那半还在发"只带增量 + `previous_response_id`"，
而服务端把增量当成整段上下文处理 —— 静默少收历史，且不报错。** 所以把客户端这半摘掉。

## 三段式

**假设**：客户端 shim 现在是有害的空转 —— 它按"服务端会用 `previous_response_id` 把历史补回来"
的前提裁掉了 `input` 的前缀，但服务端并不认这个字段。

**证伪条件**：如果假设错，应当看到 ①线上 `responses.js` 里有解析 `previous_response_id` 的代码；
或 ②伪造一个不存在的 `previous_response_id` 会被拒（400/404）；或 ③客户端 trace 里有
`fallback-full` 事件（说明服务端确实在拒，shim 自己退回了全量）。

**数据**（三条腿同向）：

| 腿 | 查法 | 结果 |
|---|---|---|
| ① 代码 | 线上产物 `resp_deployed_20260901.js` 全文 grep | `previous_response_id` **0 次**；`:676` 只解构 `{ input, instructions, stream }` |
| ② 行为 | 临时真 key 打 `/v1/responses`，带一个**伪造** id | **HTTP 200** —— 字段被端到端忽略（没有任何一层在解析） |
| ③ 兜底 | 本机 `/tmp/cx-chain-trace.log`（533 行真实事件） | `fallback-full` **0 次**（shim 唯一的安全阀是 400/404；服务端返 200 ⇒ 永不触发） |

补充事实（决定影响面）：

- chain-srv 的服务端那半**不是独立服务**，它就在 `responses.js` 里（`ZK_CHAIN_SRV` 门控的四个
  补丁点，08-30 在 lane 82 canary）。它被回滚后，客户端这半就成了单腿。
- shim 的运行时门是 `CX_CHAIN==='0'` 才关 ⇒ **默认开**；08-31 分发的安装器
  `cursor_team_setup.js` 在 `main()` 里**无条件**应用它（只有 `--zk-delta-only` 跳过）。
- shim 还顺手把 `?"responses":"chat_completions"` 改成 `"responses"`（force-responses）。
  这就是本机 trace 里真实 Cursor 流量会打 `/v1/responses` 的原因 —— 说明它确实作用在真流量上，
  不只是探针路径。
- 本机那 4 发真实 Cursor 流量全是 `passthrough-full`（`inputCount` 恒为 3，没增长 ⇒ 不满足链式
  条件），81 次 `chained` 全来自 08-30 打 `cursor-web-fc-82-terra` 的探针（那时服务端还在）。
  **所以"同事一定在丢上下文"这句话没有数据支持，不许说**；能说的是：收益已确定为零，
  风险非零且不报错，留着没有任何理由。
- zk-delta 是**另一套机制**（自己的协议、集群侧逐字节重建、位置在 LiteLLM 之前），
  不涉及 `previous_response_id`，不受这次下线影响。

## 做了什么

`scripts/zk-cursor-web/cursor_team_setup.js`：

1. 新增 `--chain`。**默认不装** chain 补丁（服务端那半回来时用这个开关装回去，代码原样保留没删）。
2. 不带 `--chain` 时反过来做：`planChainRemoval()` 把**已装的摘掉**。同事双击一次
   `REPAIR-Mac.command`（= `--repair`）就恢复，不用手工操作、不碰 Key / 配置 / 选中模型。
3. 摘除是 `planChainBundle()` 的**精确逆操作**，四处子补丁全逆：头部 shim、`__cxWrap` 包装
   （括号配平反解）、`resp:t.fetch`、**force-responses 端点还原**。
   逆完必须 ①不含 `@cx-chain:v3` ②不含 `__cxWrap` ③过语法校验，才落盘；
   任一条不满足就保持原样并提示走 `--revert` 从备份还原 —— **绝不留半截**。

README 里加了一句给已装机器的指引（附件换新后同事双击一次修复器即可）。

## 验收（这次的判据是"逐字节可逆"，不是"没报错"）

新增测试钩子 `CX_CHAIN_ROUNDTRIP=<bundle>`：对一条真 bundle 跑「装 → 摘」往返，
断言逐字节回到原样。退出码分家：`0`=往返相同 / `14`=装不上 / `15`=摘失败 / `16`=往返有差异。

- 两条真 exthost bundle（`cursor-agent-exec` 10,396,698 字符 / `cursor-local-agent-runtime`
  9,317,097 字符）往返 **逐字节相同**，四处子补丁全部逆掉。
- **门会咬**（注入实测，不是"我觉得它会拦"）：
  - 缺陷①「忘了还原 force-responses」→ `exit 16`，并精确报出差异位置 `@8377068`；
  - 缺陷②「unwrap 少吃一个右括号」→ `exit 16`。
- 主流程（备份 → 落盘）用假 RES 树（`CURSOR_APP_ROOT` 测试钩子）造了一台"装过 chain 的机器"，
  跑真 `--repair`：dry-run 不落盘 → `--repair` 落盘并先备份 → 结果与未打补丁的原始文件
  **`cmp` 逐字节相同**，`@cx-chain:v3` 与 `__cxWrap` 残留均为 0。
- 幂等 + 逃生门：再跑一次无动作；`--chain --repair` 能装回去；「装→摘→装→摘」全程逐字节可逆。
- 分发包重建后逐字节校验：包内 `cursor_team_setup.js` / `zk-delta` 两文件与仓库一致，
  `.command` 执行位在，16 个条目无陈旧残留（`zip -r` 是追加，打包脚本已先 `rm -f`）。

## 顺带修掉的一个"坏尺子"

`chain_srv_offline_cases.js` 在全量回归里被喂了线上 `responses.js`，于是 8 条断言集体红。
那不是缺陷，是量错了对象 —— 而这种红最危险：下一步很容易变成"改产品去满足断言"。
加了目标闸，退出码分家：`0`=全过 / `1`=真缺陷 / `2`=喂错对象（N/A，打印为什么不适用）。
双向实测：喂线上产物 → `exit 2`；喂正确目标 → 21/21 `exit 0`；
往正确目标里注入缺陷 → `exit 1`（**没被 N/A 吞掉**）。

## 回滚

- 客户端：`cursor_team_setup.js --revert`（从 `~/.cursor-team-setup-backup/` 最近一份还原
  bundle + blob + settings + Key secret），或直接 `--chain --repair` 把 shim 装回去。
- 服务端那半要回来：`ZK_CHAIN_SRV` 四个补丁点 + `scripts/zk-cursor-web/chain_srv_offline_cases.js`
  （21 断言，目标是补丁后的工作产物），走 CM 15-key patch + 六条 lane 全量 rollout 那套纪律。
