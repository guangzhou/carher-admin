# zk-delta

把 Cursor 每轮重发的全量会话，在**跨公网那一跳**上压成只发新增的几条消息；
到了集群里再**逐字节重建**成原样，转给 LiteLLM。

上游（LiteLLM → zerokey 网关 → GPT 网页）收到的字节和没有 zk-delta 时**完全一样**，
所以它不会改变任何模型行为。

## 它解决什么

Cursor 每一轮都把整个会话重发一遍。实测一条 16 轮的会话，第 1 轮 369KB，第 16 轮 2.22MB，
累计要往公网推 19.7MB —— 其中 89% 是前面几轮原样重复的内容。

接上 zk-delta 之后：

```
轮 1  原始   369,358B  出网  369,658B   (首轮必须发全量)
轮 2  原始   492,489B  出网  123,451B   省 74.9%
轮16  原始 2,216,366B  出网  123,459B   省 94.4%

累计 19.73MB → 2.12MB，省 89.3%；出网恒定不随轮次增长
```

跑得更长也不漂移。30 轮 soak（同一条会话一路数数，数错就说明历史丢了）：

```
轮30  原始 3,284,963B  出网 102,983B  省 96.9%  重建逐字节相同  答:"thirty"

30/30 轮重建逐字节相同，400 计数 0，出网增长 第30轮/第1轮 = 0.334x
累计 51.40MB → 3.14MB，省 93.9%
```

顺带也解决了长会话撞 LiteLLM 入口闸门返回 400 的问题——但那其实是另一个独立的修法
（给模型行显式配 `max_input_tokens`），见 `docs/zk-delta-plan-20260831.md` 的 S1。

## 形状

```
Cursor ──► 本机小代理 :8788 ──公网只发增量──► nginx /zkd/ ──► zk-delta ──重建全量──► LiteLLM ──► 网关 ──► GPT
           sidecar/sidecar.js                                server/server.js
```

## 凭什么敢说"逐字节相同"

`JSON.stringify` **不保证**能还原你读进来的那串文本：`1.0` 会变成 `1`、`1e3` 变成 `1000`、
`é` 变成字面的 é、美化过的空白会被吃掉。所以这不能当设计假设，只能当**每条请求的运行时事实**去证。

三环证明链（`common/framing.js`）：

1. **小代理每条请求本地自证**：`JSON.stringify(rebuild(template, items)) === 原始字节`。
   证不出来就原样透传、并计数——绝不硬上。
2. **前缀摘要相等** ⇒ 服务端存的每一条历史 item，其 stringify 结果与客户端的逐字符相同。
3. **增量 item 和 template 以 JSON 原样传输** ⇒ 两边 stringify 结果相同。

⇒ 服务端重建出来的，就是客户端原始的那串字节。

服务端还会把重建结果的字节数回填在 `x-zk-rebuilt-bytes` 响应头里，客户端逐轮核对。

## 失效了怎么办：硬报错，绝不静默

任何认不出基线的情况一律 `409 delta_base_not_found`，客户端收到就重发全量。
**绝不把认不出的增量当全量转发**——静默失忆正是 08-30 那次翻车的根因。

14 种 409 原因各自独立计数（`handle_unknown` / `base_digest_mismatch` /
`template_changed_but_absent` / `rebuilt_size_mismatch` / …），在 `/metrics` 里看得见。

## 跑测试

```bash
# 离线金样（不需要任何凭据，起假上游）—— 当前 45 绿 / 0 红
node zk-delta/tests/run.js

# 只跑第 ⑩ 组：编码对抗性性质测试（6025 条样本，跑完约 3 秒）
node zk-delta/tests/fuzz_encoding.js

# 真流量多轮（打真模型真号池）
ZKD_KEY=sk-xxx node zk-delta/tests/live_multiturn.js cursor-web-fc-82-terra 16 100

# 门② 真功能对照：真 agent 循环，基准腿 vs zk-delta 腿
ZKD_KEY=sk-xxx node zk-delta/tests/live_agentloop.js
ZKD_KEY=sk-xxx ZKD_TASK=lark ZKD_ALLOW_LARK=1 node zk-delta/tests/live_agentloop.js

# 门① 真 GUI 版：从 fixtures 里取一条**真 Cursor 抓包**，两腿各打一次，
# 再去网关日志比它两次实际收到的 [PROMPT] REQ 块（归一化后必须零差异）
ZKD_KEY=sk-xxx ZKD_SSH_PASS=xxx node zk-delta/tests/live_gate1.js

# 韧性 / 故障注入：服务端忘会话、小代理重启、会话交错、服务端不可达、伪造 handle
ZKD_KEY=sk-xxx ZKD_SSH_PASS=xxx node zk-delta/tests/live_resilience.js
```

