---
name: cli-anything-myai
description: >-
  排查和发布 myai（通衢 API / ai_token_dashboard，243 K3s ns `ai-token-dashboard`）。唯一入口 chat.auto-link.com.cn/myai/
  （myai.carher.net 10-29 已下线）。入口部署 `scripts/myai-chat-subpath/deploy.sh`，代码发布 `scripts/myai-release.sh`，
  198 master key 漂移自动同步 `scripts/myai-198-key-sync.sh`。「myai 打不开/很慢/health degraded/realtime STALE」也走这里：
  看健康、同步卡没卡、看板每天花费和上游对不对得上、哪些 key 不在用户表、刷新队列、实时结算、账单/outbox、临时只读 SQL。
  入口是 `scripts/myai-cli.sh`（默认带 --json）。
  适用场景：用户说「myai 看板数据不对/不更新」「lastSuccessAt 不动」「同步卡住」「通衢 API 健康检查 degraded」
  「myai 账单待处理」「查 myai 库」「243 上的 token dashboard」「看板少算钱/某天是 0/某天正好 10000」「有用户没同步过来」
  「198 换了 master key」「稳定性看板不更新」「改 myai 代码上线」。CLI 只读；发布/改配置按 §发布 走脚本。
---

# cli-anything-myai

## 系统是什么（2026-10-29 实测）

| | |
|---|---|
| 公网 | 🔴 **2026-10-29 起只有一个入口 `https://chat.auto-link.com.cn/myai/`**（国内直连，细节见「§国内入口」）。`myai.carher.net`（Cloudflare Tunnel `e22cc0ea…`，国内常被分到 CF LAX 超时）已下线：`deploy/ai-token-dashboard` 和 `deploy/cloudflared` 缩到 0，共享的 `APP_BASE_URL` 已改成 chat 入口，Casdoor 删了老回调。恢复材料 `/Data/migration/myai-retire-carher-net-20261029T0707/` |
| 主机 | 243 `cltx@10.68.13.243`（hostname `AIYJY-litellm-ps`），🔴 密码末尾的 `'` 也是密码的一部分 |
| 代码 | GitHub `davidzhu2021/ai_token_dashboard`（我们**没有推送权限**）。线上 = `2818c0d` + 本地补丁（`tools/cli-anything-myai/upstream-fixes/0001~`，见那里的 README）。本地仓库 `.work/myai-admin-perf/ai_token_dashboard`，243 上 `~/ai_token_dashboard` 与线上同一提交 |
| 镜像 | 本地导入，`imagePullPolicy: Never`，名字 `ai-token-dashboard:perf-<commit>`，4 个 deploy（chat dashboard、0 副本的主 dashboard、realtime、sync worker）统一用同一个。🔴 **现役版本只认现量**：`kubectl -n ai-token-dashboard get deploy -o custom-columns=N:.metadata.name,IMG:.spec.template.spec.containers[0].image`（这里写死的版本号每次发布都会过时）；每次发布前的版本在 `/Data/migration/images-pre-perf-*.txt` |
| 交接文档 | 飞书 `LnDFdifILo1Wuix7irecEbKOnkh` |
| 磁盘 | 根盘只有 38G，k3s 数据在 `/Data/rancher`。**myai 报 1033/502、pod Pending 或 ErrImageNeverPull，先走 skill `host-243-disk-ops`**（`scripts/host-243-disk-check.sh`）。`latest` 现在是从 `2818c0d` 重建的（`985f9288`，依赖按 9f250f8 锁定）；10-10 原版已经丢了 |

在跑的：`ai-token-dashboard-chat`（FastAPI，`USAGE_SYNC_ROLE=reader`，NodePort 30810，**10-29 起也跑 live totals 预取**）、`usage-sync-worker`、
`usage-realtime-worker`（都是 `python -m backend.usage_worker`，靠 `USAGE_REALTIME_ENABLED` 区分），
外加 `usage-db-0`（pg16）、`usage-redis-0`（2GB maxmemory）、`mail-relay-0`。
**0 副本**（10-29 下线 myai.carher.net）：`ai-token-dashboard`、`cloudflared`。`host-243-disk-check.sh` 会在它们被意外拉起时报 warn。

