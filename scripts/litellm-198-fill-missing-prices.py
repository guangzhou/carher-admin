#!/usr/bin/env python3
"""给 198 LiteLLM 补齐缺失的输入/输出/缓存价（官网目录价，2026-09-29 批次 A）。

这批只补**官网能查到**的缺口，且只补缺的字段，不动已有 input/output：

  DB 管理的 7 行（直接 SQL：备份行 + 价格字段，整行写回）
    claude-cursor-opus-5 / cursor-ultra-opus-5         in/out=0 ⇒ 官网 5/25/0.5/6.25
    claude-cursor-sonnet-4-6 / cursor-ultra-sonnet-4-6 in/out=0 ⇒ 官网 3/15/0.3/3.75
    claude-cursor-composer-2.5 / cursor-composer-2.5   缺缓存读 ⇒ Cursor 官网 0.2
    perplexity-sonar                                   缺缓存读 ⇒ 0.0625
  配置文件里的 3 行（派生 CM ``litellm-cwfb-stable-<hash>``，``subPath`` 不热更）
    wangsu-glm-5.1 / wangsu-cheliantianxia1-glm-5.1    缺缓存读 ⇒ Z.ai 官网 0.26
    zai-max-glm-4.7                                    缺缓存读 ⇒ Z.ai 官网 0.11

刻意不在这里的：google/lyria-3-pro-preview 走 ``openrouter/`` provider，LiteLLM 读
OpenRouter 回报的 ``usage.cost`` 后直接 return，写死的价根本不会被用到（同 jev）。

价格写**官网原价**，全局 ×1.3 由 ``pricing_overlay.py`` 在计费时乘，这里绝不预乘。
缓存写价只在官网单列时才写：缺它时 LiteLLM 按 input 全价记，正是官网"写入按输入价"的口径。

计费读的是 ``litellm_params``（只写 model_info 会 200 且读得回，但照旧按老价记），
DB 行只写 litellm_params（加载时镜像进 model_info），CM 行两块都写；``verify`` 只按 litellm_params 判。

🔴 DB 行不走 ``POST /model/update``：它把 updateLiteLLMParams 的 pydantic dump 整块写回，
   实测（09-29）两条 composer 丢了 ``request_timeout=300``、6 行凭空多出
   ``allow_client_keepalive_override=False``，且接口删不掉字段。PATCH ``/model/{id}/update``
   又匹配不到带 ``/`` 的 id。所以改成「备份行 dict.update 价格 ⇒ 整行 UPDATE」，
   写完用 ``diff-rows`` 断言只有价格字段变了。

用法（在 198 本机跑）:
  ./litellm-198-fill-missing-prices.py backup-sql > b.sql
  psql -A -t -F'|' -f b.sql > rows-before.txt                # 原始行快照 = 回滚源
  ./litellm-198-fill-missing-prices.py fix-sql rows-before.txt > fix.sql
  psql -v ON_ERROR_STOP=1 -f fix.sql
  psql -A -t -F'|' -f b.sql > rows-after.txt
  ./litellm-198-fill-missing-prices.py diff-rows rows-before.txt rows-after.txt   # 非价格字段变了 ⇒ 退出 6
  ./litellm-198-fill-missing-prices.py patch-cm <mounted.yaml> <out.yaml>
  # 然后建新 CM + 带 test op 的守卫 patch 切卷（这一步 rollout 同时让 DB 改动生效）
  LITELLM_MASTER_KEY=sk-... ./litellm-198-fill-missing-prices.py verify
回滚:
  DB  — ``fix-sql --restore rows-before.txt`` 生成原样写回的 SQL
  CM  — 把 deploy 卷名 patch 回旧 CM，旧 CM 不删就一直在
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")

M = 1e6  # $/M ÷ M -> $/token（用除法：0.11 * 1e-6 会得到 1.0999999999999999e-07）

_OPUS5 = {"input_cost_per_token": 5 / M, "output_cost_per_token": 25 / M,
          "cache_read_input_token_cost": 0.5 / M, "cache_creation_input_token_cost": 6.25 / M}
_SONNET46 = {"input_cost_per_token": 3 / M, "output_cost_per_token": 15 / M,
             "cache_read_input_token_cost": 0.3 / M, "cache_creation_input_token_cost": 3.75 / M}

# key = model_info.id。值 = 要补/改成的字段（只含这些字段，其余原样）。
DB_PRICES: dict[str, dict[str, float]] = {
    "claude-cursor/opus-5": _OPUS5,
    "cursor-ultra/claude-opus-5": _OPUS5,
    "claude-cursor/sonnet-4-6": _SONNET46,
    "cursor-ultra/claude-sonnet-4-6": _SONNET46,
    "claude-cursor/composer-2.5": {"cache_read_input_token_cost": 0.2 / M},
    "cursor/composer-2.5": {"cache_read_input_token_cost": 0.2 / M},
    "pplx-sonar-prod-20260923": {"cache_read_input_token_cost": 0.0625 / M},
}
CM_PRICES: dict[str, dict[str, float]] = {
    "wangsu/glm-5.1": {"cache_read_input_token_cost": 0.26 / M},
    "wangsu-cheliantianxia1/glm-5.1": {"cache_read_input_token_cost": 0.26 / M},
    "zai-max/glm-4.7": {"cache_read_input_token_cost": 0.11 / M},
}
ALL_PRICES = {**DB_PRICES, **CM_PRICES}
BILLING_BLOCK = "litellm_params"


def _close(a, b) -> bool:
    return isinstance(a, (int, float)) and abs(float(a) - b) <= 1e-12


def row_status(row: dict, want: dict[str, float]) -> list[str]:
    """返回 billing 块里不对的字段；空 = 对。"""
    params = row.get(BILLING_BLOCK) or {}
    return [f for f, v in want.items() if not _close(params.get(f), v)]


def load_rows(text: str) -> dict[str, dict]:
    """``psql -A -t -F'|'`` 输出 ⇒ {model_id: litellm_params}。id 里不含 ``|``。"""
    out = {}
    for line in text.splitlines():
        if "|" in line:
            mid, js = line.split("|", 1)
            out[mid] = json.loads(js)
    return out


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def fix_sql(rows: dict[str, dict], restore: bool = False) -> str:
    """整行写回：备份行 + DB_PRICES（``restore`` 时不加价 = 回滚）。缺行直接拒。"""
    missing = sorted(set(DB_PRICES) - set(rows))
    if missing:
        raise SystemExit(f"FATAL: 快照里缺这些 id：{missing}")
    tag = "lgx-fill-missing-prices-rollback" if restore else "lgx-fill-missing-prices-0929"
    out = ["begin;"]
    for mid, want in DB_PRICES.items():
        new = dict(rows[mid])
        if not restore:
            new.update(want)
        out.append(f'update "LiteLLM_ProxyModelTable" set litellm_params={_lit(json.dumps(new))}::jsonb, '
                   f"updated_at=now(), updated_by={_lit(tag)} where model_id={_lit(mid)};")
    out.append("commit;")
    return "\n".join(out) + "\n"


def diff_rows(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """返回越界改动；空 = 每行只动了 DB_PRICES 里该行的字段，且值对。"""
    bad = []
    for mid, want in DB_PRICES.items():
        a, b = before.get(mid), after.get(mid)
        if a is None or b is None:
            bad.append(f"{mid}: 行缺失")
            continue
        for f in sorted(set(a) | set(b)):
            if f in want:
                if not _close(b.get(f), want[f]):
                    bad.append(f"{mid}.{f}: 期望 {want[f]} 实为 {b.get(f)}")
            elif a.get(f) != b.get(f):
                bad.append(f"{mid}.{f}: 非价格字段被改 {a.get(f)!r} -> {b.get(f)!r}")
    return bad


def backup_sql(ids) -> str:
    """原始行（含 model/api_base 密文）快照 SQL；回滚 = 把 litellm_params 原样写回。"""
    quoted = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
    return ('select model_id, litellm_params::text from "LiteLLM_ProxyModelTable" '
            f"where model_id in ({quoted}) order by model_id;")


def patch_cm_text(text: str) -> tuple[str, list[str]]:
    """行级手术：在目标块的两个 ``output_cost_per_token`` 行后各插一行缓存价。

    不做 YAML round-trip，其余 80+ 条道的注释/顺序/缩进零变化。
    判据：diff 只多 ``2 * len(CM_PRICES)`` 行。
    """
    lines = text.split("\n")
    starts = [n for n, l in enumerate(lines) if l.startswith("- ")] + [len(lines)]
    out = lines[: starts[0]]
    touched: list[str] = []
    for a, b in zip(starts, starts[1:]):
        block = lines[a:b]
        body = "\n".join(block)
        m = re.search(r"^    id: (\S+)$", body, re.M)
        mid = m.group(1) if m else None
        if mid not in CM_PRICES:
            out.extend(block)
            continue
        want = CM_PRICES[mid]
        if len(want) != 1 or "cache_read_input_token_cost" not in want:
            raise SystemExit(f"FATAL: patch_cm 只会插 cache_read，{mid} 的价表形状不对")
        if "cache_read_input_token_cost" in body:
            raise SystemExit(f"FATAL: {mid} 已有 cache_read 行，拒绝重复插入")
        anchors = [i for i, l in enumerate(block) if re.match(r"^    output_cost_per_token: ", l)]
        if len(anchors) != 2:
            raise SystemExit(f"FATAL: {mid} 应有 2 行 output_cost_per_token（两块各一），实有 {len(anchors)}")
        new_line = f"    cache_read_input_token_cost: {want['cache_read_input_token_cost']!r}"
        for i in reversed(anchors):
            block.insert(i + 1, new_line)
        out.extend(block)
        touched.append(mid)
    missing = sorted(set(CM_PRICES) - set(touched))
    if missing:
        raise SystemExit(f"FATAL: CM 里找不到这些 id：{missing}")
    return "\n".join(out), touched


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc


def rows_by_id() -> dict[str, dict]:
    info = api("GET", "/model/info")
    return {(r.get("model_info") or {}).get("id"): r for r in info.get("data") or []}


def cmd_verify(_a) -> int:
    rows = rows_by_id()
    ok = 0
    for mid, want in ALL_PRICES.items():
        row = rows.get(mid)
        if row is None:
            print(f"  MISSING {mid}")
            continue
        bad = row_status(row, want)
        if bad:
            print(f"  bad {mid} ({row.get('model_name')}): {','.join(bad)}")
        else:
            ok += 1
    print(f"base={BASE} targets={len(ALL_PRICES)} ok={ok} bad={len(ALL_PRICES) - ok}")
    return 0 if ok == len(ALL_PRICES) else 4


def cmd_fix_sql(a) -> int:
    sys.stdout.write(fix_sql(load_rows(open(a.rows).read()), restore=a.restore))
    return 0


def cmd_diff_rows(a) -> int:
    bad = diff_rows(load_rows(open(a.before).read()), load_rows(open(a.after).read()))
    for line in bad:
        print(f"  BAD {line}")
    print(f"rows={len(DB_PRICES)} violations={len(bad)}")
    return 6 if bad else 0


def cmd_backup_sql(_a) -> int:
    print(backup_sql(DB_PRICES))
    return 0


def cmd_patch_cm(a) -> int:
    text = open(a.src).read()
    out, touched = patch_cm_text(text)
    added = out.count("\n") - text.count("\n")
    if added != 2 * len(CM_PRICES):
        print(f"REFUSE: 多了 {added} 行，预期 {2 * len(CM_PRICES)}", file=sys.stderr)
        return 5
    with open(a.dst, "w") as fh:
        fh.write(out)
    h = hashlib.sha256(out.encode()).hexdigest()
    print(f"OK touched={touched} +{added} lines sha256={h}")
    print(f"NEW=litellm-cwfb-stable-{h[:12]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify")
    s = sub.add_parser("fix-sql")
    s.add_argument("rows")
    s.add_argument("--restore", action="store_true")
    s = sub.add_parser("diff-rows")
    s.add_argument("before")
    s.add_argument("after")
    sub.add_parser("backup-sql")
    s = sub.add_parser("patch-cm")
    s.add_argument("src")
    s.add_argument("dst")
    a = p.parse_args()
    offline = {"patch-cm": cmd_patch_cm, "backup-sql": cmd_backup_sql,
               "fix-sql": cmd_fix_sql, "diff-rows": cmd_diff_rows}
    if a.cmd in offline:
        return offline[a.cmd](a)
    if not MASTER:
        print("LITELLM_MASTER_KEY is required", file=sys.stderr)
        return 2
    return cmd_verify(a)


if __name__ == "__main__":
    sys.exit(main())
