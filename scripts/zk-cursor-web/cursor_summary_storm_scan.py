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

用法(本机跑,经 remote198.psql 只读 198 的 SpendLogs):
  python3 cursor_summary_storm_scan.py              # 近 24h
  python3 cursor_summary_storm_scan.py --hours 72
  python3 cursor_summary_storm_scan.py --json       # 给别的脚本吃
"""
import argparse, json, os, sys
# 🔴 10-29:原来自己 ssh + 从 secret `litellm-secrets-r2` 解 DATABASE_URL ⇒ 当天 key 轮换把 secret
#    换成了 `litellm-secrets-r2`,脚本直接 FATAL。改用共用的 remote198.psql():它在 db pod 里用
#    $POSTGRES_USER 连库(不碰任何 secret),且事务开成 READ ONLY ⇒ 写不进去。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "cli-anything-litellm"))
from cli_anything.litellm.utils import remote198  # noqa: E402

SUMM_PREFIX = "You are an intelligent assistant, tasked with summ"
# 判「上下文还很空就压」的界:最小真实窗口 200k,10% = 2 万 token。
# 比这还空就触发 ⇒ 不可能是「快满了」,见上面的三段式。
LOW_CTX_TOKENS = 60000
MIN_SUMM = 3            # 少于这么多次不报(偶发/手动压缩不算风暴)
STORM_RATE = 5.0        # 每 100 条主请求压缩 ≥5 次才算风暴

SQL = """
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




# 10-28:只看"压缩风暴"会漏掉真实风险面 —— 同事跑在**我们从没量过锚点**的小版本上时,
# 补丁可能压根没打上,而他只要那天没怎么用 Cursor 就不会出现在风暴名单里。
# 所以同时报一张版本覆盖表。量过的清单与 cursor_team_setup.js 的 VERIFIED_EXACT 同源:
# 这里不复制一份常量,直接从那个文件读,免得两边漂。
def verified_exact(repo_root):
    import pathlib, re as _re
    f = pathlib.Path(repo_root) / "scripts/zk-cursor-web/cursor_team_setup.js"
    try:
        s = f.read_text(encoding="utf8")
        blk = s[s.index("const VERIFIED_EXACT"):s.index("const VERIFIED_VERSIONS")]
        return set(_re.findall(r'"(\d+\.\d+\.\d+)"', blk))
    except Exception:
        return None          # 读不到就明说,不猜


VER_SQL = """
SELECT coalesce(substring(request_tags::text from 'Cursor/([0-9.]+)'),'(未知)') ver,
       count(DISTINCT metadata->>'user_api_key_alias') people, count(*) reqs
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > now() - interval '%d hours' AND request_tags::text LIKE '%%User-Agent: Cursor%%'
GROUP BY 1 ORDER BY 2 DESC, 3 DESC;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    sql = SQL % (SUMM_PREFIX, a.hours, LOW_CTX_TOKENS)
    # 阳性对照:窗口里必须读得到行,否则下面的「没人压缩」不许当读数
    ctl = remote198.psql('SELECT count(*) AS n FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval \'30 minutes\'', timeout_ms=120000)
    if not ctl or not ctl[0].get("n", "").isdigit() or int(ctl[0]["n"]) == 0:
        sys.exit("FATAL 阳性对照失败:近 30 分钟读到 %r 行 ⇒ 下面的空结果不许当成『没人压缩』" % (ctl,))
    res = remote198.psql(sql, timeout_ms=280000)
    rows = []
    for r in res:
        main_n, summ, low = int(r["main"]), int(r["summ"]), int(r["summ_low_ctx"])
        rate = 100.0 * summ / max(main_n, 1)
        rows.append({
            "alias": r["alias"], "cursor": r["ver"], "main": main_n, "summaries": summ,
            "per_100_main": round(rate, 1), "low_ctx_summaries": low,
            "ctx_at_trigger_k_p50": int(r["summ_ctx_k_p50"]), "ctx_at_trigger_k_min": int(r["summ_ctx_k_min"]),
            "summary_minutes": int(r["summ_minutes"]),
            # 风暴 = 发得勤 **且** 触发时上下文很空。只满足一条不算。
            "storm": summ >= MIN_SUMM and rate >= STORM_RATE and low >= MIN_SUMM,
        })
    vrows = [(r["ver"], int(r["people"]), int(r["reqs"])) for r in remote198.psql(VER_SQL % a.hours, timeout_ms=200000)]

    if a.json:
        print(json.dumps({"storms": rows, "versions": [
            {"cursor": v, "people": p3, "requests": r3} for v, p3, r3 in vrows]},
            ensure_ascii=False, indent=1))
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
    import os as _os
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if not storms:
        print("没有符合风暴形状的 key(发得勤 + 触发时上下文 <%dk 都要满足)。" % (LOW_CTX_TOKENS // 1000))
        print_versions(vrows, a.hours, repo_root)
        return
    print("疑似件C(@cx-ctxwin:v3)没生效的 %d 个人:" % len(storms))
    for x in storms:
        print("  - %s(Cursor %s):每 100 条主请求压 %.0f 次,触发时上下文中位 %dk、最小 %dk,24h 里压缩占了 %d 分钟"
              % (x["alias"], x["cursor"], x["per_100_main"], x["ctx_at_trigger_k_p50"],
                 x["ctx_at_trigger_k_min"], x["summary_minutes"]))
    print("\n🔴 这只是名单,不是结论。让本人双击 DOCTOR,看 ctxwin(@cx-ctxwin:v3) 那四行:")
    print("   四行都 =3 ⇒ 件C 在,压缩是别的原因(回来重查);任一行 <3 ⇒ 双击 REPAIR 后复测。")
    print_versions(vrows, a.hours, repo_root)


def print_versions(rows, hours, repo_root):
    """版本覆盖:谁跑在我们没量过锚点的小版本上(= 补丁可能压根没打上的风险面)。"""
    ve = verified_exact(repo_root)
    print("\n=== 版本覆盖(近 %dh,风险面不等于上面的风暴名单)===" % hours)
    if ve is None:
        print("  ⚠️ 读不到 cursor_team_setup.js 的 VERIFIED_EXACT ⇒ 这张表只报版本分布,不判量没量过")
    print("  %-10s %6s %8s  %s" % ("Cursor", "人数", "请求数", "锚点"))
    unmeasured_people = unmeasured_vers = 0
    for ver, people, reqs in rows:
        tag = "" if ve is None else ("量过" if ver in ve else "**没量过**")
        if ve is not None and ver not in ve and ver != "(未知)":
            unmeasured_people += people; unmeasured_vers += 1
        print("  %-10s %6d %8d  %s" % (ver, people, reqs, tag))
    if ve is not None and unmeasured_people:
        print("\n  🔴 %d 人跑在 %d 个没量过锚点的小版本上 —— 他们的补丁是否打上**没有证据**。"
              % (unmeasured_people, unmeasured_vers))
        print("     官方只提供最新版下载(带版本号的 URL 是 404)⇒ 老小版本事后拿不到,")
        print("     只能请本人双击一次 DOCTOR 把报告发来。")





if __name__ == "__main__":
    main()
