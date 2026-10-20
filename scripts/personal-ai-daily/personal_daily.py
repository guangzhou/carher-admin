#!/usr/bin/env python3
"""个人 / 团队 AI 用量日报（demo）：每人只看自己的，leader 额外看自己团队的。

和 hermestest-14 发弋天国现单聊群的那份全员日报同一套做法（scripts/hermestest-cron/ai-usage-ledger/daily_card.py）：
HTML → headless Chromium 1080 宽 @2x 截图 → `im/v1/images` 上传 → 卡片 = 图 + 按钮 + 折叠文字。
数据也是同一份：全员日报每天 08:00 重建的总账页里内嵌的 `people`（每人 194 天逐日用量，三套网关已合并到人）。
发送方换成 `ai管理的her`（cli_aa8505…），一人一条私聊。

谁是 leader、团队里有谁：组织关系确认表（tbln0k5HFwdmkkxr）的「角色 = leader」+「归属leader(人)」。
通讯录的 leader_user_id 对跨租户的人读不到，确认表是唯一同时覆盖内外部的来源。

隐私边界（这份日报的核心要求）：
  - 成员卡：只有本人的数，**不出现任何别人的名字或金额**；排名只给「前 x%」这种位置，不给邻居是谁。
  - leader 卡：本人 + 自己团队成员；不出现团队外的人。
  - 全公司只给一个对比基准（人均），不出现任何个人。

用法：
  python3 scripts/personal-ai-daily/personal_daily.py fetch                  # 从 188 拉总账数据 + 读确认表
  python3 scripts/personal-ai-daily/personal_daily.py render --who 刘国现     # 出图到 .work/personal-daily/out/
  python3 scripts/personal-ai-daily/personal_daily.py send --who 刘国现 --to ou_xxx   # 发一张给指定的人看
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work" / "personal-daily"
CHROMIUM = os.environ.get("CHROMIUM", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
W = 1080
WEEK = "一二三四五六日"
BASE, CONF_TBL = "DlT9bsrwMad12VsogEpcK9Ptncc", "tbln0k5HFwdmkkxr"
LEDGER_URL = "https://t83dfrspj4.feishu.cn/file/G391bcnZTodPljxRBejcYvSQnFR"
MYAI = "https://chat.auto-link.com.cn/myai"
ONECLICK = "https://chat.auto-link.com.cn/oneclick/"
TOOLS = ("Cursor", "Claude Code", "Her")
TOOL_COLOR = {"Cursor": "#1f6f5c", "Claude Code": "#c2832e", "Her": "#7a5aa8"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def money(v: float) -> str:
    return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}"


def money_k(v: float) -> str:
    if v >= 1e6: return f"${v / 1e6:.2f}M"
    if v >= 1e4: return f"${v / 1e3:.1f}k"
    if v >= 1000: return f"${v / 1e3:.2f}k"
    return f"${v:,.0f}" if v >= 10 else f"${v:.1f}"


def esc(s) -> str:
    return html.escape(str(s or ""))


def txt(v) -> str:
    if v is None: return ""
    if isinstance(v, list):
        return ",".join((x.get("text") or x.get("name") or "") if isinstance(x, dict) else str(x) for x in v)
    return str(v)


# ---------------------------------------------------------------- 数据
def cmd_fetch(a) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    raw = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-n", "cltx@10.68.13.188",
                          "docker exec hermestest-14 cat /data/.openclaw/workspace/data/ai-usage-ledger.html"],
                         capture_output=True, timeout=120).stdout.decode("utf-8-sig")
    i = raw.find('{"meta":')
    if i < 0:
        log("🔴 总账页里找不到数据块"); return 3
    payload, _ = json.JSONDecoder().raw_decode(raw[i:])
    (WORK / "payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    conf = read_conf(tenant_token())
    (WORK / "conf.json").write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")
    log(f"总账 {payload['meta']['yday']}：{len(payload['people'])} 人；确认表 {len(conf)} 行")
    return 0


def tenant_token() -> str:
    body = json.dumps({"app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"]}).encode()
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", data=body,
        headers={"Content-Type": "application/json"}), timeout=20).read())
    if d.get("code") != 0:
        raise SystemExit(f"换不到 token：{d}")
    return d["tenant_access_token"]


def read_conf(token: str) -> list[dict]:
    rows, pt = [], ""
    while True:
        q = {"page_size": "500", "user_id_type": "open_id"} | ({"page_token": pt} if pt else {})
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE}/tables/{CONF_TBL}/records/search?" + urllib.parse.urlencode(q),
            data=json.dumps({"field_names": ["姓名", "本人", "归属leader(人)", "角色", "部门路径"]}).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {token}"); req.add_header("Content-Type", "application/json")
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
        rows += d.get("items") or []
        if not d.get("has_more"): return rows
        pt = d.get("page_token")


class World:
    """一次加载：人、团队、全公司基准。"""

    def __init__(self):
        p = json.loads((WORK / "payload.json").read_text())
        self.meta, self.dates = p["meta"], p["dates"]
        self.people = [x for x in p["people"] if x["tag"] != "服务"]
        self.by_name = collections.defaultdict(list)
        for x in self.people:
            self.by_name[re.sub(r"（.*?）$", "", x["n"])].append(x)
        self.conf = json.loads((WORK / "conf.json").read_text())
        self.leaders, self.team = set(), collections.defaultdict(list)
        for r in self.conf:
            f = r["fields"]
            if txt(f.get("角色")) == "leader":
                self.leaders.add(txt(f.get("姓名")))
            lp = f.get("归属leader(人)") or []
            nm = txt(f.get("姓名"))
            # 确认表里同一个人可能有两行（10-19 杜小畔、刘咏强各两行）⇒ 按名字去重
            if lp and lp[0].get("name") != nm and nm not in self.team[lp[0]["name"]]:
                self.team[lp[0]["name"]].append(nm)
        yi = len(self.dates) - 1
        # 「昨天」一律用 y（北京自然日，SpendLogs 现算）：全员日报的昨日数就是它。
        # s[-1] 是网关日汇总（≈UTC 日），两者对不上（10-19 刘国现 s=$2.94k / y=$3.54k），
        # 混用会让个人日报和群日报对同一个人给出两个「昨天」。
        active = [x for x in self.people if x["y"] > 0]
        self.co_avg_y = sum(x["y"] for x in active) / len(active) if active else 0
        self.co_active = len(active)
        # 近 30 天在用的人里的排位（用来给「前 x%」，不暴露任何邻居）
        self.d30_sorted = sorted((x["d30"] for x in self.people if x["d30"] > 0), reverse=True)

    def person(self, name: str) -> dict | None:
        xs = self.by_name.get(name) or []
        return xs[0] if len(xs) == 1 else None      # 同名不猜

    def resolve(self, name: str) -> tuple[str, dict | None]:
        """('ok', 记录) / ('ambiguous', None) 同名多人认不出 / ('absent', None) 总账里确实没有。
        ⚠️ 总账 people 只存名字不存 open_id ⇒ 同名只能报「认不出」，绝不能当「没用过」报给 leader。"""
        xs = self.by_name.get(name) or []
        if len(xs) == 1:
            return "ok", xs[0]
        return ("ambiguous" if xs else "absent"), None

    def pct_rank(self, d30: float) -> int:
        if d30 <= 0 or not self.d30_sorted: return 0
        above = sum(1 for v in self.d30_sorted if v > d30)
        return max(1, round(100 * (above + 1) / len(self.d30_sorted)))


# ---------------------------------------------------------------- 图
CSS = '''*{box-sizing:border-box;margin:0;padding:0}
body{width:%dpx;padding-bottom:1px;background:#f6f7f4;color:#1b1f1d;font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.num,.kv,.vv,.ax,.lab{font-family:"Noto Sans Mono CJK SC","SF Mono",Menlo,monospace;font-variant-numeric:tabular-nums}
.hero{background:linear-gradient(135deg,#123f35 0%%,#1f6f5c 55%%,#2b7f69 100%%);color:#fff;padding:44px 64px 40px;position:relative;overflow:hidden}
.hero:after{content:"";position:absolute;right:-120px;top:-160px;width:460px;height:460px;border-radius:50%%;background:radial-gradient(circle,rgba(255,255,255,.14),rgba(255,255,255,0) 70%%)}
.brand{font-size:20px;letter-spacing:.12em;opacity:.8}
h1{font-size:52px;font-weight:900;letter-spacing:.03em;margin-top:10px}
.date{font-size:24px;opacity:.86;margin-top:6px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:30px;position:relative;z-index:1}
.kpi{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);border-radius:16px;padding:18px 20px}
.kk{font-size:18px;opacity:.78}.kv{font-size:40px;font-weight:600;margin-top:4px}.ks{font-size:16px;opacity:.72;margin-top:2px}
.sec{margin:28px 48px 0;background:#fff;border:1px solid #e1e5df;border-radius:18px;padding:26px 28px}
.h2{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px}
.h2 b{font-size:26px;font-weight:700}.h2 span{font-size:16px;color:#6a756f}
.lg i{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 6px 0 16px;vertical-align:-1px}
.ax{font-size:14px;fill:#7b867f}.lab{font-size:14px;font-weight:600;fill:#fff}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:28px 48px 0}.two .sec{margin:0}
.br{display:grid;grid-template-columns:28px 172px 1fr 96px;gap:10px;align-items:center;height:52px}
.ix{font-size:16px;color:#8a948e;font-family:"Noto Sans Mono CJK SC",monospace}
.nm{overflow:hidden;white-space:nowrap;line-height:1.15}.nm b{font-size:19px;font-weight:600}.nm small{display:block;font-size:14px;color:#7b867f;overflow:hidden;text-overflow:ellipsis}
.tr{height:14px;background:#edf0ec;border-radius:7px;overflow:hidden}.tr i{display:block;height:100%%;border-radius:7px}
.vv{text-align:right;font-size:19px;font-weight:600}
.mix{display:flex;height:22px;border-radius:11px;overflow:hidden;margin:6px 0 14px}
.mix i{display:block;height:100%%}
.ml{display:flex;gap:28px;font-size:18px}.ml span i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:8px;vertical-align:-1px}
.ml b{font-family:"Noto Sans Mono CJK SC",monospace;margin-left:6px}
.pos{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.tl{background:linear-gradient(180deg,#f2f7f4,#fff);border:1px solid #dce8e2;border-radius:14px;padding:16px 18px}
.tk{font-size:16px;color:#5f6a64}.tv{font-size:38px;font-weight:600;color:#1f6f5c;margin-top:2px;font-family:"Noto Sans Mono CJK SC",monospace}
.tv span{font-size:18px;margin-left:4px;color:#3f7d6c;font-family:"Noto Sans CJK SC","PingFang SC",sans-serif}
.ts{font-size:15px;color:#6a756f;margin-top:2px}
.quiet{color:#9aa39e}
.foot{margin:22px 48px 0;padding-bottom:36px;font-size:15px;color:#7b867f;display:flex;justify-content:space-between}
''' % W


def trend_svg(series: list[float], dates: list[str], w: int = 926, h: int = 220, avg_line: list[float] | None = None) -> str:
    D, v = dates[-30:], series[-30:]
    n = len(v)
    pad_l, pad_r, pad_b, pad_t = 62, 30, 30, 28
    mx = (max(v) if max(v) > 0 else 1) * 1.15
    bw = (w - pad_l - pad_r) / n
    y = lambda x: pad_t + (h - pad_t - pad_b) * (1 - x / mx)
    out = []
    for k in range(4):
        gv = mx / 1.15 * k / 3
        out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(gv):.1f}" y2="{y(gv):.1f}" stroke="#e4e8e3"/>'
                   f'<text x="{pad_l - 8}" y="{y(gv) + 4:.1f}" text-anchor="end" class="ax">{money_k(gv)}</text>')
    for i, (d, x) in enumerate(zip(D, v)):
        wkend = dt.date.fromisoformat(d).weekday() >= 5
        last = i == n - 1
        col = "#c2832e" if last else ("#9cc3b5" if wkend else "#1f6f5c")
        x0 = pad_l + i * bw + bw * 0.16
        out.append(f'<rect x="{x0:.1f}" y="{y(x):.1f}" width="{bw * 0.68:.1f}" height="{max(1, h - pad_b - y(x)):.1f}" rx="2.5" fill="{col}"/>')
        mon = dt.date.fromisoformat(d).weekday() == 0
        first_ok = i == 0 and all(dt.date.fromisoformat(D[j]).weekday() != 0 for j in range(1, min(3, n)))
        if mon or first_ok or last:
            out.append(f'<text x="{x0 + bw * 0.34:.1f}" y="{h - 8}" text-anchor="middle" class="ax">{int(d[5:7])}/{int(d[8:])}</text>')
    if avg_line:
        a = avg_line[-30:]
        pts = " ".join(f"{pad_l + i * bw + bw / 2:.1f},{y(min(val, mx)):.1f}" for i, val in enumerate(a))
        out.append(f'<polyline points="{pts}" fill="none" stroke="#1b1f1d" stroke-width="2" stroke-dasharray="5 4" opacity=".5"/>')
    if v[-1] > 0:
        xl = pad_l + (n - 1) * bw + bw / 2
        out.append(f'<rect x="{xl - 42:.1f}" y="{y(v[-1]) - 30:.1f}" width="84" height="22" rx="11" fill="#c2832e"/>'
                   f'<text x="{xl:.1f}" y="{y(v[-1]) - 14:.1f}" text-anchor="middle" class="lab">{money_k(v[-1])}</text>')
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">{"".join(out)}</svg>'


def mix_bar(ty: dict) -> str:
    tot = sum(ty.get(k, 0) for k in TOOLS) or 1
    bar = "".join(f'<i style="width:{100 * ty.get(k, 0) / tot:.2f}%;background:{TOOL_COLOR[k]}"></i>' for k in TOOLS if ty.get(k))
    lab = "".join(f'<span><i style="background:{TOOL_COLOR[k]}"></i>{k}<b>{100 * ty.get(k, 0) / tot:.0f}%</b></span>' for k in TOOLS if ty.get(k))
    return f'<div class="mix">{bar}</div><div class="ml">{lab}</div>'


def sign(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x) * 100:.0f}%"


def kpi(k, v, s) -> str:
    return f'<div class="kpi"><div class="kk">{k}</div><div class="kv">{v}</div><div class="ks">{s}</div></div>'


def hero(world: World, title: str, sub: str, kpis: list[str]) -> str:
    yd = world.meta["yday"]
    wd = WEEK[dt.date.fromisoformat(yd).weekday()]
    return f'''<div class="hero"><div class="brand">AI 技术院 · {esc(title)}</div>
<h1>{int(yd[5:7])} 月 {int(yd[8:])} 日 · 周{wd}</h1><div class="date">{esc(sub)}</div>
<div class="kpis">{"".join(kpis)}</div></div>'''


def person_facts(world: World, x: dict) -> dict:
    s = x["s"]; yi = len(s) - 1
    same_wd = s[yi - 7] if yi >= 7 else 0
    wk, pw = sum(s[-7:]) / 7, sum(s[-14:-7]) / 7
    active_days = sum(1 for v in s[-30:] if v > 0)
    first = world.dates[x["f"]] if s[x["f"]] > 0 else ""
    return dict(y=x["y"], same_wd=same_wd, wk=wk, pw=pw, d30=x["d30"], total=x["t"],
                active30=active_days, first=first, pct=world.pct_rank(x["d30"]))


def member_page(world: World, x: dict) -> str:
    f = person_facts(world, x)
    yd = world.meta["yday"]; wd = WEEK[dt.date.fromisoformat(yd).weekday()]
    k = [kpi("昨日用量", money_k(f["y"]), f"上周{wd} {money_k(f['same_wd'])}" + (f" · {sign(f['y'] / f['same_wd'] - 1)}" if f["same_wd"] else "")),
         kpi("近 7 天日均", money_k(f["wk"]), f"前 7 天 {money_k(f['pw'])}" + (f" · {sign(f['wk'] / f['pw'] - 1)}" if f["pw"] else "")),
         kpi("近 30 天", money_k(f["d30"]), f"用了 {f['active30']} 天"),
         kpi("累计", money_k(f["total"]), f"自 {int(f['first'][5:7])} 月 {int(f['first'][8:])} 日起" if f["first"] else "")]
    co = world.co_avg_y
    pos = [("近 30 天用量位置", f"前 {f['pct']}<span>%</span>" if f["pct"] else "—", f"在 {len(world.d30_sorted)} 位近 30 天用过的同事里"),
           ("昨天 vs 全员人均", (f"{f['y'] / co:.1f}<span>倍</span>" if co and f["y"] else "—"), f"全员昨天人均 {money_k(co)}，{world.co_active} 人在用"),
           ("近 30 天活跃", f"{f['active30']}<span>/ 30 天</span>", "每天至少用过一次算一天")]
    pos_html = "".join(f'<div class="tl"><div class="tk">{a}</div><div class="tv">{b}</div><div class="ts">{c}</div></div>' for a, b, c in pos)
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
{hero(world, "我的 AI 用量日报", f"{x['n']} · {x['l2'] if x['l2'] != x['l1'] else x['l1']}", k)}
<div class="sec"><div class="h2"><b>我近 30 天每日用量</b><span class="lg"><i style="background:#1f6f5c"></i>工作日<i style="background:#9cc3b5"></i>周末<i style="background:#c2832e"></i>昨天</span></div>
{trend_svg(x["s"][:-1] + [x["y"]], world.dates)}</div>
<div class="sec"><div class="h2"><b>我用的工具</b><span>累计</span></div>{mix_bar(x["ty"])}</div>
<div class="sec"><div class="h2"><b>我在哪</b><span>只给位置和人均，不出现任何其他人</span></div><div class="pos">{pos_html}</div></div>
<div class="foot"><span>金额按模型公开价折算，用来看用量多少，不是实际付款</span><span>只有你本人能看到这份</span></div>
</body></html>'''


def leader_page(world: World, x: dict, members: list[dict], missing: list[str], ambiguous: list[str]) -> str:
    yi = len(world.dates) - 1
    me = person_facts(world, x)
    allp = [x] + members
    team_s = [sum(p["s"][i] for p in allp) for i in range(len(world.dates))]
    ty = collections.Counter(); [ty.update(p["ty"]) for p in allp]
    team_s[yi] = sum(p["y"] for p in allp)          # 最后一天换北京日口径
    t_y, t_same = team_s[yi], team_s[yi - 7] if yi >= 7 else 0
    t_wk, t_pw = sum(team_s[-7:]) / 7, sum(team_s[-14:-7]) / 7
    act_y = sum(1 for p in allp if p["y"] > 0)
    act_30 = sum(1 for p in allp if p["d30"] > 0)
    head = len(allp) + len(missing) + len(ambiguous)
    yd = world.meta["yday"]; wd = WEEK[dt.date.fromisoformat(yd).weekday()]
    k = [kpi("团队昨日", money_k(t_y), f"上周{wd} {money_k(t_same)}" + (f" · {sign(t_y / t_same - 1)}" if t_same else "")),
         kpi("昨日在用", f"{act_y}<span style='font-size:22px'> / {head}</span>", "人（含你）"),
         kpi("团队近 7 天日均", money_k(t_wk), f"前 7 天 {money_k(t_pw)}" + (f" · {sign(t_wk / t_pw - 1)}" if t_pw else "")),
         kpi("我昨日", money_k(me["y"]), f"近 30 天 {money_k(me['d30'])}")]
    rows = sorted(allp, key=lambda p: (-p["y"], -p["d30"]))
    mx = max([p["y"] for p in rows] + [1])
    br = []
    for i, p in enumerate(rows[:12]):
        v = p["y"]
        tools = "+".join(t for t in TOOLS if p["ty"].get(t))
        me_tag = ' <em style="font-style:normal;font-size:12px;color:#c2832e;border:1px solid currentColor;border-radius:10px;padding:0 7px">我</em>' if p is x else ""
        br.append(f'<div class="br"><span class="ix">{i + 1}</span><span class="nm"><b>{esc(re.sub(r"（.*?）$", "", p["n"]))}</b>{me_tag}<small>{esc(tools)}</small></span>'
                  f'<span class="tr"><i style="width:{100 * v / mx:.1f}%;background:{"#c2832e" if p is x else "#1f6f5c"}"></i></span>'
                  f'<span class="vv{"" if v else " quiet"}">{money_k(v) if v else "未使用"}</span></div>')
    # 近 30 天一次没用过的人：这是 leader 最该知道的
    idle = [p for p in members if p["d30"] <= 0]
    idle_names = [re.sub(r"（.*?）$", "", p["n"]) for p in idle] + missing
    idle_html = (f'<div style="font-size:18px;line-height:1.8">{"、".join(esc(n) for n in idle_names)}</div>'
                 if idle_names else '<div style="font-size:18px;color:#1f6f5c">全员近 30 天都用过 👍</div>')
    if ambiguous:
        idle_html += (f'<div style="font-size:15px;color:#7b867f;margin-top:10px">另有 {len(ambiguous)} 人同名、'
                      f'暂时认不出是哪一位，没计入：{"、".join(esc(n) for n in ambiguous)}</div>')
    more = f'<div style="font-size:15px;color:#7b867f;margin-top:6px">另有 {len(rows) - 12} 人见下方文字明细</div>' if len(rows) > 12 else ""
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
{hero(world, "团队 AI 用量日报", f"{x['n']} 的团队 · {head} 人（确认表归属）", k)}
<div class="sec"><div class="h2"><b>团队近 30 天每日用量</b><span class="lg"><i style="background:#1f6f5c"></i>工作日<i style="background:#9cc3b5"></i>周末<i style="background:#c2832e"></i>昨天</span></div>
{trend_svg(team_s, world.dates)}</div>
<div class="two">
  <div class="sec"><div class="h2"><b>昨日 · 成员</b><span>{act_y} 人在用</span></div>{"".join(br)}{more}</div>
  <div class="sec"><div class="h2"><b>工具分布</b><span>团队累计</span></div>{mix_bar(ty)}
    <div class="h2" style="margin-top:28px"><b>近 30 天没用过</b><span>{len(idle_names)} 人</span></div>{idle_html}
    <div class="h2" style="margin-top:28px"><b>团队覆盖</b><span>近 30 天</span></div>
    <div style="font-size:40px;font-weight:600;color:#1f6f5c;font-family:'Noto Sans Mono CJK SC',monospace">{act_30}<span style="font-size:20px;color:#3f7d6c"> / {head} 人用过</span></div></div>
</div>
<div class="foot"><span>金额按模型公开价折算，用来看用量多少，不是实际付款</span><span>只发给团队 leader 本人</span></div>
</body></html>'''


def render(doc: str, png: Path) -> int:
    png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "card.html"
        hp.write_text(doc.replace("</body>", '<script>document.title="H="+Math.ceil(document.body.scrollHeight)</script></body>'), encoding="utf-8")
        base = [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--disable-dev-shm-usage",
                f"--user-data-dir={td}/prof", "--virtual-time-budget=3000"]
        dom = subprocess.run(base + [f"--window-size={W},4000", "--dump-dom", hp.as_uri()], capture_output=True, text=True, timeout=120).stdout
        m = re.search(r"<title>H=(\d+)</title>", dom)
        if not m:
            raise RuntimeError("量不到页面高度")
        h = int(m.group(1))
        hp.write_text(doc, encoding="utf-8")
        subprocess.run(base + [f"--window-size={W},{h}", "--force-device-scale-factor=2", f"--screenshot={png}", hp.as_uri()],
                       capture_output=True, text=True, timeout=120)
    if not png.exists() or png.stat().st_size < 20000:
        raise RuntimeError(f"截图失败或太小：{png}")
    return h


# ---------------------------------------------------------------- 卡片 / 发送
def upload_image(token: str, png: Path) -> str:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"daily.png\"\r\nContent-Type: image/png\r\n\r\n").encode() \
        + png.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request("https://open.feishu.cn/open-apis/im/v1/images", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        d = json.loads(e.read() or b"{}")
    if d.get("code") != 0:
        raise RuntimeError(f"上传图片失败：{d.get('code')} {d.get('msg')}")
    return d["data"]["image_key"]


def card_json(world: World, kind: str, x: dict, img_key: str, detail: str) -> dict:
    yd = world.meta["yday"]
    title = "我的 AI 用量日报" if kind == "member" else "团队 AI 用量日报"
    sub = f"{x['n']} · 昨日 {money_k(x['y'])}" if kind == "member" else f"{x['n']} 的团队"
    els = [{"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": f"{title} {yd}"},
            "scale_type": "fit_horizontal", "preview": True, "corner_radius": "8px"},
           {"tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "8px", "margin": "8px 0px 0px 0px",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "button", "type": "primary_filled", "width": "fill", "text": {"tag": "plain_text", "content": "查看我的使用明细"},
                     "behaviors": [{"type": "open_url", "default_url": MYAI}]}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "button", "type": "default", "width": "fill", "text": {"tag": "plain_text", "content": "Cursor · Codex · Claude Code 一键配置"},
                     "behaviors": [{"type": "open_url", "default_url": ONECLICK}]}]}]},
           {"tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "markdown", "content": "<font color='grey'>文字明细</font>"},
                       "icon": {"tag": "standard_icon", "token": "down_outlined", "size": "16px 16px"},
                       "icon_position": "right", "icon_expanded_angle": -180},
            "border": {"color": "grey", "corner_radius": "6px"}, "padding": "8px 10px 8px 10px",
            "elements": [{"tag": "markdown", "content": detail}]}]
    return {"schema": "2.0",
            "config": {"summary": {"content": f"{title} {yd[5:]}"}},
            "header": {"title": {"tag": "plain_text", "content": f"{title} · {int(yd[5:7])}月{int(yd[8:])}日"},
                       "subtitle": {"tag": "plain_text", "content": sub},
                       "template": "turquoise",
                       "text_tag_list": [{"tag": "text_tag", "text": {"tag": "plain_text", "content": "仅你可见"}, "color": "neutral"}]},
            "body": {"elements": els}}


def build(world: World, who: str) -> tuple[str, dict, str, list[str]]:
    """返回 (kind, 本人, html, 文字明细, 出现在图里的人名列表——给隐私门禁用)"""
    x = world.person(who)
    if not x:
        raise SystemExit(f"总账里找不到唯一的「{who}」（没用过、累计 <$20 或同名）")
    if who in world.leaders and world.team.get(who):
        members, missing, ambiguous = [], [], []
        for n in world.team[who]:
            st, p = world.resolve(n)
            {"ok": members, "absent": missing, "ambiguous": ambiguous}[st].append(p if st == "ok" else n)
        doc = leader_page(world, x, members, missing, ambiguous)
        yi = len(world.dates) - 1
        lines = [f"**团队 {len(members) + len(missing) + len(ambiguous) + 1} 人**（确认表归属你）"]
        for p in sorted([x] + members, key=lambda p: -p["y"]):
            lines.append(f"- {re.sub(r'（.*?）$', '', p['n'])}　昨日 {money(p['y'])}　近 30 天 {money(p['d30'])}")
        for n in missing:
            lines.append(f"- {n}　<font color='grey'>近半年没有用量记录</font>")
        for n in ambiguous:
            lines.append(f"- {n}　<font color='grey'>同名多人，暂时认不出，未计入</font>")
        named = [re.sub(r"（.*?）$", "", p["n"]) for p in [x] + members] + missing + ambiguous
        return "leader", x, doc, "\n".join(lines), named
    doc = member_page(world, x)
    f = person_facts(world, x)
    lines = [f"昨日 {money(f['y'])} · 近 7 天日均 {money(f['wk'])} · 近 30 天 {money(f['d30'])} · 累计 {money(f['total'])}",
             "工具：" + " / ".join(f"{t} {money(x['ty'][t])}" for t in TOOLS if x["ty"].get(t))]
    return "member", x, doc, "\n".join(lines), [re.sub(r"（.*?）$", "", x["n"])]


def privacy_gate(world: World, doc: str, allowed: list[str]) -> list[str]:
    """图里不许出现名单之外的任何同事名字。返回泄露的名字。"""
    body = re.sub(r"<style>[\s\S]*?</style>", "", doc)
    names = {re.sub(r"（.*?）$", "", p["n"]) for p in world.people}
    return sorted(n for n in names if len(n) >= 2 and n not in allowed and n in body)


def cmd_render(a) -> int:
    world = World()
    for who in a.who:
        kind, x, doc, detail, named = build(world, who)
        leak = privacy_gate(world, doc, named)
        if leak:
            log(f"🔴 {who}：图里出现了不该看到的人 {leak[:5]}"); return 1
        png = WORK / "out" / f"{who}-{kind}.png"
        h = render(doc, png)
        (WORK / "out" / f"{who}-{kind}.html").write_text(doc, encoding="utf-8")
        (WORK / "out" / f"{who}-{kind}.detail.md").write_text(detail, encoding="utf-8")
        log(f"{who}：{kind}，{W}x{h} @2x，{png.stat().st_size // 1024} KB，图里出现 {len(named)} 人 → {png}")
    return 0


def cmd_send(a) -> int:
    world = World()
    token = tenant_token()
    for who in a.who:
        kind, x, doc, detail, named = build(world, who)
        if privacy_gate(world, doc, named):
            log(f"🔴 {who} 隐私门禁不过，不发"); return 1
        png = WORK / "out" / f"{who}-{kind}.png"
        render(doc, png)
        key = upload_image(token, png)
        card = card_json(world, kind, x, key, detail)
        body = {"receive_id": a.to, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        req = urllib.request.Request("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            d = json.loads(e.read() or b"{}")
        log(f"{who}（{kind}）→ {a.to}：code={d.get('code')} {(d.get('data') or {}).get('message_id') or d.get('msg')}")
        if d.get("code") != 0:
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    r = sub.add_parser("render"); r.add_argument("--who", nargs="+", required=True)
    s = sub.add_parser("send"); s.add_argument("--who", nargs="+", required=True); s.add_argument("--to", required=True)
    a = ap.parse_args()
    return {"fetch": cmd_fetch, "render": cmd_render, "send": cmd_send}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