### 韧性那组在证什么

顺风路径（链路通、答得对、字节省下来）之前验得很足，但代理真正会害人的时刻是**出岔子的时候**。
这一组的判据统一是一句话：**任何失效都必须表现为「硬报错 + 退回全量 + 计数 +1」，
绝不能表现为「静默少发历史」**——后者会让上游收到残缺会话而 HTTP 还是 200，
那正是 08-30 翻车的形状。5 条全绿，其中最关键的是「`rollout restart` 之后照样答对且 409 +1」。

### 第 ⑩ 组在证什么

⑨（真实抓包金样）想抓的风险，本质不是「Cursor 说了什么话」，而是「Cursor 那侧的
JSON 序列化器写出来的字节，我这边能不能原样复现」——那是**编码**问题，不是**内容**问题。
所以在拿到真实抓包之前，可以先用对抗性编码把这条风险面系统性打一遍，而且打得比几十条
真实样本更全。⑨ 现在已经用 23 条真抓包转绿了，第 ⑩ 组仍然留着——它覆盖的编码形态比
真实样本更极端，两者是互补不是替代。

样本分两池：对抗池 4000 条（故意塞 `1e400`、超精度整数、落单代理项、转义斜杠、
重复键、整数样式的键……全部写成 **JSON 源文本**而不是 JS 值，否则要考的东西提前被抹平了），
干净池 2000 条（用 JS 值构造再 stringify，也就是任何正常客户端写出来的那种紧凑 JSON），
外加 25 条手工刁钻样本。随机数用固定种子的 mulberry32，失败必须可复现。

| | 性质 | 实测 |
|---|---|---|
| ⑩a | 说 ok 的，走完整条链路后重建出的字节必须与原文完全相同 | 2283 条，零反例 |
| ⑩b | 说 not ok 的，理由必须在已知集合内，不能抛异常 | 拒 3742 条，全部已知 |
| ⑩d | 从任意位置切成「已存前缀 + 本轮增量」，结果 === 一次性全量重建 | 每条样本的每个切点 |
| ⑩e | 两条 stringify 结果不同的 item，摘要必须不同 | 3799 条，零碰撞 |
| ⑩f | **干净紧凑的真实形状必须 100% 走进增量** | 2000 / 2000 |

⑩f 不是「测试自检」，它本身就是产品判据：干净 body 被拒 = 静默退化成全量转发，
zk-delta 等于白装。所以要求 100%，不留余量。

⑩a 里那条「说 ok 却重建错」是**唯一会静默污染上游**的失败模式，必须恒为 0。

## 部署

```bash
ZKD_SSH_PASS=... ./zk-delta/k8s/apply.sh --dry-run
ZKD_SSH_PASS=... ./zk-delta/k8s/apply.sh
```

刻意不建新镜像：代码是纯 Node 标准库，复用节点上已有的 `zerokey-codex` 镜像，
源码用 ConfigMap 挂进去，`imagePullPolicy: Never`。既不用在构建服务器出镜像，也不碰公网仓库。

`replicas: 1` + `strategy: Recreate` 是刻意的：会话状态在进程内存里，多副本会让 handle 认不出而狂 409。
要扩副本必须先把 store 挪到 Redis，那是另一件事。

## 巡检（只读）

```bash
ZKD_SSH_PASS=... ./zk-delta/k8s/audit.sh     # 退出码 0=全部不变量成立，1=有漂移
```

zk-delta 的正确性不是靠代码写得对来保证的，是靠几条**运行时不变量**保证的：
单副本、Recreate、`pullPolicy: Never`、集群上跑的源码指纹 == 仓库这份、nginx `/zkd/` 还在、
409 占比没失控。这几条里任何一条被改掉（或被一次误 apply 回退掉），服务**照样返回 200**，
不会有任何报警——只会静默退化。所以必须有一条会用退出码说话的巡检。

