#!/usr/bin/env python3
"""cursor_summary_storm_scan.py —— 只读扫描:哪些同事的 Cursor 在「上下文还很空」时就反复自动压缩。

为什么要有它(2026-10-28「好多人说 Cursor 比 Claude 慢」查出来的):
  Cursor 会自己发一发 `You are an intelligent assistant, tasked with summarizing…`
  把会话压缩掉。正常只该在上下文快满时发;件C(@cx-ctxwin:v3)没生效的机器上
  `max_tokens` 收到的是**档位数字**(300k→300),Cursor 自家阈值 min(max-10000, max*0.9)
  变成负数 ⇒ **几乎每轮都压**。这些压缩请求又长(中位数 43~98s)、又和主请求抢同一个
  kiro 槽的并发额度 ⇒ 他自己和同槽的人都变慢。

  判据不是版本号:10-28 实测 3.18.25 的 jialixiang 压缩率 1%、且都在 150~275k(接近窗口)
  = 正常;3.21.18 的 xueqingwen 压缩率 49%、触发时只有 10~34k = 件C 没生效的形状。
  ⇒ 所以这里按「压缩率 + 触发时上下文」判,不按版本判。

  🔴 这个脚本只给**名单和证据**,不下结论。确认要让本人双击 DOCTOR 看
  `ctxwin(@cx-ctxwin:v3)` 那四行是不是都 =3;<3 就让他双击 REPAIR。

用法(本机跑,走 ssh 读 198 的 SpendLogs,只读):
  python3 cursor_summary_storm_scan.py              # 近 24h
  python3 cursor_summary_storm_scan.py --hours 72
  python3 cursor_summary_storm_scan.py --json       # 给别的脚本吃
"""
import argparse, json, subprocess, sys

SUMM_PREFIX = "You are an intelligent assistant, tasked with summ"
# 判「上下文还很空就压」的界:最小真实窗口 200k,10% = 2 万 token。
# 比这还空就触发 ⇒ 不可能是「快满了」,见上面的三段式。
LOW_CTX_TOKENS = 60000
MIN_SUMM = 3            # 少于这么多次不报(偶发/手动压缩不算风暴)
STORM_RATE = 5.0        # 每 100 条主请求压缩 ≥5 次才算风暴

SQL = """
SET statement_timeout='280s';
WITH a AS (
  SELECT metadata->>'user_api_key_alias' alias,
         substring(request_tags::text from 'Cursor/([0-9.]+)') ver,
         prompt_tokens pt, request_duration_ms d,
         coalesce(proxy_server_request::jsonb->'messages'->0->>'content','') LIKE '%s%%' summ
  FROM "LiteLLM_SpendLogs"
  WHERE "startTime" > now() - interval '%d hours'
    AND status = 'success'
    AND request_tags::text LIKE '%%User-Agent: Cursor%%'
    AND proxy_server_request IS NOT NULL)
SELECT coalesce(alias,'(no alias)') alias, coalesce(max(ver),'?') ver,
  count(*) FILTER (WHERE NOT summ) main,
  count(*) FILTER (WHERE summ) summ,
  count(*) FILTER (WHERE summ AND pt < %d) summ_low_ctx,
  coalesce(round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pt) FILTER (WHERE summ)/1000.0)::int, 0) summ_ctx_k_p50,
  coalesce(round(min(pt) FILTER (WHERE summ)/1000.0)::int, 0) summ_ctx_k_min,
  coalesce(round(sum(d) FILTER (WHERE summ)/60000.0)::int, 0) summ_minutes
FROM a GROUP BY 1 HAVING count(*) FILTER (WHERE summ) > 0 ORDER BY 4 DESC;
"""

