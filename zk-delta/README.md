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
真实样本更全。⑨ 现在已经用 22 条真抓包转绿了，第 ⑩ 组仍然留着——它覆盖的编码形态比
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

## 本机切换

```bash
./zk-delta/switch.sh on       # 切到 zk-delta（装常驻小代理 + 改 Cursor BYOK 地址）
./zk-delta/switch.sh off      # 一条命令切回今天的地址
./zk-delta/switch.sh status   # 看现在指到哪、省了多少
```

`on`/`off` 都要求 Cursor 完全退出——外部写 `state.vscdb` 有内存覆盖竞态，Cursor 开着改了也会被它写回去。

改地址走 `zk-delta/cursor_baseurl.js`，**只动 `openAIBaseUrl` 一个字段**，写完当场读回来验一条
不变量：把地址换回旧值之后两个 blob 必须完全相同，不成立就自动还原并退非零。

这里刻意**不复用** `scripts/zk-cursor-web/cursor_team_setup.js`。那是装机安装器，`--apply` 会顺手
重打 app bundle、塞 6 个 cursor-g 模型、提示粘 Key，还有最要命的一条：`mergeConfig()` 里当选中模型名
不以 `cursor-g` 开头时，会把 composer / cmd-k 覆盖成 `cursor-g-5.6-sol`。而本机 composer 正是门①门②
的基准 `cursor-web-fc-82-terra`——拿安装器当开关用，会把基准模型悄悄换掉，而且不报任何错。
**切地址和装机是两件事，不该共用一个入口。**

`on` 默认**顺便开采集**：真实 body 落到 `tests/fixtures/`，上限 40 条 / 512MB，采够自己停手
（`CAPTURE_MAX=0 ./zk-delta/switch.sh on` 可以关掉）。

采集有两道门，都是被真事故逼出来的：**UA 必须是 Cursor**（否则 `on` 里那条 93 B 的自检 curl
也会被采进去——拿我自己造的样本让 ⑨ 转绿就是假绿），**且必须是有正文的聊天请求**
（否则 Cursor 启动时那发 `GET /v1/models` 会落成 0 字节文件，⑨ 一 `JSON.parse` 就崩）。
被 UA 挡掉的发数记在 `/metrics.json` 的 `capture_skipped_ua` 里。

封顶不是可选项：真实 body 每条几 MB、Cursor 每轮一发，不封顶跑一天能把本机磁盘写爆，
而金样几十条就够用。采集进度露在 `/metrics.json` 的 `capture_taken` / `capture_left` 里。

`tests/fixtures/*.json` 在 `.gitignore` 里——**采到的是你的真实会话正文和代码，绝不入库。**

## 还没做完的

- 服务端会话状态在内存里，pod 重启会全丢。丢了不出错，只是每条会话下一发退化成全量
  （`rollout restart` 之后照样答对、409 +1，已在韧性那组实测）。要扩副本必须先把 store 挪到 Redis。
- 网关侧 `responses.js` 的按形状瘦身还没做，那要走它自己的一轮门① 验证。
- 还没并进 `cursor_team_setup.js` 给全员发——目前只在本机验过。

## 已经不成立的旧说法

早期版本的这份 README 说过两句话，现在都不成立了，留在这里免得有人照着老版本操作：

- ~~「`tests/fixtures/` 是空的，⑨ 是红的」~~ —— 2026-08-31 用**真 Cursor GUI** 采到 22 条真 body，
  ⑨ 已转绿，`run.js` 现在是 **45 绿 0 红**。
- ~~「只差一条命令 `./zk-delta/switch.sh on`」~~ —— 那条命令当时**跑不通**：脚本用系统 node 去跑
  安装器，而安装器靠 `process.execPath` 推导 Cursor 安装目录，会去 Homebrew 的 node 前缀底下找
  Cursor 而报「找不到 Cursor 资源目录」；而且调用没带 `--apply`，就算路径对了也只是 dry-run。
  也就是说在此之前 `switch.sh on` 从来没真的切过地址。已改成 `cursor_baseurl.js`，验过 off/on 往返。
