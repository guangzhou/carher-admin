# 9router Cursor 反代 lane:字段丢弃族 + agent-mode function-call 修复 + 构建/投递 SOP

对象:**9router**(Node/Next.js 反代 Cursor `cu/*` composer,对外 OpenAI
`/v1/chat/completions` + function-calling,服务 openclaw/Claude Code/Cursor/Codex 等
通用客户端)。与 `cursor-client-bundle-patch`(改用户 Mac 上的 Cursor.app 本体)、
`zk-cursor-web-fc-iterate`(改 198 CM 的网关 lane)**完全分层**,别混。

关联记忆:[[project_9router_toolcall_dies_on_retired_chatservice]]
[[project_9router_cursor_reverse_proxy_findings]]
[[reference_cursor_agentservice_protobuf_field_map]]
[[reference_cursor_web_token_needs_deeplogin_for_api2]]
[[feedback_cursor_agentservice_discards_custom_system_prompt]]
[[feedback_cursor_agentservice_discards_conversation_history]]
[[project_9router_opus5_skill_discovery_fixed_2026_09_17]]
[[feedback_9router_image_ships_via_198_local_registry]]
[[feedback_cursor_agent_raw0b_is_benign_judge_by_done_out]]
[[feedback_openclaw_agent_is_the_only_valid_toolcall_ruler]]
[[feedback_9router_translator_runs_before_executor]]
[[feedback_feishu_ws_stall_is_a_different_lane_than_reverse_proxy]]
[[project_9router_second_cursor_account_added_2026_09_17]]

## 30 秒定位表(先查这个,再往下读)

| 症状 | 最可能的病 | 第一条命令 |
|---|---|---|
| 模型说"我没有 xx 技能" / 技能目录 `COUNT=0` | f8 `custom_system_prompt` 被丢 | `probe-fold.sh` 看 LEG3 |
| 多轮里模型反问"请提供标题/目录",单轮却正常 | f7 ConversationHistory 被丢 | `probe-fold.sh` 看 LEG2 |
| 日志 `read SKILL.md` → `sessions_history` → `read` 死转 | 同上(f7),模型在找不到自己的任务 | `trace-9router.sh` 看 `sessions_history_count` |
| 改了 `open-sse/**` 但行为完全没变 | 没重建镜像(webpack 构建期打包) | `ship-via-198-registry.sh` 的 MARKER 门禁 |
| 流被掐断 / `DONE` 缺失 | **f8 被填了**(填大 prompt 会杀流) | `trace-9router.sh` 看 POST vs DONE |
| 飞书里发消息没反应 | 跟 9router 无关,是 WS 车道 | `h14-feishu-delivery-triage.sh` |
| 上游 429/额度告急,想加个 Cursor 号 | 见下「账号池」一节 | `add-cursor-account.py --list` |
| 某个模型**时好时坏**、坏的那半是 200 之后不吐字节 | 一条腿对该模型挂死,而池子没把它判坏 | `verify-pool-failover.sh` |
| `trace` 报 `STALLED` 但日志里有 `NEXT ACCOUNT` | 尺子过期:换腿成功也会 POST≠DONE | 见「这次改动让三条旧尺子过期」 |
| 客户端拿到的错误里带 `reset after Xm Ys` | **所有腿都在冷却窗口里**(不是挂死,是池子空了) | 日志 grep `accounts locked for`;等到期或加号 |
| 有人问"坏号会不会被拉黑 / 要不要手工摘" | 不会也不用,锁是**按腿×模型的到期时间** | 见「换腿语义」一节 |

## 账号池:加 / 判 Cursor 号(2026-09-17 定型)

池子在 **pod sqlite** `/app/data/db/data.sqlite` 的 `providerConnections`
(`data` 是 JSON:`accessToken`/`testStatus`/`providerSpecificData{machineId,userId}`/
`modelLock_<model>`/`backoffLevel`/`rateLimitedUntil`);按号归因只有
`usageHistory.connectionId` 一列。管理面 `POST /api/auth/login {password}` 拿
`auth_token` cookie,`settings` 表空 ⇒ 密码就是 pod env 的 `INITIAL_PASSWORD`
(⛔ 不许打印/落盘/进仓库)。**加删腿零重启**,不用滚 deploy。

```bash
# 只读:每条腿现状 + 逐条做一次真上游 models 拉取判死活
scripts/9router-cursor/add-cursor-account.py --list
# 加号:备份 sqlite → deep-login 换 IDE token → import → 验收(不过门报红+给回滚命令)
CURSOR_WEB_COOKIE='user_01XXXX::<jwt>' scripts/9router-cursor/add-cursor-account.py --apply
```

🔴 **`POST /api/oauth/cursor/import` 返 200 什么都没证明**:`validateImportToken`
**不发任何网络请求**,只查 `token.length>=50` + machineId 是 32+ 位 hex,然后无条件写
`testStatus:"active"`。拿浏览器 cookie(**web token**,`type:"web"`)直接 import
就得到一条"看起来 active、实际打不动模型"的死腿。

**判死活唯一量具 = `GET /api/providers/<id>/models`**(它是一次带 token 的真上游
gRPC `GetUsableModels`):

