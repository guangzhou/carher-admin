#!/usr/bin/env python3
"""
LiteLLM 模型稳定性 主动探针 (198)
==================================

它解决什么问题
--------------
被动指标只能看到「有人用过」的腿。一条没人打的腿是死是活，被动指标沉默。
这个探针按固定节奏主动打一发最小推理，把沉默的腿也变成可观测的。

为什么按 model_id 打而不是按模型组打
------------------------------------
打模型组 = 打池子，路由器会挑一条健康的腿给你，于是整池只要有一条活的就永远绿 ——
这是假绿，判不出单个成员死活。
litellm/proxy/route_llm_request.py 第 553 行：

    elif data["model"] in router_model_names or llm_router.has_model_id(data["model"]):
        return getattr(llm_router, f"{route_type}")(**data)

即把 deployment 的 model_info.id 当 model 传进去，会被钉死在那一条 deployment 上，
不过池、不走 fallback、也不被 cooldown 过滤（正是探针要的：能看出一条被拉黑的腿
有没有自己恢复）。
这个分支在**路由层**，不受 key 的 models 白名单影响 —— 实测 models=[] 的探针 key
一样能打中（2026-09-13 八发全 200）。

⚠️ id 必须来自 master key 拉的 /model/info。受限 key 拉到的是删减过的视图
（878 条 vs master 的 1229 条）且 id 形状不同，拿那个去打会全数 400
ProxyModelNotFoundError —— 这个坑踩过一次。

探针流量怎么跟真实流量区分开
----------------------------
指标里 requested_model 就是传进去的 id 原文。
不能用正则区分：id 没有统一形状（`acct-115-image-2` / `zk-81-image-2-zero` /
`converge/bge-m3` / `kimi-proxy` 各不相同），而且真实模型组名里也有带 `/` 的
（`BAAI/bge-m3`、`google/lyria-3-pro-preview`），按 `.*/.*` 排除会误伤真实流量。
实测：id 集合(726) ∩ 模型组名集合(296) = 空集。所以正确做法是按**集合差**排除，
看板侧用探针自己暴露的目标集去 unless：

    sum(rate(litellm_proxy_total_requests_metric_total[5m]))
      unless on(requested_model)
      label_replace(max by (model_id)(litellm_probe_state),
                    "requested_model", "$1", "model_id", "(.*)")

这样拓扑漂了，排除集合跟着漂，不用手工维护名单。

为什么按「上游端点」收敛而不是逐落点探
--------------------------------------
1229 个落点背后只有 102 个上游端点，其中 50 个是 ChatGPT 账号、24 个 zerokey 账号，
全是烧订阅额度的。逐落点全探会把账号额度烧穿，探针自己就会把池子打挂。
一个账号探一次就知道它活没活，没必要为它挂的 26 个模型名各探一次。
收敛后 82 个目标，周期 = 轮间隔(30~45 分钟随机) + 单轮耗时(串行，约 14 分钟)
⇒ **每账号每天约 22~33 发**（2026-09-20 从固定 15 分钟先放宽到固定 30 分钟，
当天又改成 30~45 分钟随机并串行化，理由见下面 PROBE_INTERVAL_MIN 那段注释）。
⚠️ 算每天几发时别只拿间隔当分母：串行化之后单轮耗时不再可忽略（原来 92s，现在约
14 分钟），漏掉它会把密度算高 1/3。

怎么不让探针自己长得像个扫描器
------------------------------
上游（ChatGPT / Cursor / xAI 这些）看到的不是「我们的监控」，是一个账号上的请求流。
**固定节奏 + 固定并发 + 固定文本 = 一眼可辨的机器指纹**，风控不需要理解意图就能命中。
所以四件事同时做，缺一件都会把指纹留下来：

  1. **轮间隔随机** `PROBE_INTERVAL_MIN..MAX`（30~45 分钟均匀抽）。固定 1800s 的话，
     每个账号每天在同样的分钟数上被打 48 次，这是最刺眼的一条。
  2. **串行，不并发**（`PROBE_CONCURRENCY` 已删，恒 1）。原来 8 并发 = 82 个目标在 92s
     内打完，对上游是一个短促的脉冲；现在摊到约 20 分钟。
  3. **轮内顺序每轮重洗** + 每发之间随机停 2~10s。顺序不洗的话，虽然并发是 1，
     但同一个账号每轮仍然固定落在第 N 分钟。
  4. **prompt 从 10 条里随机抽**（`PING_PROMPTS`），不是每次都 "ping"。

⚠️ 这四件事**不降低覆盖**：每轮仍然把全部端点探一遍。少探一部分端点会让
「连续两轮探不通」的语义坏掉（某端点这轮没被选中，指标停在陈旧值上，
告警看到的是上一轮的绿），那是拿假绿换来的安静，不接受。

⚠️ prompt 随机化只动 `messages` 里那句话。**body 的其余形状一个字段都不能动** ——
尤其不许补 `reasoning_effort`：真实客户端不发它，它必须由模型行的 `litellm_params`
提供，探针一补，「配置漏了档位」这类红就被构造性地屏蔽掉（2026-09-01 实测踩过）。

代表落点挑错会全盘假红
----------------------
第一轮实测：给 26 个 ChatGPT 账号挑的代表落点是 gpt-5.4，结果整片 400
`The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.`
—— 这是探针选错了模型，不是账号挂了。同理 max_tokens=1 会让部分上游报
`Could not finish the message because max_tokens ... was reached`。
所以：
  1. max_tokens 给 16，不给 1；
  2. 每个端点保留多个候选落点，探到 400/404 时**当轮换下一个候选**，
     而不是判它挂。全部候选都 400 才算配置/选型问题。
  3. 401/403 单独成一态（凭据），跟 5xx（上游挂）分开。

⚠️ 为什么按状态码分类而不是按错误文本
--------------------------------------
生产上挂着 error_sanitize 回调，所有上游错误在出厂前被改写成
`{"error":{"message":"API 异常 (req: <id>)"}}`，真正的原因只落在 pod 日志里。
所以探针拿到的 body 里没有任何可判据的文本 —— 按 "is not supported" 之类的
关键词分类**永远不会命中**（第二轮实测换候选 0 次，就是踩了这个）。
状态码没被改写，是探针手上唯一可信的判据。
真正的错误原因要人工查时，用 body 里的 req id 去 pod 日志里 grep
`error_sanitize: masked req=<id>`。

哪些端点探不了
--------------
34 个端点只剩图像/embedding 落点（acct-115~162、zero-81~121 这批，与账号池
50→25 暂停那批对得上）。拿 chat/completions 打图像模型必然 400，那是**假红**不是故障。
按 model_info.mode 过滤掉，并用 litellm_probe_unprobeable_endpoints 明示
「这些端点没在监控里」，而不是让它们静默消失。
"""

