#!/usr/bin/env python3
"""copilot2api_acct_quota_to_lark.py — co-acct-N 状态 + 消耗 → 飞书 Base
tblMoMWcC0yBGnHi。参考 chatgpt_acct_quota_to_lark.py 的字段/流程结构；
数据源不同（copilot2api 无 rebalance 引擎、无 state.json），直接实时探。

Sources
-------
- **225 systemd**（`cltx@10.68.13.225`）：per acct `co-acct-N.service` 状态 +
  loopback 端口 + `/v1/messages claude-fable-5.1` 探针（status_code + error.code）。
  ⚠ 探针 max_tokens 给 32，避免 opus-5 假红那类坑（reasoning 模型另说）。
- **198 k8s**（`jms ssh AIYJY-litellm`）：`kubectl -n copilot2api` 拿每号 Deployment
  的 replicas/ready，labels.acct=co-acct-N 做 acct↔deploy 映射。
- **LiteLLM DB**（`litellm-db-0` psql）：`LiteLLM_ProxyModelTable` 拿每号 svc host
  下的 model_id 集合；`LiteLLM_SpendLogs` 按 model_id 聚合最近 24h/7d spend+calls。

判据纪律
--------
- verdict 用真流量证伪，不看 systemd active 就算 OK：账号 quota_exceeded 时
  unit 照样 active、端口照样 listening、/v1/models 照样 200。
- 表列复用 chatgpt-acct schema（`site` 分区），copilot2api 无意义的列（codex_*、
  zk_*、spark%、7d%、device_code、exp_gap、reset_cards、pod_cred）恒空 —— 空白
  ≠ 0，判读时看 site 前缀。
- **sub 相关列不代表订阅续订**：Copilot Individual Max 月池每月 1 号 00:00 UTC
  归零重置（非订阅到期），所以 sub_until 填当月 1 号 00:00Z，sub_src='reset'（新
  枚举值区分自 chatgpt-acct 的 'live'/'state'），will_renew='auto'（自动重置）。
- `main_n/main$/main_n7/main$7` 按 svc host 归属（arm_id 集合来自 DB `ProxyModelTable`）;
  只统计 db_model=true 的 arms（CM 腿已在 09-03 清完，理论上无残留）。

Layout
------
  discover(): ssh 225 → enumerate co-acct-N + ports
  probe_225(): per acct → systemd + listener + /v1/messages fable-5.1
  probe_198(): jms → kubectl deploy state (label acct=co-acct-N)
  arm_map():   jms → psql → svc host 前缀 → model_id 集合
  spendlog():  jms → psql → SUM/COUNT by model_id 集合，24h + 7d
  assemble():  组行
  write():     lark-cli ensure_fields → ensure_options → delete-all → batch-create → verify
"""

from __future__ import annotations
import argparse, json, os, subprocess, sys, time, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================================
# 常量
# ============================================================================

DEFAULT_BASE_TOKEN = "MpHjbtRNfazi2PsTr4sc8iFCnBg"
DEFAULT_TABLE_ID = "tblMoMWcC0yBGnHi"

SITE = "copilot2api-198"   # 与 chatgpt-acct 的 "198"/"aliyun" 区分

# 每号 mail.com 邮箱（唯一非探测型元数据）；新加号在这里追加。
# 拿不到时留 "-"，不阻塞。
KNOWN_EMAILS = {
    "co-acct-1": "franco809@mail.com",
    "co-acct-2": "cmkman@mail.com",
    "co-acct-3": "uxaylntbvsxfp@mail.com",
    "co-acct-4": "kaleigh.jacobs@mail.com",
    # co-acct-5 是按 GitHub 用户名交付的，没拿到配套邮箱；登记用户名占位（报表只用于人眼辨号）
    "co-acct-5": "zamorarcarolinaf767 (GitHub user)",
}

SSH_225 = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
           "cltx@10.68.13.225"]
JMS_SSH_198 = [os.path.expanduser("~/codes/carher-admin/scripts/jms"),
               "ssh", "AIYJY-litellm"]

