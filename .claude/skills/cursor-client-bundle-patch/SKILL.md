# Cursor 客户端 bundle 补丁 SOP(macOS / 3.16.x)

对象:同事/本机 **Cursor.app 本体**的 workbench bundle 补丁与 BYOK 配置,与网关线
(zk-cursor-web-fc-iterate,改 198 CM)完全分层:**这边改的是用户 Mac 上的 Cursor,
那边改的是服务端 lane**。三个脚本全在 `scripts/zk-cursor-web/`:

| 脚本 | 用途 | marker |
|---|---|---|
| `cursor_team_setup.py` | 同事一键装 cursor-g(3 解锁补丁+排队泵+BYOK 配置+update.mode:none) | `@cxteam-*` + `@cx-queue-pump:v3` |
| `cursor_queue_pump_patch.py` | 单独打/升级排队泵(支持 v1/v2diag 原地升级 v3) | `@cx-queue-pump:v3` |
| `cursor_queue_diag_patch.py` | 诊断用:把泵换成"每秒打 tick 日志"版,只观测不改行为 | `@cx-queue-diag:v2` |

## 排队卡死 bug(原厂,2026-08-25 v3 端到端闭环)

**真形态(诊断 tick 实测,推翻两轮猜想)**:一轮 `Stream completed successfully` 之后,
composer status 仍=`generating` 且 `chatGenerationUUID` **残留**(hasUUID=true/bubbles=0
持续 2min+),后续消息进 addToQueue 队列后永不派发=静默丢。该负载下**几乎每轮完成后都卡**
(收尾复位被跳过,伴随 "Failed to resolve hook model legacy slug" 报错)。重启不救——
启动会自动重放队列并原样复现。

**为什么难判**:真生成时 hasUUID 也 true、bubbles 也 0——光看形态分不清真假。
**铁判据(抄 bundle 自己的口径)**:`aiService.streamingAbortControllers.has(uuid)`——
每个真生成登记控制器,流一结束(完成/中止两条路径)必被 `.delete()`。

**v3 修法**(泵 tick 内,泵类可达链 `this.composerChatService._aiService.streamingAbortControllers`):
- uuid 在但表里没它 = 僵尸标记 → 连续 **3 tick** 确认后
  `updateComposerData({status:"completed", chatGenerationUUID:void 0, generatingBubbleIds:[]})`
- 表里有它 = 真在生成 → 绝不碰(实测 70s 长生成零误杀)
- 摸不到表(未来版本改名)= 退化 v1 只清无 uuid 形态(fail-safe 不更坏)

**验收判据**:结构化日志 `[cx-queue-pump] healed stuck generating status` 带 `hadUUID` 字段;
端到端=积压队列逐条自动派发(完成→~3s heal→下一条发出→完成…)。日志位置:
`~/Library/Application Support/Cursor/logs/<session>/window*/exthost/anysphere.cursor-always-local/*Structured*.log`

## 诊断方法论(内存态读不到时,可复用)

composer 运行时状态只活在 renderer 内存,workspace state.vscdb 里没有——**磁盘取证无效**。
正解三步:①先上**只加日志、不改行为**的 tick 补丁(diag 脚本,逐字节替换现行泵);
②复现:不需要手工发消息——**重启 Cursor 即触发启动重放**(恢复队列+重放提交),卡死自己复现;
③读 tick(status/hasUUID/bubbles/queueLen 每秒一行)定形态,再写修法。禁止跳过①③直接猜。

## 硬规矩

- **动 bundle 前 Cursor 必须完全退出**;卡死时 osascript quit 和 SIGTERM 都可能不响应
  (优雅退出被挂死的流卡住)→ 只能 `kill -9`,聊天记录持续落盘不丢。
- **锚点纪律**:稳定语义地标+通用捕获 minify 尾巴,全文件命中 ≠1 → 拒绝动手;
  落盘前整 bundle `node --check`。装过 CursorX 的机器 3 解锁锚点命中 0 → 自动拒(防重复打)。
- **`update.mode:none` 必须保持**:Cursor 自动升级掀翻一切 bundle 补丁。
- **snippet 单一来源**:`cursor_team_setup.py` 的 QP_SNIPPET 必须与 `cursor_queue_pump_patch.py`
  的 SNIPPET 逐字节一致(改一处必同步另一处并跑等价断言);升级泵版本时把旧版原文加进
  OLD_SNIPPETS 以支持原地升级。
- 回滚:各脚本 `--revert`(备份链 `~/.cursor-team-setup-backup` / `~/.cursor-queue-pump-backup`
  / `~/.cursor-queue-diag-backup`)。
- key 不自动写(macOS 钥匙串加密,外部写=弹框或假绿 401),唯一人工步=Cursor 设置里粘 key。

## marker 演进

`@cx-queue-pump:v1`(只清无 uuid,救一半)→ `@cx-queue-diag:v2`(诊断 tick,用完即替)→
`@cx-queue-pump:v3`(现行,僵尸 uuid 判活)。记忆:
`feedback_cursor_queue_stuck_zombie_uuid_v3_liveness`;安装器设计与等价性证明:
`docs/cursor-g-naming-rollout-20260824.md` §七。
