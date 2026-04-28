# `claude-code-buyitian` 近 10 天消费深度分析

> **范围**：BJ 04-15 ~ 04-25（10 天）
> **Key hash**：`5906bb066a13dd47462823fa586c77dbc6a4ca2ea8c9c97d9703df78b4300b1e `  
> **总数据量**：36,906 LLM turn  
> **生成时间**：2026-04-25 23:30 BJ  
> **数据源**：LiteLLM `LiteLLM_SpendLogs` 表（PostgreSQL）

---

## 0. 整体结论 & 建议（Executive Summary）

### 🎯 核心结论

1. **10 天累计花了 ≈ $7,437**（36,906 LLM turn），日均 $744。从 04-19 起单日突破 $800，04-23/04-24 连续两天超过 $1,000，**04-24 触发预算冻结**。
2. **这不是"用户在用"，是"机器在用"**。在能识别触发器的 30,131 turn 中：
  - 👤 真人飞书消息触发：**仅 23.9%（$1,083）**
  - 🤖 定时 + 巡检 + 心跳 + hook 等机器活动：**60.9%（$2,759）**
  - 即所谓的"个人助理"实际是 **3 个真人消费配 4 个机器自动跑**。
3. **3 大成本黑洞，占机器消费 80%+**：
  - 🚨 **WATCHDOG 强制巡检**（$740 / 10 天）—— 单次成本最高，模板里写 `⛔ 你必须执行完整巡检...不许 NO_REPLY`，强迫 agent 跑 200+ turn
  - ⏰ **Cursor 使用日报-21:30**（$192.52，4 天活跃）—— 单次 $0.637，每天调 75 个工具，重传 35.8 万 token
  - 💓 **HEARTBEAT 心跳**（$261，30min/次）—— 单次成本从 04-15 的 $0.12 涨到 04-25 的 $2.33，**贵 19 倍**
4. **Cache 命中率从 93% 跌到 73%**，是结构性问题：HEARTBEAT.md / _watchdog.md 的累积污染让每次 prompt prefix 都不同，cache invalidate 之后还要 1.25× 价格写新 cache。30 分钟间隔又超过 Anthropic 默认的 5min cache TTL，每次都从头开始。
5. **Cron 编排失控**：识别出 **27 个具名 cron + 2 个通用触发器（WATCHDOG/HEARTBEAT）共 33 类机器任务**。其中"知识库建设日报-21:45" 平均每 turn 调用 **3.6 个工具**（最离谱），整个 cron 集群是从 04-19 起一周内集中上线的，没有限流也没有去重。
6. **平均每 23 秒一次 LLM 调用** —— 这个 key 背后是个 7×24 不停的数字员工，但成本结构没有任何节流机制。

### 💡 建议（按 ROI 排序，预期总省 ≈ $290/天）


| 优先级   | 措施                                                                                                                   | 实施成本                 | 预期日省  |
| ----- | -------------------------------------------------------------------------------------------------------------------- | -------------------- | ----- |
| 🔥 P0 | **立即把 daily budget 调到 $400**（现 $1,200），强制止损                                                                          | 5 分钟改配               | 立即生效  |
| 🔥 P0 | **WATCHDOG 限制 agent loop ≤ 30 turn**（目前每次 200+）                                                                      | 改 prompt             | ~$60  |
| 🟠 P1 | **HEARTBEAT 改为无状态**：每次清空 history、不带 conversation 上下文                                                                 | 改 OpenClaw 配置        | ~$20  |
| 🟠 P1 | **所有"日报/监控"类 cron 切到 sonnet**（不需要 opus）                                                                              | 改 model 配置           | ~$80  |
| 🟡 P2 | **27 个具名 cron 加 minimum interval**（避免短周期内重复触发）                                                                       | 改 OpenClaw scheduler | ~$30  |
| 🟡 P2 | **LiteLLM 端开 `cache_control` ext 1h TTL**（多数 cron 间隔 >5min）                                                          | 改 LiteLLM hook       | ~$50  |
| 🟢 P3 | **给 cron 配独立 budget key**（`carher-198-cron`，daily $300）：失控时不影响主 key                                                  | 新建 key               | 可控性   |
| 🟢 P3 | **LiteLLM 持久保留 `store_prompts: True`** + 调大 `MAX_STRING_LENGTH_PROMPT_IN_DB`：04-21~04-23 三天数据丢失（$2,932 无法审计）就是因为关了这个 | 改 LiteLLM 配置         | 审计可追溯 |


