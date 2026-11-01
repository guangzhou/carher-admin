"""Exits: what each port goes out through, switching, measuring.

Three ways mihomo lets you change the exit (all measured on 198 2026-10-28):
  1. select   PUT /proxies/<selector> {"name": X}  -> 204, instant, persisted in cache.db
  2. pin      a `listeners:` entry with `proxy: X` -> that port always uses X (needs reload)
  3. auto     url-test groups pick the lowest delay; `PUT` on a url-test *fixes* it until
              that node dies (mihomo urltest.go fast()), `DELETE` un-fixes
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from cli_anything.mihomo.core import config, instance, subscription

SCHOLAR = "https://scholar.google.com/"
SCHOLAR_QUERY = "https://scholar.google.com/scholar?q=attention+is+all+you+need"
IPINFO = "https://ipinfo.io/json"
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


def proxies(name: str) -> dict[str, Any]:
    return instance.api_ok(name, "GET", "/proxies")["proxies"]


def resolve_chain(allp: dict[str, Any], start: str) -> list[str]:
    """Follow `now` from a group down to a leaf node: ["🔰 节点选择", "♻️ 自动选择 - HK", "HK - HKT - 14"]."""
    chain, cur, seen = [start], start, {start}
    while cur in allp and allp[cur].get("type") in GROUP_TYPES and allp[cur].get("now"):
        cur = allp[cur]["now"]
        if cur in seen:
            break
        chain.append(cur)
        seen.add(cur)
    return chain


def groups(name: str) -> list[dict[str, Any]]:
    allp = proxies(name)
    out = []
    for n, p in allp.items():
        if p.get("type") in GROUP_TYPES and n != "GLOBAL":
            out.append({"name": n, "type": p["type"], "now": p.get("now"), "fixed": p.get("fixed") or None,
                        "members": len(p.get("all") or []), "leaf": resolve_chain(allp, n)[-1]})
    return out


def nodes(name: str) -> list[dict[str, Any]]:
    allp = proxies(name)
    out = []
    for n, p in allp.items():
        if p.get("type") in GROUP_TYPES or n in ("DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"):
            continue
        hist = p.get("history") or []
        out.append({"name": n, "type": p.get("type"), "region": subscription.region(n), "udp": p.get("udp"),
                    "alive": p.get("alive"), "last_delay": hist[-1]["delay"] if hist else None})
    return out


def ports(name: str) -> list[dict[str, Any]]:
    """Every port this instance serves and the node it currently exits through."""
    st = config.load_state(name)
    allp = proxies(name)
    rows = [{"port": st["mixed_port"], "kind": "default (rules)", "via": st["default_group"],
             "chain": resolve_chain(allp, st["default_group"])}]
    for pin in st["pins"]:
        rows.append({"port": pin["port"], "kind": "pin", "via": pin["target"], "chain": resolve_chain(allp, pin["target"])})
    for r in rows:
        r["exit_node"] = r["chain"][-1]
    return rows


def select(name: str, target: str, group: str | None = None) -> dict[str, Any]:
    group = group or config.load_state(name)["default_group"]
    allp = proxies(name)
    if group not in allp:
        raise instance.InstanceError(f"no group {group!r}")
    choices = allp[group].get("all") or []
    if target not in choices:
        near = [c for c in choices if target.lower() in c.lower()]
        raise instance.InstanceError(f"{target!r} is not a member of {group!r}" + (f"; did you mean {near[:5]}" if near else ""))
    instance.api_ok(name, "PUT", f"/proxies/{instance.enc(group)}", {"name": target})
    after = proxies(name)
    return {"group": group, "now": after[group].get("now"), "chain": resolve_chain(after, group)}


def unfix(name: str, group: str) -> dict[str, Any]:
    """Let a url-test/fallback group choose by itself again (DELETE /proxies/<group>)."""
    instance.api_ok(name, "DELETE", f"/proxies/{instance.enc(group)}")
    after = proxies(name)
    return {"group": group, "now": after[group].get("now"), "fixed": after[group].get("fixed") or None}


def measure(name: str, targets: list[str], url: str = SCHOLAR, timeout_ms: int = 5000) -> list[dict[str, Any]]:
    """mihomo's own URL test per node/group (GET /proxies/<n>/delay), in parallel on 198."""
    q = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
    jobs = [{"kind": "api", "method": "GET", "path": f"/proxies/{instance.enc(t)}/delay?{q}",
             "timeout": timeout_ms / 1000 + 5} for t in targets]
    res = instance.ctl(name, jobs, workers=16, timeout=60 + len(targets) * timeout_ms / 1000 / 8)
    out = []
    for t, r in zip(targets, res):
        b = r.get("body") if isinstance(r.get("body"), dict) else {}
        out.append({"name": t, "region": subscription.region(t), "delay_ms": b.get("delay") if r["status"] == 200 else None,
                    "error": None if r["status"] == 200 else (b.get("message") or r.get("error") or f"HTTP {r['status']}")})
    return sorted(out, key=lambda x: (x["delay_ms"] is None, x["delay_ms"] or 0))


