#!/usr/bin/env python3
"""
zerokey 端口 ↔ 198 chatgpt-acct 映射表（live 采集，非手维护静态 dict）。

数据源（优先级）：
  1. 188 JSZX-AI-03
     - ~/zerokey-codex-accounts/*/ops.env  → PORT / MAIL_USER / ZK_USER
     - ~/zerokey-codex（主容器 zerokey-codex :8123 kristine）
     - docker ps → 容器名 / health / 端口绑定
     - /Data/chatgpt-auth/acct-N/.creds   → email → acct-N
  2. 198 AIYJY-litellm（可选 --skip-198 跳过）
     - litellm-config CM zerokey-pool api_base 端口集合
     - GET /v1/model/info zerokey-pool live 端口（与 CM 对照）

用法（仓库根目录）：
    python3 scripts/zerokey_acct_port_map.py
    python3 scripts/zerokey_acct_port_map.py --json
    python3 scripts/zerokey_acct_port_map.py --tsv   # 默认

其它脚本可 import：
    from zerokey_acct_port_map import fetch_rows, port_name_map
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
from typing import Any

HOST_188 = "10.68.13.188"

REMOTE_188 = r"""
import glob, json, os, re, subprocess, sys
from pathlib import Path

HOME = Path.home()
ACCOUNTS_ROOT = HOME / "zerokey-codex-accounts"
MAIN_ROOT = HOME / "zerokey-codex"
AUTH_DIR = Path("/Data/chatgpt-auth")


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_email_to_acct() -> dict[str, str]:
    m: dict[str, str] = {}
    for creds in AUTH_DIR.glob("acct-*/.creds"):
        acct = creds.parent.name  # acct-39
        email = None
        for line in creds.read_text().splitlines():
            if line.startswith("email="):
                email = line.split("=", 1)[1].strip().strip("'\"")
                break
        if email:
            m[email.lower()] = acct
    return m


