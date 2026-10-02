---
name: litellm-198-worker-kill-502
description: >-
  198 LiteLLM「pod 0 重启、Ready 全绿，但某些人一直 502 / 断流」的排查 runbook。
  病根是 uvicorn 父进程 5s 心跳没收到 pong 就 kill 掉 worker 子进程（pod 不重启，只在 stdout 留
  `Child process [N] died`），worker 主线程被同步计算卡死所致。含一条命令的分诊脚本
  （worker 被杀次数 vs restartCount 并排、某把 key 的 502 与被杀分钟对齐、pod 内 pre_call 卡顿实测）、
  py-spy 抓栈定位卡在哪个函数、以及 2026-10-02 v1.103.2 实例：`log_raw_request_response` 在 1.100.1
  从未生效、1.103 开始生效后对整个请求体跑脱敏正则把 worker 卡死。
  Use when 用户说「某人一直 502」「灰度版本断流」「pod 没重启但报错」「Connection closed / ReadError 很多」
  「新版本某些用户报错旧版本没事」。
---

# 198 LiteLLM：worker 被心跳杀 ⇒ 502

> 实证：**2026-10-02** v1.103.2 灰度（254 把 key 走 gray），buyitian 的 cursor key 2h **72 次 502**，
> gray 每 pod 3h worker 被杀约 **38 次**；生产 1.100.1 同期 **0 次**。全量回退后定因、修复、回归。
> 凭据：memory `reference_198_direct_ssh`。

## 0. 先认形状：为什么常规尺子全是绿的

| 尺子 | 读数 | 为什么是假绿 |
|---|---|---|
| `kubectl get po` restartCount | 0 | 被杀的是 pod **里面**的 uvicorn worker 子进程，容器 PID 1 没死 |
| Ready | true | 2 个 worker 里另一个还活着，readiness 照样答 |
| SpendLogs 失败行 | 11 条（实际 502 有 72 次） | 在途请求在写账前就随 worker 一起没了 |
| proxy 日志 grep `Traceback` | 一屏 | 都是常驻噪音（`model isn't mapped` 等），与此无关 |

**唯一真信号**在 proxy 容器 stdout：

```
INFO:     Waiting for child process [N]
INFO:     Child process [N] died
INFO:     Started server process [M]
```

机制（uvicorn 0.51 `supervisors/multiprocess.py`）：父进程每 0.5s 对每个 worker `ping`，
`timeout_worker_healthcheck`（默认 **5s**，两版都是 5）内没收到 `pong` 就 `kill` 重启。
pong 线程是 daemon 线程，但它要拿 GIL —— **worker 主线程被同步 Python 代码占住 >5s，pong 就回不去**。

## 1. 一条命令分诊

```bash
./scripts/litellm-198-worker-kill-triage.py                      # 生产+gray，近 3h：逐 pod 被杀次数
./scripts/litellm-198-worker-kill-triage.py --sid <12位>         # 某把 key 的 502 与被杀分钟对齐
./scripts/litellm-198-worker-kill-triage.py --probe --lane gray  # pod 内实测 pre_call 卡顿
```

- `sid` = `sha256(明文key)[:12]`，也是 nginx gray 日志里的 `sid=`；不用明文：
  `select left(token,12) from "LiteLLM_VerificationToken" where key_alias='...'`。
- 退出码：0 绿 / 1 有被杀或卡顿 ≥5s / **2 量具自己坏了**（probe 没出数、或 502 对不上被杀但 pod 比 502 新）。
- ⛔ **`0/N 命中` 不是「无关」**：pod 被 rollout 换过，旧 pod 的 kill 记录跟着没了。脚本会比 pod
  启动时间与最早 502，晚于 502 就退 2，结论只能写「无法对齐」。
- ⚠️ pod 日志只留几小时（memory `feedback_pod_log_window_slides_never_read_it_as_a_trend`），事故后尽快跑。

2026-10-02 实测对齐（事故当时）：buyitian 19 个有 502 的分钟，**每一个都有 worker 被杀**
（16:34 / 17:22–17:32 / 17:38–17:44）。

## 2. 卡在哪：py-spy 抓栈（只在 gray 或测试流量上做）

容器里没有 py-spy，要在**节点**上对 worker 的宿主 PID 抓。gray 钉在 standby `10.68.13.225`。