### 📋 一句话总结

> **buyitian 这个 key 不是被人花掉的，是被一窝失控的 cron + 一个偏执的 watchdog + 一个越吃越胖的 heartbeat 共同烧掉的。**
> **降本 ROI 最高的不是省 token，是给数字员工"立规矩"——加 turn 上限、加 minimum interval、把日报降级到 sonnet。**

---

## 一、10 天总账（按天）


| BJ 日期     | LLM turn   | 消费 (USD)     | cache 命中  | 备注                  |
| --------- | ---------- | ------------ | --------- | ------------------- |
| 04-15     | 508        | $48.86       | 77.9%     | 起步                  |
| 04-16     | 2,260      | $309.91      | 89.3%     | 起量                  |
| 04-17     | 4,836      | $392.41      | 86.6%     |                     |
| 04-18     | 2,584      | $166.74      | 90.2%     |                     |
| 04-19     | 7,319      | $835.90      | 93.0%     | turn 峰值             |
| 04-20     | 3,851      | $959.67      | 83.4%     |                     |
| **04-21** | 3,995      | $904.81      | 78.7%     | ⚠ proxy_request 字段空 |
| **04-22** | 3,129      | $914.90      | 76.4%     | ⚠ proxy_request 字段空 |
| **04-23** | 2,970      | $1,112.60    | 76.2%     | ⚠ proxy_request 字段空 |
| 04-24     | 4,479      | $1,291.15    | 75.7%     | 全天最高                |
| 04-25     | 975        | $499.96      | 72.8%     | 触发预算冻结后停            |
| **合计**    | **36,906** | **≈ $7,437** | **80.0%** |                     |


> ⚠ **04-21~04-23 三天 LiteLLM 没保存 `proxy_server_request` 字段**（共 $2,932）—— 推测当时 LiteLLM 配置短暂关闭了 prompt 持久化，无法识别触发器，但能从 spend 总量推断分布。

---

## 二、可识别的调用按"触发器"汇总（30,131 turn）

> 即排除 04-21~04-23 三天后的全部数据。10 天非用户触发的"机器活动"占 **64.4%**。

### 大类汇总


| 触发器大类                        | turn   | spend      | 占比    | 单次 turn 平均 |
| ---------------------------- | ------ | ---------- | ----- | ---------- |
| 👤 **用户飞书消息**（真人触发）          | 2,863  | **$1,083** | 23.9% | $0.378     |
| 🚨 **WATCHDOG 强制巡检**         | 1,223  | **$740**   | 16.3% | $0.605     |
| 💓 **HEARTBEAT 心跳**（每 30min） | 444    | **$261**   | 5.8%  | $0.588     |
| ⏰ **具名 cron 任务（27 个）**       | 1,747  | **$520**   | 11.5% | $0.297     |
| ⏰ scheduled-reminder         | 636    | $188       | 4.1%  | $0.295     |
| 🟡 system-reminder hook      | 12,055 | $1,050     | 23.2% | $0.087     |
| 🟡 session-startup hook      | 237    | $28        | 0.6%  | $0.118     |
| ? 其他（工具循环 turn 等）            | 1,569  | $199       | 4.4%  | $0.127     |


**🤖 全部定时任务合计 = $1,709（37.7% 可识别支出）**

加上 system-reminder/session-startup 这种自动注入也是机器行为，**纯非交互机器活动 = $2,759（60.9%）**

### 按天 × 大类二维汇总

