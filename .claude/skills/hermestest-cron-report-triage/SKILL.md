---
name: hermestest-cron-report-triage
description: 排查/修复 188 上 hermestest-<N>（openclaw docker 实例）的定时任务，重点是「LiteLLM 全员每日实际消费」08:00 日报（弋天国现单聊群）。含 cron 存储位置（sqlite 不是 jobs.json）、为什么 cron 显示 ok 却数据不对、198 /spend/logs 30s 截断 500、改脚本的备份/回滚/验收、体检量具。Use when 用户说「hermestest-14 的定时任务异常」「每日消费日报数字不对/少了」「群里日报没发/发错」「cron 一直 ok 但结果不对」，或要改 hermestest workspace/scripts 下的报表脚本。
---

# hermestest 定时任务排查（以全员每日消费日报为例）

适用：**188（`10.68.13.188`）上的 docker 实例** `hermestest-<N>`，引擎 `openclaw`
（`docker exec <C> cat /data/.engine/active`）。不是 K8s 那套 carher 实例。
登录一律直连：`SSHPASS=<188 cltx 密码> sshpass -e ssh cltx@10.68.13.188`（密码见记忆 `reference_188_direct_ssh_and_nas_layout`）。

## 0. 先跑量具

```bash
export SSHPASS=...
scripts/hermestest-cron-audit.py                       # 默认 job=claude-code-daily-spend-report，看最新 1 次
scripts/hermestest-cron-audit.py --runs 5 --spendlogs --sha
scripts/hermestest-cron-audit.py --list                # 所有 enabled job 的状态/连续错误数
scripts/hermestest-cron-audit.py --job litellm-spend-sync
scripts/hermestest-cron-audit.py --job nope            # 阳性对照：必须 EXIT=3
```

| 层 | 查什么 | 判红意味着 |
|---|---|---|
| L0 | sqlite 能读、job 存在 | **尺子坏了**，别解读下面 |
| L1 | enabled / next_run 在 36h 内 / 没卡 running | 调度层坏 |
| L2 | cron 状态 + transcript 里工具输出的 `failed/Error/500/timeout` | **cron=ok 但这层红 = 部分失败被吞** |
| L3 | 198pro、aliyun 的 `/spend/logs` 逐日实打耗时 | 某天 >25s 或非 200 ⇒ 下一次日报会再缺数据 |
| L4 | 容器脚本 sha vs `scripts/hermestest-cron/` 快照 | 生产被改了（或我改完没回仓） |

退出码 `0` 绿 / `1` 红 / `3` 尺子坏 / `4` ssh 失败。
⚠️ 手动跑脚本**不进** `cron_run_logs`，修完后 L2 要等下一次真 cron 才会变绿。

## 1. 东西在哪

| 东西 | 位置（容器内） |
|---|---|
| cron 定义 + 运行日志 | `/data/.openclaw/state/openclaw.sqlite` 表 `cron_jobs` / `cron_run_logs`。`cron/jobs.json` 已 `.migrated`，**改它没用** |
| 每次运行的对话 | `/data/.openclaw/agents/main/sessions/<session_id>.jsonl`（session_id 在 `cron_run_logs`） |
| 日报入口 | `workspace/scripts/litellm-adjusted-stats.sh daily --chat=<oc_>` → `litellm-company-daily-actual-report.js` |
| 主密钥 | `workspace/.env.litellm.198pro` / `.env.litellm.aliyun`（`LITELLM_MASTER_KEY=`，别回显） |
| 快照 | `workspace/data/spend-snapshots/logs-<UTC日>.json`，`apiTotalSpend: 0` 就是那天没取到 |

容器里**没有 sqlite3**，用 `node -e` + `require("node:sqlite")`（`readOnly:true`）。
`openclaw` CLI 要 `docker exec -e HOME=/data`，且 `openclaw cron list` 会挂死，别用。
宿主 `data-home` 目录 cltx 没权限读，一律走 `docker exec` / `docker cp`。

## 2. 为什么 cron 显示 ok 但数据错了

cron 只判 agent 最后回了 `done`；agent 只看 stdout 有没有 `status=sent`。
而日报脚本对**单个数据源**失败只 `console.error` 一行、`fail++`，照样发卡片、exit 0。
⇒ **cron 的 ok ≠ 数据全**。判数据全只认 transcript 里的
`daily logs 198pro: N day rows, M active` 这一行（L2 就是看这个）。

## 3. 已知故障：198pro `/spend/logs` 窗口太大返 500（2026-09-29 起）

- 形状：`daily logs 198pro failed: 500: {"error":{"message":"/spend/logs Error"...}}`，
  卡片金额只剩 aliyun 一小部分。
- 实测：7 天一块 `09-23→09-30` 30.0s 后 500；同段 1 天一块各 3~17s 全 200 ⇒ 服务端约 30s 截断。
- 修法（已上，sha `c7cbbb8b…`）：块大小改 1 天（`LITELLM_SPEND_LOGS_CHUNK_DAYS` 可调），
  拉取失败时卡片首行加 `⚠️ 数据源拉取失败：<env> 未计入`。
- 198 SpendLogs 有 **10 天 retention**，所以每次总共最多 ~11 个有数据的日，耗时有上界。
  ⇒ 卡片里「新口径累计（自 2026-05-20）」**实际只是近 10 天累计**，标签不准（未改）。

## 4. 改脚本的流程

1. **备份**：`docker exec <C> cp -p scripts/<f> scripts/<f>.bak-<YYYYMMDD>`，记下 sha256。
2. `docker cp` 拉到本地改 → `node --check` → 推回去。
3. **先 dry-run**（不发群，但会覆盖快照文件）：
   `bash scripts/litellm-adjusted-stats.sh daily --dry-run --date <UTC日>`
   看 `daily logs 198pro: ... active` 那行 + 合计金额。
4. 需要补发再去掉 `--dry-run`，带 `--chat=<oc_>`。
5. **回仓**：把生产版本拷到 `scripts/hermestest-cron/`，跑 `--sha` 确认 L4 绿。
6. 回滚：`docker exec <C> cp -p scripts/<f>.bak-<日期> scripts/<f>`。

workspace 是个 git 仓（要 `-c safe.directory=*`），`daily-evolve` / `weekly-evolve`
会自动 commit 备份，未提交的改动会被收进去；只有有人按 tag `git checkout evolve-*` -- scripts/ 回滚才会冲掉。

## 5. 其他 job 的「连续 error」

`--list` 里 `errs` 很高的不一定真坏：例如 `litellm-spend-sync` 连续 16 次 error，
transcript 里脚本 `exit 0`、`updated 1369/1369`，报错只是 agent 收尾
「Agent couldn't generate a response」。**判真坏先读 transcript 里工具的退出码**。
