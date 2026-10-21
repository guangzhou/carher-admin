---
name: litellm-198-key-allowlist
description: >-
  批量改 198 LiteLLM 的 cursor-* / claude-* / carher-* key 的 `models` 白名单
  和 per-key `aliases`（新模型上线要"让大家都能用"、下线要"从所有人那儿摘掉"、
  把一堆内部名收敛成一个对外名再映射到真实组）。含 read-merge-write 纪律、
  `models=[]` 是全放行不能收窄、删+加+映射合成一次写、金丝雀+双对照验收、
  跑到收敛、换尺子回读、快照回滚、并发写的开跑前防撞与事后取证、
  **撤权限必须 `--include-blocked`**（blocked 只挡认证不清白名单，解封会静默复活）、
  白名单里有名字 ≠ 能用（缺 alias 的裸名过了闸门照样 400）、
  `--converge-to <参考key>` 收敛（别手算差集）、收敛的两个用户面
  （`UPSTREAM CHANGE` 静默换供应商 / `NAMES DROPPED` 名字没了直接 403，后者只报不拦）、
  `readback_ok=N/N` 回答不了"全部改完了吗"（scope 先被收窄，末尾的
  `converged_identical/spared/blocked/unrestricted/mismatch` 才是）、
  判改动是否还在只认 `token` 不认模型数
  （key 会被同名重发）、`updated_at` 与 `AuditLog` 这两把尺子在这件事上是坏的。
  Use when 用户说"把这个模型给所有 cursor/claude key 都加上"/"让同事们都能用 X"/
  "把 X 从白名单里摘了"/"把 X 的权限都删除了"/"只保留一个名字 Y 映射到 Z"/"谁能调这个模型"/
  "把这几把 key 改成和 XXX 一样的配置和映射"/"全部 key 改成和 XXX 一致，多出来的删掉"/
  "我改的怎么又变回去了"。
  ⚠️ 白名单 ≠ 引流：要让流量自动打过去是 fallback，不是这个 skill。
---

# 198 Key 白名单 + per-key alias 批量编辑

## 先分清三件事（点错了就白干）

| 想要的效果 | 该动什么 | 本 skill |
|---|---|---|
| 让某些人**能调**某模型 | key 的 `models` 白名单 | ✅ |
| 让某人的请求**自动改道**到别的组 | per-key `aliases` | ✅（单人切供应商仍看 `litellm-key-provider-swap`）|
| 主力挂了**自动兜底**到备选 | 全局 `router_settings.fallbacks` | ❌ 见 `litellm-ops` |

**白名单只发通行证，不发流量。** 加进白名单的模型，只有客户端主动选它才会被调用。
所以往白名单里放一个额度稀缺的模型是安全的；把它放进 fallback 链**不安全**——
主力挂一次就会自动打过去，几分钟内把额度抽干，而且是静默的。

## 白名单闸门查的是**改写前**的名字（2026-09-08 实测）

临时 key `models=["kimi-k3"]` + `aliases={"kimi-k3":"sa-kimi-k3-responses"}`，
目标名**不在**白名单里，照样 200。所以「对外只暴露一个公开名、背后接私有组」的做法是：

- `models` 里**只放公开名**（`/v1/models` 直接返回白名单原文，用户在 Cursor 里就只看到它）
- `--alias 公开名=真实组` 做改写

目标名不必进白名单，放进去反而会把内部名暴露给用户。

## 脚本

`scripts/litellm-198-key-allowlist.py`（单测 `backend/tests/test_litellm_198_key_allowlist.py`，13 个）。
在 198 本机跑（`127.0.0.1:30402` 是 litellm-product 的 NodePort）：

```bash
# 投递 —— ⚠️ 文件名带上会话号，198 上多会话并行，同名路径会互相覆盖
scp scripts/litellm-198-key-allowlist.py cltx@10.68.13.198:~/litellm-198-key-allowlist-<sid>.py
# 用之前 sha256sum 对一遍，确认跑的是自己那份

# 198 上
export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

S=~/litellm-198-key-allowlist-<sid>.py
python3 $S --add-model <M>                                     # ① 默认 dry-run
python3 $S --add-model <M> --limit 1 --apply --backup ~/<M>-canary-$(date +%Y%m%dT%H%M%S).json
python3 $S --add-model <M>          --apply --backup ~/<M>-full-$(date +%Y%m%dT%H%M%S).json
python3 $S --restore ~/<M>-full-<ts>.json --apply               # ④ 回滚(models+aliases 一起)
```

开关：`--add-model` / `--rm-model` / `--alias src=dst` / `--rm-alias src`（都可重复）、
`--prefix carher-`（换范围，默认 `cursor-` + `claude-`）、`--only <alias>`、`--limit N`、
`--include-blocked`（**撤权限时必须带**，见下面「blocked key」一节）。
`--model` 是 `--add-model` 的旧拼法，配 `--remove` 等于 `--rm-model`。

`--only-file PATH`（2026-09-29 加）：一行一个 alias，空行和 `#` 行跳过，与 `--only` 合并。
**名单超过十来个就用它**，别把几百个 `--only` 拼进 `ssh '…'`（远端 shell 重切分会静默丢参）。
🔴 **`--only` / `--only-file` 里有任何一个名字没匹配上就整轮拒跑（return 2）**，打
`only_requested=N only_matched=M` + 逐个 unmatched。没匹配上的三种来源：拼错、blocked 却没带
`--include-blocked`、不在 `--prefix` 范围里。09-29 前这种情况是**静默少写**，老调用方现在会被拒，
这是故意的——少写的那几把用户那边零报错。

🔴 **前缀 ≠ 意图集合**：198 `--prefix carher-` 扫出 237 把，其中 `carher-hangzhou`（杭州 gw 上游凭据）
和 09-28 的测试 key（`*probe*`/`*iso*`/`*slow8*`/`*landing8*`）不该动。给「全部 her」写就先导出精确名单再 `--only-file`
（导法见 skill `her-public-model-name`「198 的三处与阿里云不同」）。
🔴 **反过来，给一把实例 key 换后端（alias）时，要问「同一个能力还有谁在用」**：10-14 把 perplexity → grok 只加在
`carher-13/14/75`，漏了 `carher-hangzhou`（杭州全部 bot 的上游，perplexity 名真实流量最大的那把），杭州 7 天 401 无人发现。
判据：`SpendLogs` 按 `proxy_server_request->>'model'` 看这个名字近 7 天都有哪些 `api_key` 在打，每一把都要有映射或明确不管。
只改一把时 `--only <alias>` 也要配一个能覆盖它的 `--prefix`（`--prefix carher-hangzhou --only carher-hangzhou`），不然报 `not in scope` 拒跑。

验收另有一支尺子，**不要手搓 SQL**（手搓极易漏掉 blocked，见下）：

```bash
scp scripts/litellm-198-key-allowlist-verify.sh cltx@10.68.13.198:~/kav-<sid>.sh
ssh cltx@10.68.13.198 'bash ~/kav-<sid>.sh claude-deepseek-v4-pro deepseek-v4-pro'
```

**改名/收敛要在一条命令里同时删旧、加新、写 alias**，它们会合成**一次** `/key/update`：

```bash
python3 $S --prefix cursor- \
  --rm-model sa-kimi-k3 --rm-model sa-kimi-k3-responses \
  --add-model kimi-k3 --alias kimi-k3=sa-kimi-k3-responses --apply --backup ...
```

分两次写会留一个窗口：旧名已删、新名还没到，那段时间用户无模型可用。

## 「把这几把 key 改成和 <某把> 一样」用 `--converge-to`，别手算差集