| 读数 | 含义 |
|---|---|
| `n=223`、无 `warning` | 实时表,token 被上游接受 ✅ |
| `n=14` + `warning:"Cursor returned no live models; falling back to static catalog."` | 静态兜底表(`claude-4.5-opus-*` 那一代),token 被拒 ❌ |

对应 pod 日志 `CURSOR_MODELS Live model fetch failed: Cursor GetUsableModels returned 401`。
⛔ `POST …/test-models` 两条腿都 500(TimeoutError);⛔ `POST …/test` 对死腿也返
`{"valid":true}` —— 两个都是坏尺子。顺手断言 lane 模型在不在表里:
`claude-opus-5-medium` / `claude-fable-5-1-medium`。

**web token → IDE token(deep-login,无头可做,不需要浏览器)**:
`verifier H=base64url(32B)`、`challenge q=base64url(sha256(H))`、`uuid K`;
先 GET `https://cursor.com/loginDeepControl?challenge=q&uuid=K&mode=login` 焐 cookie jar,
再 `POST https://cursor.com/api/auth/loginDeepCallbackControl`
body `{uuid,challenge,redirectTarget:null,mobile:false}`,然后
poll `https://api2.cursor.sh/auth/poll?uuid=K&verifier=H`。两个坑:
⛔ **不许传 `selectedTeamId`**(`null`/`0`/`-1` 全 `400 Invalid selected team`,省掉才 200);
⛔ **必须自己收 `set-cookie`**(那页会 307 到 `/api/auth/bootstrap-cursor-web-target`
再 307 回来,不带 jar 就是无限 307,undici 报 `redirect count exceeded`,像"页面挂了")。
阳性对照:`GET https://cursor.com/api/auth/me` 必须 200 回出 email。
整套在 **pod 内**跑(本机在大陆打不动 cursor.com)。

⚠️ import 出来的新腿是 **priority 2 = 备用**,实测 8/8 真流量全落 priority 1;
`/v1/chat/completions` **没有 pin 到某条腿的 header** ⇒「新腿 usageHistory 为 0」是
设计如此不是故障,别为了造一发真流量去停用现役那把。
⚠️ 用 `sql.js` 读 sqlite 看不到刚提交的行(WAL 未 checkpoint),**一次没读到 ≠ 没流量**;
`/api/usage?limit=30` 是 404、`/api/usage/history` 返 200 但 rows=0,都别用。

全记录:memory `project_9router_second_cursor_account_added_2026_09_17` /
`reference_cursor_web_token_needs_deeplogin_for_api2`。

## 账号池:一条腿**挂死**时自动顶上(2026-09-18,镜像 `pool-20260918a` 起)

**加了号 ≠ 有账号池。** 09-17 加完第二条腿后,leg2 对 `claude-fable-5-1-medium`
**接了流然后一帧都不吐**,而 `stickyRoundRobinLimit:1` 每请求换腿 ⇒ **50% 的 fable
请求直接挂死**,客户端吊到自己的传输超时。直接读 sqlite 坐实:leg2
`modelLocks:{}` `lastError:null` `backoffLevel:0` —— **一整天里一次都没被标记过坏**。

原因不是池子没写:`src/sse/handlers/chat.js`(⛔**不在** `open-sse/handlers/`,那边只有
`chatCore.js`)的 `while(true)` 循环、
`services/accountFallback.js` 的 `modelLock_<model>` + `checkFallbackError`
(兜底就是 `{shouldFallback:true}`)全都在,而且是 provider 通用的。
**缺的是温度计**:这套转移是 error-driven,挂死**没有 status、没有 errorText**,
而 `driveTurn` 流式分支**在任何帧到达之前就把 `Response` 返回了**
⇒ `chat.js` 早就 `if (result.success) return result.response` 退出循环,
挂死发生时**已经没有循环可以重试**。

### 做法:首帧死线,把"挂死"翻译成池子听得懂的一次失败

只改 `open-sse/executors/cursor.js` + `config/errorConfig.js` 一行,
**`chat.js`/`auth.js`/`accountFallback.js` 一个字没动**。

- `CURSOR_FIRST_FRAME_TIMEOUT_MS`(默认 **45s**):首帧没来 ⇒ `throw cursor stall: …`
  ⇒ `execute()` catch 映射成 **504**(不是 500,否则和真上游 500 同指纹)
  ⇒ `createErrorResult` ⇒ `markAccountUnavailable` 写 `modelLock_<model>` ⇒ 换腿。
- `CURSOR_STALL_TIMEOUT_MS`(默认 **120s**):首帧**之后**的帧间死线。
  ⚠️ **诚实边界**:这时 `Response` 已经交给客户端了,**换不了腿**,只能把流以 error 收尾,
  避免客户端吊到 ~300s。真正的"顶上去"只覆盖首帧之前——而实测的病正是这个形状。
- `errorConfig.js` 加 `{text:"cursor stall", cooldownMs: COOLDOWN.long}`(2 分钟)。
  不加也能换腿(兜底 30s),但 30s 会让哑腿每 30s 被重试一次。
