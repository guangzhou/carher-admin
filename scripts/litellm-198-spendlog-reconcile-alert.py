#!/usr/bin/env python3
"""每天对一次账：网关记的请求次数 vs SpendLogs 明细行数，差得多就飞书告警刘国现。

为什么要有这个
--------------
2026-10-25 查出 SpendLogs 每天丢 0.5~1.4% 行（我们自己的清洗补丁把字面 \\u0000 删坏，
87515f0 修复），而且**丢了好几个月没人发现** —— 唯一的症状是查余额里「今日累计消费
（明细实算）」比计费数字低，要用户自己看出来。丢行的原因不止这一种（毒行隔离、盘满应急
TRUNCATE、写库卡住都会丢），修一个原因挡不住下一个，所以按**结果**量：只要明细比日表少，
不管什么原因，第二天早上就知道。

两本账
------
* `LiteLLM_DailyUserSpend.api_requests`：计费那条路写的，不经过 SpendLogs 的清洗/序列化，
  当分母（真实请求数）。
* `LiteLLM_SpendLogs` 行数：明细。

量具的坑（都踩过，改之前先读）
------------------------------
* **只对昨天（UTC）**。日表按 UTC 日期分桶；当天还在写、SpendLogs 落库是双峰刷盘（尾巴到
  6000s，memory feedback_litellm_spend_write_is_bimodal_not_a_fault），对当天 = 假红。
* **只按 key 点查**，别整天 `group by api_key`（2026-10-20 实测超时）。先从小表挑最忙的
  N 把 key，再 LATERAL 逐把查 `(api_key, startTime)` 索引 —— 同 `cli-anything-litellm db
  spend-reconcile` 的写法。
* **按 key 合计，不按 model_group**：兜底换组时日表记原组、明细记兜底组（或反过来），
  按组比会冒出成百上千的正负缺口，按 key 跨组合计才归零（10-25 carher-hangzhou 实测
  组级 ±1000、key 级 -15）。
* **±几行是日界**：日表按请求日期、明细按 startTime，跨午夜的请求会落两边。所以门槛同时
  要「比例」和「绝对行数」，小 key 差 2 行不报。
* **明细缺整天 = 先怀疑保留/清表**，不是丢行（memory feedback_pruned_source_reads_as_quiet_day）。
  `min(startTime)` 晚于昨天 00:00 就报「明细不全，没法对账」，不报丢行比例。
* **查询失败要报出来**，不能当「没缺口」（三量全 0 = 量具坏，不是全绿）。

用法（198 上 root 跑；只读 SQL + 一次飞书发送）
-------------------------------------------
    python3 litellm-198-spendlog-reconcile-alert.py               # 对昨天，超阈值才发
    python3 litellm-198-spendlog-reconcile-alert.py --date 2026-10-24 --dry-run   # 只打印
    python3 litellm-198-spendlog-reconcile-alert.py --always-send # 没超阈值也发（冒烟用）

飞书凭据从 ns `feishu-broadcast` 的 Secret `feishu-broadcast-cred` 读（bot「ai管理的her」），
收件人 = 刘国现在这个 bot 下的 open_id。脚本里没有任何凭据。

退出码：0 没超阈值 / 1 超阈值或对不了账（已告警）/ 2 脚本自身出错（也会尽量告警）。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import shlex
import subprocess
import sys
import urllib.request

NS = "litellm-product"
DB_POD = "litellm-db-0"
KUBECTL = ["kubectl"]
FEISHU_NS = "feishu-broadcast"
FEISHU_SECRET = "feishu-broadcast-cred"
TO_OPEN_ID = "ou_f7253e78ef0cba619d6272cecfb4efd9"   # 刘国现（bot「ai管理的her」视角，同 cursor-key-notice-build.py）

TOP_KEYS = 40                 # 挑最忙的多少把 key 逐个点查（10-20 实测 15 把约 6s）
TOTAL_RATIO = 0.003           # 全库缺口 > 0.3% 报（修复后 10-25 晚 15 分钟实测 0）
KEY_RATIO = 0.02              # 单 key 缺口 > 2% 且 …
KEY_MIN_ROWS = 50             # … 绝对 ≥ 50 行才报（滤掉日界的 ±几行）


def kubectl(*args: str, stdin: str | None = None, timeout: float = 120) -> str:
    r = subprocess.run(KUBECTL + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError("kubectl %s 失败: %s" % (" ".join(args[:4]), r.stderr.strip()[:300]))
    return r.stdout


def psql(sql: str, timeout_ms: int = 60000) -> list[dict[str, str]]:
    """只读事务 + 语句超时，同 cli-anything-litellm 的 remote198.psql。"""
    inner = ('psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -A -F "\t" -v ON_ERROR_STOP=1 '
             f'-c "SET statement_timeout={int(timeout_ms)}" '
             '-c "SET default_transaction_read_only=on" -c "$1"')
    out = kubectl("-n", NS, "exec", DB_POD, "--", "sh", "-c", inner, "sh", sql,
                  timeout=timeout_ms / 1000 + 30)
    lines = [l for l in out.splitlines() if l and l != "SET"]
    if not lines:
        return []
    head = lines[0].split("\t")
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]
            if not (l.startswith("(") and l.endswith(("row)", "rows)")))]


def reconcile(day: str) -> dict:
    d = f"'{day}'"
    cover = psql('select min("startTime") as oldest from "LiteLLM_SpendLogs"')[0]["oldest"]
    totals = psql(
        f'select (select coalesce(sum(api_requests),0) from "LiteLLM_DailyUserSpend" where date={d}) as daily,'
        f' (select count(*) from "LiteLLM_SpendLogs" where "startTime" >= {d}::date and "startTime" < {d}::date + 1) as logs',
        timeout_ms=90000)[0]
    keys = psql(
        f'with d as (select api_key, sum(api_requests) req from "LiteLLM_DailyUserSpend" where date={d}'
        f' group by 1 order by 2 desc limit {TOP_KEYS})'
        ' select coalesce(t.key_alias, left(d.api_key,10)) as alias, d.req as daily, s.n as logs'
        ' from d cross join lateral (select count(*) n from "LiteLLM_SpendLogs" l where l.api_key=d.api_key'
        f' and l."startTime" >= {d}::date and l."startTime" < {d}::date + 1) s'
        ' left join "LiteLLM_VerificationToken" t on t.token=d.api_key',
        timeout_ms=90000)
    return {"day": day, "oldest": cover, "daily": int(totals["daily"]), "logs": int(totals["logs"]),
            "keys": [{"alias": k["alias"], "daily": int(k["daily"]), "logs": int(k["logs"])} for k in keys]}


# 线上在跑的清洗代码是不是已知坏版本（helm rollback 到 10-25 以前任何 revision、手工切回旧 CM
# 都会把它带回来，发版脚本的闸门拦不住这两条路）。同 scripts/litellm-198-callbacks-drift.py 的 KNOWN_BAD。
LANES = ("litellm-proxy", "litellm-proxy-gray", "litellm-proxy-guarded-old")
SHARED_CM = "litellm-callbacks"
KNOWN_BAD = {"null_byte_sanitize.py": "_LITERAL_HIGH_LONE_RE", "sitecustomize.py": "_LITERAL_HIGH_LONE_RE"}


def known_bad_live() -> list[str]:
    hits = []
    cms = {}
    for dep in LANES:
        spec = json.loads(kubectl("-n", NS, "get", "deploy", dep, "-o", "json"))["spec"]["template"]["spec"]
        name = next((v["configMap"]["name"] for v in spec.get("volumes", [])
                     if v.get("name") == "callbacks" and "configMap" in v), None)
        if name is None:
            hits.append(f"{dep}：找不到 callbacks 卷，没法核")
            continue
        cms[name] = dep
    cms[SHARED_CM] = "共享 CM（13 个 chatgpt-acct）"
    for name, who in cms.items():
        data = json.loads(kubectl("-n", NS, "get", "cm", name, "-o", "json")).get("data") or {}
        for f, needle in KNOWN_BAD.items():
            if needle in (data.get(f) or ""):
                hits.append(f"{who}（{name}）的 {f} 是 87515f0 之前的旧版 ⇒ 明细会继续丢行")
    return hits


def judge(r: dict) -> tuple[bool, list[str]]:
    """返回 (要不要告警, 正文行)。"""
    day0 = r["day"] + " 00:00"
    if not r["oldest"] or r["oldest"][:16] > day0:
        return True, [f"⚠️ SpendLogs 最早一行是 {r['oldest'] or '（空表）'}，晚于 {day0} UTC ——",
                      "明细不全（保留期 / 清表），这一天没法对账。不是丢行结论，先查 spendlog-retention / 有没有 TRUNCATE。"]
    if r["daily"] == 0:
        return True, ["⚠️ 日表这一天请求数 = 0 —— 量具或写库有问题（不会真是零流量），没法对账。"]
    gap = r["daily"] - r["logs"]
    ratio = gap / r["daily"]
    bad = [k for k in r["keys"]
           if k["daily"] and k["daily"] - k["logs"] >= KEY_MIN_ROWS
           and (k["daily"] - k["logs"]) / k["daily"] > KEY_RATIO]
    bad.sort(key=lambda k: k["logs"] - k["daily"])
    lines = [f"全库：网关请求 {r['daily']:,} 次，明细 {r['logs']:,} 行，"
             f"缺 {gap:,} 行（{ratio:.2%}，阈值 {TOTAL_RATIO:.1%}）"]
    if bad:
        lines.append("")
        lines.append(f"缺得多的 key（>{KEY_RATIO:.0%} 且 ≥{KEY_MIN_ROWS} 行）：")
        lines.append("| key | 请求 | 明细 | 缺 |")
        lines.append("|---|---:|---:|---:|")
        for k in bad[:10]:
            g = k["daily"] - k["logs"]
            lines.append(f"| {k['alias']} | {k['daily']:,} | {k['logs']:,} | {g:,}（{g / k['daily']:.1%}） |")
    return (ratio > TOTAL_RATIO or bool(bad)), lines


def feishu_send(markdown: str, uuid: str) -> None:
    sec = json.loads(kubectl("-n", FEISHU_NS, "get", "secret", FEISHU_SECRET, "-o", "json"))["data"]
    app_id = base64.b64decode(sec["FEISHU_APP_ID"]).decode()
    app_secret = base64.b64decode(sec["FEISHU_APP_SECRET"]).decode()

    def post(url: str, body: dict, token: str | None = None) -> dict:
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json; charset=utf-8"})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())

    tok = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": app_id, "app_secret": app_secret})
    if tok.get("code") != 0:
        raise RuntimeError("飞书取 token 失败 code=%s" % tok.get("code"))
    card = {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "SpendLogs 对账"}, "template": "orange"},
            "body": {"elements": [{"tag": "markdown", "content": markdown}]}}
    res = post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
               {"receive_id": TO_OPEN_ID, "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False), "uuid": uuid},
               token=tok["tenant_access_token"])
    # HTTP 200 + code≠0 = 没送达（memory feedback_feishu_bot_http200_with_nonzero_code_is_not_delivered）
    if res.get("code") != 0:
        raise RuntimeError("飞书发送失败 code=%s msg=%s" % (res.get("code"), str(res.get("msg"))[:120]))


def main() -> int:
    ap = argparse.ArgumentParser(description="SpendLogs 明细 vs 日表请求数，每日对账告警（只读）")
    ap.add_argument("--date", help="UTC 日期 YYYY-MM-DD，默认昨天")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发飞书")
    ap.add_argument("--always-send", action="store_true", help="没超阈值也发（冒烟）")
    a = ap.parse_args()
    day = a.date or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    dt.date.fromisoformat(day)   # 只接受合法日期，防注入

    try:
        r = reconcile(day)
        alert, lines = judge(r)
        rc = 1 if alert else 0
    except Exception as exc:   # 对不了账本身就要报，不能静默成「没缺口」
        alert, lines, rc = True, [f"⚠️ 对账脚本自身出错，这一天没对上：{type(exc).__name__}: {str(exc)[:300]}"], 2
    try:
        bad = known_bad_live()
    except Exception as exc:
        bad = [f"核线上清洗版本时出错：{type(exc).__name__}: {str(exc)[:200]}"]
    if bad:
        alert, rc = True, max(rc, 1)
        lines += ["", "🔴 线上清洗代码回退了（rollback / 手工切旧 CM？）："] + [f"- {b}" for b in bad]

    head = f"**{day}（UTC）** " + ("🔴 明细少行" if rc == 1 else "🟠 对账失败" if rc == 2 else "✅ 正常")
    body = "\n".join([head, ""] + lines + ["", "日表 = 计费那条路的请求数；明细 = SpendLogs 行数。"
                      "查明细丢在哪：`cli-anything-litellm db spend-reconcile " + day + "`"])
    print(body)
    if (alert or a.always_send) and not a.dry_run:
        try:
            feishu_send(body, uuid=f"spendlog-reconcile-{day}-{rc}" + ("-smoke" if a.always_send and not alert else ""))
            print("→ 已发飞书")
        except Exception as exc:
            print(f"✗ 飞书发送失败：{exc}", file=sys.stderr)
            return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
