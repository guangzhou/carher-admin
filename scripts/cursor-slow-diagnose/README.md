# cursor-slow-diagnose —— 「同事说 Cursor 比 Claude 慢」的量具

10-28~29 查「Cursor 慢」时用来**排除假设**的几把尺子。结论与查法见 skill
`cursor-client-bundle-patch` 的「同事说 Cursor 比 Claude 慢 / 没反应」一节 —— 先看那里,
**最常见的根因是补丁被 Cursor 自动升级冲掉**,用 DOCTOR 和
`scripts/zk-cursor-web/cursor_summary_storm_scan.py` 就够,用不到下面这些。

| 文件 | 在哪跑 | 量什么 | 10-28 读数 |
|---|---|---|---|
| `ttft_ab.py <model> [轮数] [pad_kb]` | 本机 | 同一把 key、同一模型,Cursor 线型(/chat/completions)vs Claude 线型(/messages)交替打,比首字/总时长 | 两种线型同速,300KB 上下文也一样 ⇒ 排除「线型慢」 |
| `spans.py <requestTraces.log>...` | 本机(同事机也行) | Cursor 自带的 `cursor.requestTraces.log` 里每个 span 的 p50/p90 | 本地准备 p50 0.14s、hooks <60ms ⇒ 排除「客户端本地慢」 |
| `slot_latency.sh [小时]` | 198:`ssh … 'bash -s -- 6' < slot_latency.sh` | 按「key 当前绑在哪个 kiro 槽」× 客户端分组比延迟 | 同槽两客户端同速;慢集中在个别槽 —— **用户否掉了这条,别当结论** |
| `slots_map.sh <别名>...` / `slot_users.sh <槽名>` | 198 | 各槽状态 / 某些 key 在哪个槽 / 某槽上有谁 | — |

规矩(都是 10-28~29 踩出来的):
- 🔴 **不许用 osascript 模拟键盘/点击做测量**:Cursor 没拿到焦点时按键打进了用户另一个终端的会话。
- 🔴 SpendLogs `startTime` 是 naive UTC,北京时间用 `+ interval '8 hours'`,别用 `at time zone`(往回减)。
- 🔴 198 上 DB secret 会被轮换改名(`litellm-secrets` → `-r2`),脚本不许写死名字;能用
  `tools/cli-anything-litellm` 的 `remote198.psql()`(在 db pod 里用 `$POSTGRES_USER`,只读事务)就用它。