- ⛔ **`bridge.attach()` 必须保持同步**:`resumeAgent` 在 `driveTurn` 返回后**立刻**
  写 tool-result 帧。把 `driveTurn` 改成 `async` 并把 attach 放到 `await` 之后 = 死锁。
  正解是 attach 同步、帧先缓冲进数组,拿到首帧再建 `ReadableStream` 并把缓冲吐出去。
- 45s 的依据:实测健康首帧 TTFT 2.5–3.7s,最慢一发 opus 14.9s ⇒ 3× 余量。
  ⛔ 别设到 5s,长 prompt 首帧本来就慢,会造假红。

### 验收(`verify-pool-failover.sh`,四道闸缺一不算过)

拿**真实故障**当红:临时把策略翻回 `round-robin`+`sticky=1` 把 leg2 逼进轮转,
`trap restore EXIT` 无条件还原 `fill-first`。

```bash
scripts/9router-cursor/verify-pool-failover.sh
```

| 闸 | 判据 | 09-18 实测 |
|---|---|---|
| 1 客户端无感 | 0 挂死 + N/N 回显 nonce | `ok=4 hang=0`;#1 50.5s(45s 死线+重试),#2-4 回到 4s |
| 2 换腿真发生 | 日志 `UNAVAILABLE (504) → NEXT ACCOUNT` | 有,且同一发重投到 leg1 后 `📊 DONE` |
| 3 坏腿被量出来 | leg2 长出 `modelLock_<model>` | `errorCode:504` + 锁到期戳(改前该键**不存在**) |
| 4 阴性对照 | opus 两条腿都不被锁 | opus 分别落两条腿都 DONE,opus 的锁全 `null` |

第 5 条(锁会自愈)再跑一遍就看得到:锁过期后 leg2 **会被重新选中**、再挂再锁,
**不是永久拉黑** —— 一次抖动不会废掉一条腿。

⛔ **判"换了几次腿"只认 pod 日志**:`sql.js` 读 sqlite 是 WAL 快照,
第二轮的 504 在日志里明明白白,而 dump 出来的 `lastErrorAt` 还停在第一轮。

### 换腿语义:是"临时冷却",不是"黑名单"(被问过就照这张表答)

源码 = `open-sse/services/accountFallback.js` + `src/sse/services/auth.js`
+ `open-sse/config/errorConfig.js`(行号以 188 `/Data/9router-build/src` 为准)。

- **写进去的是一个到期时刻,不是一个标志位。** `modelLock_<model>` 存 ISO 时间戳,
  `isModelLockActive` / `isAccountUnavailable` 就是一句 `> Date.now()`。
  **`isActive` 从不被翻**,也没有任何地方删腿 ⇒ 钟一过腿自己回来,不需要人工摘/加回。
- **锁的粒度是「腿 × 模型」**,`modelLock___all`(`MODEL_LOCK_ALL`)只在**模型名拿不到**时才用
  = 账号级锁。实测坐实:leg2 因 fable 被锁的同一时刻 **opus 照样落在 leg2 上 DONE**。
- **顶上去发生在同一发请求内**,客户端只感知延迟:`auth.js` 的
  `getProviderCredentials(provider, excludeConnectionIds, model)` 在
  `availableConnections` 里滤掉 `excludeSet` ∪ 模型锁未到期的腿(选号有 `selectionMutex`
  串行化防竞态),`chat.js`(`src/sse/handlers/chat.js:233` 的 `while(true)`)把**同一发**
  重投下一条腿:`:310 if (result.success) return result.response` /
  `:327 markAccountUnavailable` / `:330` 打 `⇄ … UNAVAILABLE → NEXT ACCOUNT` /
  `:331 excludeConnectionIds.add`。
- **成功会提前清锁,不必等到期**:`chat.js:304` 在成功后调
  `clearAccountError(connectionId, currentConnection, model)`(`src/sse/services/auth.js:298`)。
  它清三类键:**本次成功的那个模型的锁**、`modelLock___all`、以及**所有已过期**的锁;
  且只有在**清完没有任何未到期锁剩下**时才顺带把 `testStatus:"active"` /
  `lastError` / `errorCode` / `lastErrorAt` / `backoffLevel:0` 一起归零 ⇒
  **一条腿对 A 模型恢复,不会顺手把它对 B 模型还没到期的锁擦掉**,退避等级也不会被误清。

| 坏法 | 冷却多久 | 来源 |
|---|---|---|
| **挂死** `cursor stall`(本次新增,排 `ERROR_RULES` 第一条) | `COOLDOWN.long` = **2 min** | text 规则 |
| 401 / 402 / 403 / 404 | 2 min | status 规则 |
| 429 / `rate limit` / `quota exceeded` / `capacity` / `overloaded` | **指数退避** `BACKOFF_CONFIG.base 2s × 2^(level-1)`,封顶 `max` 5 min,`maxLevel` 15 | `backoff:true` |
| `request not allowed` | `COOLDOWN.short` = 5 s | text 规则 |
| **其它一切没匹配上的** | `TRANSIENT_COOLDOWN_MS` = **30 s** | `checkFallbackError` 兜底 |

