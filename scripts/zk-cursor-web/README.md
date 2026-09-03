# `scripts/zk-cursor-web/` 索引

124 个文件是 08-23 → 09-02 十几轮实战堆出来的，**其中相当一部分是一次性探针，还有一部分是
量具坏掉那一版**。没有索引的后果不是"找不到"，是**三个月后有人捡起一把坏尺子当判据**——
09-01 就发生过一次（`lane_task_ab.py` 打错端点，量出"81~85 动手率 8~26%"，据此写补丁、上灰度、
告诉用户"大部分人吃的是坏的"，全是假的）。

分三档：**现役 / 一次性历史留档 / 已作废（尺子坏过，别再用）**。

> **新增脚本必须在这里登记一行。** 不登记 = 默认它三个月后会被当成现役判据。
>
> 拓扑与回滚看 `docs/cr-g-pool-rollback-20260902.md`（三份 CM 各管哪些 lane、每处改动的备份路径
> 与已核对的回退命令、以及**删 82 那两条裸载体腿会静默弄坏池的 `-max`** 这条没有自动化门的耦合）。

---

## 一、现役（还在用，坏了要修）

### 硬门（退出码即判据，出数前必须过）

| 脚本 | 干什么 | 判据 |
|---|---|---|
| `pool_consistency.py` | 从「谁挂了这个 CM」反查 lane，比容器内 `responses.js` sha256 与 CM 是否逐字节相等；顺带查池别名腿数 | 退出码 0。**fork 出去的 CM 必须登记进脚本里的 `FORKED_CM`**，否则那条 lane 悄悄退出一致性门 |
| `pool_consistency_selftest.py` | 上面那道门的双向实测（注入坏 CM + 假 lane） | 8/8 必红 |
| `crg_gate12_run.sh` | 真 Cursor 门①门② 一条命令：锁屏门 → composer 门 → 阳性对照 → conv8 八轮 → 三处判据对齐 | 顺序是硬的，阳性对照复现不出就退出，不许接着出数。`EXPECT_MODEL=` 指定期望选中模型 |
| `crg_family_gate.py` | 每个**载体系列**挑一个名字在真 Cursor 跑 3 轮；换模型走 `state.vscdb` **两个**存储面，硬门放在**重启之后** | `--list` 会核对 `FAMILIES` 表是否陈旧，并把「载体未覆盖」与「档位变体未单独验」分开报 |

### 驱动 / 对账

| 脚本 | 干什么 |
|---|---|
| `cursor_gui_e2e_driver.py` | GUI 驱动真 Cursor（pbcopy + osascript），产 `/tmp/e2e_manifest.jsonl` |
| `cursor_gui_e2e_correlate.py` | manifest × pod 日志 × 客户端账本三方对账 |
| `failover_drill.py` | 单发内换 lane 的 failover 演练（必须逐发证明坏 lane 被选中，"全成功"默认是假绿） |
| `conv_prefix_live_probe.py` | 会话复用现场探针（多轮 + 每轮换 instructions）。**合成绿不算数**，真验收在真 Cursor |
| `lane82_regress_probe.sh` | 82 的 scoped-key 回归（动 `LiteLLM_ProxyModelTable` 之后必跑） |

### 离线用例（改代码前先跑，从产物里抠真函数体 eval 驱动）

`conv_prefix_offline_cases.js`（会话复用最长前缀 + ts 平局，13 组 41 断言）·
`execenv_strip_offline_cases.js` · `write_dialect_offline_cases.js` ·
`sys_deconflict_offline_cases.py` · `conv_persist_offline_cases.js` ·
`contract_diet_offline_cases.js` · `url_prior_offline_cases.js` · `ide_strip_offline_cases.js` ·
`lark_esc_offline_cases.js` · `empty_retry_offline_cases.js` · `genui_strip_offline_cases.js` ·
`urlsafe_offline_cases.js` · `uq_strip_offline_cases.js` · `unterminated_block_offline_cases.js` ·
`fail_teach_offline_cases.js` · `conn_watchdog_offline_cases.js`

