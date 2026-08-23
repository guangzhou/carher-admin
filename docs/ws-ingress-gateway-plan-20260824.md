# WS ingress 网关（#24）—— 目标与作业计划

> 定位：设计+执行文档，与 `docs/gateway-compaction-harness-plan-20260823.md`（#25）同族。
> 背景见 `docs/acct-multiaccount-incremental-transport-goal.md`：主目标的"增量传输"我们已
> 完成 **pod→上游** 腿（Phase 2 全池，增量轮省 90.3%）；本项补最后一条腿——**客户端→网关**。
> 原型代码与 10/10 离线测试在 `scripts/ws-ingress-gateway/`。撰于 2026-08-24。

---

## 一、复述真实目标

你要的不是"网关支持一下 WebSocket"，而是：

1. **把端到端增量闭环补完**：现在客户端每轮仍把全量历史（均值 328KB、极端 18MB）上传到
   网关——这条上行腿的量级与已省掉的出网同阶（≈13GB/天）。补完后全链路每轮网络上
   只有"新增的那句话"。【事实：SpendLogs 实测请求体分布】
2. **把体感红利给到用户**：上行从几百 KB/轮 → 几 KB/轮，慢网络/远程用户每轮启动直接变快。
   这是 #25 网关压缩给不了的（压缩发生在收到之后，K6 已纠正过这个口径）。
3. **继续照抄官方机制**：codex ≥0.118 客户端原生支持对 provider 走 responses-over-WS
   （你本地 0.147 已就绪，配置一行 `supports_websockets=true`）；我们做协议的服务端。
4. **老规矩一条不少**：计费/路由/换号全不动（内部仍转 HTTP 打外层 litellm）；非 WS 客户端
   零感知；WS 任何异常客户端**协议内建**回落 HTTP 全量（天然兜底）；灰度=用户侧配置开关
   （谁改配置谁用，天然按人灰度）。

## 二、已就绪的（Phase A，事实）

- 原型 `app.py`：终结客户端 WS → 按连接账本重建全量 → 内部 HTTP → SSE 逐帧转 WS。
- 协议红线已落：prewarm（generate:false）**本地合成**绝不转发（CLIProxyAPI #1901 教训）；
  store/stream 转 HTTP 时补写；收据纪律；非 completed 终止→账本清空。
- 10/10 离线测试（pod 内真 aiohttp）：prewarm 零转发 / 两轮增量重建（1 delta→4 full）/
  prev 不匹配按全量 / 上游 429→error 帧 / 405 回落触发 / 401。

## 三、缺少的关键信息（诚实清单）

| # | 缺什么 | 影响与获取方式 |
|---|---|---|
| K1 | **codex 对自定义 provider 的真实 WS 线形**：URL 路径、握手头（带不带 OpenAI-Beta？）、帧字段与我们按"codex→OpenAI 上游线形"写的原型有无出入 | 决定性；S1 用你本地 codex 实测捕获 |
| K2 | 0.147 对自定义 provider 是否默认启用 v2/prewarm，还是需 `--enable=responses_websockets_v2` / features 配置 | S1 一并实测；社区帖提示需 features 开关【推测】 |
| K3 | 握手时 Authorization 是否带 env_key Bearer（我们靠它透传内部计费身份） | S1 实测；源码显示 headers 按 provider 构造【推测→验】 |
| K4 | **我们自己的 WS 收包上限**：aiohttp 服务端 `max_msg_size` 默认 4MiB——巨会话 T1 全量（>4MB）会被我们自己掐断 | 已知机制【事实】；S2 设为 16MiB 与上游对齐，超限=客户端自动回落 HTTP（与今天行为一致） |
| K5 | canary 的网络通路：你本地 codex → 我们集群内服务，走 ssh 隧道（先）还是 cc.auto-link.com.cn 的 nginx 加 WS upgrade 路由（后） | S4 决策；198 nginx 是实体文件有既往纪律【事实】 |
| K6 | zstd：codex 对自定义 provider 发不发压缩请求体 | 源码显示 zstd 仅限 ChatGPT 登录后端【事实→复核】；不带则无需处理 |
| K7 | 外层改写响应 id（responses_id_security）后，客户端回带的 prev_id 与我们账本键是否一致 | 设计上一致（我们存的就是转给客户端的那个 id）【逻辑必然】；S1 实测确认 |
| K8 | 并发/内存边界：每连接账本上限、连接数上限、TTL | S2 定值（参照 ws_transport 的 32/600s 经验） |