⚠️ **text 规则整体先于 status 规则**,且兜底是 `{shouldFallback:true}` ⇒
**任何失败都会换腿**,没有"不认识的错误就不转移"这回事。
上游自报的 reset 时间另有上限 `MAX_RATE_LIMIT_COOLDOWN_MS` = 30 min。

**所有腿都在冷却窗口里**时 `getProviderCredentials` 返
`{allRateLimited:true, retryAfter, retryAfterHuman, lastError, lastErrorCode}`,
日志出 `AUTH | cursor | all N accounts locked for <model> (reset after 1m 47s)`
+ `CHAT | [cursor/<model>] <lastError> (<retryAfterHuman>)`,`chat.js` 回
**`unavailableResponse(503, …, retryAfter, retryAfterHuman)`** ⇒ 客户端拿到
**带 reset 时间的 503**,不是无限吊着。另两条出口:本轮把所有腿都换过一遍仍不行 ⇒
`No more accounts available` + `lastStatus`;该 provider 一条活腿都没配 ⇒ **404**
`No active credentials for provider`(这条不是冷却,是没号)。
⛔ 这三条都在 `▶ POST` **之前**返回 ⇒ 日志里 POST=0,别读成"没流量",见下面第三条过期尺子。

**还兜不住的两种形状(别答成全覆盖):**
1. **首帧之后**才冻结 —— `Response` 已交客户端,换不了腿,只能把流以 error 收尾。
2. 腿把错误**当流内正文**吐回来(HTTP 200 + body 里写着 401):`chat.js` 看到
   `success:true` ⇒ 不换腿。改它要动所有客户端的错误语义,**未做,待拍板**。

现役策略 `fill-first` ⇒ leg1 是钉,leg2 只在 leg1 被锁/排除时顶上。
⛔ 别为了"分摊"改回 `round-robin`:那会让 leg2 每次抽风都多付一次 45s 学费。

### 这次改动让三条旧尺子过期

- **`POST == DONE` 作废**,改成 **`POST == DONE + ✗ERROR`**。一次**成功**的换腿必然留下
  `POST(坏腿) + ✗ERROR 504 + POST(好腿) + DONE`;拿旧判据去读,每换一次腿都报 `STALLED`。
  `trace-9router.sh` 已改,并加了 `failover_count` / `stall_count` 两个计数。
- **`POST=0` 不再等于"没有流量"**(09-18 补的第三条)。账号池**全腿都在冷却窗口**时,
  请求在 `chat.js:234` 选号阶段就 `return` 了,而 `▶ POST` 是 `chatCore`(`chat.js:268`)
  才打的 ⇒ **一行 POST 都不会有**。旧判据会报 `NO_TRAFFIC` =「你的请求没走到 9router」,
  把人往网络/路由方向带,而真正要做的是等锁到期或加号 —— 两种处置完全相反。
  `trace-9router.sh` 已加 `all_locked_count` / `no_more_accts_count` /
  `no_active_cred_count` 三个计数,并新增 `VERDICT=POOL_EMPTY` 把它和 `NO_TRAFFIC` 分开。
- **`probe-fold.sh` 不能直打 9router**:它的 `said()` 是按**非流式 JSON** 写的 sed 提取,
  9router 永远回 SSE ⇒ 三条腿全假红,**而 NEGCTL 那条「必须找不到」在坏提取下恒绿**
  ⇒ 整轮不可信。直打 9router 用 `probe-fold-sse.js`(复用 probe8 那个验过的解码器);
  `probe-fold.sh` 留给 `cc.auto-link.com.cn/pro` 那条非流式网关路。

### 止血旋钮(零代码零重启,先做这个)

`PATCH /api/settings` 把 `providerStrategies.cursor` 切 `fill-first`,
钉在 priority 1,leg2 退回真备用。⚠️ `settingsRepo.updateSettings` 是**浅合并**
`{...current,...updates}` ⇒ 传 `providerStrategies` 会**整棵子树替换**,
必须 caller-side read-merge-write(否则连 `providerThinking` 一起冲掉)。

## 铁律(每次都踩过的坑)

1. **open-sse 是构建期 webpack 打包** —— 改 `open-sse/**` 后必须重建镜像;COPY 原文件
   对运行时**零效果**。判据 = 改动新增的字符串字面量出现在 bundle chunk
   `/app/.next/server/chunks/318.js`。MARKER 硬门就是干这个,**不过门就 abort**。
   门禁必须带 sentinel 前缀(`BUNDLEHIT:`)+ 负对照:jms 登录噪声会把"输出非空"填满,
   拿"非空"当门禁 ⇒ 永绿。最好再要一条**差分门禁**(旧镜像 0 → 新镜像 1)。
2. **本机 Mac 不 build**(架构+DiskPressure);build 在 **188 = `10.68.13.188`**
   `/Data/9router-build/src`。⛔ jms 禁用资产名(`JSZX-AI-03` 会路由到 189 被拒),写 IP。
   源码改完先 `jms scp` 同步过去,`sha256sum` 两端比对(用 `grep -Eo '[0-9a-f]{64}' | head -1`,
   否则 PTY 噪声让比较假红)。
