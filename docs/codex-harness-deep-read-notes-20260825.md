# Codex Harness 二轮精读笔记(为 cursor-g 协议重写 S4 服务)

> 2026-08-25 · 源码 `~/codes/codex` commit 343074d420 · 每趟一节,读完即记,末尾总反思
> 纪律:结论全部钉本次实读的 file:line,08-24 旧锚点只当路标不当证据

---

## 第 1 趟:轮次循环——「一轮怎么开始、怎么算完、怎么算废」

### 结构(实读坐实)

- 轮主循环在 `core/src/session/turn.rs:301`(外层 `loop`),**没有 codex.rs**(旧记忆里的路标已过时)。
- 一轮(turn)= N 次采样请求(sampling request);每次采样 = 一条模型流。外层循环每圈:
  排水 pending 用户输入(:305)→ 捕获 step_context(:334,**一次捕获,上下文/工具表/工具调用共享同一请求视图**)→
  从历史构造全量输入 `clone_history().for_prompt(...)`(:370-374)→ `run_sampling_request`(:381)→ 按结果决定续圈或收工。
- `ModelClientSession` 是**轮级**的,轮内重试复用同一实例(WS + sticky 路由缓存,:293-294 注释)。

### 「继续动手 vs 收工」判据 = 三个独立信号取 OR,不是猜

1. **item 级**:模型吐了工具调用 → `needs_follow_up = true`
   (`core/src/stream_events_utils.rs:326`;工具调用不合法但可回话 → 也 true,:382——**错误答复也是继续的理由,不是终态**)。
2. **服务端级**:`response.completed` 响应体里有 **`end_turn: Option<bool>` 字段**
   (`codex-api/src/sse/responses.rs:119`,:471 传播)——`end_turn == Some(false)` → 强制续圈
   (`turn.rs:2577-2579`)。**模型/服务端有一个显式的"我说完没有"声明位**。
3. **队列级**:用户轮中追加输入 `has_pending_input` → 续圈(`turn.rs:411-423`)。

`!needs_follow_up` → 收工,返回 `last_agent_message`(`turn.rs:500-506`,:588)。

### 其它承重细节

- **工具调用 item 一 done 就立刻落历史**(`stream_events_utils.rs:316` record_completed_response_item),
  执行以 future 形式排进 `in_flight`(FuturesOrdered,`turn.rs:2391-2392`)——**"调用已发生"这个事实先于结果被持久化**。
- 流事件循环里,`Completed` 一到就 `break Ok`(`turn.rs:2580-2583`);流在 completed 前干净关闭 = 硬错
  `"stream closed before response.completed"`(`turn.rs:2281-2285`)。
- 空产出(0 item + completed 且 end_turn 非 false)= 合法收工,`last_agent_message=None`——
  **codex 不把"空"当错,因为服务端 end_turn 声明位兜住了语义**;我们没有这个位,空=歧义,这是网页线必须补的洞。

### 反思(对我们的 S4)

- **判据是"结构性事实"不是"文本长度"**:codex 判继续/收工靠 ①有没有工具调用 item ②end_turn 声明位 ③队列——
  全是结构信号,零启发式。我们 r16 的 `<24 字符` 长度门在这个世界观里根本不存在。
  → S4 里我们的第一趟判据应该同样结构化:**「收割到闭合信封」=继续,「收到 DONE 记号」=收工,两者皆无=废轮**,
  不许再有任何长度/内容启发式。
- **end_turn 声明位值得在文本协议里复刻**:让模型在第二趟末尾必吐一个终止记号(如 `⟨END⟩`),
  等价于把 end_turn 从服务端字段搬进文本流——收不到就当废轮重建,而不是把半截当全部。
- **"调用先落账,结果后到"**:我们收割到信封应立即持久化(记 call_id + 原文),再去执行——
  执行失败时账本上仍有"发生过调用"的事实,回填 error output 就不产生孤儿。

---

## 第 2 趟:失败→重建——「废了之后精确发生什么」

### 重建的精确机制(`turn.rs:1340 run_sampling_request` 内层 loop :1368)

