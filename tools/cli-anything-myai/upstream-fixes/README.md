# ai_token_dashboard 上游修复（未进 GitHub）

`davidzhu2021/ai_token_dashboard` 我们没有推送权限。这里是 243 sync worker 正在跑的
`ai-token-dashboard:fix-9f250f8` 相对线上 `2818c0d` 的两个提交，等有权限或交给项目负责人合并：

| 文件 | 内容 |
|---|---|
| `0001-*.patch` | 日志扫描：上游计数封顶时按时间二分读全；原始日志被清空（扫描 < 日汇总 50%）时判不完整，不再用 0 覆盖快照 |
| `0002-*.patch` | 按 key 补齐用户表之外的用量（`key:<alias>` 单列、只显示不计费）+ 发布前与上游总花费对账 |
| `DESIGN-log-scan-cap.md` / `DESIGN-key-coverage.md` | 两个修复的设计与实测数据 |
| `litellm_spendlog_cap_report.md` | LiteLLM 10000 计数封顶的来源调研（PR #31825 等） |

应用：`git am tools/cli-anything-myai/upstream-fixes/000*.patch`（基于 `2818c0d`；对 master `b13f58c` 两个文件无冲突）。
发布走法见 skill `cli-anything-myai` §发布。
