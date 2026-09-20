---
name: litellm-198-key-block
description: >-
  在 198 前置 nginx 上按 **API key** 封禁滥用流量，以及只知道 LiteLLM UI 上那串 key hash
  时怎么捞出 key 明文 / 真实客户端 IP / UA。含 probe→block→verify→stats 闭环、
  为什么按 IP 封在 198 上结构性不可行（TCP 源恒为前置代理、共享出口 IP 误伤、IP 会轮换）、
  以及三把量具（tcpdump snaplen、zkreq 第6列区分自返/上游、统计窗口去污染）。
  Use when the user mentions 某个 key/hash 一直在打服务器、删了 key 还在请求、
  把某个 IP 加黑名单、429/401 刷屏、Codex Desktop 死循环、封禁误伤了别人。
---

# 198 按 key 封禁滥用流量

> 作用域：**只有 198**（`cc.auto-link.com.cn` 那层 nginx，ns `litellm-product` 之前）。
> 阿里云那套没有这层前置 nginx，这套 SOP 搬不过去。
>
> 一切动作都走 `scripts/litellm-198-key-block.sh`，**别手改 conf** ——
> 手改的规则脚本的 `list` / `unblock` 管不到，会和 managed 块并存。

## 0. 先认清流量路径（决定了能封什么）

```
客户端 → 443 网关 58.241.5.230 → 内网前置代理 10.68.13.97 → 198:80 nginx → litellm
```

两个直接推论：

- **198 看到的 TCP 源地址恒为 `10.68.13.97`**。真实客户端 IP 只活在
  `X-Real-IP` / `X-Forwarded-For` 头里。
- **443 在网关就终结了，回源到 198 是明文 HTTP** —— 所以头能直接 tcpdump 读出来。
  这是「只有 hash 怎么拿明文 key」的唯一出路。

## 1. 为什么不按 IP 封（三个坑，2026-09-07 全踩过）

| 做法 | 结果 |
|---|---|
| iptables / ufw 按客户端 IP 封 | **空转** —— 那个 IP 在 198 上根本不出现 |
| iptables 封 `10.68.13.97` | 掐断**全部**用户 |
| nginx 按 `$http_x_forwarded_for` 封 | 可行，但下面两条会咬人 |

1. **用户大量共用一个出口 IP**（共享 NAT）。09-07 按 IP 封了 33 分钟，
   误伤了 749 次别人的请求 —— 用户当场反馈"误伤了很多"。
2. **IP 会轮换**。同一台 Codex Desktop 一小时内 `58.242.232.152` → `220.180.208.237`
   （中间约 5 分钟静默）。IP 规则此时**双向失效**：放过真凶，还在封无辜的旧 IP。

⇒ **按 key 封，与来源 IP 无关。** 这也是唯一一个「精确到滥用者本人」的维度。

## 2. 闭环

```bash
S=scripts/litellm-198-key-block.sh

# 1) 只有 hash 时先抓包：拿 key 明文 + 真源 IP + UA + URI
$S probe --seconds 30 --hash <sha256>

# 2) 封（备份 → 插 managed 块 → nginx -t → reload → 自检 403）
$S block sk-xxxx --reason "Codex Desktop 0.137 死循环打 /pro/v1/responses/compact"

# 3) 回归（四/五格 + litellm 侧该 hash 归零）
$S verify sk-xxxx --control-key <同出口IP的某个活key>

# 4) 观察（逐分钟 + UA 分布）
$S stats --minutes 15 --since 13:25

$S list            # 看装了哪些块 + 有没有脚本管不到的手工遗留
$S unblock sk-xxxx # 撤
```

规则本体（`block` 生成，插在 `server_name` 之后）：

```nginx
if ($http_authorization ~ "<key 明文>") { return 403 "blocked: revoked api key\n"; }
if ($arg_api_key = "<key 明文>")        { return 403 "blocked: revoked api key\n"; }
```

- **两条都要**：有客户端把 key 塞在 query 里（`?api_key=`）。
- **nginx 算不了 sha256**，只能匹配明文；hash↔明文的对应必须用一次抓包坐实，别猜
  （LiteLLM 的 key hash = 明文的裸 sha256，可以**验证**候选 key，但反推不出来）。
