#!/usr/bin/env python3
"""Write the "add these mihomo exits as kiro 30891 slots" handoff from live measurements.

Every number in the doc is measured at generation time through `cli-anything-mihomo exit table`
(exit IP + country from ipinfo, median time to the Kiro endpoint). Nothing is typed by hand,
so the doc can't drift from what 198 is actually running.

  scripts/kiro-slots-egress-handoff.py --account acct2 --ports 18891-18895
  scripts/kiro-slots-egress-handoff.py --account default --ports 17891,17892 --no-country
  scripts/kiro-slots-egress-handoff.py ... --out .work/handoff/x.md      # default: stdout

--no-country: leave country/city/org/node names out (槽名 = px-<IP>). The exit IP itself still
reveals the country to anyone who looks it up; the doc says so.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys

COUNTRY_ZH = {"US": "美国", "GB": "英国", "JP": "日本", "SG": "新加坡", "HK": "香港", "TW": "台湾", "KR": "韩国",
              "PH": "菲律宾", "MY": "马来西亚", "DE": "德国", "FR": "法国", "TR": "土耳其", "IN": "印度",
              "VN": "越南", "CA": "加拿大", "AU": "澳大利亚"}


def parse_ports(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def weight(ms: int | None) -> int:
    """Lower weight for slower exits; the existing JP slot (198 direct) is ~0.6s."""
    if ms is None:
        return 0
    return 3 if ms < 1300 else 2 if ms < 1800 else 1


def measure(account: str, samples: int) -> list[dict]:
    cmd = ["cli-anything-mihomo", "--json", "--account", account, "exit", "table", "--samples", str(samples)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        sys.exit(f"STOP: {' '.join(cmd)} failed:\n{p.stderr.strip()}")
    return json.loads(p.stdout)


def render(account: str, rows: list[dict], with_country: bool, samples: int) -> str:
    today = dt.date.today().isoformat()
    n = len(rows)
    ip_head = "| 端口（198 本机） | 出口 IP |" + (" IP 归属（ipinfo 实测） | 订阅里的节点名 |" if with_country else "")
    ip_head += f" 经它连 Kiro（`q.us-east-1`，{samples} 次中位） | 建议权重 | 建议槽名 |"
    sep = "|" + "|".join(["---"] * (ip_head.count("|") - 1)) + "|"
    lines = [ip_head, sep]
    for r in rows:
        cc = r.get("country") or "?"
        name = f"{cc.lower()}-{r['exit_ip']}" if with_country else f"px-{r['exit_ip']}"
        cell = f"| `127.0.0.1:{r['port']}` | {r['exit_ip']} |"
        if with_country:
            where = " ".join(x for x in (COUNTRY_ZH.get(cc, cc), r.get("city")) if x)
            cell += f" {where}（{r.get('org') or '-'}） | {r['exit_node']} |"
        ms = r.get("kiro_ms")
        cell += f" {ms / 1000:.2f}s（{r['kiro_ok']}） | {weight(ms)} | `{name}` |" if ms else f" **不通**（{r['kiro_ok']}） | — | 不要加 |"
        lines.append(cell)
    table = "\n".join(lines)
    acct_flag = f"--account {account}"
    country_note = ("国家按出口 IP 实测（经该端口打 `ipinfo.io`），不是按节点名猜的（订阅里有节点名写「德国」实际出口在法国）。"
                    if with_country else
                    "本文不写出口所在地。注意：出口 IP 本身拿去任何 IP 库一查就能看到归属，藏不住。")
    return f"""# Handoff：给 kiro 槽位（30891）加 {n} 个出口槽

## 一句话
198 上已经起好 {n} 个代理端口，每个端口固定一个出口 IP，下表数字都是 {today} 生成本文时实测的。现在要把它们接成 30891 Admin 里的新槽。
**现在只差一件事**：这些端口只监听 198 的 `127.0.0.1`，可 kiro-rs-slots 跑在 **242**，连不到。

## 现状（{today} 实测）

{table}

{country_note}
对照：现有日本槽（198 直连出口）约 0.6s。打 `q.us-east-1.amazonaws.com` 回 404 = 通了（没带路径的正常回包）。

| 项 | 值 |
|---|---|
| 代理程序 | mihomo，198 上以 cltx 用户跑，实例 `{account}`，非 systemd、**198 重启不会自启** |
| 管理工具 | 本机 `cli-anything-mihomo {acct_flag} …`（说明：`.claude/skills/cli-anything-mihomo/SKILL.md`） |
| 端口类型 | mixed（HTTP + SOCKS5），每个端口**钉死单节点**，出口 IP 固定 |
| 订阅 | `cli-anything-mihomo {acct_flag} sub info` 看剩余流量和到期 |
| kiro-rs-slots | ns `kiro-rs`，pod 钉在节点 `aiyjy-litellm-242`（nodeSelector），无 NetworkPolicy |

