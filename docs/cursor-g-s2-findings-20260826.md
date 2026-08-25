# S2 侦察结论:responses.js 实态 + 隔离 + 账号映射 + 金丝雀钉死(2026-08-26)

> 证据全部来自 198 实拉(CM md5 `b7124ad8...`,responses.js **1587 行**,比记忆快照多 ~530 行)。
> 本地副本:`/tmp/responses_live_20260826.js`(易失,重看请重拉)。

## 1. 五处现状 → 契约替换映射(S4 的施工图)

| # | 现状机制 | file:line(live) | 行为 | 拟被哪条契约替换 |
|---|---|---|---|---|
| 1 | 信封收割+流闸 | `looksLikeEnvelope` :682-724;收割编译 :982-1029(ask/spawn/exec 三路,含 ⟦⟧ compileToExec) | 流中冻结疑似信封,finish() 统一收割编译成 function_call/custom_tool_call | **保留骨架**(这就是"闭合才收割"),收割成功=轮次合法产物①,判据并入三分法 |
| 2 | near-empty 救 | :1110-1146(`_ftLen===0 一律救;<24 且 !hadToolResult 才救`,fresh-conv 全量重问) | 长度启发式→剥 [EXECUTION ENVIRONMENT]→freshConv 重问→saveConvSession 重存 | **删长度门**。空/短不再特判:无信封+无终止记号=incomplete→走统一重建;有终止记号的短答=合法 |
| 3 | 只说不做检测 | :1147-1194(`_REFUSE_RE/_PLAN_RE` 正则,kick 重问,收割/信封/prose 三出口) | prose 语义正则猜"拒答/口头计划"→同会话强制行动重问一次 | **降级为分类器输入**:拒答文本→归"显式失败"桶(可带 kick 重试一次,预算内);不再是独立补丁路径 |
| 4 | 信封 miss 兜底 | :1195-1198(**ZK_WEB_RETRY 默认关→直接 returning first pass**);:1199-1214 escalated retry(开关开才走) | **垃圾交付点在此**:默认路径把 1 字符 first pass 原样交给 Cursor | **删除此分支**。miss=incomplete→重建(预算内)→耗尽=诚实报错。ZK_WEB_RETRY 开关废弃 |
| 5 | 升级重试(bpi线) | :963-979(`needsEscalation`,一次) + ⟦块重问 :1090-1099 | 又两条独立重试路,各有自己的开关与计数 | 并入统一重试预算(同一个计数器,不再各数各的) |
| ⑤ | conv 落账 | `_convCache` 进程内 Map+TTL240min :255-296;saveConvSession :287;重存点 :1137/:1172 | **进程内非持久**;真实历史=上游对话树(convId+parentId 链) | 断点续跑=同 conv 同 parent 重问(上游树天然容忍 fork);**已收割信封已 emit 给 Cursor 的轮次不存在重建问题**(重建只发生在未出字节的窗口,原则 3 保证) |
| + | call_id 现状 | :986/:1002/:1012/:1388(`crypto.randomBytes` 随机);回程 `[TOOL RESULT ${call_id}]` :99 | **call_id 已存在但随机**,每次生成不同;回程配对靠 Cursor 带回 | 保留生成,但**孤儿合成物必须确定性 id**(codex normalize.rs:19 教训);出口 normalize 新增 |

**结构性认知修正**:记忆里的"BPI 两趟设计(pass1 收割/pass2 答)"在当前代码里**不是固定两趟**,而是
"一趟 + 级联条件重试"(escalate/near-empty/act-retry/envelope-retry/⟦-retry 五条各自独立的重试路,
各有开关各有计数)。S4 的"每趟单一产物"不是恢复旧设计,是**新建**轮次结构——这正是五条重试路
能塌缩成一个状态机的原因。

## 2. 隔离坐实(活数据)

- `zk-cursor-bpi-patch` 消费者 = **仅** `zero-cursor-bpi` + `zero-cursor-bpi-82`(kubectl 全 deploy 扫描)。
- codex 线:`zero-cursor-101` → `zk-cursor-protocol-patch`;zero-NN 舰队 → `zk-image-patch`。物理不同 CM。
- **注意**:`zero-101` 和 `zero-82`(zk-image-patch 舰队里)= acct101/acct82 的 codex CLI 服务 pod——
  **两个账号都同时服务 codex CLI 线**,账号纠缠对称,谁当金丝雀都不能靠"账号不纠缠"加分,
  只能靠"不碰账号面"纪律(我们只改 responses.js,成立)。S6 codex 回归要把 zero-82/zero-101 的行为纳入观察。

## 3. 生产画像(近 14 天,SpendLogs 全量)

| 用户 | 流量 | 主要落点 |
|---|---|---|
| cursor-liuguoxian04(**=实验身份**) | cursor-g-sol/sol-high 101 轮 | 池(2 deploy),WA 钉位待探 |
| cursor-zhangkairui(**同事,真生产**) | 11 轮 | **主用 101 直连**(cursor-web-fc-terra ×8),82 直连仅 3 |
| cursor-liuguoxian03 | **近 7 天全模型零使用** | 计划里"03=生产"的前提已过时 |

**SpendLogs 的 model_id 本线全空**(usage 合成老坑)→ 钉位只能靠 proxy WA 日志/pod 暗号,不能靠账单。

## 4. 金丝雀结论

- **金丝雀 = 82 lane(zero-cursor-bpi-82)**。理由:同事 zhangkairui 主用 101 直连,金丝雀放 101 会把
  他 8/11 的流量卷进实验;放 82 只暴露他 3 轮低频直连(残余风险,见下)。
- **钉死方式**:首选 key04 + 82 直连调试名(`cursor-web-fc-82-terra*`,已授权,确定性落 82);
  cursor-g 池名走 WA 亲和,钉位需 S1 首轮实测(每轮 pod 日志 grep 暗号验证,本来就是 S1 纪律)。
- **残余暴露与备选**:env-gate 开在 82 deploy 时,zhangkairui 若再用 82 直连(近 14 天 3 次)会进实验。
  两个处置:(a) 接受(低频+新契约行为面向用户是改善);(b) **更干净的备选:克隆第三 deploy
  `zero-cursor-bpi-canary`+独立 CM 副本+只授 key04 的直连名**——零同事暴露、gate-off 等价性验证
  都省了、回滚=删 deploy(克隆脚本 clone_web_fc_lane_v2.py 现成,账号仍借 acct82 tokens hostPath,
  不碰账号面)。**(b) 推荐,S4 拍板**。

## 5. 遗留待验(进 S1/S4)

- key04 池名流量的 WA 实际钉位(S1 首轮顺手验,暗号 grep)。
- 断点续跑的上游语义:同 parent 重问是否稳定 fork(S3 顺手加 3 轮实测)。
- zhangkairui 的 key 是否也该在金丝雀验收后第一批受益切换(S7 推广次序)。