def docker_rows() -> dict[int, dict]:
    # port -> container / health
    try:
        raw = subprocess.check_output(
            ["docker", "ps", "-a", "--filter", "name=zerokey-codex",
             "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            text=True,
        )
    except Exception as e:
        print(json.dumps({"error": f"docker ps: {e}", "rows": []}))
        sys.exit(1)
    by_port: dict[int, dict] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        name, status, ports = parts[0], parts[1], parts[2]
        for m in re.finditer(r"0\.0\.0\.0:(\d+)->", ports):
            port = int(m.group(1))
            if 8123 <= port <= 8199:
                by_port[port] = {
                    "container": name,
                    "docker_status": status,
                    "healthy": "healthy" in status.lower(),
                }
    return by_port


def add_row(rows: list, seen: set, *, port: int, zk_id: str, email: str,
            email_to_acct: dict, docker: dict, creds_hint: str | None):
    if port in seen:
        return
    seen.add(port)
    acct = email_to_acct.get(email.lower(), "") if email else ""
    if not acct and zk_id.startswith("acct") and zk_id[4:].isdigit():
        acct = f"acct-{zk_id[4:]}"
    d = docker.get(port, {})
    rows.append({
        "port": port,
        "zk_id": zk_id,
        "chatgpt_acct": acct,
        "email": email,
        "container": d.get("container", ""),
        "healthy": d.get("healthy", False),
        "docker_status": d.get("docker_status", ""),
        "creds_path": creds_hint or (str(AUTH_DIR / acct / ".creds") if acct else ""),
    })


def collect_188() -> list[dict]:
    email_to_acct = load_email_to_acct()
    docker = docker_rows()
    rows: list[dict] = []
    seen: set[int] = set()

    # Per-account dirs (timothy, elise, acct32, …)
    for ops in sorted(ACCOUNTS_ROOT.glob("*/ops.env")):
        zk_id = ops.parent.name
        env = parse_env_file(ops)
        port_s = env.get("PORT", "")
        email = env.get("MAIL_USER", "")
        if not port_s.isdigit():
            continue
        port = int(port_s)
        creds = AUTH_DIR / email_to_acct.get(email.lower(), "noop") / ".creds"
        creds_hint = str(creds) if creds.is_file() else ""
        if not creds_hint and email:
            for c in AUTH_DIR.glob("acct-*/.creds"):
                txt = c.read_text()
                if f"email={email}" in txt or f"email='{email}'" in txt:
                    creds_hint = str(c)
                    break
        add_row(rows, seen, port=port, zk_id=zk_id, email=email,
                 email_to_acct=email_to_acct, docker=docker, creds_hint=creds_hint)

    # Main kristine :8123 (~/zerokey-codex, container zerokey-codex)
    main_env = parse_env_file(MAIN_ROOT / "ops.env")
    main_email = main_env.get("MAIL_USER", "")
    if not main_email:
        creds39 = AUTH_DIR / "acct-39" / ".creds"
        if creds39.is_file():
            for line in creds39.read_text().splitlines():
                if line.startswith("email="):
                    main_email = line.split("=", 1)[1].strip().strip("'\"")
                    break
    main_port = 8123
    if main_env.get("PORT", "").isdigit():
        main_port = int(main_env["PORT"])
    elif 8123 in docker:
        main_port = 8123
    add_row(rows, seen, port=main_port, zk_id="kristine", email=main_email,
             email_to_acct=email_to_acct, docker=docker,
             creds_hint=str(AUTH_DIR / "acct-39" / ".creds"))

    rows.sort(key=lambda r: r["port"])
    return rows


print(json.dumps(collect_188(), ensure_ascii=False))
"""

REMOTE_198 = r"""
import json, subprocess, sys, urllib.request, yaml, base64

NS = "litellm-product"
CM = "litellm-config"
HOST = "10.68.13.188"


def sh(cmd):
    return subprocess.check_output(cmd).decode()


def pool_ports_from_cm() -> set[int]:
    cm = json.loads(sh(["kubectl", "get", "cm", "-n", NS, CM, "-o", "json"]))
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    ports = set()
    for m in cfg.get("model_list", []):
        if m.get("model_name") != "zerokey-pool":
            continue
        base = m.get("litellm_params", {}).get("api_base", "")
        if f"{HOST}:" in base:
            try:
                ports.add(int(base.split(":")[-1].split("/")[0]))
            except ValueError:
                pass
    return ports


def pool_ports_live() -> set[int]:
    MK = base64.b64decode(
        sh(["kubectl", "get", "secret", "litellm-secrets", "-n", NS,
            "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    ).decode().strip()
    req = urllib.request.Request(
        "http://localhost:30402/v1/model/info",
        headers={"Authorization": f"Bearer {MK}"},
    )
    rows = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
    ports = set()
    for m in rows:
        if m.get("model_name") != "zerokey-pool":
            continue
        base = m.get("litellm_params", {}).get("api_base", "")
        if f"{HOST}:" in base:
            try:
                ports.add(int(base.split(":")[-1].split("/")[0]))
            except ValueError:
                pass
    return ports


try:
    cm = sorted(pool_ports_from_cm())
    live = sorted(pool_ports_live())
    print(json.dumps({"cm_ports": cm, "live_ports": live}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"error": str(e), "cm_ports": [], "live_ports": []}))
    sys.exit(1)
"""

# Static fallback when JMS unreachable (last known prod mapping)
FALLBACK_ROWS: list[dict[str, Any]] = [
    {"port": 8123, "zk_id": "kristine", "chatgpt_acct": "acct-39", "email": "kristine_free517@mail.com"},
    {"port": 8124, "zk_id": "timothy", "chatgpt_acct": "acct-36", "email": "timothy_mossey871@mail.com"},
    {"port": 8125, "zk_id": "zyq", "chatgpt_acct": "acct-48", "email": "zyqvjxkylmphi@mail.com"},
    {"port": 8126, "zk_id": "owp", "chatgpt_acct": "acct-45", "email": "owpfpxdxevtcu@mail.com"},
    {"port": 8127, "zk_id": "hgg", "chatgpt_acct": "acct-46", "email": "hggaqkprgxjaz@mail.com"},
    {"port": 8128, "zk_id": "dvo", "chatgpt_acct": "acct-47", "email": "dvoirljlnotkx@mail.com"},
    {"port": 8129, "zk_id": "elise", "chatgpt_acct": "acct-40", "email": "elise_chicalace981@mail.com"},
    {"port": 8130, "zk_id": "herbert", "chatgpt_acct": "acct-41", "email": "herbert_praesentiumxs@mail.com"},
    {"port": 8131, "zk_id": "olga", "chatgpt_acct": "acct-42", "email": "olga_recusandaepiv@mail.com"},
    {"port": 8132, "zk_id": "tania", "chatgpt_acct": "acct-43", "email": "tania_accusamuspu@mail.com"},
    {"port": 8133, "zk_id": "iheyv", "chatgpt_acct": "acct-44", "email": "iheyvlrwfyiki@mail.com"},
    {"port": 8134, "zk_id": "acct37", "chatgpt_acct": "acct-37", "email": "elicia_grad835@mail.com"},
    {"port": 8135, "zk_id": "acct32", "chatgpt_acct": "acct-32", "email": "6220977@qq.com"},
    {"port": 8136, "zk_id": "acct34", "chatgpt_acct": "acct-34", "email": "3007630355@qq.com"},
]


def jms(*args: str) -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(here, "jms"), *args]


def _run_remote_b64(host: str, script: str) -> Any:
    b64 = base64.b64encode(script.encode()).decode()
    remote = f"python3 -c \"import base64;exec(base64.b64decode('{b64}').decode())\""
    out = subprocess.check_output(jms("ssh", host, remote), text=True)
    return json.loads(out.strip())


def fetch_rows(*, skip_198: bool = False, use_fallback: bool = False) -> list[dict[str, Any]]:
    if use_fallback:
        rows = [dict(r) for r in FALLBACK_ROWS]
    else:
        try:
            rows = _run_remote_b64("JSZX-AI-03", REMOTE_188)
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
            sys.stderr.write(f"WARN: 188 collect failed ({e}); using static fallback\n")
            rows = [dict(r) for r in FALLBACK_ROWS]

    pool_cm: set[int] = set()
    pool_live: set[int] = set()
    if not skip_198:
        try:
            p198 = _run_remote_b64("AIYJY-litellm", REMOTE_198)
            pool_cm = set(p198.get("cm_ports") or [])
            pool_live = set(p198.get("live_ports") or [])
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
            sys.stderr.write(f"WARN: 198 pool ports unavailable ({e})\n")

    for r in rows:
        port = int(r["port"])
        r["api_base"] = f"http://{HOST_188}:{port}/v1"
        r["in_cm_pool"] = port in pool_cm if pool_cm else None
        r["in_live_pool"] = port in pool_live if pool_live else None
        if r.get("healthy") is None:
            r["healthy"] = False
    return rows


def port_name_map(rows: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """port(str) -> zk_id，供 zerokey-account-usage 等脚本使用。"""
    rows = rows or fetch_rows()
    return {str(r["port"]): str(r["zk_id"]) for r in rows}


def acct_by_port(rows: list[dict[str, Any]] | None = None) -> dict[int, str]:
    rows = rows or fetch_rows()
    return {int(r["port"]): str(r.get("chatgpt_acct") or "") for r in rows}


def render_tsv(rows: list[dict[str, Any]]) -> str:
    hdr = ("port", "zk_id", "chatgpt_acct", "email", "container", "healthy",
           "in_cm_pool", "in_live_pool", "api_base")
    lines = ["\t".join(hdr)]
    for r in rows:
        lines.append("\t".join(
            str(r.get(k, "")) if r.get(k) is not None else "-"
            for k in hdr
        ))
    cm_n = sum(1 for r in rows if r.get("in_cm_pool"))
    live_n = sum(1 for r in rows if r.get("in_live_pool"))
    lines.append("")
    lines.append(f"# rows={len(rows)}  in_cm_pool={cm_n or '?'}  in_live_pool={live_n or '?'}")
    return "\n".join(lines)


def render_table(rows: list[dict[str, Any]]) -> str:
    cols = [
        ("port", 5), ("zk_id", 10), ("chatgpt_acct", 12), ("email", 34),
        ("pool", 5), ("ok", 3),
    ]
    L = ["=== zerokey 端口 ↔ 198 chatgpt-acct（188 live + 198 zerokey-pool）===",
         "pool=live/router  ok=容器 healthy",
         ""]
    hdr = " ".join(f"{n:>{w}}" if w > 5 else f"{n:>{w}}" for n, w in cols)
    L.append(hdr)
    L.append("-" * len(hdr))
    for r in rows:
        pool = "Y" if r.get("in_live_pool") else ("cm" if r.get("in_cm_pool") else "-")
        ok = "Y" if r.get("healthy") else "N"
        L.append(
            f"{r['port']:>5} {r['zk_id']:>10} {r.get('chatgpt_acct') or '?':>12} "
            f"{(r.get('email') or '-'):34} {pool:>5} {ok:>3}"
        )
    unmapped = [r for r in rows if not r.get("chatgpt_acct")]
    drift_cm = [r for r in rows if r.get("in_cm_pool") and not r.get("in_live_pool")]
    drift_live = [r for r in rows if r.get("in_live_pool") and not r.get("in_cm_pool")]
    L.append("")
    L.append(f"共 {len(rows)} 个 zerokey 端口；"
             f"CM pool {sum(1 for r in rows if r.get('in_cm_pool')) or '?'}/"
             f"live {sum(1 for r in rows if r.get('in_live_pool')) or '?'}")
    if unmapped:
        L.append(f"WARN 未解析 chatgpt_acct: {[r['port'] for r in unmapped]}")
    if drift_cm:
        L.append(f"WARN CM 有但 live 无: {[r['port'] for r in drift_cm]}")
    if drift_live:
        L.append(f"WARN live 有但 188 无容器: {[r['port'] for r in drift_live]}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="zerokey port ↔ 198 chatgpt-acct mapping")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tsv", action="store_true")
    ap.add_argument("--skip-198", action="store_true", help="only collect from 188")
    ap.add_argument("--fallback", action="store_true", help="skip JMS; use static fallback")
    args = ap.parse_args()

    rows = fetch_rows(skip_198=args.skip_198, use_fallback=args.fallback)
    if args.json:
        print(json.dumps({"host_188": HOST_188, "accounts": rows}, ensure_ascii=False, indent=2))
    elif args.tsv:
        print(render_tsv(rows))
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
