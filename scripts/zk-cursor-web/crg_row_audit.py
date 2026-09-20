#!/usr/bin/env python3
"""crg_row_audit.py —— cr-g-* 行数/名数的判据：DB 是真源，**四个 proxy 副本逐个查过**才算数。

为什么要逐副本：litellm 每个副本各持一份内存路由表，`/model/new` 之后刷新有滞后。
只查一个副本 = 假绿（09-02 踩过）。真源是 DB `LiteLLM_ProxyModelTable`，副本只是用来
证明"每个副本都已经看见了 DB 里那些行"。

查询只取 `model_name` 和 `model_info->>'id'` 两列 —— **禁止拉 `litellm_params`**，那列是
加密凭据。副本侧走 `/model/info`，同样只读这两个字段。
"""
import json
import os
import subprocess
import sys

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SUDO = "sudo -n kubectl -n %s " % NS


def _require_env(name):
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：写死一个真 PG 口令等于把凭据提交进仓库，而且口令轮转后
    老默认值还会静默生效，打出来的认证失败看不出是"忘了设 env"还是"口令真的换了"。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<litellm-db-0 的 PG 口令>（别写进文件/命令行历史）"
            % (name, name)
        )
    return v


PG_PW = _require_env("LITELLM_PG_PW")
WANT_ROWS, WANT_NAMES = 72, 28


def sh(cmd, timeout=180):
    return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout).stdout


def main():
    sql = ("select model_name, model_info->>'id' from \\\"LiteLLM_ProxyModelTable\\\" "
           "where model_name like 'cr-g-%' order by model_name;")
    out = sh(SUDO + "exec litellm-db-0 -- env PGPASSWORD='%s' psql -U litellm -d litellm "
             "-At -F'|' -c \"%s\" 2>/dev/null" % (PG_PW, sql))
    rows = [ln.split("|") for ln in out.strip().splitlines() if "|" in ln]
    names = sorted({n for n, _ in rows})
    ids = {i for _, i in rows}
    print("== DB(真源) ==")
    print("  cr-g-* 行数 = %d (期望 %d)   名数 = %d (期望 %d)"
          % (len(rows), WANT_ROWS, len(names), WANT_NAMES))
    per_name = {}
    for n, i in rows:
        per_name.setdefault(n, []).append(i)
    pool_names = sorted(n for n in names if not n.endswith("-82"))
    print("  池别名 %d 个，各自腿数:" % len(pool_names))
    for n in pool_names:
        lanes = sorted({i.split("zerokey-cr-g-")[1].split("-")[0]
                        for i in per_name[n] if "zerokey-cr-g-" in i})
        flag = "  " if len(lanes) == 4 else "⚠️"
        print("    %s %-26s lanes=%s" % (flag, n, ",".join(lanes)))

    ok = len(rows) == WANT_ROWS and len(names) == WANT_NAMES

    # 逐副本：每个 pod 各查一次自己的 /model/info
    print("\n== 四个 proxy 副本各自的内存路由表 ==")
    pods = json.loads(sh(SUDO + "get pod -l app=litellm-proxy "
                         "--field-selector=status.phase=Running -o json"))["items"]
    prog = ("import urllib.request,json,os\n"
            "req=urllib.request.Request('http://localhost:4000/model/info',"
            "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']})\n"
            "d=[r for r in json.load(urllib.request.urlopen(req,timeout=30))['data'] "
            "if (r.get('model_name') or '').startswith('cr-g-')]\n"
            "print(json.dumps({'rows':len(d),'names':len(set(r['model_name'] for r in d)),"
            "'ids':sorted((r.get('model_info') or {}).get('id') or '' for r in d)}))\n")
    for p in pods:
        pn = p["metadata"]["name"]
        out = subprocess.run(SSH + [SUDO + "exec -i %s -- python3 -" % pn],
                             input=prog, capture_output=True, text=True, timeout=120).stdout
        line = [l for l in out.splitlines() if l.startswith("{")]
        if not line:
            print("  ❌ %-34s 查不到（%s）" % (pn, out[-120:]))
            ok = False
            continue
        d = json.loads(line[-1])
        miss = ids - set(d["ids"])
        extra = set(d["ids"]) - ids
        mark = "✅" if (d["rows"] == WANT_ROWS and not miss and not extra) else "❌"
        if mark == "❌":
            ok = False
        print("  %s %-34s rows=%d names=%d%s%s"
              % (mark, pn, d["rows"], d["names"],
                 "  缺 %d 个 id" % len(miss) if miss else "",
                 "  多 %d 个 id" % len(extra) if extra else ""))

    print("\n" + "=" * 68)
    print("VERDICT: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