> ⚠️ 离线用例**不是回归门**。语法过 + 离线绿 ≠ 产品没坏
> （`feedback_syntax_check_is_not_a_test_gate`）。

### 补丁脚本（anchor-assert 模式：`assert src.count(anchor)==K`，产物必过 `node --check`）

`patch_conv_prefix.py`（08-31 最长前缀）· `patch_handoff_poll.py`（09-01 `stream_handoff` 轮询）·
`patch_handoff_stale.py`（09-02 抢跑闸，按 convId 比对上一轮正文）·
`patch_sys_deconflict.py` + `patch_strip_plan.py`（09-01 系统提示词两刀）

> **打之前先查这条 lane 挂的是哪份 CM。** 09-02 傍晚起是**三份**，改错一份等于没改：
> `zk-cursor-bpi-patch-82`（只有 82，canary）· `zk-cursor-bpi-patch-pool`（84/135~140，生产池）·
> `zk-cursor-bpi-patch`（只剩 101，旧方案只读对照）。**改共用 CM 现在一条生产腿都够不到。**
> 权威表在 `pool_consistency.py` 的 `FORKED_CM`，别凭记忆。

### 池运维（09-02 建池 + 换腿这一轮新增，都在用）

| 脚本 | 干什么 | 判据 / 陷阱 |
|---|---|---|
| `lane_model_catalog.py` | **入池门**：借 lane 自己的 `ChatGPTAPI` 打 `/backend-api/models`，看这条腿背后账号**到底有哪些模型** | ≥19 slug 且含 `thinking`/`pro`/`instant`。free 档只有 10 个 —— 那种腿**物理上答不出**菜单里大多数名字。**裸 fetch 打 chatgpt.com 一律 CF 403，必须借 lane**；lane 镜像是 node 基底，没有 python3 |
| `clone_lane_from_live.py` | 从**一条活腿**克隆出新 lane 的 deploy/svc | 拷贝源必须是活的：写死模板会陈旧（`new-pod.sh` 那份带着过期 `tolerations`，而钉了 `nodeName` 的 pod 绕开调度器，`NoSchedule` 根本不生效） |
| `lane_seed_capture.sh` / `lane_seed_install.sh` | 抓 web 登录态 → 灌进 225 的 `/Data/zerokey-sessions/zero-<N>/` | **必须在 188 抓**（`cf_clearance` 绑出口 IP）。灌之前先备份旧 seed；**别 `rm -rf state/`**，profile 复用是最大提速手段 |
| `crg_pool_register.py` | 把 82 的 14 行整份拷到池腿（只改 `api_base`/`model_info.id`/`model_name`） | 拷贝源 = **`/model/info` 解密视图**不是 DB raw（`litellm_params` 在 DB 里是加密列）；`api_key` 占位符 `sk-zerokey-web-noop` 必须显式补（漏了真流量 401，master key 测是 200 假绿） |
| `crg_lane_retire.py` | 把某几条 lane 的腿从池别名里摘掉 | 硬门：**绝不把池别名摘成 0 腿**（0 腿 = `No deployments available`，比留着坏腿更糟）。名字里编了 lane 号的**直连名**例外，随 lane 一起消失是对的 |
| `crg_pool_probe.py` | 池的形状验收：临时真 key + **复刻 Cursor 真实线型**（`/v1/chat/completions` + stream + tools，不带 `reasoning_effort`） | 落点**只认 SpendLogs 的 `model_id`**（响应头 `x-litellm-model-id` 回内部句柄、`api-base` 被 198 脱敏成 `-`）。亲和是 **key 级**的 —— 单把 key 打 100 发全落一条腿是尺子用错，要 `--keys` 换 key 才换腿 |
| `crg_key_grant.py` | 给 key 补授权（读-合并-写） | `/key/update` 的 `models` 是**整表覆盖**，且吃 `key` 不吃 `key_alias` |
| `lane_direct_probe.py` | 逐腿定点探针（钉死某条 lane 打某个名字），用来分辨「池坏了」和「某条腿坏了」 | |
| `crg_row_audit.py` / `lane_conv_dump.py` / `serve_check_lanes.py` / `lane_coldstart_probe.py` | 只读查配置行 / 倒会话 / 查 serve 状态 / 冷启延迟 | 查 `LiteLLM_ProxyModelTable` **只取 `model_name` 和 `model_info->>'id'` 两列，禁止拉 `litellm_params`** |