- 改前 `cp -a` 备份到 `/root/nginx-backup-cc.conf.<ts>`；只 `nginx -s reload`，**禁 restart**。

### 删 key ≠ 客户端停手

被删的 key 打过来是 401，客户端只会**重试得更凶**（09-07：100 → 250~450 req/min）。
nginx 层 403 的收益是**不再读那 1MB body、不进 litellm、不写 SpendLogs**；
要让流量根本到不了 198，得在 443 网关侧封 —— 那不归我们控。

## 3. 三把量具

1. **tcpdump 的 `-s` 必须 ≥ 2000。** `-s 1400` 会把 `Authorization` 头截掉，
   跑出来是干干净净的「0 命中」，看着像"没人在用这把 key"。09-07 我据此宣布过一次"已经停了"，
   实际是 123 次/30s。
2. **403 是我拦的还是上游 litellm 回的：看 zkreq 第 6 列 `$upstream_response_time`。**
   nginx 自返写 `-`，上游回的写耗时。不切这一刀，上游 403 基线（≈28/小时）会被算成自己的误伤。
   ⚠️ 是 `$6=="-"` 不是 `$6==""` —— 写成后者所有 403 都会被判成上游的。
3. **统计窗口必须排掉自己上一版干预的时段**（见下节）。

`zkreq` 的 log_format **不含 IP 字段**，别指望从日志找源 IP，只能抓包。

## 4. 两条不许违反的读数纪律

**① 别把静默当收工。** per-minute 序列出现几分钟空档，多半是客户端退避或换 IP，不是它放弃了。
一次抓包 0 命中就宣布结束会翻车。要下"停了"的结论，至少隔几分钟重抓一次 + 看 stats 序列。

**② 统计窗口被自己上一版规则污染。** 09-07 我先按 IP 封了 33 分钟，
再看整份日志的「被拦 403 按 UA 分布」，出来一堆不同 UA，于是得出
**「这把死 key 被多台机器共用」—— 这是假的**，那些全是 IP 规则的误伤。
把窗口限到只有 key 规则生效的时段后，只剩**唯一一个 UA**。
⇒ `stats` 不带 `--since` 时会打印一条大字警告；要下"多机共用"这种结论必须带 `--since`。

顺带：**nginx graceful reload 后旧 worker 还会用旧配置服务一两分钟**，
所以撤掉规则后的 2 分钟内仍能看到旧规则的拦截，别当成"没撤干净"。

## 4.5 凭据会换的时候：按**形状**封，不按 hash 封

09-07 第二例：UI 上报 `401: LiteLLM Virtual Key expected. Received=eyJh****`，
`user_api_key = hashed-jwt-<sha256>`。这**不是** `sk-` key，是个 JWT。

查库先确认两件事，再决定封法：

```sql
-- ① 这类凭据会不会换？  73 个不同 token / 7 天 ⇒ 封单个 hash 永远追不上
select api_key, count(*), min("startTime"), max("startTime") from "LiteLLM_SpendLogs"
 where api_key like 'hashed-jwt%' group by 1 order by 2 desc;
-- ② 这一类有没有成功过？  162 行全 failure / 0 success ⇒ 整类封是零损失
select status, count(*), count(distinct api_key) from "LiteLLM_SpendLogs"
 where api_key like 'hashed-jwt%' group by 1;
```

配套确认 `config.yaml` 里没有 jwt 字样（`enable_jwt_auth` 没开）⇒ JWT 结构性不可能过鉴权。

**证伪腿（必须打，不打会打瘸管理 UI）**：只按「JWT + 路径」两条件封会误伤 LiteLLM 管理 UI ——
UI 前端包里**确实**有 `/v1/models`、`/v1/chat/completions`、`/v1/responses`（在 pod 里
`grep -rhoE '"/?(v1/[a-z_/]+)"' .../proxy/_experimental/out` 实测到）。所以要第三个条件把
浏览器和原生客户端分开：**浏览器一定发 `Sec-Fetch-*`，原生客户端一定不发。**

```bash
$S block-jwt                                   # 三条件 AND：JWT × /pro|dev|stg/v1/ × 非浏览器
$S verify-jwt --control-key <活key>            # 四格
$S unblock jwt-class                           # unblock 也吃标签，不只吃 sk- key
```

