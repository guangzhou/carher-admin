# zk-cursor-bpi web 通道 responses.js 迭代 SOP（Cursor web 份额 function-calling）

r8→r18 十轮实战沉淀。对象：CM `zk-cursor-bpi-patch` 的 `responses.js` key（namespace
`litellm-product`，消费者 deploy/zero-cursor-bpi + zero-cursor-bpi-82（可克隆扩线），端口 8201），
承载 `cursor-web-fc-*` 全族的 web 份额工具调用：单线模型 `cursor-web-fc[-N]-terra{,-high,-max}` +
组池别名 `cursor-web-fc-pool-terra{,-high,-max}`（双线加权，见"组池"节）。

> **命名换装（2026-08-24 上线）**：面向用户的菜单已从内部代号 `cursor-web-fc*` 换成 `cursor-g-*`
> （见名知意、不暴露 "web"、池名无数字不暴露账号拓扑）。**旧 9 名全部冻结、零改零删，当调试工具**；
> 本 SOP 的迭代对象(responses.js/CM/env)完全不变。命名换装这一件事的活文档 =
> `docs/cursor-g-naming-rollout-20260824.md`。下面"cursor-g 命名换装"一节是速查。

## ⛔ 动手前必看：现网是三代命名共存 + lane→CM 已分叉（2026-09-02 11:53 实测）

**这两张表比本 SOP 其余任何一节都优先。** 底下大量段落写于 08-24~08-31，那时只有一份共用 CM、
只有 `cursor-g-*` 一代名字；照旧执行会**静默改不到 82**。

### 三代名字同时在现网（DB `LiteLLM_ProxyModelTable`，两个 proxy 副本一致）

| 代 | 行数 / 名字数 | 在哪 | 状态 |
|---|---|---|---|
| `cr-g-*` | 16 行 / 14 名 | **只在 lane 82** | 现役菜单（09-02 灰度中） |
| `cursor-g-*` | 55 行 / 31 名 | 6 个池别名（各挂 81/82/83/84/85 五条腿）+ 81/83/84/85 直连名 + 遗留 `cursor-g-82-sol` | 同事在用 |
| `cursor-web-fc-*` | 8 行 / 8 名 | — | terra 冻结期，调试用 |

### lane → CM 拓扑（**2026-09-03 01:30 实测**，以此为准；上一版表已过期）

| lane | 挂的 CM | `responses.js` sha | `ZK_SKILL_HINT` |
|---|---|---|---|
| `-135` `-136` `-137` `-138` `-139` `-140`（六腿） | **`zk-cursor-bpi-patch-135`** | `fc77f9e90e134981` = pool 版 + skill-hint/kick 五刀 | 1 |
| `-84` | `zk-cursor-bpi-patch-pool` | `6a1c249e33fc7467`（含 skill-hint v1 常量，默认关） | unset |
| `-82`（canary，**不许动**） | `zk-cursor-bpi-patch-82` | `ba2f5e77955b8893` | unset |
| `zero-cursor-bpi`(101，只读对照) | `zk-cursor-bpi-patch` | `701a7f50` 退化版 | unset |

⇒ **改 `-135` = 六条腿必须全滚**（`pool_consistency.py` 逐条验字节）；改 `-pool` 现在只影响 84。
⇒ 池名：13 个 `cr-g-*` 池名 7 腿（84+135~140），**`cr-g-5.6` 6 腿（无 84）**；直连名只有 `cr-g-*-135` 14 个和 `-82`，
136~140 **没有直连名**——要精准打某条腿得先建。池名按 key 钉一条腿，"只有 135 在动、别的没反应"多半是流量没到（看 lane `[PROMPT] REQ` 计数），不是坏。
真源 = `pool_consistency.py` 的 `FORKED_CM` + 跑一遍门；别背这张表。

## cursor-g 命名换装速查（2026-08-24 上线）

**hook gate 已扩成元组（唯一共享面改动）**：`cursor_web_fc_sys_rewrite.py`
`_TARGET_PREFIX = ("cursor-web-fc-", "cursor-g-")`（`str.startswith` 原生吃元组）。两前缀都 fire，
旧名行为零变化。改 gate 后必滚 proxy 盯 `rollout status` 到全就绪。

**上线 6 池别名（2026-08-24 Step 9 修正：litellm 层=点分载体，真身在 lane ALIASES 映射）**：

> ⚠️ **桥接承重机制（Step 9 事故教训）**：Cursor 对本线所有名字一律发 `/v1/chat/completions`
> （反编译 3.16.29 端点决策：非 api.openai.com host 且名不含 codex → chat）。能走 responses.js
> 正路全靠 **litellm `responses_api_bridge_check` 的 chat→responses 桥**，它只认**点分 gpt-5.4+
> slug**（`is_model_gpt_5_4_plus("gpt-5-6")=False`，横杠全 False）且要求 tools+reasoning_effort。
> **litellm 层注册横杠真身 slug = 拆桥 = chat 直达 lane chatgpt.js 坏路**（ToolCompiler 把
> api_key 当 IDE 名，`user is not a function` 崩→冷却→"No deployments available"）。名实相符由
> bpi CM `raw.js` ALIASES 载体→真身映射保证。**验收探针必须复刻 Cursor 真实线型**
> （chat+stream+tools+effort）——`/v1/responses` 合成探针全绿是假绿。

| 池别名（用户面，无数字） | litellm 载体 slug | lane 映射真身 | reasoning_effort |
|---|---|---|---|
| `cursor-g-5.6-sol` | `openai/gpt-5.6-sol` | `gpt-5-6` | `medium` |
| `cursor-g-5.6-sol-high` | `openai/gpt-5.6-sol` | `gpt-5-6` | `high`(→web extended) |
| `cursor-g-5.6-luna` | `openai/gpt-5.6-luna` | `gpt-5-6-t-mini` | `medium` |
| `cursor-g-5.6-pro` | `openai/gpt-5.6-pro` | `gpt-5-6-pro` | `medium` |
| `cursor-g-5.6-instant` | `openai/gpt-5.6-instant` | `gpt-5-6-instant` | `medium` |
| `cursor-g-5.5` | `openai/gpt-5.5-thinking` | `gpt-5-5-thinking` | `medium` |

> ⚠️ **本表 effort 一栏 2026-09-01 订正**：原写「不设(standard)」是错的，且这个错误直接导致了当天
> 的 82 事故（见下节）。**线上实测（`/model/info` 全量审计，63 行）：`cursor-g-*` 61 行
> 全部带 `reasoning_effort`（medium/high），一行不落**；唯二不带的 2 行是老 terra 线
> （`cursor-web-fc-terra`/`cursor-web-fc-pool-terra`，已弃用）。
> **`reasoning_effort` 不是"档位偏好"，它是桥的触发条件之一 —— 漏了这一行就崩（见上方承重机制）。**
> 建/改任何 `cursor-g-*` 行时它是**必填字段**，不是可选项。

- 每名各挂 101+82 两 deployment（`model_info.id=zerokey-cursor-g-{101,82}-<变体>`，weight:1、
  `api_key:sk-zerokey-web-noop` 占位符必带、`mode:chat`），WA 自动跨线负载+容灾。已授权 key03/key04。
- 运维直连名（带账号号,钉单线调试,自新号启用）：`cursor-g-<N>-5.6-sol…`。
- **⚠️ 「xhigh = 第三个虚构档」这条判决 2026-09-01 已撤回，别再照着它砍档**：当时的实测对象是
  真身 `gpt-5-6`，而上游 `/backend-api/models` 对它报 `configurable_thinking_effort: false` /
  `thinking_efforts: []` —— **它本来就一个档都没有**，测的是"给无档模型传 max"，结论**不能外推**。
  `gpt-5-6-thinking` 与 `gpt-5.6-{sol,terra,luna}-wm` 上游明确给 `[min,standard,extended,max]`。
  且当时的判据 `output_tokens=0` 与 `cursor-g-5.6-pro` 的空回显**同形态**，而 pro 那次已定因为
  **`stream_handoff` 第二通道没收割到**并修好了（见灵魂律令：空回显 = 我没接住，不是上游没产出）。
  **现在的正确说法**：`gpt-5-6` 无档（事实）；有档模型在 max 下的行为**没测过**（未知）。
  同理 `cursor-g-5.6-sol-high` 是**假档**（真身 `gpt-5-6` 无档，effort 传下去没有对应物）。
  加档前仍然要临时真 key 打实质 prompt，但**空回显的第一动作是打原始 SSE 帧看身份，不是判它虚构**。

