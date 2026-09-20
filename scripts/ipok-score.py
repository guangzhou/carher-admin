#!/usr/bin/env python3
"""批量把 IP 过 ipok.io，按风险值升序排出最干净的几个。

用法:
    ipok-score.py 1.2.3.4 5.6.7.8 ...
    ipok-score.py -f ips.txt            # 每行一个 IP，# 开头是注释
    ipok-score.py -f ips.txt --json out.json

判据说明（来自 ipok.io 自己的 riskBreakdown）：
    final = max(weightedAvg, 各 floor)
    `hosting` 这个 floor 是 35 —— 所以任何机房/托管段 IP 的分数**不可能低于 35**，
    无论它有没有被举报过。想拿到 10 分以内只能是 residential / mobile 段。
    脚本因此单独打印 ipType 和命中的 floor，别只看总分。

⚠️ 只读脚本：不写任何远端状态，不碰配置，无删除/覆盖行为。
   唯一的落盘是 --json 指定的那个文件（会覆盖，路径由调用方决定）。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://ipok.io/api/ip?ip={}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# ipok 的限流是按"来源 IP"算的，整批 100+ 个 IP 打完必然撞墙。
# 给多个出口代理就能把配额摊开：--proxy-file 每行一个 proxy URL，
# 例如 http://user:pass@127.0.0.1:8118 。凭据只存在那个文件里，脚本不落盘。
_PROXIES = []
_pidx = 0


def _opener():
    """按轮转取下一个代理；没配代理就直连。"""
    global _pidx
    if not _PROXIES:
        return urllib.request.build_opener()
    p = _PROXIES[_pidx % len(_PROXIES)]
    _pidx += 1
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p}))


def fetch(ip, timeout=30, retries=5):
    """取一个 IP 的评分。

    429 单独处理：ipok 的限流窗口是分钟级，原来 2s/4s 的重试等于没退避，
    整批会全军覆没。这里对 429 走 30s/60s/120s… 的长退避，其它错误保持短重试；
    配了代理时每次重试会换到下一个出口。
    """
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API.format(ip), headers={
            "User-Agent": UA,
            "Accept": "application/json",
        })
        try:
            with _opener().open(req, timeout=timeout) as r:
                body = r.read()
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < retries:
                # 有多个出口时先换出口重试，换过一圈再退避
                if _PROXIES and attempt < len(_PROXIES):
                    continue
                back = min(30 * (2 ** attempt), 300)
                print(f"   429 {ip}，退避 {back}s", file=sys.stderr)
                time.sleep(back)
                continue
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{ip}: {last}")


def summarize(ip, d):
    geo = d.get("geo") or {}
    rb = d.get("riskBreakdown") or {}
    floors = rb.get("floors") or []
    return {
        "ip": ip,
        "risk": d.get("risk"),
        "ipType": d.get("ipType"),
        "usageType": d.get("usageType"),
        "weightedAvg": rb.get("weightedAvg"),
        "floors": [f"{f.get('key')}={f.get('floor')}" for f in floors],
        "signals": d.get("signals") or [],
        "country": geo.get("country"),
        "city": geo.get("city"),
        "asn": geo.get("asn"),
        "asName": geo.get("asName"),
        "isp": geo.get("isp"),
        "reverse": geo.get("reverse"),
        "contributors": {c.get("source"): c.get("risk")
                         for c in (rb.get("contributors") or [])},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ips", nargs="*")
    ap.add_argument("-f", "--file", help="每行一个 IP 的文件")
    ap.add_argument("--json", dest="json_out", help="完整结果写到这个文件")
    ap.add_argument("--sleep", type=float, default=1.5, help="每次查询间隔秒")
    ap.add_argument("--proxy-file", help="每行一个 proxy URL，轮流使用以摊开 ipok 的限流配额")
    args = ap.parse_args()

    if args.proxy_file:
        with open(args.proxy_file) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    _PROXIES.append(line)
        print(f"出口代理 {len(_PROXIES)} 个", file=sys.stderr)

    ips = list(args.ips)
    if args.file:
        with open(args.file) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    ips.append(line)
    # 去重保序
    seen, ordered = set(), []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    if not ordered:
        ap.error("没有给任何 IP")

    rows, full, errors = [], {}, []
    for i, ip in enumerate(ordered):
        if i:
            time.sleep(args.sleep)
        try:
            d = fetch(ip)
        except RuntimeError as e:
            errors.append(str(e))
            print(f"!! {e}", file=sys.stderr)
            continue
        full[ip] = d
        rows.append(summarize(ip, d))
        print(f"[{i + 1}/{len(ordered)}] {ip} risk={d.get('risk')} "
              f"{d.get('ipType')}", file=sys.stderr)

    rows.sort(key=lambda r: (r["risk"] if r["risk"] is not None else 999, r["ip"]))

    print(f"{'risk':>5}  {'type':<12} {'floor':<14} {'ip':<16} "
          f"{'asn':<10} {'geo':<22} rdns")
    print("-" * 118)
    for r in rows:
        geo = f"{r['country'] or '?'}/{r['city'] or '?'}"
        print(f"{str(r['risk']):>5}  {str(r['ipType'] or '?'):<12} "
              f"{(','.join(r['floors']) or '-'):<14} {r['ip']:<16} "
              f"{str(r['asn'] or '?'):<10} {geo[:22]:<22} {r['reverse'] or '-'}")

    clean = [r for r in rows if r["risk"] is not None and r["risk"] < 10]
    print()
    if clean:
        print(f"✅ 10 分以内: {len(clean)} 个")
        for r in clean:
            print(f"   {r['ip']}  risk={r['risk']}  {r['ipType']}  "
                  f"{r['isp']}  贡献={r['contributors']}")
    else:
        print("❌ 没有 10 分以内的。注意 ipok 对 hosting 段有 floor=35 的硬下限，")
        print("   机房 IP 无论多干净都到不了 10 分以下——要低分只能换 residential/mobile 段。")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"summary": rows, "full": full, "errors": errors},
                      fh, ensure_ascii=False, indent=2)
        print(f"\n完整结果 -> {args.json_out}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
