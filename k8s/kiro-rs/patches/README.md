# kiro.rs 本地补丁

上游 `hank9999/kiro.rs`。**槽位实例 `kiro-rs-slots` 镜像 `v2026.10.1-carher-slots24`（10-25，= 0001~0008 + 0010 + 0011）；老实例 `kiro-rs` 镜像 `kiro-rs:v2026.10.1-carher-ctxwin`（10-23 起，= 0001~0008 + 0010；0009 未入镜像）**；更早 `kiro-rs:v2026.10.1-carher-filter`（10-18 起）= 上游 v2026.10.1（master `22f36e69`，10-08）+ 下面 0001~0006 按文件名序打上。**
本目录是补丁的唯一真源；188 上的构建树只是产物，`scripts/kiro-rs-build.sh` 每次从干净上游重建。

```bash
scripts/kiro-admin-ui/verify.sh                             # 改了 admin-ui：本机编 + mock 回放 live 池子 + Playwright（先跑这个）
scripts/kiro-rs-build.sh 22f36e69 <镜像tag> --skip-build   # 只验：打补丁 + cargo test 对照 + 链式复现
scripts/kiro-rs-build.sh 22f36e69 <镜像tag>                # 再构建镜像 + tarball
scripts/kiro-rs-ship.sh  <镜像tag> <sha256>                 # 送进 242（不碰生产）
scripts/kiro-rs-e2e.sh   <镜像tag> --tarball <198上路径>    # 隔离容器真上游 13 例（只改 admin-ui 时可跳过，见 0004）
scripts/kiro-rs-ship.sh  <镜像tag> <sha256> --switch [--expect-ui <字面串>]  # 切生产（Recreate 停 ~10s）；改 UI 必带 --expect-ui
scripts/kiro-lane-regress.sh --rounds 2                     # 经 LiteLLM 回归全部 kiro-* 道
```

完整流程与坑见 skill `kiro-rs-ops` §C。

| 补丁 | 内容 | 首次上线 |
|---|---|---|
| `0001-carher-base-models.patch` | 9 个非 Claude 模型透传表、`kiro-`/`kiro/` 别名前缀、sonnet-5.5 映射 + 1M 窗口、`/v1/models` 11 条 | 09-12 / 10-04 |
| `0002-websearch-server-tool-loop.patch` | 服务端 web_search 搜索回合 | 10-15 `master-websearch-loop` |
| `0003-overage-limit-exceeded-switch-credential.patch` | 402 `OVERAGE_REQUEST_LIMIT_EXCEEDED` 也算额度用尽 ⇒ 禁号换号 | 10-15 `master-overage-fix` |
| `0004-admin-ui-sort-current-enabled-first.patch` | Admin UI（`:30890/admin`）列表：启用（在用置顶）→ 禁用，组内按 `lastUsedAt` 倒序（最近用完的在前，从未用的垫底）；只改前端，调度不变 | 10-15 `master-ui-sort`；10-16 改组内排序 `v2026.10.1-carher-sort2` |
| `0005-kimi-k3-passthrough.patch` | 透传表加 `kimi-k3`（1M 窗口）+ `/v1/models` 一条 + 单测（含 `kimi-k2.7-code` 阴性对照）。cargo test 235→236，E2E 13/13 | 10-16 `v2026.10.1-carher-kimi` |
| `0006-admin-ui-filters-and-card-header.patch` | Admin UI 查询栏（新文件 `credential-filters.tsx`）：关键字（邮箱子串 / `#ID`，空格分隔多词 OR）、状态（启用/当前/已禁用/按禁用原因）、登录方式、优先级、邮箱域名、只看有失败；下拉带计数。卡片标题栏修溢出（`#ID` 前缀、邮箱截断、开关不换行、徽章挪下一行）。只改前端：过滤在 0004 排序之后、分页之前；批量操作仍按勾选 ID，「清除已禁用」仍针对全部禁用号 | 10-18 `v2026.10.1-carher-filter` |
| `0010-context-overflow-anthropic-wording.patch` | 上游超长（`CONTENT_LENGTH_EXCEEDS_THRESHOLD` / `Input is too long`）⇒ 400 `prompt is too long: N tokens > M maximum`（Anthropic 原句，N≥M+1）。Claude Code 只认这句才自动压缩；LiteLLM 只认这句才映射 ContextWindowExceeded、不走普通兜底（修前被兜到 sa-grok-4.20 复读）。6 个单测；e2e 加「超长拒收」一例（新 14/14，旧 sticky 13/14 = 阴性对照） | 10-23 `v2026.10.1-carher-ctxwin`（= 0001~0008 + 0010，**不含 0009**） |
| `0011-slots-one-account-one-ip-user-binding.patch` | 槽位模式 `loadBalancingMode=slots`：N 个命名槽，每槽 1 号 + 1 固定出口；带身份（`x-litellm-user_api_key_hash`，只在本模式读、不发 Kiro 官方）的 key 绑槽；AIMD 并发 + 排队超时 529；429 官方退避；402/被限退役换冷备；槽运行时增删停用（按名字 reconcile，停用即换绑）；灰度名单；按号×模型×北京日用量 + 统计前基线；SIGTERM 优雅停机；haiku-5.5 映射；Admin 槽位面板/配置/测试/批量查额度。cargo test 321 | 10-25 `v2026.10.1-carher-slots25`（被限探测改中性问题+号自己的 ARN；slots24 用户原文探测会冤退役好号）（**只给槽位实例**）。运维见 skill `kiro-rs-slots` |
| `0013-opus55-refusal-switch-model.patch` | 槽位：opus-5.5 安全审查静默空回复（号没被限）⇒ 同槽改用 `slotRefusalFallbackModel`（默认 opus-4.8）重答 + 末尾提示，同会话 6h 粘住；4.8 也空回明确提示。cargo test 352 | 10-27 `v2026.10.1-carher-slots43`（只给槽位实例） |