### 装机 / 克隆 / 发版

| 脚本 | 干什么 |
|---|---|
| `cursor_team_setup.js` | 给同事的装机器（现役）。**它不是开关**：`--apply` 默认会把 composer 覆盖成它内置的那个模型名 |
| `package_team_setup.sh` | 打 `cursor-g-setup.zip`。`zip -r` 是**追加不是重建**，打包前必 `rm -f`；zip 禁中文名 |
| `clone_web_fc_lane_v2.py` | 克隆新 lane（建直连名 + 挂池 + grant + 临时真 key 自动验收）。模板必须照抄**活的** lane，写死的会陈旧 |

### 回归 harness（跑在 litellm-proxy pod 的 `/tmp/cw/`，pod 重启后 `kubectl cp` 回去）

`sse_dump.py`（事件序列 + delta_chars）· `cmp_delta_done.py`（delta 拼接 == done 逐字符；
**只适用 prose 场景**，tool-call 场景 0 delta 报 MISMATCH 是预期）· `loop_ls.py` / `loop_dl.py`
（闭环验收，单轮重放测不出回程 bug）· `timing.py` · `ls_check.py` · `usage_check.py` ·
`replay_runner.py` · `codex_regress.py` · `pool_accept.py` / `pool_register.py` ·
`conv_persist_accept.py` · `contract_diet_accept.py`

---

## 二、一次性历史留档（当时的证据链，别当现役量具）

这些是**某一天为回答某一个问题写的**，答案已经进了记忆/文档，脚本留着是为了「当初那个数是怎么来的」
可复查。**重跑它们大概率打错对象**（模型名、lane、端点都变过好几轮）。

- **S1/S3/S4 系列（08-26 → 08-28，协议服从率那三天）**：`s1_baseline.py`（抓包 fixture 回放基线）、
  `s3_probe.py`/`s3c_ll_probe.py`/`s3d_probe.py`/`s3e_probe.py`（契约服从率）、
  `s3f_validate.py`（`ZK_TR_VIS=1` 金丝雀）、`s3g_latency.py`（sol vs instant 延迟对照）、
  `s3h_regress.py`（tr-vis + th-pulse 全量回归）、`s3i_speedab.py`（延迟净收益 A/B）、
  `s4_v2_probe.py`/`s4_v21_probe.py`/`s4_v22_probe.py`（协议 v2 最小闭环）
- **握手/服从对照实验（08-27）**：`teach_conv_probe.py`（教学轮握手）、
  `seeded_compliance_probe.py`（结构性先例 vs 口头 ack）、`scenario_coverage_probe.py`（场景分布）
- **单点裁定探针**：`complex_task_probe.py`（复杂任务首轮必须动手）、
  `tool_r2_text_probe.py`（裁定 hollow 是严判还是真空）、`url_toolfeed_probe.py`（URL 残缺根因）、
  `contract_diet_zero_probe.py`（DIET 零档多轮服从）、`tool_diet_a6_probe.py`（A 线目录瘦身）
- **MCP 那条线（B 线，已判作废）**：`mcp_bridge*.js`、`mcp_jsonrpc*.js`、`mcp_rendezvous*.js`、
  `mcp_connector_mgr_offline_cases.js`、`mcp_probe_server.js`、`registry_apply.py`、
  `registry_offline_cases.js`、`registry_b7_probe.py`
  （`registry_b7_probe.py` 自己的 docstring 就写着「作废线：MCP 信封命中 <6/12 → B 线作废」）
