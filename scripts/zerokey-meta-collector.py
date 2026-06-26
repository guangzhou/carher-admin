#!/usr/bin/env python3
"""zerokey-meta-collector — 188 host 端 cron，写 /home/cltx/.acct-admin/zerokey-meta.json.

acct-admin backend 容器没装 docker / kubectl / curl，只能读 bind-mount。
这里负责把 zerokey-pool 的 (container × users.json × 198 router entry) 三源
拼成一份扁平 meta，由 cron 每 2min 刷一次，backend 直接 cat /data/zerokey-meta.json。

输出 schema:
{
  "gathered_at": 1782486000.0,
  "rows": [
    {
      "name": "elise",                 # docker container suffix（即 host dir 名）
      "port": 8129,                    # 188 host port
      "container": "zerokey-codex-elise",
      "status": "Up 4 hours (healthy)",
      "healthy": true,
      "email": "elise_chicalace981@mail.com",
      "plan": "pro",
      "sub_until_epoch": 1784966884,
      "jwt_exp_epoch": 1783016972,
      "router_acct_id": "acct-40",     # 198 router /model/info 里这一条 entry 的 model_info.id
      "router_present": true,          # 是否在 198 LiteLLM router 里
      "deploy_loc": "188"
    }
  ],
  "errors": []
}
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ACCT_DIR = "/home/cltx/zerokey-codex-accounts"
OUT_PATH = os.environ.get("ZEROKEY_META_OUT", "/home/cltx/zerokey-meta/zerokey-meta.json")
LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
LITELLM_KEY = os.environ.get(
    "LITELLM_MASTER_KEY", "sk-pro-litellm-ce077e2b0721bb419a633e4d"
)


def docker_ps_zerokey() -> list[dict]:
    """docker ps 列 zerokey-codex-* 容器，返回 [{name, container, port, status, healthy}]."""
    fmt = "{{.Names}}\t{{.Ports}}\t{{.Status}}"
    out = subprocess.run(
        ["docker", "ps", "--format", fmt, "--filter", "name=zerokey-codex"],
        capture_output=True, text=True, timeout=15,
    )
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        container, ports, status = parts[0], parts[1], parts[2]
        if not container.startswith("zerokey-codex"):
            continue
        name = container.replace("zerokey-codex-", "", 1)
        if name == "zerokey-codex":
            name = "default"
        # 解 host port (取第一段形如 0.0.0.0:8129->8129/tcp)
        port = 0
        for chunk in ports.split(","):
            chunk = chunk.strip()
            if "->" in chunk and ":" in chunk:
                try:
                    port = int(chunk.split(":")[-1].split("->")[0])
                    break
                except ValueError:
                    continue
        rows.append({
            "name": name,
            "container": container,
            "port": port,
            "status": status,
            "healthy": "healthy" in status.lower(),
        })
    return rows


def parse_users_json(path: str) -> dict:
    """读 users.json 里的 chatgpt.<key>.parsedFetch.headers.authorization JWT，
    返回 {email, plan, sub_until_epoch, jwt_exp_epoch}."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    cg = data.get("chatgpt") or {}
    if not isinstance(cg, dict) or not cg:
        return {}
    # 第一个 key 通常就是 username（同 dir name）
    user_obj = next(iter(cg.values()), {})
    auth = (((user_obj.get("parsedFetch") or {}).get("headers") or {})
            .get("authorization") or "")
    if not auth.startswith("Bearer "):
        return {}
    token = auth[len("Bearer "):]
    if token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    auth_meta = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    sub_until_raw = auth_meta.get("chatgpt_subscription_active_until")
    sub_until_epoch = None
    if isinstance(sub_until_raw, (int, float)):
        sub_until_epoch = float(sub_until_raw)
    elif isinstance(sub_until_raw, str):
        try:
            from datetime import datetime
            sub_until_epoch = datetime.fromisoformat(
                sub_until_raw.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            sub_until_epoch = None
    return {
        "email": profile.get("email") or "",
        "plan": auth_meta.get("chatgpt_plan_type") or "",
        "sub_until_epoch": sub_until_epoch,
        "jwt_exp_epoch": claims.get("exp"),
    }


def fetch_router_zerokey() -> dict[int, str]:
    """198 /v1/model/info，filter zerokey-pool entries，返回 {port: model_info.id}."""
    req = urllib.request.Request(
        f"{LITELLM_BASE}/v1/model/info",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return {"_err": f"{type(exc).__name__}: {exc}"}  # type: ignore
    out: dict[int, str] = {}
    for entry in (body.get("data") or []):
        if entry.get("model_name") != "zerokey-pool":
            continue
        lp = entry.get("litellm_params") or {}
        ab = lp.get("api_base") or ""
        # api_base 形如 http://10.68.13.188:81XX/v1
        if "10.68.13.188:" not in ab:
            continue
        try:
            port = int(ab.split(":")[-1].split("/")[0])
        except ValueError:
            continue
        acct_id = (entry.get("model_info") or {}).get("id") or ""
        out[port] = acct_id
    return out


def main() -> int:
    errors: list[str] = []
    try:
        containers = docker_ps_zerokey()
    except Exception as exc:
        print(f"docker ps failed: {exc}", file=sys.stderr)
        return 2
    try:
        router_map = fetch_router_zerokey()
    except Exception as exc:
        errors.append(f"router fetch: {exc}")
        router_map = {}
    if "_err" in router_map:
        errors.append(f"router: {router_map.pop('_err')}")

    rows = []
    for c in containers:
        # default 容器 (zerokey-codex 不带后缀) 挂的是 /home/cltx/zerokey-codex/state/users.json
        if c["name"] == "default":
            users_path = "/home/cltx/zerokey-codex/state/users.json"
        else:
            users_path = os.path.join(ACCT_DIR, c["name"], "state", "users.json")
        meta = parse_users_json(users_path)
        if not meta:
            errors.append(f"{c['name']}: users.json missing or unparseable")
        router_id = router_map.get(c["port"])
        rows.append({
            **c,
            **meta,
            "router_acct_id": router_id,
            "router_present": bool(router_id),
            "deploy_loc": "188",
        })

    # 按 port 排序
    rows.sort(key=lambda r: r.get("port") or 0)

    # router 里有但 container 没的 entry → ghost row（typical: scale=0 残留）
    container_ports = {r["port"] for r in rows if r.get("port")}
    for port, acct_id in router_map.items():
        if port in container_ports:
            continue
        rows.append({
            "name": f"ghost-{acct_id}-{port}",
            "container": None,
            "port": port,
            "status": "router-only (no container)",
            "healthy": False,
            "email": None,
            "plan": None,
            "sub_until_epoch": None,
            "jwt_exp_epoch": None,
            "router_acct_id": acct_id,
            "router_present": True,
            "deploy_loc": "188",
            "ghost": True,
        })
    rows.sort(key=lambda r: r.get("port") or 0)
    payload = {
        "gathered_at": time.time(),
        "rows": rows,
        "errors": errors,
        "container_count": len(rows),
        "router_entry_count": len(router_map),
    }
    tmp = OUT_PATH + ".tmp"
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, OUT_PATH)
    print(f"wrote {OUT_PATH} ({len(rows)} rows, {len(errors)} errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