全脚本只有 `get` / `curl`，没有 `apply` / `patch` / `delete` / `restart`。

上游非 2xx 按状态码分开判，不一锅炖：**401/403** 是客户端凭据问题（比如 `switch.sh` 自检不带 Key），
zk-delta 只是忠实透传，不算漂移；**400** 是要盯死的那个形状（Cursor 长会话撞入口闸门那条线），
一发都要报；其余 4xx/5xx 同理。混成一个数的后果是巡检见 401 就喊狼，喊几次之后就没人看了。

### 怎么证明"这一轮回归没吃掉 codex 的额度"

`cursor-web-fc-82-terra` 钉在 acct-82，而 acct-82 也在 codex 那两个组的 35 个账号里——
同一个 ChatGPT 账号 = 同一个 24h/7d 窗口。zk-delta 本身动不了它（重建在 LiteLLM 之前，
上游收到的请求数和字节与不装它时逐字节相同），**能吃额度的是我的回归流量**。

```bash
ZKD_SSH_PASS=... ./zk-delta/k8s/codex7d_guard.sh mine 3     # 这 3 小时里各模型各打了多少
```

08-31 那轮实测：整夜 26 发全部落在 `local-deepseek-v4-flash`（自建 GPU 盒），
ChatGPT 侧零消耗。

**别用 `snap` / `diff` 那对子命令下这个结论——上班时段它 100% 假阳。** 7 天窗口是滑动的，
前后差只在集群空闲时才等于"我打的量"。实测 03:00 那一小时 `gpt-5.6-sol` 全公司正常业务
就有 4165 发，而我自己只有 26 发，diff 于是报出一堆 `!` 和"codex 侧有新增请求"——
那全是别人在正常上班。`mine` 才是有效的那把尺子：**我打了哪个模型是我自己控制的**，
别人的流量落在别的模型名上，一眼分得开。

## 全员发（08-31 上线）

装机安装器 `scripts/zk-cursor-web/cursor_team_setup.js` 已经带上 zk-delta，**默认开**。
同事拿到的 `cursor-g-setup.zip` 里多了 `zk-delta/`（小代理 + framing）和一个
`TURN-OFF-DELTA-Mac.command`（双击退回公网直连）。

```bash
sh cursor_team_setup.sh --apply                  # 整包装机，含 zk-delta
sh cursor_team_setup.sh --apply --zk-delta-only  # 只装小代理 + 改地址，别的一样不碰
sh cursor_team_setup.sh --apply --no-zk-delta --zk-delta-only   # 关掉（= 那个双击按钮）
```

几个刻意的取舍，都是被具体问题逼出来的：

- **小代理用 Cursor 自带的 Electron 跑**（`ELECTRON_RUN_AS_NODE=1`），不用 `node`。
  分发包的卖点就是"不用装 Node"；plist 里写 `node` 的话，同事机器上没有它服务就起不来，
  而这时 BYOK 地址已经指到 `127.0.0.1:8788` 了 —— **等于把他的 Cursor 弄坏**。
- **顺序：先起小代理，起来了才改地址。** 倒过来就是上面那个坏法。起不来会当场打日志、
  保持公网直连、并明说没改地址。
- **同事那边抓包硬关**（`ZKD_CAPTURE_MAX=0`）。开着等于把别人的真实工作内容写到他自己磁盘上。
- **`--zk-delta-only` / `--keep-model` 不碰选中模型。** 默认逻辑是"选中的不是 cursor-g 就换成
  `cursor-g-5.6-sol`"，在基准机上那等于换掉量具。
- **只在 macOS 实测过。** Windows/Linux 没有 launchd，这一步直接跳过并说明，
  不写一个没验证过的服务定义假装支持。
- **`--revert` 会一并卸掉小代理**：回滚把地址还原成公网，留着服务就是个占着 8788 的孤儿。

三层失效兜底（都实测过）：集群挂 → 小代理回落直连（`passthru`）；进程挂 → launchd `KeepAlive`
约 1 秒拉起（代价：内存里的会话句柄丢了，每条会话下一发 409 重发一次全量）；想彻底退 → 那个双击按钮。

