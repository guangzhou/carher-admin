"""Subscription: fetch (via 188 SOCKS, 198 itself gets 403 from the feed) and describe.

The subscription URL is a credential. It lives only in ``<state dir>/sub.url`` (0600) and is
never printed; summaries show node names/types/regions, never servers or passwords.
"""
from __future__ import annotations

import collections
import os
import re
import subprocess
import time
from typing import Any

import yaml

BASE_DIR = os.path.expanduser("~/.config/cli-anything-mihomo")  # account "default"; also the binary cache
_STATE_DIR = os.path.expanduser(os.environ.get("MIHOMO_STATE_DIR", BASE_DIR))
FETCH_PROXY = os.environ.get("MIHOMO_FETCH_PROXY", "socks5h://127.0.0.1:18880")
TUNNEL_HOST = os.environ.get("MIHOMO_TUNNEL_HOST", "cltx@10.68.13.188")
USER_AGENT = "clash-verge/v1.3.8"  # the feed returns Clash YAML for Clash UAs
SECRET_KEYS = {"server", "password", "uuid", "username", "private-key", "psk", "token", "ws-headers", "ws-opts", "sni"}


class SubscriptionError(RuntimeError):
    pass


def account_dir(account: str) -> str:
    """One iGuge account = one state dir: default -> ~/.config/cli-anything-mihomo, X -> …-X."""
    if not account.replace("-", "").replace("_", "").isalnum():
        raise SubscriptionError(f"account name {account!r}: letters, digits, - and _ only")
    return BASE_DIR if account == "default" else f"{BASE_DIR}-{account}"


def use_account(account: str | None) -> None:
    """Switch every path below to this account (CLI --account). None keeps MIHOMO_STATE_DIR/default."""
    global _STATE_DIR
    if account:
        _STATE_DIR = account_dir(account)


def state_dir() -> str:
    return _STATE_DIR


def account() -> str:
    """Name of the active account, derived from the state dir (written into instance markers on 198)."""
    d = os.path.normpath(_STATE_DIR)
    if d == os.path.normpath(BASE_DIR):
        return "default"
    prefix = os.path.normpath(BASE_DIR) + "-"
    return d[len(prefix):] if d.startswith(prefix) else os.path.basename(d)


def sub_url_file() -> str:
    return os.path.join(_STATE_DIR, "sub.url")


def sub_yaml() -> str:
    return os.path.join(_STATE_DIR, "sub.yaml")


def sub_headers() -> str:
    return os.path.join(_STATE_DIR, "sub.headers")


def _ensure_state_dir() -> None:
    os.makedirs(_STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(_STATE_DIR, 0o700)


def set_url(url: str) -> None:
    if not re.match(r"^https?://", url):
        raise SubscriptionError("subscription URL must start with http(s)://")
    _ensure_state_dir()
    fd = os.open(sub_url_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(url.strip() + "\n")


def _url() -> str:
    try:
        with open(sub_url_file()) as fh:
            return fh.read().strip()
    except FileNotFoundError as exc:
        raise SubscriptionError(f"no subscription URL; run `cli-anything-mihomo sub set-url` ({sub_url_file()})") from exc


def _proxy_port(proxy: str) -> int | None:
    m = re.match(r"^socks5h?://127\.0\.0\.1:(\d+)$", proxy)
    return int(m.group(1)) if m else None


def _listening(port: int) -> bool:
    import socket
    with socket.socket() as so:
        so.settimeout(2)
        return so.connect_ex(("127.0.0.1", port)) == 0


def ensure_tunnel(proxy: str = FETCH_PROXY) -> dict[str, Any]:
    """Make sure the local SOCKS tunnel to 188 is listening; start it if not.

    No `ClearAllForwardings` here: that option also drops `-D`, so ssh exits 0 while nothing
    listens (10-28). The RemoteForward warning from ~/.ssh/config is harmless for a tunnel.
    """
    port = _proxy_port(proxy)
    if port is None:
        return {"proxy": proxy, "managed": False}
    if _listening(port):
        return {"proxy": proxy, "port": port, "started": False, "listening": True}
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ExitOnForwardFailure=yes",
                        "-fN", "-D", str(port), TUNNEL_HOST], capture_output=True, text=True, timeout=40)
    for _ in range(20):
        if _listening(port):
            return {"proxy": proxy, "port": port, "started": True, "listening": True}
        time.sleep(0.25)
    err = "\n".join(l for l in r.stderr.splitlines() if "port forwarding" not in l)
    raise SubscriptionError(f"SOCKS tunnel 127.0.0.1:{port} -> {TUNNEL_HOST} did not come up (ssh rc={r.returncode}): {err[:300]}")