共享配置 `dashboard-config`(CM) + `dashboard-secrets`：`APP_BASE_URL=https://chat.auto-link.com.cn/myai`（10-29 起，邮件/密码重置/邀请/支付回调链接都用它）、`LITELLM_ADMIN_KEY`（=198 现役 master key，见 §198 key）。

两个数据源：`primary` = `LITELLM_BASE_URL`（`cc.auto-link.com.cn/pro`），`her` = `HER_LITELLM_BASE_URL`（`litellm.carher.net`）。

## 入口 chat.auto-link.com.cn/myai/（10-19 上线，10-29 起唯一）

用户说「myai 打不开 / 很慢」：先跑 `scripts/host-243-disk-check.sh` 排除 243，再测 `https://chat.auto-link.com.cn/myai/api/health`。
🔴 **页面能开 ≠ 数据在更新**：10-28 页面 200/0.2s，但 198 用量已经 403 了 8 小时。用户说「看板不对」先看 health 里 `usageRealtime`。
myai.carher.net 已下线（CF 530），别再让人去用；恢复材料在 243 `/Data/migration/myai-retire-carher-net-20261029T0707/`（deploys/configmaps/secret 备份）+ 198 `~/casdoor-ai-token-dashboard.pre-retire-carher-net.*.json`。

链路：IT 入口 58.241.5.230 → 198 nginx `chat.auto-link.com.cn.conf` 里的 `# >>> myai-subpath >>>` 段 → 243 NodePort **30810** → `deploy/ai-token-dashboard-chat`（`k8s/myai-243-chat-subpath.yaml`）。

```bash
HOST243_PW=... scripts/myai-chat-subpath/deploy.sh apply     # 243 + Casdoor 回调 + nginx 段 + 验收（各步幂等）
scripts/myai-chat-subpath/deploy.sh nginx                     # 只更新 nginx 段（改了 nginx-block.conf 之后）
HOST243_PW=... scripts/myai-chat-subpath/deploy.sh verify     # 16 项外网验收，含 4 个旁路服务 + 老入口确实是 0 副本
HOST243_PW=... scripts/myai-chat-subpath/deploy.sh rollback  # 🔴 唯一入口，回滚=全站不可用；要设 I_KNOW_THIS_IS_THE_ONLY_ENTRY=1
```

**为什么 chat 是单独一个 Deployment**（读 2818c0d 源码确认）：`enforce_csrf` 拿 Origin 和 `APP_BASE_URL` 严格比 scheme+netloc；`sso_start` 用 `APP_BASE_URL + /api/auth/callback` 作为回调 ⇒ 一个进程只认一个域名。manifest `k8s/myai-243-chat-subpath.yaml`。
🔴 manifest 里的 `image:` 必须是线上那个 `perf-<commit>`（10-29 前写的是 `:latest`，apply 一次就会打回 10-10 的无补丁版本）。改前先 `kubectl diff -f` 确认无差异。

**nginx 段做了什么（全在 `scripts/myai-chat-subpath/nginx-block.conf`）**
- `sub_filter` 改写前端写死的根路径：`"/api` `` `/api `` `src="/assets/` → 加 `/myai` 前缀。上游要发 `Accept-Encoding: ""` 才能改写，对外的 gzip 由 198 nginx 再做。2818c0d 实测改写 86 处、残留 0。**前端升级后验收里「86」这一项会变，要重新数一遍**
- `proxy_redirect ~^/(.*)$ https://chat.auto-link.com.cn/myai/$1`：后端登录完回 `"/?auth_callback=success"`。🔴 替换串必须写全 https 域名，写成 `/myai/$1` 时 nginx 会自己补成 `http://...`，而对外 80 不通 ⇒ 扫码登录完落到空页（10-19 用桩上游测出来的）。SSO 的绝对跳转（飞书 / dash）不匹配这条，会原样放行
- `proxy_cookie_path / /myai/`：session cookie 只在 /myai 下发送，不和 Open WebUI 的混在一起
- `location = /myai` 用 `absolute_redirect off`：IT 回源时 Host 会被改写，绝对跳转会带错域名（见记忆 `feedback_198_gateway_host_rewrite_redirect_ip_fixed`）

