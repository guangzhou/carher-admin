# 会话复用改「最长严格前缀匹配」— 修「同一个 session 每问几轮就在 GPT 网页新开一条会话」

2026-08-31。对象:`zero-cursor-bpi-82` 的 `responses.js`(存活在 ConfigMap `zk-cursor-bpi-patch`)。

---

## 1. 症状

Cursor 里同一个 chat 连问多个问题,GPT 网页侧不是一条会话多轮问答,而是**每隔几轮就新开一条**。

## 2. 诊断(假设 → 证伪条件 → 数据)

**假设**:网关认不出"这还是刚才那条会话",于是当成首轮全量重发,上游因此新建会话。

**证伪条件**:如果假设成立,断链处应当能看到「非复用路径的日志」紧跟「首轮握手」;
且缓存里应当存在**历史前缀完全相同、却被当成两条**的会话。如果历史前缀确实不同
(用户改了上文 / Cursor 截断了历史),那就不是认门的问题,假设作废。

**数据**:

`ZK_CONV_PERSIST=1` 落盘的 `/app/convcache/conv-cache.json`,32 条,三对断链指纹:

| 对比 | 历史前缀逐项 sha1 | `input[0]` | `instructions` |
|---|---|---|---|
| `6a9512bd`(28) vs `6a9514f0`(30) | **28 项全等** | 相同 | **不同** |
| `6a9512bd`(28) vs `6a950cab`(30) | **28 项全等** | 相同 | **不同** |
| `6a9514f0`(30) vs `6a950cab`(30) | **30 项全等** | 相同 | **不同** |

日志侧同源:断链处必定是 `[diet] framework msg … chars` 紧跟
`[handshake] implicit (first turn carries preamble+contract, no ack round)`——
`dietCursorInput` 只在非复用路径调用,所以这两行同时出现 = 网关把一个已有 18/28 项
历史的请求判成了首轮。

规模(19 小时,单 pod):63 条网页会话,47 条首次出现时 `items<=2`(真·新 chat),
16 条首次出现时 `items>2`(断链新开);其中 4 条是 `ZK_EMPTY_RETRY=3` 的设计内重试,
**12 条是本 bug**。

**结论**:成立。根因在 key 的构成 ——

```js
function _convKeyOf(input, instructions) {
  return _itemDigest(input[0]) + ':' + _itemDigest(instructions || '')
}
```

`instructions` 直接来自 `req.body`(网关全程不改写),由 Cursor 客户端产生,
**同一个 chat 内隔几轮就会变**。key 一变 `_convCache.get(key)` 直接 `undefined`,
**连后面那段逐项前缀 digest 校验都走不到**,直接回落全量。

附带矛盾:走复用路径时是 `flattenInput(prepareCodexInput(...), null)`——
`instructions` **根本不发给上游**。它被当"稳定锚点"写进 key,却既不稳定、
也不参与复用路径的内容。

## 3. 已排除的候选(都有数据)

| 候选 | 排除依据 |
|---|---|
| 多 worker / 多副本各持一份内存缓存 | 容器 `exec node` 单进程,`replicas: 1` |
| 请求在两个网关 pod 间弹跳 | 101 lane pod 的 `[conv]` 行数 = 0,流量全在 82 单 pod |
| diet/strip 改坏了被哈希的 input | 5 处 `saveConvSession` 传的都是裸 `input`,全文件 `input` 未被重新赋值 |
| 会话失效触发删缓存 | `[conv] delta send failed` 全量日志里 0 次 |
| TTL 过期 | TTL 240 分钟,断链间隔是分钟级 |

## 4. 没有证据、因此不下结论的一条

**为什么"昨天 10 点之前是好的"。** `_convKeyOf` 这个构成在 r18(08-22)之前就已存在,
**不是那天改的**;而 live 代码在 ConfigMap 里、由集群上直接 patch,
`/Data/backups/zk-cursor-bpi-cm-*.json` 只到 08-24,拿不到 10 点前的实物来 diff。
机制成立是**事实**;"某次改动让触发率上升"是**推测**,本文档不据此做任何设计决策。