```bash
# 本机：从 PyPI 取 py-spy 0.4.1 manylinux x86_64 wheel，按 PyPI 公布的 sha256 校验后解出二进制
# 传到 225 的 /root/l3/pyspy/py-spy（0700）
# 找 gray worker 宿主 PID：cgroup 路径里带 pod uid（'-' 写成 '_'）
for pid in $(pgrep -f 'multiprocessing.spawn'); do grep -oE 'pod[0-9a-f_]{36}' /proc/$pid/cgroup | head -1 | sed "s/^/$pid /"; done
/root/l3/pyspy/py-spy dump --pid <PID> --nonblocking      # 看 MainThread 栈顶
```

**判据**：MainThread 栈顶不在 `asyncio/runners.py` / `selectors` 而在某个同步函数里，且连续几次 dump 不变。
2026-10-02 抓到的：

```
redact_string (secret_redaction.py:92)
_redact_string (litellm/_logging.py:73)
pre_call (litellm_logging.py:1310)
async_response_api_handler (llm_http_handler.py:2927)
... run_async_fallback / _maybe_run_weighted_failover × 8 层
```

⚠️ 自己造合成请求复现**不一定能复现**：2026-10-02 用 4 个 500KB 大请求、正常上游，0 次被杀。
真实触发条件是**上游失败 → 兜底链多跳**，每跳各跑一次 pre_call。用 §1 `--probe`（直接调 `pre_call`
6 并发 × 8 跳）才是能读出红的尺子。

## 3. 2026-10-02 实例：`log_raw_request_response`

| | 1.100.1（生产） | 1.103.2（gray） |
|---|---|---|
| config 值 | `true` | `true`（照抄生产） |
| 代码读法 | 模块加载时 `from litellm import log_raw_request_response` **按值拷贝**默认 False | 读活值 `litellm.log_raw_request_response` |
| 实际生效 | **从未生效** | 生效 |
| 500KB 请求单次 `pre_call` | 0.000s | **0.29s**（全耗在 `redact_string`） |
| 6 并发 × 8 跳事件循环最长卡 | 0.00s | **5.32s** ≥ 5s ⇒ 被杀 |

**它是干嘛的**：每次转上游前，把请求拼成一条可直接运行的 curl 命令、脱敏后存进该请求的 metadata
`raw_request`，给排查「是我们发错了还是上游错了」用。
**和界面上的 Request & Response 无关** —— 那个走 `store_prompts_in_spend_logs`（两版都开着，SpendLogs
`proxy_server_request` / `response` 列）。实测生产 1h 6801 行：6481 存了请求、6350 存了回复、
`raw_request` **0 行** ⇒ 没有任何消费者在用它。

**修复**：gray config `log_raw_request_response: false`（= 旧版实际行为）。
修后同一 probe：0.000s / 0.00s；全套回归新旧一致；worker 被杀 0。
⚠️ **转正 prod 时 prod 的 config 也必须带这一行**，否则全量用户都会撞上。

## 4. 通用排查顺序（不止这一个开关）

1. 跑 §1 不带参数 —— 有没有 worker 被杀？哪条 lane？生产有没有？（**只有新版有 ⇒ 版本差异**）
2. `--sid` 对齐报错人的 502 —— 对得上才说是这个病。
3. py-spy 抓栈 —— 找卡住的同步函数。**不许**从「看起来 CPU 重」的代码推断，必须栈里真看到。
4. 对新旧两版同一个函数做同输入计时（§1 `--probe` 的写法）—— 有数量级差才算定因。
5. 查这个函数为什么在新版被走到：开关语义变了 / 新增调用点 / 输入变大。
   1.100.1 → 1.103.2 新增 9 处 `redact_string` 调用（含全局日志 Filter），这次是 pre_call 那处。

## 5. 回滚与止血

- 先止血：指名灰度的人逐把 `gray-key-route.sh force-prod`（明文从飞书表现取，用完 shred）；
  多人就 `gray-global-rollback.sh`（单程票，本轮 run 关闭）。
- gray 上改配置走 `prepare-values.py` 冻结 → 删除集/形状门禁 → `helm upgrade`，不手改 CM。
- 做阳性对照时 `helm rollback` 到旧 revision 可能报 `no ConfigMap ... found`（chart 的内容寻址 CM
  已被上次 upgrade 删掉）⇒ 先从 `cm-backup/` 重建那个 CM 再 rollback。

## 6. 灰度门禁要补的

**「worker 被杀次数 = 0」必须进灰度观察项**。这次 3 个多小时里所有门禁都绿，就是因为只看 pod 重启。
放量每一档结束前跑一次 §1，任何被杀 ≥1 即停。

相关：`litellm-gray-key-rollout`、`litellm-198-latency-triage`、`patches/v1.103.2/README.md`。