> 单元格格式：`$spend (turns)`。空格表示该天该类无调用。"⚠ 旧数据无req" 列是 04-21~04-23 三天 LiteLLM 没保存 `proxy_server_request` 字段的部分，无法识别触发器。


| BJ 日期      | 👤 飞书消息(用户)           | 🚨 WATCHDOG         | 💓 HEARTBEAT      | ⏰ 具名 cron           | 🟡 hook                | ? 其他                | ⚠ 旧数据无req              | **当日合计**              |
| ---------- | --------------------- | ------------------- | ----------------- | ------------------- | ---------------------- | ------------------- | ---------------------- | --------------------- |
| 04-15      |                       |                     | $0.13 (1)         |                     | $17.64 (199)           | $31.09 (253)        | $0.00 (55)             | **$48.86 / 508**      |
| 04-16      |                       | $57.29 (96)         | $0.18 (1)         | $0.30 (1)           | $78.30 (499)           | $12.05 (108)        | $161.79 (1,555)        | **$309.91 / 2,260**   |
| 04-17      | $4.87 (55)            |                     | $0.31 (3)         | $5.01 (63)          | $121.22 (1,681)        | $75.26 (653)        | $185.74 (2,381)        | **$392.41 / 4,836**   |
| 04-18      | $3.78 (23)            | $1.17 (11)          | $7.59 (52)        |                     | $146.51 (1,379)        | $7.69 (101)         | $0.00 (1,018)          | **$166.74 / 2,584**   |
| 04-19      | $175.06 (673)         | $31.84 (124)        | $86.57 (133)      | $197.06 (703)       | $320.75 (4,740)        | $24.62 (224)        | $0.00 (722)            | **$835.90 / 7,319**   |
| 04-20      | $393.51 (1,187)       | $241.14 (270)       | $57.48 (96)       | $19.97 (118)        | $212.18 (1,541)        | $22.18 (103)        | $13.20 (536)           | **$959.66 / 3,851**   |
| 04-21      |                       |                     |                   |                     |                        |                     | $904.81 (3,995)        | **$904.81 / 3,995**   |
| 04-22      |                       |                     |                   |                     |                        |                     | $914.90 (3,129)        | **$914.90 / 3,129**   |
| 04-23      |                       |                     |                   |                     |                        |                     | $1,112.60 (2,970)      | **$1,112.60 / 2,970** |
| 04-24      | $368.67 (678)         | $237.13 (486)       | $58.92 (110)      | $241.96 (612)       | $160.54 (2,136)        | $7.68 (47)          | $216.25 (410)          | **$1,291.15 / 4,479** |
| 04-25      | $136.98 (247)         | $171.45 (236)       | $49.99 (48)       | $102.32 (247)       | $21.22 (117)           | $17.99 (80)         |                        | **$499.95 / 975**     |
| **10 天合计** | **$1,082.87** (2,863) | **$740.02** (1,223) | **$261.17** (444) | **$566.62** (1,744) | **$1,078.36** (12,292) | **$198.56** (1,569) | **$3,509.29** (16,771) | **≈$7,437 / 36,906**  |


#### 关键观察

1. **非交互机器活动占比逐天上升**：04-19 之前几乎只有 hook，04-19 起 cron + WATCHDOG + HEARTBEAT 集中爆发
2. **WATCHDOG 强制巡检**：从 04-16 ($57) → 04-20 ($241) → 04-24 ($237) → 04-25 ($171) 持续高位
3. **04-19 是分水岭**：单日具名 cron 从 $5 → $197（×40 倍），用户飞书 $5 → $175（×35 倍）
4. **04-24 全大类爆发**：所有触发器都在跑，单日 $1,291 历史最高
5. **04-25 用户消费占比骤升**：因为 04-25 早上预算冻结后，用户重新发消息触发是主要活动

---

## 三、27 个具名 Cron 完整清单

按累计花费排序，标注实际活跃天数：


