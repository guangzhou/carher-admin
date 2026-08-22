# zk-cursor-bpi web 通道 responses.js 迭代 SOP（Cursor web 份额 function-calling）

r8→r18 十轮实战沉淀。对象：CM `zk-cursor-bpi-patch` 的 `responses.js` key（namespace
`litellm-product`，唯一消费者 deploy/zero-cursor-bpi，端口 8201），承载
`cursor-web-fc-terra{,-high,-max}` 三个模型的 web 份额工具调用。

## 心智模型（改代码前必读）

```
Cursor(/v1/responses, 19工具+9.4K instructions)
  → 198 LiteLLM(hook cursor_web_fc_sys_rewrite 注入 [EXECUTION ENVIRONMENT])
  → pod:8201 responses.js(本 SOP 的对象)
  → 网页 ChatGPT(有状态: conversation_id/parent_message_id; thinking 模型)
```

- **Cursor 只从 `content_part.added`/`output_text.delta` 渲染正文**；只发 item.done 不发
  delta = 界面判空。empty_response 判据是 usage 的 outputTokens（CJK 要诚实计数）。
- **上游 SSE 消息有身份**（author.role/recipient/content_type）：python/container.exec
  代码与散文走同一条 append 通道，不看身份消费文本必漏内部通道给用户。
- **`function_call_output` 的内容在 `output` 字段不在 `content`**：flatten 不转译则模型
  对工具结果全盲（"回程"与"去程"同等重要）。
- **web 份额工具调用 = best-effort**。三条"动手"通道（sandbox shell / sandbox python /
  JSON envelope）都要能收割；"只说不做"prose 要检测+强制行动重问。100% 只有 codex 原生 FC。
- usage 必须报**客户端手里那份**上下文（不是网关压缩后实发的），否则客户端原生压缩永不触发。

## 迭代循环（每轮固定六步）

1. **取证**：用户现象 + Cursor 本地结构化日志
   （`~/Library/Application Support/Cursor/logs/<ts>/window*/exthost/anysphere.cursor-always-local/*.log`，
   看 `nal.tool_call.*`/`nal.empty_response.*`）+ pod 日志决策行（见下）。
2. **写 anchor-assert 补丁脚本**：`scripts/zk-cursor-web/patch_roundN.py` 模式——
   `assert src.count(anchor)==K` 锚点唯一性硬校验，产出 `/tmp/responses.rN.js`，
   **必过 `node --check`**。
3. **备份**：`kubectl get cm -o json` → `/Data/backups/zk-cursor-bpi-cm-<ts>-pre-rN.json`。
4. **部署**：CM 有 12 个 key，**只能 `kubectl patch cm --type merge --patch-file`**
   （`create --from-file` 会抹掉另外 11 个）。patch 后校验 key 数=12，再
   `rollout restart deploy/zero-cursor-bpi` + `rollout status`。
5. **回归**（litellm-proxy pod `/tmp/cw/`，`MK=$LITELLM_MASTER_KEY`；源码备份在仓库
   `scripts/zk-cursor-web/` 与 198 `/Data/backups/zk-cursor-web-harness-*.tgz`，pod 重启后
   `kubectl cp` 回去）：
   - `sse_dump.py <scenario>`——事件序列 + delta_chars；
   - `cmp_delta_done.py <scenario>`——delta 拼接==done 逐字符 + PUA/cite 残留。
     **只适用 prose 场景**：tool-call 场景 0 delta 报 MISMATCH 是预期；
   - `loop_ls.py` / `loop_dl.py`——**闭环验收**（call→回灌结果→下轮），单轮重放测不出
     回程 bug 和"只说不做"；
   - `timing.py <scenario> [effort]`——延迟与 effort 透传。**前门 SSE 无 `event:` 行，
     type 在 data JSON 里**；
   - 计费：litellm-db-0 查 SpendLogs，`model`+`model_id` 两列一起看，spend>0。
6. **记账**：memory 长log（feedback_cursor_gpt_web_toolcall_collapses_under_full_payload）
   追加本轮条目：根因/修法/验收/备份路径/诚实项。

## 环境与访问

- 直连 SSH：`sshpass -p '<pw>' ssh cltx@10.68.13.198`；`echo '<pw>' | sudo -S kubectl ...`
  （jms 间歇 Permission denied，别用）。
- 回滚：apply 备份 CM + rollout restart；或 env 开关秒关（见下表）。

## env 开关总表（kubectl set env deploy/zero-cursor-bpi X=0 即关）

| 开关 | 功能 | 轮次 |
|---|---|---|
| ZK_CHAT_ONLY | 问候快路（不套工具框架） | r6 |
| ZK_CHAT_FALLBACK | 近空回退纯聊天重问 | r4 |
| ZK_DIET / ZK_HISTDIET | 框架散文瘦身 / 历史工具输出截断 | diet/r18 |
| ZK_CONV_REUSE / ZK_CONV_TTL_MIN | 会话复用增量续发 / TTL(默认240min) | conv/r18 |
| ZK_ACT_RETRY | "只说不做"强制行动重问 | r14 |
| ZK_TE_DEFAULT | web-tools 轮默认 thinking 档(standard) | r17 |
| ZK_HB | 主路径 5s 保活注释帧 | r18 |
| ZK_WEB_RETRY=1 | 旧版 envelope 升级重试(默认关) | - |

pod 日志决策行可 grep：`[chat-only] [conv] [diet] [harvest] [act] [te] [cite] [stall] [turn]`。

## thinking 档位三通道（全部 live 实证）

1. 客户端 body `reasoning.effort`/`output_config.effort`/`reasoning_effort` 经 LiteLLM 透传
   （真实 Cursor 不发，不可依赖）；
2. **主用法**：LiteLLM `/model/new` 的 `litellm_params.reasoning_effort` 注入——已注册
   `cursor-web-fc-terra-high`(→extended)/`-max`(xhigh→max)，Cursor 切模型名即切档；
3. pod `ZK_TE_DEFAULT` 默认档。映射：low/medium→standard, high→extended, xhigh→max。

## 高频坑（每条都真踩过）

- delta 数 ≠ done 数先想 **UTF-16 vs 码点**（emoji JS 计 2 / Python 计 1），不是丢字。
- 流式收口"切尾续发"只在交付文本以已流出文本为**前缀**时合法；替换文本必须
  "收口悬空 shell + 新 item 另发 + 换新 item id"。
- 重试/回退轮开的新会话要**回传 conversation_id 重存缓存**，否则下轮 fork 回旧分支。
- 引用剥离器 held 缓冲被非 body 字符打断时，单 token 标记残段（citeturn…）按 EOF flush
  同政策丢弃，否则可见残留。
- setInterval 回调引用的变量若在 `let` 声明前启动定时器 → TDZ ReferenceError **杀 node 进程**。
- 合成探针全绿 ≠ 真流量能过：真实 payload 才有 instructions/hook 注入层；端到端验收必须
  走 LiteLLM 前门 + 真实抓包重放，最终 ground truth 是 Cursor GUI 点击。
- cap 抓包字段表可能缺 `instructions`——"字段为空"的结论先确认抓包器抓没抓。

## 未做的下一层杠杆

acct101 账号级契约（行动协议写进 ChatGPT 账号自定义指令，优先级压过 Cursor persona）；
多账号扩池破 acquireSlot 串行；codex 原生 FC（快+100%，烧 codex 份额，见
`.claude/plans/indexed-riding-mountain.md` 的 cursor-fc-* plan）。