2026-09-21 把 5 把新 key + 1 把模板 key 收敛到 `cursor-liuguoxian-std` 的 32 模型 3 alias，
我是手算了 **36 个 `--rm-model` + 19 个 `--rm-alias`** 干的。手算的清单每次写之前都得自证完整
（`comm -23 rm36 source` 必须为空、剩下的 `diff` 参考 key 必须 `IDENTICAL`），
而且那串数组一旦在本地拼好再插进 ssh 命令串就会被远端重新分词 —— argparse 只认第一个
`--rm-model`，其余变成裸位置参数。**已封成开关，别再手算：**

```bash
python3 $S --only cursor-peilu-18ta --only cursor-fenglili-klmo \
  --converge-to cursor-liuguoxian-std          # ① dry-run
python3 $S --only ... --converge-to cursor-liuguoxian-std \
  --apply --backup ~/recurate-$(date -u +%Y%m%dT%H%M%S).json
```

它做四件手算做不到的事：

1. **期望态从参考 key 读**，不存在"清单漏一个名字"这种失败模式；顺序也照抄，
   所以收敛后各行**逐字节相同**，验收判据可以是 `count(distinct sig)=1`。
2. 参考 key **落在写入范围里就拒跑** —— 否则它自己 no-op 会被算进 `applied_ok` 充数。
3. 参考 key 是 `models=[]`（全放行）**拒跑** —— 收敛到它等于给所有人开全部权限。
4. **`--converge-to` 与 `--add-model/--rm-model/--alias/--rm-alias` 互斥**，
   避免"收敛完又被差集补丁改一刀"。

readback 在 `--converge-to` 下改判**相等**而不是包含 —— add/rm 集合是空的，
原来那套 `set(add) <= models` 断言会**恒真通过**（假绿）。

⚠️ **`cursor-liuguoxian-std` 10-12 起是刘国现的实验 key，不再是标准形状**（别的会话会随时往上加实验名，
10-14 下午就加了 argon/muse/gemini-web 系列，到 10-15 是 44 名）。
上面例子里拿它当 `--converge-to` 参考的写法**别照抄**：现在收敛过去会把同事的
`gpt-5.2` / `gpt-5.4` / `composer-2.5` / `deepseek-v4-flash` / `grok-4.20-0309-reasoning` /
`grok-4.5-latest` / `grok-4.6-latest` 一起删掉（Cursor 装机菜单里还有这 7 个名）。
**10-14 起全员标准形状 = 「std 减实验名」那份文件**（见下一节「全员收敛」），不是 std 本身。

### 期望态没有任何一把 key 带着时：`--converge-to-file`（2026-10-12 加）

手工对齐出来的新形状（10-12：两把 key 合并去重后逐行和用户核对的 36 名 / 27 alias）
没有现成参考 key。写成 JSON 文件，用文件当参考：

```bash
# target.json = {"models": [...按你要的顺序...], "aliases": {"公开名": "真实组", ...}}
python3 $S --prefix cursor- --only <key> --converge-to-file ~/target-<sid>.json       # ① dry-run
python3 $S --prefix cursor- --only <key> --converge-to-file ~/target-<sid>.json \
  --allow-upstream-change --apply --backup ~/<key>-pre-$(date -u +%Y%m%dT%H%M%SZ).json
```

与 `--converge-to` 的差别：

- **必须带 `--only`/`--only-file`**，不许拿一份文件扫整个前缀。
- 文件本身先过三道闸门，不合格直接拒跑：`models` 为空（=全放行）、有重复名、
  alias 的源名不在 `models` 里（闸门查改写前的名字，这种 alias 永远不触发 ——
  10-12 实例就是 `chatgpt-gpt-5.5`）。
- dry-run 逐项打印 `- model` / `+ model` / `- alias` / `+ alias` / `~ alias 旧 -> 新`，
  可以直接贴给用户逐行确认。
- `NAMES DROPPED` 与 `UPSTREAM CHANGE` 两块照常打印、照常门禁。

⚠️ 写之前要先证明**每个名都能落到正式车道的活组**（per-key alias 优先，否则走全局
`model_group_alias`，再否则同名组）：`/model_group/info` 和 `/get/config/callbacks`
都在 pod 内用 python3 读。10-12 那次 36 个名全部解析成功，才去跑的 dry-run。

### 全员收敛（10-14 实跑 545 把）：先金丝雀，参考 key 的「新名」要先过三客户端

用户原话「除了 liuguoxian 系列，其他所有人的 cursor key 用 std 覆盖」。实际做法与必须问的四件事：

1. **范围导成精确名单**（`--only-file`），三类要用户拍板：自己的系列（排除）、
   `models=['deepseek-v4.1-flash']` 收窄过的 163 把（**跳过**，否则一下子放开 opus）、blocked（**一起写**）。
2. **dry-run 的 `NAMES DROPPED` 必须按 14 天流量量出「谁在用」**再给用户：10-14 是 `gpt-5.2/5.4`
   ≤1948 次/38 把、`deepseek-v4-flash` 520 次/8 把。还有一类富形状 key（19 把，56 名）会掉 43 个名 ——
   查实全是 blocked 且 14 天 0 次，才放行。
3. **金丝雀 3 把有流量的 key**，用克隆形状跑三客户端线型探针（Claude Code `/v1/messages`、Codex
   `/v1/responses`、Cursor chat，**要求真发出工具调用**，不是回一句话）。10-14 金丝雀抓到 std 当天新加的
   `gemini-4-argon`（网页版 2 号小池、提示词模拟工具）与 `muse-spark` 三客户端都不调工具 ⇒
   用户改为「std 减这 3 个」的 `--converge-to-file`，35 名/26 alias。⛔ 直接 `--converge-to std` 会把实验名推给全员。
4. **per-key `router_settings`** 不在 models/aliases 里，收敛不覆盖它；要「连兜底一起覆盖」得单独清
   （`/key/update router_settings={}`，逐把存快照）。

验收换尺子：psql 按 `models=<目标> and aliases=<目标>` 分组数（10-14：fleet 545/545、deepseek-only 0、liuguoxian 0），
再克隆一把收敛后的 key 跑三客户端探针 + 删掉的名逐个打 403。

⚠️ 探针提示词别写成命令式「你必须调用 X，不许文字回答」—— 5 个模型当成提示注入直接拒（10-12 实测）；
用「这台机器 hostname 是什么？读 /etc/hostname 看」+ 编码 agent 系统提示。kiro 的 `claude-opus-4-7`
对系统文件类问题恒回 `I can't discuss that.`（快汇那条不会），是既存现象不是收敛造成的。

### 🔴 模板 key ≠ 新 key：收敛会静默把人搬到另一个供应商

新发的 key 是 `68 models / 0 aliases`，收敛过去只是减白名单。
但**已经带 22 条标准 alias 的模板 key**收敛到 3 条参考 alias = **删掉 19 条**。
其中危险的子集是：**被删/改的 alias，其源名仍留在白名单里** ——
那就不是 403，而是**毫无症状地换了上游**。

脚本把这个子集单独算出来打成 `UPSTREAM CHANGE`，默认 **rc=2 拒跑**，
必须显式 `--allow-upstream-change` 才动。09-21 `cursor-wangzhongwei-w1qn` 上正是 3 条：

| 源名 | 原目标 | 收敛后落点 |
|---|---|---|
| `gpt-6-astra` | `chatgpt-gpt-6-astra` | 同名真实组（**没验过它能用**）|
| `deepseek-v4-flash` | `openrouter-deepseek-v4.1-flash` | 同名真实组 |
| `glm-5.3-flash` | `openrouter-glm-5.3-flash` | `zai-coding-glm-5.3-flash`（实测约 58% 成功，199 ok / 146 fail；**openrouter 那条没测过，他原来那条可能更稳**）|