存档：`0001-carher-base-models.5ca5703.patch` 是旧基线版 0001（含 opus-5.5，上游后来自带了），`kiro-rs-build.sh` 不会打它。

## 10-15 升到 v2026.10.1

上游比旧基线 `5ca5703`（07-27）多 6 个提交，对我们有用的只有一条：**`tool_result` 里的 image 块不再被静默丢掉**
（读图片文件、截屏类工具）。同号同请求 A/B：旧版两次都答「工具只返回了文字 screenshot captured」，新版答对绿/蓝。
另有 fable-5.1（不是每个号都有：#138 PRO+ 目录里没有 ⇒ 400 Invalid model；198 侧没开道）和 opus-5.5（与 0001 重合）。
两条分支（master、`refactor/v2`）都没改 Admin UI 样式。

**0001 冲突与重做**：上游自己加了 opus-5.5，0001 有三处打不上。按新上游重做，只保留上游没有的部分；
移植脚本 `port_0001_to_v2026.10.1.py`（每处替换断言命中 1 次 + 断言每个 `fn test_` 上一行是 `#[test]`）。
🔴 后一条是真踩的：从 patch 里按 fn 行截取新增块会漏掉上一行的 `#[test]`，cargo 不报错只是不跑（漏了 2 个）。
0002~0004 原样能打。

验证：
- cargo test 上游 210 → 打补丁 235，0 失败，无「never used」测试函数；漏 `#[test]` 的阴性对照 `kiro-rs-build.sh` rc=1。
- 链式复现：干净上游 + 本目录 0001~0004 ⇒ 与构建树逐字相同。
- 隔离 E2E `kiro-rs-e2e.sh` 新版 11/11；旧版 `master-websearch-loop` 9/11（缺 tool_result 图片、models 31 条）= 阴性对照成立。
- 上线后 `kiro-lane-regress.sh --rounds 2` 40/40，in_tok 全在基线 ±5% 内。

## 回滚链（都在 242 本地 containerd）

`set image deploy/kiro-rs kiro-rs=docker.io/library/kiro-rs:<tag> seed-config=同`，每次切换前的 deploy 在 `198:~/kiro-rs-deploy.pre-<新tag>.json`。

