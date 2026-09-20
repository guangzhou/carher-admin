#!/usr/bin/env python3
"""
可观测性自检 —— 灰度升级的第 −1 天，不过不许开始切流。

它解决什么问题
--------------
2026-09-19 复盘：09-13 灰度方案落地到 09-20 共 38 个提交，其中 **11 个是在修门禁
和监控自己**（尺子坏了），不是在修被测对象。最贵的单点是
**Prometheus 六天没抓过生产**，而门禁一直在读它出的数 —— 那六天里每一次「绿灯」
都是空数据渲染出来的，之后所有修门禁的返工，根都在这里。

所以这个脚本不测业务，只测**尺子本身能不能用**。四条判据，全部可执行：

  1. LIVE     每个 scrape target 最近有数据（up==1 且 sample 新鲜）
  2. RED-ABLE 每条告警规则能被合成红触发（不是读配置说「看起来对」）
  3. DELIVER  告警真能送到人手上（判据是投递计数器，不是 HTTP 200）
  4. FAIL-RED 数据断了必须变红：禁 noDataState:OK、禁 execErrState:OK、禁 eq 阈值
  5. CADENCE  采样间隔与读它的窗口/阈值算术自洽（改间隔不改尺子 ⇒ 全员假报）

为什么「绿」必须是昂贵的
------------------------
`noDataState: OK` 的含义是「查不到数据就算通过」。一个数据源死了，
它下面所有规则集体变绿 —— 看起来比真的健康还健康。同族的坏尺子：
`eq` 阈值（浮点相等，永不触发）、`log.info` 当判据（应用装 handler 前跑的
logger 走 lastResort level=30，info 恒不可见）、`replicas>0` 当「在服务」、
`LEDGER_MAX_ITEMS` 封条数而占内存的是字节。每一个都得单独踩一次才发现。
⇒ 一条告警规则最该保证的属性是：**数据断了它必须变红。**

用法
----
  # 只读检查，任何一条不过就 exit 1（可以直接当 CI / 切流前置门禁）
  ./observability-preflight.py --rules litellm-stability.yaml --prom-config prometheus.yml

  # 加上活性检查（需要能打到 Prometheus）
  ./observability-preflight.py --rules litellm-stability.yaml \
      --prom-url http://10.68.13.242:30300/api/datasources/proxy/1

  # 只看静态那两项（不联网），适合本地跑
  ./observability-preflight.py --rules litellm-stability.yaml --static-only

  # 改过 PROBE_INTERVAL 之后必跑：校验窗口/阈值跟新间隔还自洽
  ./observability-preflight.py --rules litellm-stability.yaml --probe-script probe.py

这个脚本只读，不改任何线上对象。RED-ABLE 与 DELIVER 两项需要人工配合
（合成红要真发一发、投递要看计数器），脚本会把**该怎么做**和**判据是什么**
打出来，并把这两项标成 MANUAL 而不是假装绿。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# 数据断了却仍然算通过的取值
FAIL_OPEN_NODATA = {"OK", "NoData"}  # NoData 也不行：它不进告警，没人看得到
FAIL_OPEN_EXECERR = {"OK"}
# 浮点相等永不触发（Grafana evaluator 层；PromQL 里对整数枚举用 == 是合法的）
FORBIDDEN_EVALUATOR_TYPES = {"eq", "ne"}

# 指标族前缀 → 提供它的 scrape job。
#
# 这张表是**人工判断**，不能从规则文件推出来：`up{job="litellm-proxy"}` 死了会带走
# litellm_proxy_* / litellm_deployment_* / litellm_request_*，但这层归属关系只存在于
# 被抓的那个进程里。所以它必须显式写下来，并在加新指标族时同步 —— 本检查最大的价值
# 就是**新出现的指标族如果没人认领，会当场报红**，而不是悄悄多一个没人盯的绿灯。
FAMILY_OWNER = {
    "litellm_proxy_": "litellm-proxy",
    "litellm_deployment_": "litellm-proxy",
    "litellm_request_": "litellm-proxy",
    "litellm_probe_": "litellm-probe",
}

# 看门狗读的活性量形状：up{job="X"}（直接点名 job），或某族自己的心跳时间戳
UP_JOB_RE = re.compile(r'up\{[^}]*job\s*=\s*"([^"]+)"')
HEARTBEAT_RE = re.compile(r"\b(litellm_[a-z0-9_]*_last_round_timestamp[a-z0-9_]*)\b")
METRIC_RE = re.compile(r"\b(litellm_[a-z0-9_]+)\b")
# by (...) / without (...) 里是标签名不是指标名；引号里是 label_replace 的参数
STRIP_RE = re.compile(r'"[^"]*"|\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)')


def _exprs(rule: dict) -> list[str]:
    out = []
    for dd in rule.get("data", []) or []:
        expr = (dd.get("model") or {}).get("expr")
        if expr:
            out.append(str(expr))
    return out


def _families(rule: dict) -> set[str]:
    """这条规则读了哪些指标族。族 = FAMILY_OWNER 里的前缀，未知前缀原样返回。"""
    out: set[str] = set()
    for expr in _exprs(rule):
        cleaned = STRIP_RE.sub(" ", expr)  # 去掉标签名和字符串参数，避免把标签当指标
        for name in METRIC_RE.findall(cleaned):
            for pref in FAMILY_OWNER:
                if name.startswith(pref):
                    out.add(pref)
                    break
            else:
                out.add(name)  # 没人认领的新族，留着报红
    return out


def _guards(rule: dict) -> set[str]:
    """这条规则作为看门狗，盯住了哪些 job。"""
    jobs: set[str] = set()
    for expr in _exprs(rule):
        jobs |= set(UP_JOB_RE.findall(expr))
        for hb in HEARTBEAT_RE.findall(expr):
            for pref, job in FAMILY_OWNER.items():
                if hb.startswith(pref):
                    jobs.add(job)
    return jobs


def _load_yaml(path: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("需要 PyYAML：pip install pyyaml（或用 --static-only 之外的方式提供 JSON）")
    with open(path) as fh:
        return yaml.safe_load(fh)


def _walk(node):
    """深度遍历，把所有 dict 吐出来 —— 规则文件的嵌套深度不稳定，不写死路径。"""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def check_fail_red(rules_path: str) -> list[str]:
    """判据 4：数据断了必须变红 —— 但要红在对的那一层。

    一条 `increase(失败数)` 规则，noData 的正确含义是「没有失败」，把它设成
    Alerting 只会天天误报，而天天误报的门禁等于没门禁。真正必须保证的是：
    **每个业务规则读的指标族，都有一条 fail-closed 的活性看门狗盯着。**
    所以这里不搞一刀切，查的是覆盖关系：
      · 看门狗（读 up / last_round_timestamp 这类活性量）必须 fail-closed
      · 业务规则可以 noDataState:OK，前提是它读的每个族都被某条看门狗覆盖
      · 没人覆盖的族 ⇒ 报红，那才是六天空数据渲染绿灯的真实形状
      · execErrState:OK 一律报红：查询报错不等于「没有问题」
    """
    doc = _load_yaml(rules_path)
    problems: list[str] = []
    rules = [d for d in _walk(doc) if "noDataState" in d and "data" in d]
    if not rules:
        return [
            "  ✗ 规则文件里找不到带 noDataState 的规则 —— 要么路径不对，"
            "要么根本没设，两种都得人去看。"
        ]

    watched_jobs: set[str] = set()
    watchdogs = []
    for r in rules:
        g = _guards(r)
        if not g:
            continue
        watchdogs.append(r)
        title = r.get("title") or r.get("uid")
        if r.get("noDataState") in FAIL_OPEN_NODATA:
            problems.append(
                f"  ✗ 看门狗「{title}」自己是 noDataState={r.get('noDataState')} —— "
                f"它一断就没人知道，下面所有规则跟着变绿。必须 Alerting。"
            )
        else:
            watched_jobs |= g

    if not watchdogs:
        problems.append(
            "  ✗ 一条活性看门狗都没有 —— 数据源死了没有任何规则会响。"
            "这正是 Prometheus 六天没抓生产却全绿的形状。"
        )

    for r in rules:
        title = r.get("title") or r.get("uid") or "<无标题规则>"
        if r.get("execErrState") in FAIL_OPEN_EXECERR:
            problems.append(
                f"  ✗ {title}: execErrState={r.get('execErrState')} —— "
                f"查询报错被当成通过。改成 Error。"
            )
        if r in watchdogs or r.get("noDataState") not in FAIL_OPEN_NODATA:
            continue
        for fam in sorted(_families(r)):
            owner = FAMILY_OWNER.get(fam)
            if owner is None:
                problems.append(
                    f"  ✗ {title}: 指标族 `{fam}` 不在 FAMILY_OWNER 表里 —— "
                    f"没人说得清它由哪个 scrape job 提供，也就没人能保证它停了会有人知道。"
                    f"把它登记进表里（并确认有看门狗盯那个 job）。"
                )
            elif owner not in watched_jobs:
                problems.append(
                    f"  ✗ {title}: noDataState=OK，但它读的 `{fam}*` 来自 job "
                    f"`{owner}`，而没有 fail-closed 看门狗盯这个 job —— "
                    f"该 job 停了这条规则会静静变绿。"
                )

    for d in _walk(doc):
        if d.get("type") in FORBIDDEN_EVALUATOR_TYPES and "params" in d:
            problems.append(
                f"  ✗ evaluator type={d.get('type')} params={d.get('params')} —— "
                f"浮点相等/不等永不按预期触发，用 gt/lt。"
            )

    if not problems:
        wd = ", ".join(str(w.get("uid")) for w in watchdogs)
        problems.append(
            f"  ✓ {len(rules)} 条规则；看门狗 {len(watchdogs)} 条（{wd}）全部 fail-closed，"
            f"盯住 job {sorted(watched_jobs)}；每条 fail-open 规则的指标族都有人认领，"
            f"无 eq/ne 阈值"
        )
    return problems


# 探针间隔现在是一个区间（`PROBE_INTERVAL_MIN`..`MAX`，30~45 分钟随机），不再是单值。
# **下游尺子必须按 MAX 标定**：按均值标，抽到 45 分钟那一轮就会假报。
# 两个正则都必须命中，缺一个就报「提取器跟脚本脱节」而不是「间隔没问题」——
# 这是量具自己的失效形状，2026-09-20 的教训是提取器静默失配比阈值错更难发现。
INTERVAL_MIN_RE = re.compile(
    r'^INTERVAL_MIN\s*=\s*int\(os\.environ\.get\(\s*"PROBE_INTERVAL_MIN"\s*,\s*"(\d+)"')
INTERVAL_MAX_RE = re.compile(
    r'^INTERVAL_MAX\s*=\s*int\(os\.environ\.get\(\s*"PROBE_INTERVAL_MAX"\s*,\s*"(\d+)"')
GAP_MAX_RE = re.compile(
    r'^GAP_MAX\s*=\s*float\(os\.environ\.get\(\s*"PROBE_GAP_MAX"\s*,\s*"([\d.]+)"')
DURATION_RE = re.compile(r"\[(\d+)([smhd])\]")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# 单轮耗时上界。2026-09-20 起探针串行且发间随机停 2~10s，单轮 = 82 × (停顿均值 6s
# + 请求本身) ≈ 1200s，不再是并发时代的 92~102s。这个数由 check_cadence 按
# 「目标数 × GAP_MAX」重算，下面的常量只是目标数拿不到时的兜底。
# 改它要有实测支撑，不要为了让 check 过而调它。
ROUND_BUDGET_S = 1200
# 探针目标数。用于按 GAP_MAX 推算单轮上界；实测 82（2026-09-20）。
# 端点数涨了要改这里 —— 涨了不改 ⇒ 单轮上界算小了 ⇒ stale 阈值假报。
PROBE_TARGETS = 82


def check_cadence(rules_path: str, probe_script: str) -> list[str]:
    """判据 5：采样间隔与读它的尺子算术自洽。

    2026-09-20 踩的形状：`PROBE_INTERVAL` 从 900 放宽到 1800，而
      · `llm-probe-stale` 的 gt 阈值按 900 算的（2400s），新间隔下每轮都假报；
      · `min_over_time(litellm_probe_state[35m])` 里的 35m 编码的是「连续两轮」，
        对 900s 成立，对 1800s 静默退化成「一轮」—— 一次抖动就报 critical。
    两处都不会报错，只会开始说谎。所以这条耦合关系必须机器校验，不能只写注释。

    同日第二次改动把间隔改成**区间**（30~45 分钟随机，反风控指纹）并把探针串行化。
    于是这里的算术要跟着变，两处都容易错：
      · **按 MAX 标定，不按均值**。按均值 ⇒ 抽到 45 分钟那一轮每次假报，
        而且假报是间歇性的，比每轮都报更难查。
      · **单轮耗时不再是 92s**。串行 + 发间停顿 ⇒ 上界 ≈ 目标数 × GAP_MAX。
        沿用 180s 的旧预算会把 stale 阈值算松，探针半死不活时不报。

    两个不变量（interval 一律取 MAX）：
      · stale 阈值 > MAX + 单轮耗时（否则正常节奏就超阈值）
      · 每个 min_over_time(litellm_probe_*) 窗口 ≥ 2×MAX + 单轮耗时
        （「连续两轮」的字面含义：窗口里必须真的装得下两个采样点）
    """
    problems: list[str] = []
    lo = hi = gap_max = None
    with open(probe_script) as fh:
        for line in fh:
            for rx, setter in ((INTERVAL_MIN_RE, "lo"), (INTERVAL_MAX_RE, "hi"),
                               (GAP_MAX_RE, "gap")):
                m = rx.match(line)
                if not m:
                    continue
                if setter == "lo":
                    lo = int(m.group(1))
                elif setter == "hi":
                    hi = int(m.group(1))
                else:
                    gap_max = float(m.group(1))
    missing = [n for n, v in (("PROBE_INTERVAL_MIN", lo), ("PROBE_INTERVAL_MAX", hi),
                              ("PROBE_GAP_MAX", gap_max)) if v is None]
    if missing:
        return [
            f"  ✗ 在 {probe_script} 里没提取到 {'、'.join(missing)} "
            f"—— 提取器跟脚本写法脱节了，先修提取器，别当成「间隔没问题」。"
        ]
    if lo > hi:
        return [f"  ✗ {probe_script}: PROBE_INTERVAL_MIN({lo}) > MAX({hi})，区间是反的。"]

    # 串行单轮上界：目标数 × 发间停顿上界。请求本身的耗时已被 GAP_MAX 的余量吸收
    # （实测单发 3~5s，抽到 10s 停顿的那些发把均值拉平）；取两者较大者兜底。
    round_budget = max(ROUND_BUDGET_S, int(PROBE_TARGETS * gap_max))
    doc = _load_yaml(rules_path)
    rules = [d for d in _walk(doc) if "noDataState" in d and "data" in d]
    # 一律按 MAX 标定 —— 按均值会让「抽到最长那一轮」间歇性假报
    interval = hi
    two_round_min = 2 * interval + round_budget
    stale_min = interval + round_budget
    checked = 0

    for r in rules:
        title = r.get("uid") or r.get("title") or "<无标题规则>"
        for expr in _exprs(r):
            # 「连续两轮」窗口
            for win in re.findall(r"min_over_time\(\s*litellm_probe_[a-z_]*\s*\[(\d+[smhd])\]", expr):
                checked += 1
                num, unit = DURATION_RE.match(f"[{win}]").groups()
                secs = int(num) * _UNIT_S[unit]
                if secs < two_round_min:
                    problems.append(
                        f"  ✗ {title}: min_over_time 窗口 {win}({secs}s) < 2×间隔+单轮 "
                        f"({two_round_min}s，间隔={interval}s) —— 「连续两轮才报」会退化成"
                        f"「一轮就报」，一次抖动即 critical。"
                    )
            # stale 看门狗阈值
            if "litellm_probe_last_round_timestamp" not in expr:
                continue
            for dd in r.get("data", []) or []:
                for cond in ((dd.get("model") or {}).get("conditions") or []):
                    ev = cond.get("evaluator") or {}
                    if ev.get("type") != "gt" or not ev.get("params"):
                        continue
                    checked += 1
                    thr = float(ev["params"][0])
                    if thr <= stale_min:
                        problems.append(
                            f"  ✗ {title}: stale 阈值 {thr:g}s ≤ 间隔+单轮 ({stale_min}s) "
                            f"—— 正常节奏就会超阈值，这条看门狗每轮假报。"
                        )
                    elif thr > 4 * interval:
                        problems.append(
                            f"  ✗ {title}: stale 阈值 {thr:g}s > 4×间隔 ({4 * interval}s) "
                            f"—— 探针死了要漏过 4 轮才报，太松。取 ≈2 轮。"
                        )

    if not checked:
        problems.append(
            "  ✗ 一条跟探针间隔耦合的尺子都没找到（既无 min_over_time(litellm_probe_*)，"
            "也无 stale 阈值）—— 要么规则文件不对，要么这两道尺子没了，两种都得人去看。"
        )
    if not problems:
        problems.append(
            f"  ✓ 间隔 {lo}~{hi}s 随机（按 MAX={interval}s 标定，单轮预算 {round_budget}s"
            f"＝{PROBE_TARGETS} 目标 × {gap_max:g}s 停顿上界）：{checked} 处耦合尺子全部自洽"
            f"（两轮窗口 ≥{two_round_min}s，stale 阈值 ∈ ({stale_min}s, {4 * interval}s]）"
        )
    return problems


def declared_jobs(prom_config: str) -> list[str]:
    doc = _load_yaml(prom_config)
    return [sc.get("job_name") for sc in (doc.get("scrape_configs") or []) if sc.get("job_name")]


def _prom_query(base: str, expr: str, timeout: float = 15.0):
    url = base.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(expr)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def check_live(prom_url: str, declared: list[str] | None = None) -> list[str]:
    """判据 1：每个 target 最近有数据。静默的 target 必须被点名，不能沉默。

    ⛔ 这里要抓的不只是 up==0。**声明了却一个 target 都没匹配上的 job，
    连 up 序列都不存在，它永远不会「down」，只是不在那里。**
    2026-09-20 实测：prometheus.yml 声明 8 个 job，现网只有 7 个有 up 序列，
    alert2feishu 零 target —— 而 17/17 全绿。这与六天空数据是同一个形状：
    缺席不报警。所以必须拿声明清单去对账，不能只看已存在的序列。
    """
    problems: list[str] = []
    try:
        up = _prom_query(prom_url, "up")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        # ⛔ 这里绝不能吞掉异常然后返回「没问题」—— 那正是本脚本要消灭的形状
        return [f"  ✗ 打不到 Prometheus（{exc}）—— 这本身就是不过，不是「无法判断」。"]

    result = (up.get("data") or {}).get("result") or []
    if not result:
        return ["  ✗ `up` 查询零结果 —— Prometheus 在跑但一个 target 都没有。"]

    down = []
    live_jobs: dict[str, list[int]] = {}
    for series in result:
        metric = series.get("metric", {})
        job = metric.get("job", "?")
        inst = metric.get("instance", "?")
        val = str((series.get("value") or [None, "0"])[1]).strip()
        live_jobs.setdefault(job, [0, 0])
        live_jobs[job][0] += 1
        if val == "1":
            live_jobs[job][1] += 1
        else:
            down.append(f"{job}/{inst}")
    problems.append(f"  · target 总数 {len(result)}，down {len(down)}")
    for j in sorted(live_jobs):
        problems.append(f"  · job {j}: up {live_jobs[j][1]}/{live_jobs[j][0]}")
    for d in sorted(down):
        problems.append(f"  ✗ target down: {d} —— 它下面的规则现在全是空数据渲染的绿。")

    for j in declared or []:
        if j not in live_jobs:
            problems.append(
                f"  ✗ job `{j}` 在 prometheus.yml 里声明了，但一条 up 序列都没有 —— "
                f"零 target 的 job 永远不会 down，它只是不在那里。"
                f"要么服务发现没匹配上（label/regex），要么该被抓的对象不存在。"
            )

    # 新鲜度：up==1 但样本很旧同样是死的
    try:
        stale = _prom_query(prom_url, 'count(time() - timestamp(up) > 300)')
        sr = (stale.get("data") or {}).get("result") or []
        if sr:
            n = (sr[0].get("value") or [None, "0"])[1]
            if str(n).strip() not in ("0", ""):
                problems.append(
                    f"  ✗ 有 {n} 个 target 的最新样本超过 5 分钟 —— up 可能还是 1，"
                    f"但数据已经不动了。"
                )
    except Exception as exc:  # 明确打出来，不静默跳过
        problems.append(f"  · 新鲜度查询失败（{exc}），这一项没结论，需要人看")
    return problems


def check_delivery(prom_url: str, kubectl_json: str | None) -> list[str]:
    """判据 3：告警真能送到人手上。

    这一腿最容易假绿，坏尺子有三层，从外到内：
      · HTTP 200 —— 飞书 200 + body `code != 0` = 没送到
      · replicas > 0 —— 数字不是就绪，Secret 缺了会 CrashLoop
      · `Available: True` —— **0 副本的 Deployment 也报 Available=True
        （"Deployment has minimum availability"，因为 0 满足 0）**
        2026-09-20 实测 alert-to-feishu 正是这个形状
    ⇒ 只认两条：readyReplicas ≥ 1，且投递计数器在 Prometheus 里存在。
    """
    problems: list[str] = []
    if kubectl_json:
        try:
            with open(kubectl_json) as fh:
                dep = json.load(fh)
        except (OSError, ValueError) as exc:
            return [f"  ✗ 读不到投递侧 Deployment JSON（{exc}）—— 不是「无法判断」，是不过。"]
        st = dep.get("status", {}) or {}
        spec = dep.get("spec", {}) or {}
        ready = st.get("readyReplicas") or 0
        if ready < 1:
            problems.append(
                f"  ✗ 投递侧 Deployment readyReplicas={ready}（spec.replicas="
                f"{spec.get('replicas')}）—— 告警评估得再对也没有出口。"
                f"⛔ 注意 status.conditions 里的 Available=True 在 0 副本时照样为真，"
                f"不能拿它当判据。"
            )
        else:
            problems.append(f"  · 投递侧 readyReplicas={ready}")

    if prom_url:
        # 计数器名对齐 alert2feishu.py:51-53，改那边要同步改这里
        for metric, what in (
            ("alert2feishu_sent_total", "成功送达"),
            ("alert2feishu_delivery_failures_total", "送达失败"),
        ):
            try:
                r = _prom_query(prom_url, metric)
                series = (r.get("data") or {}).get("result") or []
                if not series:
                    problems.append(
                        f"  ✗ Prometheus 里查不到{what}计数器 `{metric}` —— "
                        f"没有这个数就没法判「送到了」，只能靠人盯飞书群，那不是判据。"
                    )
                else:
                    problems.append(f"  · {what}计数器 `{metric}` 在，序列 {len(series)} 条")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                problems.append(f"  ✗ 查 `{metric}` 失败（{exc}）")
    return problems


MANUAL_STEPS = """
以下一项脚本测不了，必须人工做一遍 —— 标 MANUAL，不当绿：