打包脚本里有个坑值得记：`zip -r` 是**往已有归档里追加**而不是重建，改过文件名之后旧名字
会留在包里。已改成先 `rm -f` 再打。（另：zip 里存中文文件名跨平台会乱码，按钮名用 ASCII。）

## 容量：单副本能扛多少人

`tests/capacity_probe.js`（纯本地，一发不打上游）灌到 277MB store 实测：

| | |
|---|---|
| 边际拟合 | `rss ≈ 175MB + 1.13 × store`，相邻点最大斜率 **1.24x**（按这个保守值判） |
| 生产 `ZKD_MAX_BYTES=800MB` 撑满 | RSS ≈ 1169MB，加 400MB 并发 buffer ≤ 2Gi limit → **不用改** |
| 生产 `ZKD_MAX_CONV=400` 单独 | 长会话下 1847MB store → RSS ≈ 2471MB → **单独拦不住，会 OOM** |
| 20 人 × 8 条活跃会话 | 739MB store，在上限内 |
| 40 人 × 8 条活跃会话 | 1478MB store，超上限 → 淘汰最久没用的会话（那条下一发退化成全量，不出错） |

**真正在拦的是字节上限，不是条数上限，两条都得留着。**（每人 8 条会话是**假设**不是实测——
只有我自己一台机器的行为数据。）

判上限**不许用 `rss/store` 这个总倍率**：它被固定基座污染，store 小时会飙到 20x，
照它算出来的上限小得荒唐。我第一版就是这么算的，得出"800MB 必须调小"的错误结论。

`audit.sh` 第 [7] 组现在会盯这条：RSS 超 limit 70% 报黄、85% 报红，并按边际斜率外推
"把配的上限撑满会到多少"。理由是 OOMKill 的失败形态是**静默**的——单副本 + 内存态，
炸一次所有人的会话全丢却不报错，只是每条会话下一发退化成全量。所以必须在发生**之前**看得见。

## 本机切换

```bash
./zk-delta/switch.sh on       # 切到 zk-delta（装常驻小代理 + 改 Cursor BYOK 地址）
./zk-delta/switch.sh off      # 一条命令切回今天的地址
./zk-delta/switch.sh status   # 看现在指到哪、省了多少
```

`on`/`off` 都要求 Cursor 完全退出——外部写 `state.vscdb` 有内存覆盖竞态，Cursor 开着改了也会被它写回去。

改地址走 `zk-delta/cursor_baseurl.js`，**只动 `openAIBaseUrl` 一个字段**，写完当场读回来验一条
不变量：把地址换回旧值之后两个 blob 必须完全相同，不成立就自动还原并退非零。

这个 `switch.sh` 和上面那个安装器是**两个入口，各管各的**：安装器是给同事装机用的分发路径，
`switch.sh` 是我本机改一个地址用的。之所以没合并成一个，是因为安装器 `--apply` 会顺手重打
app bundle、塞 6 个 cursor-g 模型、提示粘 Key，还有最要命的一条：`mergeConfig()` 里当选中模型名
不以 `cursor-g` 开头时，会把 composer / cmd-k 覆盖成 `cursor-g-5.6-sol`。而本机 composer 正是门①门②
的基准 `cursor-web-fc-82-terra`——拿安装器当开关用，会把基准模型悄悄换掉，而且不报任何错。

08-31 全员发那一轮给安装器补了 `--keep-model` / `--zk-delta-only` 两个开关把这条堵上了
（`--zk-delta-only` 隐含 `--keep-model`），装 / 卸 / 再装三趟实测 composer 一直是
`cursor-web-fc-82-terra` 没动过。但**默认路径依然会换模型**，所以在基准机上永远走 `switch.sh`，
或者走安装器时必须显式带 `--zk-delta-only`。

`on` 默认**顺便开采集**：真实 body 落到 `tests/fixtures/`，上限 40 条 / 512MB，采够自己停手
（`CAPTURE_MAX=0 ./zk-delta/switch.sh on` 可以关掉）。

采集有三道门，每一道都是被真事故逼出来的：

1. **UA 必须是 Cursor** —— 否则 `on` 里那条 93 B 的自检 curl 也会被采进去，拿我自己造的
   样本让 ⑨ 转绿就是假绿。被挡的记 `capture_skipped_ua`。
