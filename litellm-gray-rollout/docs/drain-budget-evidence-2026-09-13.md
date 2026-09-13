# 排水预算实测 —— p100 `upstream_response_time` 与截断成本（runbook line 145～147）

chart `values.yaml` 把排水写成一条推导链而不是三个旋钮：

```
terminationGracePeriodSeconds >= drain.preStopSeconds + drain.streamDrainSeconds
nginx proxy_read_timeout      == drain.streamDrainSeconds
```

当前值：`preStopSeconds=30`、`streamDrainSeconds=570`、`grace=600`。
values.yaml 自己注明 **570 是按 grace 600 反推的预算上限，不是实测的最长 SSE 时长**，
并要求每次 run 从 access log 量出 p100。这份文件就是那次实测。

---

## 1. 结论（先给答案）

**排水做不到"零截断"，差了四个数量级，必须显式接受截断。**

| 项 | 值 |
|---|---|
| 预算 `streamDrainSeconds` | **570 s** |
| 实测 p100 `upstream_response_time`（/pro 全体） | **81,665 s ≈ 22.7 小时** |
| 实测 p99（`responses` 类） | **1,442 s ≈ 24 分钟** |
| 超 570 s 的请求占比 | **0.0940%**（1,169 / 1,243,452） |
| 被截断的主要消费者 | **Codex Desktop / codex-tui**，打 `/pro/v1/responses` |

"抬高 grace 直到覆盖 p100"这条路**不可行**：grace 要设到 22.7 小时，
滚动更新会挂着 Terminating pod 一整天，比截断本身危害大得多。
⇒ 走 runbook line 147 的第二条分支：**接受截断，写明比例和受影响消费者**。

---

## 2. 量具

| 项 | 值 |
|---|---|
| access log | `/var/log/nginx/zkreq.log` + `zkreq.log.1`（198） |
| log_format | `zkreq`，字段 6 = `$upstream_response_time`，字段 8 = `$request_uri` |
| 窗口 | 2 天（今日 + 昨日轮转），`/pro` 有效样本 **1,243,452** |
| `uri_class` | 用 `render-production-nginx.py` 里那份 `map $uri $uri_class` 的**同一组正则**，对 `$request_uri` 匹配（正则均无锚点，加不加 `/pro` 前缀等价） |
| 重试多值 | `rt` 为逗号分隔时取 **max**（排水关心的是单条上游连接被占用多久） |

⚠️ 全程只输出聚合数值，**不打印任何原始 URI** —— query string 里可能带 key。

### 2.1 两把坏尺子（都当场换掉了）

| 坏尺子 | 症状 | 换成 |
|---|---|---|
| `sudo wc -l < file` | 重定向由调用用户（cltx）打开，不经 sudo ⇒ 读不到，报出假的 "1 行"，而文件实际 16,463 行 | `sudo sh -c 'wc -l file'` |
| 只 grep `nginx.conf` + `conf.d/` 找 `access_log` | 漏掉 `sites-enabled/`，读出"只有 combined 格式、没有 upstream 时间"⇒ 差点得出"这一项测不了" | `grep -r /etc/nginx/`，找到 `sites-enabled/` 里两条 `access_log … zkreq` |

第二条尤其值得记：**它的失败方向是"报告本项无法测量"**，
而不是报错——如果不追一下"配置里定义了 `zkreq` 格式，为什么没有对应的日志文件"，
这一项就会以"缺数据"的名义被跳过，而数据其实一直在。

---

## 3. 实测数据（窗口 = 2 天，单位秒）

| uri_class | count | p50 | p95 | p99 | **p100** |
|---|---:|---:|---:|---:|---:|
| other | 1,149,148 | 0.01 | 1.25 | 2.15 | 60.70 |
| **responses** | 72,025 | 9.97 | 94.07 | **1,442.19** | **81,665.42** |
| chat | 15,707 | 9.99 | 67.36 | 166.10 | 600.07 |
| messages | 5,171 | 13.38 | 94.79 | 299.93 | 902.71 |
| image | 30 | 32.91 | 53.10 | 57.11 | 58.24 |
| **ALL /pro** | **1,243,452** | 0.02 | 7.03 | 37.74 | **81,665.42** |

### 3.1 超阈分布（这才是"截断成本"的正文）

| 阈值 | 超阈条数 | 占比 | 构成 |
|---:|---:|---:|---|
| 60 s | 8,129 | 0.6537% | responses=6456 chat=917 messages=755 other=1 |
| 300 s | 1,728 | 0.1390% | responses=1585 chat=94 messages=49 |
| **570 s**（当前预算） | **1,169** | **0.0940%** | responses=1117 chat=49 messages=3 |
| 900 s | 882 | 0.0709% | responses=880 messages=2 |
| 1800 s | 667 | 0.0536% | responses=667 |
| 3600 s | 323 | 0.0260% | responses=323 |
| 7200 s | 182 | 0.0146% | responses=182 |

> 182 条跑满 2 小时以上 —— 这是一个**有厚度的尾巴，不是一条离群点**。
> 所以 81,665 s 不能当成日志 artifact 抹掉；`responses` 长流是这条链路的常态形状。

### 3.2 受影响消费者（>570 s 的请求，按 UA）

| 条数 | User-Agent（截断 40 字符） |
|---:|---|
| 271 | `Codex Desktop/0.153.4 (Windows 10.0.2620` |
| 104 | `codex-tui/0.153.4 (Windows 10.0.26200; x` |
| 76 | `Codex Desktop/0.153.4 (Windows 10.0.1904` |
| 72 | `Codex Desktop/0.154.0-alpha.6.2 (Windows` |
| 62 | `Codex Desktop/0.148.0-alpha.21 (Mac OS 1` |
| 61 | `codex-tui/0.154.0 (Windows 10.0.22631; x` |