| 镜像 | 内容 | 备份 |
|---|---|---|
| `v2026.10.1-carher-filter`（现役，10-18） | 上游 v2026.10.1 + 0001~0006（0006 查询栏 + 卡片标题栏） | — |
| `v2026.10.1-carher-sort2` | 上游 v2026.10.1 + 0001~0005（0004 改组内按 lastUsedAt 倒序） | `pre-v2026.10.1-carher-filter.json` |
| `v2026.10.1-carher-kimi` | 上游 v2026.10.1 + 0001~0005（0004 旧排序） | `pre-v2026.10.1-carher-sort2.json` |
| `v2026.10.1-carher` | 上游 v2026.10.1 + 0001(重做)~0004 | `pre-v2026.10.1-carher-kimi.json` |
| `master-ui-sort` | 5ca5703 + 0001~0004 | `pre-v2026.10.1.json` |
| `master-overage-fix` | 5ca5703 + 0001~0003 | `pre-ui-sort.json` |
| `master-websearch-loop` | 5ca5703 + 0001~0002 | `pre-overage-fix.json` |
| `master-sonnet55` | 5ca5703 + 0001 | `pre-websearch-loop.json` |

## 0002 做了什么

请求带 Anthropic 服务端搜索工具（`web_search_20250305` 等）时，Kiro 原样转发会
400 `Invalid tool use format`，LiteLLM 静默兜底到 grok / wangsu（grok 走 Messages 不搜还假装搜过）。

- `converter.rs`：服务端搜索工具改写成带 `{query}` schema 的普通函数工具；
  历史里客户端回显的 `server_tool_use` / `web_search_tool_result` 拍平成文本（srvtoolu id 不进 Kiro 历史）。
- `search_loop.rs`（新）：整轮缓冲的搜索回合。模型调 web_search ⇒ Kiro MCP 搜 ⇒ tool_result 交回 ⇒ 续调，
  最多 max_uses（缺省 5、硬上限 10）次搜索 / 8 轮。客户端只看到原生 `server_tool_use` + `web_search_tool_result`。
  流式先发 message_start，等待期间 25s ping，最后一次性发完。第一轮上游错误仍以 HTTP 状态码返回（LiteLLM 照常兜底）。
- `websearch.rs`：老路径（Claude Code 的 `Perform a web search for the query:` 子请求）保留；
  修 `stream=false` 也回 SSE 的 bug（改回 JSON）。
- `handlers.rs`：`/v1/messages` 与 `/cc/v1/messages` 都先判搜索回合，再判老路径。

验证（旧基线时）：cargo test 239 = 224+15 新增通过；隔离容器 E2E 9 例全过；生产经 LiteLLM 4 例 `attempted-fallbacks: 0`。

## 0003 做了什么

开了 overage 的号用满 2000 后，上游对每个请求都回 `402 OVERAGE_REQUEST_LIMIT_EXCEEDED`
（不是 `MONTHLY_REQUEST_COUNT`）。kiro.rs 只认后者，结果把它当普通 4xx 原样返回、不换号。
10-15 00:44~02:11 UTC 卡在 #128 上 87 分钟，期间 opus 全部兜底到 wangsu / grok。

改动：`src/kiro/endpoint/mod.rs` 的 `default_is_monthly_request_limit` 认这两个 reason
（加 2 条单测，含 4 个别的 reason 的阴性对照）。日志文案同步改了。

验证：隔离 A/B（死号 #128 优先、好号 #131 其次）：旧镜像 2/2 失败，currentId 停在 128；
新镜像 2/2 成功，#128 → `QuotaExceeded` 禁用并切到 #131。

## 0004 做了什么

后端 `GET /api/admin/credentials` 按 priority 升序返回，前端直接按 12 个一页切。禁用的老号 priority 是 0/1/2，
于是 126 个禁用号占满前十几页。改 `admin-ui/src/components/dashboard.tsx`，分页前排序（只改显示，后端选号不变）：

1. **启用在前、禁用在后**；启用组里「当前在用」置顶。
2. **组内按 `lastUsedAt` 倒序**（用户 10-16 定的「最近用完的排前面」）；从未用过的（`null`）垫组尾；同时间保持后端 priority 序（sort 稳定）。