⇒ 这三条要**拿给用户裁决**，不是配上 flag 就过。"和 std 一样"这句话在模板 key 上
不等于"更干净"，等于"换供应商"。⚠️ `models=[]` 的全放行 key 每个名字都仍可调，
所以它的**任何** alias 变动都算 exposed。

### 🔴 `UPSTREAM CHANGE` 只是一半：另一半是**名字直接没了**（2026-09-26 加）

`upstream_changes` 只管"名字还在、上游变了"这个**静默**面。另一个面它一个字都不报：
**源名连白名单一起被删 ⇒ 用户下一次调用直接 403。**

09-26 收敛 688 把 `claude-code-*` 到 std 时，门禁对着 GPT-5.6 那批 retarget 大喊大叫，
而真正不可替代的损失**全程无人提起**：`anthropic.claude-fable-5`
（14 天 1030 次 / 9 把 key）离开白名单，**参考 key 上根本没有等价物**——
kiro 整个没有 fable 族。我是另写了一段 SQL 反推才发现的。

现在脚本自己算 `NAMES DROPPED`，**只报不拦**（09-21 那把模板 key 一跑就是 49 个名字）：

```
NAMES DROPPED from the allowlist: 49 distinct -- every key below starts 403ing on them
  anthropic.claude-fable-5  (9 key(s))
  ...
  ^ measure 14d traffic on these BEFORE --apply
```

**为什么不拦**：一个名字消失了是否要紧，取决于"参考 key 上还有没有等价能力"，
这是只有人能下的判断，脚本猜一个反而会训练人去无脑加 flag。
**吵不等于无害** —— 403 对用户可见，对跑 sweep 的人完全不可见。

🔴 **它打印在 `UPSTREAM CHANGE` 门禁之前，这是刻意的。** 我第一版写在后面，
那道门禁 `return 2`，结果真跑的时候这份清单一次都没打出来 ——
而操作者最需要它的时刻，恰恰是被门禁拦住、正在决定要不要加
`--allow-upstream-change` 的那一刻。阳性对照抓到的就是这个。

⚠️ **去量这些名字的流量时，尺子本身有个洞**：`LiteLLM_SpendLogs` 记的是
**改写后**的组名，所以一把 key 有多个 alias 指向同一目标时，
**分辨不出用户实际敲的是哪个名**。反推出来的每名 req 数是**上界不是计数**
（09-26：`glm-5.3-flash` / `-low` / `-high` / `-medium` 各 752，
其实是同一批 752 次；`claude-opus-5` 4709 与 `anthropic.claude-opus-5` 4705 同理）。

> 🔴 **量 14 天之前先看明细从哪天起才有**（10-14 加）：`LiteLLM_SpendLogs` 是 10 天 retention，盘满时还会被整表
> TRUNCATE（10-14 发生过，清完只剩几小时）。`select min("startTime") from "LiteLLM_SpendLogs"` 晚于 14 天前，
> 明细量出的 0 就不能当"没人用"。这时改量**日汇总 `LiteLLM_DailyUserSpend`**（按 `api_key` + `model`，`date` 是 text）：
> ```sql
> select model, sum(api_requests), round(sum(spend)::numeric,2) from "LiteLLM_DailyUserSpend"
>  where api_key = any(array['<sha>', ...]) and date >= (current_date - 14)::text group by 1 order by 2 desc;
> ```
> 它记的 `model` 和 SpendLogs 一样是改写后的组名，上面"尺子有个洞"那条照样成立。

### 🔴 `readback_ok=668/668 mismatches=0` 回答不了"全部改完了吗"（2026-09-26 加）

`applied_ok` / `readback_ok` 只谈**它自己 planned 的那批**。而 scope 是**先被收窄**的 ——
**一把静默不在 scope 里的 key，读起来和一把不需要改的 key 一模一样。**

09-26 那一跑：`scoped_keys=669 planned=668 applied_ok=668 readback_ok=668/668 mismatches=0`，
完美，而且作为"全部"的答案是错的。漏了 **23 把**：19 把 `blocked=t`
（`--include-blocked` 默认关，压根没进 scope）+ 4 把 `models=[]`（结构上收敛不了）。
是我另写了一次全表独立回读才发现的。

那次回读现在折进脚本了，`--converge-to --apply` 末尾必打：

```
converged_identical=685 spared=3 blocked=0 unrestricted=4 mismatch=0
```

四个桶按优先级互斥，**且只装「还不一致」的**：

| 桶 | 含义 | 要不要动 |
|---|---|---|
| `spared` | 被 `--exclude`/`--exclude-prefix` 显式豁免 | 不动，这是你要的 |
| `blocked` | 因为没带 `--include-blocked` 被跳过 | 🔴 **它们留着旧白名单**，哪天解封就静默还原回 sweep 前的形状 |
| `unrestricted` | `models=[]`，设计上永不收窄 | 不动，它**永远**等不了参考 key，这不是失败 |
| `mismatch` | 在 scope 里、可写、仍然不一样 | **只有这个桶红，返回 4** |

⚠️ 第一版按 flag 分桶、**没先测相等**，于是刚用 `--include-blocked` 收敛完的
那 19 把既算进 `converged_identical` 又列在 `blocked` 里 ——
同一把 key 报两次、结论相反。**相等判断必须排在分桶之前**，
让四个桶和 `converged_identical` 正好把 scope 切完（有单测钉住这条）。

### 全舰队收敛的完整命令形状（09-26 实跑 688 把）

```bash
for pass in 1 2 3; do        # /key/list 分页会少数，必须跑到 planned=0
  python3 $S --prefix claude-code- \
    --exclude-prefix claude-code-liuguoxian02 \
    --exclude-prefix claude-code-liuguoxian03 \
    --exclude-prefix claude-code-liuguoxian04 \
    --exclude claude-code-liuguoxian-std \
    --converge-to claude-code-liuguoxian-std \
    --allow-upstream-change \
    --apply --backup ~/fleet-bak/fleet-p$pass-$TS.json
done
# blocked 那批要单独补一遍，加 --include-blocked（它们不在上面的 scope 里）
```

⛔ **`export LITELLM_MASTER_KEY=...` 必须和脚本在同一条 ssh 命令里** ——
少了它报的是 `HTTP 401: Authentication Error, API 异常 (req: ...)`，
形状和"key 真的没权限"一模一样，别拿它去查权限。

**"fallback 链也要一致"不需要 per-key 做任何事**，但要证明：per-key fallbacks
是**整表替换**全局表，带了就不可能一致。判据是一条 SQL：

```sql
select count(*) filter (where metadata ? 'fallbacks'), count(*)
  from "LiteLLM_VerificationToken" where key_alias like 'claude-code-%';
-- 0|692  ⇒ 全部吃全局表；alias 改写在 router 之前，11 个 alias 目标相同 ⇒ 链必然相同
```

## 判「我的改动还在不在」：对 `token`，不对模型数

09-21 我 Phase 1 写好的 `cursor-liuguoxian-std`（32/3）几小时后变回 `68/0`。
第一反应是"被并发会话压回去了"，**错**：这把 key 是**被删掉重建**的 ——
同一个 `key_alias`，**新的 `token`、新的 `created_at`**。

⇒ 判据是 **`--backup` 快照里的 `token` 对当前行的 `token`**。
同一个 alias 不代表同一行。只比 `cardinality(models)` 会把"被重建"读成"被覆盖"，
处置完全不同：被覆盖 → 重跑到收敛；被重建 → 重跑也只能保到下一次重建，
得去找发 key 的流水线。