**登录**：Casdoor `admin/ai-token-dashboard` 的 redirectUris 现在 3 条：`ai-usage.auto-link.com.cn`、`127.0.0.1:8010`、`chat.auto-link.com.cn/myai`（myai.carher.net 那条 10-29 删了）。飞书后台不用改：飞书回调的是 Casdoor（`dash.auto-link.com.cn/callback`）。
🔴 验 SSO 回调**不能只 grep Location**：`OIDC_SKIP_CASDOOR_PAGE=true` 时 Location 直接是飞书，myai 自己的 redirect_uri 藏在飞书 `state`（base64）和 session cookie 的 `_state_*` 里。verify 两处都解开比对，并且拿原入口做对照。
没验到的：**真人扫码登录完整走一遍**（需要人）。

## 工具

```bash
scripts/myai-cli.sh install                     # 首次安装（uv tool editable，改代码即生效）
export MYAI_SSH_PASSWORD='...'                  # 经 sshpass -e 走环境变量，不进 argv；不设就用 ssh key
M=scripts/myai-cli.sh                           # 默认 --json；要人读格式设 MYAI_HUMAN=1
```

真身代码在 `tools/cli-anything-myai/`（按 CLI-Anything 方法搭建）。

## 排查顺序

```bash
$M status                      # 第 0 步：公网 /api/health 摘要，findings 列出每一项异常（默认打唯一入口 chat.auto-link.com.cn/myai）
$M diagnose sync-stuck         # lastSuccessAt 不动时先跑这个（默认 7 天，约 90s）
$M diagnose coverage           # 🔴 每天 看板花费 vs 上游全局花费，偏差 >1% 给出原因提示（漏数据的总入口）
$M diagnose truncation         # 看板某天请求数正好 10000：上游计数封顶截断
$M upstream keys --days 7      # 哪些 key 有花费、owner 在不在 LiteLLM_UserTable（不在 = 按人同步看不到）
$M sync errors                 # 高频 error_message
$M sync runs --days 7          # 每天 ok/partial/failed 计数
$M queue                       # 刷新队列：attempts 几百 = 永远过不了
$M usage frontier              # 历史快照最新到哪天
$M usage daily --days 7        # 看板实际读到的每日用量（usage_query_daily）
$M realtime                    # 实时结算滞后
$M billing orders | billing outbox
$M upstream daily --backend her --days 14   # 上游自己报的每日请求/花费
$M probe-pod up / --probe-pod … / probe-pod down   # 上游探针改到隔离 pod 跑（见下）
$M identity find <id|名字|邮箱>
$M logs sync --since 1h --grep quality
$M db tables | db sql "SELECT ..."
```

不带子命令运行进入 REPL。

## 读结果别读错

| 现象 | 实际含义 |
|---|---|
| `status=degraded` | 子项任何一个 degraded 就会这样。10-29 起正常应为 `ok`；逐项看 §health 判据 |
| `organization.status=unavailable` + `knownLimitation` | 198 没有 Enterprise 许可，`/organization/list` 永远 403。**不拉低整体**（补丁 0014）；只在 teams/keys 正常、唯一失败是许可 403 时才这么标，key 失效那种三项全 403 仍是 degraded |
| `organization.settlement` 差异 55 笔/$2.4 万 | 🔴 **不是对账错，是组织日结从没跑过**：`settle_usage_rows` 只在 `USAGE_SYNC_ROLE=combined` 的 `run_usage_sync` 里调，生产拆成 reader+worker 后就没人调（`customer_usage_settlement` 0 行）。涉及北汽计费，**要用户拍板才能补**，CLI 标 `known` |
| `lastSuccessAt` 停在旧日期 | 只有 `status=ok` 的同步才推进它。**partial 照样发布通过门禁的数据源**（`publishedAt` 是新的），所以 primary 的数据并不旧 |
| `usage daily` 当天有数、`usage frontier` 却停在 10-10 | 看板读的是视图 `usage_query_daily`：当天实时数据就绪后会盖过历史快照。frontier 只说明历史快照发布到了哪天 |
| `queue` 里 attempts 几百 | 失败后退避重试（30s→900s），不会自己消失；门禁原因不消除就永远 pending |
| `usageRefreshQueue.oldestAgeSeconds` 几千秒但 pending=0 | 只是正在跑的那条（running 也算进 oldest），不是积压 |
| `usageRealtime` 在 ok/degraded 之间跳 | 10-29 前阈值 120s 落在正常区间里（误报）；补丁 0016 改 180s 后应稳定 ok。仍跳就看 §realtime 卡顿 |
| `settlementLagSeconds` 突然 4 万秒 | 10-29 前每次 realtime worker 重启都会这样（水位被打回 0 点，几分钟追回）；补丁 0012 已修。修后再出现 = 真卡住 |
| 子进程 rc=0 | 经 ssh 跑 psql 时不能只信 rc，CLI 会再扫 stderr 里有没有 `ERROR:` |
| `coverage` 偏差几个百分点 | 走日志扫描的日子按北京日界算，上游按 UTC 日界算，属正常；>10% 才算漏 |
| `coverage` 某天 shownSpend=0 但 realtime 有数 | **myai 视图 bug（未修）**：`usage_query_daily` 第三支只在 `usage_daily` 这天**一行都没有**时才露出实时数据；her 哪怕 1 行就把 primary 全挡住。10-13 实测：realtime 144,601 请求 / $27,832，看板 0。等这天被历史快照重新发布就好 |
| `upstream daily` 报 timed out waiting for the condition | 探针默认在 sync worker 里跑，worker 缩到 0 时打不到 ⇒ 用 `probe-pod up` + `--probe-pod` |