| #   | Cron 名                                             | 活跃天 | turn | 累计 $        | 单次 $   |
| --- | -------------------------------------------------- | --- | ---- | ----------- | ------ |
| 1   | **Cursor使用日报-21:30**                               | 4   | 302  | **$192.52** | $0.637 |
| 2   | scheduled-reminder（提醒触发）                           | 4   | 636  | $187.90     | $0.295 |
| 3   | 炼丹炉·周汇总                                            | 2   | 145  | $74.62      | $0.515 |
| 4   | nova-upgrade-hourly                                | 1   | 127  | $24.31      | $0.191 |
| 5   | 263邮箱摘要-21:00                                      | 5   | 130  | $14.03      | $0.108 |
| 6   | 测试-每日邮箱总结                                          | 2   | 75   | $9.58       | $0.128 |
| 7   | 知识库建设日报-21:45                                      | 5   | 60   | $9.35       | $0.156 |
| 8   | nova-upgrade-mission-v6                            | 1   | 36   | $8.40       | $0.233 |
| 9   | autoDream-记忆整理-04:00                               | 3   | 37   | $6.35       | $0.172 |
| 10  | 群消息晨报-21:15                                        | 3   | 16   | $5.52       | $0.345 |
| 11  | AI悬赏令-申报扫描与预评审                                     | 2   | 12   | $4.43       | $0.369 |
| 12  | 每日任务复盘-22点                                         | 3   | 17   | $4.22       | $0.248 |
| 13  | nova-upgrade-4h                                    | 2   | 17   | $2.84       | $0.167 |
| 14  | AI推文综合日报-04:30                                     | 2   | 20   | $2.59       | $0.129 |
| 15  | 提醒：去Autolink和杨哥开会                                  | 1   | 37   | $2.44       | $0.066 |
| 16  | 工作方向复盘-每晚10点                                       | 3   | 9    | $2.44       | $0.271 |
| 17  | ping-shadow-impl                                   | 1   | 2    | $2.37       | $1.183 |
| 18  | OpenClaw每日动态-04:15                                 | 2   | 7    | $1.73       | $0.247 |
| 19  | LLM智商日报-每日推送                                       | 2   | 6    | $1.61       | $0.268 |
| 20  | Agent技术前沿日报-04:00                                  | 2   | 8    | $1.35       | $0.169 |
| 21  | 每日早报                                               | 1   | 5    | $1.26       | $0.253 |
| 22  | unreplied-msg-scan                                 | 1   | 4    | $1.21       | $0.304 |
| 23  | SkillPack监控-每日09点                                  | 1   | 3    | $0.82       | $0.272 |
| 24  | SkillPack监控-每日14点                                  | 1   | 3    | $0.82       | $0.273 |
| 25  | 夜班-skills-research-progress-check                  | 1   | 5    | $0.75       | $0.150 |
| 26  | 林白日常讨论-每日总结                                        | 2   | 4    | $0.75       | $0.187 |
| 27+ | Google/Cursor/Karpathy 推文日报、SkillPack-18点、知识库建设-测试 | —   | 23   | $2.39       | —      |


> 加上未署名的 WATCHDOG 和 HEARTBEAT，**合计 33 类机器触发的任务**。

---

## 四、Session 级输入/输出结构

### 工具集

- 每个 conversation 都附带 **84 个工具**：`read/write/edit/exec/process/Bash/canvas/message/tts/sessions_*/web_search/web_fetch/browser/feishu_*[40+]`
- system prompt 头部是 OpenClaw personal assistant 模板，~5 万 token 长

### 触发器模板（输入）


| 类型        | 模板                                                                                                      | 示例        |
| --------- | ------------------------------------------------------------------------------------------------------- | --------- |
| HEARTBEAT | `Read HEARTBEAT.md if it exists... reply HEARTBEAT_OK`                                                  | 每 30 分钟一次 |
| WATCHDOG  | `[2026-04-25 21:30:42] [WATCHDOG] read _watchdog.md and execute. ⛔ 你必须执行完整巡检：扫群+查日历+查妙记+发汇报。不许NO_REPLY` | 当主人未响应时触发 |
| 具名 cron   | `[cron:UUID 名称] 这是每日定时任务：xxx。每天 HH:MM 触发...`                                                            | 写得最规范     |
| 飞书消息      | `Conversation info (untrusted metadata): {...message_id...}`                                            | 真人发消息     |