### 两把在这件事上坏掉的尺子

- 🔴 **`updated_at` 量不出"配置什么时候被改的"** —— LiteLLM 写 spend 时会刷它，
  所以按分钟做 `updated_at` 直方图量的是**流量**不是改动。
  （上面防撞那节靠 `distinct_len` 形状列救回来了，但**用它给一次改动定时间是无效的**。）
- 🔴 **`LiteLLM_AuditLog` 对这两个 token 返 0 行**（`store_audit_logs` 疑似没开）。
  "没有审计行"**不是**"没人改过"的证据，是尺子坏了。
- grep 仓库里的 `/key/delete` `/key/generate` 命中 30+ 文件 —— **代码存在 ≠ 该路径被执行**，
  别拿它指认凶手。

### 管用的那把尺子：按 `(nmodels, naliases)` 分桶

两把尺子都坏之后，是**指纹分桶**给出的答案：

```sql
select cardinality(models) nm, count(*) FILTER (WHERE true) n,
       (select count(*) from jsonb_object_keys(coalesce(aliases,'{}'::jsonb))) -- 见下
from "LiteLLM_VerificationToken" where key_alias like 'cursor-%' ...;
-- 实用写法：先在子查询里算好 (nm, na)，外层 group by 1,2 order by 3 desc
```

683 把 `cursor-*` 里 **497 把是 `68/22` 标准模板**，而 `68/0` 桶里**恰好 5 把**，
就是最近 12 小时内新建的那 5 把（02:01 / 03:08 / 03:23 / 03:32 / 03:52 单个错开，
`created_by=default_user_id`）。

⇒ **发 key 的流水线用 68 名默认白名单发，且 22 条 alias 一条都不带。**
那 5 个人打 `glm-5.3-flash` 这类模板名**必然 400**（白名单有名字 ≠ 能用，见下文）。
这个缺陷**早于且独立于**我的改动，且**至今没定位**（grep 找不到、审计为空）；
任何一次重发都会把收敛过的 key 打回原形。

⚠️ 顺带修正一条旧结论：memory 里"别拿 `cursor-liuguoxian-std` 当授权模板（aliases n=0）"
的**成因**就是这个 —— 它 02:01 被重发过，不是它本来就没 alias。

### 验收：`count(distinct sig) = 1`，且从库里查

脚本自带的 `readback_ok` 走的是**写它的同一个 API**，不能自证。收敛的验收判据：

```sql
select count(distinct md5(array_to_string(models,',')) || md5(coalesce(aliases,'{}')::text))
from "LiteLLM_VerificationToken"
where key_alias in ('<ref>','<t1>','<t2>', ...);
-- 必须回 1。回 2 就是有 key 没收敛到位（或参考 key 自己又被重发了）
```

09-21 终态 `DISTINCT_SIGS: 1 of 6`（`nm=32 na=3`，`msig=df586b0f…` `asig=dd4007d5…`）。
⚠️ 指纹相等只证明**配置一致**，不证明那几个人**真能调通** —— 那需要探针
（`cr-g-*` 线要 vscode 形状的 api_key，master key 直打会假红）。

## 远端调用形状（这轮踩的两个坑）

```bash
ssh cltx@10.68.13.198 'bash -s' <<'REMOTE'
export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
[ -z "$LITELLM_MASTER_KEY" ] && { echo "ALERT: empty master key"; exit 2; }
echo "mk_len=${#LITELLM_MASTER_KEY}"   # 应为 39，光看命令成功不算
python3 ~/litellm-198-key-allowlist-<sid>.py --only ... --converge-to ... --apply --backup ~/...json
REMOTE
```

- 🔴 **多值参数数组必须在远端 heredoc 里拼**。在本地拼好插进命令串，远端会重新分词，
  argparse 只认第一个 flag、其余报 `unrecognized arguments`（没报错的话就是静默只改一个名字）。
  非要手拼就断言 `echo "${#ARGS[@]}"` 等于 2×N。
- 🔴 **`LITELLM_MASTER_KEY` 不在远端 env 里 ⇒ `/key/list` 直接 401**。
  空值守卫 + 打长度，别只看 `export` 那行有没有报错。

## 五条不许违反的纪律

1. **`/key/update` 是整字段替换，不是 merge**——`models` 和 `aliases` 都是。必须读回当前值
   再拼回去。直接 `{"models":["新模型"]}` = 把那把 key 其它几十个模型全部抹掉；
   直接 `{"aliases":{"a":"b"}}` = 把那把 key 其余映射全清空（198 上 cursor 平均 9 条 alias）。
   ⚠️ 但**没送的字段不会被动**——只送 `models` 不会清掉 `aliases`（1256 把实测）。
2. **`models == []` 表示「不限制、全部放行」，不许写 `models`。** 给这种 key 写白名单是**收窄权限**。
   脚本 `plan_key` 对空列表恒不产出 `models` 补丁。alias 可以照写（映射不收窄任何东西）。
   198 上就 4 把：`claude-code-canary-bn-{full,quota}-{1787146596,1787148171}`
   （08-19 建、`created_by=default_user_id`、`expires` 空=永不过期、近 30 天 0 请求）。
   ⚠️ **撤模型时这 4 把摘不掉**——它们没有显式白名单可减，所以任何「已全部撤销」的结论
   都要把它们单列出来讲，别报成 0 残留。
3. **金丝雀先行。** `--limit 1` 打一把、验通了再全量。全量 1256 把耗时约 1 分 13 秒。
4. **必须留快照。** `--apply` 强制 `--backup`；连续 2 次写失败自动中止。
5. **跑到收敛，别信单轮的 `applied_ok`。** 同一条命令重复跑，直到 `planned=0` 且 psql 复核为准。
   见下一节。

### ⚠️ drift-verify 的 `--snapshot` 吃的是本脚本 `--backup` 的形状（2026-09-20 修）

`scripts/litellm-key-drift-verify.py` 旧版的 `load_snapshot` 只读 `row["token"]`，
而本脚本 `--backup` 写出来的是 `{token: {key_alias, models, aliases}}`——
**token 在映射键上，行里没有**。于是整份快照被读空，报
`snapshot=0 keys` / `FAIL: zero keys verified`。

**这个假红的形状和 `--prefix` 写错一模一样**，会让你以为范围选错了去改 prefix，
而真相是验收器自己坏了。已修（两种快照形状都吃，行内 `token` 优先），
附单测 `backend/tests/test_litellm_key_drift_verify.py`。

198 上还要两个参数否则照样红得没道理：`--ns litellm-product`（默认是阿里云的 `carher`），
且它 shell 出去调的是**裸 `kubectl`**（不带 sudo、不读 env override），需要 PATH shim：

```bash
mkdir -p ~/bin-0a && printf '#!/bin/sh\nexec sudo /usr/local/bin/kubectl "$@"\n' > ~/bin-0a/kubectl
chmod +x ~/bin-0a/kubectl && export PATH=~/bin-0a:$PATH
```

## 并发写会静默吃掉你的改动（2026-09-08 实测）

198 是**多 Claude 会话并行作业**的机器。两个会话同时对同一批 key 做 read-merge-write 时，
后写的那个用的是自己开跑前读到的快照，会把先写的成果**整片压回去**——而且
两边的 `/key/update` 都返回 200，`applied_ok` 全绿。

当天实测：cursor 632 把写入 `applied_ok=632/632`，几分钟后 psql 查库只剩 **115** 把生效，
518 把回到旧名字；对方的改动完好无损，损伤是单向的。