def _parse_ipinfo(text: str) -> dict[str, Any]:
    import json
    try:
        d = json.loads(text)
    except ValueError:
        return {}
    return {k: d.get(k) for k in ("ip", "country", "city", "org")}


def scholar_hits(html: str) -> int:
    """Result blocks on a Scholar SERP; 0 on the 403/captcha page."""
    return len(re.findall(r'class="gs_ri"', html))


def verify(name: str, port_list: list[int] | None = None, query_url: str = SCHOLAR_QUERY) -> list[dict[str, Any]]:
    """Through each port: real exit IP + a real Scholar search. The only 'it works' that counts."""
    st = config.load_state(name)
    port_list = port_list or [st["mixed_port"], *(p["port"] for p in st["pins"])]
    jobs = []
    for p in port_list:
        jobs.append({"kind": "fetch", "port": p, "url": IPINFO, "timeout": 15})
        jobs.append({"kind": "fetch", "port": p, "url": query_url, "timeout": 25, "max": 400000})
    res = instance.ctl(name, jobs, workers=8, timeout=120)
    out = []
    for i, p in enumerate(port_list):
        ip, sch = res[2 * i], res[2 * i + 1]
        hits = scholar_hits(sch.get("text", "")) if sch["status"] == 200 else 0
        out.append({"port": p, "exit": _parse_ipinfo(ip.get("text", "")) if ip["status"] == 200 else {"error": ip.get("error") or ip["status"]},
                    "scholar_status": sch["status"], "scholar_results": hits, "scholar_ms": sch.get("ms"),
                    "ok": sch["status"] == 200 and hits > 0})
    return out


KIRO = "https://q.us-east-1.amazonaws.com/"  # 404 without a path = reachable


def table(name: str, samples: int = 3) -> list[dict[str, Any]]:
    """Per port: exit node, real exit IP/country/org (ipinfo), median time to the Kiro endpoint.

    This is the table a slot handoff needs. Country comes from the exit IP, never from the node
    name (the feed's "DE - 德国2线" exits in France).
    """
    rows = ports(name)
    jobs = []
    for r in rows:
        jobs.append({"kind": "fetch", "port": r["port"], "url": IPINFO, "timeout": 15})
        jobs += [{"kind": "fetch", "port": r["port"], "url": KIRO, "timeout": 15, "max": 1024} for _ in range(samples)]
    # one port at a time, samples serially: parallel probes through one node skew its latency
    res = instance.ctl(name, jobs, workers=1, timeout=60 + len(jobs) * 16)
    step = 1 + samples
    for i, r in enumerate(rows):
        ip, ks = res[i * step], res[i * step + 1:(i + 1) * step]
        info = _parse_ipinfo(ip.get("text", "")) if ip["status"] == 200 else {}
        ok = sorted(k["ms"] for k in ks if k["status"] and k["status"] < 500)
        r.update({"exit_ip": info.get("ip"), "country": info.get("country"), "city": info.get("city"), "org": info.get("org"),
                  "kiro_ms": ok[len(ok) // 2] if ok else None, "kiro_ok": f"{len(ok)}/{samples}",
                  "kiro_status": sorted({k["status"] for k in ks})})
    return rows


def geo(name: str, ports_nodes: list[tuple[int, str]], tries: int = 3) -> list[dict[str, Any]]:
    """Real exit IP/country of each node, given a port already pinned to it. Serial with retries.

    Built for the "which countries does this subscription really have" question; run it on a
    throwaway instance that pins every node (see `exit geo --all`).
    """
    jobs = []
    for port, _ in ports_nodes:
        jobs += [{"kind": "fetch", "port": port, "url": IPINFO, "timeout": 20} for _ in range(tries)]
    res = instance.ctl(name, jobs, workers=1, timeout=60 + len(jobs) * 21)
    out = []
    for i, (port, node) in enumerate(ports_nodes):
        hit = next((_parse_ipinfo(r["text"]) for r in res[i * tries:(i + 1) * tries] if r["status"] == 200), None)
        errs = [r.get("error") or f"HTTP {r['status']}" for r in res[i * tries:(i + 1) * tries] if r["status"] != 200]
        out.append({"node": node, "port": port, "label_region": subscription.region(node),
                    **(hit or {"ip": None, "country": None, "city": None, "org": None}),
                    "ok": bool(hit), "error": None if hit else (errs[-1] if errs else "no answer")})
    return out