[RED-ABLE] 每条规则都能被合成红触发
  做法：对每条规则，把它的判据人为弄红一次（例如临时指向一个必然越界的
        表达式，或在旁路环境注入越界样本），确认它真的进了 Alerting。
  判据：Grafana 里那条规则的状态确实翻成 Alerting，且下游收到了。
  ⛔ 读配置说「阈值看起来对」不算 —— 第 0 步纪律：合成红与合成绿同样不可信，
     但「没立过阳性对照的绿」比两者都不可信。

DELIVER 腿的接线：k8s/monitoring/wire-feishu-alerts.sh
  唯一缺的输入是飞书群机器人 webhook，只有群管理员能拿到；
  拿到之后该脚本会建 Secret 并把副本拉起来。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="灰度切流前的可观测性自检")
    ap.add_argument("--rules", help="Grafana 告警规则 YAML（litellm-stability.yaml）")
    ap.add_argument("--prom-config", help="prometheus.yml")
    ap.add_argument("--prom-url", help="Prometheus base URL，用于活性检查")
    ap.add_argument(
        "--delivery-deploy-json",
        help="投递侧 Deployment 的 `kubectl get deploy alert-to-feishu -o json` 落地文件",
    )
    ap.add_argument(
        "--probe-script",
        help="probe.py 路径；给了就校验 PROBE_INTERVAL 与规则里的窗口/阈值是否自洽",
    )
    ap.add_argument("--static-only", action="store_true", help="只跑不联网的检查")
    args = ap.parse_args()

    if not args.rules and not args.prom_config and not args.prom_url:
        ap.error("至少给一个 --rules / --prom-config / --prom-url")
    if args.probe_script and not args.rules:
        ap.error("--probe-script 需要同时给 --rules（要比对的尺子在规则文件里）")

    failed = False
    print("=" * 72)
    print("可观测性自检（灰度第 −1 天）—— 任何一条不过，不许开始切流")
    print("=" * 72)

    if args.rules:
        print("\n[FAIL-RED] 数据断了必须变红（判据=每个指标族都有 fail-closed 看门狗）")
        for p in check_fail_red(args.rules):
            print(p)
            if p.lstrip().startswith("✗"):
                failed = True

    if args.probe_script:
        print("\n[CADENCE] 采样间隔与读它的窗口/阈值算术自洽")
        for p in check_cadence(args.rules, args.probe_script):
            print(p)
            if p.lstrip().startswith("✗"):
                failed = True

    declared: list[str] = []
    if args.prom_config:
        print("\n[CONFIG] scrape 配置")
        declared = declared_jobs(args.prom_config)
        print(f"  · 配置声明的 job（{len(declared)} 个）：{', '.join(declared)}")
        if not declared:
            print("  ✗ 一个 scrape job 都没有")
            failed = True

    if args.prom_url and not args.static_only:
        print("\n[LIVE] 每个 target 最近有数据（含：声明了但零 target 的 job）")
        probs = check_live(args.prom_url, declared)
        for p in probs:
            print(p)
            if p.lstrip().startswith("✗"):
                failed = True
    elif not args.static_only:
        print("\n[LIVE] 跳过（没给 --prom-url）—— 这一项没结论，不是通过")

    if not args.static_only and (args.delivery_deploy_json or args.prom_url):
        print("\n[DELIVER] 告警真能送到人手上（判据=readyReplicas + 投递计数器）")
        for p in check_delivery(
            args.prom_url if not args.static_only else None, args.delivery_deploy_json
        ):
            print(p)
            if p.lstrip().startswith("✗"):
                failed = True

    print(MANUAL_STEPS)
    print("=" * 72)
    if failed:
        print("结论：不过。上面每条 ✗ 都是「尺子坏了」，先修尺子再谈切流。")
        return 1
    print("结论：自动那几项过了。RED-ABLE 与 DELIVER 仍是 MANUAL，做完才算齐。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