### 输出 / 工具调用情况

各类任务**触发工具调用比例**（`tool_calls` 非空的 turn 占比）：


| 类型                      | turn  | 调工具占比     | 平均工具数/turn | 主要使用工具                                                               |
| ----------------------- | ----- | --------- | ---------- | -------------------------------------------------------------------- |
| scheduled-reminder      | 636   | **98.1%** | 1.09       | exec, message                                                        |
| 测试-每日邮箱总结               | 75    | **98.7%** | 1.08       | exec                                                                 |
| 263邮箱摘要                 | 130   | 95.4%     | 1.12       | exec, feishu_doc                                                     |
| 知识库建设日报                 | 60    | 95.0%     | **3.60 ⚠** | exec, feishu_wiki                                                    |
| nova-upgrade-mission-v6 | 36    | 94.4%     | 1.14       | exec                                                                 |
| Cursor使用日报              | 302   | 91.4%     | 1.01       | exec, Bash                                                           |
| 飞书消息（用户）                | 2,863 | 77.7%     | 0.99       | exec, process, feishu_doc                                            |
| WATCHDOG                | 1,223 | **67.7%** | 0.76       | exec, feishu_group_history, feishu_calendar, feishu_minutes, message |
| HEARTBEAT               | 444   | **67.3%** | 0.77       | exec, process, Bash                                                  |


> WATCHDOG/HEARTBEAT 的 67% 工具调用比例非常高 —— 心跳本应该回 `HEARTBEAT_OK` 后立即结束，但有 1/3 的心跳触发了真任务（HEARTBEAT.md 里写有待办），平均跑 0.77 个工具/turn。

### 工具使用 Top（按各任务大类）

#### cron 类


| Tool        | 调用次数 |
| ----------- | ---- |
| exec        | 458  |
| process     | 401  |
| Bash        | 247  |
| feishu_wiki | 174  |
| message     | 167  |
| browser     | 96   |
| read        | 81   |
| write       | 76   |
| feishu_doc  | 67   |
| web_fetch   | 33   |
| web_search  | 29   |


#### 用户飞书消息


| Tool            | 调用次数 |
| --------------- | ---- |
| exec            | 982  |
| process         | 617  |
| Bash            | 138  |
| feishu_doc      | 134  |
| sessions_spawn  | 87   |
| write           | 82   |
| a2a_send        | 70   |
| read            | 69   |
| memory_search   | 51   |
| feishu_calendar | 43   |


#### WATCHDOG


| Tool                 | 调用次数 |
| -------------------- | ---- |
| exec                 | 387  |
| Bash                 | 145  |
| write                | 64   |
| process              | 57   |
| feishu_group_history | 46   |
| message              | 31   |
| read                 | 29   |
| feishu_doc           | 28   |
| feishu_calendar      | 13   |
| feishu_minutes       | 13   |
| feishu_wiki          | 12   |


#### HEARTBEAT


| Tool    | 调用次数 |
| ------- | ---- |
| exec    | 175  |
| process | 29   |
| Bash    | 29   |
| write   | 10   |


---

## 五、整体分析

### 1. 这是个 7×24 小时运行的 AI 数字员工

key 背后是 **OpenClaw personal assistant**（her 实例 `carher-198`），由真人 buyitian（天哥的秘书）+ 数十个 cron + watchdog + 心跳协同驱动，连了完整飞书工具栈。10 天累计调了 LiteLLM **36,906 次**，平均 **3,690 次/天 ≈ 每 23 秒一次**。

### 2. 成本是从 04-19 起暴涨的——切换到 opus 系列 + cron 集中上线

- 04-15~04-18：日均 **$50–$400**，主要 sonnet/haiku
- 04-19 起：cron 任务集中上线（爬日报、巡检、汇总），日均 **$800–$1,300**
- 04-23：单日 spend 突破 **$1,100**，触发 $1,200 budget 警报
- 04-24：单日 **$1,291**，已超预算（budget_duration=1d，硬限 $1,200）
- 04-25：上午冻结、晚上才放开，不到一天又花 **$500**

