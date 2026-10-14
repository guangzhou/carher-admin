---
name: cli-anything-myai
description: >-
  只读排查 myai.carher.net（通衢 API / ai_token_dashboard，跑在 243 K3s 的 ns `ai-token-dashboard`）：
  看健康、同步卡没卡、看板每天花费和上游对不对得上、哪些 key 不在用户表、刷新队列、实时结算、账单/outbox、临时只读 SQL。
  入口是 `scripts/myai-cli.sh`（默认带 --json）。
  适用场景：用户说「myai 看板数据不对/不更新」「lastSuccessAt 不动」「同步卡住」「通衢 API 健康检查 degraded」
  「myai 账单待处理」「查 myai 库」「243 上的 token dashboard」「看板少算钱/某天是 0/某天正好 10000」「有用户没同步过来」。
  只读；要部署、改配置、改代码走交接文档的发布流程，不在本 skill 范围。
---

# cli-anything-myai

## 系统是什么（2026-10-14 实测）

| | |
|---|---|
| 公网 | `https://myai.carher.net`（Cloudflare Tunnel `e22cc0ea…`，ingress 只这一条） |
| 主机 | 243 `cltx@10.68.13.243`（hostname `AIYJY-litellm-ps`），🔴 密码末尾的 `'` 也是密码的一部分 |
| 代码 | GitHub `davidzhu2021/ai_token_dashboard`（我们**没有推送权限**）。dashboard/realtime 跑 `2818c0d`；**sync worker 跑 `9f250f8`**（= `2818c0d` + 本地两个修复提交，补丁在 `work/myai-migration/patches/`）。243 上 `~/ai_token_dashboard` 在分支 `fix/log-scan-capped-window-split-v2` = 线上 sync worker |
| 镜像 | 本地导入，`imagePullPolicy: Never`；243 上没有 docker。dashboard/realtime = `ai-token-dashboard:latest`，sync worker = `ai-token-dashboard:fix-9f250f8` |
| 交接文档 | 飞书 `LnDFdifILo1Wuix7irecEbKOnkh` |

进程：`ai-token-dashboard`（FastAPI，`USAGE_SYNC_ROLE=reader`）、`usage-sync-worker`、
`usage-realtime-worker`（都是 `python -m backend.usage_worker`，靠 `USAGE_REALTIME_ENABLED` 区分），
外加 `usage-db-0`（pg16）、`usage-redis-0`、`mail-relay-0`、`cloudflared`×2。

两个数据源：`primary` = `LITELLM_BASE_URL`（`cc.auto-link.com.cn/pro`），`her` = `HER_LITELLM_BASE_URL`（`litellm.carher.net`）。

## 工具

```bash
scripts/myai-cli.sh install                     # 首次安装（uv tool editable，改代码即生效）
export MYAI_SSH_PASSWORD='...'                  # 经 sshpass -e 走环境变量，不进 argv；不设就用 ssh key
M=scripts/myai-cli.sh                           # 默认 --json；要人读格式设 MYAI_HUMAN=1
```

真身代码在 `tools/cli-anything-myai/`（按 CLI-Anything 方法搭建）。

## 排查顺序