**识别形状**（`applied_ok` 高 + psql 数远低于它 + 数字随时间往回掉）时的三段式取证：

| 假设 | 证伪条件 | 怎么取数 |
|---|---|---|
| 我的写失败了 | `applied_ok` 应该有 fail | 看脚本输出 |
| 被流量回写覆盖 | 有流量组和无流量组的保住率应有差异 | 关联 30min 内 `LiteLLM_SpendLogs.api_key`，两组比例一样就证伪 |
| **被并发会话覆盖** | `/home/cltx` 下应有别人新投的脚本；`updated_at` 应密集落在我的写入窗口 | `ls -lt ~/*.py`、按分钟聚合 `updated_at` × 是否含新名字；再 `ListAgents` 找 busy 会话直接问 |

**处置**：先 `ListAgents` 联系对方确认它写完了，再重跑到收敛。**不要盲目重写**，会再撞一次。

### 开跑前的四步防撞（2026-09-09 实测跑通，成本约 2 分钟）

上面那张表是**事后取证**。撞了再查代价很大（要双向核损伤、还要重跑），所以写之前先走这四步：

1. **`ListAgents`** —— 有 busy 的会话就当作有并发写手，别赌。
2. **`ls -lt ~/*.py`** 看 198 上有没有别人新投的脚本，文件名和 mtime 就是对方的作业指纹。
3. **`updated_at` 直方图**——按分钟聚合最近 45 分钟，**并且带上形状列**：

   ```sql
   select date_trunc('minute',updated_at) mb, count(*) n,
          count(distinct cardinality(models)) distinct_len
   from "LiteLLM_VerificationToken" where updated_at > now()-interval '45 minutes'
   group by 1 order by 1 desc;
   ```

   **只看 `n` 会误判**：正常流量刷 spend 也会顶到 60~70/min。真正的判据是
   `distinct_len` —— 批量写是**同一形状**（少数几个长度），流量是**参差**（十几个长度）。
   09-09 我看到 71/min 一度以为有人在写，`distinct_len=12` 证伪了它，是流量。
4. **`SendMessage` 直接问**对方三件事：还在写吗 / 何时收敛 / **动的是哪些字段和模型名**。
   第三问最关键——如果和你要动的名字零交集，两边可以并行，不必排队。

**收尾必须反向核对方的字段**：跑完拿对方告诉你的模型名去 psql 数一遍，确认它的成果
还在（09-09 我核了对方的 `gemini-3.8-flash` / `grok-4.6` 各 633/633）。
只核自己那半边 = 只证明了「我没被覆盖」，没证明「我没覆盖别人」。损伤是单向的，
所以两个方向都要查。

脚本已按 token 去重（`/key/list` 分页在写入期间理论上可能让行在页间跳，造成同一把写两次、
另一把一次没写）。⚠️ 但**在无并发写的窗口里两轮实测重复数为 0**，一轮就 518/518 全中——
所以「分页丢行」这个机制在我手上**没有证据**，别拿它当默认解释。要坐实它，判据是：
在确认没有任何并发写的窗口跑一次全量，仍出现重复 token 或 psql 数低于 `planned`。

## 验收：正反双对照 + 换尺子

脚本自带的 readback 用的是**同一个 API**，不能自证。两件事必须另外做：

**① 端到端双对照。** 用户 key 在库里是哈希存的（`token` 字段就是哈希），
**拿它当 Bearer 认证必然 401**——那是打错凭据不是灰度坏了。明文拿不到，
所以造一把**和目标 key 同形状**的临时 key 来验：

```bash
MK=$(sudo kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
NK=$(curl -s -X POST http://127.0.0.1:30402/key/generate -H "Authorization: Bearer $MK" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias":"tmp-probe","models":["<公开名>"],"aliases":{"<公开名>":"<真实组>"},"duration":"20m"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

# 阳性：白名单内 → 应正常回 nonce（客户端会用的每个面都要打：chat / responses / messages）
# 阴性：白名单外（比如刚摘掉的旧名字）→ 必须 403 key_model_access_denied
# /v1/models：应当只看得到公开名，看不到真实组
curl -s -X POST http://127.0.0.1:30402/key/delete -H "Authorization: Bearer $MK" \
  -H 'Content-Type: application/json' -d "{\"keys\":[\"$NK\"]}"
```

只做阳性 = 合成绿。阴性那一枪证明的是「白名单闸门是活的」。
⚠️ 解析 `/v1/messages` 的返回时 `content[0]` 可能是 `thinking` 块（没有 `text` 字段），
文本在 `content[1]`——按 `content[0]["text"]` 取会得到 `None`，那是提取写错不是模型没回。

**② 换尺子查库**（psql 双引号会被当标识符，必须写成 `.sql` 文件再 `kubectl cp`；
角色是 `-U litellm -d litellm`，不是 `llmproxy`）：

```sql
select case when key_alias like 'cursor-%' then 'cursor' else 'claude' end fam,
       count(*) filter (where '<新名>' = ANY(models))      as has_new,
       count(*) filter (where '<旧名>' = ANY(models))      as still_old,
       count(*) filter (where aliases ? '<新名>')          as has_alias,
       count(*) filter (where cardinality(models)=0)       as unrestricted,
       count(*) as total
from "LiteLLM_VerificationToken"
where (key_alias like 'cursor-%' or key_alias like 'claude-%')
  and (blocked is null or blocked=false) group by 1;

-- 误伤：范围外的 key 不该出现这个模型
select count(*) from "LiteLLM_VerificationToken"
 where '<新名>' = ANY(models) and key_alias not like 'cursor-%' and key_alias not like 'claude-%';

-- 既有 alias 有没有被整片抹掉：改动前后 avg(jsonb_object_keys 数) 应只 +1
-- 无关模型有没有被殃及：随手挑一个（如 kimi-k2.7-code）比对改动前后计数
```

还要看**白名单长度分布**：正常应该是整体平移（+1/-1）。出现别的长度 = 有 key 被写坏了。

### ③ 探针已封成脚本，别再每次手搓

`scripts/litellm-198-grok6-cursor-probe.py` 把上面这套判据（复刻形状建临时 key →
逐名真打 → 阴性对照必须红得不一样 → `finally` 删临时 key）封进去了，
`--clone-from` 直接吃 allowlist 脚本 `--backup` 产出的快照。改模型名列表即可复用：

```bash
scp scripts/litellm-198-grok6-cursor-probe.py cltx@10.68.13.198:~/grok-onboard/
ssh cltx@10.68.13.198 'cd ~/grok-onboard && LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product \
  get secret litellm-secrets -o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d) \
  python3 litellm-198-grok6-cursor-probe.py --clone-from $(ls -t backups/cursor-grok6-full-*.json | head -1)'
```

⚠️ 它从 backup 快照复刻形状，而**快照是写之前的 `models`** ⇒ 脚本会把这批新名字
补进复刻列表，复刻出来的是"写完之后应该长的样子"。换用途时这段（`clone_shape`）要一起改。

⛔ `GET /key/info?key_alias=<alias>` **返 404**——这个 filter 不存在，别拿它去取形状
（我 09-20 在这上面浪费了一轮）。形状的来源只有两个：allowlist 脚本的 backup 快照，
或 `/key/list` 翻页找到 token 后 `GET /key/info?key=<token_hash>`。

### 🔴 `mode` 不是 chat 的名字，探针必须换端点

