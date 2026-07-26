# zerokey bridge testkit

从 2026-07-27 修复多轮工具循环(0/6 → 10/10)沉淀。**每个脚本都对应一个我踩过的坑**,
不是通用测试模板。

## 为什么需要它

在这之前所有 bridge 测试都是**单轮**:发一个请求、看回复。但用户报的故障是**多轮**——
模型跑一条命令(`lark-cli --help`)就停下来写"下一步需要...",多步任务全灭。
单轮测试永远复现不出来。

## 脚本

### `agentloop.py` — 真闭环(最重要)

扮演 Codex 的另一半:发请求 → 收 `custom_tool_call` → **在本机真执行** → 回灌 history
→ 循环。这就是生产拓扑(远端 bridge + 本地执行)。

```bash
python3 agentloop.py "帮我看看这个飞书文档 <URL> 用 lark-cli 搞定" 10
ZK_DUMP=/tmp/hist.json python3 agentloop.py "..." 10   # 顺便 dump history 供排查
```

⚠️ **`exec` 工具必须放在 `input[]` 里 type=`additional_tools` 的项**,不能放顶层字段。
`_req_uses_exec_tool()` 只扫 `input[]`;放错位置 → bridge 认为 tools=False → 不注入
GUIDE → 秒拒答。我第一版 harness 就是这么"复现"了一个自己造出来的 bug。

### `loop_trials.py` — 通过率

N 个循环并发跑,分类 ANSWERED / BRAKED / REFUSED / NOCMD / VAGUE。

```bash
python3 loop_trials.py 10
python3 -c "import sys;sys.path.insert(0,'.');import loop_trials as L;L.suite(4)"  # 跨任务套件
```

判定要求答案含任务的**真实数据 token**(如 `95.1`、`1380`),否则"我看了一下"也会算通过。

⚠️ **n=6 时 ±2 样本是常态**。我曾把 1/6→3/6 当成修复生效,实际那期间跑的是旧代码,
涨幅纯噪声。跨不过噪声门槛就别谈趋势。

### `corpus.py` + `refusal_detector_candidate.py` — 离线迭代拒答检测

语料全是**真实 pod 回复**。改正则后秒级出分,**不要用网络往返调正则**。

```bash
python3 refusal_detector_candidate.py     # 打分 + 列出 MISS / FALSE+
```

当前基准:语料 17/17,留出集 12/12,误报 0/20。

设计要点:光匹配否定动词会把"该配置无法直接读取环境变量"误判成拒答。必须要求否定
指向**模型自身能力**(denial+self / handoff / narration 三触发器),这样窗口才能从
200 放宽到 600 字——真实拒答以合作句开头("我可以帮你...但"),否定落在 60-160 字。

### `toolname_ab.py` — 上游工具名 A/B

名字决定 pod 注入模式(`detectShellTool` 匹配 shell/terminal/bash/exec → exec-harvest),
描述决定意愿。两者可解耦。

```bash
# 在 bridge pod 里跑(要能访问 zero-N 的集群内 DNS)
python3 toolname_ab.py zero-90,zero-92,zero-93,zero-94,zero-95
python3 toolname_ab.py zero-90,zero-92 shell_enqueue_job   # 只测一个变体
```

实测(三轮独立,28 有效样本):`enqueue_job` 25% / `run_in_terminal` 56% /
`run_shell` 61% / **`shell_enqueue_job` 90%**。

### `survey-pool-toolcall.py` — 全池能力普查

```bash
python3 survey-pool-toolcall.py     # 在 bridge pod 里跑
```

输出 CALL / TEXT / NOTOKEN 分布。`NOTOKEN` = 该 pod 漏了启动 cp,跑的是旧
`responses.js`,对带 `tools[]` 的请求一律 503。修法见上级目录
`patch-zero-pod-startup.py`。

⚠️ **并发压到 4-5**。15 路并发曾把 23/29 健康 pod 报成"不可用",据此我差点得出
"整池挂了"的错误结论。

## 通用教训

1. **先证代码在跑,再测指标**。查 `/proc/1/cmdline`,不是 grep 文件(文件可能是没人读的副本)。
2. **枚举,不要按区间抽查**。按 pod 名区间探测漏了 zero-28/50/52/129,其中 129 在
   bridge 池里是线上缺陷。
3. **能离线就离线**。拒答检测抽成语料后,迭代速度从 40s/次变成毫秒级。
4. **手搓 curl 会测出不存在的场景**。我手搓的请求没有 `environment_context`,
   模型猜 Windows 是必然的;真 Codex 一次就过。