- **第一发**用调用方传入的 `initial_input`(:1369-1370);**任何重试发**输入直接
  `sess.clone_history().for_prompt(...)` **从历史重建**(:1372-1374)。
- **历史是唯一事实源,且重建自动"接着干"**:失败流里已闭合落史的 item(含工具调用)都在历史里,
  重建出的 prompt 天然包含模型断点前已完成的工作;delta 从不落史,断点后半截自动消失。
  **重建 ≠ 从零重来,= 从最后一个已闭合 item 继续。**
- 快速失败在循环体内联分类:`ContextWindowExceeded` → 立刻返回 Err 且标记 token 满(:1405-1407);
  `UsageLimitReached` → 更新限额快照后返回 Err(:1409-1415);其余走 `is_retryable()` 门(:1424)。

### 三层重试结构(`core/src/responses_retry.rs:44`)——比"预算+backoff"精细得多

1. **网络断连专列(无预算上限)**:`ConnectionFailed` 走独立计数器,5s 起步、×2、封顶 60s
   (:17-18,:58-83),用户看到 "Reconnecting... waiting for network"。**断网不算失败,算等待**。
2. **常规可重试错误(有预算)**:`retries < max_retries` → 延迟 = **服务端建议的 retry_delay 优先,
   没有才用本地 backoff**(:105 `err.retry_delay().unwrap_or_else(backoff)`),用户看到
   "Reconnecting... n/max"(:116-121;注释原话 :114-115 "instead of staring at a seemingly frozen
   screen"——**可见性是设计进去的,不是顺手打的 log**)。
3. **预算耗尽 → 换传输 + 重置预算**:能切 WS→HTTPS 就切,**`retries = 0` 给兜底传输一份全新预算**
   (:85-99);没得切了才把 Err 抛出去(:128)。

### 错误穿透到轮层的终局(`turn.rs:574-584`)

- 终端错误 = 记 telemetry + 发**诚实 ErrorEvent** 给客户端 + `break`;注释原话(:582)
  **"let the user continue the conversation"**——轮死了,对话必须还活着。零无声、零假输出。

### 反思(对我们的 S4)

- **我们的"失败→重建"必须以持久 conv 为事实源**,且要利用"已闭合信封已落账"这一点做**断点续跑**
  而非全量重问——这正是 codex 重建循环的形状,而我们的 conv_id + parentMessageId 链就是现成的历史。
- **错误分类要三层,不是一刀切**:网页线也该分 ①网络/CF 抖动(可宽松重试)②上游 5xx/流断(预算内重试)
  ③确定性失败(空 completion 恒定复现、额度尽——**零重试直接诚实报错**)。
- **服务端建议延迟优先于本地 backoff**:网页面 429 响应若带 Retry-After 要认。
- **换路径后重置预算**:我们若做"信封重问失败 → 降级纯聊天交付"这类兜底切换,切换后也该给新路径
  独立的一次机会,而不是带着旧计数进场。
- **终局必须是诚实报错**:预算耗尽给 Cursor 一条明确错误(SSE error event),
  让用户能立刻重发;绝不 silent 空转或吐 1 字符假答案——这是 §0.1 原则 2 的原文依据。

---

## 第 3 趟:错误分类学——「哪些错值得重试」

### 完整分类(`protocol/src/error.rs:364 is_retryable`,本次逐行实读)

**不可重试(直接终局,给用户诚实错误):**
- 用户意图类:TurnAborted / Interrupted
- 额度与窗口类:QuotaExceeded / UsageLimitReached / UsageNotIncluded / SessionBudgetExceeded / ContextWindowExceeded
- 请求与配置类:InvalidRequest / InvalidImageRequest / UnsupportedOperation / ToolCollision / EnvVar / RefreshTokenFailed
- 策略类:CyberPolicy / MisalignmentPolicyViolation
- **⭐ ServerOverloaded ——反直觉:服务端过载不可重试**。设计哲学:上游都喊挤了就别再锤,立刻告诉用户。
- RetryLimit 本身不可重试(防递归)、Fatal / Sandbox / Spawn 等本地硬错。

**可重试(预算内):** Stream / Timeout / RequestTimeout / UnexpectedStatus / ResponseStreamFailed /
ConnectionFailed / InternalServerError / InternalAgentDied / Io / TokioJoin / **⭐ Json**
——**JSON 解析烂 = 瞬态错,重试**(:399)。对应到我们:**"半截信封/烂 JSON"在 codex 分类学里就是该重试的
Stream 级失败,不是该启发式打捞的东西**。

### 延迟来源的优先级链

- 错误自带 `retry_delay`(`error.rs:406`,由 `with_retry_delay` 注入):服务端 rate-limit 报错文本里
  `"try again in 11.054s"` 用正则抠出(`codex-api/src/sse/responses.rs:715`,大小写不敏感,支持 s/ms)。
- 没有服务端建议 → 本地 backoff:`INITIAL_DELAY_MS × FACTOR^(n-1) × U(0.9,1.1)`(`core/src/util.rs:86-91`)。

### 反思(对我们的 S4:网页线错误分类表初稿)

| 网页线现象 | codex 对应类 | 处置 |
|---|---|---|
| 流中断/超时/上游 5xx | Stream/Timeout/ISE | 预算内重建重试 |
| 半截信封/烂 JSON 信封 | **Json(可重试)** | 预算内重建重试(**不打捞**) |
| CF 403/网络抖动 | ConnectionFailed | 专列宽松重试(5s→60s 封顶) |
| 空 completion 恒定复现(xhigh 前科) | 无直接对应——**属 InvalidRequest 性质(请求形状对该模型就是错的)** | **零重试,诚实报错**;归类判据=同输入连续 2 次同样空 → 判确定性 |
| 网页额度尽/登录态失效 | UsageLimitReached/RefreshTokenFailed | 零重试,报错并提示换 lane |
| 上游明示"稍后再试" | retry_delay | **服务端建议延迟优先于本地 backoff** |

- **关键领悟:分类学的价值在"不可重试"名单**。codex 23 类不可重试 vs 11 类可重试——大部分失败
  **不该重试**,重试是特权不是默认。我们现在的线相反:什么都兜、什么都救,没有一个"直接死给用户看"的分支。

---

## 第 4 趟:配对与孤儿——「烂数据怎么保证不出门」

### ⭐ 最大发现:校验跑在出口边界,不在写入时

- `normalize_history` 的唯一调用链 = `for_prompt`(history.rs:206)→ `for_prompt_annotated`(:214)
  → `normalize_history`(:218→:450)。**即:每次从历史构建 prompt(首发和每次重建)都幂等重跑校验**。
- **设计哲学:历史允许脏(中断、崩溃随便留孤儿),出口边界保证净**。不追求"永远干净的账本",
  只追求"出门的每一份 prompt 都干净"——幂等、无状态、每次全量。

### 四条不变量(history.rs:446-449 docstring 原文)

1. 每个 call(function/custom)必有对应 output;
2. 每个 output 必有对应 call(或是具名外部工具事件);
3./4. 剥掉模型不支持的图/音内容。

### 孤儿处理的两个方向不对称(normalize.rs)

- **有 call 无 output** → **合成补齐**:两遍扫描(先收集全部 output 的 call_id 集合,再扫 call),
  缺配对的在 **call 原位之后**插入合成 output,正文 `"aborted"`(:21-127);
- **有 output 无 call** → **直接删除**(`remove_orphan_outputs`,history.rs:457)。
- 合成 output 的 id = 固定 namespace 的**确定性 UUID**(normalize.rs:19),注释原话:
  "Changing this value would change model-visible IDs and invalidate prompt caches"——
  **确定性是为了缓存前缀稳定,同一孤儿每次重建合成出同一个 id**。

### 顺手的第三发现:截断也在出口边界

- `process_item`(history.rs:466)在 prompt 构建时对工具 output 做截断(×1.2 序列化预算),
  **历史里保留全文,出门时才截**——与"历史=事实源,出口=策略层"同一哲学。

### 反思(对我们的 S4)

- **我们的 call_id 校验应该放在"每次向上游发请求前",幂等全量跑**,而不是试图在每个写入点维护配对。
  bpi 的 conv 历史(parentMessageId 链)允许脏;构建下一发 prompt 时跑 normalize:
  信封 call 无结果 → 原位合成 `{"call_id":X,"output":"aborted"}`;结果无 call → 删。
- **合成物的 id 必须确定性**(同一孤儿重建N次=同一 id),否则每次重建都产生新文本,
  上游对话树的前缀稳定性被打穿(我们的 conv 粘连同理受益)。
- **方向不对称要照抄**:缺结果=补(模型需要知道"调用发生过但没成");缺调用=删(没头的结果只会误导)。

---

## 第 5 趟:终止符的服务端语义——「completed 到底承诺了什么」

### ⭐ 结局是三分法,不是二分法(`codex-api/src/sse/responses.rs`)

| 结局事件 | 语义 | codex 处置 |
|---|---|---|
| `response.completed`(:464) | 正常结束 | 携带 `{response_id, usage, end_turn}`(struct :114-120)→ 收流 |
| `response.failed`(:408) | 服务端宣告失败 | **流层就地分类**:context_window/quota/usage_not_included/policy/invalid_prompt/overloaded 各归各类,其余归 Retryable 且顺手抠 retry-after 延迟(:441-443) |
| `response.incomplete`(:453) | **服务端显式说"我没说完"+ reason** | 归 Stream 错(可重试),reason 进错误文本(:460-462) |

**"没说完"是一等公民事件,不是靠客户端猜出来的。** 这是我们网页线最缺的一块:上游不会替我们说
"这条是半截",我们的协议必须让**模型自己**承担这个声明义务(终止记号),收不到就按 incomplete 处理。

### completed 的承诺边界(诚实记录:比我预想的少)

- `ResponseCompleted` **只有** id + usage + end_turn(:114-120)——**没有 item 清单/计数校验**。
  codex 不核对"收到的 item 数 == 服务端发的 item 数";完整性交给传输层(TLS/SSE 顺序性),
  completed 只负责宣告"结束了"+ 记账 + 续轮信号。
- → 对我们的启示要打个折:**codex 没做清单校验,是因为它的传输层可信**;我们的"传输层"是模型嘴,
  item(信封)和终止符走同一条有损通道。**要不要在终止记号里带 item 计数,是我们比 codex 更严的
  自选动作,不是照抄**——放进 S4 用 S3 数据决定(计数记号会增加模型服从难度)。

### 容错的分层(鲁棒但有硬底)

- **单个 SSE 事件解析烂 → debug log + `continue` 跳过**(:582-593),不崩流;
- **未知事件类型 → trace/debug 忽略**(:497-518),前向兼容;
- 但硬底不动摇:**EOF 无 completed = 硬错**(:565-571 "stream closed before response.completed"),
  **idle 超时 = 硬错**(:554 每个 chunk 读都包 `timeout()`,:572-577),**completed 本身解析烂 = 硬错**(:474-478)。
- **模式:噪声可以宽容,结局必须严格。** 中途烂一个事件没关系,但"怎么结束的"零含糊。

### 顺带坐实

- `response.function_call_arguments.delta/done` 在"明确不处理"名单里(:501-502)——
  codex 从不自己拼参数 delta,只认 `output_item.done` 的完整 item(:352)。

---

## 总反思:Codex 轮次状态机 → 网页线协议状态机 映射表

### 七条精华(全部钉了 file:line)

| # | Codex 原则 | 原文锚点 | 我们怎么搬 |
|---|---|---|---|
| 1 | 收工判据全是结构信号(工具 item / end_turn 位 / 队列),零长度启发式 | turn.rs:2397,2577-2579;stream_events_utils.rs:326 | 第一趟:闭合信封=继续,DONE 记号=收工,两者皆无=废轮。删光长度门 |
| 2 | 结局三分法:completed/failed/incomplete,"没说完"是显式事件 | responses.rs:408/453/464 | 终止记号协议:模型必吐 ⟨END⟩;缺=incomplete=废轮重建,不打捞 |
| 3 | 噪声宽容,结局严格:中途烂事件跳过,EOF 无终止符=硬错 | responses.rs:582-593 vs :565-571 | 信封中途文本乱可容忍;轮次结局零含糊 |
| 4 | 重建=从历史断点续跑,不是从零重来(闭合 item 已落史,delta 从不落史) | turn.rs:1368-1375;2114 | 以 conv 树为事实源重建;已收割信封已落账,重发只补断点后 |
| 5 | 重试三层:断网无限等(5s→60s)/常规有预算+服务端延迟优先/耗尽换路径且预算重置;终局=诚实报错 | responses_retry.rs:58-83,102-126,85-100;turn.rs:574-584 | 网页线照抄三层;xhigh 类确定性空=不可重试名单;预算尽给 Cursor 真错误 |
| 6 | 不可重试是大名单(23类),重试是特权不是默认;ServerOverloaded 都不重试 | error.rs:364-404 | 建我们的分类表(见第3趟);烂JSON信封=可重试,确定性空=不可重试 |
| 7 | 配对校验在出口边界幂等跑,历史允许脏;缺output=原位合成"aborted"(确定性id),缺call=删 | history.rs:206/218/450;normalize.rs:19-127 | 每次向上游发请求前跑 normalize;合成id确定性保 conv 前缀稳定 |

### 对既有设计三原则(plan 文档 §0.1)的复核

- 原则1(网关分配模式)✅ 被第1趟强化:codex 的判据全在 harness 侧结构化完成。
- 原则2(重试预算+分类)✅ 被第2/3趟细化:要三层不是一刀切;**新增"换路径后预算重置"**。
- 原则3(协议趟零外流)✅ 被第5趟旁证:codex delta 只给 UI、不落史;我们协议趟不给客户端字节。
- **新增候选原则4:出口边界幂等校验**(第4趟)——配对/孤儿在每次构建上游请求时全量重跑,不维护"永远干净"的状态。
- **新增候选原则5:结局三分法**(第5趟)——把 incomplete 从"异常"升格为协议一等公民。

### 两条诚实边界(防止过度搬运)

1. completed 无清单校验——codex 信传输层,我们信不了模型嘴;"终止记号带 item 计数"是**超出 codex 的
   自选加严**,要 S3 数据支持才上(服从率代价未知)。
2. end_turn 是服务端字段,我们的 DONE/⟨END⟩ 是模型吐的文本——**可靠性天花板不同**,我们的版本
   必然有漏吐率,所以第 2/5 趟的"废轮重建+预算+诚实报错"链路才是真正的承重墙,记号只是第一道闸。

---

## 事后反思(2026-08-26,S1/S3 实测数据回灌后补)

1. **原理≠诊断(本轮最大教训)**:本笔记提取的机制全部站住,但最初被拿去治一个 S1 证明已被 r16
   治好的病(近空 0/27)。分叉门流程救了场。原理是弹药不是靶子,靶子只能靠测量定。
2. **精华排序修正:可观测性第一,状态机第二**。S1 最大发现——合法 prose 11/11 全走
   `envelope miss` 打捞出口,合法/失败终态共用代码路与日志——证明没有 verdict 层连"病在不在"
   都测不出。codex "instead of staring at a frozen screen"(responses_retry.rs:114)不是配角,
   是第一交付件。
3. **新原理(笔记原文没有,P-a/P-b 实测赠送):结构性契约 ≫ 位置性记号**。
   "整个回复必须是一个信封或 DONE"= 20/20 满分;"末尾追加 [END-OF-ANSWER]"= 3/20 惨败。
   完整输出形状是高显著度约束;尾部追加 token 跟模型"自然收尾"习惯竞争,还可能被网关
   held-尾段过滤吃掉。**协议记号必须定义整个输出的形状,不能是追加物。**(这也回头解释了
   codex 为何用 end_turn 结构字段而非让模型说"我说完了"。)
4. **协议的语义盲区(P-c 实证)**:ll 轮在三分法里判 complete——流利的答非所问穿透任何结构协议。
   协议管"脑→手的通道",管不了"脑听没听懂";harness 只能让失败干净,不能让答案正确。
   codex 同理。任何基于本笔记的设计不许承诺语义正确性。
5. **end_turn 补不出来已实证**(P-b):INCOMPLETE 判据只剩结构信号(0 字符/半截栅栏/流终止),
   此盲区永久存在,写进验收口径,不藏。