## health 判据（10-29 实测正常值）

| 子项 | 正常 | 异常时先看 |
|---|---|---|
| `usageRealtime` | ok，`maxSettlementLagSeconds` 60~140，`latestEventLagSeconds` <20 | worker 日志 `REALTIME_POLL_FAILED` 的 HTTP 码（403=key，见 §198 key） |
| `usageSync` | `idle`/`running`/`skipped`，`lastError` 空 | `sync runs` 有没有连续 `TimeoutError`（见 §发布合并超时） |
| `usageRefreshQueue` | pending 0 | `queue` 看 attempts 和 last_error |
| `organization` | `unavailable` + `knownLimitation`（常驻） | 别的状态 = 真问题 |
| `billing` | ok，pendingSyncCount 0 | `billing orders` |

**10-28/29 修过的几种「卡住」（补丁 0009~0018，都已上线）**，再出现时的形状：
- **realtime 每 5 分钟停摆约 100s**：目录刷新 + 15 分钟回扫在主循环里同步跑 ⇒ 移后台（0010）。量法：日志里相邻两次 primary live 轮询（窗口 50~80s 的 `/spend/logs/v2`）间隔 >20s
- **结算滞后常年 90~200s**：结算排在 cost/backfill 轮转里约 60s 才轮一次 ⇒ 每轮单独拉起（0011）
- **当天快照每轮 `TimeoutError`（约 880s 白跑）**：整天 20 万行 `usage_event_attribution` 合并 43~74s 撞池 `command_timeout=30s` ⇒ 合并单独 600s（0009，`USAGE_PUBLISH_MERGE_TIMEOUT_SECONDS`）。量法：只读回滚事务里重放 upsert 计时
- **稳定性看板不更新**（`observability_dashboard_snapshots` stability 行 `updated_at` 停住）：一周 66 万条尝试去重 16~59s 撞 30s ⇒ 独立连接 + `work_mem=256MB`（11s）+ 110s（0017）
- **Redis 淘汰**（`evicted_keys` 增长）：`usage:realtime:event:*` 无人读取却占 1.1GB ⇒ 不再写（0018）。旧 key 3 天 TTL 自然过期
- **her 某天被「非空用户过少」挡几百次**：owner 为空的 key（carher-1 等）按人回退查不到 ⇒ her 也走 key 补漏（0013）+ 门禁按**花费**覆盖率 ≥99% 放行（0015；不带 key 的鉴权失败花费 0、归属不了，按请求数比永远过不了）

## 198 key（`LITELLM_ADMIN_KEY`）

