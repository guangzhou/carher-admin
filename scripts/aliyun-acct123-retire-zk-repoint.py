#!/usr/bin/env python3
"""
aliyun-acct123-retire-zk-repoint.py — 阿里云 ACK(ns carher)一次性收敛：

  1. acct-123 退役：2026-08-02 实跑 OAuth 判定 ACCOUNT_DEACTIVATED（提交本次新码后
     仍停在 auth.openai.com/email-verification），codex 侧 401 token_invalidated。
     → 从 prod + canary 两个 litellm CM 里摘掉它的全部 model_list 条目。
  2. 僵尸 zerokey entry 清理：DB 里 56 条 zk-aliyun-{70..78}-* 指向 hostPort
     8170-8178，那批 serve pod 早已不存在（18/18 探针 connection refused）。
     → 删除。
  3. 5.6 三组重指：把 chatgpt-gpt-5.6-{sol,terra,luna} 补上活着的 zerokey
     (8222/8224/8225/8226 = acct 122/124/125/126，实测四个模型名都能出正文)。
     gpt-5.6-* 不再单独注册，由 CM 的 model_group_alias 兜。
  4. zerokey-pool-aliyun-123 删除：acct-123 网页侧吃的是停用前抓的快照 token，
     capture 已永久失败，留着就是定时炸弹。

**只动阿里云**，不碰 188/198/225/226 上的任何东西。

必须在集群节点上跑（kubectl + 集群凭证在节点上；本地 jms proxy 隧道不可用，
见 memory feedback_jms_laoyang_must_use_tty）：

    python3 /tmp/aliyun-acct123-retire-zk-repoint.py --phase all            # dry-run
    python3 /tmp/aliyun-acct123-retire-zk-repoint.py --phase all --apply

分阶段：--phase db|cm|k8s|rollout|verify|all
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

NS = "carher"
DEAD_ACCT = 123
DEAD_SVC = f"chatgpt-acct-{DEAD_ACCT}.carher.svc"
DEAD_ZK_PORTS = range(8170, 8180)          # acct 70-78 那批,serve pod 已不存在
DEAD_ZK_MODEL_ID = f"zerokey-pool-aliyun-{DEAD_ACCT}"

# 活着的 zerokey serve：acct -> (节点内网 IP, hostPort)。刻意排除 123。
LIVE_ZK = {
    122: ("172.16.0.86", 8222),
    124: ("172.16.0.86", 8224),
    125: ("172.16.16.122", 8225),
    126: ("172.16.0.86", 8226),
}
VARIANTS = ("sol", "terra", "luna")

# 与现网 zk entry 一致(取自 zk-aliyun-70-chatgpt-gpt-5.6-sol 模板)
ZK_RPM = 30
ZK_IN_COST = 5e-06
ZK_OUT_COST = 3e-05


def sh(args: list[str], check: bool = True) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"FATAL: {' '.join(args[:4])}... rc={p.returncode}\n{p.stderr[:800]}")
    return p.stdout


def kubectl(*args: str, check: bool = True) -> str:
    return sh(["kubectl", "-n", NS, *args], check=check)


def proxy_base() -> str:
    ip = kubectl("get", "svc", "litellm-proxy", "-o", "jsonpath={.spec.clusterIP}").strip()
    return f"http://{ip}:4000"


def master_key() -> str:
    b64 = kubectl("get", "secret", "litellm-secrets",
                  "-o", "jsonpath={.data.LITELLM_MASTER_KEY}").strip()
    import base64
    return base64.b64decode(b64).decode()


def api(method: str, path: str, key: str, payload: dict | None = None,
        full: bool = False) -> tuple[int, str]:
    """full=True 时不截断正文 —— /v1/model/info 有 1.2MB,截断会直接毁掉 JSON。"""
    url = proxy_base() + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    cut = (lambda s: s) if full else (lambda s: s[:400])
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, cut(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:400]


def fetch_model_info(key: str) -> list[dict]:
    code, body = api("GET", "/v1/model/info", key, full=True)
    if code != 200:
        sys.exit(f"FATAL: /v1/model/info -> {code} {body[:400]}")
    return json.loads(body).get("data", [])


def zk_port(entry: dict) -> int | None:
    ab = str((entry.get("litellm_params") or {}).get("api_base") or "")
    m = re.search(r":(\d{4})/v1", ab)
    return int(m.group(1)) if m else None


# ───────────────────────────── phase: db ─────────────────────────────
def phase_db(apply: bool) -> None:
    key = master_key()
    data = fetch_model_info(key)

    dead_ids: dict[str, str] = {}
    for e in data:
        p = zk_port(e)
        mid = (e.get("model_info") or {}).get("id")
        if p in DEAD_ZK_PORTS and mid:
            dead_ids[mid] = str((e.get("litellm_params") or {}).get("api_base"))
    if any((e.get("model_info") or {}).get("id") == DEAD_ZK_MODEL_ID for e in data):
        dead_ids[DEAD_ZK_MODEL_ID] = f"acct-{DEAD_ACCT} 网页侧(账号已停用)"

    live_ids = {(e.get("model_info") or {}).get("id")
                for e in data if (zk_port(e) or 0) in {p for _, p in LIVE_ZK.values()}}
    _ = live_ids  # 仅用于 dry-run 时人工核对现网已有哪些活 zk id

    adds = []
    for v in VARIANTS:
        for n, (host, port) in sorted(LIVE_ZK.items()):
            mid = f"zerokey-pool-aliyun-{n}-chatgpt-gpt-5.6-{v}"
            adds.append({
                "model_name": f"chatgpt-gpt-5.6-{v}",
                "litellm_params": {
                    "model": f"openai/gpt-5.6-{v}",
                    "api_base": f"http://{host}:{port}/v1",
                    # api_key 必须给：litellm 的 openai client 没有 key 会直接抛
                    # AuthenticationError（zerokey serve 本身不校验，值随意但不能空）。
                    # 全仓 zerokey 注册脚本统一用 "raw"。
                    "api_key": "raw",
                    "rpm": ZK_RPM,
                    "input_cost_per_token": ZK_IN_COST,
                    "output_cost_per_token": ZK_OUT_COST,
                },
                # mode=chat 强制走 /chat/completions；否则新版 litellm 对 gpt-5 系列默认
                # /responses，而 zerokey serve 不支持 /v1/responses（404）。
                "model_info": {"id": mid, "mode": "chat"},
            })

    print(f"[db] 待删除 {len(dead_ids)} 条 · 待新增 {len(adds)} 条")
    for mid, why in sorted(dead_ids.items()):
        print(f"  DEL {mid:<44} {why}")
    for a in adds:
        print(f"  ADD {a['model_info']['id']:<44} {a['model_name']} -> {a['litellm_params']['api_base']}")
    if not apply:
        print("[db] dry-run，未改动。加 --apply 生效。")
        return

    ok = fail = 0
    for mid in sorted(dead_ids):
        code, body = api("POST", "/model/delete", key, {"id": mid})
        if code == 200:
            ok += 1
        else:
            fail += 1
            print(f"  !! DEL {mid} -> {code} {body}")
    for a in adds:
        # 先 delete 再 new：/model/new 对已存在的 id 不会覆盖，重跑本脚本
        # （比如补 api_key/mode 这种修正）必须先删掉旧的那条。
        api("POST", "/model/delete", key, {"id": a["model_info"]["id"]})
        code, body = api("POST", "/model/new", key, a)
        if code == 200:
            ok += 1
        else:
            fail += 1
            print(f"  !! ADD {a['model_info']['id']} -> {code} {body}")
    print(f"[db] ok={ok} fail={fail}")
    if fail:
        sys.exit("[db] 有失败项，停在这里，别往下走（见 memory feedback_patch_fail_2_stop_verify）")


# ───────────────────────────── phase: cm ─────────────────────────────
def phase_cm(apply: bool) -> None:
    import yaml
    for cm in ("litellm-config", "litellm-config-canary"):
        raw = kubectl("get", "cm", cm, "-o", 'go-template={{index .data "config.yaml"}}')
        doc = yaml.safe_load(raw)
        ml = doc.get("model_list") or []
        keep, drop = [], []
        for e in ml:
            ab = str((e.get("litellm_params") or {}).get("api_base") or "")
            (drop if DEAD_SVC in ab else keep).append(e)
        groups = sorted({e.get("model_name") for e in drop})
        print(f"[cm] {cm}: model_list {len(ml)} -> {len(keep)}，摘掉 {len(drop)} 条，涉及组 {groups}")
        if not drop or not apply:
            continue
        doc["model_list"] = keep
        new = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=10**6)
        patch = json.dumps({"data": {"config.yaml": new}})
        path = f"/tmp/{cm}-patch.json"
        with open(path, "w") as f:
            f.write(patch)
        kubectl("patch", "cm", cm, "--type", "merge", "--patch-file", path)
        # 独立回查：不信任 patch 的返回，重新拉一次数
        back = yaml.safe_load(kubectl("get", "cm", cm, "-o",
                                      'go-template={{index .data "config.yaml"}}'))
        left = sum(1 for e in (back.get("model_list") or [])
                   if DEAD_SVC in str((e.get("litellm_params") or {}).get("api_base") or ""))
        print(f"[cm] {cm}: 回查残留 acct-{DEAD_ACCT} 条目 = {left}")
        if left:
            sys.exit(f"[cm] {cm} patch 后仍有残留，停")
    if not apply:
        print("[cm] dry-run，未改动。")


# ──────────────────────────── phase: k8s ─────────────────────────────
def phase_k8s(apply: bool) -> None:
    acts = [
        ("scale", ["scale", "deploy", f"chatgpt-acct-{DEAD_ACCT}", "--replicas=0"]),
        ("scale", ["scale", "deploy", f"zerokey-serve-{DEAD_ACCT}", "--replicas=0"]),
        ("suspend", ["patch", "cronjob", f"zerokey-capture-{DEAD_ACCT}", "--type", "merge",
                     "-p", '{"spec":{"suspend":true}}']),
    ]
    for label, args in acts:
        print(f"[k8s] {label}: kubectl -n {NS} {' '.join(args)}")
        if apply:
            kubectl(*args)
    if not apply:
        print("[k8s] dry-run，未改动。")
        return
    print("[k8s] 回查:")
    print(kubectl("get", "deploy", f"chatgpt-acct-{DEAD_ACCT}", f"zerokey-serve-{DEAD_ACCT}",
                  "--no-headers", "-o",
                  "custom-columns=N:.metadata.name,R:.spec.replicas"))
    print(kubectl("get", "cronjob", f"zerokey-capture-{DEAD_ACCT}", "--no-headers", "-o",
                  "custom-columns=N:.metadata.name,SUSPEND:.spec.suspend"))


# ────────────────────────── phase: rollout ───────────────────────────
def phase_rollout(apply: bool) -> None:
    for d in ("litellm-proxy", "litellm-proxy-canary"):
        print(f"[rollout] kubectl -n {NS} rollout restart deploy/{d}")
        if apply:
            kubectl("rollout", "restart", f"deploy/{d}")
            print(kubectl("rollout", "status", f"deploy/{d}", "--timeout=300s"))
    if not apply:
        print("[rollout] dry-run，未改动。")


# ─────────────────────────── phase: verify ───────────────────────────
def phase_verify(_apply: bool) -> None:
    key = master_key()
    data = fetch_model_info(key)
    from collections import defaultdict
    stat = defaultdict(lambda: {"live_zk": 0, "dead_zk": 0, "acct": 0, "dead_acct": 0})
    for e in data:
        g = e.get("model_name")
        if g not in {f"chatgpt-gpt-5.6-{v}" for v in VARIANTS} | {
                "chatgpt-gpt-5.5", "gpt-5.5", "image-2"} | {f"gpt-5.6-{v}" for v in VARIANTS}:
            continue
        ab = str((e.get("litellm_params") or {}).get("api_base") or "")
        p = zk_port(e)
        if p in DEAD_ZK_PORTS:
            stat[g]["dead_zk"] += 1
        elif p in {pp for _, pp in LIVE_ZK.values()} or p == 8223:
            stat[g]["live_zk"] += 1
        elif DEAD_SVC in ab:
            stat[g]["dead_acct"] += 1
        elif "chatgpt-acct-" in ab:
            stat[g]["acct"] += 1
    for g in sorted(stat):
        s = stat[g]
        flag = "  <<< 仍有僵尸" if (s["dead_zk"] or s["dead_acct"]) else ""
        print(f"  {g:<26} acct={s['acct']} live_zk={s['live_zk']} "
              f"dead_zk={s['dead_zk']} dead_acct={s['dead_acct']}{flag}")


PHASES = {"db": phase_db, "cm": phase_cm, "k8s": phase_k8s,
          "rollout": phase_rollout, "verify": phase_verify}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all",
                    choices=[*PHASES, "all"])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    order = ["db", "cm", "k8s", "rollout", "verify"] if a.phase == "all" else [a.phase]
    for ph in order:
        print(f"\n=== phase {ph} (apply={a.apply}) ===")
        PHASES[ph](a.apply)


if __name__ == "__main__":
    main()