## 四、最小可执行步骤（不跳步）

### S1. 真实客户端线形捕获与对齐（决定性一步）
- 输入：Phase A 原型；你本地 codex 0.147；ssh 隧道（Mac→198→集群 svc 或本机直跑原型）
- 动作：本地起原型（LITELLM_URL 指向 198 外层）；codex 配 `supports_websockets=true` +
  provider base_url 指原型；跑 2-3 轮真实对话；原型加 verbose 捕获（握手头/每帧全文）
- 预期输出：真实线形记录（K1/K2/K3/K6 全部落地）；原型按差异修正
- 验收：真实 codex 连续 3 轮经 WS 完成、答案正常、日志显示 T2/T3 为 delta-only 重建

### S2. 硬化（对照 ws_transport 的既有纪律）
- 输入：S1 修正后的原型
- 动作：`max_msg_size=16MiB`（K4）；账本上限+连接 TTL（K8，参照 32/600s）；结构化观测行
  `ws_ingress turn mode= in= full= dt=`；异常演练测试补齐（掐上游/掐客户端/超限帧/并发双连接）
- 预期输出：硬化版 + 演练测试全过
- 验收：新增演练全绿；超限帧路径=客户端无感回落 HTTP（与今天行为逐字节一致）

### S3. 集群部署（canary 形态）
- 输入：硬化版；198 registry 烘镜像纪律（本仓 skill 已沉淀）
- 动作：烘镜像（FROM 含 aiohttp 的现有 base 或 python-slim+aiohttp，推 198 registry）；
  Deployment(1 副本)+svc 落 litellm-product；LITELLM_URL 指内部 litellm-proxy svc
- 预期输出：集群内可访问的 ws-ingress svc
- 验收：集群内 curl 405/healthz 正常；离线测试同款探针经 svc 全过

### S4. canary 通路（先隧道后正门）
- 输入：S3 svc；你的本地环境
- 动作：第一步 ssh 隧道（Mac:port→svc:8799）你本地 codex 指隧道；稳定后第二步在
  198 nginx（实体文件纪律）为 cc.auto-link.com.cn 增加 WS upgrade 分流到 svc
  （仅 Upgrade 请求走新路，POST 原路不动）
- 预期输出：你日常使用的 codex 无缝走上 WS ingress
- 验收：连续一个工作时段正常使用；停掉 svc 时 codex 自动回落 HTTP 无感【协议内建，实测确认】

### S5. 收益量化与灰度判定
- 输入：S4 稳定运行 ≥半天；服务观测日志
- 动作：统计 `in=`(delta) vs `full=` 字节比；对照 SpendLogs 同会话前后上行体积；
  quantify 每轮上行降幅与端到端延迟变化
- 预期输出：ingress 腿的实测收益数字（对齐主目标书"看实发内容"的验证纪律）
- 验收：delta-only 轮占比 ≥60%（同 WS incremental 闸线）；零新增错误类
- 之后扩灰度=通知更多 codex 用户改一行配置（用户侧开关，天然按人灰度、随时个体回退）

### S6.（可选）nginx 正门全量开放决策
- 输入：S5 数据 + 多用户反馈
- 动作：决策是否文档化推广给全部 codex 系用户
- 验收：交你拍板

## 五、事实 / 推测标注

- **事实**：上行量级与请求体分布（SpendLogs 实测）；codex ≥0.118 WS-first 行为与 0.147
  本地版本；prewarm 红线（社区 #1901 + 官方源码注释）；aiohttp 服务端 4MiB 默认收包上限；
  上游 16MiB 上限（自测）；Phase A 原型 10/10；198 nginx 实体文件纪律；zstd 仅限
  ChatGPT 后端（官方源码 client.rs:1434 区域，待 S1 复核）。
- **逻辑必然**：账本键与客户端 prev_id 一致性（我们存的就是发给客户端的 id）；
  停 svc → codex 回落 HTTP（协议内建，仍需 S4 实测确认以闭环）。
- **推测（待 S1 实测）**：codex 对自定义 provider 的确切握手头/帧形；v2 是否需显式
  features 开关；Authorization 透传形态；delta-only 命中率 ≥60% 的预期值。