> 📐 **本节 6 名的整表已被 `cr-g-` 22 名重构取代（2026-09-01 定稿，尚未动手）**：命名规则收敛成
> 一条——名字 = `cr-g-` + 真身点分写法 + 档位后缀，载体 = `openai/` + 真身点分写法（逐字等于真身）。
> 22 名 = 5 无档 + 1 pro + 4 thinking + 12 memory，覆盖 10 个真身。四处代码改动（hook
> `_TARGET_PREFIX` 加 `"cr-g-"`、raw.js ALIASES 改 1 加 4、WEB_MODELS 加 3 个 `-wm`、档位表加
> `minimal→min`）与三条未验证风险见记忆 `project_cr_g_22_name_menu_design_2026_09_01` 与飞书
> 文档 `G4x4dOqTmoqr1WxYn1uc5wd9nlb`。**上面这 6 名在 cutover 完成前照常有效，退役是最后一步。**

## 改一行现有模型（改名 / 换载体）—— 2026-09-01 事故后固化，必须逐条照做

用户说"把 A 改成 B"时，**这是加法，不是替换**。当天把它做成替换 + 默写，代价是用户生产两次中断、
一次 50~75s/发的全线报错，一句话的活修了两小时。

### 四步，顺序不许换

1. **add**：`/model/new` 建新名。**新行的 `litellm_params` 必须从一条活的同类行整份拷贝下来，
   只改要改的那一个键**（改名就只改 `model_name`；换载体就只改 `model`；换 lane 就只改 `api_base`）。
   **禁止凭记忆重新填一份** —— 当天就是默写漏了 `reasoning_effort`（顺带还漏了 `weight`、
   `use_chat_completions_api`），漏的那项恰好是桥的触发条件。参照行取法：`/model/info` 里
   `cursor-g-<别的号>-<同档>`（如 `cursor-g-81-5.6-sol`）。
2. **verify**：**逐 proxy 副本**查（当前 4 副本），不是"我 exec 进去的那台能用"。
   ```bash
   for P in $(kubectl get pod -n litellm-product --no-headers|awk '{print $1}'|grep ^litellm-proxy-); do
     kubectl exec -n litellm-product $P -- python3 -c '...读 /model/info 断言新名在且 effort 非空...'
   done
   ```
   `/model/new` 之后**必须 `kubectl rollout restart deploy/litellm-proxy` + `rollout status`**
   （禁 `kubectl apply`），否则新行只进了被打到的那个副本的内存路由表，其余副本报
   `No fallback model group found for original model_group=<新名>` → 走 fallback 链空等 → 用户
   体感"奇慢无比"，SpendLogs 里长这样：`n=1 avg=0.0s` 且 `model_id` 为空（压根没到 deployment）。
3. **cutover**：让用户在 Cursor 里切到新名，用他的日常用法确认好用。
4. **remove**：只有 3 过了才 `/model/delete` 旧行。**remove 永远不与 add 在同一个脚本里。**

### 硬门（当天缺这两个门才炸的）

- **`/model/new` 返 500 必须中止，不许继续往下删。** 500 的响应体是不透明的
  `{'error': 'Failed to add model to db...'}`，**真因只在 proxy pod 日志里**：
  `prisma.errors.UniqueViolationError: Unique constraint failed on the fields: (model_id)`
  ——`model_info.id` 全局唯一，而 `zerokey-cursor-g-82-sol` 早被池别名 `cursor-g-5.6-sol` 的 82 腿占了。
  直连行的 id 用 `zerokey-cursor-g-<N>-direct-<档>` 才不撞（81/83/84/85 全是这个命名，照抄即可）。
- **`/model/info` 是内存视图，会滞后于 DB**：删掉的行可能还在里面。判"到底有没有"以 DB + 逐副本为准。

### 验收探针的红线（当天第二次被自己的尺子骗）

**探针里绝不许出现真实 Cursor 不会发的字段。** Cursor **不发** `reasoning_effort`（它靠模型行注入）。
当天我在探针 body 里手写了 `reasoning_effort:"medium"`，于是探针替配置行补上了它缺的那一项 →
探针 TTFB 2.1s 全绿，用户同时在收 500。**量具含有被测对象缺的成分 = 红被构造性屏蔽，加多少发都绿。**
正确形状：`chat + stream + tools`，**不带 effort**，模型名带 `cursor-g-`/`cursor-web-fc-` 前缀
（否则 hook `cursor_web_fc_sys_rewrite` 不 fire），走 `/key/generate` 临时真 key（禁 master）。

### 报错签名速查（拿到用户截图先对这张表）

| 用户/客户端看到的 | 服务端真实错误 | 含义与修法 |
|---|---|---|
| `API 异常 (req: xxxxxxxx)` | 被 08-08 上线的对外脱敏改写了 | 拿 req id 去 proxy 日志 grep `error_sanitize: masked req=<id>` 取 `original=` |
| Cursor `Error in consumeStream: Failed after 3 attempts` + 每发 50~75s + `outputTokens 0` | 见下行 | 不是"慢"，是**在失败重试**；Cursor 重试 3 次 × LiteLLM 重试 5 次 |
| `TypeError: user is not a function at ToolCompiler.formatPrompt at /app/routes/chatgpt.js:52` | 同上 | **chat→responses 桥没触发**，请求直冲 lane 的 `chatgpt.js` 坏路。第一件事查该行 `litellm_params.reasoning_effort` 是不是丢了（其次查载体 slug 是不是被写成横杠真身） |
| `No fallback model group found for original model_group=<名>`（只有部分副本报） | — | 新/改的行没加载到全部副本 → `rollout restart` |

### Cursor 客户端日志才是这条线的判据

`~/Library/Application Support/Cursor/logs/<ts>/window*/exthost/anysphere.cursor-always-local/Cursor Structured Logs*.log`
——`key=composer|agent_exec` 那两类行带 `modelName`、`usedTokens/outputTokens`、
`Error in consumeStream` 与 req id，**是模型伪造不了的本机账本**。用户喊慢/喊坏时先读它，
再拿 req id 回服务端对账；不要先跑自己的探针。