## 要做的三步

### 1. 让 242 能连到这些代理（二选一，**动生产前先跟刘国现确认**）

- **A（推荐）做成 k3s 服务**：在 k3s 里起一个 mihomo Deployment（单副本，配置用
  `cli-anything-mihomo {acct_flag} config render` 生成的那份，含节点密码 ⇒ 放 Secret），
  `listeners` 里这几条保持 `proxy:` 不变，监听改成 `0.0.0.0`，**每条加 `users: [{{username, password}}]`**，
  只通过 ClusterIP Service 暴露这些端口。槽位填 `http://<svc ClusterIP>:<端口>`。好处：重启自愈、只在集群内可达。
- **B 临时**：198 上把监听改成 198 内网 IP + 加密码。工具**故意只允许 127.0.0.1**（`tools/cli-anything-mihomo/cli_anything/mihomo/core/config.py` 的 audit 会拒绝），要改代码；198 重启后也不会自己起。不建议长期用。

注意：
- 别用 ShellCrash 的任何「路由/透明代理」模式，会改 198（k3s master）的 iptables、sysctl、resolv.conf。只能是「开端口的普通代理」。
- 控制接口不能对外，secret 为空 = 不鉴权。

### 2. 先从 242 逐个验代理（不经 Admin）

```bash
# 在 242 上（或 kiro-rs ns 里起个临时 curl pod），每个端口各跑一遍
P=http://<用户>:<密码>@<地址>:<端口>
curl -s -o /dev/null -w '%{{http_code}}\\n' -x $P https://q.us-east-1.amazonaws.com/   # 期望 404
curl -s -x $P https://ipinfo.io/ip                                                  # 期望 = 上表该端口的出口 IP
curl -s -o /dev/null -w '%{{http_code}}\\n' -x http://<地址>:<端口> https://q.us-east-1.amazonaws.com/  # 不带密码，期望 407/000
```

### 3. 在 Admin 加槽（一次一个）

打开 `http://10.68.13.198:30891/admin` → 设置 → 槽位配置 → 每个端口新增一行：

| 栏 | 填什么 |
|---|---|
| 名字 | 上表「建议槽名」 |
| 出口代理地址 | `http://<地址>:<端口>`（只能是 http/https/socks5 开头，**账号密码不要写进地址**，否则保存报错） |
| 用户名 / 密码 | 第 1 步设的那对 |
| 权重 | 上表「建议权重」（默认 5，这里按延迟给低，观察后再加） |
| 接新人 | 先开；想只给少数人试就关掉 + 填「专属 key」 |

每加一个：保存 → 点该槽「测试」（= `POST /slots/<i>/test`，打 hello + tool 两发），两发都过再加下一个。
全部加完后 30 分钟看 `python3 scripts/kiro-slots-watch.py --minutes 30` 每个新槽的：首发 429 / 被限 / 排队超时，以及「首字 Xs」。

## 风险 / 要知道的

- **延迟比现有槽高**：每次请求多 0.5~2s 握手。长对话无感，短请求明显。
- **节点挂了对应槽就挂**（钉死单节点，不会自动换）。处理：Admin 先停用该槽，告诉刘国现换节点（IP 会变，槽名跟着改）。
  现量：`cli-anything-mihomo {acct_flag} exit table`（每个端口：出口 IP + 连 Kiro 耗时）。
- 这是机场节点，**多人共用 IP**，Kiro 风控上可能比我们自己的阿里云 IP 差，先小权重试。
- 订阅链接 = 凭据（在刘国现本机），别贴群里。

## 出事怎么撤

Admin 里把对应槽「停用」⇒ 绑在它上面的人下一个请求自动换到别的槽，在途的跑完。不用重启任何东西。
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default="default")
    ap.add_argument("--ports", required=True, help="e.g. 18891-18895 or 17891,17892")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--no-country", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    want = parse_ports(a.ports)
    rows = {r["port"]: r for r in measure(a.account, a.samples)}
    missing = [p for p in want if p not in rows]
    if missing:
        sys.exit(f"STOP: ports {missing} are not ports of instance {a.account!r}: have {sorted(rows)}")
    picked = [rows[p] for p in want]
    for r in picked:
        print(f"{'OK  ' if r.get('kiro_ms') else 'STOP'} {r['port']} {r['exit_ip']} {r.get('country')} kiro={r.get('kiro_ms')}ms ({r['kiro_ok']})",
              file=sys.stderr)
    ips = [r["exit_ip"] for r in picked]
    if len(set(ips)) != len(ips):
        sys.exit(f"STOP: two ports share an exit IP {ips}; one slot = one IP")
    doc = render(a.account, picked, not a.no_country, a.samples)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print(f"written {a.out}", file=sys.stderr)
    else:
        sys.stdout.write(doc)


if __name__ == "__main__":
    main()
