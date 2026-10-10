#!/usr/bin/env python3
"""188 上 hermestest-<N> 实例 openclaw cron 任务的体检量具。

为什么需要它：cron 的 last_run_status=ok 只代表 agent 回了一句 done，
不代表脚本取全了数据。2026-09-29~10-01 全员日报连续三天 ok，实际 198pro
的 /spend/logs 返 500 被脚本吞掉，卡片少了九成金额。所以这里分四层看：

  L0  阳性对照：openclaw.sqlite 能读、cron_jobs 非空、目标 job 存在。
      读不到 ⇒ exit 3（尺子坏了），不解读下面任何层。
  L1  调度：enabled、next_run 在未来 36h 内、没有卡住的 running_at。
  L2  最近 N 次运行：cron 状态 + 会话 transcript 里工具输出的
      `failed|Error|500|timeout` 行（ok 但部分失败就在这层判红）。
  L3  (--spendlogs) 198pro / aliyun 的 /spend/logs 逐日实打，报耗时；
      任一天 >25s 或非 200 判红（服务端 ~30s 截断返 500）。
  L4  (--sha) 容器里脚本的 sha256 与仓库 scripts/hermestest-cron/ 快照对比。

凭据：SSHPASS 环境变量（188 cltx 密码），本脚本不带默认值。

用法：
  SSHPASS=... scripts/hermestest-cron-audit.py                       # 默认查全员日报
  SSHPASS=... scripts/hermestest-cron-audit.py --spendlogs --sha
  SSHPASS=... scripts/hermestest-cron-audit.py --job litellm-spend-sync --runs 5
  SSHPASS=... scripts/hermestest-cron-audit.py --list                # 列所有 enabled job

退出码：0 全绿 / 1 有层判红 / 3 尺子坏了 / 4 ssh 调用失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "hermestest-cron")
HOST = "cltx@10.68.13.188"
WS = "/data/.openclaw/workspace"
DEFAULT_JOB = "claude-code-daily-spend-report"
SHA_FILES = ["litellm-company-daily-actual-report.js", "litellm-adjusted-stats.sh"]

# 在容器里跑的 node 程序：只读 sqlite + transcript，输出一个 JSON。
REMOTE_JS = r"""
const fs = require("fs");
const { DatabaseSync } = require("node:sqlite");
const a = JSON.parse(process.env.AUDIT_ARGS);
const out = { ok: false };
try {
  const db = new DatabaseSync("/data/.openclaw/state/openclaw.sqlite", { readOnly: true });
  out.jobCount = db.prepare("select count(*) n from cron_jobs").get().n;
  if (a.list) {
    out.jobs = db.prepare("select name,schedule_expr,schedule_tz,last_run_status,consecutive_errors,next_run_at_ms from cron_jobs where enabled=1 order by name").all();
  } else {
    out.job = db.prepare("select job_id,name,enabled,schedule_expr,schedule_tz,next_run_at_ms,running_at_ms,payload_timeout_seconds,last_run_status,consecutive_errors from cron_jobs where name=?").get(a.job) || null;
    if (out.job) {
      const runs = db.prepare("select ts,status,summary,session_id,duration_ms,model from cron_run_logs where job_id=? order by ts desc limit ?").all(out.job.job_id, a.runs);
      for (const r of runs) {
        r.bad = [];
        const f = `/data/.openclaw/agents/main/sessions/${r.session_id}.jsonl`;
        if (!fs.existsSync(f)) { r.transcript = "missing"; continue; }
        for (const l of fs.readFileSync(f, "utf8").split("\n")) {
          let o; try { o = JSON.parse(l); } catch { continue; }
          const m = o.message || {};
          if (m.role !== "toolResult") continue;
          const c = Array.isArray(m.content) ? m.content.map(x => x.text || "").join("\n") : String(m.content || "");
          for (const line of c.split("\n")) if (/failed|Error|\b500\b|timeout|exited with code [1-9]/.test(line)) r.bad.push(line.trim().slice(0, 240));
        }
        r.summary = String(r.summary || "").slice(0, 120);
      }
      out.runs = runs;
    }
  }
  if (a.spendlogs) {
    const https = require("https");
    const envs = [["198pro", "https://cc.auto-link.com.cn/pro"], ["aliyun", "https://litellm.carher.net"]];
    const get = (base, key, p) => new Promise(res => {
      const t = Date.now(), u = new URL(base + p);
      const q = https.request({ hostname: u.hostname, path: u.pathname + u.search, headers: { Authorization: "Bearer " + key } }, r => {
        let d = ""; r.on("data", c => d += c);
        r.on("end", () => { let spend = null; try { spend = JSON.parse(d).reduce((s, x) => s + Number(x.spend || 0), 0); } catch {} res({ code: r.statusCode, ms: Date.now() - t, spend }); });
      });
      q.setTimeout(60000, () => { q.destroy(); res({ code: "timeout", ms: Date.now() - t }); });
      q.on("error", e => res({ code: "err " + e.message, ms: Date.now() - t }));
      q.end();
    });
    const add = (s, n) => { const d = new Date(s + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
    const today = new Date().toISOString().slice(0, 10);
    out.spendlogs = {};
    (async () => {
      for (const [label, base] of envs) {
        const key = (fs.readFileSync(`${a.ws}/.env.litellm.${label}`, "utf8").match(/LITELLM_MASTER_KEY=(.+)/) || [])[1];
        if (!key) { out.spendlogs[label] = "no master key"; continue; }
        const rows = [];
        for (let s = add(today, -a.days); s < today; s = add(s, 1)) rows.push({ day: s, ...(await get(base, key.trim(), `/spend/logs?start_date=${s}&end_date=${add(s, 1)}`)) });
        out.spendlogs[label] = rows;
      }
      out.ok = true; console.log(JSON.stringify(out));
    })();
  } else { out.ok = true; console.log(JSON.stringify(out)); }
} catch (e) { out.error = String(e.stack || e); console.log(JSON.stringify(out)); }
"""


def ssh(cmd: str, stdin: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", HOST, cmd],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def bj(ms: int | None) -> str:
    if not ms:
        return "-"
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M BJ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--instance", default="hermestest-14")
    ap.add_argument("--job", default=DEFAULT_JOB)
    ap.add_argument("--runs", type=int, default=1, help="L2 看最近几次（默认只判最新一次，避免历史失败把门禁钉成常红）")
    ap.add_argument("--list", action="store_true", help="列出所有 enabled job 后退出")
    ap.add_argument("--spendlogs", action="store_true", help="L3: 逐日实打 /spend/logs")
    ap.add_argument("--days", type=int, default=11, help="L3 回看天数（198 retention 10 天）")
    ap.add_argument("--sha", action="store_true", help="L4: 容器脚本 sha vs 仓库快照")
    a = ap.parse_args()

    if not os.environ.get("SSHPASS"):
        print("SSHPASS 未设置（188 cltx 密码）", file=sys.stderr)
        return 4

    env = json.dumps({"job": a.job, "runs": a.runs, "list": a.list, "spendlogs": a.spendlogs, "days": a.days, "ws": WS})
    # AUDIT_ARGS 走 docker exec -e，单引号包住 JSON（JSON 内不含单引号）
    r = ssh(f"docker exec -i -e AUDIT_ARGS='{env}' {a.instance} node --no-warnings -", stdin=REMOTE_JS)
    if r.returncode != 0 and not r.stdout.strip():
        print(f"ssh/docker 失败 rc={r.returncode}: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 4
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print(f"L0 RULER-BROKEN: 远端输出不是 JSON: {r.stdout[:300]} {r.stderr[:300]}")
        return 3

    # L0
    if not out.get("ok") or not out.get("jobCount"):
        print(f"L0 RULER-BROKEN: sqlite 读不到或 cron_jobs 为空: {out.get('error', '')[:300]}")
        return 3
    if a.list:
        for j in out["jobs"]:
            print(f"{j['name']:<45} {j['schedule_expr'] or '-':<14} {j['schedule_tz'] or '':<14} last={j['last_run_status']} errs={j['consecutive_errors']} next={bj(j['next_run_at_ms'])}")
        return 0
    job = out.get("job")
    if not job:
        print(f"L0 RULER-BROKEN: job '{a.job}' 不存在（共 {out['jobCount']} 个；--list 看名字）")
        return 3
    print(f"L0 OK  sqlite 可读，{out['jobCount']} 个 job，目标 {job['name']}")

    red = False
    # L1
    import time
    now = time.time() * 1000
    l1 = []
    if not job["enabled"]:
        l1.append("disabled")
    if not job["next_run_at_ms"] or not (now < job["next_run_at_ms"] < now + 36 * 3600e3):
        l1.append(f"next_run 异常 {bj(job['next_run_at_ms'])}")
    if job["running_at_ms"] and now - job["running_at_ms"] > (job["payload_timeout_seconds"] or 600) * 1000 * 2:
        l1.append(f"running_at 卡在 {bj(job['running_at_ms'])}")
    red |= bool(l1)
    print(f"L1 {'RED ' if l1 else 'OK  '} {job['schedule_expr']} {job['schedule_tz']} next={bj(job['next_run_at_ms'])} timeout={job['payload_timeout_seconds']}s {'; '.join(l1)}")

    # L2
    for run in out.get("runs", []):
        bad = run.get("bad", [])
        is_red = run["status"] != "ok" or bool(bad) or run.get("transcript") == "missing"
        red |= is_red
        tag = "RED " if is_red else "OK  "
        print(f"L2 {tag} {bj(run['ts'])} cron={run['status']} {run['duration_ms'] // 1000}s model={run['model']} summary={run['summary']!r}")
        if run.get("transcript") == "missing":
            print("        transcript 缺失，判不了是否部分失败")
        for b in bad[:6]:
            print(f"        ! {b}")

    # L3
    if a.spendlogs:
        for label, rows in out["spendlogs"].items():
            if isinstance(rows, str):
                print(f"L3 RED  {label}: {rows}")
                red = True
                continue
            slow = [x for x in rows if x["code"] != 200 or x["ms"] > 25000]
            red |= bool(slow)
            mx = max((x["ms"] for x in rows), default=0)
            print(f"L3 {'RED ' if slow else 'OK  '} {label}: {len(rows)} 天，最慢 {mx / 1000:.1f}s")
            for x in rows:
                if x["code"] != 200 or x["ms"] > 25000 or (x["spend"] or 0) > 0:
                    sp = f"${x['spend']:.0f}" if x["spend"] is not None else "-"
                    print(f"        {x['day']} {x['code']} {x['ms'] / 1000:5.1f}s spend={sp}")

    # L4
    if a.sha:
        for f in SHA_FILES:
            rr = ssh(f"docker exec {a.instance} sha256sum {WS}/scripts/{f}")
            live = (rr.stdout.split() or ["?"])[0]
            snap_path = os.path.join(SNAP_DIR, f)
            snap = hashlib.sha256(open(snap_path, "rb").read()).hexdigest() if os.path.exists(snap_path) else "missing"
            same = live == snap
            red |= not same
            print(f"L4 {'OK  ' if same else 'RED '} {f} live={live[:12]} repo={snap[:12]}")

    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