**v2 克隆脚本 = `scripts/zk-cursor-web/clone_web_fc_lane_v2.py`**（v1 保留当 terra 冻结era 参照）：
一键出新线——step3 建 6 直连名 `cursor-g-<N>-*`（点分载体 slug、幂等）、step5 挂 6 池成员进别名、
grant 读旧合并 12 名、step6 临时真 key 自动验收（禁 master）。载体 slug 表内置、无 xhigh；
新线共用 bpi CM，ALIASES 真身映射自动生效。
两前提写进 docstring：①新号首次必须手抓 web seed（非 WS 成员时 `--live-from-ws` 不可用）；
②全部 lane 钉 standby 单 node（seed 是 hostPath）。live token 经 **ssh stdin** 传（不过 argv/命令
串，防 ps 泄漏）。dry-run 默认，`--apply` 执行。回滚=`/model/delete` 12 个 `zerokey-cursor-g-<N>-*`。


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
4. **部署**：
   > 🔴 **第 0 个动作：先查这条 lane 挂的是哪份 CM，别默认只有一份。**
   > ```bash
   > sudo -n kubectl -n litellm-product get deploy -o json \
   >   | jq -r '.items[]|select(.metadata.name|startswith("zero-cursor-bpi"))
   >            |"\(.metadata.name)  \([.spec.template.spec.volumes[].configMap.name//empty]|join(","))"'
   > ```
   > 09-02 起 82 走自己那份 `zk-cursor-bpi-patch-82`（见开头的分叉表）。
   > **patch 共用 CM + 全 lane rollout 这条老配方对 82 完全无效**——它会成功、会绿、
   > 而 82 一个字节都没变。要同时改两边就 patch 两份 CM，不要靠"重启一下应该就好了"。
   >
   > fork 出去的 CM **必须登记进 `pool_consistency.py` 的 `FORKED_CM`**（带日期 + 收敛条件），
   > 否则那条 lane 悄悄退出一致性门：A 段不再逐字节比对、B 段还会误报 dangling。

   CM 现有 **15 个 key**（会长，patch 前先 `get cm -o json | jq '.data|length'`
   记下当前值），**只能 `kubectl patch cm --type merge --patch-file`**
   （`create --from-file` 会抹掉其余所有 key）。patch 后校验 key 数没变，再
   **对挂了这份 CM 的每一条 lane** 依次 `rollout restart` + `rollout status`。
   > ⚠️ **改 CM 必须全 lane 重启，不是只重启有流量的那条。** 两条 lane
   > （`zero-cursor-bpi` = 101、`zero-cursor-bpi-82`）挂的是同一个 CM，但 pod 只有重启才
   > 把新文件 cp 进 `/app/routes/`。2026-08-31 只重启了 82，101 静默跑旧代码 27h —— 它当时
   > 零流量所以没人发现，可一旦 key 级亲和换绑或 82 被 fail-mark，bug 原样复发。
   >
   > **收尾硬门（退出码即判据）**：
   > ```bash
   > python3 scripts/zk-cursor-web/pool_consistency.py     # 0=通过，1=有 lane 在跑旧代码
   > ```
   > 它从「谁挂了这个 CM」反查 lane，**新克隆的 lane 自动进检查范围**，不用改脚本；
   > 比的是容器内 `responses.js` 的 sha256 与 CM 逐字节是否相等（不是"我记得重启过"），
   > 顺带查池别名的 lane 覆盖（dangling / 孤儿 lane / 各档腿数不齐）。
   > 门本身的双向实测：`pool_consistency_selftest.py`（注入坏 CM 与假 lane，4/4 必红）。
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

## 迭代循环之后：真 Cursor 验收（门①门②，唯一算数的那把尺子）

上面第 5 步的 harness 全是**合成**的。合成绿不构成上线依据，**合成红也不构成结论**——
09-01 那次「81~85 动手率 8~26%」就是合成红，据它改了文案、上了灰度、告诉了用户，全是假的
（探针打 `/v1/responses`，而这些名字全是 `mode:chat`）。**判据只能是真 Cursor。**

两个门：
- **门①（干净载荷）**：`ls` / `hello` 这种简单请求发到 GPT 网页端必须干净原样——
  不加转录体行标、不塞大 item、不触发 >20 万字符的文件上传路径。
- **门②（功能不准回退）**：shell 命令和飞书文档创建必须稳定出结果。

```bash
bash scripts/zk-cursor-web/crg_gate12_run.sh              # 八轮同一 chat，三处判据对齐
EXPECT_MODEL=cr-g-5.6-thinking-82 bash …/crg_gate12_run.sh # 验别的名字
python3 scripts/zk-cursor-web/crg_family_gate.py --list    # 先看覆盖有没有陈旧
```

**顺序是硬的，别跳**：

| 门 | 判据 | 跳过的后果 |
|---|---|---|
| ⓪ 屏幕锁没锁 | `ioreg -n Root -d1 -a` 里 `CGSSessionScreenIsLocked` | 锁着时 osascript 键击**全打在锁屏上**，`fired_ok` 照样 true，红是假红（09-02 01:03 踩过：六分钟零 `[PROMPT]`） |
| ① 选中模型对不对 | `state.vscdb` **两个**存储面，且**重启之后**再验 | 见下 |
| ② 阳性对照 | 先发一发 `hi`，nonce 必须出现在 82 的 pod 日志里 | 量具在答案已知的样本上都复现不出，后面的数一律不算 |
| ③ 三方对账 | pod 日志 + **Cursor 客户端执行账本** + 飞书逐 nonce 精确直搜 | 只看一处 = 假绿 |

**换选中模型有两个存储面，只改一个会被启动还原**（09-02 实测，比红更坏）：

| 面 | 路径 | 角色 |
|---|---|---|
| `ItemTable / applicationUser` | `.aiSettings.modelConfig.composer.{modelName,selectedModels[].modelId}` | 运行时 |
| `cursorDiskKV / composerData:empty-state-draft` | `.modelConfig.{modelName,selectedModels[].modelId}` | **启动还原源** |

只改前者：写盘成功、写后复查通过、门放行 → **启动 26s 后被改回旧值**
（11:11:15 写 / 11:11:16 启动 / 11:11:42 被改回），于是拿旧模型跑完一整轮还记成新模型的成绩。
⇒ 顺序必须是 **退出 Cursor → 两个面一起改 → 重启 → sleep 35s → 再复查 → 不符直接退出**。
零额度自检：`crg_family_gate.py --swaptest <名字>`（只换模型不发请求）。

其余纪律：
- 提示词里**不许加我自己发明的免责/限域从句**（`只读别改`、`别动我项目`、`工作目录 /tmp`）——
  实测那些从句恰好在教模型别动手。
- 三方对账的**时间窗口必须不重叠**：给每个系列的右边界加 settle 会串到下一个系列，
  命令长度和 convId 互相污染。正确右边界 = 下一个系列第一发 − 20s。
- 飞书判据**逐 nonce 精确直搜**，泛搜 15 条分页会撒谎。
- `pro`（走 `stream_handoff` 第二通道）与 `research`（deep research）**慢不等于红**，
  给它们更长的落地预算（`crg_family_gate.py` 的 `SLOW` 表：150s / 240s）。
- **覆盖别吹**：`crg_family_gate.py` 的 `FAMILIES` 按**载体（真身）**分系列，
  一个载体下还有 `-min`/`-high`/`-max` 三个走不同 `reasoning_effort` 的**档位变体**。
  「按载体各挑一个跑完」= 载体维度验过，**档位维度没验过**。`--list` 会把两者分开报，别混成"全绿"。

## skill-hint / skill-kick：让网页模型会搜本机 skill（2026-09-03 上线，六腿）

**问题**：用户说"建个飞书文档"，模型答「我没有飞书工具」收工——机器上明明有 `lark-cli` 和 27 个 `lark-*` skill。
**答案层选择**（都实测过，别再走回头路）：
- ❌ hook 层注入：lane 三处正则剥 `[EXECUTION ENVIRONMENT]…\n\n`（`_chatOnlyize`/`_stripExecEnv`/r15），写什么都被剥。
- ❌ 照搬 codex 全量目录（`render.rs` 一行一 skill）：本机 478 个 skill，光 `.cursor/skills` 45 个就 20721 字 vs 首轮 6803，撞门④；且那 45 条没一条匹配"建飞书文档"。
- ✅ lane `responses.js` 首轮握手支拼一段提示（`_sHint`，门 `ZK_SKILL_HINT=1`），只首轮发一次，增量轮 DIET-ZERO 不带。

**光有提示不够（三版实测）**：807c/1279c 文案模型都读到了仍拒（「当前环境没连你本机终端」）。翻转它的是**一条真实工具结果**。
所以 **skill-kick**：首轮拒绝（`_REFUSE_RE`）且无新鲜工具结果 → 流式层冻住拒绝词（复用 `wtFrozen`）+ 用 `execToToolCall(shellTool, grep…)`
发真 Shell 调用给 Cursor 跑；kw 由 `extractUserQuery` 推（飞书→`lark`、k8s、litellm、carher，否则 `ls` 三处目录）。
独立预算 `_skillKicked`（不吃 `actRetried`，搜完仍拒还能 prose 重问一次）；`_skillKickedConv` 记 convId，后续轮拒绝词也冻。
提示词末尾两句管啰嗦：「照 skill 干活只发块不播报」「本会话读过 SKILL.md 不重读」——中间步骤 `prose 0 chars`、同会话第二次 0 命令直接答。

**判据（真 Cursor，读 lane 日志）**：`[handshake] … skill-hint 1578c`；`[skill-kick] refusal spotted in stream … -> hold`；
`[skill-kick] refusal without tool result … -> real search call kw=lark (n=1)`；随后 `complete-run … prose 0 chars` 串；
`[url-prior]` 出 docx 链接（**日志截掉 URL 末字符**，拿会话原文全 token 用 `lark-cli docs +fetch --as user --doc <url>` 独立打开）。
Cursor 客户端账本 `nal.tool_call.start→success` 对数要等于 lane 的 `complete-run` 数。

