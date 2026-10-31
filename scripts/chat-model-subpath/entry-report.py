#!/usr/bin/env python3
"""cc → chat 迁移观测（只读）：每把 key × 每个工具，最近 N 小时走的是新地址还是老地址。

  scripts/chat-model-subpath/entry-report.py                 # 默认 24h，汇总 + 还停在 cc 上的前 30 名
  scripts/chat-model-subpath/entry-report.py --hours 72 --csv .work/entry-observe/report.csv

判据 = LiteLLM_SpendLogs.metadata.spend_logs_metadata.entry：
  "chat" ⇒ 经 chat.auto-link.com.cn/model 进来（198 chat 段 proxy_set_header 打的标记，scripts/chat-model-subpath/deploy.sh）
  空     ⇒ 直连 cc.auto-link.com.cn/pro（或内网直打 30402，见「内部」列）
🔴 标记只在 /model 段加；cc 直连的客户端理论上能自己带同名头伪造成 chat ⇒ 本报告只用于统计，**不能当拦截依据**。
🔴 SpendLogs 只留约 2 天（min(startTime) 先看），--hours 超过保留期会读成「没流量」。
工具按 user_agent 归类；媒体按 call_type + 模型名归类（chat 线型里选出图模型也算媒体）。
"""
import argparse
import csv
import json
import re
import subprocess
import sys

H198 = "cltx@10.68.13.198"
PSQL = ('sudo -n k3s kubectl -n litellm-product exec -i litellm-db-0 -- sh -c '
        '"psql -U \\${POSTGRES_USER:-litellm} -d \\${POSTGRES_DB:-litellm} -At -v ON_ERROR_STOP=1"')

TOOLS = [  # (工具, user_agent 正则)。10-30 按实际 UA 前 25 归类；顺序有意义（先命中先算）
    ("claude-code", r"^claude-cli/|^claude-code"),
    ("cursor", r"^Cursor/"),
    ("codex", r"^codex|^Codex"),
    ("kimi", r"^kimi-code"),
    ("deepseek", r"^deepseek-harness"),
    ("browser", r"^Mozilla/"),
    # 下面这些多半是服务/脚本（her、内部探针、二次转发），判白名单时看 alias + IP，不按「用户工具」迁
    ("sdk/脚本", r"^(AsyncOpenAI|OpenAI|OpenAIClient\w*|Python|python-|litellm|Go-http-client|node|magpie|cli-proxy|curl|axios|undici)"),
]
MEDIA_CALL = re.compile(r"image|video|speech|transcri", re.I)
MEDIA_MODEL = re.compile(r"image|video|music|veo|sora|seedance|seedream|kling|lyria|suno|muse-(spark|video|image)|imagine", re.I)


def tool_of(ua):
    for name, pat in TOOLS:
        if re.search(pat, ua or "", re.I):
            return name
    return "other"


def query(hours):
    sql = f"""
select 'R' || row_to_json(r)::text from (
  select coalesce(v.key_alias, '(无别名)') alias, left(s.api_key, 12) kh,
         coalesce(s.metadata->>'user_agent', '') ua,
         coalesce(s.metadata->'spend_logs_metadata'->>'entry', '') entry,
         coalesce(s.call_type, '') ct, coalesce(s.model_group, '') mg,
         coalesce(s.requester_ip_address, '') ip,
         count(*) n, max(s."startTime") last
  from "LiteLLM_SpendLogs" s left join "LiteLLM_VerificationToken" v on v.token = s.api_key
  where s."startTime" > now() - interval '{int(hours)} hour' and s.api_key <> ''
  group by 1,2,3,4,5,6,7
) r;
select 'M' || min("startTime") from "LiteLLM_SpendLogs";
"""
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", H198, PSQL],
                         input=sql, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        sys.exit(f"STOP: 查库失败 rc={out.returncode}: {out.stderr.strip()[-300:]}")
    lines = out.stdout.splitlines()
    rows = [json.loads(l[1:]) for l in lines if l.startswith("R")]
    oldest = next((l[1:] for l in lines if l.startswith("M")), "?")
    return rows, oldest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--csv")
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    rows, oldest = query(a.hours)
    print(f"SpendLogs 最早一行 {oldest}（窗口 {a.hours}h；早于它的部分读不到）")

    per = {}  # (alias, kh, tool) -> counters
    for r in rows:
        k = (r["alias"], r["kh"], tool_of(r["ua"]))
        c = per.setdefault(k, {"chat": 0, "cc": 0, "cc_media": 0, "last_cc": "", "last_chat": ""})
        media = bool(MEDIA_CALL.search(r["ct"]) or MEDIA_MODEL.search(r["mg"]))
        if r["entry"] == "chat":
            c["chat"] += r["n"]; c["last_chat"] = max(c["last_chat"], r["last"] or "")
        else:
            c["cc"] += r["n"]; c["last_cc"] = max(c["last_cc"], r["last"] or "")
            if media:
                c["cc_media"] += r["n"]

    tot = {"chat": 0, "cc": 0}
    by_tool = {}
    for (alias, kh, tool), c in per.items():
        tot["chat"] += c["chat"]; tot["cc"] += c["cc"]
        t = by_tool.setdefault(tool, {"只走chat": 0, "两边都有": 0, "只走cc": 0})
        t["只走chat" if c["cc"] == 0 else ("只走cc" if c["chat"] == 0 else "两边都有")] += 1
    allreq = tot["chat"] + tot["cc"] or 1
    print(f"请求数：chat {tot['chat']}（{tot['chat'] * 100 / allreq:.1f}%） · cc {tot['cc']}")
    print("\nkey×工具 组合数（「两边都有」= 迁了一半，第二阶段会被拦的就是这批里的 cc 那部分）")
    print(f"  {'工具':<12}{'只走chat':>10}{'两边都有':>10}{'只走cc':>10}")
    for tool, t in sorted(by_tool.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {tool:<12}{t['只走chat']:>10}{t['两边都有']:>10}{t['只走cc']:>10}")

    stuck = sorted(((k, c) for k, c in per.items() if c["cc"] > 0), key=lambda x: -x[1]["cc"])
    media = [x for x in stuck if x[1]["cc_media"] > 0]
    print(f"\n还在 cc 上的 key×工具：{len(stuck)}；其中 cc 上用过媒体的：{len(media)}（第三阶段停媒体会碰到的人）")
    print(f"  {'别名':<34}{'工具':<12}{'cc':>8}{'cc媒体':>8}{'chat':>7}  最后一次cc")
    for (alias, kh, tool), c in stuck[:a.top]:
        print(f"  {alias[:33]:<34}{tool:<12}{c['cc']:>8}{c['cc_media']:>8}{c['chat']:>7}  {c['last_cc'][:16]}")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["alias", "key_hash12", "tool", "chat", "cc", "cc_media", "last_chat", "last_cc"])
            for (alias, kh, tool), c in sorted(per.items()):
                w.writerow([alias, kh, tool, c["chat"], c["cc"], c["cc_media"], c["last_chat"], c["last_cc"]])
        print(f"\nCSV ⇒ {a.csv}")


if __name__ == "__main__":
    main()
