# hermestest-14 workspace 报表脚本快照

生产真身在 188 `hermestest-14` 容器内 `/data/.openclaw/workspace/scripts/`，这里是**只读快照**，
用来留第二个来源 + 给 `scripts/hermestest-cron-audit.py --sha` 判漂移。
改生产后必须把新版本拷回这里（流程见 skill `hermestest-cron-report-triage` §4）。

| 文件 | sha256 前 12 | 说明 |
|---|---|---|
| `litellm-adjusted-stats.sh` | `0710b85255a8` | cron `claude-code-daily-spend-report` 的入口 |
| `litellm-company-daily-actual-report.js` | `c7cbbb8bb502` | 2026-10-01 改 1 天一块取 `/spend/logs`（原版 `fef27741ef52`，容器里留 `.bak-20261001`） |
