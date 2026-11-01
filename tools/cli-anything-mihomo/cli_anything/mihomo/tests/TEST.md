# TEST.md — cli-anything-mihomo

## Plan (written before the tests)

### test_core.py — unit, synthetic data, no network (~20)

| module | what | edge cases |
|---|---|---|
| `config.build` | strips tun/redir/tproxy/secret/authentication/bypass/dns.listen; forces 127.0.0.1, allow-lan false, ctl 127.0.0.1 | subscription that *enables* tun + allow-lan + 0.0.0.0 controller must still come out safe |
| `config.build` | pins become 127.0.0.1 `listeners` with `proxy:` | pin to unknown target, duplicate port, port < 1024, default_group missing → ConfigError |
| `config.build` | url-test/fallback interval overridden; selector untouched | interval 0 = keep feed value |
| `config.audit` | flags every unsafe key; clean config → [] | listener on 0.0.0.0 flagged |
| `subscription.region / summary / nodes` | region tag, counts, duplicate names | `nodes()` output has no server/password/uuid |
| `subscription.parse_userinfo` | header → ints | junk parts ignored |
| `subscription.load` | non-YAML / no proxies → SubscriptionError | |
| `exits.resolve_chain` | group → group → leaf; cycle stops | |
| `exits.scholar_hits` | counts `gs_ri` blocks; 403 page → 0 | |
| `instance.rdir` | rejects `../x`, spaces | |
| `shellcrash` tables | every fact has a `where` | |
| accounts (1.1) | `--account X` switches sub/state/instances dirs; binary cache stays shared | bad account name |
| owner guard (1.1) | stop/start/destroy/push and every controller call refuse an instance owned by another account | dir absent / pre-marker = allowed |
| `exits.table` / `exits.geo` (1.1) | serial (workers=1), median Kiro time, country from exit IP; geo retries 3× and keeps last error | label says DE, exits FR |

### test_full_e2e.py — real 198 + real feed + real mihomo, via the installed command (~10)

Needs: ssh `cltx@10.68.13.198`, 188 SOCKS on 127.0.0.1:18880, `sub set-url` done. FAILS (never skips) if any is missing.
Uses its own instance name `e2e` on ports 27890/29090/27891 so it never collides with `default`.

Workflow **"give one service a US exit, another the default, verify, switch, tear down"**:
1. `sub fetch` → ≥ 40 nodes, groups include `🔰 节点选择`
2. `binary list` contains the configured version
3. `config set` ports + `config pin 27891 "♻️ 自动选择 - US"` → `config render --audit-only` = OK
4. `instance start` → running, all 3 ports listening, `exposed` empty (nothing beyond 127.0.0.1)
5. `exit verify` → every port: HTTP 200 from Scholar **and** results > 0; 27891 exit country = US
6. `exit select "♻️ 自动选择 - JP"` → `exit verify --port 27890` exit country = JP (switch is real, not just API 204)
7. `instance reload` → selection still JP (cache.db), config re-tested by `mihomo -t`
8. `exit measure --region US` → ≥ 1 reachable, results sorted by delay
9. bad input: `exit select nosuch` → non-zero, message names the group
7b. `exit table` → pin port country = US (from ipinfo), Kiro median present
7c. another `--account` cannot stop it (owner marker), instance still running
7d. `instance list` sees it, nothing exposed beyond 127.0.0.1
10. `instance destroy --yes` → no mihomo-harness process left on 198, ports closed

Plus `TestCLISubprocess`: `--help`, `shellcrash modes --json` parse.

## Results

(appended after the run)

### Run 2026-10-28 15:49 (CLI_ANYTHING_FORCE_INSTALLED=1, real 198 + feed + mihomo v1.19.33)