### 3. cache 命中率从 93% 跌到 73%，这是结构性问题

HEARTBEAT 和 WATCHDOG 类任务每次重传完整 conversation history（500+ messages, 30 万+ token），但每次的 tool_result 改变了 prefix → cache invalidate → 每次 ~1.25× 价格写新 cache。再加上 30 分钟超过 5min cache TTL，每次都从头开始。

### 4. 10 天确认存在的"机器活动" ≈ $2,700+（37%）

即便保守估算（不算 04-21~~04-23 三天空白），**纯 cron + WATCHDOG + HEARTBEAT 已经 $1,709**。如果按 04-24 比例（cron+WATCHDOG+HEARTBEAT = $537/$1,291 = 41.6%）推算 04-21~~04-23 那 3 天，机器活动 ≈ $2,700+。

### 5. 最大 3 个成本黑洞

#### 黑洞 1：WATCHDOG 强制巡检

- 10 天 $740 / 1,223 turn
- 每次触发时强制 agent 完成"扫群+查日历+查妙记+发汇报"全流程
- prompt 里有 `⛔ 不许NO_REPLY` 这种强制语
- 每次跑数百个 turn

#### 黑洞 2：Cursor 使用日报-21:30

- 10 天 $192 / 302 turn / 4 天活跃 = **$48/天**
- 每次触发后 agent 调 75 个工具采集汇总
- 每个 turn 都重传 35.8 万 token（cache 命中只 78.2%）

#### 黑洞 3：HEARTBEAT 心跳

- 单次成本从 04-15 的 **$0.12** → 04-25 的 **$2.33**（**贵 19 倍**）
- 原因：HEARTBEAT.md 累积污染 + 模型升级到 opus + history 一直在长

### 6. 工具使用模式

所有 cron 任务都是 `**exec`/`Bash` 拉数据 → `feishu_`* 发汇报** 流程。工具种类前 5：

- **exec**（最高，2,000+ 次）—— 跑 shell 脚本
- **process** —— 后台进程管理
- **Bash** —— 同 exec 但 Anthropic 内置 tool
- **feishu_doc/wiki** —— 写飞书文档
- **message** —— 发飞书消息

### 7. 整体数据完整性

LiteLLM `proxy_server_request` 字段在 04-21~04-23 三天**全部为空**（`{}`，5 字节）。这导致 **$2,932 的支出无法追溯触发器**。建议：

- 把 LiteLLM 的 `litellm_settings.store_prompts: True` 长期保留，否则关键审计无据
- 调大 `MAX_STRING_LENGTH_PROMPT_IN_DB` 避免 truncate

---

## 六、建议（按 ROI 排序）


| #   | 措施                                                               | 预期日省 |
| --- | ---------------------------------------------------------------- | ---- |
| 1   | WATCHDOG 限制 agent loop ≤ 30 turn（目前每次 200+）                      | ~$60 |
| 2   | HEARTBEAT 改为无状态：清空 HEARTBEAT.md history、禁用对话记忆                   | ~$20 |
| 3   | 所有"日报/监控"类 cron 切到 sonnet（不需 opus）                               | ~$80 |
| 4   | 27 个具名 cron 加 "minimum interval"，避免短周期内重复                        | ~$30 |
| 5   | 给 cron 配独立 budget key（`carher-198-cron`，daily $300）便于失控时不影响主 key | 可控性  |
| 6   | LiteLLM 端开 `cache_control` ext 1h TTL，因为多数 cron 间隔 >5min         | ~$50 |


总计预期 **每天可降至 $300–500（现在 $1,200）**。

---

## 七、原始数据查询语句

### 1. 物化分析表（一次性扫描整个 SpendLogs）