3. **镜像投递走 `198:5000` 本地 registry**(见下"镜像投递")。
   ⛔ 作废旧路:特权 `nodedbg-225` + `kubectl cp` + `chroot /host k3s ctr images import`
   —— 225 无 SSH,`sideload.sh` 的 step5 是坏的。`imagePullPolicy: IfNotPresent` 必须保持。
4. **198 现网 manifest 禁 `apply`**;只 `set image`/`patch`。**新建一次性 pod 用 `create` 可以**。
5. ⚠️ **9router 是 `replicas=1 + strategy: Recreate`** —— 换镜像 = 旧 Pod 先死再起,
   **几十秒硬中断**,反代整条腿不可用。**动之前先跟用户说**。别把"2 副本无感滚动"的
   直觉套过来。禁手删正在服务的 pod。
6. 所有中转 tar 用 `tmp_` 前缀、跑完删。⚠️ 188 根分区常年 ~93%,每个 tar ≈ 713M;
   写 `/Data`,别攒。删 tar 前先确认 registry 里还有对应 tag,否则回滚没退路。
7. 判据 = **真实客户端实测 > 探针**。探针 ALL PASS 只是合成绿。判工具链只认
   `openclaw agent` 真实回合(约 46 工具),小合成 curl 触发不到 field17 = 假绿。

## 镜像投递(现役路径)

```bash
# 改完 /private/tmp/9router/open-sse/**,同步到 188:
scripts/jms scp /private/tmp/9router/open-sse/executors/cursor.js \
  10.68.13.188:/Data/9router-build/src/open-sse/executors/cursor.js
# MARKER = 本次改动里新增、独一无二的字符串字面量(bundle 硬门)
TAG=fc-20260918a MARKER='<conversation_so_far>' \
  scripts/9router-cursor/ship-via-198-registry.sh            # 只投递,不切流
TAG=fc-20260918a MARKER='<conversation_so_far>' APPLY=1 \
  scripts/9router-cursor/ship-via-198-registry.sh            # 投递并 set image(硬中断)
```

全程:188 build → **bundle MARKER 硬门 + 负对照** → `docker save` →
`jms scp` 188→Mac→198(每跳 sha256 比对;**188↔198 直连 scp 不通,必须以 Mac 中转**)→
198 `docker load` → `docker tag`/`docker push 127.0.0.1:5000/9router:<tag>` →
`set image deploy/9router` + `rollout status` → pod 内复验 MARKER → 清所有 tar。
⛔ 188 直推 `198:5000` 失败(`http: server gave HTTP response to HTTPS client`)。
容器名/deploy 名都是 `9router`。ns=`litellm-product`。节点钉在 `aiyjy-litellm-standby`(225)。
**回滚**:registry 里留着历史 tag,`set image` 换回上一个即可(`rollout undo` 也行,
但 registry 那条更确定)。节点:198=`aiyjy-litellm`、242=`aiyjy-litellm-242`、225=`aiyjy-litellm-standby`。

`sideload.sh` 保留作历史参考,**别再拿它上线**。

## ★字段丢弃族:AgentService 静默吃掉 f8 / f7(2026-09-17 定论)

**症状家族**:反代腿"看不到本地 skill 目录"、"多轮里反问、单轮却好"、"`read SKILL.md`→
`sessions_history`→`read` 死转"。这三个长得完全不同,**是同一个病的三个切面**。
共同点:**HTTP 全 200、日志无 error**,所以只能用 nonce 探针量,读代码判不出来。

**三层 bug,按发现顺序**:

1. **f8 `custom_system_prompt` 被丢** —— Cursor AgentService 收下这个字段但不用,
   换成它自己的 IDE system prompt。openclaw 把 151 条 skill 目录(含 `lark-doc`)放在
   system message 里 ⇒ 反代腿一条都看不到。判据:同模型同 nonce 的 system-vs-user 折叠
   A/B(`LARKDOC=NO`/`COUNT=0` vs 正常腿 `YES`/`150-151`)。
2. **translator 跑在 executor 之前** —— `translator/request/openai-to-cursor.js` 会把每个
   `role:"system"` 重写成独立的 `role:"user"` 消息、前缀 `[System Instructions]`。
   所以 `buildAgentRunFrame` 里 `filter(role === "system")` 匹配到的是 **0 条**,
   第一版修复是**静默 no-op**。⚠️ **executor 拿到的是 `translatedBody`,不是客户端原始 body**
   —— 日志 `FMT: openai→cursor`(`handlers/chatCore.js:222`)就是这条路的凭证。
   凡是在 executor 里按 role 过滤的逻辑,都必须先想清楚 translator 已经改过什么。
3. **f7 ConversationHistory 也一起被丢** —— 直接证明:3 条消息、nonce 放在**第一个** user
   turn,第三 turn 问回来,答 `HIST_LOST`(HTTP=200)。叠加 translator 把 `role:"tool"`
   也改成 user ⇒ 每个工具结果回合,`lastIndexOf("user")` 选中的是**工具结果**,
   用户的真任务掉进被丢弃的历史里 ⇒ 模型开始"找我的任务"死转。