**探针形状硬门**：打 `cr-g-*` 必须 chat + `stream:true` + **tools 非空**，不手填 `reasoning_effort`（配置提供）。
不带 tools → 不走 chat→responses 桥 → lane `chatgpt.js` 报 `TypeError: user is not a function` 500，且 LiteLLM 重试+fallback 会把六条腿各撞一次、留 60s `weighted_affinity:fail:v1` 标记。那是探针错，不是腿坏。

**补丁脚本**：`scripts/zk-cursor-web/skill-hint/patch_skill{hint,hint_v2,kick,kick_v2,kick_v3}.py`（anchor-assert，顺序执行）。
**回滚**：`docs/skill-hint-rollback-20260903.md`（秒关 = `set env deploy/zero-cursor-bpi-<N> ZK_SKILL_HINT-`）。
**GUI 驱动坑**：`cursor_gui_e2e_driver.fire()` 靠 pbcopy+Cmd+V，用户同时在用 Cursor/剪贴板会把我那发顶掉——发后必查网关收到的 `<user_query>` 是不是我的 nonce。

## 给同事开通（装机包 v3 + key 授权，2026-09-03 定稿）

**三件事缺一不可，且顺序固定**：①key 授权 → ②领取表里有 API Key + 飞书 App Secret → ③同事按文档装。
09-03 查过：~640 把 `cursor-*` key 里只有 4 把有 `cr-g-*`（我、shangwensheng、linsen×2）。**文档默认"已授权"，
没授权的人选 `cr-g-5.6` 直接 401** —— 同事报"用不了"先查这一条，不查代码。

1. **授权**：`scripts/zk-cursor-web/crg_key_grant.py --alias cursor-<name> [--alias …] --apply`。
   只加 14 个池名；不给 `-82/-135` 直连名（那是钉单腿调试用的）；不动 aliases（同事的 glm/kimi 映射是团队标配，
   我 key 上 8 条 `chatgpt-*→sa-gpt-*` 是个人实验别照搬）。判据 = DB 直读 `models` unnest 计数，不认 API 自述。
   `sa-grok-4.5/4.6` 同事 key 里早就有，进菜单不需要额外授权。
2. **装机包**：`scripts/zk-cursor-web/package_team_setup.sh` 出 `cursor-g-setup.zip`（1.9MB）。菜单 = cr-g 14 名 +
   `sa-grok-4.5/4.6`；`MODEL_PREFIXES=["cr-g-","sa-grok-"]`（单前缀会把选了 grok 的人 REPAIR 时打回默认）。
   第 6 步 lark **默认关（09-03 下午用户拍板：同事被飞书那步搞糊涂），`--lark` 显式才做，包默认不带 lark-skills**。做时全部有就跳过：lark-cli 1.0.90 从 `registry.npmmirror.com/-/binary/lark-cli/v1.0.90/` 拉官方二进制
   （sha256 = npm 包 `checksums.txt`；同事机器没 npm、GitHub 不通，这是唯一不依赖两者的路）→ 28 个官方 `lark-*` skill
   随包（`cp -RL ~/.agents/skills/lark-*`）拷到 `~/.claude/skills`，一个不覆盖 → `config init --app-id cli_a91569fab9b81bc6
   --app-secret-stdin`（提示粘，只走 stdin）→ `auth login` Device Flow。判活用 `auth status --json`（`appId` /
   `identities.user.status==ready`），`config show` 不认 `--json`。裸机路径与全有路径 09-03 本机都跑过；**Windows 分支未实测**。
**Cursor 版本兼容（09-03 3.18.25 事故）**：同事报「The model you chose is not available」→ 先 `bundle_anchor_probe.js`
（只读，不用退 Cursor）数 8 个锚点命中；任一 hits=0 安装器就整体拒绝 → gate 补丁没打 → 选择器拒自定义模型。
修法 = 正则 `(?:旧形状|新形状)` 两代都认、仍要求恰好 1 次；js/py 两份 + `setup_impl_parity.py` 一起过；重打包换附件。
已验版本：3.16.x / 3.17.19 / 3.18.25。新版本出来先跑探针。
**"Cursor 正在运行"假阳（09-03）**：zk-delta 小代理借 Cursor 二进制当 node 跑，第一次 INSTALL 装上后第二次跑安装器
`pgrep -f …/MacOS/Cursor` 把它当 GUI；`cursorRunning()` 已改成排除自身 pid + 带 `.js` 参数的进程。同事说"我明明退了"先想到这个。
**key 编辑三件套**：`crg_key_grant.py --alias`（14 池名）/ `crg_key_grant_all.py`（全量，pod 内跑，跳过 models 空的 key）/
`litellm_key_add.py --alias X --models … --copy-from Y`（任意 models+aliases 照参照 key 抄）。全部读-合并-写、备份、回读；判据 DB 直读。

3. **文档** `OQCPdLd4MovEVoxGzdMcD3CJnCf`：一页纸，用户无感的一律不写（小代理/增量/skill 机制/sha/备份目录）。
   换附件正解 = `cd` 到 zip 目录后 `docs +update --command append --content '<figure view-type="Card"><source path="@./x.zip" name="x.zip"/></figure>'`；
   `docs +media-insert` 会把 zip 插成 `<img>` 块。`block_replace` 大范围会撞"中间兄弟没 block id"，整篇重写用 `overwrite` +
   `<source token=…>` 把附件带回。

## 环境与访问

- 直连 SSH：`sshpass -p '<pw>' ssh cltx@10.68.13.198`；`echo '<pw>' | sudo -S kubectl ...`
  （jms 间歇 Permission denied，别用）。
- 回滚：apply 备份 CM + rollout restart；或 env 开关秒关（见下表）。

## env 开关总表（kubectl set env deploy/zero-cursor-bpi X=0 即关）

> ⚠️ **开关不在 deploy 的 env 里 ≠ 它是关的。** 不在 env = 走代码里的默认值，
> 而下面这些**默认全是开**。实测 82 的 env 里根本没有 `ZK_HANDOFF_*` / `CURSOR_G_*` 四个，
> 它们照样在生效。要确认某个特性开没开，判据是 pod 日志里那个 tag 的**命中计数 n>0**，
> 不是"我在 env 里没看见"，也不是"代码在那儿"。

| 开关 | 功能 | 轮次 |
|---|---|---|
| ZK_CHAT_ONLY | 问候快路（不套工具框架） | r6 |
| ZK_CHAT_FALLBACK | 近空回退纯聊天重问 | r4 |
| ZK_DIET / ZK_HISTDIET | 框架散文瘦身 / 历史工具输出截断 | diet/r18 |
| ZK_CONV_REUSE / ZK_CONV_TTL_MIN | 会话复用增量续发 / TTL(默认240min) | conv/r18 |
| ZK_ACT_RETRY | "只说不做"强制行动重问 | r14 |
| ZK_TE_DEFAULT | web-tools 轮默认 thinking 档(standard) | r17 |
| ZK_HB | 主路径 5s 保活注释帧 | r18 |
| ZK_SKILL_HINT=1 | 首轮 skill 提示 + 拒绝→真 grep 调用(skill-kick)，见专节 | 09-03 |
| ZK_WEB_RETRY=1 | 旧版 envelope 升级重试(默认关) | - |
| ZK_HANDOFF_POLL=0 | 关掉 `stream_handoff` 转轮询兜底(默认开) | 09-01 |
| ZK_HANDOFF_MAX_MS / ZK_HANDOFF_POLL_MS | 轮询总预算(默认 240000)/ 间隔(默认 2500) | 09-01 |
| ZK_HANDOFF_STALE_GUARD=0 | 关掉轮询**抢跑闸**(默认开)：按 convId 比对上一轮交付正文，逐字相同则继续轮询 | 09-02 |
| ZK_HANDOFF_STALE_MS | 抢跑闸最长等待 | 09-02 |
| CURSOR_G_DECONFLICT=0 | 关掉「剥 Cursor rule3 禁自定义工具格式」那一刀(默认开) | 09-01 |
| CURSOR_G_STRIP_PLAN=0 | 关掉「剥现役 task_management / mode_selection 段」那一刀(默认开) | 09-01 |