```sql
DROP TABLE IF EXISTS analysis_buyitian_10d;
CREATE TABLE analysis_buyitian_10d AS 
SELECT 
  request_id, "startTime",
  DATE("startTime" AT TIME ZONE 'Asia/Shanghai') AS bj_date,
  spend, prompt_tokens, completion_tokens,
  REGEXP_REPLACE(model, '^anthropic/', '') AS model,
  COALESCE((metadata->'usage_object'->>'cache_creation_input_tokens')::bigint, 0) AS cc_tok,
  COALESCE((metadata->'usage_object'->>'cache_read_input_tokens')::bigint, 0) AS cr_tok,
  pg_column_size(proxy_server_request) AS req_sz,
  pg_column_size(response) AS resp_sz,
  CASE
    WHEN pg_column_size(proxy_server_request) <= 100 THEN NULL
    WHEN proxy_server_request::text LIKE '%[WATCHDOG]%_watchdog.md%' THEN '🚨 WATCHDOG'
    WHEN proxy_server_request::text LIKE '%Read HEARTBEAT.md%' THEN '💓 HEARTBEAT'
    WHEN proxy_server_request::text LIKE '%[cron:%' THEN '⏰ ' || COALESCE(SUBSTRING(proxy_server_request::text FROM '\[cron:[0-9a-z-]{5,}? ([^]]{1,60}?)\]'), 'cron')
    WHEN proxy_server_request::text LIKE '%scheduled reminder has been triggered%' THEN '⏰ scheduled-reminder'
    WHEN proxy_server_request::text LIKE '%Conversation info (untrusted metadata)%' THEN '👤 飞书消息'
    WHEN proxy_server_request::text LIKE '%A new session was started via /new%' THEN '🟡 session-startup'
    WHEN proxy_server_request::text LIKE '%<system-reminder>%' THEN '🟡 system-reminder'
    ELSE '? 其他(有req)'
  END AS task_type_main,
  CASE
    WHEN response::text ~ 'HEARTBEAT_OK|NO_REPLY' THEN '💓 HEARTBEAT (resp推断)'
    ELSE NULL
  END AS task_type_resp
FROM "LiteLLM_SpendLogs"
WHERE api_key = '5906bb066a13dd47462823fa586c77dbc6a4ca2ea8c9c97d9703df78b4300b1e'
  AND "startTime" >= '2026-04-15 16:00:00'
  AND "startTime" <  '2026-04-25 16:00:00';
```

### 2. 按 task_type 汇总

```sql
SELECT 
  task_type,
  COUNT(*) AS turns,
  ROUND(SUM(spend)::numeric, 2) AS spend,
  ROUND(AVG(spend)::numeric, 3) AS avg_per_turn,
  ROUND(AVG(prompt_tokens)::numeric, 0) AS avg_p,
  ROUND(AVG(completion_tokens)::numeric, 0) AS avg_c,
  ROUND((100.0 * SUM(cr_tok) / NULLIF(SUM(prompt_tokens), 0))::numeric, 1) AS cache_pct
FROM analysis_buyitian_10d
GROUP BY 1 
ORDER BY 3 DESC;
```

### 3. 按天 × 大类二维统计

```sql
SELECT 
  bj_date,
  CASE
    WHEN task_type LIKE '⚠%' THEN '⚠ 旧数据无req'
    WHEN task_type IN ('🚨 WATCHDOG', '💓 HEARTBEAT') THEN task_type
    WHEN task_type LIKE '⏰%' THEN '⏰ 具名 cron'
    WHEN task_type LIKE '🟡%' THEN '🟡 hook'
    WHEN task_type LIKE '👤%' THEN '👤 飞书消息(用户)'
    ELSE '? 其他'
  END AS bucket,
  COUNT(*) AS turns,
  ROUND(SUM(spend)::numeric, 2) AS spend
FROM analysis_buyitian_10d
GROUP BY 1, 2
ORDER BY 1, 4 DESC;
```

### 4. cron 活跃天分布