# 表格列顺序 = 写入顺序。复用 chatgpt-acct schema（43 列），未映射列在 assemble 里恒 None。
COLUMNS = [
    "site", "acct", "email", "take", "status",
    "verdict", "ready", "refresh_err",
    "device_code", "exp_gap",
    "upstream_tier", "live_probe", "state_tier",
    "probe_age", "auth_sync", "pod_cred",
    "7d%", "spark%", "reset",
    "main_n", "main$", "main_n7", "main$7",
    "codex_n", "codex$", "codex_n7", "codex$7",
    "zk_n", "zk$", "zk_n7", "zk$7",
    "zk_lat_avg", "zk_lat_p95", "zk_empty%",
    "next_reset", "restore",
    "sub_until", "sub_left",
    "sub_src", "will_renew",
    "reset_cards", "cause", "snapshot_at",
]

# ============================================================================
# 通用工具
# ============================================================================

def _run(cmd: list[str], *, input_data: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_data, capture_output=True, text=True, timeout=timeout)

def log(*a):
    print("[c2a-quota]", *a, file=sys.stderr, flush=True)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def next_monthly_reset(t: datetime) -> datetime:
    """GitHub Copilot Individual Max 月池每月 1 号 00:00 UTC 归零。"""
    if t.month == 12:
        return datetime(t.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(t.year, t.month + 1, 1, tzinfo=timezone.utc)

def days_left(target: datetime, ref: datetime) -> int:
    return max(0, (target - ref).days)

# ============================================================================
# 225: discover + probe
# ============================================================================

DISCOVER_SH = r"""
set -e
# enumerate active/loaded co-acct-N.service (排除 -auth 临时 unit)
systemctl list-units --type=service --all --no-legend --plain 2>/dev/null \
  | awk '$1 ~ /^co-acct-[0-9]+\.service$/ && $1 !~ /-auth\./ {print $1}' \
  | sort -V
"""

PROBE_SH = r"""
set -e
python3 - "$@" <<'PY'
import json, os, re, subprocess, sys, urllib.request, urllib.error, time
accts = sys.argv[1].split(",")
out = []
for acct in accts:
    unit = acct + ".service"
    r = subprocess.run(["systemctl", "show", unit,
                        "-p", "ActiveState", "-p", "SubState",
                        "-p", "MainPID", "-p", "Environment"],
                       capture_output=True, text=True)
    env = {}
    active = "unknown"; substate = ""
    for line in r.stdout.splitlines():
        if line.startswith("ActiveState="): active = line.split("=",1)[1]
        elif line.startswith("SubState="): substate = line.split("=",1)[1]
        elif line.startswith("Environment="):
            for kv in line.split("=",1)[1].split():
                if "=" in kv:
                    k, v = kv.split("=", 1); env[k] = v
    port = env.get("COPILOT2API_PORT") or ""
    tok_dir = env.get("COPILOT2API_TOKEN_DIR") or ""
    listening = False
    if port:
        p = subprocess.run(["ss","-ltn","sport = :"+port], capture_output=True, text=True)
        listening = ":"+port in p.stdout
    probe = {"http": None, "err_code": None, "err_msg": None, "latency_ms": None}
    if active == "active" and listening and port:
        body = json.dumps({"model":"claude-fable-5.1","max_tokens":32,
                           "messages":[{"role":"user","content":"ping"}]}).encode()
        t0 = time.time()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages",
                                          data=body,
                                          headers={"content-type":"application/json"})
            with urllib.request.urlopen(req, timeout=25) as r_:
                _ = r_.read()
                probe["http"] = r_.status
                probe["latency_ms"] = int((time.time()-t0)*1000)
        except urllib.error.HTTPError as e:
            probe["http"] = e.code
            probe["latency_ms"] = int((time.time()-t0)*1000)
            try:
                body_j = json.loads(e.read().decode())
                probe["err_code"] = str(body_j.get("error",{}).get("code") or "")
                probe["err_msg"] = str(body_j.get("error",{}).get("message") or "")[:200]
            except Exception:
                pass
        except Exception as e:
            probe["err_msg"] = f"{type(e).__name__}: {str(e)[:120]}"
    cred_path = os.path.join(tok_dir, "credentials.json") if tok_dir else ""
    cred_stat = {}
    if cred_path and os.path.exists(cred_path):
        try:
            st = os.stat(cred_path)
            cred_stat = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except Exception: pass
    out.append({
        "acct": acct, "port": port, "active": active, "substate": substate,
        "listening": listening, "token_dir": tok_dir, "cred": cred_stat,
        "probe": probe,
    })
print(json.dumps({"accts": out, "sampled_at": int(time.time())}))
PY
"""

def discover_accts_225() -> list[str]:
    r = _run(SSH_225 + [DISCOVER_SH])
    if r.returncode != 0:
        log("discover FAILED:", r.stderr.strip()); sys.exit(2)
    accts = [ln.replace(".service","") for ln in r.stdout.strip().splitlines() if ln.strip()]
    log(f"discovered on 225: {accts}")
    return accts

def probe_225(accts: list[str]) -> dict:
    r = _run(SSH_225 + ["bash", "-s", "--", ",".join(accts)],
             input_data=PROBE_SH, timeout=90)
    if r.returncode != 0:
        log("probe 225 FAILED:", (r.stderr or "")[-500:]); sys.exit(3)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        log("probe 225 parse fail:", e, "raw:", r.stdout[-500:]); sys.exit(3)

# ============================================================================
# 198: k8s deploy state + LiteLLM DB (arm map + spendlogs)
# ============================================================================

K8S_DEPLOY_SH = r"""
sudo kubectl -n copilot2api get deploy,svc -o json 2>/dev/null
"""

def probe_198_k8s() -> dict:
    r = _run(JMS_SSH_198 + [K8S_DEPLOY_SH], timeout=45)
    if r.returncode != 0:
        log("k8s deploy list FAILED:", (r.stderr or "")[-500:]); return {}
    d = json.loads(r.stdout)
    out, svc_port = {}, {}
    for item in d.get("items", []):
        kind = item.get("kind") or ""
        name = (item.get("metadata") or {}).get("name") or ""
        if kind == "Service":
            ports = (item.get("spec") or {}).get("ports") or []
            if ports:
                svc_port[name] = ports[0].get("port")
            continue
        labels = (item.get("metadata") or {}).get("labels") or {}
        acct = labels.get("acct")
        if not acct:
            continue
        spec_replicas = item.get("spec", {}).get("replicas", 0)
        status = item.get("status", {})
        ready = status.get("readyReplicas", 0) or 0
        out[acct] = {
            "deploy": name, "spec_replicas": spec_replicas,
            "ready_replicas": ready,
            "svc_host": f"{name}.copilot2api.svc.cluster.local",
        }
    for info in out.values():
        info["svc_port"] = svc_port.get(info["deploy"], 7777)
    return out

# 只跑在 198 集群里的号（225 上 systemd unit 被有意停掉/禁用，例如 co-acct-4：
# 同账号两个 listener 会抢 copilot token）不能靠 127.0.0.1 探针判活，
# 否则唯一的活号会整行缺席、报表把池报成全灭。改成在 litellm-proxy pod 里
# 直连 svc 打真流量 /v1/messages。
SVC_PROBE_PY = r"""
import base64, json, os, time, urllib.request, urllib.error
targets = json.loads(base64.b64decode(os.environ["TARGETS"]).decode())
res = {}
for acct, (host, port) in targets.items():
    p = {"http": None, "err_code": None, "err_msg": None, "latency_ms": None}
    body = json.dumps({"model":"claude-fable-5.1","max_tokens":32,
                       "messages":[{"role":"user","content":"ping"}]}).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request("http://%s:%s/v1/messages" % (host, port),
                                     data=body,
                                     headers={"content-type":"application/json"})
        with urllib.request.urlopen(req, timeout=25) as r_:
            r_.read()
            p["http"] = r_.status
    except urllib.error.HTTPError as e:
        p["http"] = e.code
        try:
            bj = json.loads(e.read().decode())
            p["err_code"] = str(bj.get("error",{}).get("code") or "")
            p["err_msg"] = str(bj.get("error",{}).get("message") or "")[:200]
        except Exception:
            pass
    except Exception as e:
        p["err_msg"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    p["latency_ms"] = int((time.time()-t0)*1000)
    res[acct] = p
print("SVCPROBE:" + base64.b64encode(json.dumps(res).encode()).decode())
"""

def probe_k8s_svc(targets: dict) -> dict:
    """targets: {acct: (svc_host, port)} → {acct: probe dict}. 在 litellm-proxy pod 内跑。"""
    if not targets:
        return {}
    import base64
    tb64 = base64.b64encode(json.dumps(targets).encode()).decode()
    sb64 = base64.b64encode(SVC_PROBE_PY.encode()).decode()
    sh = (
        'POD=$(sudo kubectl -n litellm-product get pod -l app=litellm-proxy '
        '-o jsonpath="{.items[0].metadata.name}"); '
        f'sudo kubectl -n litellm-product exec "$POD" -- '
        f'env TARGETS="{tb64}" SCRIPT="{sb64}" '
        'python3 -c "import base64,os; exec(base64.b64decode(os.environ[\\"SCRIPT\\"]).decode())"'
    )
    r = _run(JMS_SSH_198 + [sh], timeout=120)
    if r.returncode != 0:
        log("k8s svc probe FAILED:", (r.stderr or "")[-500:]); return {}
    for line in r.stdout.splitlines():
        if line.startswith("SVCPROBE:"):
            return json.loads(base64.b64decode(line[len("SVCPROBE:"):]).decode())
    log("k8s svc probe: no SVCPROBE: line; stdout tail:", r.stdout[-400:])
    return {}

ARM_MAP_PY = r"""
import json, os, urllib.request
MK=os.environ["MK"]
d=json.load(urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:4000/v1/model/info",
    headers={"Authorization":"Bearer "+MK})))["data"]
out=[]
for m in d:
    if not m.get("model_info",{}).get("db_model"): continue
    ab = str(m.get("litellm_params",{}).get("api_base") or "")
    if "copilot2api" not in ab: continue
    out.append({"model_id": m["model_info"].get("id"),
                "model_name": m["model_name"],
                "api_base": ab})
import base64
print("ARMMAP:" + base64.b64encode(json.dumps(out).encode()).decode())
"""

def fetch_arm_map() -> dict:
    """Return {svc_host_prefix: [(model_id, model_name), ...]} via /v1/model/info
    inside litellm-proxy pod (api_base is encrypted in DB — never LIKE-search)."""
    import base64
    b64 = base64.b64encode(ARM_MAP_PY.encode()).decode()
    sh = (
        'POD=$(sudo kubectl -n litellm-product get pod -l app=litellm-proxy '
        '-o jsonpath="{.items[0].metadata.name}"); '
        'MK=$(sudo kubectl -n litellm-product get secret litellm-secrets '
        '-o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d); '
        # ship the python source via SCRIPT env var (b64), decode inside pod
        f'sudo kubectl -n litellm-product exec "$POD" -- '
        f'env MK="$MK" SCRIPT="{b64}" '
        'python3 -c "import base64,os,sys; exec(base64.b64decode(os.environ[\\"SCRIPT\\"]).decode())"'
    )
    r = _run(JMS_SSH_198 + [sh], timeout=45)
    if r.returncode != 0:
        log("arm map FAILED:", (r.stderr or "")[-500:]); return {}
    arms_json = None
    for line in r.stdout.splitlines():
        if line.startswith("ARMMAP:"):
            arms_json = base64.b64decode(line[len("ARMMAP:"):]).decode(); break
    if not arms_json:
        log("arm map: no ARMMAP: line; stdout tail:", r.stdout[-400:]); return {}
    arms = json.loads(arms_json)
    out: dict[str, list] = {}
    for a in arms:
        m = re.search(r"//([a-z0-9\-]+)\.copilot2api\.svc\.cluster\.local[:/]", a["api_base"])
        if not m: continue
        out.setdefault(m.group(1), []).append((a["model_id"], a["model_name"]))
    return out

def fetch_spend(arm_ids: list[str], window: str) -> dict:
    """Return {model_id: (n_calls, spend_usd)} for arm_ids within window.
    走 base64 传 SQL 文件到 db pod（多层 SSH 里手拼 IN 列表反复被 shell 吃掉单引号）。"""
    if not arm_ids:
        return {}
    import base64
    id_lit = ",".join("'" + i.replace("'", "''") + "'" for i in arm_ids)
    sql = (
        f'SELECT model_id, COUNT(*), COALESCE(SUM(spend),0) '
        f'FROM "LiteLLM_SpendLogs" '
        f'WHERE model_id IN ({id_lit}) '
        f"AND \"startTime\" > NOW() - INTERVAL '{window}' "
        f'GROUP BY model_id;'
    )
    b64 = base64.b64encode(sql.encode()).decode()
    sh = (
        f'echo {b64} | base64 -d > /tmp/c2a_spend.sql && '
        f'sudo kubectl -n litellm-product cp /tmp/c2a_spend.sql '
        f'litellm-db-0:/tmp/c2a_spend.sql && '
        f'sudo kubectl -n litellm-product exec litellm-db-0 -- '
        f'sh -c "psql -U \\$POSTGRES_USER \\$POSTGRES_DB -A -F\\"|\\" -t -f /tmp/c2a_spend.sql"'
    )
    r = _run(JMS_SSH_198 + [sh], timeout=60)
    if r.returncode != 0:
        log(f"spend fetch ({window}) FAILED:", (r.stderr or "")[-500:]); return {}
    out = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3: continue
        mid, n, sp = parts[0], parts[1], parts[2]
        try:
            out[mid] = (int(n), float(sp))
        except ValueError:
            continue
    return out

# ============================================================================
# assemble rows
# ============================================================================

def acct_to_host(acct: str) -> str:
    """co-acct-1 → copilot2api, co-acct-N → copilot2api-N (N>=2)."""
    n = acct.rsplit("-", 1)[-1]
    return "copilot2api" if n == "1" else f"copilot2api-{n}"

def assemble_rows(probe: dict, k8s: dict, arms: dict,
                  spend24: dict, spend7d: dict) -> list[list]:
    now = now_utc()
    reset_dt = next_monthly_reset(now)
    reset_iso = reset_dt.strftime("%Y-%m-%d %H:%MZ")
    sub_left = f"{days_left(reset_dt, now)}d"

    rows = []
    for a in probe["accts"]:
        acct = a["acct"]
        port = a["port"]
        active = a["active"]
        listening = a["listening"]
        probe_info = a["probe"] or {}
        http = probe_info.get("http")
        err_code = probe_info.get("err_code") or ""
        err_msg = probe_info.get("err_msg") or ""

        host = acct_to_host(acct)
        k8s_info = k8s.get(acct) or {}
        arm_list = arms.get(host, [])
        arm_ids = [aid for aid, _ in arm_list]

        # verdict
        probe_src = a.get("probe_src") or "225-local"
        cause_bits = []
        if probe_src == "k8s-svc":
            cause_bits.append("probe=k8s-svc")
        if active != "active":
            verdict = "OFFLINE"
            cause_bits.append("no-ready-pod" if probe_src == "k8s-svc"
                              else f"systemd={active}/{a['substate']}")
        elif not listening:
            verdict = "OFFLINE"; cause_bits.append("no-listener")
        elif http == 200:
            verdict = "OK"
        elif http == 402 and err_code == "quota_exceeded":
            verdict = "QUOTA_EXCEEDED"; cause_bits.append("upstream quota_exceeded")
        elif http in (401, 403):
            verdict = "TOKEN_INVALID"; cause_bits.append(f"probe http={http} code={err_code}")
        elif http is None:
            verdict = "PROBE_ERR"; cause_bits.append(err_msg or "probe timeout")
        else:
            verdict = f"HTTP_{http}"; cause_bits.append(f"code={err_code} msg={err_msg[:80]}")

        # live_probe (与 chatgpt-acct 的语义一致：live / off / no-pod / 4xx:kind / ERR:*)
        if http == 200:
            live_probe = "live"
        elif http == 402 and err_code:
            live_probe = f"402:{err_code}"
        elif http in (401, 403):
            live_probe = f"{http}:{err_code or 'unknown'}"
        elif active != "active" or not listening:
            live_probe = "off"
        elif http is None:
            live_probe = "ERR:probe"
        else:
            live_probe = f"{http}"

        # upstream_tier
        if verdict == "OK":
            upstream_tier = "HEALTHY"
        elif verdict == "QUOTA_EXCEEDED":
            upstream_tier = "QUOTA_EXCEEDED"
        elif k8s_info.get("spec_replicas") == 0 and active != "active":
            upstream_tier = "SCALED_DOWN"
        else:
            upstream_tier = verdict

        # take
        take = "yes" if arm_ids else "-"

        # ready column (k8s pod)
        ready_str = f"{k8s_info.get('ready_replicas', 0)}/{k8s_info.get('spec_replicas', 0)}" if k8s_info else "-/-"

        # spend aggregations
        n24 = sum(spend24.get(aid, (0, 0.0))[0] for aid in arm_ids)
        s24 = sum(spend24.get(aid, (0, 0.0))[1] for aid in arm_ids)
        n7  = sum(spend7d.get(aid, (0, 0.0))[0] for aid in arm_ids)
        s7  = sum(spend7d.get(aid, (0, 0.0))[1] for aid in arm_ids)

        row = {
            "site": SITE,
            "acct": acct,
            "email": KNOWN_EMAILS.get(acct, "-"),
            "take": take,
            "status": "ONLINE" if verdict == "OK" else ("OFFLINE" if verdict == "OFFLINE" else "PAUSED"),
            "verdict": verdict,
            "ready": ready_str,
            "refresh_err": None,   # 未收集
            "device_code": None,
            "exp_gap": None,
            "upstream_tier": upstream_tier,
            "live_probe": live_probe,
            "state_tier": "no-engine",
            "probe_age": "live",
            "auth_sync": "no-local",
            "pod_cred": None,
            "7d%": None,
            "spark%": None,
            "reset": reset_iso,
            "main_n": n24 or None,
            "main$": round(s24, 6) if s24 else None,
            "main_n7": n7 or None,
            "main$7": round(s7, 6) if s7 else None,
            # codex_/zk_ 在 copilot2api 无对应流量，恒空
            "codex_n": None, "codex$": None, "codex_n7": None, "codex$7": None,
            "zk_n": None, "zk$": None, "zk_n7": None, "zk$7": None,
            "zk_lat_avg": None, "zk_lat_p95": None, "zk_empty%": None,
            "next_reset": reset_iso,
            "restore": "-",
            "sub_until": reset_iso,     # 每月 1 号池子重置（不是订阅到期）
            "sub_left": sub_left,
            "sub_src": "reset",         # 新枚举值：区分自 chatgpt-acct 的 live/state
            "will_renew": "auto",       # 月池自动重置，无需人工续订
            "reset_cards": None,
            "cause": "; ".join(cause_bits) if cause_bits else None,
            "snapshot_at": now.strftime("%Y-%m-%d %H:%M:%SZ"),
        }
        rows.append([row.get(col) for col in COLUMNS])
    return rows

# ============================================================================
# Lark 写入（复用 chatgpt_acct_quota_to_lark.py 里的形状）
# ============================================================================

LARK_ENV = {"LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}

def lark_cli(args: list[str], *, input_data: str | None = None, timeout: int = 30) -> dict:
    env = {**os.environ, **LARK_ENV}
    r = subprocess.run(["lark-cli"] + args, input=input_data,
                       capture_output=True, text=True, timeout=timeout, env=env)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": r.stderr or r.stdout or f"rc={r.returncode}"}}

LARK_HUES = ["Red", "Orange", "Yellow", "Lime", "Green", "Turquoise",
             "Wathet", "Blue", "Carmine", "Purple", "Gray"]

def ensure_select_options(base_token: str, table_id: str, rows: list[list]) -> None:
    """把本次要写的值补进 select 列 options，避免删表后写入被拒清空整表
    （2026-08-05 事故同款）。"""
    resp = lark_cli(["base", "+field-list", "--base-token", base_token,
                     "--table-id", table_id, "--as", "user"])
    fields = ((resp.get("data") or {}).get("fields") or [])
    if not fields:
        log("字段列表读不到，跳过 select 预检 —— 后续删表如失败会清空"); return
    by_name = {f.get("name"): f for f in fields}
    idx = {name: i for i, name in enumerate(COLUMNS)}
    for fname, field in by_name.items():
        if "options" not in field or fname not in idx:
            continue
        values = {str(r[idx[fname]]) for r in rows if r[idx[fname]] is not None}
        have = {o.get("name") for o in (field.get("options") or [])}
        missing = sorted(v for v in values if v and v not in have)
        if not missing:
            continue
        opts = list(field.get("options") or [])
        base = len(opts)
        for i, name in enumerate(missing):
            opts.append({"name": name, "hue": LARK_HUES[(base + i) % len(LARK_HUES)],
                         "lightness": "Lighter"})
        payload = {"name": fname, "type": "select",
                   "multiple": bool(field.get("multiple")), "options": opts}
        r = lark_cli(["base", "+field-update", "--base-token", base_token,
                      "--table-id", table_id, "--field-id", fname,
                      "--json", json.dumps(payload, ensure_ascii=False),
                      "--yes", "--as", "user"])
        if r.get("ok"):
            log(f"{fname} += {missing}")
        else:
            log(f"{fname} 补 select 失败 —— 中止（不动表）：",
                (r.get("error") or {}).get("message"))
            sys.exit(6)

def delete_all_records(base_token: str, table_id: str) -> int:
    deleted = 0
    while True:
        resp = lark_cli(["base", "+record-list", "--base-token", base_token,
                         "--table-id", table_id, "--limit", "200",
                         "--as", "user", "--format", "json"])
        d = resp.get("data", {})
        ids = d.get("record_id_list", [])
        if not ids: break
        tmp = Path("_c2a_del_batch.json")
        tmp.write_text(json.dumps({"record_id_list": ids}))
        try:
            dr = lark_cli(["base", "+record-delete", "--base-token", base_token,
                           "--table-id", table_id, "--as", "user",
                           "--json", f"@{tmp.name}", "--yes", "--format", "json"])
            if not dr.get("ok"):
                log("delete ERR:", (dr.get("error") or {}).get("message")); break
            deleted += len(ids)
            log(f"deleted {deleted} so far")
        finally:
            tmp.unlink(missing_ok=True)
    return deleted

def batch_create_records(base_token: str, table_id: str, rows: list[list]) -> int:
    created = 0
    for i in range(0, len(rows), 200):
        batch = rows[i:i+200]
        tmp = Path("_c2a_create_batch.json")
        tmp.write_text(json.dumps({"fields": COLUMNS, "rows": batch}, ensure_ascii=False),
                        encoding="utf-8")
        try:
            resp = lark_cli(["base", "+record-batch-create", "--base-token", base_token,
                             "--table-id", table_id, "--as", "user",
                             "--json", f"@{tmp.name}", "--format", "json"], timeout=60)
            if not resp.get("ok"):
                log("create ERR:", (resp.get("error") or {}).get("message")); break
            rid_list = resp.get("data", {}).get("record_id_list", [])
            created += len(rid_list)
            log(f"batch {i//200+1}: {len(rid_list)} created")
        finally:
            tmp.unlink(missing_ok=True)
    return created

def verify_records(base_token: str, table_id: str, rows: list[list]) -> bool:
    """行主键 = (site, acct)。逐格比对，不只数条数。"""
    idx = {name: i for i, name in enumerate(COLUMNS)}
    want = {(r[idx["site"]], r[idx["acct"]]): r for r in rows}
    got: list[list] = []; header: list[str] = []
    page = None
    while True:
        cmd = ["base", "+record-list", "--base-token", base_token,
               "--table-id", table_id, "--limit", "200",
               "--as", "user", "--format", "json"]
        if page: cmd.extend(["--page-token", page])
        resp = lark_cli(cmd)
        d = resp.get("data", {})
        header = d.get("fields") or header
        got.extend(d.get("data") or [])
        if not d.get("has_more"): break
        page = d.get("page_token", "")
        if not page: break
    if len(got) != len(rows):
        log(f"verify MISMATCH: got={len(got)} expect={len(rows)}"); return False
    hp = {n: i for i, n in enumerate(header)}
    diffs = 0
    for gr in got:
        k = (gr[hp["site"]], gr[hp["acct"]])
        exp = want.get(k)
        if exp is None: diffs += 1; continue
        for col, si in idx.items():
            if col not in hp: continue
            e, g = exp[si], gr[hp[col]]
            if isinstance(g, list): g = g[0] if g else None
            if g in ("", []): g = None
            if isinstance(e, (int, float)) and isinstance(g, (int, float)):
                if abs(float(e) - float(g)) > 1e-6: diffs += 1
            elif (e if e not in ("", "-") else e) != g:
                # 允许 '-'/None/'' 互认
                if not (e in (None, "", "-") and g in (None, "", "-")):
                    diffs += 1
    if diffs:
        log(f"verify: {diffs} cell diffs"); return False
    log(f"verify OK: {len(got)} rows × {len(idx)} cols")
    return True

# ============================================================================
# main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="co-acct quota → Lark Base")
    ap.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    ap.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    ap.add_argument("--dry-run", action="store_true",
                    help="只取数打印，不动表")
    ap.add_argument("--skip-delete", action="store_true",
                    help="不清表直接追加（一般用于二次调试）")
    args = ap.parse_args()

    t0 = time.time()
    accts = discover_accts_225()

    log("fetching k8s deploy state on 198...")
    k8s = probe_198_k8s()
    log(f"k8s deploys with acct= label: {sorted(k8s.keys())}")

    if not accts and not k8s:
        log("no co-acct anywhere (225 systemd + 198 k8s) — abort"); return 1

    probe = {"accts": [], "sampled_at": int(time.time())}
    if accts:
        log(f"probing {len(accts)} accts on 225 (systemd+listener+/v1/messages)...")
        probe = probe_225(accts)
        log(f"probe OK in {time.time()-t0:.1f}s")

    # 池成员 = 225 systemd ∪ 198 k8s（acct= label）。只在集群里跑的号必须走 svc 探针，
    # 否则它整行缺席 —— 09-04 c4 就是这样让报表把全池报成 QUOTA_EXCEEDED。
    k8s_only = [a for a in k8s if a not in accts]
    if k8s_only:
        log(f"k8s-only accts (no active unit on 225): {sorted(k8s_only)} — probing via svc")
        targets = {a: (k8s[a]["svc_host"], k8s[a].get("svc_port", 7777))
                   for a in k8s_only if (k8s[a].get("ready_replicas") or 0) > 0}
        svc_res = probe_k8s_svc(targets)
        for a in sorted(k8s_only):
            pr = svc_res.get(a) or {"http": None, "err_code": None,
                                    "err_msg": None if a in targets else "no ready pod",
                                    "latency_ms": None}
            probe["accts"].append({
                "acct": a, "port": k8s[a].get("svc_port", 7777),
                "active": "active" if a in targets else "inactive",
                "substate": "k8s-pod",
                "listening": a in targets,
                "token_dir": "", "cred": {},
                "probe": pr, "probe_src": "k8s-svc",
            })

    log("fetching arm map from LiteLLM DB...")
    arms = fetch_arm_map()
    log("arm hosts:", {h: len(v) for h, v in arms.items()})
    all_arm_ids = [mid for lst in arms.values() for (mid, _) in lst]

    log("fetching spendlogs 24h + 7d...")
    spend24 = fetch_spend(all_arm_ids, "24 hours")
    spend7d = fetch_spend(all_arm_ids, "7 days")
    log(f"spend24: {len(spend24)} arms; spend7d: {len(spend7d)} arms")

    rows = assemble_rows(probe, k8s, arms, spend24, spend7d)
    log(f"assembled {len(rows)} rows in {time.time()-t0:.1f}s total")

    # 打印一份可读的自检摘要（写表前的最后一眼）
    idx = {n: i for i, n in enumerate(COLUMNS)}
    for r in rows:
        print(f"[preview] {r[idx['site']]}/{r[idx['acct']]:12s} "
              f"verdict={r[idx['verdict']]:<15s} "
              f"live={r[idx['live_probe']]:<20s} "
              f"take={r[idx['take']]:<3s} "
              f"ready={r[idx['ready']]:<5s} "
              f"main_n7={r[idx['main_n7']] or 0} "
              f"main$7={r[idx['main$7']] or 0} "
              f"cause={r[idx['cause']] or ''}", file=sys.stderr)

    if args.dry_run:
        log(f"[dry-run] 不写表，preview 已打印"); return 0

    ensure_select_options(args.base_token, args.table_id, rows)

    if not args.skip_delete:
        # 只删本 site 的行，别把可能共存的 chatgpt-acct 行一并清了。
        # 现表 0 行也无所谓，delete_all 是幂等的。
        # ⚠ 如果 co-acct 与 chatgpt-acct 未来真共存本表，改成按 site 过滤的删除。
        deleted = delete_all_records(args.base_token, args.table_id)
        log(f"deleted {deleted} old records")

    created = batch_create_records(args.base_token, args.table_id, rows)
    log(f"created {created} rows")

    if created != len(rows):
        log(f"WARN: created {created} != rows {len(rows)}")
        return 2

    ok = verify_records(args.base_token, args.table_id, rows)
    print(f"\n[DONE] {created} records written")
    print(f"  https://t83dfrspj4.feishu.cn/base/{args.base_token}?table={args.table_id}")
    return 0 if ok else 4

if __name__ == "__main__":
    sys.exit(main())