def fetch(proxy: str | None = FETCH_PROXY, timeout: int = 60) -> dict[str, Any]:
    """Download the Clash YAML. Fails loudly on non-200 / non-YAML / zero proxies."""
    _ensure_state_dir()
    if proxy:
        ensure_tunnel(proxy)
    tmp, htmp = sub_yaml() + ".part", sub_headers() + ".part"  # a failed fetch must not blank the last good copy
    cmd = ["curl", "-sS", "-m", str(timeout), "-A", USER_AGENT, "-D", htmp, "-o", tmp, "-w", "%{http_code}"]
    if proxy:
        cmd += ["-x", proxy]
    old = os.umask(0o077)
    try:
        for attempt in range(3):
            r = subprocess.run(cmd + [_url()], capture_output=True, text=True, timeout=timeout + 10)
            # 97 = proxy closed the connection, 7/28/35/56 = connect/timeout/TLS/recv: the 188 tunnel blips, retry
            if r.returncode not in (7, 28, 35, 56, 97):
                break
            if proxy:
                ensure_tunnel(proxy)
            time.sleep(2 * (attempt + 1))
    finally:
        os.umask(old)
    if r.returncode != 0:
        hint = " (is the 188 tunnel up? `ssh -fN -D 18880 cltx@10.68.13.188`)" if proxy and "18880" in proxy else ""
        raise SubscriptionError(f"curl rc={r.returncode}: {r.stderr.strip()[:300]}{hint}")
    if r.stdout.strip() != "200":
        raise SubscriptionError(f"feed returned HTTP {r.stdout.strip()}")
    data = load(tmp)
    os.replace(tmp, sub_yaml())
    os.replace(htmp, sub_headers())
    return {"account": account(), "fetched_at": int(time.time()), "bytes": os.path.getsize(sub_yaml()), **summary(data)}


def load(path: str | None = None) -> dict[str, Any]:
    path = path or sub_yaml()
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise SubscriptionError(f"{path} missing; run `cli-anything-mihomo sub fetch`") from exc
    except yaml.YAMLError as exc:
        raise SubscriptionError(f"subscription is not YAML: {exc}") from exc
    if not isinstance(data, dict) or not data.get("proxies"):
        raise SubscriptionError("subscription has no proxies (wrong User-Agent, expired, or base64 feed?)")
    return data


def region(name: str) -> str:
    """Leading 2-letter region tag the feed uses ("HK - 香港原生 - 04" -> HK); else "OTHER"."""
    m = re.match(r"^([A-Z]{2})\b", name)
    return m.group(1) if m else "OTHER"


def summary(data: dict[str, Any]) -> dict[str, Any]:
    proxies = data.get("proxies") or []
    names = [p.get("name") for p in proxies]
    dup = [n for n, c in collections.Counter(names).items() if c > 1]
    return {
        "proxies": len(proxies),
        "by_type": dict(collections.Counter(str(p.get("type")) for p in proxies)),
        "by_region": dict(collections.Counter(region(str(n)) for n in names)),
        "groups": [{"name": g.get("name"), "type": g.get("type"), "members": len(g.get("proxies") or [])}
                   for g in data.get("proxy-groups") or []],
        "rules": len(data.get("rules") or []),
        "duplicate_names": dup,
    }


def nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Node list without secrets: name, type, region, port, udp."""
    return [{"name": p.get("name"), "type": p.get("type"), "region": region(str(p.get("name"))),
             "port": p.get("port"), "udp": bool(p.get("udp"))}
            for p in data.get("proxies") or []]


def parse_userinfo(header_value: str) -> dict[str, int]:
    """`upload=0; download=899898; total=214748364800; expire=1823520489` -> dict of ints."""
    out: dict[str, int] = {}
    for part in header_value.split(";"):
        k, _, v = part.strip().partition("=")
        if k and v.strip().isdigit():
            out[k] = int(v.strip())
    return out


def accounts() -> list[str]:
    """Local accounts that have a stored subscription URL."""
    parent, base = os.path.split(BASE_DIR)
    out = []
    for d in sorted(os.listdir(parent)):
        if (d == base or d.startswith(base + "-")) and os.path.exists(os.path.join(parent, d, "sub.url")):
            out.append("default" if d == base else d[len(base) + 1:])
    return out


def quota() -> dict[str, Any]:
    """Traffic/expiry from the last fetch's `subscription-userinfo` header."""
    try:
        with open(sub_headers(), encoding="utf-8", errors="replace") as fh:
            headers = fh.read()
    except FileNotFoundError as exc:
        raise SubscriptionError("no headers yet; run `cli-anything-mihomo sub fetch`") from exc
    m = re.search(r"(?im)^subscription-userinfo:\s*(.+?)\s*$", headers)
    if not m:
        raise SubscriptionError("feed sent no subscription-userinfo header")
    u = parse_userinfo(m.group(1))
    used = u.get("upload", 0) + u.get("download", 0)
    total = u.get("total", 0)
    exp = u.get("expire")
    reset = next_reset(exp)
    return {
        "used_bytes": used, "total_bytes": total,
        "used_gb": round(used / 1024 ** 3, 3), "total_gb": round(total / 1024 ** 3, 1),
        "left_gb": round((total - used) / 1024 ** 3, 2) if total else None,
        "used_pct": round(100 * used / total, 2) if total else None,
        "expire": exp, "expire_date": time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else None,
        "reset_date": reset,
        # feed page: 「当月流量达到 200G，手机端账号会被自动暂停，直到重置日自动恢复」 ⇒ same rule from the header
        "account_status": ("暂停" if total and used >= total else "正常") if total else None,
        "headers_mtime": int(os.path.getmtime(sub_headers())),
    }


def next_reset(expire: int | None, now: float | None = None) -> str | None:
    """iGuge resets the client traffic monthly on the subscription's day-of-month (feed page: 「每月流量重置日 /28日」;
    acct2 expires 2027-10-28 and shows 28). Returns the next reset date; at 200GB the client account pauses until then.
    """
    if not expire:
        return None
    day = time.localtime(expire).tm_mday
    t = time.localtime(now if now is not None else time.time())
    y, mo = t.tm_year, t.tm_mon
    if t.tm_mday >= day:
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    import calendar
    return f"{y:04d}-{mo:02d}-{min(day, calendar.monthrange(y, mo)[1]):02d}"