198 master key 轮换后 myai 不会报错页，只是 realtime 全 403、看板停更（10-28 停了 8h 没人发现）。
```bash
HOST243_PW=... scripts/myai-198-key-sync.sh check   # sha256 前 12 位对比，不改；漂移 exit 1
HOST243_PW=... scripts/myai-198-key-sync.sh apply   # 243 实打新 key 200 → 等同步安全窗口 → 备份 → 写 → 逐个重启 3 个 deploy → 验 403=0
```
本机 launchd `com.carher.myai-key-sync` 每小时跑 `apply`（`scripts/install-myai-key-sync-launchd.sh` 装；243 密码在钥匙串 `carher-host243`；日志 `.tmp/myai-key-sync/launchd.log`）。198 侧完整下游清单见记忆 `reference_198_master_key_rotation_downstream_checklist`。

## sync-stuck 怎么判

`diagnose sync-stuck` 在 sync worker pod 里，用它自己的 key **重放门禁的原始量法**：
- 尝试人数：`/user/list` 按 `sort_by=user_id` 分页取 `carher-*` 账号
- 有用量：逐个账号逐天调 `/user/daily/activity/aggregated`，返回非空就算有
- 判定：有用量人数 < `max(1, int(尝试人数 × 0.05))` 就会被拦（门禁在 `backend/usage_sync.py` 约 1316 行；`her` 永远不走日志扫描，所以每次都过这道门禁）

10-13 实测：305 个账号，门槛 15 人，10-07~10-13 每天 0~1 人，全部会被拦。
**阳性对照**：`--end 2026-09-21 --days 2` 得到 161 人和 172 人，不拦。尺子两个方向都量得对。
⇒ 判定：her 上游从 09-28 起几乎没有流量，同步卡住是门禁按设计生效，**不是采集坏了**。her 的流量去了哪里，还没查。

✅ **10-20 已修（补丁 0006，线上已跑）**：门禁触发时再查一次上游全局 `/user/daily/activity/aggregated` 的 metadata，
全局请求 ≤ 每天 `USAGE_SYNC_SPARSE_MAX_DAILY_REQUESTS`（默认 100）就放行；设 0 恢复旧行为。日志：
`sparse fallback upstream check backend=her ... upstream_requests=21 ... confirmed=True`。
（只调 `USAGE_SYNC_FALLBACK_MIN_NONEMPTY_RATIO` 修不干净：`max(1, …)` 保证零流量的天仍会被拦。）

**刷新队列另外两个坑（也已修）**：
- 只按 `requested_at` 领取 ⇒ 重试上千次的老请求永远排在最前，新日期 attempts=0 从没被领过 ⇒ 补丁 0005 改成 `ORDER BY attempts, requested_at`
- 历史日期的刷新每次都先陪跑「今天」的稳定性扫描（2000+ 页，约 23 分钟）⇒ 撞 `USAGE_REFRESH_TASK_TIMEOUT_SECONDS=1800` 被杀 ⇒ 补丁 0007 让 `end_date < 今天` 时跳过（日志 `stability sync skipped for historical refresh`）
- 读队列别读错：`last_error=sync failed`、`duration_ms` 只有几毫秒 = 拿不到同步锁被 skipped（`usage_sync_runs.status=skipped`、`已有同步任务正在运行`），不是真失败

## 看板少算钱：三种已知形状（10-13/10-14 实测，均已修进 `9f250f8`）

`diagnose coverage` 每天给 hint；三种形状的根因和修法：

| 形状 | 根因 | 修法（`backend/`） |
|---|---|---|
| 某天请求数**正好 10000** | LiteLLM ≥1.93（PR #31825）`/spend/logs/v2` 计数封顶：`total=10000`、`total_is_capped=true`，`total_pages` 只是下限；没有游标，也去不掉每行约 96KB 的 metadata。日志扫描读满 100 页就停还判完整 | `sync_rows_from_logs`：`total_is_capped` 时按时间二分，直到每个子窗口计数精确再读全；子窗口去重后行数 ≠ 精确计数就判不完整。实测 10-08 分 17 段、122,272 行一行不差 |
| 某天**整天是 0** | 198 原始 SpendLogs 是短保留表（10 天 cronjob，盘满时直接 TRUNCATE，10-14 发生过）；被清空的日子扫出 0 行，当成"没流量"发布，把已有快照删成 0 | 扫描结果 < 日汇总 `LiteLLM_DailyUserSpend` 的 50%（且日汇总 ≥100 请求）就判不完整，回退 daily activity |
| 每天都**稳定少 5~20%** | 按人回退只查 `LiteLLM_UserTable`；key 的 owner 为空（`carher-hangzhou`）或不在用户表（`claude-code-*-std`、`carher-75`…）的花费永远进不来。30 天约 $58k（7.7%） | daily activity 路径末尾用**不带 user_id** 的 `/user/daily/activity` 取 `models[m].api_key_breakdown`，只补 owner 不在本次用户集合的 key；`key:<alias>` 单列（carher-hangzhou 可见）；`attribution_source=key_rollup`、`billing_eligible=false`（只显示，计费仍走 key→组织映射）。开关 `USAGE_SYNC_KEY_ROLLUP_ENABLED` |