`image_generation` 的名字（如 `sa-grok-imagine` / `sa-grok-imagine-image-2.0`）
用 `/v1/chat/completions` 打**必然红**，而那个红和"白名单没生效"长得一模一样。
写白名单前先 `/model/info` 看每个名字的 `mode`，chat 的走 chat、image 的走
`/v1/images/generations`（判据 `data` 数组非空）。09-20 六个名字里两个是 image，
提前分好端点，一轮就全绿。

## blocked key：加模型时跳过，**撤模型时必须带上**

脚本默认跳过 `blocked=true` 的 key（198 上 38 把：19 把 `cursor-*` + 19 把 `claude-code-*`，
同一批人）。铺新模型时这是对的——它们连认证都过不了，写了是噪音。

**但撤模型时默认值是错的。** blocked 只挡认证，不清白名单：那 38 把 key 的 `models` 原封不动
留着你刚撤掉的模型，将来谁把人解封，权限就跟着静默复活。所以撤权限要显式加
`--include-blocked`：

```bash
python3 $S --prefix cursor- --prefix claude- --include-blocked \
  --rm-model <M> --rm-alias <M> --apply --backup ...
```

输出会打 `include_blocked=` 和 `blocked_in_scope=` 两个数，用来确认范围真的扩到了。
不带这个 flag 时行为与旧版完全一致。

🔴 **`--converge-to` 也吃这条，而且更阴**：收敛本质上就是一次大规模撤权限。
09-26 那次 19 把 blocked key 静默留在 sweep 外，脚本一路全绿 —— 详见上面
「`readback_ok` 回答不了"全部改完了吗"」。现在末尾的 `blocked=N` 桶会点名它们。

判「撤干净了」的 psql 判据**不能过滤 blocked**，否则 38 把残留正好落在你的尺子外面：

```sql
-- 对的：全库扫，一行都不该回
select m, count(*) from "LiteLLM_VerificationToken" t, unnest(t.models) m
where m = '<撤掉的名字>' group by 1;
```

## 198 现状基线（2026-09-09 收盘实测，用来判漂移）

| 家族 | 未 blocked | blocked | `models=[]` 全放行 | 平均 models | 平均 alias |
|---|---|---|---|---|---|
| `cursor-*` | 633 | 19 | 0 | 50.9 | 9.8 |
| `claude-*`（含 `claude-code-*`）| 629 | 19 | 4 | 22.7 | 17.1 |
| `carher-*` | 227 | 0 | 0 | 34.6 | 5.1 |

`scoped_keys`：默认 1262（cursor+claude 未 blocked）· 加 `--carher-` 1489 ·
再加 `--include-blocked` **1527**。三个数对不上就是范围点错了。

全量耗时约 1 分 13 秒（1452 把）。

**2026-09-10 增量**：给 `gpt-image-2` 铺白名单，终态 `cursor-* 634/634`（当天只差 1 把）·
`carher-* 227/227`。全库 1853 活跃 = 1486 有权限 + 285 `models=[]` 全放行 +
**82 把故意不给**（`wa-*` 44 / `tmpreg-*` 10 / `probe-*` / `diag-*` 等内部探针）。
链路上下文见 skill `codex-oneclick-rollout`。

⚠️ 平均值是**判写坏的尺子**，改动前后应当整体平移 ±1；出现别的位移 = 有 key 被写坏。
09-09 撤 4 个 deepseek pro 名字后 cursor 51.94→50.89（-1.05）、claude 23.61→22.65（-0.96），
正好等于人均摘掉的模型数，这才叫干净。

**2026-09-20 增量**：给 cursor 铺 6 个 `sa-*` grok 名（4 chat + 2 image）。
`--prefix cursor- --include-blocked` ⇒ `scoped_keys=677`（比 09-09 的 633+19=652 多 25，
期间新建的）· `unrestricted_in_scope=0` · `blocked_in_scope=19` ·
`applied_ok=676`（+1 把 canary 已写故 `no_change=1`）· `readback 676/676 mismatches=0`。
终态 psql `has_all_six=677/677`，耗时约 100 秒。
**proxy 当前只 1 个副本** ⇒ 这轮没有跨 pod 传播延迟；副本数变回多个时逐 pod 那条纪律要捡回来。

**2026-09-25 增量（撤权限的完整形状，可当模板）**：把 `cr-g` 全族从 cursor 家族摘掉，
只留 `cursor-liuguoxian02-7w7g` / `-liuguoxian03` / `-liuguoxian04-5rub` 三把。

```bash
# 14 个名字必须在**远端** heredoc 里拼数组；--exclude-prefix 防的是发 key 流水线换随机后缀
ARGS=(); for n in $NAMES; do ARGS+=(--rm-model "$n"); done   # 断言 ${#ARGS[@]} == 2N
python3 $S --prefix cursor- --include-blocked \
  --exclude-prefix cursor-liuguoxian02 --exclude-prefix cursor-liuguoxian03 \
  --exclude-prefix cursor-liuguoxian04 "${ARGS[@]}" --apply --backup ~/…/full-<ts>.json
```

`excluded=3` · `scoped_keys=693` · `planned=692`（金丝雀那把 `no_change=1`）·
`blocked_in_scope=19` · `unrestricted_in_scope=0` · `applied_ok=692/692` · **43 秒**。
终态 `cursor-* 673 把 nm=28`（原 41，-13）· blocked 18 把 `nm=55`（原 68）。

三条这轮才立起来的判据：

1. **名字全集从库里取，不许手列。** cr-g 在 key 白名单里有 **42** 个名字，但按腿的
   `-82` / `-135` 后缀那 28 个**只长在豁免的 04 和一把 09-02 的临时 key 上**；
   `select distinct m … where key_alias like 'cursor-%' and key_alias not in (豁免)`
   回的是 **14** 个。少列一个是零症状残留，多列一个会让 `--exclude` 白名单外的 key 也被扫。
2. **验收判据是「快照 vs 当前库逐 key 比对」，且这把尺子要带阳性对照。**
   readback 走写它的同一个 API 不能自证；`avg(models)` 只能看出整体位移。
   判据：对 692 把逐个断言 `db.models == snapshot.models − cr-g` 且
   `db.aliases == snapshot.aliases` **逐字相等**（这条正是防 `/key/update` 逐字段覆盖
   顺手冲掉 alias —— 198 上有一批 key 靠 per-key alias 活着，冲掉零症状）。
   阳性对照：拿「快照 vs 快照减 cr-g」比一次，**必须报出 692 个差异**；
   对照不亮说明比对器自己坏了，本轮所有 `0 差异` 结论作废。
3. **阴性腿要看 `type` 不只看状态码。** 三个摘掉的名字全回
   `403 key_model_access_denied` / 0.0s；阳性腿 `gpt-5.6-luna` 200 出 `pong`。
   ⚠️ 同一轮里 `grok-4.6` 返 **400**，那是**既存**状态不是本轮带入 ——
   pod 日志原文 `ProxyModelNotFoundError: Invalid model name passed in model=grok-4.6`，
   而复刻源 key 本来就 `aliases=0`（快照已证明 alias 一字未动）。
   ⇒ 回归里的 4xx 先去日志分类，再去快照对照，两步都过了才算「我改坏的」。

### 🔴 回归里的 403 先分「本来就没有」和「我冲掉了」

09-20 探针里 `sa-grok-4.6` 在某把 key 上返 403，第一反应是"`/key/update` 整字段替换
把它冲掉了"。**去 backup 快照里查这把 key 写之前的 `models`，本来就没有这个名**——
它 37 个模型里只有裸名 `grok-4.6`，靠 per-key alias 落到 `sa-grok-4.6`。
全库这种「有 alias 无同名白名单」的 cursor key 有 **8 把**。