```bash
$M status                      # 第 0 步：公网 /api/health 摘要，findings 列出每一项异常
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
| `status=degraded` | 子项任何一个 degraded 就会这样。organizations 403（LiteLLM 非 Enterprise）是**常驻**的，摘要里标成 `known` |
| `lastSuccessAt` 停在旧日期 | 只有 `status=ok` 的同步才推进它。**partial 照样发布通过门禁的数据源**（`publishedAt` 是新的），所以 primary 的数据并不旧 |
| `usage daily` 当天有数、`usage frontier` 却停在 10-10 | 看板读的是视图 `usage_query_daily`：当天实时数据就绪后会盖过历史快照。frontier 只说明历史快照发布到了哪天 |
| `queue` 里 attempts 几百 | 失败后退避重试（30s→900s），不会自己消失；门禁原因不消除就永远 pending |
| 子进程 rc=0 | 经 ssh 跑 psql 时不能只信 rc，CLI 会再扫 stderr 里有没有 `ERROR:` |
| `coverage` 偏差几个百分点 | 走日志扫描的日子按北京日界算，上游按 UTC 日界算，属正常；>10% 才算漏 |
| `coverage` 某天 shownSpend=0 但 realtime 有数 | **myai 视图 bug（未修）**：`usage_query_daily` 第三支只在 `usage_daily` 这天**一行都没有**时才露出实时数据；her 哪怕 1 行就把 primary 全挡住。10-13 实测：realtime 144,601 请求 / $27,832，看板 0。等这天被历史快照重新发布就好 |
| `upstream daily` 报 timed out waiting for the condition | 探针默认在 sync worker 里跑，worker 缩到 0 时打不到 ⇒ 用 `probe-pod up` + `--probe-pod` |

## sync-stuck 怎么判

`diagnose sync-stuck` 在 sync worker pod 里，用它自己的 key **重放门禁的原始量法**：
- 尝试人数：`/user/list` 按 `sort_by=user_id` 分页取 `carher-*` 账号
- 有用量：逐个账号逐天调 `/user/daily/activity/aggregated`，返回非空就算有
- 判定：有用量人数 < `max(1, int(尝试人数 × 0.05))` 就会被拦（门禁在 `backend/usage_sync.py` 约 1316 行；`her` 永远不走日志扫描，所以每次都过这道门禁）

10-13 实测：305 个账号，门槛 15 人，10-07~10-13 每天 0~1 人，全部会被拦。
**阳性对照**：`--end 2026-09-21 --days 2` 得到 161 人和 172 人，不拦。尺子两个方向都量得对。
⇒ 判定：her 上游从 09-28 起几乎没有流量，同步卡住是门禁按设计生效，**不是采集坏了**。her 的流量去了哪里，还没查。

修复方向（都要改生产，需要用户或项目负责人拍板）：her 源改指新网关、下掉 her 源，或者改门禁（全局请求本来就近 0 时，空结果算正常）。
只调 `USAGE_SYNC_FALLBACK_MIN_NONEMPTY_RATIO` 修不干净：`max(1, …)` 保证零流量的天仍会被拦。

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

## 发布（sync worker 热修复的走法）

GitHub 推不了，镜像在 188 构建（与 243 同为 x86_64）：
```bash
git archive --format=tgz -o /tmp/x.tgz <commit>                       # 只打包已提交内容
# 188: 解包到新目录 /Data/myai-build-<commit> → docker build -t ai-token-dashboard:fix-<commit> .
#      docker run 一遍新测试；docker save -o image.tar（🔴 别用管道 save|ssh import：docker 29 的 OCI 包 ctr 从 stdin 认不出）
# 243: scp image.tar → sudo k3s ctr -n k8s.io images import /tmp/image.tar（/ 只剩约 2.5G，导完删 tar）
kubectl -n ai-token-dashboard exec usage-db-0 -- pg_dump ... -t usage_daily -t usage_sync_coverage -t usage_team_membership_daily --data-only -Fc > /Data/migration/<备份>.dump
kubectl -n ai-token-dashboard set image deploy/usage-sync-worker worker=ai-token-dashboard:fix-<commit>
```
验收看 `diagnose coverage`、`sync runs`（无 TimeoutError）、worker 重启数。回滚：`set image … worker=ai-token-dashboard:latest`。
🔴 镜像里的文件要和 commit 逐个 sha 对过再 rollout（`git show <c>:<f> | shasum` vs `docker run … sha256sum`）。
🔴 改完 sync worker 别忘了把 243 上 `~/ai_token_dashboard` 也切到同一提交（用 `git bundle` 搬），否则又会和线上对不上。

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