pod 日志决策行可 grep：`[chat-only] [conv] [diet] [harvest] [act] [te] [cite] [stall] [turn] [handoff]`
`[handoff-stale]` `[execenv-strip]` `[proto2]`。

> ⚠️ **同一个功能在不同分支可能打不同 tag**：强制行动重问在两条分支上分别是 `[act]` 和
> `[turn-verdict-v2]`。只 grep 一个 tag 就断言"这条 lane 没触发"是假结论（09-01 踩过）。
> 同理，**没有命中计数的过滤器可以空转任意久**——判据是 `n>0`，不是"代码在那儿"。

**`[handoff-stale]`（09-02 补的抢跑闸）**：上面那个轮询原本"取最新一条"，没判是不是**本轮**的，
于是会抢跑取到**上一轮**的消息 —— 用户面症状是同一条命令跑 2~3 次、问快排答 `ls`。
修法 = 按 convId 比对上一轮交付正文，逐字相同就继续轮询。判据只认 `[handoff-stale]` 那两行日志。
补丁 `patch_handoff_stale.py`；**注意它只落在 82 的 fork CM 里**（共用 CM 里 `stream_handoff`
出现 0 次）。

**82 的 responses.js 里有一句陈旧注释**：写着「只有 pro 产生 `stream_handoff`」——
09-02 实测为假（`-wm` 族也走这条通道）。下次动 82 时顺手改掉，别照它推断隔离性。

**`cursor-g-5.6-pro` 为什么需要 `[handoff]` 这条路**：`gpt-5-6-pro` 的主 SSE 走到
`stream_handoff` 事件就收口（options `resume_sse_endpoint` / `subscribe_ws_topic`），
正文在另一条通道继续生成 → 不跟就是 HTTP 200 + `output_tokens=0`（用户面"点了没反应"）。
resume 端点参数形状未知（7 种组合全 400/404/405），**实测可行的是轮询
`/backend-api/conversation/<id>`**。隔离性是构造性的：只有流里真出现 `stream_handoff` 才进得去，
而 sol / luna / instant / 5.5 **从不产生**该事件（实测对照：`gpt-5-6` 21 事件收在
`message_stream_complete`，pro 12 事件无该事件）。判读：`[handoff] stream_handoff …
-> 主流收口后转轮询` 然后 `[handoff] done in 13s polls=4 chars=15`。
补丁脚本 `scripts/zk-cursor-web/patch_handoff_poll.py`（3 个 `assert count==1` 锚点），
活文档 `docs/cursor-g-pro-stream-handoff-20260901.md`。

## thinking 档位三通道（全部 live 实证）

1. 客户端 body `reasoning.effort`/`output_config.effort`/`reasoning_effort` 经 LiteLLM 透传
   （真实 Cursor 不发，不可依赖）；
2. **主用法**：LiteLLM `/model/new` 的 `litellm_params.reasoning_effort` 注入——已注册
   `cursor-web-fc-terra-high`(→extended)/`-max`(xhigh→max)，Cursor 切模型名即切档；
3. pod `ZK_TE_DEFAULT` 默认档。映射：low/medium→standard, high→extended, xhigh→max。

## 会话复用机制（指纹/会话id/增量续发——常被误解为"加密压缩"）

**省输入的不是加密，是服务端状态**。网页 ChatGPT 后端有状态（`conversation_id` +
`parent_message_id`，pod `api.js:119` 原生支持，原作者三处调 `chatCompletion` 全传
null 把它废了）。命中复用时只发 conv id + 新增 items，上文由服务端会话自己记得——
不存在"把上文加密后发过去"（密文同样耗 token，上游也解不了）。

三个组件的分工：

| 组件 | 作用 | 关键约束 |
|---|---|---|
| SHA-1 逐项指纹（`_itemDigest`） | **本地防错闸，不出网关**：缓存记上轮每条 item 指纹，下轮逐项比对前缀，全等才敢增量 | **不做单键查表**（2026-08-31 改）：在所有候选里找「是当前 items 的**严格前缀**且最长」那条，等长再按 `ts` 取最近；缓存槽键 = `convId` |
| `conversation_id`/`parentId`（`_cvSeen`/`_pmSeen`） | 续会话线索，`finish()` 时 `saveConvSession` 存入缓存 | **重试/fallback 轮开的新交换必须回传 id 重存**（r15），否则下轮 fork 回旧分支，模型看不见自己上轮说的话 |
| 增量 `_convDelta` | `input.slice(count)` 后 `_stripAssistantItems`（剔 assistant/reasoning/function_call，留工具结果+新用户消息） | 增量轮无新 `<user_query>` 时要换 continue 框架语（r11） |

**异常全部收敛到"退化成第一次输入"，构造上不会发错上下文**：
- 换账号/会话失效 → 上游报错 → `catch(_convErr)` 删缓存+**同请求内**全量重发新会话；
- pod 重启 → 内存缓存清空 → 天然 miss；TTL(240min)过期/前缀指纹不符 → miss 回落全量；
- 重试通道续会话失败 → `collectWebTextR` 自 catch 返空 → 交付首轮文本。
诚实边界：缓存不记"conv 属于哪个账号"，跨账号正确性靠上游报错触发兜底（bpi 单
captured 会话，换号必伴随 pod 重启/session 重抓，两条路都自然清缓存）。

日志判读：`[conv] saved items=N conv=xxx`=全量后建会话存指纹；
`[conv] delta send K new items, M chars`=增量真省了（客户端全量几万字符只透传 M）；
`[conv] delta send failed -> full resend`=失效兜底触发（该分支尚无 live 样本，代码层保证）；
`[conv] miss items=… cands=… stale=… notprefix=… mismatch=…`=没命中及其原因
（`items<=2` 的 miss 是**真·新会话**，正常；`items>2` 的 miss 才是断链）。

> ⚠️ **`delta send … M chars` 是 `execenv-strip` 之前的数**。判门①要看后一行
> `[execenv-strip] M -> M' chars` 里的 **M'** 才是实发上游的量。实测同一轮
> `delta send 660 chars` → `execenv-strip 660 -> 145 chars`，拿 660 判门①会误判成膨胀。
> 另有 `[proto2] v2 contract DIET-ZERO (delta turn, 0c vs 3019c full)`=增量轮完全不发契约。

### key 的构成：踩过两次，别再回去（2026-08-31）

1. **不许把 `instructions` 放进 key**。它来自 `req.body`、由 Cursor 客户端产生、
   同一个 chat 内隔几轮就变。key 一变 `get()` 直接 `undefined`，**连逐项前缀校验都走不到**，
   判成首轮 → 全量重发 → 网页新开会话。实测 19h/63 条会话里 12 条由此产生。
   附带矛盾：复用路径是 `flattenInput(..., null)`，`instructions` 压根不发上游——
   它被当"稳定锚点"写进 key，却既不稳定、也不参与复用路径的内容。
2. **也不许 key 只留 `input[0]`**。Cursor 每条会话第 0 条框架消息都一样，
   所有 chat 会撞成同一个槽互相覆盖，比现状更糟（`zk-delta/common/framing.js` 注释里
   写过这条）。
3. **等长前缀平局必须按 `ts` 取最近**。两条会话有等长严格前缀不是理论问题——
   同一句开场白问两次就够了；平局取先遇到的那条会把新一轮接到旧会话上（实测抓到）。

改 key 的构成前先跑 `scripts/zk-cursor-web/conv_prefix_offline_cases.js`（13 组 41 断言，
从产物里逐字抠真函数体 eval 驱动）；现场验收用
`scripts/zk-cursor-web/conv_prefix_live_probe.py`（多轮 + 每轮换 instructions），
但**合成绿不算数**，真验收在真 Cursor 一条 chat 连问 ≥8 轮看是不是全程一个 convId。
详见 `docs/conv-reuse-prefix-match-20260831.md`。

### 官方口径核对（2026-08-22，platform.openai.com/docs/guides/conversation-state 原文）

