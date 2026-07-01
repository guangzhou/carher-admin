#!/usr/bin/env python3
"""
198 zerokey-pool 按账户（=端口）消耗视图。

为什么是这个数据源：zerokey-pool 的真实 11 个 deployment 在 **198 litellm-product**
的 LiteLLM 里，11 个账户共用一个 model_name `zerokey-pool`，只靠 `api_base`(端口) 区分。
唯一能拆到账户粒度的地方是 198 DB 的 `LiteLLM_SpendLogs.api_base` 列。
（Aliyun litellm-proxy 侧只看到聚合的一个 zerokey-pool，api_base=cc.auto-link.com.cn/pro，
拆不开 —— 所以本脚本走 198，不走 Aliyun，跟 zerokey-prod-monitor.py 是两个平面。）

为什么看 calls/tokens 不看 $：zerokey deployment 的 cost_per_token 写死为 0（网页额度，
非付费 API），`spend` 列对 zerokey 恒为 0。真实“消耗”= 调用数 + token 数（=该 ChatGPT
账户网页额度被烧了多少）。

路径：本机 → jms ssh AIYJY-litellm → kubectl exec litellm-db-0 -- psql。

端口 ↔ 198 acct 映射：scripts/zerokey_acct_port_map.py（live 采集，勿手改 PORT_NAME）

用法（本机，仓库根目录）：
    python3 scripts/zerokey-account-usage.py            # 过去 24h
    python3 scripts/zerokey-account-usage.py --hours 5  # 过去 5h
    python3 scripts/zerokey-account-usage.py --hours 168
    python3 scripts/zerokey-account-usage.py --json
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys

def _load_port_name_map() -> dict[str, str]:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "zerokey_acct_port_map.py")
    spec = importlib.util.spec_from_file_location("zerokey_acct_port_map", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        return mod.port_name_map()
    except Exception as e:
        sys.stderr.write(f"WARN: live port map failed ({e}); using fallback\n")
        return mod.port_name_map(mod.FALLBACK_ROWS)


PORT_NAME = _load_port_name_map()

REMOTE_SCRIPT = r"""
import os, subprocess, sys
HOURS = int(os.environ.get('HOURS', '24'))
sql = (
    "SELECT regexp_replace(api_base,'.*:(\\d+)/.*','\\1') AS port, "
    "COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens, "
    "COALESCE(SUM(prompt_tokens),0) AS ptok, "
    "COALESCE(SUM(completion_tokens),0) AS ctok, "
    "ROUND(SUM(spend)::numeric,4) AS spend "
    'FROM "LiteLLM_SpendLogs" '
    "WHERE api_base LIKE '%10.68.13.188:81%' "
    "AND \"startTime\" > NOW() - INTERVAL '" + str(HOURS) + " hours' "
    "GROUP BY 1 ORDER BY calls DESC;"
)
try:
    raw = subprocess.check_output(
        ["kubectl", "-n", "litellm-product", "exec", "litellm-db-0", "--",
         "psql", "-U", "litellm", "-d", "litellm", "-A", "-F|", "-t", "-c", sql],
        text=True, timeout=30,
    )
except Exception as e:
    print("ERR " + str(e), file=sys.stderr)
    sys.exit(1)
sys.stdout.write(raw)
"""


def jms(*args: str) -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(here, "jms"), *args]


def collect(hours: int) -> list[dict]:
    # base64-over-ssh：只依赖 `jms ssh`（jms scp 在本环境会 Permission denied），
    # 且 b64 是纯 [A-Za-z0-9+/=]，避免嵌套引号问题。
    b64 = base64.b64encode(REMOTE_SCRIPT.encode()).decode()
    remote = (f"HOURS={hours} python3 -c "
              f"\"import base64;exec(base64.b64decode('{b64}').decode())\"")
    out = subprocess.check_output(jms("ssh", "AIYJY-litellm", remote), text=True)
    rows = []
    for line in out.splitlines():
        p = [c.strip() for c in line.split("|")]
        if len(p) < 6 or not p[0]:
            continue
        rows.append({
            "port": p[0], "acct": PORT_NAME.get(p[0], "?"),
            "calls": int(p[1]), "tokens": int(p[2]),
            "ptok": int(p[3]), "ctok": int(p[4]), "spend": float(p[5]),
        })
    return rows


def render(rows: list[dict], hours: int) -> str:
    seen = {r["port"] for r in rows}
    # 已知端口但本窗口零流量的也列出来（healthy 但没被 LB 选中）
    for port, name in PORT_NAME.items():
        if port not in seen:
            rows.append({"port": port, "acct": name, "calls": 0, "tokens": 0,
                         "ptok": 0, "ctok": 0, "spend": 0.0})
    rows.sort(key=lambda r: r["calls"], reverse=True)
    tc = sum(r["calls"] for r in rows) or 1
    tt = sum(r["tokens"] for r in rows)
    L = [f"=== zerokey-pool 按账户消耗（过去 {hours}h，198 litellm-product）===",
         "（spend 对 zerokey 恒为 0：cost_per_token 写死 0；zerokey 不上报 prompt token，"
         "看 calls + tokens）", "",
         f"{'port':>5s} {'account':10s} {'calls':>6s} {'share':>6s} {'tokens':>9s}"]
    for r in rows:
        L.append(f"{r['port']:>5s} {r['acct']:10s} {r['calls']:>6d} "
                 f"{100*r['calls']/tc:>5.1f}% {r['tokens']:>9d}")
    L.append("")
    L.append(f"合计: {tc} calls / {tt} tokens；活跃端口 "
             f"{sum(1 for r in rows if r['calls'] > 0)}/{len(PORT_NAME)}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = collect(args.hours)
    if args.json:
        print(json.dumps({"hours": args.hours, "accounts": rows}, ensure_ascii=False))
    else:
        print(render(rows, args.hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