- **ROI #3 每日摘要**：`verdict_digest.py` + `digest_notify.py` + `verdict_digest_offline_cases.py`
  （离线纯解析、dry-run，没接成常驻）
- **旧版本补丁**：`patch_round15.py` ~ `patch_round18.py`、`tool_diet_apply.py`、
  `cursor_queue_pump_patch.py`、`cursor_queue_diag_patch.py`、`regress_queue_pump.js`、
  `v2_offline_cases.js`
- **装机器旧形态**：`cursor_team_setup.py` / `.sh` / `.cmd`（已被 `cursor_team_setup.js` 取代）
- **克隆器 v1**：`clone_web_fc_lane.py`（terra 冻结期的参照，现用 v2）

---

## 三、已作废 —— 尺子坏过，别再用它出数

| 脚本 | 坏在哪 | 处置 |
|---|---|---|
| `lane_task_ab.py` | ①打的是 `/v1/responses`，而这些模型 `/model/info` 实查全是 `mode:chat`（真 Cursor 走 chat + LiteLLM 的 chat→responses 桥）；②两臂载体 slug / effort 没配平。据它出的「81~85 动手率 8~26%」是假的，用户日常真 Cursor 里 `ls` 一直正常 | 文件顶部已刻警告框。**要用先修端点 + 配平两臂**，否则别开 |
| `patch_act_kick_v3.py` + `act_kick_offline_cases.js` | 它修的是上面那把坏尺子量出来的"病"。灰度失败，CM 已回退到 `df9506` | 留档，别再打 |
| `cursor_chain_patch.py` + `chain_srv_offline_cases.js` | 客户端链式增量 shim `@cx-chain:v3`，09-01 与服务端半一起下线（单腿 = 静默少收历史且不报错） | 安装器默认已摘除 |

**共同的教训**：这三条都不是"代码写错了"，是**量具/方案层面被证伪**。
所以判断一个脚本能不能用，不能只看它跑不跑得起来。

---

## 跑之前的四条硬规矩

1. **第 0 步 = 阳性对照**。新的/久没用的量具，先在答案已知的样本上复现已知答案；复现不出不许出数。
   **合成红与合成绿同样不可信；用户日常生产实测 > 我自己写的探针，冲突时被告是探针。**
2. **验收走临时真 key**（`/key/generate` → 打暗号 → `/key/delete`）。master key 绕过 per-key gate
   与 `api_key` gate，必假绿。只读查配置才用 master key。
3. **探针不许带 `reasoning_effort`** —— 真 Cursor 不发它，且配置已经提供
   （`litellm_params.reasoning_effort`）。**探针不许补配置本该提供的字段**，否则红被构造性屏蔽。
4. **提示词里不许加我自己发明的免责/限域从句**（`只读别改`、`别动我项目`、`工作目录 /tmp`）——
   09-01 实测那些从句恰好在教模型别动手。

产物一律写**带时间戳的唯一路径**，不要写死 `/tmp/xxx.jsonl`：固定路径会读到别人/上一轮的陈旧文件，
而三方对账靠时间窗口划分，陈旧行会把窗口撑歪（`crg_family_gate.py` 踩过）。

## 2026-09-03 一日沉淀（现役）