回归判据里**「同一个 JWT 加上浏览器头必须放行」这一格不能省** —— 它是 UI 没被误伤的唯一证据。
实测：403 / 401 / 200 / 200（原生 403、浏览器 401、`/pro/ui/` 200、活 key 200）。

顺带一条口径：这类流量是**你自己的用户配错了**（Codex Desktop 用 ChatGPT OAuth 登录、
没填 `sk-` key），57 次/天、0 token、0 成本。封掉只是去掉失败噪音，**不会让这些人能用**——
他们要的是去配 key。别把它当成洪水来汇报。

## 4.6 抓不到明文时：按 key **尾巴**封（临时刀）

09-07 第三例：hash `99cba050…e66c`，41 次挤在一分钟里然后停。这种**突发型**流量
`probe` 赶不上（我抓 25s 就是 0 命中），而 nginx 只能按明文匹配、LiteLLM 只存 sha256 ——
明文两头都拿不到。

出路：`LiteLLM_VerificationToken.key_name` 存成 `sk-...<尾4位>`，**尾巴是能从库里读到的**。

```bash
$S block-tail ybLw --reason "..."      # 带强制碰撞门
$S watch --hash <sha256> --minutes 45  # 后台守着，等下一次突发把明文记下来
$S unblock tail-ybLw                   # 抓到明文后 block <明文> 再撤这条
```

**碰撞门**：库里以该尾巴结尾的 key 必须**恰好 1 把**，否则脚本拒绝安装
（实测 `ybLw` 在全库 1866 把 key 里唯一）。安装后自检**阳性+阴性双对照**：
合成的 `sk-selftest0000ybLw` → 403，差一位的 `...ybLwz` → 非 403（防正则过宽）。

⚠️ 这是**临时刀**，因为**将来**新建的 key 仍有约 1/14.7M 的碰撞概率
（1866 把 key 量级下 ≈0.013%）。所以配 `watch`：抓到明文就按
「加 `block <明文>` → verify → 撤 `unblock tail-xxxx`」的顺序换成精确匹配，
**别 add 和 remove 同一步做**（[[feedback_rename_is_add_verify_cutover_then_remove]]）。

顺带一个诊断口径：这一例查库发现 key **还在** `LiteLLM_VerificationToken` 里，
但 `expires` 比请求时间早 9 分钟 —— 是**过期 key 在被重打**，不是被删的 key。
`team_id = litellm-dashboard` 说明它是 UI 上生成的短期 key。
查 hash 时**三张形状都要分**：①被删的 sk- key ②过期但仍在表里的 sk- key ③根本不是 sk- 的 JWT，
三种的封法完全不同。

## 5. 09-07 案例（判据都在这）

- 症状：LiteLLM UI 上 hash `1aefd5118e73…49aa1` 的 key 已删除，仍在狂刷 401。
- 抓包定性：`Codex Desktop/0.137.0-alpha.4 (Windows 10.0.22621; x86_64)` 死循环打
  `POST /pro/v1/responses/compact`，每次 ~1MB body，key 明文 `sk-y1HTYynFpbBVeGYUSlKG3g`。
- 处置：按 IP 封（12:47–13:20，误伤 749 次）→ 换成按 key 封（13:20 起）。
- 验收：死 key 403 / 伪造 XFF 仍 403 / `?api_key=` 403 / 无 key 401 / **同 IP 活 key 200**；
  4 个 litellm pod `--since=60s | grep -c <hash>` 全 0。
- 14:42 复抓：25s / 112 次，仍是**同一台机器**在打，全被挡在 nginx。

## 6. 相关

- 脚本：`scripts/litellm-198-key-block.sh`
- [[feedback_198_abuse_block_by_key_not_ip]]
- [[reference_198_direct_ssh]] — 进 198 的方式（direct ssh + jms 兜底）
- [[topic_ruler_failure_shapes]] — 量具会坏的形状，本轮贡献了第 13 条
- [[feedback_manifest_prod_drift_apply_overwrites]] — 198 禁 kubectl apply（本 skill 只碰 nginx）
- [[topic_litellm_ops_index]]
