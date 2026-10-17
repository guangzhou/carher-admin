"""动作白名单。每个动作 = 调一个已有脚本（和人在终端敲的是同一条命令），驾驶台自己不改任何路由状态。

- 只有这里列出的动作能被页面触发；参数逐个校验。
- 动作执行前抢 $D/.console.lock（非阻塞 flock）。抢不到 = 有别的驾驶者（另一个驾驶台实例或人在终端
  跑 run/ 脚本时手动 `flock $D/.console.lock`），直接拒绝，不排队。
- 现有脚本内部还有自己的 `_lib.sh lock_acquire`，两把锁互不替代。
- 每个动作的 stdout/stderr 落 $D/console/actions/<ts>-<name>.log，事件写 $D/console-events.jsonl。
- 长动作（ramp 内含容量检查、converge 约 15 分钟）后台跑，页面看日志尾巴。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import threading
from pathlib import Path

from collect import run_dir

ALLOWED_RAMP = (1, 5, 10, 50, 100)
_running: dict[str, subprocess.Popen] = {}
_mu = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(kind: str, who: str, **kw) -> None:
    rd = run_dir()
    if rd is None:
        return
    rec = {"ts": _now(), "kind": kind, "who": who, **kw}
    f = rd / "console-events.jsonl"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_events(limit: int = 200) -> list[dict]:
    rd = run_dir()
    if rd is None:
        return []
    try:
        lines = (rd / "console-events.jsonl").read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _cmd_for(action: str, arg) -> list[str] | None:
    rd = run_dir()
    if rd is None:
        return None
    run = rd / "src" / "litellm-gray-rollout" / "scripts" / "run"
    # 优先用 run 目录里冻结的那份 src（和本轮灰度用的是同一份脚本）
    if action == "ramp":
        n = int(arg)
        if n not in ALLOWED_RAMP:
            raise ValueError(f"split {n} 不在 {ALLOWED_RAMP}")
        # dwell=0：驻留已由驾驶台判过；step.sh 仍会自己再验周期、worker、5xx、容量
        return ["bash", str(rd / "step.sh") if (rd / "step.sh").exists() else str(run / "step.sh"), str(n), "0"]
    if action == "converge":
        note = str(arg or "驾驶台：100% 稳定后由人确认收敛")
        return ["bash", str(rd / "converge.sh") if (rd / "converge.sh").exists() else str(run / "converge.sh"), note]
    if action == "commit":
        note = str(arg or "驾驶台：生产核对通过后由人确认提交")
        return ["bash", str(rd / "commit.sh") if (rd / "commit.sh").exists() else str(run / "commit.sh"), note]
    if action == "close_run":
        return ["bash", "-c", f'. {rd}/target.env; set -a; . {rd}/nginx/frozen-env.sh; set +a; '
                f'bash {rd}/sign.sh run_close "驾驶台：由人确认关闭本轮" >/dev/null && bash $SRC/scripts/gray-run-close.sh']
    if action == "global_rollback":
        return ["bash", "-c", f'. {rd}/target.env; set -a; . {rd}/nginx/frozen-env.sh; set +a; '
                f'bash {rd}/sign.sh global_rollback "驾驶台：由人确认全部退回旧版" >/dev/null && bash $SRC/scripts/gray-global-rollback.sh']
    return None


def _lock(rd: Path):
    f = open(rd / ".console.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def lock_holder_busy() -> bool:
    rd = run_dir()
    if rd is None:
        return False
    f = _lock(rd)
    if f is None:
        return True
    f.close()
    return False


def running() -> dict:
    with _mu:
        return {k: {"pid": p.pid, "done": p.poll() is not None, "rc": p.poll()} for k, p in _running.items()}


def start(action: str, arg=None, who: str = "machine", generation: str | None = None) -> dict:
    rd = run_dir()
    if rd is None:
        return {"ok": False, "error": "找不到 run 目录（/root/l3/.gray-run-latest）"}
    with _mu:
        if any(p.poll() is None for p in _running.values()):
            return {"ok": False, "error": "已有动作在执行，等它结束"}
    try:
        cmd = _cmd_for(action, arg)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if cmd is None:
        return {"ok": False, "error": f"不认识的动作 {action}"}
    lockf = _lock(rd)
    if lockf is None:
        return {"ok": False, "error": "别的驾驶者持有 .console.lock（人工接管中），驾驶台不动"}
    logdir = rd / "console" / "actions"
    logdir.mkdir(parents=True, mode=0o700, exist_ok=True)
    logf = logdir / f"{_now()}-{action}.log"
    out = open(logf, "w")
    os.chmod(logf, 0o600)
    env = dict(os.environ, KUBECONFIG=os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"))
    p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    with _mu:
        _running[action] = p
    log_event("action_start", who, action=action, arg=arg, log=str(logf), pid=p.pid, generation=generation)

    def _wait():
        rc = p.wait()
        out.close()
        lockf.close()
        tail = ""
        try:
            tail = "\n".join(Path(logf).read_text(errors="ignore").splitlines()[-6:])
        except OSError:
            pass
        log_event("action_end", who, action=action, rc=rc, tail=tail[-800:], pid=p.pid, generation=generation)

    threading.Thread(target=_wait, daemon=True).start()
    return {"ok": True, "log": str(logf), "pid": p.pid}


def tail_log(path: str, n: int = 40) -> str:
    rd = run_dir()
    if rd is None or not path.startswith(str(rd / "console" / "actions")):
        return ""
    try:
        return "\n".join(Path(path).read_text(errors="ignore").splitlines()[-n:])
    except OSError:
        return ""
