#!/usr/bin/env python3
"""chatgpt_acct_quota_to_lark.py — 将 198 chatgpt-acct 配额数据写入飞书多维表格。

用法：
  # 先运行 quota 脚本生成结构化数据，再写入飞书
  bash scripts/chatgpt-acct-quota.sh --json-rows --quiet
  python3 scripts/chatgpt_acct_quota_to_lark.py

  # 一步到位
  python3 scripts/chatgpt_acct_quota_to_lark.py --run-quota

  # 指定目标表格（默认使用硬编码的表格）
  python3 scripts/chatgpt_acct_quota_to_lark.py --base-token XXX --table-id tblXXX

数据源：/tmp/chatgpt-acct-quota-rows.json（chatgpt-acct-quota.sh --json-rows 输出）

2026-07-26：从「固定宽度反解渲染后的文本表」改为消费 view 的 --json。旧解析靠
line[100:107] 这类字符偏移，加一列就要重算全部偏移，且错位静默（切出半个数字仍能
float()）。新增 zerokey 累计消耗 6 列时正是踩到这个点，故一并重构。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUOTA_SCRIPT = SCRIPT_DIR / "chatgpt-acct-quota.sh"
ALIYUN_QUOTA_SCRIPT = SCRIPT_DIR / "chatgpt-acct-quota-aliyun.sh"
ROWS_JSON = Path("/tmp/chatgpt-acct-quota-rows.json")
ALIYUN_ROWS_JSON = Path("/tmp/chatgpt-acct-quota-aliyun-rows.json")

DEFAULT_BASE_TOKEN = "YdrZb5xaNaziDHssawGcDpd1nqf"
DEFAULT_TABLE_ID = "tblsw3E1zZ2IuBbW"

# 表格列 → view --json 的 row key。顺序即表格列顺序。
# zk_lat_* 是端到端总时延（秒），不是首 token 时间 —— LiteLLM 的 completionStartTime
# 在流式下几乎等于响应结束点（实测 endTime-cst 中位数 1ms），DB 内无真 TTFT。
COLUMNS: list[tuple[str, str]] = [
    # 2026-08-03 新增，且刻意放第一列：这张表从此不只有 198。两个集群的号编号
    # 各自独立（198 是 33-131，阿里云是 7/11/122-126），今天恰好不撞，但把
    # site 摆在最左边是为了让"这行是哪个集群的"永远不需要推断 —— 且 acct 编号
    # 撞车时它是唯一的区分依据（verify 的行主键也因此改成 (site, acct)）。
    ("site", "site"),
    ("acct", "acct"),
    ("email", "email"),
    ("take", "take"),
    ("status", "status"),
    # ⚠ 列序刻意：verdict 排在 upstream_tier **之前**。2026-08-02 用户问
    # 「acct-121 明明有问题为什么检查不出来」—— 当时第一眼列是 tier=HEALTHY，
    # 而同一时刻 deploy 是 0/1 READY。谁在最左边，谁就是读者的总判定。
    ("verdict", "verdict"),
    ("ready", "ready"),
    ("refresh_err", "refresh_err"),
    # 凭证侧根因判据。与 ready/refresh_err 互补：2026-08-02 实测 acct-120 有 dcr、
    # 115/118 没有，三者同时 ready=0/2 —— 单用任一套都漏一半。
    ("device_code", "device_code"),
    ("exp_gap", "exp_gap"),
    # 2026-08-02 由 `tier` 改名：它只覆盖上游配额平面，从来不是"这号好不好用"。
    # 2026-08-05 语义再变：198 侧这一列现在是 **in-pod 实探**的结论，不再是引擎
    # 快照。引擎那份挪到 state_tier 并排放 —— 两者不一致本身就是要看的信号。
    ("upstream_tier", "upstream_tier"),
    # 实探这一格：live / off / no-pod / 401:<kind> / ERR:* / FETCH_FAIL。
    # 它是整行可信度的分界：FETCH_FAIL 意味着 7d%/reset 退回了引擎快照。
    ("live_probe", "live_probe"),
    ("state_tier", "state_tier"),
    # probe 新鲜度 + 凭证平面。2026-08-01：表上 12 个 `401 需re-OAuth` 让人以为
    # "已重认证却还是 401"，真因是判定是快照（manual_offline 每 6h 才重探）
    # 且引擎只读 pod 内凭证。没这三列，陈旧判定与真故障在表上长得一模一样。
    # （原名 state_age，2026-08-02 改名 probe_age —— ts 是真探时刻不是写入时刻）
    # 2026-08-05：实探成功的行恒为 'live'，退回快照的行才显示快照年龄。
    ("probe_age", "probe_age"),
    ("auth_sync", "auth_sync"),
    ("pod_cred", "pod_cred"),
    ("7d%", "pct7d"),
    # GPT-5.3-Codex-Spark 的独立 7d 子窗口（additional_rate_limits[0]）。
    # 与主窗口不同尺度，实测主窗口 100% 时 Spark 常只有 3~5%。
    ("spark%", "spark_pct"),
    ("reset", "reset"),
    # 2026-08-25 加 7d 窗口列（与 24h 并存）：main_n/main$/codex_n/codex$ 仍是
    # 最近 24h（近况），*_n7/*$7 是最近 7 天 —— 与上游 7d 配额窗口同尺度，
    # 撞顶前的累计消耗在同一窗口里可比。命名跟 zk_n7/zk$7 的先例。
    ("main_n", "main_n"),
    ("main$", "main_spend"),
    ("main_n7", "main_n7"),
    ("main$7", "main_spend7"),
    ("codex_n", "codex_n"),
    ("codex$", "codex_spend"),
    ("codex_n7", "codex_n7"),
    ("codex$7", "codex_spend7"),
    ("zk_n", "zk_n"),
    ("zk$", "zk_spend"),
    ("zk_n7", "zk_n7"),
    ("zk$7", "zk_spend7"),
    ("zk_lat_avg", "zk_lat_avg"),
    ("zk_lat_p95", "zk_lat_p95"),
    ("zk_empty%", "zk_empty_pct"),
    ("next_reset", "next_reset"),
    ("restore", "restore"),
    ("sub_until", "sub_until"),
    ("sub_left", "sub_left"),
    # 2026-08-20 新增：订阅到期改由 in-pod 实探 /accounts/check/v4 得出（续订后立刻
    # 反映，不再是冻结在签发时刻的 JWT claim）。sub_src 标明该行的 sub_until 来源
    # （live=实探到 / state=退回 JWT 快照 / off/no-pod/401/ERR/FETCH_FAIL=探不到），
    # 与 live_probe 对 7d% 的作用一致：读者据此判断订阅日期是不是现值。
    # will_renew=yes/no 是实探的续订意向（acct-237 情形：active 但 will_renew=no）。
    ("sub_src", "sub_src"),
    ("will_renew", "will_renew"),
    ("reset_cards", "reset_cards"),
    ("cause", "cause"),
    # 数据采样时刻（payload.generated_at，UTC）。没有这列时，删 64 行重建 64 行后
    # 屏幕上肉眼与上一版几乎一样（上游 7d% 基本不动），看表的人无法判断是否刷新过。
    ("snapshot_at", "snapshot_at"),
]

FIELDS = [name for name, _ in COLUMNS]

# 新列的建表定义（幂等；已存在则跳过）
NEW_FIELD_SPECS = [
    {"name": "site", "type": "text"},
    {"name": "zk_n", "type": "number"},
    {"name": "zk$", "type": "number"},
    {"name": "zk_n7", "type": "number"},
    {"name": "zk$7", "type": "number"},
    {"name": "zk_lat_avg", "type": "number"},
    {"name": "zk_lat_p95", "type": "number"},
    {"name": "zk_empty%", "type": "number"},
    {"name": "snapshot_at", "type": "text"},
    {"name": "probe_age", "type": "text"},
    {"name": "auth_sync", "type": "text"},
    {"name": "pod_cred", "type": "text"},
    {"name": "verdict", "type": "text"},
    {"name": "ready", "type": "text"},
    {"name": "refresh_err", "type": "number"},
    {"name": "upstream_tier", "type": "text"},
    {"name": "device_code", "type": "text"},
    {"name": "exp_gap", "type": "number"},
    {"name": "live_probe", "type": "text"},
    {"name": "state_tier", "type": "text"},
    {"name": "spark%", "type": "number"},
    # 2026-08-20：订阅实探来源标记 + 续订意向。**text 类型（非单选）** —— 单选列
    # 新增枚举值必须先补 options 否则 delete-all 后写入全失败清空整表（2026-08-05 事故）。
    {"name": "sub_src", "type": "text"},
    {"name": "will_renew", "type": "text"},
    # 2026-08-25：7d spend 窗口列（与 24h 并存）
    {"name": "main_n7", "type": "number"},
    {"name": "main$7", "type": "number"},
    {"name": "codex_n7", "type": "number"},
    {"name": "codex$7", "type": "number"},
]

# 改名/删列后留在飞书表里的空壳列。留着比删掉更坏：`tier` 和 `pod_auth` 会跟
# `upstream_tier`/`pod_cred` 并排显示，全是空格子，读者不知道该看哪一列。
# 2026-08-02：`pod_auth` 是我 01:56 那次写入留下的僵尸列。
ORPHAN_FIELDS = ["tier", "state_age", "pod_auth"]

LARK_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


def run_quota_script() -> None:
    print("[quota] running chatgpt-acct-quota.sh --json-rows --quiet ...", file=sys.stderr)
    r = subprocess.run(
        ["bash", str(QUOTA_SCRIPT), "--json-rows", "--quiet"],
        capture_output=True, text=True, timeout=420,
    )
    if r.returncode != 0:
        print(f"[quota] FAILED rc={r.returncode}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"[quota] OK → {ROWS_JSON}", file=sys.stderr)


def run_aliyun_quota_script() -> None:
    """阿里云取数。整份 view 送到 k8s-work-226 上跑，来回都校 sha256。

    超时给到 30min：链路吞吐会塌（同一条 jms 通道实测过 20min 才回一次），
    而 view 本身还要采 2 次 readyReplicas（间隔 20s）+ 逐号探上游。
    """
    print("[quota-aliyun] running chatgpt-acct-quota-aliyun.sh --json-rows ...",
          file=sys.stderr)
    r = subprocess.run(
        ["bash", str(ALIYUN_QUOTA_SCRIPT), "--json-rows"],
        capture_output=True, text=True, timeout=1800,
    )
    if r.returncode != 0:
        print(f"[quota-aliyun] FAILED rc={r.returncode}", file=sys.stderr)
        print((r.stderr or "")[-2000:], file=sys.stderr)
        sys.exit(1)
    for line in (r.stderr or "").splitlines():
        if line.startswith("["):
            print(line, file=sys.stderr)
    print(f"[quota-aliyun] OK → {ALIYUN_ROWS_JSON}", file=sys.stderr)


def load_payload(path: Path, *, label: str, required: bool) -> dict | None:
    """读一份 rows payload。required 时缺失即致命。

    fail-closed 的理由见 main()：写表是"先删全表再重建"，任何一个源缺失都必须在
    **删除之前**就中止，否则那个集群的行会静默从表上消失。
    """
    if not path.exists():
        msg = (f"[parse] {path} not found — 先跑对应的 --json-rows"
               f"（或加 --run-quota{'-aliyun' if label != '198' else ''}）")
        if required:
            print(msg, file=sys.stderr)
            sys.exit(1)
        return None
    payload = json.loads(path.read_text())
    if not (payload.get("rows") or []):
        print(f"[parse] {label} rows 为空", file=sys.stderr)
        if required:
            sys.exit(1)
        return None
    return payload


def normalize_aliyun_live_fields(r: dict) -> None:
    """给阿里云行补上 198 侧新增的三列，**显式标注而非留空**。

    阿里云那侧从一开始就是 in-pod 实探（`probe_age` 恒 'live'），所以 `live_probe`
    是可以如实推导的 —— 不能因为字段名是 198 新加的就把它留白。留白与 'live' 在
    表上同形，会让读者以为阿里云那半边没实探过，而真相恰好相反。

    `state_tier='no-engine'`：阿里云没有 quota-rebalance 引擎，**不存在**可对照的
    快照。这与"取不到快照"是两件事，同 `auth_sync='no-local'` / `restore='-'` 的
    处理口径（见 chatgpt-acct-quota-lark skill）。
    """
    tier = str(r.get("upstream_tier") or "")
    if not r.get("live_probe"):
        if tier == "SCALED_DOWN" or r.get("ready") == "off":
            r["live_probe"] = "off"
        elif tier.startswith("PROBE_ERR") or r.get("probe_err"):
            r["live_probe"] = "ERR:probe"
        elif r.get("pct7d") is None:
            r["live_probe"] = "no-pod"
        else:
            r["live_probe"] = "live"
    r.setdefault("state_tier", "no-engine")
    r.setdefault("spark_pct", None)


def load_rows(cards: dict | None = None,
              aliyun_payload: dict | None = None) -> tuple[list[list], dict]:
    payload = load_payload(ROWS_JSON, label="198", required=True)
    cards = cards or {}
    src_rows = list(payload.get("rows") or [])
    # 198 侧 view 不产 site 字段（它只知道自己），在这里补。改 198 view 的代价是
    # 它整份要用 stdin 送到 JSZX-AI-03 跑，动它等于动生产取数路径 —— 不值得。
    for r in src_rows:
        r.setdefault("site", "198")

    aliyun_rows: list[dict] = []
    if aliyun_payload:
        aliyun_rows = list(aliyun_payload.get("rows") or [])
        for r in aliyun_rows:
            r.setdefault("site", "aliyun")
            normalize_aliyun_live_fields(r)
        src_rows += aliyun_rows
    if aliyun_rows:
        print("[aliyun] state_tier 恒为 'no-engine'、spark% 恒空 —— 阿里云无 quota 引擎"
              "（没有快照可与实探对照），且只跑 gpt-5.5 一档未取 "
              "additional_rate_limits。**空白不代表 0**", file=sys.stderr)
    if not payload.get("live_fetched", True):
        print("[live] ⚠ 198 侧实探链路失败，本次 upstream_tier/7d%/reset 是**引擎快照**"
              "而非实探值（live_probe 列全为 FETCH_FAIL）—— 按快照口径解读", file=sys.stderr)
    else:
        print(f"[live] 198 侧实探时刻 = {payload.get('live_probed_at') or '?'}；"
              f"state.json 有 {payload.get('state_keys') or '?'} 个 key、"
              f"表上 {len(src_rows) - len(aliyun_rows)} 行"
              f"（差额=引擎不认识的号）", file=sys.stderr)

    def blank_to_none(v):
        # 文本列的 '-' 是"无数据"占位，写进表格会被当成真值
        return None if v in ("", "-") else v

    # 采样时刻取 payload.generated_at（数据实际采集时间），不用写入时刻 —— 两者可
    # 差若干分钟（quota 脚本跑 198 要几分钟），表格该反映数据有多新，不是写得有多新。
    # 两个集群各自的采样时刻不同，所以按行取自己那份，不用一个全局值。
    snapshot_198 = payload.get("generated_at")
    snapshot_aliyun = (aliyun_payload or {}).get("generated_at")

    rows: list[list] = []
    for r in src_rows:
        # reset_cards: 从 cards dict 取 available_count(banked reset 卡数);
        # 探测失败/未探则 None(表格显示空)。cards 只探过 198。
        card_info = cards.get(r["acct"]) or {} if r.get("site") == "198" else {}
        r = {**r, "reset_cards": (card_info.get("credits")
                                  if card_info.get("s") == "OK" else None),
             "snapshot_at": (snapshot_aliyun if r.get("site") == "aliyun"
                             else snapshot_198)}
        rows.append([blank_to_none(r.get(key)) for _, key in COLUMNS])

    n198 = sum(1 for r in src_rows if r.get("site") == "198")
    print(f"[parse] {len(rows)} rows = 198:{n198} + aliyun:{len(aliyun_rows)}  "
          f"(198 generated_at={snapshot_198 or '?'}"
          f"{f', aliyun={snapshot_aliyun}' if aliyun_rows else ''})", file=sys.stderr)
    return rows, payload


def lark_cli(args: list[str], *, input_data: str | None = None, timeout: int = 30) -> dict:
    env = {**os.environ, **LARK_ENV}
    r = subprocess.run(
        ["lark-cli"] + args,
        input=input_data, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": r.stderr or r.stdout or f"rc={r.returncode}"}}


def report_zk_coverage(rows: list[list], payload: dict) -> None:
    """自检 + 诊断：表内 zk 合计 vs 全池合计，差额=未归属（state.json 无对应 acct）。

    写表格前必须印出来 —— 表格只有 state 内的 64 行，若不报差额，看表的人会把
    表内合计当成全池真值。
    """
    idx = {name: i for i, (name, _) in enumerate(COLUMNS)}
    tbl_n = sum(r[idx["zk_n"]] or 0 for r in rows)
    tbl_sp = sum(r[idx["zk$"]] or 0.0 for r in rows)
    pool = payload.get("zk_pool_totals") or {}
    pool_n, pool_sp = pool.get("calls"), pool.get("spend")
    print(f"[zk] 表内合计 {tbl_n} calls / ${tbl_sp:.2f}", file=sys.stderr)
    if pool_n is not None:
        d_n, d_sp = pool_n - tbl_n, (pool_sp or 0.0) - tbl_sp
        print(f"[zk] 全池合计 {pool_n} calls / ${pool_sp:.2f}"
              + (f"  → 未归属 {d_n} calls / ${d_sp:.2f}" if d_n else "  → 全部归属"),
              file=sys.stderr)

    diag = payload.get("diagnostics") or {}
    for key, label in (("zk_unattributed", "未归属编号"),
                       ("zk_orphan_route", "孤儿路由(无 zero-N svc)"),
                       ("zk_empty_output", "稳定空返(疑订阅失效)")):
        items = diag.get(key) or []
        if items:
            print(f"[zk] {label} ({len(items)}): "
                  f"{[i['acct'] for i in items]}", file=sys.stderr)


def report_auth_plane(payload: dict) -> None:
    """凭证平面自检 —— 与 zk 覆盖率同理：写表前必须印，否则表上只剩一个
    `401 需re-OAuth` 标签，而底下是成因互不相同的若干组（认证没进 PVC /
    repair_frozen / 判定陈旧 / 判定将被重测 / 读不到凭证 / 需实跑判活），
    在表格里长得完全一样，看表的人会对全部做同一个无效动作。
    """
    diag = payload.get("diagnostics") or {}
    for key, label in (("auth_not_in_pvc", "认证没进 PVC(引擎读不到,认证不生效)"),
                       ("repair_frozen", "repair_frozen(不会自愈,需人工)"),
                       ("verdict_stale", "401 判定已陈旧>6h(可能已重认证)"),
                       ("verdict_predates_cred", "判定将被重测(凭证晚于判定;新凭证≠账号活)"),
                       ("no_pod_auth", "pod 内读不到凭证(先查 scale=0)"),
                       ("needs_live_probe", "排除法剩下,需实跑 OAuth 判活")):
        items = diag.get(key) or []
        if items:
            print(f"[auth] {label} ({len(items)}): "
                  f"{[i['acct'] for i in items]}", file=sys.stderr)


def subscription_buckets(rows: list[list]) -> dict[str, list]:
    """订阅到期专项分桶（纯函数，可单测）。行是 COLUMNS 顺序的 list。

    2026-08-25 加：订阅到期是这张表最贵的信号（一个号 ≈ $200/月），但它埋在
    39 列的第 30 列上，且 sub_src≠live 的行日期是冻结快照 —— 不聚合着说出来，
    看表的人要么漏掉快到期的号，要么把快照日期当现值。
    分桶互不排斥（一个号可以既 ≤7d 又 will_renew=no —— 这正是最该看的组合）。
    """
    idx = {name: i for i, (name, _) in enumerate(COLUMNS)}

    def cell(r, name):
        return r[idx[name]] or ""

    def days_left(r) -> int | None:
        left = str(cell(r, "sub_left"))
        if left.endswith("d") and left[:-1].isdigit():
            return int(left[:-1])
        return None

    def brief(r) -> str:
        # acct@site: 到期日(剩余) src[将续订与否]
        site = cell(r, "site")
        tag = f"{cell(r, 'acct')}" + (f"@{site}" if site != "198" else "")
        until = str(cell(r, "sub_until")).replace(" UTC", "")
        renew = cell(r, "will_renew")
        renew_s = f" renew={renew}" if renew and renew != "-" else ""
        return f"{tag} {until} ({cell(r, 'sub_left')}) src={cell(r, 'sub_src')}{renew_s}"

    out: dict[str, list] = {"expired": [], "d7": [], "no_renew": [], "stale_src": []}
    for r in rows:
        if cell(r, "sub_left") == "expired":
            out["expired"].append(brief(r))
        d = days_left(r)
        if d is not None and d < 7:
            out["d7"].append(brief(r))
        if cell(r, "will_renew") == "no":
            out["no_renew"].append(brief(r))
        # 日期不是现值的行（探不到/退快照），且确实带着一个日期 —— 单独点名，
        # 免得读者把冻结的 JWT 快照当续订后的现值
        if cell(r, "sub_src") not in ("live", "") and cell(r, "sub_until"):
            out["stale_src"].append(cell(r, "acct"))
    return out


def report_subscription_plane(rows: list[list]) -> None:
    """订阅到期专项报告 —— 写表前必须印（两个集群一起看）。"""
    b = subscription_buckets(rows)
    if b["expired"]:
        print(f"[sub] ⛔ 订阅已过期 ({len(b['expired'])}):", file=sys.stderr)
        for line in b["expired"]:
            print(f"[sub]     {line}", file=sys.stderr)
    if b["d7"]:
        print(f"[sub] ⚠ 7 天内到期 ({len(b['d7'])}):", file=sys.stderr)
        for line in b["d7"]:
            print(f"[sub]     {line}", file=sys.stderr)
    if b["no_renew"]:
        print(f"[sub] ⚠ 实探确认不续订 will_renew=no ({len(b['no_renew'])}):", file=sys.stderr)
        for line in b["no_renew"]:
            print(f"[sub]     {line}", file=sys.stderr)
    if not (b["expired"] or b["d7"] or b["no_renew"]):
        print("[sub] ✓ 无已过期/7天内到期/不续订的号", file=sys.stderr)
    if b["stale_src"]:
        print(f"[sub] ⓘ {len(b['stale_src'])} 行的订阅日期是冻结快照（sub_src≠live，"
              f"scale=0/探针失败探不到）—— 这些行的到期日**不反映续订**，"
              f"以实际探到当日为准", file=sys.stderr)


def ensure_fields(base_token: str, table_id: str) -> None:
    """幂等建新列。字段列表在 data.fields（不是 data.items —— 和 record 系列的
    data.record_id_list 一样属于 lark-cli 的响应形状坑）。"""
    resp = lark_cli([
        "base", "+field-list",
        "--base-token", base_token, "--table-id", table_id,
        "--as", "user", "--format", "json",
    ])
    existing = {f.get("name") for f in (resp.get("data", {}).get("fields") or [])}
    if not existing:
        print("[fields] WARN: 读不到现有字段，跳过建列（避免重复建）", file=sys.stderr)
        return
    missing = [s for s in NEW_FIELD_SPECS if s["name"] not in existing]
    if not missing:
        print(f"[fields] OK: {len(existing)} 列已齐，无需新建", file=sys.stderr)
        return
    for spec in missing:
        tmp = Path("_lark_field.json")
        tmp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        try:
            r = lark_cli([
                "base", "+field-create",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--format", "json",
            ])
            if r.get("ok"):
                print(f"[fields] created {spec['name']} ({spec['type']})", file=sys.stderr)
            else:
                print(f"[fields] ERROR creating {spec['name']}: "
                      f"{r.get('error', {}).get('message', '?')}", file=sys.stderr)
                sys.exit(3)
        finally:
            tmp.unlink(missing_ok=True)


def prune_orphan_fields(base_token: str, table_id: str, *, apply: bool) -> None:
    """报告（可选删除）改名后留下的空壳列。

    默认只报不删 —— 删飞书列会连历史数据一起删，不是幂等操作，必须显式 --prune-fields。
    """
    resp = lark_cli([
        "base", "+field-list",
        "--base-token", base_token, "--table-id", table_id,
        "--as", "user", "--format", "json",
    ])
    fields = resp.get("data", {}).get("fields") or []
    # ⚠ field id 的键是 `id` 不是 `field_id` —— 又一个 lark-cli 响应形状坑，
    # 和 `data.fields`(非 data.items) / `data.record_id_list` 是同一类。
    # 2026-08-02 用 f.get("field_id") 取到 None，直接把 subprocess 打崩。
    by_name = {f.get("name"): f.get("id") for f in fields}
    stale = [n for n in ORPHAN_FIELDS if by_name.get(n)]
    if not stale:
        return
    if not apply:
        print(f"[fields] 僵尸列 {stale} 仍在表上（改名后的空壳）—— "
              f"加 --prune-fields 才会删", file=sys.stderr)
        return
    for name in stale:
        r = lark_cli([
            "base", "+field-delete",
            "--base-token", base_token, "--table-id", table_id,
            "--field-id", by_name[name],
            "--as", "user", "--yes", "--format", "json",
        ])
        if r.get("ok"):
            print(f"[fields] pruned 僵尸列 {name}", file=sys.stderr)
        else:
            print(f"[fields] ERROR pruning {name}: "
                  f"{r.get('error', {}).get('message', '?')}", file=sys.stderr)


def report_aliyun_plane(payload: dict) -> None:
    """阿里云侧自检。判据只用阿里云真有的平面，不套 198 的分桶。

    刻意逐项报「空白的原因」：阿里云的 zk_*/codex_* 列是空的，而空白在表上与
    「消耗为 0」完全同形。不在这里说出来，看表的人只能靠猜。
    """
    rows = payload.get("rows") or []
    def accts(pred) -> list[str]:
        return sorted((r["acct"] for r in rows if pred(r)),
                      key=lambda a: int(a.split("-")[1]) if a.split("-")[-1].isdigit()
                      else 10**9)

    print(f"[aliyun] {len(rows)} 行 | 采样时刻={payload.get('serving_sampled_at')} "
          f"次数={payload.get('serving_samples')}", file=sys.stderr)
    for label, pred in [
        ("scale=0(预期内下线,非故障)", lambda r: r.get("upstream_tier") == "SCALED_DOWN"),
        ("探针失败(状态未知,已 fail-closed 不接单)",
         lambda r: r.get("upstream_tier") == "PROBE_ERR"),
        ("verdict 非 OK", lambda r: r.get("verdict") not in ("OK", "OFFLINE")),
        ("device-code hell", lambda r: r.get("device_code") == "yes"),
        ("从未成功 refresh(exp_gap>0)",
         lambda r: isinstance(r.get("exp_gap"), (int, float)) and r["exp_gap"] > 0),
        ("7d>=90%", lambda r: isinstance(r.get("pct7d"), (int, float)) and r["pct7d"] >= 90),
        ("订阅已过期/剩<7d", lambda r: (r.get("sub_left") or "").startswith(("expired", "0d",
                                        "1d", "2d", "3d", "4d", "5d", "6d"))),
    ]:
        if hit := accts(pred):
            print(f"[aliyun] {label} ({len(hit)}): {hit}", file=sys.stderr)
    print("[aliyun] zk_*/codex_* 列恒空 —— 阿里云只跑 gpt-5.5 一档，且 zk 用量聚合"
          "本期未接入。**空白不代表 0**", file=sys.stderr)
    print("[aliyun] auth_sync 恒为 no-local、restore 恒为 '-' —— 阿里云无 /Data 凭证"
          "镜像、无 quota 引擎，这两列在该集群无定义（不是取数失败）", file=sys.stderr)


def report_serving_plane(payload: dict) -> None:
    """服务平面自检 —— 必须在写表前印。

    2026-08-02 前这一段不存在，因此 acct-121 这类「上游 HEALTHY + deploy 0/1 READY」
    在表上和真健康号完全同形。用户问「明明有问题为什么检查不出来」，答案就是缺这段。
    """
    diag = payload.get("diagnostics") or {}
    sv_at = payload.get("serving_sampled_at")
    n = payload.get("serving_samples")
    print(f"[serving] 采样时刻={sv_at} 次数={n}", file=sys.stderr)
    for key, label in (
        ("serving_dead_upstream_ok", "上游绿但服务已死(挂池当黑洞,引擎不会摘)"),
        # dcr 与 ready/refresh_err 互补：120 有 dcr、115/118 没有却同时 ready=0/2
        ("device_code_hell", "device-code地狱(每请求试refresh→同步阻塞堵event loop)"),
        ("never_refreshed", "从未成功refresh过(exp_gap>0,刷成一次即永久自愈)"),
        ("refresh_dying", "token刷新已断(refresh 401,需re-OAuth)"),
        ("flapping", "Ready抖动(单点采样会随机给出健康/已死)"),
        ("serving_unknown", "服务平面取数失败(未知,别当没问题)"),
    ):
        items = diag.get(key) or []
        if items:
            print(f"[serving] {label} ({len(items)}): "
                  f"{[i['acct'] for i in items]}", file=sys.stderr)


def missing_select_options(field: dict, values: set[str]) -> list[str]:
    """这个单选列缺哪些选项（纯函数，可单测）。

    飞书单选列拒绝写入不在 options 里的值，报 `code 800030005 not_found`。
    """
    have = {o.get("name") for o in (field.get("options") or [])}
    return sorted(v for v in values if v and v not in have)


# 飞书只接受这 11 个 hue（实测 'Violet' 被拒：Invalid enum value）。
# 新选项从这里循环取色，不要凭印象写颜色名。
LARK_HUES = ["Red", "Orange", "Yellow", "Lime", "Green", "Turquoise",
             "Wathet", "Blue", "Carmine", "Purple", "Gray"]


def ensure_select_options(base_token: str, table_id: str,
                          rows: list[list]) -> None:
    """把本次要写的值补进单选列的 options —— **必须在 delete 之前跑**。

    2026-08-05 事故：`status` 新增取值 `UNTRACKED`、`take` 新增 `?`，而这两列在飞书
    是单选、options 分别只有 `ONLINE/OFFLINE/PAUSED/SLOW` 和 `yes/-`。
    执行顺序是「建列 → 删全表 → 批量写」，于是：**70 行删掉了，84 行一条没写进去，
    表被清空**。`created==0 != parsed==84` 的 WARN 是事后的，救不回数据。

    原有的 fail-closed 只覆盖「取数失败」（在删之前中止），没覆盖「写入被拒」。
    这个函数补的就是那一半：把 schema 与数据的兼容性检查也挪到删除之前。

    `ensure_fields` 只保证列**存在**，不保证列**接受这些值** —— 两件事。
    """
    resp = lark_cli([
        "base", "+field-list",
        "--base-token", base_token, "--table-id", table_id,
        "--as", "user",
    ])
    fields = ((resp.get("data") or {}).get("fields") or []) if resp.get("ok") else []
    if not fields:
        print("[options] 取字段列表失败 —— 无法预检单选列，中止（不动表）", file=sys.stderr)
        sys.exit(6)
    by_name = {f.get("name"): f for f in fields}
    idx = {name: i for i, (name, _) in enumerate(COLUMNS)}

    for fname, field in by_name.items():
        if "options" not in field or fname not in idx:
            continue
        values = {str(r[idx[fname]]) for r in rows if r[idx[fname]] is not None}
        missing = missing_select_options(field, values)
        if not missing:
            continue
        opts = list(field.get("options") or [])
        base = len(opts)
        for i, name in enumerate(missing):
            opts.append({"name": name,
                         "hue": LARK_HUES[(base + i) % len(LARK_HUES)],
                         "lightness": "Lighter"})
        # PUT 全量语义：必须带上原有 options，否则等于删掉它们（连数据一起）
        payload = {"name": fname, "type": "select",
                   "multiple": bool(field.get("multiple")), "options": opts}
        r = lark_cli([
            "base", "+field-update",
            "--base-token", base_token, "--table-id", table_id,
            "--field-id", fname, "--json", json.dumps(payload, ensure_ascii=False),
            "--yes", "--as", "user",
        ])
        if r.get("ok"):
            print(f"[options] {fname} += {missing}", file=sys.stderr)
        else:
            msg = ((r.get("error") or {}).get("message") or "?")
            print(f"[options] {fname} 补选项失败: {msg} —— 中止（不动表）",
                  file=sys.stderr)
            sys.exit(6)


def delete_all_records(base_token: str, table_id: str) -> int:
    deleted = 0
    while True:
        resp = lark_cli([
            "base", "+record-list",
            "--base-token", base_token, "--table-id", table_id,
            "--limit", "200", "--as", "user", "--format", "json",
        ])
        d = resp.get("data", {})
        ids = d.get("record_id_list", [])
        if not ids:
            break
        payload = json.dumps({"record_id_list": ids})
        tmp = Path("_lark_del_batch.json")
        tmp.write_text(payload)
        try:
            dr = lark_cli([
                "base", "+record-delete",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--yes", "--format", "json",
            ])
            if not dr.get("ok"):
                print(f"[delete] ERROR: {dr.get('error', {}).get('message', '?')}", file=sys.stderr)
                break
            deleted += len(ids)
            print(f"[delete] {deleted} records deleted so far", file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)
    return deleted


def batch_create_records(base_token: str, table_id: str, rows: list[list]) -> int:
    created = 0
    for i in range(0, len(rows), 200):
        batch = rows[i:i + 200]
        payload = json.dumps({"fields": FIELDS, "rows": batch}, ensure_ascii=False)
        tmp = Path("_lark_create_batch.json")
        tmp.write_text(payload, encoding="utf-8")
        try:
            resp = lark_cli([
                "base", "+record-batch-create",
                "--base-token", base_token, "--table-id", table_id,
                "--as", "user", "--json", f"@{tmp.name}", "--format", "json",
            ], timeout=60)
            if not resp.get("ok"):
                print(f"[create] ERROR: {resp.get('error', {}).get('message', '?')}", file=sys.stderr)
                break
            rid_list = resp.get("data", {}).get("record_id_list", [])
            created += len(rid_list)
            print(f"[create] batch {i // 200 + 1}: {len(rid_list)} records created", file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)
    return created


def verify_records(base_token: str, table_id: str, rows: list[list]) -> bool:
    """读回表格并逐格比对，不只数条数。

    只数条数会把「64 行全是空格子」也报成 OK —— 2026-07-27 就因此把一次成功写入
    误当作可疑（真正的问题是表里没有采样时刻列，肉眼看不出刷新）。条数相等是必要
    条件，不是充分条件，故这里比对内容。`+record-list` 是列式返回：`data.fields`
    是表头、`data.data` 是行数组。

    行主键是 **(site, acct)** 而不是 acct。2026-08-03 加 site 时改的：单 acct 主键
    在两个集群编号撞车时会静默互相覆盖 —— 条数照样对得上、内容悄悄错，正是
    「只数条数」那一类坑的翻版。今天 198(33-131) 与阿里云(7/11/122-126) 恰好不撞，
    但"恰好"不是不变量。
    """
    expected = len(rows)
    idx = {name: i for i, (name, _) in enumerate(COLUMNS)}

    def key_of(row: list, pos: dict[str, int]) -> tuple:
        return (row[pos["site"]] if "site" in pos else None, row[pos["acct"]])

    want = {key_of(r, idx): r for r in rows}
    if len(want) != expected:
        print(f"[verify] MISMATCH: (site,acct) 主键有重复 —— {expected} 行只产出 "
              f"{len(want)} 个唯一键，写入会互相覆盖", file=sys.stderr)
        return False

    got_rows: list[list] = []
    header: list[str] = []
    page_token = None
    while True:
        cmd = [
            "base", "+record-list",
            "--base-token", base_token, "--table-id", table_id,
            "--limit", "200", "--as", "user", "--format", "json",
        ]
        if page_token:
            cmd.extend(["--page-token", page_token])
        resp = lark_cli(cmd)
        d = resp.get("data", {})
        header = d.get("fields") or header
        got_rows.extend(d.get("data") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
        if not page_token:
            break

    if len(got_rows) != expected:
        print(f"[verify] MISMATCH: {len(got_rows)} records in table, "
              f"expected {expected}", file=sys.stderr)
        return False
    if "acct" not in header:
        print("[verify] WARN: 读不到表头，跳过逐格比对", file=sys.stderr)
        return False

    # 表格列顺序与 COLUMNS 无关，按列名取
    hpos = {name: i for i, name in enumerate(header)}
    diffs: list[str] = []
    for gr in got_rows:
        k = key_of(gr, hpos)
        label = f"{k[0] or '?'}/{k[1]}"
        exp = want.get(k)
        if exp is None:
            diffs.append(f"{label}: 表内多出的行")
            continue
        for col, src_i in idx.items():
            if col not in hpos:
                continue
            e, g = exp[src_i], gr[hpos[col]]
            if isinstance(g, list):  # select 列回读为 list
                g = g[0] if g else None
            if g in ("", []):
                g = None
            if isinstance(e, (int, float)) and isinstance(g, (int, float)):
                if abs(float(e) - float(g)) > 1e-6:
                    diffs.append(f"{label}.{col}: 期望 {e} 实际 {g}")
            elif (e if e not in ("", "-") else None) != g:
                diffs.append(f"{label}.{col}: 期望 {e!r} 实际 {g!r}")

    missing = set(want) - {key_of(gr, hpos) for gr in got_rows}
    for a in sorted(missing, key=lambda t: (str(t[0]), str(t[1]))):
        diffs.append(f"{a[0] or '?'}/{a[1]}: 表内缺失")

    if diffs:
        print(f"[verify] MISMATCH: {len(got_rows)} 行数对得上，但 "
              f"{len(diffs)} 处内容不符：", file=sys.stderr)
        for d_ in diffs[:15]:
            print(f"  {d_}", file=sys.stderr)
        if len(diffs) > 15:
            print(f"  ... 另有 {len(diffs) - 15} 处", file=sys.stderr)
        return False

    print(f"[verify] OK: {len(got_rows)} 行 × {len(idx)} 列逐格比对一致", file=sys.stderr)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Write 198 chatgpt-acct quota data to Lark Base")
    parser.add_argument("--run-quota", action="store_true", help="Run chatgpt-acct-quota.sh first")
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--skip-delete", action="store_true", help="Skip deleting old records")
    parser.add_argument("--prune-fields", action="store_true",
                        help="删掉改名后留下的僵尸列(tier/state_age/pod_auth)——会连数据一起删")
    parser.add_argument("--cards-json", default="",
                        help="reset 卡数据 JSON 文件路径(chatgpt-acct-reset-cards.py 输出)")
    parser.add_argument("--run-quota-aliyun", action="store_true",
                        help="先跑 chatgpt-acct-quota-aliyun.sh --json-rows 取阿里云数据")
    parser.add_argument("--aliyun-rows", default="",
                        help=f"阿里云 rows JSON 路径(默认 {ALIYUN_ROWS_JSON};"
                             f" 不传且不加 --run-quota-aliyun 则只写 198)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只取数+自检，不建列不删不写")
    args = parser.parse_args()

    # ⚠ 取数必须全部走完再动表。写表是「先删全表再重建」，若阿里云取数失败发生在
    # 删除之后，结果是整表被清空、只写回 198 —— 阿里云那些行**静默消失**，
    # 而脚本还会报 created==parsed 的 OK。所以两个源都在这里先落地。
    #
    # 两侧并发：198 走 ssh JSZX-AI-03，阿里云走 jms→k8s-work-226，是两台互不相干的
    # 主机，串行等于白等。阿里云那条 jms 链路吞吐会塌（可能秒回也可能爬十几分钟），
    # 让它和 198 重叠跑，整轮 wall-clock 取决于慢的那个而不是两者之和。
    jobs = []
    if args.run_quota:
        jobs.append(("198", run_quota_script))
    if args.run_quota_aliyun:
        jobs.append(("aliyun", run_aliyun_quota_script))
    if jobs:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {ex.submit(fn): name for name, fn in jobs}
            failed = []
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    fut.result()
                except SystemExit as e:      # 子函数用 sys.exit 报错
                    failed.append((name, f"exit {e.code}"))
                except Exception as e:
                    failed.append((name, f"{type(e).__name__}: {e}"))
        print(f"[fetch] 两侧取数耗时 {time.time() - t0:.0f}s", file=sys.stderr)
        if failed:
            for name, why in failed:
                print(f"[fetch] {name} 取数失败: {why}", file=sys.stderr)
            print("[fetch] 取数未全部成功 —— 不动表（避免删表后只写回一侧）",
                  file=sys.stderr)
            return 1

    cards = {}
    if args.cards_json:
        try:
            cards = json.loads(Path(args.cards_json).read_text())
            n_with = sum(1 for v in cards.values() if v.get("s") == "OK")
            print(f"[cards] loaded {len(cards)} accts, {n_with} probed OK", file=sys.stderr)
        except Exception as e:
            print(f"[cards] WARN failed to load {args.cards_json}: {e}", file=sys.stderr)

    # 阿里云是否参与：显式给了路径、或刚跑过取数。两种情况下缺文件都是致命的
    # —— 「本该有阿里云却静默只写 198」比直接失败糟得多。
    aliyun_path = Path(args.aliyun_rows) if args.aliyun_rows else ALIYUN_ROWS_JSON
    aliyun_expected = bool(args.aliyun_rows or args.run_quota_aliyun)
    aliyun_payload = None
    if aliyun_expected:
        aliyun_payload = load_payload(aliyun_path, label="aliyun", required=True)
    else:
        print("[parse] 未指定阿里云数据源 —— 本次只写 198 行"
              "（要含阿里云请加 --run-quota-aliyun）", file=sys.stderr)

    rows, payload = load_rows(cards, aliyun_payload=aliyun_payload)
    if not rows:
        print("[ERROR] no data to write", file=sys.stderr)
        return 1

    report_zk_coverage(rows, payload)
    report_auth_plane(payload)
    report_serving_plane(payload)
    report_subscription_plane(rows)   # 订阅到期专项：两个集群一起看，2026-08-25 加
    if aliyun_payload:
        # 阿里云不复用 198 的三段自检：那些分桶的判据预设了 198 的平面
        # （auth_not_in_pvc 要有 /Data 镜像、zk 覆盖率要有 zk 池）。硬套会让 7 行
        # 阿里云数据涌进不适用的桶，把 198 的真信号淹掉。
        report_aliyun_plane(aliyun_payload)

    if args.dry_run:
        n_by_site: dict[str, int] = {}
        site_i = [i for i, (n, _) in enumerate(COLUMNS) if n == "site"][0]
        for r in rows:
            n_by_site[r[site_i] or "?"] = n_by_site.get(r[site_i] or "?", 0) + 1
        print(f"[dry-run] 共 {len(rows)} 行 × {len(COLUMNS)} 列，分布 {n_by_site}"
              f" —— 未建列、未删除、未写入", file=sys.stderr)
        return 0

    ensure_fields(args.base_token, args.table_id)
    ensure_select_options(args.base_token, args.table_id, rows)
    prune_orphan_fields(args.base_token, args.table_id, apply=args.prune_fields)

    if not args.skip_delete:
        deleted = delete_all_records(args.base_token, args.table_id)
        print(f"[delete] total deleted: {deleted}", file=sys.stderr)

    created = batch_create_records(args.base_token, args.table_id, rows)
    print(f"[create] total created: {created}", file=sys.stderr)

    if created != len(rows):
        print(f"[WARN] created {created} != parsed {len(rows)}", file=sys.stderr)
        return 2

    ok = verify_records(args.base_token, args.table_id, rows)

    print(f"\n[DONE] {created} records written to Lark Base", file=sys.stderr)
    print(f"  https://t83dfrspj4.feishu.cn/base/{args.base_token}?table={args.table_id}")
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