为什么「最近用完」直接用 `lastUsedAt`：额度用尽禁号（`QuotaExceeded`）、refresh 失效禁号（`InvalidRefreshToken`）
时 `token_manager.rs` 都会把 `last_used_at` 写成禁号那一刻 ⇒ 对禁用组，「最后使用」就是「用完/被禁的时间」，不需要新字段。
`Manual` 禁用不改这个字段，读到的是禁用前最后一次成功。

`lastUsedAt` 是 9 位小数的 RFC3339。补丁里先截到毫秒再 `Date.parse`，属防御写法：V8 实测 9 位能解析，
**Safari/Firefox 没测过**，别把注释里那句当已验证的事实。解析失败按「从未使用」处理，不会抛错。

上线记录：
- 10-15 `master-ui-sort`：在用 → 启用 → 禁用，组内后端序。池子切前切后 133/7/currentId=138 一致。
- 10-16 `v2026.10.1-carher-sort2`：改成上面的规则。上线前拿 live `/credentials` 在 node 里跑同一个比较函数，
  第 1 页 = 在用 #151 + 10 个启用号 + 今天刚 `QuotaExceeded` 的 #150；cargo test 210→236 / 0 失败；
  切前切后 149/11/currentId=151 一致；线上 bundle 含新排序代码；`kiro-lane-regress.sh --rounds 2` 42/42。
  只改 admin-ui ⇒ 没跑 `kiro-rs-e2e.sh`（13 例全是 API 行为，与这处改动无交集）。

🔴 补丁头要写 `a/admin-ui/...`、`b/admin-ui/...`：最早的 0004 头是 188 上的 `kiro-ws/` `kiro-ui/` 目录名加时间戳，
`patch -p1` 恰好能打，但重新生成时照抄就错。重做用 `diff -u --label a/<路径> --label b/<路径>`。

## 0006 做了什么

用户 10-18：「界面太丑，加几个查询项，千万别影响主功能」。新文件 `admin-ui/src/components/credential-filters.tsx`
（纯函数 `filterCredentials` + 查询栏组件），`dashboard.tsx` 接入，`credential-card.tsx` 改标题栏。

- 查询项：关键字（邮箱子串 / `#ID` 精确，空格或逗号分隔多词 = OR）· 状态（启用 / 当前在用 / 已禁用 / 按 `disabledReason` 细分）·
  登录方式 · 优先级 · 邮箱域名 · 只看有失败（`failureCount` 或 `refreshFailureCount` > 0）。下拉选项与计数从 live 数据现算，不写死枚举。
- 位置：0004 排序之后、分页之前。改条件回第 1 页；30s 自动刷新后结果变少、页码越界时拉回末页。
- **不影响主功能的边界**：批量操作仍按勾选 ID；「清除已禁用」仍针对全部 `data.credentials` 里的禁用号（不是筛选结果）；
  顶部三张统计卡仍是全量。后端、调度零改动。
- 卡片标题栏：原来长邮箱把「启用」开关挤出卡片 ⇒ `#ID` 前缀 + 邮箱单行截断（`title` 悬停看全）+ 开关 `shrink-0` + 徽章挪第二行可折行。

上线记录（10-18 `v2026.10.1-carher-filter`）：
- `scripts/kiro-admin-ui/verify.sh` 用 live 176 号快照 29/29（每个条件条数 = Python 独立算的期望）；mock 收到写请求 0；
  阴性对照：挪走 0006 再跑 ⇒ STOP。
- cargo test 210 → 236 / 0 失败；链式复现逐字相同。切前切后 178/27/currentId=166 一致。
- 线上 bundle sha == 本地 dist sha；`没有符合条件的凭据` HIT、随机串 MISS；`kiro-lane-regress.sh --rounds 2` 42/42。
- 回归报 gpt5.6 三道 in_tok 429→465（+8%）：**10-17 19:10 那轮（旧镜像）已是 465**，与本次切换无关；原因未查，基线未改。

