"""198 backend: the real LiteLLM deployment this harness inspects.

Every call here is **read-only** and runs on 198 over ssh with `sudo -n`
(ssh key, no password). Three real backends are reached through it:

* the running proxy pods (``kubectl exec`` into a lane's pod)
* the upstream source tree (``git -C /root/litellm-fork show/archive <tag>``)
* the production Postgres (``psql`` inside ``litellm-db-0`` with a statement timeout)

Nothing is reimplemented locally: patch effectiveness is read from the live
interpreter, schema from the tag's ``schema.prisma``, and DB facts from Postgres.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass

HOST = os.environ.get("LITELLM_198_HOST", "cltx@10.68.13.198")
NS = "litellm-product"
FORK = "/root/litellm-fork"
DB_POD = "litellm-db-0"
KUBE = "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; "
LANES: dict[str, str] = {
    "prod": "litellm-proxy",
    "gray": "litellm-proxy-gray",
    "guarded-old": "litellm-proxy-guarded-old",
}
# psql runs against a shared DB (casdoor / owui / myai use it too, max_connections=100):
# one session per call, hard statement timeout, never a full scan of SpendLogs.
DEFAULT_STATEMENT_TIMEOUT_MS = 20000


@dataclass(frozen=True, slots=True)
class RemoteError(Exception):
    message: str
    returncode: int | None = None
    stderr: str = ""

    def __str__(self) -> str:
        return self.message


def run(script: str, *, timeout: float = 180, stdin: str | None = None) -> str:
    """Run a bash script on 198 as root (sudo -n). Returns stdout; raises RemoteError on failure."""
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOST,
        f"sudo -n bash -c {shlex.quote(script)}",
    ]
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"198 command timed out after {timeout:g}s") from exc
    if r.returncode != 0:
        tail = "\n".join(l for l in r.stderr.splitlines() if "remote port forwarding" not in l)[-800:]
        raise RemoteError(f"198 command failed rc={r.returncode}: {script[:100]}", r.returncode, tail)
    return r.stdout


def lane_deploy(lane: str) -> str:
    if lane not in LANES:
        raise RemoteError(f"unknown lane {lane!r}; choose one of {sorted(LANES)}")
    return LANES[lane]


def lane_pod(lane: str) -> str:
    """A Running pod of the lane, found through the Deployment's own selector (never hardcoded)."""
    deploy = lane_deploy(lane)
    sel = run(KUBE + f"kubectl get deploy -n {NS} {deploy} -o jsonpath='{{.spec.selector.matchLabels}}'").strip()
    labels = ",".join(f"{k}={v}" for k, v in json.loads(sel).items()) if sel else f"app={deploy}"
    pod = run(
        KUBE + f"kubectl get pod -n {NS} -l {shlex.quote(labels)} --field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    ).strip()
    if not pod:
        raise RemoteError(f"lane {lane} has no Running pod (selector {labels})")
    return pod


def pod_python(pod: str, code: str, *, timeout: float = 120) -> str:
    """Run python inside a proxy pod with stdin as the program (so quoting never bites)."""
    return run(KUBE + f"kubectl exec -i -n {NS} {pod} -- python -", stdin=code, timeout=timeout)


def live_version(pod: str) -> str:
    out = pod_python(pod, "import importlib.metadata as m; print(m.version('litellm'))")
    return out.strip().splitlines()[-1]


def fork_has_tag(tag: str) -> bool:
    out = run(f"git -C {FORK} rev-parse -q --verify {shlex.quote('refs/tags/' + tag)} >/dev/null && echo yes || echo no")
    return out.strip() == "yes"


def fork_show(tag: str, path: str) -> str:
    return run(f"git -C {FORK} show {shlex.quote(tag + ':' + path)}", timeout=120)


def psql(sql: str, *, timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> list[dict[str, str]]:
    """Read-only query against prod Postgres. Returns rows as dicts (all values as text).

    The transaction is opened READ ONLY, so a stray write fails in Postgres instead of landing.
    """
    sql_q = shlex.quote(sql)
    script = (
        KUBE
        + f"kubectl exec -n {NS} {DB_POD} -- sh -c "
        + shlex.quote(
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -A -F "\t" -v ON_ERROR_STOP=1 '
            f'-c "SET statement_timeout={int(timeout_ms)}" '
            '-c "SET default_transaction_read_only=on" '
            '-c "$1"'
        )
        + f" sh {sql_q}"
    )
    out = run(script, timeout=max(30.0, timeout_ms / 1000 + 20))
    lines = [l for l in out.splitlines() if l and l not in ("SET",)]
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if line.startswith("(") and line.endswith(("row)", "rows)")):
            continue
        rows.append(dict(zip(header, line.split("\t"))))
    return rows