2. **必须是有正文的聊天请求** —— 否则 Cursor 启动时那发 `GET /v1/models` 会落成 0 字节
   文件，⑨ 一 `JSON.parse` 就崩。
3. **不许带 `x-zkd-synthetic: 1`** —— `tests/` 里每个 live 脚本都写死
   `'user-agent': 'Cursor/3.17.19'`（为了走同一条代码路径），所以第 1 道门**认不出我自己**。
   08-31 复查时 40 个采集额度里有 17 个是我的测试脚本吃掉的，`capture_left` 归零，
   池子冻死，真 body 再也进不来。被挡的记 `capture_skipped_synthetic`。
   **门认的东西必须是被验对象无法满足的；UA 由我自己写，就不是那种东西。**

出处在**采集当场**记进 `fixtures/_manifest.jsonl`（file / bytes / ua / tools / provenance）。
⑨ 只回灌 manifest 里有出处的条目；目录里有文件而没有 manifest = 出处不明，⑨ 直接红。
事后靠 body 大小猜是真抓包还是我自己造的，那是猜，不是证据。
`provenance` 写 `gate_passed` 不写 `gui`：小代理只能证到「过了门」，证不到「来自真 GUI」。

封顶不是可选项：真实 body 每条几 MB、Cursor 每轮一发，不封顶跑一天能把本机磁盘写爆，
而金样几十条就够用。**封顶按磁盘上已有多少算**，不是按本进程采了多少——原先每次重启
额度就重新给满 40，重启 N 次能攒 40N 条，而封顶存在的理由恰恰是跨重启才成立的。
采集进度露在 `/metrics.json` 的 `capture_taken` / `capture_left` 里。

`tests/fixtures/` 整个目录在 `.gitignore` 里——**采到的是你的真实会话正文和代码，绝不入库。**
（原先只写 `fixtures/*.json`，而 `_manifest.jsonl` 不匹配 `*.json`，差一个字母就漏出去了。）

## 还没做完的

- 服务端会话状态在内存里，pod 重启会全丢。丢了不出错，只是每条会话下一发退化成全量
  （`rollout restart` 之后照样答对、409 +1，已在韧性那组实测）。要扩副本必须先把 store 挪到 Redis。
- 网关侧 `responses.js` 的按形状瘦身还没做，那要走它自己的一轮门① 验证。
- 只有 macOS 有小代理常驻服务。Windows/Linux 没有 launchd，安装器那一步直接跳过，
  同事在那两个平台上是公网直连（功能正常，只是不省流量）。
- 「每人 8 条活跃会话」是拍的假设，没有多人实测数据。等真有人用起来了，
  `/metrics.json` 的 `conv_live` 就是真数据，那时候上面那张按人数的表要重算。

## 已经不成立的旧说法

早期版本的这份 README 说过两句话，现在都不成立了，留在这里免得有人照着老版本操作：

- ~~「`tests/fixtures/` 是空的，⑨ 是红的」~~ —— 2026-08-31 用**真 Cursor GUI** 采到真 body，
  ⑨ 已转绿，`run.js` 现在是 **45 绿 0 红**。
- ~~「⑨ 是 40 条真抓包」~~ —— 复查发现其中 **17 条是我自己的 live 测试脚本**（它们写死
  UA=Cursor 骗过了采集门），真 GUI 抓包只有 **23 条**（`tools=19` 是真 Cursor 的指纹，
  我的脚本只发 1 个）。不是假绿（23 条真抓包本身就够 ⑨ 成立），但**报的数虚增了 17**，
  且采集额度被自己吃满冻死。已加第三道门 + 出处 manifest，⑨ 现在报 `n=23`。
- ~~「只差一条命令 `./zk-delta/switch.sh on`」~~ —— 那条命令当时**跑不通**：脚本用系统 node 去跑
  安装器，而安装器靠 `process.execPath` 推导 Cursor 安装目录，会去 Homebrew 的 node 前缀底下找
  Cursor 而报「找不到 Cursor 资源目录」；而且调用没带 `--apply`，就算路径对了也只是 dry-run。
  也就是说在此之前 `switch.sh on` 从来没真的切过地址。已改成 `cursor_baseurl.js`，验过 off/on 往返。