已顺带证伪一条:zk-delta 不碰 `instructions`(`zk-delta/{common/framing,sidecar/sidecar,server/server}.js`
对该字段零引用,它作为 template 的非数组键原样穿透),所以不是它把提示词改花的。

## 5. 修法

### 5.1 不要按单键查表,改「最长严格前缀匹配」

这个问题 zk-delta 已经解过一遍,`zk-delta/common/framing.js` 里我自己写过:

> 刻意不按 messages[0] 建索引 —— Cursor 每条会话的第 0 条框架消息都一样,
> 按它建索引会让所有会话撞成同一个 key(**这正是网关旧 `_convKeyOf` 的缺陷**)。

**所以"key 只留 `input[0]`"是错的**——那会让不同 chat 撞成同一个槽互相覆盖,比现状更糟。
正解是照抄 `findLongestPrefix` 的语义:在所有候选里找「是当前 items 的**严格前缀**且最长」的那条。

- 免疫 `instructions` 漂移(它不再参与匹配)
- 免疫不同会话撞车(靠整段前缀区分,而不是靠第 0 条)
- **正确性只增不减**:今天能命中的,新逻辑必然也命中(今天 = 键相等 ∧ 前缀全等;
  新 = 前缀全等)。今天会漏掉的才是新增命中。安全网还是那段逐项 sha1 校验,一字未动。

存储槽的键改用 `convId`:今天键是 `(input[0], instructions)`,提示词一漂**同一条会话会占多个槽**
(实测缓存里同一条对话确实占了 3 个槽);改成 `convId` 后一条会话一个槽,原地更新。

### 5.2 命中但 `instructions` 变了 → 把新提示词随增量一起送

否则新指令永远到不了模型(今天是靠"全量重发"顺带送到的,修完不能把它弄丢)。

- 条目里存 `instrDigest`,命中时比对
- 变了且长度 ≤ `ZK_CONV_INSTR_MAX`(默认 8192)→ 增量里带上 `SYSTEM: <新 instructions>`
- 变了且超长 → **退回今天的行为**(不复用、全量重发),不冒门①的险

门①方向是**改善**:今天提示词一变就重发整段历史 + instructions,改完只发增量 + instructions,
严格更少。

### 5.3 补 miss 原因日志

今天四条 miss 路径(键没命中 / 长度不是严格前缀 / 前缀第 i 项不等 / TTL)打印**完全一样**,
这次定位全靠去翻盘上缓存文件。新增一行 `[conv] miss cands=N stale=… notprefix=… mismatch=… items=…`。
这是**补工具,不是补证据** —— 根因已由 5.2 节的缓存实物坐实。

### 5.4 加载旧缓存时按 convId 重建键

`_loadConvCache` 改成 `set(String(e.convId), …)`,老文件里 `key="sha1:sha1"` 的条目自动迁移,
否则上线后每条在途会话都要白白全量重发一次。

### 5.5 等长前缀平局取最近使用的那条(上线后由探针抓出的第二个缺陷)

**这条不是设计时想到的,是第一版上线后被自己的探针抓出来的。**

第一版按 zk-delta `findLongestPrefix` 的写法 `if (!best || s.count > best.count)`,
平局时保留先遇到的那个。实测:同样的探针文本连跑两遍,第二遍第 6 轮同时匹配上
「上一次那条会话」和「本次这条」,前缀长度都是 10,结果**新一轮被接到了上一次那条旧会话上**
(日志:`delta send … (conv=6a9523af)` 出现在一串 `conv=6a95244e` 中间)。

真实场景同样会撞——同一句开场白问两次就够了。改成:

```js
if (!best || s.count > best.count || (s.count === best.count && s.ts > best.ts)) { best = s; bestKey = k }
```

长度仍然优先,只有等长才比新旧;活跃的那条才是用户正在说话的那条。

> **顺带记一笔**:`zk-delta/common/framing.js` 的 `findLongestPrefix` 是同样的
> 「平局取首个」写法,存在同一个隐患。它候选来源不同(每连接一份),本次**没有动它**,
> 只在此标注,避免以后照抄时把这个坑一起抄走。

## 6. 回归结果(全部我自己跑,门在退出码)

