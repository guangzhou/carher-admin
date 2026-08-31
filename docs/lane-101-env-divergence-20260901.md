# lane 101 与其余五条 lane 的 env 分歧（2026-09-01 结账）

## 事实

六条 lane（101/81/82/83/84/85）挂**同一个 CM** `zk-cursor-bpi-patch`，容器内
`responses.js` 的 sha256 逐字节相等（`pool_consistency.py` A 段一路绿）。
但 env 不一样：

| lane | env 个数 | 走哪条路 |
|---|---|---|
| `zero-cursor-bpi`（101） | 10 | 九补丁级联（proto2 关） |
| `zero-cursor-bpi-{81,82,83,84,85}` | 25 | proto2（槽位化契约） |

`responses.js` 里几乎每个功能都由 `process.env.ZK_*` 门控，所以
**A 段绿只证明"六条跑同一份代码"，证不了"六条行为一致"**。用户被 WA（key 级黏性）
钉到哪条 lane，就吃哪条 lane 的行为——同一个菜单项行为不确定。这是 08-31 收尾时
留下的盲区，本文是它的结账。

## 做了什么

### 1. 删掉 101 上唯一没有理由的那条：`ZK_CHAIN_SRV=1`

证伪过再删的，不是看着眼生就删：

- CM 15 个 key 里 `ZK_CHAIN_SRV` 出现 **0 次**；
- 101 活 pod 里 `grep -rl ZK_CHAIN_SRV /app` **无任何文件命中**。

即**没有消费者**。删是对的，但当时给的理由写错了，更正如下：chain-srv 的服务端那半
**不是独立服务，它就在 `responses.js` 里**（`ZK_CHAIN_SRV` 门控的四个补丁点，08-30 在 lane 82
做 canary）。这条 env 之所以成了死配置，是因为**那段代码后来被回滚了**，不是因为它属于别的服务。
判据不变（CM 0 次命中 + pod 内 0 文件命中），结论不变，只是归因当时说错了。
配套的下线决定见 `docs/cx-chain-shim-decommission-20260901.md`。

```bash
kubectl -n litellm-product set env deploy/zero-cursor-bpi ZK_CHAIN_SRV-
kubectl -n litellm-product rollout status deploy/zero-cursor-bpi
```

回滚点：`/Data/backups/zero-cursor-bpi-101-pre-envalign-20260901-001251.json`
（同一份也在 198 的 `/home/cltx/laneenv/`），整份 deployment JSON，`kubectl apply -f` 即回。

### 2. **没有**把 101 翻成 proto2

理由是实测，不是"稳一点好"：

用真抓包 `cap-6.json`（instructions 9961c / 19 tools / item0 57872c）跑
`lane_task_ab.py` **逐发交错** A/B（不是先跑完一臂再跑另一臂——那正是 failover
演练假绿的成因）：

| 轮次 | 101（九补丁级联） | 82（proto2） |
|---|---|---|
| `--shots 6` turn1 服从 | 6/6 | 6/6 |
| `--shots 4 --followup` turn1 | 4/4 | 4/4 |
| 同上 turn2（工具结果回灌） | 4/4 | 4/4 |
| 删 `ZK_CHAIN_SRV` 后复测 turn1/turn2 | 4/4 / 4/4 | 4/4 / 4/4 |

EMPTY 0、ERR 0，延迟中位数两臂同量级（3.9s vs 4.1s）。唯一可见差别是风格：
101 有两次直接用正文答（含 "19"），82 倾向再发一个 call——都算合格形态。

**测不出 101 差**，所以：

- 没有「101 更弱」这个待修的缺陷可修；
- 翻 proto2 会造出**第四种配置**（既不是 08-27 验过的九补丁形态、也不是验过的
  proto2 全量形态，而是"101 硬件 + 部分 proto2 env"），风险大于收益；
- 本仓库自己的规矩是**合成绿不构成上线依据**，所以也不允许拿上面这张表当翻档许可。

要收掉这条分歧，判据是**真 Cursor 走一遍门②**（shell `ls` + 飞书建文档），
不是再跑一遍合成探针。

### 3. 把分歧变成「看得见 + 新漂移会红」

`pool_consistency.py` 加 **C 段 env 一致性**：

- 除身份类变量（`ZK_USER`）外，每个变量取各 lane 的**众数值**为基准，偏离即 drift；
- drift 必须在 `ACCEPTED_ENV_DRIFT` 里有**带日期和原因**的条目才放行；
- 放行的**照样打印**（允许清单是决策记录，不是消音器）；
- 允许清单里已无对应分歧的**陈旧条目**也会被点出来，避免留着当免疫。

101 那 15 条 proto2 家族变量按上面的决策进了允许清单，注明日期与"待真 Cursor 验收"。

**门会红是实测过的**（`pool_consistency_selftest.py`，7/7）：

| 注入 | 期望 | 结果 |
|---|---|---|
| ① 不注入 | A/B/C 全 PASS | ✅ |
| ② CM 里 `responses.js` 多一个字节 | A 段 FAIL | ✅ |
| ③ DB 插一条指向 lane 99 的别名 | B 段 dangling FAIL | ✅ |
| ④ 基线 env（带允许清单） | C 段 PASS | ✅ |
| ⑤ 给某条 lane 塞一个别人没有的 env | C 段 FAIL | ✅ |
| ⑥ 清空允许清单 | C 段 FAIL（证明 101 那 15 条是被允许清单放行的，不是压根没看见） | ✅ |

⑥ 这条是关键：没有它，"C 段 PASS" 和 "C 段瞎了" 无法区分。

## 顺带补上的一个缺口

81/83/84/85 入池时只验过"出字回显"，**从没验过工具服从**，而这四条现在承载
约三分之二的池流量。同一套 A/B（`cursor-g-<N>-5.6-sol`，同一载体 slug，
逐发交错，3 发/臂）实测：

```
81  turn1 ACTED 3/3  EMPTY 0  ERR 0  |  turn2 OK 3/3
83  turn1 ACTED 3/3  EMPTY 0  ERR 0  |  turn2 OK 3/3
84  turn1 ACTED 3/3  EMPTY 0  ERR 0  |  turn2 OK 3/3
85  turn1 ACTED 3/3  EMPTY 0  ERR 0  |  turn2 OK 3/3
```

诚实边界：这套 A/B 覆盖的是**简单 shell 动手轮 + 工具结果回灌轮**。
它**没有**覆盖复杂多步任务，也**没有**覆盖门②的飞书建文档那条腿
（MCP 会合桥已回滚，合成路径打不到它）。

## 复现命令

```bash
python3 scripts/zk-cursor-web/pool_consistency.py            # A+B+C
python3 scripts/zk-cursor-web/pool_consistency.py --env      # 只 C
python3 scripts/zk-cursor-web/pool_consistency_selftest.py   # 证明门会红

# 198 上跑（要 kubectl + cap 抓包）
python3 lane_task_ab.py --arms 101=cursor-web-fc-terra 82=cursor-web-fc-82-terra \
        --shots 4 --followup --tag after-envclean
```
