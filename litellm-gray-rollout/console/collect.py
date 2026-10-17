"""只读采集：从磁盘和集群读出一份 Readings。不写任何东西、不改路由。

读的东西和 run/step.sh、run/obs.sh 完全一样（同一份 state.env、monitor-loop.log、nginx gray 日志、
kubectl 日志），这样驾驶台看到的数字和人在终端看到的是同一个数。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
from pathlib import Path

from engine import Readings

GRAY_ROOT = Path(os.environ.get("GRAY_ROOT", "/etc/nginx/gray-route"))
ACTIVE = GRAY_ROOT / "active"
EVIDENCE = GRAY_ROOT / "evidence"
NGINX_LOG = Path(os.environ.get("GRAY_NGINX_LOG", "/var/log/nginx/cc-auto-link.gray.log"))
NS = "litellm-product"
KUBECONFIG = os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
NOISY_SIDS = tuple(s for s in os.environ.get("GRAY_NOISY_SIDS", "750def84402f,aa820a9360ad").split(",") if s)


def run_dir() -> Path | None:
    p = Path(os.environ.get("GRAY_RUN_POINTER", "/root/l3/.gray-run-latest"))
    try:
        return Path(p.read_text().strip())
    except OSError:
        return None


def _kubectl(*args: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, "-n", NS, *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def read_state() -> dict:
    try:
        target = ACTIVE.resolve(strict=True)
    except (OSError, RuntimeError):
        return {}
    out = {"_dir": str(target)}
    try:
        for line in (target / "state.env").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    except OSError:
        return {}
    for m in ("force-gray", "force-prod", "protected-prod", "key-sid"):
        try:
            out[f"n_{m}"] = sum(1 for line in (target / f"{m}.map").read_text().splitlines()
                                if line.strip() and not line.lstrip().startswith("#"))
        except OSError:
            out[f"n_{m}"] = None
    return out


def _generation_started(gen: str) -> dt.datetime | None:
    m = re.match(r"^g(\d{8}T\d{6}Z)-", gen or "")
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)


def split_since(state: dict) -> dt.datetime | None:
    """当前 split 是从哪个 generation 开始的：从 generations/ 里倒着找第一个 split 不同的。"""
    gen_dir = GRAY_ROOT / "generations"
    run, split = state.get("run_id"), state.get("split")
    rows = []
    for d in gen_dir.iterdir() if gen_dir.is_dir() else []:
        st = d / "state.env"
        try:
            kv = dict(line.split("=", 1) for line in st.read_text().splitlines() if "=" in line)
        except OSError:
            continue
        if kv.get("run_id") != run:
            continue
        t = _generation_started(d.name)
        if t:
            rows.append((t, kv.get("split")))
    rows.sort()
    since = None
    for t, s in rows:
        if s == split:
            since = since or t
        else:
            since = None
    return since


def monitor(rd: Path | None, since: dt.datetime | None) -> dict:
    out = {"ok": None, "bad": None, "alive": None, "last_line": None}
    if not rd:
        return out
    log = rd / "monitor" / "monitor-loop.log"
    pid = rd / "monitor" / "monitor-loop.pid"
    try:
        p = int(pid.read_text().strip())
        out["alive"] = os.path.exists(f"/proc/{p}") and "gray-monitor-loop" in Path(f"/proc/{p}/cmdline").read_text(errors="ignore")
    except Exception:
        out["alive"] = False
    try:
        lines = log.read_text(errors="ignore").splitlines()[-3000:]
    except OSError:
        return out
    ws = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else ""
    win = [ln for ln in lines if ln[:20] >= ws]
    out["ok"] = sum(1 for ln in win if "cycle rc=0" in ln)
    out["bad"] = sum(1 for ln in win if re.search(r"cycle rc=[1-9]|FAIL", ln))
    cyc = [ln for ln in lines if "cycle rc=" in ln or "FAIL" in ln]
    out["last_line"] = cyc[-1] if cyc else None
    return out


def nginx_pools(since: dt.datetime | None, noisy=NOISY_SIDS) -> dict:
    """新旧两池请求量与 5xx（排除 ws 车道、other 类、巡检号/杭州凭据）。日志时间是 +08:00。"""
    res = {"canary": [0, 0], "stable": [0, 0]}
    if not since or not NGINX_LOG.exists():
        return res
    cut = since.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        size = NGINX_LOG.stat().st_size
        with NGINX_LOG.open("rb") as fh:
            fh.seek(max(0, size - 80_000_000))
            data = fh.read().decode(errors="ignore").splitlines()[1:]
    except OSError:
        return res
    for ln in data:
        if not ln.startswith("ts=") or ln[3:22] < cut or "upstream=127.0.0.1:30403" in ln:
            continue
        m = re.search(r"\bpool=(\w+)", ln); u = re.search(r"\buri_class=(\S+)", ln); s = re.search(r"\bsid=(\S+)", ln)
        if not m or not u or u.group(1) == "other" or m.group(1) not in res:
            continue
        if s and s.group(1)[:12] in noisy:
            continue
        parts = ln.split()
        res[m.group(1)][0] += 1
        if len(parts) > 2 and parts[2].startswith("5"):
            res[m.group(1)][1] += 1
    return res


def gray_pods(since: dt.datetime | None) -> dict:
    out = {"killed": None, "restarts": None}
    raw = _kubectl("get", "po", "-l", "app=litellm-proxy-gray", "-o", "json")
    if not raw:
        return out
    items = [i for i in json.loads(raw)["items"] if not i["metadata"].get("deletionTimestamp")]
    out["restarts"] = sorted(sum(c.get("restartCount", 0) for c in i["status"].get("containerStatuses", [])) for i in items)
    if since:
        st = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        k = 0
        for i in items:
            log = _kubectl("logs", i["metadata"]["name"], "-c", "litellm", f"--since-time={st}", timeout=60)
            k += len(re.findall(r"Child process \[\d+\] died", log))
        out["killed"] = k
    return out


def guarded_old() -> dict:
    raw = _kubectl("get", "deploy", "litellm-proxy-guarded-old", "-o", "json")
    out = {"ready": None, "health": None}
    if raw:
        d = json.loads(raw)
        out["ready"] = f"{d['status'].get('readyReplicas', 0)}/{d['spec'].get('replicas', 0)}"
    try:
        import urllib.request
        out["health"] = urllib.request.urlopen("http://127.0.0.1:30406/health/liveliness", timeout=8).status
    except Exception as e:
        out["health"] = getattr(e, "code", 0)
    return out


def capacity(state: dict) -> dict:
    f = EVIDENCE / "split_capacity.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return {"status": None, "errors": [], "bound": None}
    bound = d.get("run_id") == state.get("run_id") and d.get("generation") == state.get("generation")
    return {"status": d.get("status") if bound else None, "errors": d.get("errors") or [], "bound": bound,
            "util": d.get("utilisation"), "max_split": d.get("max_supportable_split")}


def last_metrics(state: dict) -> dict:
    files = sorted(glob.glob(str(EVIDENCE / "metrics-*.json")), key=os.path.getmtime)[-1:]
    if not files:
        return {}
    try:
        d = json.loads(Path(files[0]).read_text())
    except Exception:
        return {}
    rec = d.get("dispatcher_recommendation") or {}
    return {"action": rec.get("action"), "codes": rec.get("reason_codes") or [], "status": d.get("status"),
            "captured_at": d.get("captured_at"), "alerts": d.get("alerts") or [], "file": os.path.basename(files[0]),
            "user_facing": d.get("user_facing_failures")}


def collect() -> tuple[Readings, dict]:
    """返回 (判定用读数, 页面展示用原始数据)。"""
    st = read_state()
    rd = run_dir()
    raw: dict = {"state": st, "run_dir": str(rd) if rd else None}
    if not st:
        return Readings(phase=None), raw
    since = split_since(st)
    mon = monitor(rd, since)
    pools = nginx_pools(since) if st.get("phase") == "normal_gray" else {"canary": [0, 0], "stable": [0, 0]}
    pods = gray_pods(since) if st.get("phase") == "normal_gray" else {"killed": None, "restarts": None}
    go = guarded_old()
    cap = capacity(st)
    met = last_metrics(st)
    prev = raw_restarts_baseline(rd, st, pods.get("restarts"))
    minutes = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 60 if since else None
    r = Readings(
        phase=st.get("phase"), split=int(st["split"]) if st.get("split", "").isdigit() else None,
        run_id=st.get("run_id"), generation=st.get("generation"), minutes_at_split=minutes,
        ok_cycles=mon["ok"], bad_cycles=mon["bad"], monitor_alive=mon["alive"],
        gray_requests=pools["canary"][0], gray_5xx=pools["canary"][1],
        stable_requests=pools["stable"][0], stable_5xx=pools["stable"][1],
        worker_killed=pods["killed"], restarts_changed=(prev is not None and pods.get("restarts") is not None and prev != pods["restarts"]),
        capacity_status=cap["status"], capacity_errors=cap["errors"], capacity_bound_to_run=cap["bound"],
        guarded_old_ready=go["ready"], guarded_old_health=go["health"],
        last_metrics_reason_codes=met.get("codes", []), last_metrics_action=met.get("action"),
        noisy_keys_protected=(st.get("n_protected-prod") or 0) >= 2 if st.get("phase") == "normal_gray" else None,
    )
    raw.update({"since": since.isoformat() if since else None, "monitor": mon, "pools": pools, "pods": pods,
                "guarded_old": go, "capacity": cap, "metrics": met})
    return r, raw


def raw_restarts_baseline(rd: Path | None, st: dict, now: list | None):
    """每个 generation 第一次看到的重启数存一份，之后比较。只写在 run 目录的 console 子目录里。"""
    if rd is None or now is None:
        return None
    f = rd / "console" / f"restarts-{st.get('generation')}.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        try:
            f.parent.mkdir(mode=0o700, exist_ok=True)
            f.write_text(json.dumps(now))
        except OSError:
            pass
        return now