```
test_core.py::TestBuild::test_strips_everything_that_hijacks_or_exposes PASSED [  2%]
test_core.py::TestBuild::test_does_not_mutate_input PASSED [  5%]
test_core.py::TestBuild::test_pins_become_localhost_listeners PASSED [  7%]
test_core.py::TestBuild::test_pin_to_unknown_target PASSED [ 10%]
test_core.py::TestBuild::test_duplicate_port PASSED [ 12%]
test_core.py::TestBuild::test_privileged_port PASSED [ 15%]
test_core.py::TestBuild::test_default_group_must_exist PASSED [ 17%]
test_core.py::TestBuild::test_url_test_interval_override PASSED [ 20%]
test_core.py::TestBuild::test_interval_zero_keeps_feed_value PASSED [ 23%]
test_core.py::TestBuild::test_store_selected_on PASSED [ 25%]
test_core.py::TestBuild::test_dump_roundtrip PASSED [ 28%]
test_core.py::TestAudit::test_flags_unsafe PASSED [ 30%]
test_core.py::TestAudit::test_flags_public_listener PASSED [ 33%]
test_core.py::TestSubscription::test_region PASSED [ 35%]
test_core.py::TestSubscription::test_summary PASSED [ 38%]
test_core.py::TestSubscription::test_nodes_have_no_secrets PASSED [ 41%]
test_core.py::TestSubscription::test_userinfo PASSED [ 43%]
test_core.py::TestSubscription::test_load_rejects_non_clash PASSED [ 46%]
test_core.py::TestExits::test_chain PASSED [ 48%]
test_core.py::TestExits::test_chain_leaf PASSED [ 51%]
test_core.py::TestExits::test_chain_cycle_terminates PASSED [ 53%]
test_core.py::TestExits::test_scholar_hits PASSED [ 56%]
test_core.py::TestInstanceName::test_rejects[../x] PASSED [ 58%]
test_core.py::TestInstanceName::test_rejects[a b] PASSED [ 61%]
test_core.py::TestInstanceName::test_rejects[a/b] PASSED [ 64%]
test_core.py::TestInstanceName::test_rejects[] PASSED [ 66%]
test_core.py::TestInstanceName::test_accepts PASSED [ 69%]
test_core.py::test_shellcrash_facts_are_sourced PASSED [ 71%]
test_full_e2e.py::TestWorkflow::test_1_subscription PASSED [ 74%]
test_full_e2e.py::TestWorkflow::test_2_render_is_safe PASSED [ 76%]
test_full_e2e.py::TestWorkflow::test_3_running_and_localhost_only PASSED [ 79%]
test_full_e2e.py::TestWorkflow::test_4_verify_real_scholar_results PASSED [ 82%]
test_full_e2e.py::TestWorkflow::test_5_switch_changes_real_exit PASSED [ 84%]
test_full_e2e.py::TestWorkflow::test_6_reload_keeps_selection PASSED [ 87%]
test_full_e2e.py::TestWorkflow::test_7_measure_region PASSED [ 89%]
test_full_e2e.py::TestWorkflow::test_8_bad_select_fails_loudly PASSED [ 92%]
test_full_e2e.py::TestWorkflow::test_9_destroy_leaves_nothing PASSED [ 94%]
test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 97%]
test_full_e2e.py::TestCLISubprocess::test_shellcrash_modes_json PASSED [100%]
============================= 39 passed in 19.34s ==============================
```

Coverage gaps: the controller secret is generated/kept on 198 and not unit-tested; behaviour after a 198 reboot (instance does not survive it) is by design, not tested; subscription base64/v2ray feeds are rejected, not converted.

### Run 2026-10-28 20:07 — v1.1 (accounts, owner guard, exit table/geo, instance list)

