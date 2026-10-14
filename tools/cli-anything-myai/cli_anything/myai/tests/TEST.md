# cli-anything-myai 测试计划

## 清单

- `test_core.py`:约 25 个单测,纯本地,不连网
- `test_full_e2e.py`:约 8 个,装好的命令走子进程(`_resolve_cli`)+ 打真 243 的只读探针

## 单测(test_core.py)

| 模块 | 测什么 |
|---|---|
| `core/remote.py` | ssh argv 里不出现密码;有密码走 `sshpass -e`,无密码带 `BatchMode=yes`;psql 命令带 `default_transaction_read_only=on`;stderr 含 `ERROR:` 即使 rc=0 也抛错;`parse_csv` 空输出 → `[]` |
| `core/queries.py` | `validate_adhoc` 拒 INSERT/UPDATE/DELETE/多语句/`\` 元命令,放行 SELECT/WITH/EXPLAIN;字面量单引号转义;天数/条数夹在范围内 |
| `core/diagnose.py` | 门禁公式与源码一致(305 人 × 0.05 = 15;<20 人不拦;0 人天也拦);`gate_report` 的 blockedDays/verdict |
| `core/health.py` | organizations 403 判为 `known`;lastSuccessAt 超 6h、refresh 最老超 1h、outbox failed、对账差异各出一条 finding;健康体无 finding |
| `core/upstream.py` | 未知 backend 报错;`day_list` 顺序与边界;`parse_script_output` 跳过前置告警行 |
| CLI(CliRunner + 假 Remote) | `db sql` 写语句在本地被拒且不发远端;`--json` 输出可解析 |

## E2E(test_full_e2e.py)

真后端 = 243 上的生产命名空间,全部只读。需要 `MYAI_SSH_PASSWORD` 或 ssh key;缺了直接失败,不跳过。

| 场景 | 链路 | 验证 |
|---|---|---|
| 健康 | `status --json` | JSON 有 `status`/`findings`,workerId 以 `usage-sync-worker-` 开头(证明流量落 243) |
| 只读护栏 | `db sql "SELECT 1"` 成功;服务端对 `SELECT ... FOR UPDATE` 之类写锁拒绝 | 证明 read-only 事务真生效,不只是本地校验 |
| 同步链 | `sync state` → `sync runs --days 3` → `queue` | 行结构字段齐全 |
| 卡住诊断 | `diagnose sync-stuck --days 7 --json` | attemptedUsers>0,每天带 need/blocked,verdict 非空 |
| 上游 | `upstream daily --backend her --days 3 --json` | 3 天,每天有 status=200 |
| 子进程 | `--help`、`--version` | rc=0 |

## 测试结果(2026-10-13,`CLI_ANYTHING_FORCE_INSTALLED=1`,真 243)

```
cli_anything/myai/tests/test_core.py::test_ssh_argv_password_never_in_argv PASSED [  2%]
cli_anything/myai/tests/test_core.py::test_ssh_argv_key_mode_is_batch PASSED [  5%]
cli_anything/myai/tests/test_core.py::test_psql_command_forces_read_only PASSED [  7%]
cli_anything/myai/tests/test_core.py::test_remote_error_on_psql_error_even_when_rc_zero PASSED [ 10%]
cli_anything/myai/tests/test_core.py::test_remote_password_goes_through_env PASSED [ 13%]
cli_anything/myai/tests/test_core.py::test_parse_csv_empty PASSED        [ 15%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_allows_reads[SELECT 1] PASSED [ 18%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_allows_reads[with a as (select 1) select * from a] PASSED [ 21%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_allows_reads[EXPLAIN SELECT 1] PASSED [ 23%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_allows_reads[  select 1;  ] PASSED [ 26%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[DELETE FROM usage_daily] PASSED [ 28%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[update billing_order set status='paid'] PASSED [ 31%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[INSERT INTO x VALUES (1)] PASSED [ 34%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[SELECT 1; DROP TABLE usage_daily] PASSED [ 36%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[\\copy usage_daily to '/tmp/x'] PASSED [ 39%]
cli_anything/myai/tests/test_core.py::test_validate_adhoc_rejects_writes[] PASSED [ 42%]
cli_anything/myai/tests/test_core.py::test_identity_find_escapes_quotes PASSED [ 44%]
cli_anything/myai/tests/test_core.py::test_billing_status_literal_escaped PASSED [ 47%]
cli_anything/myai/tests/test_core.py::test_day_window_is_clamped PASSED  [ 50%]
cli_anything/myai/tests/test_core.py::test_gate_formula_matches_source PASSED [ 52%]
cli_anything/myai/tests/test_core.py::test_gate_report_verdict PASSED    [ 55%]
cli_anything/myai/tests/test_core.py::test_gate_report_nothing_blocked PASSED [ 57%]
cli_anything/myai/tests/test_core.py::test_her_gate_script_embeds_days PASSED [ 60%]
cli_anything/myai/tests/test_core.py::test_day_list_order_and_bounds PASSED [ 63%]
cli_anything/myai/tests/test_core.py::test_daily_script_rejects_unknown_backend PASSED [ 65%]
cli_anything/myai/tests/test_core.py::test_parse_script_output_skips_warnings PASSED [ 68%]
cli_anything/myai/tests/test_core.py::test_health_summary_flags_stuck_sync PASSED [ 71%]
cli_anything/myai/tests/test_core.py::test_health_summary_clean PASSED   [ 73%]
cli_anything/myai/tests/test_core.py::test_health_unknown_org_error_is_warn PASSED [ 76%]
cli_anything/myai/tests/test_core.py::test_cli_db_sql_write_rejected_locally PASSED [ 78%]
cli_anything/myai/tests/test_core.py::test_cli_json_output_parses PASSED [ 81%]
cli_anything/myai/tests/test_core.py::test_usage_daily_reads_dashboard_view_by_default PASSED [ 84%]
cli_anything/myai/tests/test_full_e2e.py::test_help_and_version PASSED   [ 86%]
cli_anything/myai/tests/test_full_e2e.py::test_status_points_at_243 PASSED [ 89%]
cli_anything/myai/tests/test_full_e2e.py::test_read_only_select_and_server_side_guard PASSED [ 92%]
cli_anything/myai/tests/test_full_e2e.py::test_sync_chain_shapes PASSED  [ 94%]
cli_anything/myai/tests/test_full_e2e.py::test_diagnose_sync_stuck PASSED [ 97%]
cli_anything/myai/tests/test_full_e2e.py::test_upstream_daily_her PASSED [100%]
======================== 38 passed in 95.14s (0:01:35) =========================
```

- 32 个单测 + 6 个 E2E 全过,约 95s(大头是 `diagnose sync-stuck` 的逐账号探针)
- 阳性对照(手动跑,不进套件,太慢):`diagnose sync-stuck --end 2026-09-21 --days 2` ⇒ 161/172 人有用量、不拦
- 没覆盖:`billing`/`identity`/`logs`/`pods`/`realtime`/`usage frontier` 只手动冒烟过;REPL 只手动过 help/queue/quit

## 10-14 追加

- 单测 +3:`coverage_report` 四种 hint(10000 指纹 / 整天 0 / 无 key_rollup / ok)、`summarize_keys` 分类、`probe_pod_manifest` 与 `--probe-pod` 目标切换
- 真 243 冒烟(手动):`diagnose coverage --days 14 --end 2026-10-13` 列出 10 天偏差并指出 10-13 视图遮挡;
  `upstream keys --days 7` 分出 known 500 / owner_not_in_usertable 7 / no_owner 6 / deleted 91;
  `probe-pod up --ttl 300` → `--probe-pod upstream daily` → `probe-pod down` 全链路通