另有发布前对账：每次同步把花费和上游全局比一次，偏差 >1% 记 `usage coverage gap … gap_pct=`（WARNING）。
修后实测：单日、7 天窗口与上游 spend 偏差 **0.00%**。没被新代码重算过的日子 `coverage` 会提示"没有 key_rollup 行"。

⛔ **试过、失败、回滚的做法**（别重做）：封顶时一直 OFFSET 翻到短页。单日 1250 页约 5GB/13 分钟，叠加 her/稳定性/身份刷新超过 `USAGE_REFRESH_TASK_TIMEOUT_SECONDS=1800`，10-13 晚 18 个任务连续被杀。

## 探针在哪跑

- 默认在 `deploy/usage-sync-worker` 里跑（有 key，免配置），但那是生产进程：**10-14 一次探针把 12 万行原始记录留在内存里，worker 被 OOM 杀（137）**，3 秒后自动恢复。
- 重的探针（扫原始日志、跑整段 `collect_backend`）一律放隔离 pod：
  ```bash
  $M probe-pod up --memory 2Gi --ttl 3600     # 同镜像、同 CM/Secret、自带内存上限，到点自己退出
  $M --probe-pod upstream daily ...           # 或 MYAI_PROBE_TARGET=pod/myai-probe
  $M probe-pod down
  ```
- 要在 pod 里跑**改过的**本地代码：`tar czf p.tgz backend` → `kubectl cp` 到 `/tmp/patched` → `sys.path.insert(0, "/tmp/patched")`，用 `UsageSynchronizer(client, NoWriteStore())` 调 `collect_backend` 做不落库的干跑（10-14 用这个验证了三个修复）。

## 发布（`scripts/myai-release.sh`）

GitHub 推不了。本地仓库 `.work/myai-admin-perf/ai_token_dashboard`（分支 `perf/admin-board-latency`，从 243 的 `~/ai_token_dashboard` 用 `git bundle` 拉来）。
改完、提交，然后：

