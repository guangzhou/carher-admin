#!/usr/bin/env python3
"""生成库对账 SQL：验证选中的 key 在 LiteLLM_VerificationToken 里真的有行。

为什么必须做这一步：飞书表里的明文可能是陈旧的 —— key 早被删了、表里那行还在。
这种废 key 切过去就是 401，症状和「灰度切坏了」一模一样，会把下一轮诊断带偏。

明文永远不进 SQL —— 只用 sha256 指纹做 CTE join。

用法:
  ./reconcile-keys.py half_expect.json > q_recon.sql
  base64 < q_recon.sql | tr -d '\\n'          # 传到 198，见 SKILL.md §3
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 1

    recs = json.load(open(sys.argv[1], encoding="utf-8"))
    if not recs:
        print("expect 文件是空的", file=sys.stderr)
        return 1

    # 只取 n 和 sid，明文不在这个文件里，天然安全
    vals = ",".join("('%s','%s')" % (r["n"], r["sid"]) for r in recs)

    print(f"""\\pset pager off
\\echo === 汇总 ===
WITH t(n,h) AS (VALUES {vals})
SELECT count(*) AS 送检,
       count(v.token) AS 库里有行,
       count(*) - count(v.token) AS 无行,
       count(*) FILTER (WHERE v.blocked) AS blocked,
       count(*) FILTER (WHERE v.expires IS NOT NULL AND v.expires < now()) AS 已过期
FROM t LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = t.h;

\\echo
\\echo === 无行的（必须剔除，切过去就是 401） ===
WITH t(n,h) AS (VALUES {vals})
SELECT t.n AS 编号, left(t.h,12) AS sid
FROM t LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = t.h
WHERE v.token IS NULL ORDER BY t.n::int;

\\echo
\\echo === blocked 的（可切，但要报给用户；blocked 与路由无关） ===
WITH t(n,h) AS (VALUES {vals})
SELECT t.n AS 编号, v.key_alias
FROM t JOIN "LiteLLM_VerificationToken" v ON v.token = t.h
WHERE v.blocked ORDER BY t.n::int;

\\echo
\\echo === 已过期的（可切，但切过去也用不了） ===
WITH t(n,h) AS (VALUES {vals})
SELECT t.n AS 编号, v.key_alias, v.expires
FROM t JOIN "LiteLLM_VerificationToken" v ON v.token = t.h
WHERE v.expires IS NOT NULL AND v.expires < now() ORDER BY t.n::int;""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