| # | 内容 | 判据 | 结果 |
|---|---|---|---|
| R0 | `node --check` 打完补丁的 responses.js | 退出码 0 | ✅ |
| R1 | `conv_prefix_offline_cases.js`(从产物逐字抠真函数体 eval 驱动) | 全绿 | ✅ **41/41** |
| R2 | **反向门**:故意改坏,R1 必须变红 | 改坏版退出码 ≠ 0 | ✅ B1/B2/B3 + **B4(去掉 ts 平局判据)退出码 1**,而 `node --check` 全部放行 |
| R3 | 既有 `conv_persist_offline_cases.js` 对新产物 | 全绿 | ✅ **19/19** |
| R3.5 | 合成探针 `conv_prefix_live_probe.py`(6 轮,每轮换 instructions) | 一个 convId | ✅ PASS;且**撞车候选确实在场**(`loaded 6 skipped 0`、`cands=6`),不是空转 |
| R4 | 真 Cursor 同一 chat 连问 ≥8 轮 | 全程一个 convId、无 miss | ✅ **连续 12 轮**(3 轮真实使用 + 9 个驱动请求)全程 `conv=6a9525ce`;窗口内 handshake 0 次、miss 0 条 |
| R5 | 门①:`ls` / 问候的增量仍是小量级 | 与基线 59/61 同量级 | ✅ 实发 **12 / 16 / 23 / 69** 字符;4 个纯增量轮契约 `DIET-ZERO`(0c vs 3019c) |
| R6 | 门②:shell 命令 + 飞书建文档 | 都出结果 | ✅ shell 出结果 5 次(cmd 3–140 字符);文档 `VxM8dPRrPoJuk9x6xZWc3mxBnhd` 标题逐字 = nonce |

**方法学两条,记下来别再踩**:

1. `[conv] delta send … chars` 打的是 **`execenv-strip` 之前**的数,门① 要看
   `[execenv-strip] N -> M chars` 里的 M。用前者判门①会把 660 当成膨胀,实际只发了 145。
2. R4 那一跑里驱动器的 `Cmd+N` **没生效**(`[conv] saved items=` 从 8 起步而非 2),
   所以那 8 发是接在既有对话后面的。这不影响 R4 判据(反而把链拉到 12 轮),
   但**不能把它说成"新开会话后连问 8 轮"** —— 那是没发生的事。

**合成压测绿不构成上线依据**;R4–R6 的数据全部来自真 Cursor 客户端。

## 7. 上线与回滚

- CM 有 15 个 key,**只能** `kubectl patch cm --type merge --patch-file`,禁 `kubectl apply`
- `kubectl rollout restart deploy/zero-cursor-bpi-82` + `rollout status`,不手删 pod
- 回滚 = 把改前的 `responses.js` 用同样的 patch-file 方式打回去(改前字节已存 `/tmp/resp_live_82.js`
  并在 198 上留一份 `/Data/backups/`)

实际上线两次:

| 时间 | 备份 | 产物 sha256 | 内容 |
|---|---|---|---|
| 14:41 | `zk-cursor-bpi-cm-20260831-144140-pre-conv-prefix.json` | `35c36958…f096eb6` | 最长严格前缀匹配 + instructions 补发 |
| 14:53 | `zk-cursor-bpi-cm-20260831-145339-pre-tiebreak.json` | `6b0d67f6…2edc5883` | 追加 5.5 的平局取最近 |

两次都核过:patch 后 CM 仍是 15 个 key,`data['responses.js']` 的 sha256 与本地产物逐字节一致;
基线 `/tmp/resp_live_82.js` 全程未变(`701a7f50…e45d15ef`),第二版是从**同一份基线**重打的,
不是在第一版上叠补丁。

## 8. 事实 / 推测标注

**事实**:第 2 节全部(缓存实物 + 日志 + 计数);第 3 节全部;zk-delta 不碰 `instructions`;
`_convKeyOf` 的构成早于 08-22;第 5.5 节的平局缺陷(现场日志实物);第 6 节全部回归数据。
**推测**:"某次改动使触发率在 08-30 上升"——无实物,不作为设计依据。
**设计判断(非实测)**:`ZK_CONV_INSTR_MAX` 默认 8192 是拍的保守值,可用 env 调;
超限退回全量 = 与今天等价,所以拍错的代价上界 = 现状。