```bash
HOST243_PW=... scripts/myai-release.sh build     # 本地测试门禁 → 188 构建（pip 锁 9f250f8 的 freeze）→ 源码 sha 对提交 → 导入 243 + pin
HOST243_PW=... scripts/myai-release.sh deploy    # 等同步安全窗口 → 记录现镜像 + 备份 4 张表 → 金丝雀(chat) → realtime → sync，逐个等就绪、间隔 30~40s（0 副本的主看板只换镜像）
HOST243_PW=... scripts/myai-release.sh rollback  # 按 /Data/migration/images-pre-perf-<commit>.txt 逐个换回
```
- **测试门禁比的是失败集合**：ai_token_dashboard 改前就有 15 个失败（前端/静态资源断言），基线在 `.work/myai-admin-perf/baseline.txt`。不能要求全绿，只要求不新增
- 🔴 **worker 不能同时 rollout**：启动时 `apply_usage_schema` 会互相死锁（10-20 第一次发布时各重启 1~2 次）。补丁 0008 让锁冲突改为等锁重试；脚本仍然逐个换
- 🔴 **同步发布阶段别换 pod**：同步末尾的合并在长事务里拿着锁，新 pod 建表等锁 → liveness 杀（10-29 chat 金丝雀重启 1 次）。`deploy` 默认先跑 `scripts/myai-wait-sync-idle.sh`：判据是「最新一轮不在跑，或刚开始 <480s」—— 同步首尾相接（上一轮结束同一秒下一轮开始），**等「没有 running」永远等不到**。手动换 pod（`rollout restart`）前也先跑它
- 🔴 判状态别 grep CLI 的表格：`grep '^ *1 *$'` 对不上列对齐，等于没判（10-29 就是这样撞上同步的）。读 `MYAI_HUMAN=` 的 JSON 按字段取
- 🔴 `docker save | ssh ctr import -` 管道不行（docker 29 的 OCI 包 ctr 从 stdin 认不出），脚本是落 tar 再导入
- 🔴 tar 留在 `/Data/migration` 别删：没 pin 的镜像会被 image GC 删、拉不回（见 skill `host-243-disk-ops`）
- 发布后把 243 的 `~/ai_token_dashboard` 切到同一提交（`git bundle` 搬过去），否则下次又对不上
- 改补丁后重新导出：`git format-patch 9f250f8..perf/admin-board-latency --start-number 3 -o tools/cli-anything-myai/upstream-fixes/`；`git am` 必须 `--keep-cr`。中文提交标题会被 format-patch 吃成 `0011-.patch`，导出后手动改个英文名
- 🔴 **`backend/main.py` 是 CRLF/LF 混合**：`git stash` / `stash pop` 会把整个文件改成另一种行尾（diff 显示 2.8 万行）。要在旧代码上跑测试证明「改前失败」，用 `cp f /tmp/new; git show HEAD:f > f; pytest …; cp /tmp/new f`，**别 stash**。提交前 `git diff --stat` 行数对不上就是踩了
- 往 243 传要执行的脚本用 `scp` + `sha256sum` 比对再跑：`sshpass … ssh host 'cat > f' < local` 10-29 实测落了 0 字节，后面 `bash f` 静默什么都不做
- 验收看：pod 重启数、`diagnose coverage`、`sync runs`（无 TimeoutError）、`logs sync --grep "sparse fallback|stability sync skipped"`；health 连续采样（`for i in $(seq 60)` 每 6s 一次）要全 `ok`，单次 ok 不算
- 每个修复要有「改前失败、改后通过」的测试，并配一个**阴性对照**（例：许可 403 不降级，但 key 失效三项全 403 仍降级；花费覆盖率放行，但 $0.14/$21.91 仍挡）

## 已知未处理（10-29，需要拍板或改 198 生产）

- **198 网关 observability pusher 一直打不通**：`deploy/litellm-proxy` 的 env `OBSERVABILITY_INGEST_URL=http://10.68.13.188:8000/...`（188 已无服务，ConnectError，每 pod 每天几百次 warning）。数据没丢：`stability_attempt_events` 每天十几万行来自 sync worker 自己扫日志（`event_type=final_request`）；pusher 推的逐次尝试（success/failure）**10-08 起就没再进来**。要恢复：env 改成 `http://10.68.13.243:30810/api/internal/observability/events`（10-29 从 litellm pod 签名实测：签名/HMAC 对得上，只差非空 events）。属于改 198 生产网关 env，要按灰度流程走
- **组织日结从没跑过**（health `organization.settlement` 差异 55 天 / $2.4 万）：见 §读结果别读错。涉及北汽计费
- `usage_query_daily` 视图「当天只要有 1 行 her 就挡住整天实时数据」（10-13 记的）没修

## 只读保证

- SQL 全部在 `default_transaction_read_only=on` 事务里跑（E2E 用 `SELECT … FOR UPDATE` 实测会被服务端拒绝）；`db sql` 在本地还会先拦一道
- 上游探针只发 GET，脚本是包内固定模板；`diagnose sync-stuck` 每天约 300 个 GET，并发 4（与 worker 默认值相同）
- 唯一会创建/删除资源的是 `probe-pod up/down`（只动 `pod/myai-probe` 这一个名字）；没有 rollout、apply 业务资源

## 测试

```bash
cd tools/cli-anything-myai
uv run --with click --with prompt-toolkit --with pytest python -m pytest cli_anything/myai/tests/test_core.py -q
MYAI_SSH_PASSWORD=... CLI_ANYTHING_FORCE_INSTALLED=1 uv run --with click --with prompt-toolkit --with pytest python -m pytest cli_anything/myai/tests/ -v
```
