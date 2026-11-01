"""A mihomo instance on 198: ~/mihomo-harness/<name>/{config.yaml,secret,pid,log,cache.db,binary}.

Runs as cltx (no sudo), detached with setsid, 127.0.0.1-only. Not a system service: it does not
survive a 198 reboot (that's what a k3s Deployment would be for; see MIHOMO.md).
All controller calls go through remote_ctl.py executed on 198, so the secret stays there.
"""
from __future__ import annotations

import json
import os
import shlex
import urllib.parse
from typing import Any

from cli_anything.mihomo.core import binary, config, subscription
from cli_anything.mihomo.utils import remote198

_CTL_SRC = os.path.join(os.path.dirname(__file__), "remote_ctl.py")


class InstanceError(RuntimeError):
    pass


_OWNER_OK: set[str] = set()  # instances whose 198 owner marker was checked in this process


def rdir(name: str) -> str:
    config._state_path(name)  # validates the name
    return f"{remote198.ROOT}/{name}"


def enc(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def ctl(name: str, jobs: list[dict[str, Any]], *, workers: int = 16, timeout: float = 180) -> list[dict[str, Any]]:
    """Run controller/fetch jobs on 198 against this instance. One result per job."""
    if name not in _OWNER_OK:
        check_owner(name)
        _OWNER_OK.add(name)
    st = config.load_state(name)
    with open(_CTL_SRC) as fh:
        src = fh.read()
    spec = json.dumps({"dir": f"{remote198.home()}/{rdir(name)}", "ctl": st["ctl_port"],
                       "jobs": jobs, "workers": workers})
    out = remote198.run(f"python3 -c {shlex.quote(src)}", stdin=spec, timeout=timeout)
    return json.loads(out)


def api(name: str, method: str, path: str, body: Any = None, timeout: int = 30) -> dict[str, Any]:
    r = ctl(name, [{"kind": "api", "method": method, "path": path, "body": body, "timeout": timeout}],
            timeout=timeout + 60)[0]
    if r["status"] == 0:
        raise InstanceError(f"controller unreachable ({r.get('error')}); is `{name}` running? `instance status {name}`")
    return r


def api_ok(name: str, method: str, path: str, body: Any = None, timeout: int = 30) -> Any:
    r = api(name, method, path, body, timeout)
    if r["status"] >= 400:
        msg = r["body"].get("message") if isinstance(r["body"], dict) else r["body"]
        raise InstanceError(f"{method} {urllib.parse.unquote(path)} -> HTTP {r['status']}: {msg}")
    return r["body"]


def render(name: str) -> tuple[dict[str, Any], str]:
    """Build the config for `name` from the cached subscription; audit it; return (cfg, yaml)."""
    cfg = config.build(subscription.load(), config.load_state(name))
    bad = config.audit(cfg)
    if bad:
        raise InstanceError(f"refusing unsafe config: {bad}")
    return cfg, config.dump(cfg)


def check_owner(name: str) -> str | None:
    """Instance dirs on 198 are shared by all local accounts; refuse to touch one owned by another.

    Returns the owner recorded on 198 (None = dir absent or pre-marker). Raises on mismatch.
    """
    d = rdir(name)
    out = remote198.run(f"cat ~/{d}/account 2>/dev/null || true").strip() or None
    if out and out != subscription.account():
        raise InstanceError(f"198 ~/{d} belongs to account {out!r}, you are {subscription.account()!r}: "
                            f"pass `--account {out}` or pick another --name")
    return out


def push_config(name: str) -> dict[str, Any]:
    """Write config.yaml on 198 (0600) and `mihomo -t` it with the instance's binary."""
    check_owner(name)
    _, text = render(name)
    d = rdir(name)
    st = config.load_state(name)
    exe = _binary_for(name, st)
    remote198.run(f"umask 077; mkdir -p ~/{d}; printf %s {shlex.quote(subscription.account())} > ~/{d}/account; "
                  f"cat > ~/{d}/config.yaml.part", stdin=text)
    out = remote198.run(f"cd ~/{d}; ~/{exe} -t -d . -f config.yaml.part 2>&1 | tail -3; "
                        f"~/{exe} -t -d . -f config.yaml.part >/dev/null 2>&1 && mv config.yaml.part config.yaml")
    if "successful" not in out:
        raise InstanceError(f"mihomo -t rejected the config:\n{out}")
    return {"remote": f"~/{d}/config.yaml", "bytes": len(text.encode()), "test": out.strip().splitlines()[-1]}


def _binary_for(name: str, st: dict[str, Any]) -> str:
    v = st.get("version")
    if not v:
        have = binary.installed()
        if not have:
            raise InstanceError("no mihomo on 198; run `binary fetch` then `binary install`")
        v = have[-1]
    return binary.remote_path(v)


_PORTS_FREE = ("for p in {ports}; do if ss -ltnH \"sport = :$p\" | grep -q .; then echo \"busy $p\"; fi; done")


def start(name: str) -> dict[str, Any]:
    check_owner(name)
    st = config.load_state(name)
    ports = config.check_ports(st)
    if status(name)["running"]:
        raise InstanceError(f"{name} already running; use `instance reload` or `instance stop`")
    busy = remote198.run(_PORTS_FREE.format(ports=" ".join(map(str, ports)))).split()
    if busy:
        raise InstanceError(f"ports busy on 198: {busy[1::2]}")
    pushed = push_config(name)
    d, exe = rdir(name), _binary_for(name, st)
    remote198.run(
        f"cd ~/{d}; umask 077; test -s secret || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \\n' > secret; "
        f"CLASH_OVERRIDE_SECRET=$(cat secret) setsid nohup ~/{exe} -d . -f config.yaml >> log 2>&1 < /dev/null & "
        f"echo $! > pid; "
        f"for i in $(seq 1 40); do ss -ltnH 'sport = :{st['ctl_port']}' | grep -q . && break; sleep 0.5; done")
    s = status(name)
    if not s["running"] or not s["ctl_listening"]:
        raise InstanceError(f"{name} did not come up:\n{logs(name, 20)}")
    return {"started": name, "config": pushed, **s}


def stop(name: str) -> dict[str, Any]:
    check_owner(name)
    d = rdir(name)
    out = remote198.run(
        f"cd ~/{d} 2>/dev/null || {{ echo absent; exit 0; }}; P=$(cat pid 2>/dev/null || true); "
        f"if [ -n \"$P\" ] && readlink /proc/$P/exe 2>/dev/null | grep -q /mihomo-harness/bin/; then kill $P; "
        f"for i in $(seq 1 20); do kill -0 $P 2>/dev/null || break; sleep 0.25; done; rm -f pid; echo stopped; "
        f"else rm -f pid; echo not-running; fi").strip()
    return {"instance": name, "result": out}


def status(name: str) -> dict[str, Any]:
    st = config.load_state(name)
    d = rdir(name)
    raw = remote198.run(
        f"cd ~/{d} 2>/dev/null || {{ echo '{{\"present\":false}}'; exit 0; }}; P=$(cat pid 2>/dev/null || true); "
        f"E=$( [ -n \"$P\" ] && readlink /proc/$P/exe 2>/dev/null || true); "
        f"L=$(ss -ltnH 2>/dev/null | awk '{{print $4}}' | tr '\\n' ' '); "
        f"printf '{{\"present\":true,\"pid\":\"%s\",\"exe\":\"%s\",\"listening\":\"%s\"}}' \"$P\" \"$E\" \"$L\"")
    s = json.loads(raw)
    running = bool(s.get("present") and s.get("pid") and "/mihomo-harness/bin/" in s.get("exe", ""))
    listening = set(s.get("listening", "").split())
    want = [st["mixed_port"], *(p["port"] for p in st["pins"])]
    return {
        "instance": name, "present": s.get("present", False), "running": running,
        "pid": int(s["pid"]) if running else None, "binary": s.get("exe") if running else None,
        "ctl_listening": f"127.0.0.1:{st['ctl_port']}" in listening,
        "ports": {str(p): f"127.0.0.1:{p}" in listening for p in want},
        "exposed": sorted(a for a in listening if a.rsplit(":", 1)[-1] in {str(p) for p in [*want, st["ctl_port"]]}
                          and not a.startswith("127.0.0.1:")),
    }


def list_remote() -> list[dict[str, Any]]:
    """Every instance dir on 198 with its owner account, pid liveness and listening ports."""
    raw = remote198.run(
        f"cd ~/{remote198.ROOT} 2>/dev/null || exit 0; for d in */; do d=${{d%/}}; [ \"$d\" = bin ] && continue; "
        f"P=$(cat $d/pid 2>/dev/null || true); E=$( [ -n \"$P\" ] && readlink /proc/$P/exe 2>/dev/null || true); "
        f"A=$(cat $d/account 2>/dev/null || true); "
        f"L=$( [ -n \"$E\" ] && ss -ltnpH 2>/dev/null | grep \"pid=$P,\" | awk '{{print $4}}' | tr '\\n' ' ' || true); "
        f"printf '%s|%s|%s|%s\\n' \"$d\" \"$A\" \"$E\" \"$L\"; done")
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        d, acct, exe, listen = (line.split("|") + ["", "", ""])[:4]
        rows.append({"instance": d, "account": acct or None, "running": "/mihomo-harness/bin/" in exe,
                     "listening": sorted(listen.split(), key=lambda a: int(a.rsplit(":", 1)[-1]))})
    return rows


def logs(name: str, lines: int = 50) -> str:
    return remote198.run(f"tail -n {int(lines)} ~/{rdir(name)}/log 2>/dev/null || true")


def reload(name: str) -> dict[str, Any]:
    """Re-render, push, hot-apply via PUT /configs?force=true. Selections survive (cache.db)."""
    pushed = push_config(name)
    path = f"{remote198.home()}/{rdir(name)}/config.yaml"
    api_ok(name, "PUT", "/configs?force=true", {"path": path}, timeout=60)
    return {"reloaded": name, "config": pushed}


def destroy(name: str) -> dict[str, Any]:
    check_owner(name)
    stopped = stop(name)
    remote198.run(f"rm -rf ~/{rdir(name)}")
    try:
        os.remove(config._state_path(name))
    except FileNotFoundError:
        pass
    return {"destroyed": name, **stopped}
