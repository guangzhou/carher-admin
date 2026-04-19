# Her Billing Stats

端到端统计所有 her 实例的 OpenClaw 本地账本，按 session 类型（主对话 / DM / 群聊 / 子代理 / Dreaming / Realtime / 孤儿 / 定时任务）拆桶，回写到飞书多维表格。

**数据源**：每个 her pod / PVC 下 `/data/.openclaw/agents/main/sessions/*.jsonl{,.reset.*}` 和 `/data/.openclaw/cron/runs/*.jsonl`。**不使用 LiteLLM SpendLogs**（代理层账单和本地账本可能不一致，我们以本地为准）。

**目标表格**：
- Spend 表：`MBKKbBkGcaTLOPs7KuncM7Rkn6g` / `tblpcw43LreeMmNP`
- 注册表（uid → 人员）：wiki `DFqqwIMsIiLUWdkTfs4c1VqLnnh` → base / `tblcvJPRIFV91yHy`

## 目录结构

```
scripts/her-billing-stats/
├── README.md
└── bin/
    ├── her-cost-stats.js          # 采集脚本（上游来源），pod 内 node 执行
    ├── inventory.py               # 列 active pod + paused PVC
    ├── run_one_pod.sh             # 单 pod 执行包装（idempotent）
    ├── scan_paused_pvcs.py        # debug pod 挂载 paused PVC 离线扫描
    ├── fetch_registration.py      # 拉 her 注册表 → uid_to_person.json
    ├── build_rows.py              # 聚合 JSON → Bitable 行（JSONL）
    ├── write_to_bitable.py        # batch_create 写入（可选 --replace-category 清旧）
    ├── review.py                  # 覆盖率 + 对账 + 人员链接校验
    └── run_pipeline.sh            # 一键跑完整 pipeline
```

## 前置条件

1. `kubectl` 已配置，能访问 carher 集群
2. `lark-cli` 已安装并认证（`--as bot` 身份对 spend 表有 record:edit 权限；对注册表有 record:read）
3. `python3` 3.10+，`node 22+`（脚本会拉 `node:22-alpine` 到 debug pod）

## 一键运行

```bash
cd /Users/Liuguoxian/codes/carher-admin/scripts/her-billing-stats
./bin/run_pipeline.sh
```

环境变量（可选）：
- `WORK_DIR`：中间产物目录（默认 `./her-billing-run-<ts>`）
- `NAMESPACE`：集群命名空间（默认 `carher`）
- `PARALLEL`：active pod 并发数（默认 4，过高会 TLS timeout）
- `IDENTITY`：lark-cli 身份（`bot`|`user`，默认 `bot`）
- `REPLACE_EXISTING`：是否先删掉旧的 `OpenClaw-*` 行再写入（默认 `1`）

## 分步运行

```bash
cd $WORK_DIR

# 1) 清单
python3 bin/inventory.py --out inventory.json

# 2) active pod 采集（并发）
python3 -c "import json;d=json.load(open('inventory.json'));print('\\n'.join(f'{u} {p}' for u,p in d['active'].items()))" > pairs.txt
OUT_DIR=stats xargs -n 2 -P 4 -a pairs.txt bash -c '"$0" "$@"' bin/run_one_pod.sh

# 3) paused PVC 离线扫描
python3 bin/scan_paused_pvcs.py --inventory inventory.json --out-dir stats

# 4) 注册表 uid → open_id
python3 bin/fetch_registration.py --out-dir reg

# 5) 构建行
python3 bin/build_rows.py --stats-dir stats --uid-to-person reg/uid_to_person.json \
    --out openclaw_rows.jsonl

# 6) 写入（可选先清旧）
python3 bin/write_to_bitable.py --rows openclaw_rows.jsonl --replace-category "OpenClaw-"

# 7) 校验
python3 bin/review.py --stats-dir stats --uid-to-person reg/uid_to_person.json
```

## Bitable 字段说明

每条插入的 row：

| 字段 | 含义 | 来源 |
|------|------|------|
| `key_alias` | `carher-<uid>` | uid |
| `统计日` | `YYYY-MM-DD` | 快照当天 UTC |
| `日期` | 毫秒时间戳（统计日 0 点 UTC） | 同上 |
| `统计日期` | 毫秒时间戳（写入时刻） | `now()` |
| `费用(USD)` | 该 bucket 总 USD | `sources[bucket].cost_usd` |
| `Input Tokens` / `Output Tokens` / `Cache Read` / `Cache Write` | 对应桶的 token 累加 | `sources[bucket].tokens.*` |
| `交互次数` | 调用数 | `sources[bucket].calls` |
| `账户类型` | `OpenClaw-<中文标签>` | 硬编码映射 |
| `人员` | 注册表中对应 uid 的 `姓名` 人员 | `uid_to_person.json` |

**桶标签映射**（`bin/build_rows.py` 中的 `BUCKETS`）：

| script key | Bitable 账户类型 |
|-----------|----------------|
| `main_chat` | `OpenClaw-主对话` |
| `dm` | `OpenClaw-DM私聊` |
| `group_chat` | `OpenClaw-群聊` |
| `subagent` | `OpenClaw-子代理` |
| `dreaming` | `OpenClaw-Dreaming` |
| `realtime` | `OpenClaw-Realtime` |
| `orphan_sessions` | `OpenClaw-孤儿Session` |
| `cron` | `OpenClaw-定时任务` |

## 注意事项

- **幂等**：`run_one_pod.sh` 对已有有效 JSON 会 skip。重跑只补失败的 uid。
- **TLS timeout**：并发 >10 会压垮 kube API，推荐 `PARALLEL=4`。失败的重跑即可。
- **paused PVC**：单 pod 最多挂 20 个 volume（脚本按 `CHUNK_SIZE` 分批起 debug pod）。
- **人员字段默认值**：Bitable 如果不显式传 `人员`，会把创建者（bot）身份自动填进去。无注册记录的 uid，`build_rows.py` 不写 `人员` 字段 → 落到表里仍会被 Bitable 自动填成 bot 身份，需要跑一次 `review.py` 看报告，再手工或补脚本清除。
- **删除 her**：若某个 her 被删且 PVC 释放，其历史数据无法再采集。
- **S3 / 迁移数据**：集群之前的历史数据如果没搬到 NAS，脚本也拿不到。

## 已知差距（vs 供应商账单）

本 pipeline 产出的是 "OpenClaw 本地账本"。与供应商账单的差距主要来自：

1. Non-Her key 用量（`claude-code-*`、`cursor-*`、`default_user_id` 等）—— 这些不走 OpenClaw agent 运行时，需从 LiteLLM SpendLogs 单独统计。
2. 供应商在 proxy 原价之上的 markup（openrouter / 网宿）。
3. Streaming retry / 失败请求的 tokens 可能进了供应商账单但没写入 session `cost.total`。
4. 集群建立之前的历史 API 调用。

以上都**不在**本 pipeline 范围内。