- **省的是传输不是计费 token**："Even when using previous_response_id, all previous
  input tokens for responses in the chain are **billed as input tokens in the API**."
  ——服务端有状态 ≠ 模型少读上下文；metered 面只有 prompt caching 折扣（cached 档）。
  bpi 走**网页订阅额度面**（按消息/速率，不按 token），少传的几万字符才是净赚。
- **安全地板有官方背书**："If an uncached ID cannot be resolved, send a new turn with
  previous_response_id set to **null and pass full input context**."——失效→null+全量
  就是 OpenAI 自己规定的 fallback，不是我们的私设。
- 网页面 `conversation_id`+`parent_message_id` 与计价面 Conversations API/
  `previous_response_id` 是**同构不同 API**（都是服务端有状态树），引用官方文档只能
  证架构，不能直接证 bpi 行为——bpi 的正证据是 pod live 日志（delta send 53 chars）。
- 裸脚本打 `chatgpt.com/backend-api/conversation*`/`sentinel` = CF 边缘 403 挑战页
  （188 实测），这就是必须捕获真实浏览器 session 的原因；198 宿主机 DNS 对
  chatgpt.com 投毒（face:b00c 段），文档验证走 platform.openai.com 不受影响。

### 压缩次数与体感（分两个"压缩"回答，别混）

- **网关 ①diet/②histdiet：结构性省**——只在全量轮跑，命中增量时无工作对象。
  30 轮长会话 ≈ 1-2 次全量压缩 + 28 次增量（旧模式=30 次全量）。
- **Cursor 自身 compaction：不省，故意不省**——usage 上报客户端真实上下文，
  客户端 compaction 是防会话无限长的唯一闸（低报=永不触发=长会话失控，codex 侧实证）。
- **体感收益在最坏情况**：附件上传悬崖（>100K，十几秒+实测零输出）结构性消除；
  常规轮 TTFB 大头是上游 thinking（3.5-5.5s），少传几万字符只省几百 ms，体感有限。

## 克隆到新账号（多账号扩池，2026-08-24 acct-82 实战跑通）

单条 bpi/101 线 acquireSlot 串行是瓶颈。加一条独立线 = 复制一份 deploy/svc + 一份 web
seed + 三档模型 + 授权 key。一键脚本 `scripts/zk-cursor-web/clone_web_fc_lane.py`
（dry-run 默认；`--apply` 执行）。四步，每步都有各自的假绿陷阱：

1. **web seed 的登录态：直接借 WS 线的活 token**。zero-N web seed
   （standby `/Data/zerokey-sessions/zero-N/users.json`）里 `parsedFetch.headers.authorization`
   那个 bearer 是手抓的、会失效——**token 里的 exp ≠ 上游还认它**（zero-82 的 JWT exp 在
   未来，握手仍 `Sentinel 401 token_invalidated`；唯一判据是真去 sentinel 握手，见高频坑）。
   若该号同时是 `chatgpt-acct-N` WS 线成员，它 PVC `/chatgpt-auth/auth.json` 里有一份
   **带 refresh_token+offline_access、会自动续**的 codex OAuth `access_token`（同一账号）——
   把这份灌进 web seed 的 authorization 头即可，**web 端点 `/backend-api/f/conversation`
   接受这个 codex OAuth token**（live 实证）。这就是"两套登录态、WS 的能驱动 web 线"。
   脚本 `--live-from-ws` 走这条；改前自动备份到 `/Data/backups/zero-N-users-pre-livetoken-*`。
2. **deploy/svc `zero-cursor-bpi-N`**：照 bpi 模板逐字节，只换 name/labels/selector、
   `ZK_USER=acctN`、seed hostPath `/Data/zerokey-sessions/zero-N`。同一 patch CM
   `zk-cursor-bpi-patch`、同镜像、同 8201、nodeName aiyjy-litellm-standby、dnsPolicy None、
   strategy Recreate。（账号可与 WS 增量线共用——各走各的额度，用户已确认。）
3. **三档模型 `cursor-web-fc-N-terra{,-high,-max}`**：`openai/gpt-5.6-terra`，api_base 指
   新 svc，`-high`→reasoning_effort high、`-max`→xhigh。**必带 `api_key` 占位符**
   （见下"假绿①②"）；`model_info` 必带 `id`+`mode`（漏了 /model/update 报 400
   "model_info not provided"）。模型名前缀 `cursor-web-fc-` 不能改——hook
   `cursor_web_fc_sys_rewrite` gate 在 `model.startswith("cursor-web-fc-")`，改名 hook 不 fire。
4. **授权 key**：`/key/update` 把三个模型名 append 到目标 key（acct-82 给了
   `cursor-liuguoxian04-5rub`，41→44）。

### 假绿三连（本次真踩，验收纪律）

- **假绿①：openai/ 模型漏 api_key，真流量 401、master key 测不出**。openai/ provider 的
  真实调用路径要求 litellm_params 里有 api_key，否则抛
  `AuthenticationError: api_key client option must be set`。占位符
  `sk-zerokey-web-noop` 即可（bpi 不校验，登录态在 seed 里）。**用 master key 测会走另一条
  分支绕过这个 gate → HTTP 200 假绿**，真用户 "hi" 却 401。101 老线一直带 api_key，82 漏了
  就是 "hi 无回复" 的根因。
- **假绿②：`/model/info` 对 api_key 脱敏，两条线都显示 `has api_key: False`**。要对比配置
  差异**只能读 DB raw `litellm_params`**（101 有、82 无，肉眼可辨），信 /model/info 的
  decrypt 视图必被带偏。（同记忆 `feedback_hardcoded_log_string` 脱敏陷阱家族。）
- **假绿③：验收必须走真 key 路径，不能用 master key**。master key 绕过 per-key 鉴权与
  api_key gate。正确做法：`/key/generate` 建个临时 scoped key → 打 `/v1/responses` 带暗号
  → 看 200+暗号回显 + grep `zero-cursor-bpi-N` pod 日志 conversation/200 → `/key/delete`。
  （DB `LiteLLM_VerificationToken.token` 是 hash，不能当 bearer；/key/info 只能拿它查不能用它发。）

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
- web seed 里 bearer 的 **JWT `exp` 在未来 ≠ 上游还认它**：zero-82 exp 未过期仍
  `Sentinel 401 token_invalidated`（外部 re-capture 批把 seed 判死）。唯一判据是真去
  sentinel 握手，别拿 exp 当活证。修法见"克隆到新账号 step1"——借 WS 线的活 OAuth token。

## 从 codex 真源码搬用 —— 落地作业单（2026-08-24 精读 ~/codes/codex commit 343074d）

> **审计判决（2026-08-24，198 实拉 hook + responses.js 逐条三段式**静态审计**，非运行时计数）：
> 作业 1–4 判决全部"已实现 / 架构不适用"，bpi 线无需因 codex 学习改代码。** 其中作业2/3 是架构层
> 定论可直接信；**作业1 的"无累积"已经 live 闭环坐实**（08-24 组池回归：`[conv] delta send 1
> items, 96 chars`，注入块零累积）；作业4 的"已实现"仍是数据流推断（usage 未逐轮核对）。详细
> file:line 证据表见 `docs/cursor-ide-chatgpt-web-status-and-plan-20260823.md` §4.0。速查：
> - **作业1（证伪+已实现）**：hook 有 `_already_done` 哨兵幂等；`responses.js:371-372` delta 轮
>   `flattenInput(...,null)` 不发 instructions；`:407` `_chatOnlyize` 已识别+剥 `[EXECUTION ENVIRONMENT]`。
> - **作业2（架构 N/A）**：`responses.js:95-101` 把 call/output 转文本发网页，上游非结构化，无 400。
> - **作业3（已满足）**：无原生 FC 无 delta 可拼；`:1400-1415` 出站 delta==done 纯 UI。
> - **作业4（核心已实现）**：`:936-957` r9 已报客户端真实上下文；web 面无 per-token 权威数可锚。
>
> 下面保留原作业单描述作**推导依据留档**（怎么从 codex 机制推出这些假设）；要动手前先看上面的判决，
> 别重复实现。真正未做的杠杆是**组池**（doc §4.1）与**跨线压缩 skill**（doc §4.2，另一 codebase）。