```
test_core.py::TestBuild::test_strips_everything_that_hijacks_or_exposes PASSED [  1%]
test_core.py::TestBuild::test_does_not_mutate_input PASSED [  3%]
test_core.py::TestBuild::test_pins_become_localhost_listeners PASSED [  5%]
test_core.py::TestBuild::test_pin_to_unknown_target PASSED [  7%]
test_core.py::TestBuild::test_duplicate_port PASSED [  9%]
test_core.py::TestBuild::test_privileged_port PASSED [ 11%]
test_core.py::TestBuild::test_default_group_must_exist PASSED [ 13%]
test_core.py::TestBuild::test_url_test_interval_override PASSED [ 15%]
test_core.py::TestBuild::test_interval_zero_keeps_feed_value PASSED [ 16%]
test_core.py::TestBuild::test_store_selected_on PASSED [ 18%]
test_core.py::TestBuild::test_dump_roundtrip PASSED [ 20%]
test_core.py::TestAudit::test_flags_unsafe PASSED [ 22%]
test_core.py::TestAudit::test_flags_public_listener PASSED [ 24%]
test_core.py::TestSubscription::test_region PASSED [ 26%]
test_core.py::TestSubscription::test_summary PASSED [ 28%]
test_core.py::TestSubscription::test_nodes_have_no_secrets PASSED [ 30%]
test_core.py::TestSubscription::test_userinfo PASSED [ 32%]
test_core.py::TestSubscription::test_load_rejects_non_clash PASSED [ 33%]
test_core.py::TestExits::test_chain PASSED [ 35%]
test_core.py::TestExits::test_chain_leaf PASSED [ 37%]
test_core.py::TestExits::test_chain_cycle_terminates PASSED [ 39%]
test_core.py::TestExits::test_scholar_hits PASSED [ 41%]
test_core.py::TestInstanceName::test_rejects[../x] PASSED [ 43%]
test_core.py::TestInstanceName::test_rejects[a b] PASSED [ 45%]
test_core.py::TestInstanceName::test_rejects[a/b] PASSED [ 47%]
test_core.py::TestInstanceName::test_rejects[] PASSED [ 49%]
test_core.py::TestInstanceName::test_accepts PASSED [ 50%]
test_core.py::test_shellcrash_facts_are_sourced PASSED [ 52%]
test_core.py::TestAccounts::test_account_dirs PASSED [ 54%]
test_core.py::TestAccounts::test_use_account_switches_every_path PASSED [ 56%]
test_core.py::TestAccounts::test_binary_cache_is_shared PASSED [ 58%]
test_core.py::TestOwnerGuard::test_mismatch_refused PASSED [ 60%]
test_core.py::TestOwnerGuard::test_match_and_absent_ok PASSED [ 62%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[stop] PASSED [ 64%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[start] PASSED [ 66%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[destroy] PASSED [ 67%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[push_config] PASSED [ 69%]
test_core.py::TestTableAndGeo::test_table PASSED [ 71%]
test_core.py::TestTableAndGeo::test_geo_retries_and_flags_label PASSED [ 73%]
test_full_e2e.py::TestWorkflow::test_1_subscription PASSED [ 75%]
test_full_e2e.py::TestWorkflow::test_2_render_is_safe PASSED [ 77%]
test_full_e2e.py::TestWorkflow::test_3_running_and_localhost_only PASSED [ 79%]
test_full_e2e.py::TestWorkflow::test_4_verify_real_scholar_results PASSED [ 81%]
test_full_e2e.py::TestWorkflow::test_5_switch_changes_real_exit PASSED [ 83%]
test_full_e2e.py::TestWorkflow::test_6_reload_keeps_selection PASSED [ 84%]
test_full_e2e.py::TestWorkflow::test_7_measure_region PASSED [ 86%]
test_full_e2e.py::TestWorkflow::test_7b_exit_table_has_real_country PASSED [ 88%]
test_full_e2e.py::TestWorkflow::test_7c_other_account_cannot_touch_it PASSED [ 90%]
test_full_e2e.py::TestWorkflow::test_7d_instance_list_sees_it PASSED [ 92%]
test_full_e2e.py::TestWorkflow::test_8_bad_select_fails_loudly PASSED [ 94%]
test_full_e2e.py::TestWorkflow::test_9_destroy_leaves_nothing PASSED [ 96%]
test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 98%]
test_full_e2e.py::TestCLISubprocess::test_shellcrash_modes_json PASSED [100%]
============================= 53 passed in 28.80s ==============================
```

Regression this run caught by hand, now covered: v1.1 first cut let `--account acct2 --name default instance stop` stop the default instance (stop() skipped the owner check). Fixed + test_every_mutation_checks_owner + e2e 7c.

### Run 2026-10-29 19:42 — v1.2 (sub quota, tunnel, fetch retry + atomic headers)