**修法**:把 system 和历史**折进当前 user turn 的文本**,用带语义的标签包住,
让模型知道那是"之前的回合"而不是新指令。`cursor.js` 里的锚点:

- `foldSystemIntoUserText` —— `<caller_system_prompt>` 包住调用方 system
- `foldHistoryIntoUserText` —— `<conversation_so_far>` + 每轮 `<turn role="...">`,
  外加一段明写的指令("任务就在里面,执行它,别再问、别重读已读过的文件")
  和 `<current_turn>` 包住当前 turn
- `encodeHistoryText` / `encodeHistoryMessage` —— 把 assistant 的 `tool_calls`
  也渲染进历史文本(否则工具调用在历史里凭空消失)
- `SYSTEM_INSTRUCTIONS_MARKER = "[System Instructions]"` —— 用来识别 translator
  改写过的伪 user 消息,`isSystemMessage`/`systemTextOf` 靠它把 system 认回来

⛔ **只折 system 不折历史 = 单轮假绿、工具链照挂。** 两个必须一起做。

### ⚠️⚠️ f8 必须保持"不发"

**往 f8 塞大 prompt 会掐断流。** `fc-20260917b` 第一次真的填了 f8,直接造成
**约 6 分钟生产中断**(1 副本 + Recreate,没有第二条腿兜)。
不变量:**`FIELD8_REMAINING=0`** —— 9router 只发 f1/f2/f4/f9。
f7 继续发无害(被忽略而已),不用去删。

### 判据(缺一不算过)

```bash
# 1) 合成腿:f7/f8 折叠三条腿 + 负对照
NR_KEY_FILE=/tmp/tmp_key.txt MODEL=opus-5 scripts/9router-cursor/probe-fold.sh
# 2) 9router 侧量具:POST==DONE / restarts=0 / declin=0 / ★sessions_history=0★
scripts/jms scp scripts/9router-cursor/trace-9router.sh 10.68.13.198:/tmp/tmp_trace.sh
scripts/jms ssh 10.68.13.198 'sh /tmp/tmp_trace.sh'
# 3) 真实工具链:让被测腿真建一份飞书文档
scripts/9router-cursor/acceptance-feishu-doc.sh litellm/opus-5
# 4) 对照模型把它读回来 —— 自报不算过
scripts/9router-cursor/verify-doc-by-control.sh <DOC_URL> <期望标题>
```

**尺子纪律**:
- ⛔ `iu.field 13 raw(0B)` / `stream ended by server` **不是**红。健康 pod 实测 6 行
  `raw(0B)` 与 4 行成功 `DONE` 共存 ⇒ 零判别力。判上游回没回只认
  `📊 DONE <ms> · IN <n> · OUT <n>` + 客户端 body 非空。
- ⛔ 模型"反问"不等于"模型不配合"。`acceptance-feishu-doc.sh` 的题面已封死所有反问出口,
  它还在反问 ⇒ **先跑 `probe-fold.sh` 查 f7/f8,别去调提示词**。
  我自己在这里misdiagnose过一轮:把基础设施 bug 读成 prompt/behaviour gap,
  那个方向会让人对着一个基础设施缺陷永远调提示词。
- ⛔ 凭据(网关 key)只从 env/文件读,用完删,永不 echo 值、永不进交付物。

## 回归探针(合成绿,不替代真实 IDE)

```bash
# 探针在 pod 内打 127.0.0.1:20128。内部 key 在 pod sqlite,别硬编码进交付物。
P=$(ssh cltx@10.68.13.198 "sudo kubectl -n litellm-product get pods -l app=9router -o jsonpath='{.items[0].metadata.name}'")
# 取内部 key(apiKeys 表,name 形如 litellm-bridge):见下"取 key"。export NR_KEY=...
B64=$(base64 < scripts/9router-cursor/regress_probe.js | tr -d '\n')
ssh cltx@10.68.13.198 "echo $B64 | base64 -d > /tmp/tmp_regress.js; \
  sudo kubectl cp /tmp/tmp_regress.js litellm-product/$P:/tmp/tmp_regress.js; \
  sudo kubectl -n litellm-product exec $P -- env NR_KEY=$NR_KEY node /tmp/tmp_regress.js"
```

覆盖 4 场景:A 纯文本 / B 单工具→tool_call / **C 多轮喂回 tool 结果→模型作答** /
D 多工具选对。**C 是重点** —— 就是历史上死在 "Update Required" 的多轮 tool 对话路。

取 key:内部 key 存 pod `/app/data/db/data.sqlite` 的 `apiKeys` 表。用 pod 里的
`node_modules/sql.js`(镜像已带)读:
```bash
ssh cltx@10.68.13.198 "sudo kubectl -n litellm-product exec $P -- node -e '
  const fs=require(\"fs\"), initSql=require(\"/app/node_modules/sql.js\");
  initSql().then(SQL=>{const db=new SQL.Database(fs.readFileSync(\"/app/data/db/data.sqlite\"));
  const r=db.exec(\"select key from apiKeys limit 5\"); console.log(JSON.stringify(r));});'"
```

## 真实 IDE 失败诊断路(agent 模式 function-call)