⇒ **清一色 Codex（Desktop + TUI）的长 agent 会话**，端点是 `/pro/v1/responses`。
没有 CarHer / cursor / 其它 lane 混在里面。截断影响面是明确且单一的。

---

## 4. ⚠️ gate 措辞本身要改：`proxy_read_timeout` 不是总时长上限

runbook line 146/147 与 values.yaml 的注释都隐含「`proxy_read_timeout` 能兜住流的总长」。
**它兜不住。** nginx 的 `proxy_read_timeout` 是**两次成功读之间**的间隔上限，
不是一次请求的总时长上限。

现网证据自己就说明了这点：

| 事实 | 值 |
|---|---|
| 现网 `/pro` lane 的 `proxy_read_timeout` | **600 s**（`sites-enabled/cc.auto-link.com.cn.conf`） |
| 同一窗口实测的最长 `upstream_response_time` | **81,665 s** |

600 s 的超时下跑出 22.7 小时的请求，**不是矛盾，是定义**：SSE 只要持续有 chunk 吐出，
每次读的间隔都远小于 600 s，超时永远不触发。

这条修正会改变两件事：

1. **"把 p100 和 `streamDrainSeconds` 比大小"这个动作本身是在比两个不同的量。**
   真正决定排水时长的是**总时长**（Pod 被 SIGKILL 前在途流还要活多久），
   而 `proxy_read_timeout` 管的是**静默检测**。gate 要同时说清楚这两件事。
2. **把 `proxy_read_timeout` 调成 570 s 并不能防止截断。** 它的真实作用是：
   当上游因为 Pod 正在死而**变哑**时，nginx 在 Pod 被 SIGKILL（600 s）**之前**放弃，
   给客户端一个干净的 504，而不是在 Pod 死后收到 RST、把截断伪装成"回答突然没了"。
   对**仍在活跃吐字**的流，SIGKILL 到点就是截断，`proxy_read_timeout` 无能为力。

⇒ 现网 600 s 与模板 570 s 的差值不是笔误，而是这条契约的落点：
**nginx 必须比 Pod 先放弃（570 < 600）**，否则就会出现"nginx 还在等一个已经死掉的 Pod"。

---

## 5. 结论写进执行单的形式

| runbook 行 | 填什么 |
|---|---|
| 145 Measured p100 per `uri_class` | 见 §3 表；窗口 = `zkreq.log` + `.1`（2 天），路径 `/var/log/nginx/zkreq.log` |
| 146 Drain budget holds | `600 >= 30 + 570` ✅；`nginx proxy_read_timeout(570) == streamDrainSeconds(570)` ✅ —— ⚠️ 需把现网 600 s 改成 570 s，这是一次**真实的行为变更**，不是照抄模板 |
| 147 若 p100 超预算 | **选"显式接受截断"**：比例 **0.0940%**（1,169/1,243,452），消费者 **Codex Desktop / codex-tui @ `/pro/v1/responses`**；不抬 grace，理由见 §1 |

执行当天必须重量一次（流量形状会变），并把新的比例覆盖上去。

---

## 6. `proxy_read_timeout` 600 → 570 的代价已量出来；**时点定在窗口内，不在今天**

§5 把这条标成"真实的行为变更"但没给数。补上（同窗口，`zkreq.log` + `.1`）：

| 项 | 值 |
|---|---:|
| `/pro` 总请求 | 710,761 |
| 其中 **504** | **28**（0.0039%） |
| 28 条里 `upstream_response_time` 落在 560~610 s | **28 条，全部** |
| 最长的一条 | **600.15 s** |

⇒ **今天线上每一条 504 都是这个 600 s 超时自己在响**，没有第二个来源。
改成 570 s 之后，这 28 条的报错**早 30 秒到达**，结局不变（它们本来就失败）。

### 6.1 量不出来的那一半，必须说出口

访问日志记的是**总时长**，不是**静默间隔**，而 `proxy_read_timeout` 管的是后者（见 §4）。
所以有一类请求这份日志看不见：**今天中间静默了 570~600 s、然后又恢复吐字的**。
改完之后它们会被判 504。

能给的只是上界：只有总时长 > 570 s 的请求才**可能**有 > 570 s 的静默 —— 这类共 1,169 条，
其中静默真的越过 600 s 的是 28 条。所以 570~600 s 这条 30 s 窄带里的条数
与那 28 条**同一量级**（个位数到十几条／两天）。这是框出来的范围，不是实测值。

### 6.2 时点：窗口内、切 helm **之前**，不是今天

今天改 = 零收益的一次线上改动。这 570 存在的唯一意义是**让 nginx 比 Pod 先放手**，
而今天 Pod 在 30 s 就被 SIGKILL、nginx 等 600 s —— 这个倒挂早就存在且大得多，
把 600 降到 570 对它毫无帮助；真正修它的是把 grace 抬到 600（见
`prepare-values-prod-rehearsal-2026-09-13.md` §4.1）。

切过去之后才轮到它：那时 grace=600、nginx=600，**两个数相等就是在赛跑**。
所以顺序固定为「先改 nginx 570 → 再切 helm」，让余量从 grace 变成 600 的那一刻就在。

⚠️ 当天重量一次再改：上表的 28 条是 09-13/09-14 窗口的数，流量形状会变。