对照 OpenAI codex 真源码（源码锚点地图见记忆
[[reference_codex_source_harness_anchors_2026_08_24]]）。**先确认：我们的代理形状、增量前缀
校验+全量兜底、工具结果在 output 无 role，都与官方同构，方向对**。下面是可搬的改进，每条给
输入/动作/预期/验收。诚实边界：codex 消费的是 `/backend-api/codex/responses`（codex 份额面），
我们 web 线消费 `/backend-api/f/conversation`（网页订阅面）的 SSE 再转译成 responses 事件发给
Cursor——**能借的是上下文管理思路，不是端点或事件形状**，逐条标注是否直接适用。

### 作业 1：hook 注入打标记再回收（直接适用，最高优先）
- **输入**：hook `cursor_web_fc_sys_rewrite` 当前把 `[EXECUTION ENVIRONMENT]` 无标记注入
  input；conv 复用/增量路径会把它重复累积。
- **动作**：给注入块包一对哨兵（如 `<!--ZKENV_START-->…<!--ZKENV_END-->`）；注入前先扫
  input 里有没有上一轮留下的同名块，有则**先整块删掉再注入一次**（照 codex
  `ContextualUserFragment` 的 markers + `matches_text` 回收，`context-fragments/src/fragment.rs:30`）。
- **预期输出**：无论第几轮，发往上游/缓存 key 的 payload 里 ZKENV 块**恰好 1 个**；增量轮不因
  注入而逐轮增长。
- **验收**：`loop_ls.py`/`loop_dl.py` 跑 ≥5 轮闭环 → 抓每轮实发 payload，`grep -c ZKENV_START`
  恒 =1；`[conv] delta send … chars` 不含注入块体积的逐轮累加。回归 `cmp_delta_done.py` 散文
  场景仍逐字符相等。

### 作业 2：发送前配对体检 normalize（直接适用）
- **输入**：responses.js 构造上游请求前的 input items 数组（可能含被打断的孤儿 tool call）。
- **动作**：实现 `normalizePairing(items)`——每个 function_call 必须有匹配 call_id 的
  function_call_output，否则丢弃该 call（或补一条空 output）；每个 output 必须有对应 call，
  否则丢弃；裁最旧项时连带删配对项（照 codex `history.rs:446 normalize_history` +
  `remove_first_item:279`）。
- **预期输出**：发出的 payload 无孤儿 call/output。
- **验收**：构造"工具调用后中断"场景（call 无 output）→ 重放，断言上游不再 400、pod 日志显示
  规整后计数；回归 `loop_ls.py` 已有闭环仍 PASS（不能把正常的 call/output 对误删）。

### 作业 3：工具参数只认 done item，delta 仅 UI（需先审计，可能已满足）
- **输入**：responses.js 对上游流的消费逻辑。
- **动作**：审计是否在自行累加 `function_call_arguments.delta` 拼最终参数；codex 明确把 arg
  delta 当 trace-only（`sse/responses.rs:501`），只认 `response.output_item.done` 的完整 item。
  若我们在拼 delta，改成以 done item 的参数为准，delta 只用于界面增量。**注意**：web 面 SSE
  形状与 codex /responses 不同，本条先审计确认是否适用，别盲改。
- **预期输出**：最终工具参数来源于 done item。
- **验收**：`sse_dump.py <tool场景>` → 断言交付给 Cursor 的最终 args == done item 的 args；
  `loop_ls.py` 工具闭环 PASS。

### 作业 4：usage 上报对齐"服务端末轮 + 本地估后续"（直接适用）
- **输入**：responses.js 回给 Cursor 的 usage 字段来源。
- **动作**：口径改为"服务端权威的上一轮 total + 本地估算该轮之后追加项（工具输出等）"，而非纯
  客户端计数（照 codex `history.rs:421 get_total_token_usage`）。**保持对客户端诚实上报**——
  低报会让 Cursor 原生 compaction 永不触发（见高频坑与
  [[feedback_gateway_usage_must_report_client_context_not_sent_prompt]]）。
- **预期输出**：usage.outputTokens/inputTokens 反映客户端真实上下文。
- **验收**：长会话 ≥20 轮，Cursor 侧最终能触发自身 compaction；`timing.py` 显示 usage 单调
  合理，不出现"实发压缩后小数字"。

### 跨线（压缩线，不落 bpi）：token 触发 + 留一条总结当账本
- **不适用 bpi**（bpi 靠服务端会话态 conv-reuse + ZK_DIET/HISTDIET 结构瘦身，无模型总结压缩）。
  **目标文件**是压缩线 skill：`~/.claude/skills/codex-compaction-v2-nonnative/SKILL.md`、
  `codex-deepseek-tool-triage` 相关压缩。
- **动作**：压缩触发改**按 token 阈值**（如上下文窗口 90%，codex `openai_models.rs:486`），不按
  轮数；保留法照 codex `compact.rs:639`——只留最近 user 消息（≤20K token，newest→oldest）+
  **一条总结**（模型自写的该轮末条 assistant），推理/工具历史丢掉，**总结即账本**（解开
  [[feedback_compaction_without_ledger_causes_amnesia_loop]]）。
- **验收**：压缩前后闭环 harness 不失忆（能引用被压掉轮次的关键结论）；token 触发点可复现。

### 作业 5：上游流空闲超时兜底（2026-08-24 精读补审，此前从未审过）
- **codex 怎么做**：SSE 面**完全不靠心跳**，`timeout(idle_timeout, stream.next())` 每读一 chunk 套
  空闲超时（默认 300s，`sse/responses.rs:554`），超时→可重试 `Stream` 错→**整轮从历史重建**；
  耗尽 max_retries 后 WS→HTTPS session 级兜底。锚点全表见记忆
  [[reference_codex_transport_resilience_anchors_2026_08_24]]。
- **bpi 现状**：responses.js `_fetch`=裸 `fetch()` 无 AbortController；`ZK_HB`(5s) 是**下游**保活
  （保 Cursor 连接不死，检测不到上游已死）；`[stall]` 日志只在下一 chunk 到时**回溯打印**、不 abort
  不重试；唯一兜底 = undici 默认 `bodyTimeout≈300s`（与 codex 300s 同量级）。
- **判决（三段式）**：假设"上游卡死挂死 turn"——**数据不支持**（主线 `[stall]` 0 次、bpi-82 3 次
  3–6s 短空档全自恢复、0 终端挂死）。代码路径存在≠在咬人（CLAUDE.md 红线）→**不盲打生产**。
- **唯一隔离安全候选**：`ZK_IDLE_ABORT`（**默认关**）——仅在**首字节前 `started===false`** 窗口计时，
  超阈值（>最坏 thinking 20s+，取 120s，<undici 300s）→ abort 上游 + **单次干净重试**。零重复风险
  （尚未吐字节，重试铁律见记忆）。改动触及 api.js(`_fetch` 挂 AbortController)+responses.js(计时器)，
  **两文件都在 CM `zk-cursor-bpi-patch`，隔离仍只碰 bpi 两 pod**。**要上须走六步 + 临时真 key 回归，
  且需用户点头**（预防性硬化、当前无触发事件）。
- **第二轮深读补判（08-24，中断/缓存/生命周期三区）**：bpi 对**客户端断连零处理**（live grep 全 0）
  ——但**构造安全**：上游消费到完整结束+conv 照常存（与服务端一致），下轮指纹 miss 退全量，与
  codex"WS 基线只在 Completed 提交→打断必回全量"同构。代价有界（slot 占用+白烧一条已计入消息），
  **判决不改**；蹲证据可加 `req.on('close')` 观测计数。缓存前缀纪律/截断 middle-out/配对靠 call_id
  均已同构或 N/A。两轮合计 **0 必改**，锚点全表见 doc §4.3/§4.4 + 记忆
  [[reference_codex_transport_resilience_anchors_2026_08_24]]。

## 组池（2026-08-24 已落地：cursor-web-fc-pool-terra{,-high,-max} 双线加权池）