### skill-hint / skill-kick（服务端，lane responses.js）
- `skill-hint/patch_skill{hint,hint_v2,kick,kick_v2,kick_v3}.py`：按序五刀（anchor-assert）。产物 = CM `zk-cursor-bpi-patch-135`（sha `fc77f9e9…`），**135~140 六腿共用**，改它必六腿全滚；84 仍在 `-pool`，82 canary 未动。门控 `ZK_SKILL_HINT=1`。
- 机制/判据/回滚：skill `zk-cursor-web-fc-iterate` 专节 + `docs/skill-hint-rollback-20260903.md`。
- `crg_pool_register.py --only cr-g-5.6 --lanes 136,…,140`：主池名从 1 腿补到 6 腿（其余 13 池名 7 腿）。在 proxy pod 内跑，**`sudo -n` 别用 `sudo -S`**（和 `exec -i` 抢 stdin）。
- `pool_consistency.py`：FORKED_CM 登记 135~140→`-135`、84→`-pool`；ZK_SKILL_HINT 众数=1，82/84/101 三条"没开"登记在 ACCEPTED_ENV_DRIFT。**同一 lane 登记两处后写覆盖 = 静默退出门**，改登记后看 C 段"参与比对的 lane"名单。

### 装机包 v3（`cursor_team_setup.js` / `package_team_setup.sh`）
- 菜单 = cr-g 14 名 + `sa-grok-4.5/4.6`（Cursor 线型 chat+stream+tools 实探过）；`MODEL_PREFIXES=["cr-g-","sa-grok-"]`（单前缀会把选了 grok 的人 REPAIR 时打回默认）。`setup_impl_parity.py` 守 js/py 三常量相等。
- **Cursor 3.18.25 兼容**：`localagent`（前缀变 `localMode:vl.localMode})`）、`norelay`（参数表删 `agentBackend`）两条正则改成两代形状都认、仍要求恰好 1 次。`bundle_anchor_probe.js` 只读数 8 个锚点命中，Cursor 不用退——同事报「model not available」先跑它。已验 3.16.x / 3.17.19 / 3.18.25。
- **"Cursor 正在运行"假阳**：zk-delta 小代理借 Cursor 二进制当 node 跑（`…/MacOS/Cursor ~/.zk-delta/sidecar/sidecar.js`），第二次跑安装器就被当成 GUI。`cursorRunning()` 改成排除自身 pid + 带 `.js` 参数的进程（Win 用 wmic 取命令行，无 wmic 退回 tasklist）。
- lark-cli/skills/飞书登录那一步**默认关**（同事困惑），`--lark` 显式才做；包默认不带 lark-skills（`LARK_SKILLS=1` 打包才带）。做时全部"有就跳过"，二进制从 npmmirror 拉（sha256 对 npm 包 checksums）。
- 装机文档 `OQCPdLd4MovEVoxGzdMcD3CJnCf`：一页纸，含 Cursor 下载/注册链接，无飞书字样。换附件正解 `docs +update --command append/block_replace --content '<figure view-type="Card"><source path="@./x.zip" …/></figure>'`（须 cd 到文件目录）；`media-insert` 会插成 `<img>`；大范围 `block_replace` 撞"中间兄弟无 id"时用 `overwrite` + `<source token=…>` 带回附件。

### key 授权 / 编辑
- `crg_key_grant.py --alias <a> [--alias …] [--apply]`：给 key 加 14 个 cr-g 池名（读-合并-写、备份 `key-<alias>-<ts>-pre-crg-pool.json`、回读逐名核对）。不给 -82/-135 直连名、不动 aliases。
- `crg_key_grant_all.py`：在 proxy pod 内批量（env `TOKENS_FILE`=`alias<TAB>token` 文件），09-03 645 把 83 秒跑完，**跳过 models 为空的 key**（空 = 全部可用，加名单反而收窄）。备份 `/Data/backups/keys-cursor-all-20260903-112011-pre-crg-pool.json`（638 条）。
- `litellm_key_add.py --alias X --models a,b [--aliases JSON | --copy-from Y] [--apply]`：通用追加 models/aliases，先查 /model/info 名字能否解析，备份 `key-<alias>-<ts>-pre-add.json`。09-03 用它照 carher-1 给 carher-13 加 `claude-fable-5.1`/`claude-opus-5`。
- 判据一律 **DB 直读**（`unnest(models)` 计数、`aliases` 键数），不认 API 自述。