import os
import re
import json
import time
import random
import logging
import urllib.request
import urllib.error
from collections import defaultdict
from urllib.parse import urlparse

from prometheus_client import start_http_server, Gauge, Counter, Histogram

log = logging.getLogger("probe")

PROXY_BASE = os.environ.get("PROXY_BASE", "http://litellm-proxy:4000").rstrip("/")
PROBE_KEY = os.environ["PROBE_KEY"]
# 拓扑必须用 master key 拉，受限 key 拿到的是删减视图（见模块 docstring）
ADMIN_KEY = os.environ.get("ADMIN_KEY") or PROBE_KEY
# 轮间隔：30~45 分钟之间均匀随机抽，不是固定值。
# 沿革：900（15min，每账号每天 96 发，太密）→ 1800 固定 → 30~45 随机。
# 为什么必须随机：固定 1800s 会让每个账号每天在同样的分钟数上被打，加上原来的
# 8 并发和固定 "ping" 文本，三件事叠起来就是一个不需要理解意图就能命中的机器指纹。
#
# ⚠️ 下游尺子按 **MAX** 标定，不是按均值 —— 按均值标会让「抽到 45 分钟那一轮」
# 每次都假报。三处耦合（只改这里不改那里不会报错，只会开始说谎）：
#   - llm-probe-stale 的 gt 阈值：算法 = MAX + 单轮耗时，再留两轮余量。
#     单轮耗时随串行化涨到约 1200s（82 个目标 × 平均 6s 停顿 + 请求本身），
#     2700 + 1200 ≈ 3900s 是正常最大间隔，阈值取 7800s（≈ 连续漏两轮以上）。
#   - llm-probe-endpoint-down / -auth / -misconfig 的 min_over_time(...[W])：
#     W 编码的是「连续两轮」，必须 ≥ 2×MAX + 单轮 = 6600s ⇒ 取 115m。
#     退化成装不下两个采样点时，「连续两轮才报」会静默变成「一轮就报」。
#   - 上面三条的 relativeTimeRange.from 要盖住 W。
# 机器校验：observability-preflight.py 的 CADENCE 腿，改完必跑。
INTERVAL_MIN = int(os.environ.get("PROBE_INTERVAL_MIN", "1800"))   # 30 分钟
INTERVAL_MAX = int(os.environ.get("PROBE_INTERVAL_MAX", "2700"))   # 45 分钟
TOPO_REFRESH = int(os.environ.get("TOPO_REFRESH", "3600"))    # 1 小时重算一次拓扑
# 90 不是拍的：实测 cursor/grok-4.6 单发 67~72s，60s 会把它判成假红
REQ_TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "90"))
SLOW_SECONDS = float(os.environ.get("PROBE_SLOW_SECONDS", "20"))
# 发与发之间的随机停顿（秒）。串行 + 这个停顿 = 一轮摊到约 20 分钟，
# 而不是 92s 内一个脉冲。并发已取消（原 PROBE_CONCURRENCY=8），恒串行。
GAP_MIN = float(os.environ.get("PROBE_GAP_MIN", "2"))
GAP_MAX = float(os.environ.get("PROBE_GAP_MAX", "10"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9109"))

# 10 条探针问句，每发随机抽一条。全部满足三个条件：
#   · 极短（省额度，也省 token 计费）；
#   · 语义上是个真问题，不是控制字符串 —— 上游侧看着像人；
#   · 答案不参与判据。判据只有状态码（生产挂着 error_sanitize，body 里的
#     错误文本已被抹成 "API 异常 (req: <id>)"，文本不可判）。所以换文本
#     不会动摇任何一条告警的语义。
PING_PROMPTS = (
    "ping",
    "hi",
    "say ok",
    "1+1=?",
    "what day is it?",
    "reply with one word",
    "are you there?",
    "name a color",
    "count to three",
    "short greeting please",
)

# 探针状态。429 单独成一态 —— 被限流不等于挂了，混在一起会把「额度用完」
# 误报成「上游故障」，是两种完全不同的处置。
STATE_UP = 0
STATE_DEGRADED = 1      # 通了但慢
STATE_RATELIMITED = 2   # 429
STATE_DOWN = 3          # 真挂
STATE_RETIRED = 4       # 所有候选落点都被上游 400 拒掉（配置/选型问题，不是 outage）
STATE_AUTH = 5          # 401/403：凭据过期/被封，跟"上游挂了"是两种处置

# ⚠️ 状态指标的标签里**不能**放 model_id/model_group。
# 探针会换候选落点，一换就换了标签组合 = 新开一条时间序列，旧那条变陈旧值留在原地。
# 后果有两个，都很难看：
#   1. 幽灵告警 —— `min_over_time(litellm_probe_state[115m]) == 4` 会把"修好之前那一轮"
#      的残影当成还在故障，实测一次改动后凭空多出 25 条命中；
#   2. cardinality 只增不减，每次轮换都留一条永不复用的序列。
# 所以按 Prometheus 的惯例拆成两个指标：状态只按端点，"当前探的是谁"单独用 info 指标，
# 看板/告警要显示落点时用 `* on(endpoint) group_left(model_id, model_group) litellm_probe_target_info`。
LBL = ["endpoint", "kind"]

g_state = Gauge("litellm_probe_state",
                "探针判定 0=正常 1=慢 2=被限流 3=挂了 4=配置/选型问题 5=凭据失效", LBL)
g_up = Gauge("litellm_probe_up", "1=这一发探针成功拿到回复", LBL)
g_latency = Gauge("litellm_probe_latency_seconds", "最近一次探针耗时(秒)", LBL)
g_last_ok = Gauge("litellm_probe_last_success_timestamp_seconds",
                  "最后一次成功的 unix 时间戳", LBL)
c_total = Counter("litellm_probe_attempts_total", "探针发出总数", LBL + ["result"])
g_target_info = Gauge("litellm_probe_target_info",
                      "恒为 1；标签说明该端点当前探的是哪个落点",
                      LBL + ["model_group", "model_id"])
h_lat = Histogram("litellm_probe_duration_seconds", "探针耗时分布", ["kind"],
                  buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90))
g_targets = Gauge("litellm_probe_targets", "本轮探针目标数", ["kind"])
g_round_ts = Gauge("litellm_probe_last_round_timestamp_seconds", "最近一轮结束时间")
g_round_dur = Gauge("litellm_probe_round_duration_seconds", "最近一轮耗时")
g_topo_deploy = Gauge("litellm_probe_known_deployments", "路由器里的落点总数")
g_topo_ep = Gauge("litellm_probe_known_endpoints", "路由器里的上游端点总数")
g_unprobeable = Gauge("litellm_probe_unprobeable_endpoints",
                      "只剩图像/embedding 落点、chat 探针覆盖不到的端点数")
g_topo_fail = Counter("litellm_probe_topology_failures_total", "拉拓扑失败次数")

# 挑代表落点的优先级：先按档位从低到高（别去烧强模型那个额度桶），
# 再优先带 "/" 的 id。
#
# 2026-09-20：把 "5.3" 提到 "5.4" 前面。旧表 "5.4" 在索引 0，于是每次重算拓扑，
# 凡是手上有 gpt-5.4 的端点都先挑它当代表（实测 82 个端点里有 53 个）。
# zerokey 那批（zk-NNN-gpt-5.3，实测 3.5~4.6s 全 200）现在首发命中。
TIER_HINTS = ["5.3", "mini", "flash", "haiku", "lite", "small", "air", "5.4", "auto"]

# 负向词：mode 字段没标全，这些名字一看就不是 chat，选中了必然假红。
NEG_HINTS = ("image", "lyria", "veo", "imagen", "sora", "video", "music",
             "tts", "whisper", "embed", "bge", "rerank", "moderation")

# 「上游明确不支持」的落点：拿它当代表必然 400，靠 probe_one 轮换能兜住结果，
# 但轮换结果只活在内存里，TOPO_REFRESH 一到就清空 —— 于是每次重算拓扑，
# 每个账号都白烧一发注定失败的请求。写进这里让打分直接把它踢到最后。
#
# 判据是实测（2026-09-20，chat 与 responses 两个端点、max_tokens 16 与 256 各打过）：
#   openai/chatgpt-gpt-5.4              -> 400 "The 'gpt-5.4' model is not supported
#                                              when using Codex with a ChatGPT account."
#   openai/chatgpt-gpt-5.3-codex-spark  -> 400 同上，只是模型名换成 gpt-5.3-codex-spark
# 注意第二条：id 后缀写的是 `-gpt-5.3-codex`，上游真名却是 `...-codex-spark`，
# 所以必须按 litellm_params.model 匹配，只看 id 名字会漏。
#
# ⚠️ 这是「这个上游不接受这个模型」，不是「这个模型不能探」：同名落点挂在别的
# 端点上照样是好的（wangsu7-gpt-5.3-codex 实测 200）。所以匹配串带上了
# chatgpt- 前缀，只作用于 ChatGPT 账号池。
UNSUPPORTED_UPSTREAM_MODELS = (
    "chatgpt-gpt-5.4",
    "chatgpt-gpt-5.3-codex-spark",
)
CHAT_MODES = {"chat", "responses", None}
CANDIDATES = int(os.environ.get("PROBE_CANDIDATES", "4"))


def _http(method, path, key, body=None, timeout=30):
    req = urllib.request.Request(
        PROXY_BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def endpoint_of(params):
    """把一个落点归到它的『上游端点』。同一个账号/网关的所有模型共用一个端点。"""
    ab = params.get("api_base")
    if ab:
        host = urlparse(ab).netloc or ab
        # chatgpt-acct-175.litellm-product.svc.cluster.local:4000 -> chatgpt-acct-175
        m = re.match(r"([a-z0-9-]+)\.litellm-(product|dev)\.svc", host)
        if m:
            return m.group(1)
        return host
    return "(官方)" + str(params.get("model", "")).split("/")[0]


def kind_of(ep):
    if re.match(r"chatgpt-acct-\d+", ep):
        return "chatgpt账号池"
    if ep.startswith("zero-cursor"):
        return "zerokey-cursor"
    if re.match(r"zero-\d+", ep):
        return "zerokey账号"
    if "kiro" in ep:
        return "kiro"
    if "copilot" in ep:
        return "copilot2api"
    if "cursor" in ep or "shim" in ep or "9router" in ep or "router9" in ep:
        return "cursor网关"
    if "sub2api" in ep or "grok" in ep:
        return "sub2api"
    if ep.startswith("(官方)"):
        return "官方API"
    return "外部网关"


def build_targets():
    """拉路由表，按上游端点收敛成探针目标，每个端点挑一个代表落点。"""
    st, body = _http("GET", "/model/info", ADMIN_KEY, timeout=300)
    if st != 200:
        raise RuntimeError("拉 /model/info 失败 HTTP %s: %s" % (st, body[:200]))
    data = json.loads(body)["data"]
    g_topo_deploy.set(len(data))

    all_eps = set()
    by_ep = defaultdict(list)
    for m in data:
        p = m.get("litellm_params") or {}
        mi = m.get("model_info") or {}
        ep = endpoint_of(p)
        all_eps.add(ep)
        # 图像/embedding 落点拿 chat 打必然 400，那是假红不是故障，直接不收
        if mi.get("mode") in CHAT_MODES and mi.get("id"):
            by_ep[ep].append((mi["id"], m.get("model_name") or "", p))

    g_topo_ep.set(len(all_eps))
    g_unprobeable.set(len(all_eps) - len(by_ep))

    targets = []
    for ep, legs in by_ep.items():
        def score(x):
            name = (x[0] + " " + x[1] + " " + str(x[2].get("model", ""))).lower()
            upstream = str(x[2].get("model", "")).lower()
            neg = 1 if any(h in name for h in NEG_HINTS) else 0
            # 已知上游不支持的落点排最后：仍留在候选里（万一哪天上游支持了，
            # 轮换还能用上），但绝不当首选，免得每轮白烧一发注定的 400。
            unsupported = 1 if any(h in upstream
                                   for h in UNSUPPORTED_UPSTREAM_MODELS) else 0
            tier = next((i for i, h in enumerate(TIER_HINTS) if h in name),
                        len(TIER_HINTS))
            return (neg, unsupported, tier, 0 if "/" in x[0] else 1, x[0])

        # 留多个候选：代表落点可能被上游拒（如 Codex 账号不支持 gpt-5.4），
        # 探到 400 时当轮换下一个，而不是把端点判成挂。
        # ⚠️ 必须按 model_id 去重：legs 是按 (id, 模型组名) 展开的，同一个 id 会挂在
        # 多个组名下（acct-175 有 26 条腿但只有 8 个不同 id）。不去重的话
        # "换下一个候选" 会换成同一个 id 的副本，换 4 次等于原地踏步。
        cands, seen = [], set()
        for x in sorted(legs, key=score):
            if x[0] in seen:
                continue
            seen.add(x[0])
            cands.append(x)
            if len(cands) >= CANDIDATES:
                break
        mid, group, _ = cands[0]
        targets.append({"endpoint": ep, "kind": kind_of(ep),
                        "model_group": group, "model_id": mid, "legs": len(legs),
                        "candidates": [(c[0], c[1]) for c in cands]})
    targets.sort(key=lambda t: (t["kind"], t["endpoint"]))
    return targets


def classify(status, elapsed):
    """只认状态码（body 被 error_sanitize 抹了，见模块 docstring）。
    返回 (state, result, 是否该换个候选落点重试)"""
    if status == 200:
        return ((STATE_DEGRADED, "slow", False) if elapsed > SLOW_SECONDS
                else (STATE_UP, "ok", False))
    if status == 429:
        return STATE_RATELIMITED, "ratelimited", False
    if status in (400, 404, 422):
        # 探针发的是最小合法请求，上游还 400 —— 多半是这个落点本身不接受，
        # 不是上游挂了。换个候选试，全试完还 400 才算配置/选型问题。
        return STATE_RETIRED, "rejected", True
    if status in (401, 403):
        return STATE_AUTH, "auth", False
    return STATE_DOWN, "down", False


def _send(model_id):
    payload = {
        "model": model_id,
        # 每发随机抽一条，别让上游看到一串一模一样的 "ping"（见模块 docstring
        # 「怎么不让探针自己长得像个扫描器」）。判据只有状态码，所以换文本
        # 不影响任何告警的语义。
        "messages": [{"role": "user", "content": random.choice(PING_PROMPTS)}],
        # 不能给 1：部分上游会报 "max_tokens ... was reached" 硬 400，那是假红
        "max_tokens": 16,
        "temperature": 0,
        # 打上标签，方便事后在 SpendLogs 里把探针流量整段捞出来/排除掉
        "metadata": {"tags": ["stability-probe"]},
    }
    t0 = time.time()
    try:
        status, body = _http("POST", "/v1/chat/completions", PROBE_KEY,
                             payload, timeout=REQ_TIMEOUT)
    except Exception as e:            # 超时 / 连接失败
        status, body = 0, "probe exception: %r" % (e,)
    return status, body, time.time() - t0


def probe_one(t):
    cands = t.get("candidates") or [(t["model_id"], t["model_group"])]
    tried = []
    for mid, grp in cands:
        status, body, elapsed = _send(mid)
        state, result, retry_other = classify(status, elapsed)
        tried.append(mid)
        if not retry_other:
            break
        log.info("%-22s 候选 %s 被拒(HTTP %s)，换下一个", t["endpoint"], mid, status)
    else:
        # 所有候选都被 400 拒 —— 这才算这个端点真的配置/选型有问题
        result = "all-rejected"

    # 把探成功的候选记下来，下一轮直接从它开始，别每轮都重踩一遍
    if state in (STATE_UP, STATE_DEGRADED) and mid != cands[0][0]:
        t["candidates"] = [(mid, grp)] + [c for c in cands if c[0] != mid]
    t["model_id"], t["model_group"] = mid, grp

    labels = (t["endpoint"], t["kind"])
    # 换了候选就把上一条 info 序列删掉，别让旧落点名留在指标里当陈旧值
    prev = t.get("info_labels")
    if prev and prev != (grp, mid):
        try:
            g_target_info.remove(t["endpoint"], t["kind"], prev[0], prev[1])
        except KeyError:
            pass
    g_target_info.labels(*labels, grp, mid).set(1)
    t["info_labels"] = (grp, mid)

    g_state.labels(*labels).set(state)
    g_up.labels(*labels).set(1 if state in (STATE_UP, STATE_DEGRADED) else 0)
    g_latency.labels(*labels).set(elapsed)
    c_total.labels(*labels, result).inc()
    h_lat.labels(t["kind"]).observe(elapsed)
    if state in (STATE_UP, STATE_DEGRADED):
        g_last_ok.labels(*labels).set(time.time())
    if state != STATE_UP:
        log.info("%-22s %-14s %-16s HTTP=%s %.1fs id=%s %s",
                 t["endpoint"], t["kind"], result, status, elapsed, mid,
                 (body or "")[:160])
    return state


def run_round(targets):
    t0 = time.time()
    kinds = defaultdict(int)
    for t in targets:
        kinds[t["kind"]] += 1
    for k, v in kinds.items():
        g_targets.labels(k).set(v)

    # 串行 + 每轮重洗顺序 + 发间随机停顿。三件事都是为了不给上游留机器指纹：
    #   · 串行取代原来的 8 并发：82 发不再挤在 92s 里成一个脉冲；
    #   · 重洗顺序：否则并发虽是 1，同一个账号每轮仍固定落在第 N 分钟；
    #   · 随机停顿：把一轮摊到约 20 分钟。
    # 注意仍然是**全量**一轮探完，不抽样 —— 抽样会让「连续两轮探不通」读到陈旧值。
    q = list(targets)
    random.shuffle(q)
    tally = defaultdict(int)

    for i, t in enumerate(q):
        tally[probe_one(t)] += 1
        if i != len(q) - 1:
            time.sleep(random.uniform(GAP_MIN, GAP_MAX))

    dur = time.time() - t0
    g_round_dur.set(dur)
    g_round_ts.set(time.time())
    log.info("一轮完成 %d 个目标 %.1fs | 正常=%d 慢=%d 限流=%d 挂=%d 配置/凭据=%d",
             len(targets), dur, tally[STATE_UP], tally[STATE_DEGRADED],
             tally[STATE_RATELIMITED], tally[STATE_DOWN],
             tally[STATE_RETIRED] + tally[STATE_AUTH])


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(METRICS_PORT)
    log.info("探针启动 proxy=%s 轮间隔=%d~%ds(随机) 串行 发间停顿=%.0f~%.0fs",
             PROXY_BASE, INTERVAL_MIN, INTERVAL_MAX, GAP_MIN, GAP_MAX)

    targets, topo_at = [], 0.0
    while True:
        try:
            if time.time() - topo_at > TOPO_REFRESH or not targets:
                new = build_targets()
                targets, topo_at = new, time.time()
                log.info("拓扑已刷新：%d 个上游端点可探，%d 个只剩图像/embedding 探不了",
                         len(targets), int(g_unprobeable._value.get()))
            run_round(targets)
        except Exception:
            # 拉拓扑失败时保留上一轮的 targets 继续探，不要因为控制面抖动就瞎了
            g_topo_fail.inc()
            log.exception("本轮失败")
        nap = random.uniform(INTERVAL_MIN, INTERVAL_MAX)
        log.info("下一轮 %.0fs 后（%d~%d 随机）", nap, INTERVAL_MIN, INTERVAL_MAX)
        time.sleep(nap)


if __name__ == "__main__":
    main()