REMOTE = r"""
set -u
DU=$(sudo kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.DATABASE_URL}' | base64 -d)
U=$(echo "$DU"|sed -E 's#^[a-z+]+://([^:]+):.*#\1#')
PW=$(echo "$DU"|sed -E 's#^[a-z+]+://[^:]+:([^@]+)@.*#\1#')
D=$(echo "$DU"|sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')
q(){ sudo kubectl -n litellm-product exec litellm-db-0 -- env PGPASSWORD="$PW" psql -h 127.0.0.1 -U "$U" -d "$D" "$@"; }
# 阳性对照:这一步不是正整数,下面的空结果就不许当读数
CTL=$(q -t -A -c 'SELECT count(*) FROM "LiteLLM_SpendLogs" WHERE "startTime">now()-interval '"'"'30 minutes'"'"';' 2>&1)
case "$CTL" in ''|*[!0-9]*) echo "FATAL 阳性对照失败,读不到库: $CTL" >&2; exit 2;; esac
echo "CTL=$CTL" >&2
q -t -A -F '|' -c "$SQLTEXT"
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--host", default="cltx@10.68.13.198")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    sql = SQL % (SUMM_PREFIX, a.hours, LOW_CTX_TOKENS)
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "-o", "ClearAllForwardings=yes", a.host,
         "SQLTEXT=" + json_quote(sql) + " bash -s"],
        input=REMOTE, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit("读库失败,上面是原文(空结果不许当成『没人压缩』)")

    rows = []
    for line in r.stdout.splitlines():
        p = line.split("|")
        if len(p) != 8:
            continue
        alias, ver, main_n, summ, low, ctx_p50, ctx_min, mins = p
        main_n, summ, low = int(main_n), int(summ), int(low)
        rate = 100.0 * summ / max(main_n, 1)
        rows.append({
            "alias": alias, "cursor": ver, "main": main_n, "summaries": summ,
            "per_100_main": round(rate, 1), "low_ctx_summaries": low,
            "ctx_at_trigger_k_p50": int(ctx_p50), "ctx_at_trigger_k_min": int(ctx_min),
            "summary_minutes": int(mins),
            # 风暴 = 发得勤 **且** 触发时上下文很空。只满足一条不算。
            "storm": summ >= MIN_SUMM and rate >= STORM_RATE and low >= MIN_SUMM,
        })

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    storms = [x for x in rows if x["storm"]]
    print("=== Cursor 自动压缩扫描(近 %dh,只读)===" % a.hours)
    print("%-28s %-8s %6s %6s %8s %10s %9s" %
          ("key 别名", "Cursor", "主请求", "压缩", "每百条", "触发时k中位", "压缩分钟"))
    for x in sorted(rows, key=lambda y: -y["per_100_main"]):
        print("%-28s %-8s %6d %6d %8.1f %10d %9d%s" %
              (x["alias"][:28], x["cursor"], x["main"], x["summaries"], x["per_100_main"],
               x["ctx_at_trigger_k_p50"], x["summary_minutes"], "   ← 疑似件C没生效" if x["storm"] else ""))
    print()
    if not storms:
        print("没有符合风暴形状的 key(发得勤 + 触发时上下文 <%dk 都要满足)。" % (LOW_CTX_TOKENS // 1000))
        return
    print("疑似件C(@cx-ctxwin:v3)没生效的 %d 个人:" % len(storms))
    for x in storms:
        print("  - %s(Cursor %s):每 100 条主请求压 %.0f 次,触发时上下文中位 %dk、最小 %dk,24h 里压缩占了 %d 分钟"
              % (x["alias"], x["cursor"], x["per_100_main"], x["ctx_at_trigger_k_p50"],
                 x["ctx_at_trigger_k_min"], x["summary_minutes"]))
    print("\n🔴 这只是名单,不是结论。让本人双击 DOCTOR,看 ctxwin(@cx-ctxwin:v3) 那四行:")
    print("   四行都 =3 ⇒ 件C 在,压缩是别的原因(回来重查);任一行 <3 ⇒ 双击 REPAIR 后复测。")


def json_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    main()