**开诊断日志**:deploy 的 env `CURSOR_STREAM_DEBUG=1`(修完关掉再回归)。日志前缀
`[CURSOR AGENT ...]`。抓真实请求帧:`kubectl logs` 拿 `interaction_query fields=[...]`,
b64 离线 decode。

**已定的根因链(数据坐实,不是猜)**:agent 模式下 composer 优先调 Cursor **自带工具**
(通用客户端没有)→ 9router 不应答 → 每 10s heartbeat → 客户端 ~300s transport 超时 →
retry 死循环。自带工具走两条通道,分别要 decline:

| 通道 | proto 位置 | 现象 | 处理 |
|---|---|---|---|
| exec_server_message.native args | `serverMessage(2)` 的 shell=2/write=3/…/fetch=20 | 模型直接调编辑器工具 | `NATIVE_TOOL_DECLINE` reject,同流回 |
| interaction_query.web_search | `serverMessage(7)` → `InteractionQuery.web_search(2)` | 问天气/事实先查网 | `createWebSearchDeclineResponse`:回 `WebSearchRequestResponse.rejected{reason}` |
| interaction_query.fetch | `serverMessage(7)` → `InteractionQuery.fetch(9)` | web_search 被拒后改直抓 URL | `createFetchDeclineResponse`:回 `FetchResult.error{url,reason}` |

decline 的目的:把模型推回**调用方声明的 OpenAI 工具 + 自身知识**。回桥无效(通用客户端
没有 Cursor 自带工具),所以在 9router 层 decline,不 bridge。

**canonical proto 权威源**:`CoreUnit-NET/cursed-gateway` 的 `lib/cursorProto/agent.pb.go`
(188 `/tmp/agent.pb.go`)。字段号见 [[reference_cursor_agentservice_protobuf_field_map]]。
⚠️ 该快照 `InteractionQuery` oneof 只到 field 8;**live 客户端更新,fetch 在 field 9**
(比快照新),按 query↔response 字段号镜像规律推出 response 也落 field 9 载 `FetchResult`。
遇到快照没有的新 field:先从 wire b64 decode 出结构,对齐 canonical 同名 message,别凭空造。

**下一个可能的 fallback**:若 fetch decline 后模型仍卡,看日志新的
`Unhandled interaction_query fields=[...]`(可能 exa_search=5/exa_fetch=6/ask_question=3),
按同样镜像规律加 decline。**没数据不预造** —— 只对日志真出现过的 field 动手。

## 车道分离(判错车道 = 整轮诊断作废)

| 车道 | 入口 | 出问题时用 |
|---|---|---|
| 反代腿 | 客户端 → 9router → Cursor AgentService | 本 skill + `probe-fold.sh`/`trace-9router.sh` |
| 容器 CLI | `docker exec … openclaw agent` | `acceptance-feishu-doc.sh` |
| 用户飞书 | 飞书 WS 长连接 → 容器 | `h14-feishu-delivery-triage.sh`(**与 9router 无关**) |

反代腿验收全绿 **≠** 用户在飞书里发消息能收到,反之亦然。
`h14-feishu-delivery-triage.sh` 的核心判据是**时间轴**:最后一条 `received from` 的时刻
vs 用户按下发送的时刻。早于发送时刻 ⇒ 消息根本没到容器,别去查模型。
⛔ 那条车道上 `healthy` / `RESTARTS=0` / 网关 PID uptime 全是坏尺子。
三种"没反应"完全不同:群里没 @(设计如此,不是故障)、WS 停滞(`message om_… expired,
discarding`)、真在跑但慢。脚本把它们分开,并明写"区分不出 WS 是断开还是活着但静默"。

## 关键文件(工作副本 /private/tmp/9router,源在 188 /Data/9router-build/src)

- `open-sse/executors/cursor.js` —— `onFrame`(帧路由)、`execute()`(三分支)、
  `create*DeclineResponse`(各 decline 封帧)、`buildAgentRunFrame`/`executeAgent`/
  `driveTurn`/`resumeAgent`、`agentSessions` parked-bridge Map;
  **字段丢弃族的修复全在这里**:`textFromContent`/`renderToolCallsAsText`/
  `encodeHistoryText`/`encodeHistoryMessage`/`foldSystemIntoUserText`/
  `foldHistoryIntoUserText`/`isSystemMessage`/`systemTextOf`/`buildAgentRunFrame`
  (约 69–260 行区间;行号会漂,按函数名找)。
- `open-sse/translator/request/openai-to-cursor.js` —— ⚠️ **跑在 executor 之前**,
  把 `role:"system"` 和 `role:"tool"` 都重写成 `role:"user"`。改 executor 里任何
  按 role 过滤的逻辑,先读它。
- `open-sse/handlers/chatCore.js` —— `translateRequest(sourceFormat, targetFormat, …)`
  的调用点,日志 `FMT: openai→cursor` 出处;executor 收到的是 `translatedBody`。
- `open-sse/utils/cursorProtobuf.js` —— `encodeField`/`wrapConnectRPCFrame`/`decodeMessage`/
  `decodeMcpArgs`(已证 OK)、`NATIVE_TOOL_DECLINE`/`isNativeToolArg`/`encodeNativeToolDecline`。

