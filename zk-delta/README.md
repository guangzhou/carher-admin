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
# 离线金样（不需要任何凭据，起假上游）
node zk-delta/tests/run.js

# 真流量多轮（打真模型真号池）
ZKD_KEY=sk-xxx node zk-delta/tests/live_multiturn.js cursor-web-fc-82-terra 16 100

# 门② 真功能对照：真 agent 循环，基准腿 vs zk-delta 腿
ZKD_KEY=sk-xxx node zk-delta/tests/live_agentloop.js
ZKD_KEY=sk-xxx ZKD_TASK=lark ZKD_ALLOW_LARK=1 node zk-delta/tests/live_agentloop.js
```

## 部署

```bash
ZKD_SSH_PASS=... ./zk-delta/k8s/apply.sh --dry-run
ZKD_SSH_PASS=... ./zk-delta/k8s/apply.sh
```

刻意不建新镜像：代码是纯 Node 标准库，复用节点上已有的 `zerokey-codex` 镜像，
源码用 ConfigMap 挂进去，`imagePullPolicy: Never`。既不用在构建服务器出镜像，也不碰公网仓库。

`replicas: 1` + `strategy: Recreate` 是刻意的：会话状态在进程内存里，多副本会让 handle 认不出而狂 409。
要扩副本必须先把 store 挪到 Redis，那是另一件事。

## 本机切换

```bash
./zk-delta/switch.sh on       # 切到 zk-delta（装常驻小代理 + 改 Cursor BYOK 地址）
./zk-delta/switch.sh off      # 一条命令切回今天的地址
./zk-delta/switch.sh status   # 看现在指到哪、省了多少
```

`on`/`off` 都要求 Cursor 完全退出——外部写 `state.vscdb` 有内存覆盖竞态，Cursor 开着改了也会被它写回去。

## 还没做完的

- `tests/fixtures/` 是空的，所以 `run.js` 的第 ⑨ 项（真实抓包金样）是红的。
  切到 zk-delta 之后，`ZKD_CAPTURE=<目录>` 会自动攒真实 body，回灌重跑就能转绿。
- 服务端会话状态在内存里，pod 重启会全丢。丢了不出错，只是每条会话下一发退化成全量。