```
test_core.py::TestBuild::test_strips_everything_that_hijacks_or_exposes PASSED [  1%]
test_core.py::TestBuild::test_does_not_mutate_input PASSED [  3%]
test_core.py::TestBuild::test_pins_become_localhost_listeners PASSED [  5%]
test_core.py::TestBuild::test_pin_to_unknown_target PASSED [  6%]
test_core.py::TestBuild::test_duplicate_port PASSED [  8%]
test_core.py::TestBuild::test_privileged_port PASSED [ 10%]
test_core.py::TestBuild::test_default_group_must_exist PASSED [ 11%]
test_core.py::TestBuild::test_url_test_interval_override PASSED [ 13%]
test_core.py::TestBuild::test_interval_zero_keeps_feed_value PASSED [ 15%]
test_core.py::TestBuild::test_store_selected_on PASSED [ 16%]
test_core.py::TestBuild::test_dump_roundtrip PASSED [ 18%]
test_core.py::TestAudit::test_flags_unsafe PASSED [ 20%]
test_core.py::TestAudit::test_flags_public_listener PASSED [ 21%]
test_core.py::TestSubscription::test_region PASSED [ 23%]
test_core.py::TestSubscription::test_summary PASSED [ 25%]
test_core.py::TestSubscription::test_nodes_have_no_secrets PASSED [ 26%]
test_core.py::TestSubscription::test_userinfo PASSED [ 28%]
test_core.py::TestSubscription::test_load_rejects_non_clash PASSED [ 30%]
test_core.py::TestExits::test_chain PASSED [ 31%]
test_core.py::TestExits::test_chain_leaf PASSED [ 33%]
test_core.py::TestExits::test_chain_cycle_terminates PASSED [ 35%]
test_core.py::TestExits::test_scholar_hits PASSED [ 36%]
test_core.py::TestInstanceName::test_rejects[../x] PASSED [ 38%]
test_core.py::TestInstanceName::test_rejects[a b] PASSED [ 40%]
test_core.py::TestInstanceName::test_rejects[a/b] PASSED [ 41%]
test_core.py::TestInstanceName::test_rejects[] PASSED [ 43%]
test_core.py::TestInstanceName::test_accepts PASSED [ 45%]
test_core.py::test_shellcrash_facts_are_sourced PASSED [ 46%]
test_core.py::TestAccounts::test_account_dirs PASSED [ 48%]
test_core.py::TestAccounts::test_use_account_switches_every_path PASSED [ 50%]
test_core.py::TestAccounts::test_binary_cache_is_shared PASSED [ 51%]
test_core.py::TestOwnerGuard::test_mismatch_refused PASSED [ 53%]
test_core.py::TestOwnerGuard::test_match_and_absent_ok PASSED [ 55%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[stop] PASSED [ 56%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[start] PASSED [ 58%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[destroy] PASSED [ 60%]
test_core.py::TestOwnerGuard::test_every_mutation_checks_owner[push_config] PASSED [ 61%]
test_core.py::TestTableAndGeo::test_table PASSED [ 63%]
test_core.py::TestTableAndGeo::test_geo_retries_and_flags_label PASSED [ 65%]
test_core.py::TestTunnelAndQuota::test_tunnel_already_up_does_not_spawn PASSED [ 66%]
test_core.py::TestTunnelAndQuota::test_tunnel_never_uses_clear_all_forwardings PASSED [ 68%]
test_core.py::TestTunnelAndQuota::test_tunnel_not_managed_for_other_proxies PASSED [ 70%]
test_core.py::TestTunnelAndQuota::test_accounts_lists_dirs_with_url PASSED [ 71%]
test_core.py::TestTunnelAndQuota::test_failed_fetch_keeps_last_good_headers PASSED [ 73%]
test_core.py::TestTunnelAndQuota::test_quota_cli_warns PASSED [ 75%]
test_full_e2e.py::TestWorkflow::test_1_subscription PASSED [ 76%]
test_full_e2e.py::TestWorkflow::test_1b_quota_from_live_header PASSED [ 78%]
test_full_e2e.py::TestWorkflow::test_2_render_is_safe PASSED [ 80%]
test_full_e2e.py::TestWorkflow::test_3_running_and_localhost_only PASSED [ 81%]
test_full_e2e.py::TestWorkflow::test_4_verify_real_scholar_results PASSED [ 83%]
test_full_e2e.py::TestWorkflow::test_5_switch_changes_real_exit PASSED [ 85%]
test_full_e2e.py::TestWorkflow::test_6_reload_keeps_selection PASSED [ 86%]
test_full_e2e.py::TestWorkflow::test_7_measure_region PASSED [ 88%]
test_full_e2e.py::TestWorkflow::test_7b_exit_table_has_real_country PASSED [ 90%]
test_full_e2e.py::TestWorkflow::test_7c_other_account_cannot_touch_it PASSED [ 91%]
test_full_e2e.py::TestWorkflow::test_7d_instance_list_sees_it PASSED [ 93%]
test_full_e2e.py::TestWorkflow::test_8_bad_select_fails_loudly PASSED [ 95%]
test_full_e2e.py::TestWorkflow::test_9_destroy_leaves_nothing PASSED [ 96%]
test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 98%]
test_full_e2e.py::TestCLISubprocess::test_shellcrash_modes_json PASSED [100%]
============================= 60 passed in 36.75s ==============================
```

Bug this round: a fetch that died mid-way (curl 97, 188 tunnel blip) truncated sub.headers, so the next `sub quota` said 'no subscription-userinfo header'. Fixed: headers/yaml written to .part and swapped only on success; curl 7/28/35/56/97 retried 3x with tunnel re-check.