**账号池那条路上的四个文件(provider 通用,改之前先读完再动)**:

- `open-sse/executors/cursor.js` —— `FIRST_FRAME_TIMEOUT_MS`(~423)/`STALL_TIMEOUT_MS`(~427)
  /`STALL_ERROR_PREFIX="cursor stall"`(~429)/`stallAwareStatus`(~435,映射 504)
  /两处 `reject(new Error(STALL_ERROR_PREFIX…))`(~1140 帧间、~1219 首帧)
  /~1223 首帧之后只能往流里塞 error 帧。⛔ `STALL_ERROR_PREFIX` 改字面量必须同步
  `errorConfig.js` 那条 text 规则,否则悄悄退化成 30s 兜底。
- `open-sse/config/errorConfig.js` —— `ERROR_RULES`(text 规则整体先于 status)、
  `BACKOFF_CONFIG`(32)、`TRANSIENT_COOLDOWN_MS`(39)、`MAX_RATE_LIMIT_COOLDOWN_MS`(42)、
  `COOLDOWN`(45)。
- `open-sse/services/accountFallback.js` —— `checkFallbackError`(兜底 `shouldFallback:true`
  在 ~49)、`getQuotaCooldown`、`isAccountUnavailable`/`isModelLockActive`(都是时间比较)、
  `MODEL_LOCK_PREFIX`/`MODEL_LOCK_ALL`/`getModelLockKey`/`formatRetryAfter`。
- `src/sse/services/auth.js` —— `getProviderCredentials(provider, excludeConnectionIds, model,
  options)`:`selectionMutex` 串行选号、`availableConnections` 过滤、
  全锁分支(~123 日志 / ~125 `allRateLimited:true`)、非冷却不可用分支(~132)、
  策略 `fill-first` / `round-robin`(`stickyRoundRobinLimit` 默认 3);
  `clearAccountError`(~298)= 成功后的清锁逻辑。
- `src/sse/handlers/chat.js` —— 换腿主循环。⛔ **它不在 `open-sse/handlers/`**
  (那个目录里只有 `chatCore.js` 一族),别再去那里找 `chat.js`:`:229` `excludeConnectionIds`、
  `:233` `while(true)`、`:304` 成功清锁、`:310` 成功即返回、`:327` 标坏、`:331` 排除并续循环。
  同一套循环也在 `embeddings.js`/`search.js`/`videoGeneration.js`/`fetch.js` 里 ⇒
  改换腿语义会**同时影响这些通道**。

⚠️ 本次挂死转移**只动了 `executors/cursor.js` 和 `errorConfig.js` 一行**,
`accountFallback.js` / `auth.js` / `chat.js` 一个字没改。

## 脚本清单(`scripts/9router-cursor/`)

| 脚本 | 用途 |
|---|---|
| `ship-via-198-registry.sh` | 现役投递:188 build → MARKER 门禁 → Mac 中转 → 198 registry →(可选)set image |
| `probe-fold.sh` | f7/f8 折叠三条腿 + 负对照(合成绿)。⛔ **只适用于非流式网关**(`cc.auto-link.com.cn/pro`);直打 9router 会全假红且 NEGCTL 恒绿 ⇒ 用 `probe-fold-sse.js` |
| `trace-9router.sh` | 9router 侧日志量具(POST/DONE/**ERROR/failover/stall**/sessions_history/declin/**all_locked**);判据 `POST==DONE+ERROR`,`POOL_EMPTY` 与 `NO_TRAFFIC` 已分开 |
| `acceptance-feishu-doc.sh` | 真实工具链验收:让被测模型真建飞书文档 |
| `verify-doc-by-control.sh` | 对照模型读回该文档,防自报自证 |
| `h14-feishu-delivery-triage.sh` | 飞书 WS 车道分诊(不是 9router 的病) |
| `regress_probe.js` | 4 场景旧探针,C(多轮 tool 结果回灌)仍有用 |
| `add-cursor-account.py` | 账号池:`--list` 判每条腿死活 / `--apply` deep-login 换 IDE token 后加号 |
| `verify-pool-failover.sh` | 账号池**挂死转移**验收:逼 leg2 进轮转当真红,四道闸,trap 还原策略 |
| `probe-fold-sse.js` | 折叠三兄弟的 **SSE 版**,直打 9router 用这个(pod 内跑,key 现取不落盘) |
| `sideload.sh` | ⛔ 历史参考,step5 已坏,别上线 |

## 低层编码器备忘(cursor.js 内)
`PROTOBUF_VARINT=0`/`PROTOBUF_LEN=2`;`agentString(f,v)`/`agentMessage(f,v)`=
`encodeField(f,LEN,v)`;`encodeField(fieldNum,wireType,value)`(LEN 收 string/Uint8Array/Buffer);
`concatBuffers(...)`;`wrapConnectRPCFrame(payload)`;`decodeMessage(buf)`→`Map(field→[{wireType,value}])`,
**LEN 的 value 是 Buffer**(`String(buf)` = utf8)。响应封帧统一:
`AgentClientMessage.interaction_response(6){ InteractionResponse{ id(1), <mirror field>(N)=<Result> } }`。