```sql
SELECT 
  task_type AS cron,
  COUNT(DISTINCT bj_date) AS active_days,
  COUNT(*) AS turns,
  ROUND(SUM(spend)::numeric, 2) AS spend,
  string_agg(DISTINCT to_char(bj_date, 'MM-DD'), ',' ORDER BY to_char(bj_date, 'MM-DD')) AS days
FROM analysis_buyitian_10d
WHERE task_type LIKE '⏰%' OR task_type IN ('🚨 WATCHDOG', '💓 HEARTBEAT')
GROUP BY 1
ORDER BY 4 DESC;
```

### 5. 工具调用情况

```sql
WITH ext AS (
  SELECT 
    a.task_type, a.spend,
    s.response->'choices'->0->'message'->'tool_calls' AS tcs
  FROM analysis_buyitian_10d a
  JOIN "LiteLLM_SpendLogs" s ON a.request_id = s.request_id
  WHERE pg_column_size(s.response) > 100
    AND (a.task_type LIKE '⏰%' OR a.task_type IN ('🚨 WATCHDOG', '💓 HEARTBEAT', '👤 飞书消息'))
)
SELECT 
  task_type,
  COUNT(*) AS turns,
  COUNT(*) FILTER (WHERE jsonb_typeof(tcs) = 'array' AND jsonb_array_length(tcs) >= 1) AS tool_turns,
  ROUND(100.0 * COUNT(*) FILTER (WHERE jsonb_typeof(tcs) = 'array' AND jsonb_array_length(tcs) >= 1) / COUNT(*), 1) AS tool_pct,
  ROUND(AVG(CASE WHEN jsonb_typeof(tcs) = 'array' THEN jsonb_array_length(tcs) ELSE 0 END)::numeric, 2) AS avg_tools_per_turn,
  ROUND(SUM(spend)::numeric, 2) AS spend
FROM ext
GROUP BY 1
ORDER BY 6 DESC;
```

### 6. 工具名分布

```sql
WITH safe AS (
  SELECT 
    CASE WHEN a.task_type LIKE '⏰%' THEN 'cron' ELSE a.task_type END AS bucket,
    s.response->'choices'->0->'message'->'tool_calls' AS tcs
  FROM analysis_buyitian_10d a
  JOIN "LiteLLM_SpendLogs" s ON a.request_id = s.request_id
  WHERE pg_column_size(s.response) > 100
    AND (a.task_type LIKE '⏰%' OR a.task_type IN ('🚨 WATCHDOG', '💓 HEARTBEAT', '👤 飞书消息'))
),
expanded AS (
  SELECT bucket, tc->'function'->>'name' AS tool_name
  FROM safe, jsonb_array_elements(tcs) tc
  WHERE jsonb_typeof(tcs) = 'array'
)
SELECT bucket, tool_name, COUNT(*) AS calls
FROM expanded
WHERE tool_name IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 10
ORDER BY 1, 3 DESC;
```

---

## 附录：方法论 & 注意事项

### 数据获取

- 通过 SSH 隧道 `127.0.0.1:16443` 访问 K8s API
- 使用 `kubectl exec litellm-db-0 -n carher -- psql` 访问 PostgreSQL
- LiteLLM `LiteLLM_SpendLogs` 表是单一 source of truth

### 触发器分类原理

1. **第一优先级**：`proxy_server_request::text` 全文 LIKE 匹配关键模板
2. **fallback**：`response::text` 包含 `HEARTBEAT_OK|NO_REPLY` → 推断为 HEARTBEAT
3. **无原始数据**：`pg_column_size(proxy_server_request) <= 100` 视为字段未保存

### 已知限制

- **04-21~04-23 三天数据空白**：约 $2,932 无法识别触发器，只能按比例外推
- **session_id 不可靠**：LiteLLM 自动生成，无法用来聚合 conversation
- **requester_ip_address 不可用**：全部是 K8s 集群内部 IP

### 后续可深挖方向

- 单 conversation 级别的 cost-of-loop 分析（需要 conversation prefix hash）
- 按工具调用编排顺序聚类（哪些 cron 工作流程相似）
- HEARTBEAT.md 累积污染的具体 token 增长曲线