⇒ 判「我有没有冲掉」的尺子是 **backup 快照，不是当前库**。快照就是你写之前那一刻的
真值，对着它比对才有意义；拿当前库和记忆里的印象比 = 没有对照。

同理，回归必须验**裸名**而不只是 `sa-` 真名：633 把 cursor key 的
`grok-4.6 → sa-grok-4.6` alias 才是主流量路径，裸名 200 才算没回退。

### 🔴 回归里的 **400 ≠ 403**：403 是我可能冲掉的，400 从来不是（2026-09-28 加）

上一条讲 403。**400 是另一个病，别混着查。** 09-28 给全舰队加完 `jev` 后跑回归，
29 名白名单里 `grok-4.7` 和 `glm-5.3-flash` 返 **400**，其余 200。

- **403 = 白名单闸门挡的**（`type=key_model_access_denied`）⇒ 名字不在这把 key 的 `models` 里
  ⇒ 这才是"我可能冲掉了"的形状，按上一条去 backup 快照比。
- **400 = 路由里没这个组**（消息被我们的补丁打码成中文 `API 异常 (req: ...)`，`type=None`）
  ⇒ 白名单里**有**这个名，只是 198 上没有同名组。**白名单有名 ≠ 路由有组**
  （[[feedback_allowlist_name_without_alias_is_400]]），跟 key 改动无关。

分开的判据，三样都要（只看状态码会把既存故障读成"我打坏了"）：

```
# ① 这个名在组注册表里吗（唯一权威是 /model_group/info，PMT 不全）
curl -s -H "Authorization: Bearer $MK" $PROXY/model_group/info \
  | python3 -c 'import sys,json;print({g["model_group"] for g in json.load(sys.stdin)["data"]} & {"grok-4.7","glm-5.3-flash"})'
# ② master key 直打 —— 还是 400 就与 key 白名单无关
# ③ 改动前它成功过吗：SpendLogs 里 spend>0 的行数，不是总行数
select model, count(*) total,
       count(*) filter (where "startTime" < '<改动时刻>') before_change,
       count(*) filter (where "startTime" < '<改动时刻>' and spend > 0) before_and_billed
from "LiteLLM_SpendLogs" where model in (...) group by model;
```

09-28 实测：两个名都**不在** 325 个组里、master key 直打同样 400、改动前 155/35 条记录
而 `spend>0` 的**一条都没有**（从 09-22 / 09-21 起就没成功过）⇒ 既存故障，改动前后同形。

⚠️ **③ 的 `count(*)` 单独用会撒谎**：我第一次查的是 `max(startTime)`，拿到的是**我刚才那两次
探测本身**（探测时刻晚于改动时刻），看着像"改动后还在用"。有失败行不代表通过，
判"曾经能用"只认 `spend > 0`。

### 两层回归：值层比快照，行为层真打（09-28 定型）

`applied_ok` / `readback_ok` 是脚本给自己打的分（见上面 §`readback_ok` 那条）。回归要另外两层：

**值层** —— 逐把 key 做集合比较，判据是 `now == old ∪ {新增名}`，一个名都不许少：

```python
lost   = set(backup[tok]["models"]) - set(now[tok]["models"])          # 必须空
gained = set(now[tok]["models"]) - set(backup[tok]["models"]) - {NEW}  # 必须空
aliases_changed = (backup[tok]["aliases"] or {}) != (now[tok]["aliases"] or {})  # 必须 False
```

四个读数要分开报：`EXACT(=old∪{new})` / `NAMES DROPPED` / `UNEXPECTED EXTRA` / `rows_vanished`。
别合成一个百分比（[[feedback_two_failure_faces_must_be_counted_separately]]）。

**行为层** —— 值层只证"名字在表里"，证不了"还能打"。明文 key 取不回来（库里只有 hash），
所以**克隆一把被改过的真 key 的 `models` 数组**起一把短时 `duration=20m` 临时 key，
打三类名：改动前就有的老名字（回归对象）+ 新增名（正对照）+ 白名单外的名（阴性对照，必须 403，
证明门禁真在挡、那些 200 不是因为门没关）。跑完 `/key/delete` 并回库 `count(*)` 复核为 0。

🔴 **导出 DB 做比对别用 `-F$"\t"`**：经 `ssh '...'` 传过去时 `$"\t"` 不展开，psql 收到的是
**字面两字符 `\t`**，文件里一个真 tab 都没有 ⇒ `split("\t")` 把所有行静默丢进
`len(p) < 4` 分支 ⇒ 解析出 0 行，却报成 `rows_vanished=1700`。**尺子坏和真丢行同形。**
改用不依赖分隔符的单列 JSON，并在比对前断言自己数得到：

```bash
psql -t -A -c "select json_build_object('t',token,'m',models,'a',aliases)::text from \"LiteLLM_VerificationToken\";" > now.jsonl
```
```python
assert len(now) == EXPECTED_DB_ROWS, f"ruler broken: {len(now)}"   # 先自证数得到
# 阳性对照：人为往快照里塞一个假名，尺子必须抓到它丢了
assert (set(probe) - set(now[tk]["models"])) == {"__synthetic_name__"}
```


## 命名惯例

同一个后端常有多个前缀名，**Cursor 和 Claude Code 发的名字不一样**，铺的时候别只铺一个：

- 裸名 `grok-4.6` / cursor 系 `cursor-grok-4.6` / claude 系 `claude-grok-4.6` / sub2api 系 `sa-grok-4.6`
- 若客户端可能发多个名字，要么全部加进白名单，要么用 per-key `aliases` 把它们映射到同一组
  （见 `litellm-key-provider-swap` 的「双 alias 全量同步」——只改裸名漏掉前缀名，日志会显示
  已改、实际仍走旧上游）。
- **同名不同后端要当心**：`claude-kimi-k3` 走 kimi-proxy、`cursor-kimi-k3` 走 cursor-agents-shim、
  `sa-kimi-k3*` 走 sub2api，名字像但是三条独立的路。摘名字前先查它的 `litellm_params.model`
  和 `api_base`，别把「统一命名」做成「把用户从一条能用的路挪到另一条」。

### 🔴 客户端发的名字**只有他的 SpendLogs 知道**，探针名字是我自己给的（2026-09-22）

给 `cursor-liuguoxian-std` 配完 kiro 的三个 claude 名之后，用户报「haiku 有问题」。
我把 `claude-haiku-4.5` **四个面全打绿了**：Cursor 线型（`stream`+`tools`）200 出字、
chat 非流式 200 回显 nonce、`/v1/messages` 200、`max_tokens` 64000/128000 都 200、
阴性编造名 403 —— 尺子有分辨力，读数不作废，**但和他的故障无关**。

因为他 Cursor 里发的是 **`claude-haiku-4-5`（连字符）**，白名单里是
**`claude-haiku-4.5`（点号）**。两个都是合法写法，差一个字符，403 死在准入闸门，
**根本没走到 kiro**。opus-5 / sonnet-5 两边写法恰好一致，所以只有 haiku "有问题"。

| 假设 | 证伪条件 | 数据 |
|---|---|---|
| kiro 没有 haiku 模型 | `ListAvailableModels` 里应查不到 | **有**，`claude-haiku-4.5` 0.4x / 200K，19 个模型里第 14 个 |
| alias 没配对 / 配错了 | 库里 `aliases` 应为空或指别处 | 指的就是 `kiro-claude-haiku-4-5` |
| 名字对但链路坏 | 四个面里应至少一个红 | 全绿，且 SpendLogs 落 `kiro-rs/claude-haiku-4-5-20251001` |
| **他发的不是这个名字** | 他 key 的 SpendLogs 里应只有点号名 | **`model=claude-haiku-4-5` `status=failure` `error_code=403`** |