3 个共享别名各挂 bpi+bpi-82 两 deployment（`model_info.id=zerokey-cursor-web-fc-pool-{101,82}-terra*`，
weight:1，api_key 占位符必带）。**零代码改动**：weighted_affinity 钩子对任何多 deployment group
自动生效——Cursor 不发 session 头 → **key 级亲和**（同 key 钉同 pod，conv-reuse 缓存局部性不受损）。
**fail-over 已演练（08-24 scale0 实测）**：①终止宽限期 30s 内 Terminating pod 带病服务（K8s
ProxyTerminatingEndpoints 兜底），请求照常 200；②pod 死透后钉死 key = **前门 0 字节黑洞 ≈120s**
（时钟=litellm `stream_timeout:120`，无 HTTP 头无心跳，客户端只能靠自己超时）；③120s 抛
InternalServerError → WA fail-mark 180s + re-picking → **下一发自动甩健康线**（MISS→重选→200）。
最坏代价=钉线 key 一发挂 ~2min，自愈无需人工。换线安全性=构造保证（新 pod 无缓存→指纹 miss→退化
全量新会话）。已授权 key03/key04。三个纠偏：
- **"acquireSlot 串行"说法不准**：镜像内 rate-limiter.js 是滑动窗限速器（每 pod 每 label 15s 放 5 个），
  非互斥锁；单 pod 突发 >5/15s 才排队。
- `/key/list` 的 `size` 上限 100（200→422）；**422 被 try/except 包住会伪装成"没找到 key"假阴性**。
- `/key/update` 的 models 是**整表覆盖**：必须先读现有列表合并再写回。
验收范式（同假绿③）：临时 scoped key 打暗号 ≥4 次 → 200+回显 + proxy 日志 WA MISS→HIT 链 +
两 pod grep 暗号归属（本次 4:0）→ 删 key。回滚=`/model/delete` 6 个 pool id，旧 6 名全程未动。

### 新账号入池清单（以后加号照抄，两步）

1. **克隆新线**：`clone_web_fc_lane.py --apply --live-from-ws`（四步+假绿三连见"克隆到新账号"节）。
2. **挂进池**：3 个 `/model/new`——`model_name` 用 pool 别名原名（同名即入池），`api_base` 指新线
   svc，`model_info.id=zerokey-cursor-web-fc-pool-N-terra{,-high,-max}`（唯一），`weight:1`、
   `api_key` 占位符、`mode:chat`、`-high/-max` 加 `reasoning_effort: high/xhigh`。模板=仓库
   `scripts/zk-cursor-web/pool_register.py`（LANES 字典加一行即可）。**不用动 WA/hook/key**——钩子自动纳入加权，
   已授权 pool 别名的 key 无需再授权；已有用户黏在原线（TTL 1h 过期后重新加权摊匀）。
3. 验收走上面的范式，重点 grep **新** pod 日志确认真有流量落它（防"注册了但没人路由到"假绿）。
   权重想不均衡（好号多吃）就在该 deployment 的 `litellm_params.weight` 调大，WA 实时生效。
4. **收尾跑 `pool_consistency.py`**：新 lane 挂了同一个 CM，克隆出来时是新 pod、代码天然是新的，
   但这一步能立刻暴露两件事——①别名少挂了某一档（各档腿数不齐 → 那一档没有 fallback）；
   ②`/model/new` 写了 lane 但 deployment 名没对上（dangling → 打到它必超时/502）。

### fallback 的真实形态（2026-08-31 实测重写，旧版"≈2min 打脸"已作废）

路由是 **key 级黏性**（Cursor 不发 session 头，WA 按 key 钉 lane，为的是保住会话缓存局部性）。
所以平时流量可能长期只压一条 lane，另一条零流量——**那是设计，不是故障**，但也意味着
静默故障（比如跑着旧代码）只在切换那一刻才暴露，这正是 `pool_consistency.py` 存在的理由。

**一条 lane 挂了，这一发会当场换台，用户基本无感**——已实测，不是推断：

| 坏法 | 线型 | 首字节 vs 控制组 |
|---|---|---|
| dns 解析不到 | 非流式 / 流式 | +0～1s |
| hang（连得上永不回）| 流式 | +10s（连接超时踩下去就换，不撞 300s 天花板）|

日志形状：`weighted-pick deployment=<坏>` → 失败 → `Selected deployment: <好>` → 正常出字。
全局配置：`num_retries=5` / `enable_weighted_failover=true` / `cooldown_time=60` /
`allowed_fails=3` / `router_settings.timeout=300`；**544 个 deployment 没有一个设
`stream_timeout`**（旧文档里的 `stream_timeout:120` 在当前 DB 不成立）。
08-24 那个"≈2min"讲的是**下一发**才甩到健康线，不是这一发的代价，别再引用。

**还没实测**：lane 返回 200 但空流 / 吐了字节后中途死。读源码
`/app/streaming_output_backfill.py`、`/app/midstream_fallback_loop.py` **无模型名闸门**
（文件里的 `acct` 全在注释里）→ cursor-g 吃的是和 acct 同一套。这是读代码不是实测，别当结论用。

#### 演练脚本

```bash
python3 scripts/zk-cursor-web/failover_drill.py [B段发几次=3] [间隔秒=200] [--stream] [--hang]
```

不碰 82/101、不改 router_settings；`/model/new` 临时注册自用组 + 20m 临时 key，跑完全删。
**读结果只认 `SAVED` / `DROPPED`，`NOT_EXERCISED` 是没考到、不算数。**
`成功率高` 本身不是证据——第一版就是这么报出 "4/4 ✅" 假绿的（A 段先跑打了 180s fail-mark，
坏 lane 一次都没被挑到）。改脚本前先读文件头那四条约束（B 在 A 前 / 每发换 key /
间隔过 fail-mark / 逐发日志取证）。

## 已撤回的结论（写进过这份 SOP / 记忆 / 甚至告诉过用户，后来被证伪）

留着不是为了自责，是因为**这些结论当时都"看起来自洽"，不标出来就会被下一个人捡回去**。

| 结论 | 当时的依据 | 怎么塌的 | 现在的状态 |
|---|---|---|---|
| 「81~85 的动手率只有 8~26%，大部分同事吃的是坏的」 | `lane_task_ab.py` 两臂 A/B | 探针打 `/v1/responses`，而这些名字全是 `mode:chat`（真 Cursor 走 chat + 桥）；两臂 slug/effort 也没配平 | **假**。用户日常真 Cursor 里 `ls` 一直正常。脚本顶部已刻警告框 |
| proto2「拒绝动手」定向文案补丁（act-kick v3） | 上面那个假数 | 修的是假病，灰度失败 | 当天回退，CM 回到 `df9506` |
| 「xhigh 是第三个虚构档」 | 空 completion，跨 4 次一致 | 测的 `gpt-5-6` 本来就没有档；空回显与 pro 同形态，而 pro 是**我没收割到** | 09-01 撤回（详见第 75 行那条） |
| 「82 只宣告不动手是硬通道关着」 | 计划文档里的推断 | 09-01 定因为**系统提示词冲突**，hook 两刀修好（首轮 announce 5/5→0/1） | 计划文档 `.claude/plans/mellow-fluttering-giraffe.md` 已整篇标作废 |
| 「`-wm` 只有 handoff、零正文，救不回」 | 07-27 直测留档 | 缺的是 `stream_handoff` 轮询那段代码，恢复后就能收割到正文 | 09-02 闭 |

**共同形状**：每一条都能解释用户当时正在抱怨的症状，所以我审查得最少。
**能解释症状的第一个机制不是结论**——说出口前先花一次查询打它的证伪腿。

## 未做的下一层杠杆

acct101 账号级契约（行动协议写进 ChatGPT 账号自定义指令，优先级压过 Cursor persona）；
~~多账号扩池破 acquireSlot 串行~~（已做首例 acct-82，见"克隆到新账号"）；~~多条线组池~~（已落地
+fail-over 已演练，见"组池"节）；第三条线克隆（clone_web_fc_lane.py + 入池只需 /model/new 挂进
pool 别名）；codex 原生 FC（快+100%，烧 codex 份额，见
`.claude/plans/indexed-riding-mountain.md` 的 cursor-fc-* plan）。