⇒ **配完模型的验收，最后一道必须是他真 key 的 SpendLogs**，不是我挑名字打的探针。
探针量的是「我以为他发的名字通不通」；他发什么名字，只有他的行里有。

```sql
-- 失败行**带** user_api_key_alias（实测过，这把尺子是好的），直接按 alias 捞
select "startTime", model, model_group, status,
       metadata->'error_information'->>'error_code',
       left(metadata->'error_information'->>'error_message', 80)
from "LiteLLM_SpendLogs"
where metadata->>'user_api_key_alias' = '<key_alias>'
  and "startTime" > now() - interval '12 hours' and status = 'failure'
order by "startTime" desc limit 20;
```

🔴 **`model` 这一列在成功行和失败行里语义不同**：成功行是**落点**
（`anthropic/claude-haiku-4-5-20251001`），403 失败行是客户端**发来的原始名**
（`claude-haiku-4-5`，未经 alias 改写、`model_group` 和 `model_id` 都是空）。
两种名混在一列里，不带 `status`/`model_group` 一起 group by 就读不出来。
⇒ 判「死在准入/解析这道」的判据是 **`status='failure'` 且 `model_group` 为空**。

这两段已经落进 `scripts/litellm-key-activity.sh`（model summary 加 `model_group`+`status`
两维、Failure breakdown 加 `model` 列、新增一段 "requested names that never landed"）。
**别再手搓这个查询**，跑 `./scripts/litellm-key-activity.sh <key_alias> pro 12`。

### 改名：add → verify → cutover → remove，与「合成一次写」的边界

上面那节「改名/收敛要在一条命令里同时删旧、加新」防的是**旧名还在服务**时的窗口。
但当**旧名此刻零流量**（比如它从来就没被客户端发过，用户打的一直是新名），
就该按加法律令拆三步，因为 verify 必须发生在 remove 之前：

```bash
# ① add：新名 + alias 一次写（旧名不动，此刻两个名都在）
python3 $S --only <key> --add-model claude-haiku-4-5 \
  --alias claude-haiku-4-5=kiro-claude-haiku-4-5 --apply --backup ~/…-add-<ts>.json
# ② verify：Cursor 线型真打新名 + **换尺子看 SpendLogs 落点**（见下条，光 200 不够）
# ③ cutover/remove：确认落点对了，再摘旧名 + 旧 alias，一次写
python3 $S --only <key> --rm-model claude-haiku-4.5 --rm-alias claude-haiku-4.5 \
  --apply --backup ~/…-cutover-<ts>.json
# ④ 阴性复核：旧名现在必须 403（证明摘干净了，不是"还留着只是没走到"）
```

🔴 **新名撞上同名真实组时，②那步的落点复核不能省。** `claude-haiku-4-5` 在 198 上
**本身就是一个真实组**（网宿 `aigateway.edgecloudapp.com`），`claude-haiku-4.5` 也是
（copilot2api-4/-5 两腿）。只加白名单不写 alias ⇒ 照样 200，但**静默落到别的供应商**，
和 `UPSTREAM CHANGE` 是同一种零症状故障。判据只认 SpendLogs 的 `model_id`
（09-22 复核：两行全是 `kiro-rs/…`，零行落网宿），⛔ 不认 `x-litellm-model-id`（是 hash）。

### ⛔ `/key/delete` 不认 `key_aliases`，只认 token hash

清理探针临时 key 时 `{"key_aliases":["zz-probe-…"]}` 返 **404 `{'error': 'No keys found'}`**，
看起来像"这把 key 不存在"，其实是这个 filter 不生效。正解是先从库里取 `token`
（库里存的就是 sha256），再 `{"keys":["<token hash>"]}` —— 返 `deleted_keys` 才算删掉。
⚠️ 探针脚本被 `TaskStop` / ssh 断开打断时 `finally` 不一定跑得到，远端 python
还会**继续跑**（ssh 断了进程不死），要 `pgrep -af <脚本名>` 杀掉再按上面这条清 key。



白名单只管**准入**，解析是另一道。一个名字要真能调通，得满足其一：
它是 `LiteLLM_ProxyModelTable` 里的真实组、或有全局 `model_group_alias`、或有 per-key `aliases`。
**三样都没有 = 过了准入闸门然后 400。**

09-09 实测（两把同形状临时 key 打真流量）：

| key | 结果 |
|---|---|
| `models=["grok-4.6"]`，**无 alias** | **400** |
| `models=["grok-4.6"]` + `aliases={"grok-4.6":"sa-grok-4.6"}` | **200**，原样回 nonce |

裸名 `grok-4.6` 不在 `ProxyModelTable`（真实组是 `cursor-grok-4.6` / `sa-grok-4.6`），
也没有全局 alias，`/v1/models` 拿 master key 查根本列不出它。
成因是路 A `grok-proxy` 09-08 被删，名字留在了 1200+ 把 key 的白名单里。

⇒ **清点「谁能用 X」时，只数 `models` 会高估。** 判据是
`count(*) filter (where 'X' = ANY(models) and (aliases ? 'X' or X 是真实组))`。
198 上现存这种半截状态：**19 把 blocked `cursor-*` 有 `grok-4.6` 无 alias**
（用户 09-09 裁决保持现状，解封前要记得补 alias 否则打了就 400）。


## 铺之前先算一道除法

给 1200+ 把 key 开一个模型之前，先看它背后的额度是**独享**还是**共享一个上游账号**。

2026-09-08 `sa-kimi-k3` 就是反面教材：背后是**一个** Kimi Allegro 会员，
**100 次/5h 且 100 次/周**，1258 把 key 共用——全公司一周总共 100 次。
铺是照做了（用户拍板），但必须同时讲清它只能是「尝鲜/低频备选」，
且**绝不进 fallback 链**。详见 memory `project_kimi_allegro_sub2api_198_2026_09_08`。

## 相关

- `litellm-key-provider-swap` —— per-key alias 改道 / 单人切供应商
- `her-public-model-name` —— 同一支脚本打**阿里云**（`LITELLM_BASE` 指哪打哪）给 420 把
  `carher-*` 铺对外名的特化流程：目标组要先在阿里云存在（跨集群搬组名会 400）、
  两个 proxy pod 的 key 元数据缓存差约 2 分钟、`scripts/litellm-key-drift-verify.py`
  把「我的没落地」和「别人的被我压了」分开报。
- `sub2api-grok-ops` —— sa-* 系列 entry 的后端（sub2api）运维
- 历史一次性脚本 `scripts/litellm-cursor-add-astra.py` 是本脚本的前身；
  **白名单/alias 批量增删一律用本脚本**，别再复制新的一次性版本。
  ⚠️ 2026-09-10 我又犯了一次：先手搓 `/tmp/batch.py` 铺完 227 把 carher key，
  事后才发现本脚本早就支持 `--prefix carher- --add-model gpt-image-2`。
  手搓那版能跑，但**没有 `--backup` 快照、没有 `--restore`、没有 `--limit` 灰度**——
  真写坏了只能靠 DB 备份回滚。**动 key 之前先 `ls scripts/litellm-198-key-*`。**
- 跨机传脚本**必核 sha256 再执行**：09-10 `scp` 静默产出过同字节数（1925）但开头一片 `\x00`
  的坏文件。比指纹要用同一把尺子（mac `shasum -a 256` 与 linux `sha256sum` 输出格式不同，
  我因此先误判成"传坏了"），统一 `openssl dgst -sha256`；更稳的传法是 base64 管道。
