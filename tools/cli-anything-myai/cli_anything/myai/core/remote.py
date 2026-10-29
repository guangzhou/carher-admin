"""Transport to the 243 K3s host: ssh → kubectl / psql / python in pods.

Every call is read-only by construction:
- psql runs with ``default_transaction_read_only=on`` (server rejects writes);
- worker scripts are fixed templates shipped in this package, not free text.

Credentials: ``MYAI_SSH_PASSWORD`` (passed to sshpass through the environment,
never argv) or, when unset, ssh keys with ``BatchMode=yes``.
"""

from __future__ import annotations

import csv
import io
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field


DEFAULT_HOST = "cltx@10.68.13.243"
DEFAULT_NAMESPACE = "ai-token-dashboard"
# 唯一入口（10-29 起）。myai.carher.net 已下线（国内被 CF 分到 LAX，常超时）。
DEFAULT_PUBLIC_URL = "https://chat.auto-link.com.cn/myai"

DB_POD = "usage-db-0"
SYNC_WORKER = "deploy/usage-sync-worker"

COMPONENTS = {
    "dashboard": "deploy/ai-token-dashboard",
    "sync": SYNC_WORKER,
    "realtime": "deploy/usage-realtime-worker",
    "cloudflared": "deploy/cloudflared",
    "db": f"pod/{DB_POD}",
    "redis": "pod/usage-redis-0",
}


class RemoteError(RuntimeError):
    """The remote command failed; message carries the stderr tail."""


@dataclass
class RemoteConfig:
    host: str = DEFAULT_HOST
    namespace: str = DEFAULT_NAMESPACE
    public_url: str = DEFAULT_PUBLIC_URL
    timeout: float = 120.0
    # Where upstream probes run. The sync worker already holds the LiteLLM
    # keys but is production: a heavy probe can OOM it, and it is unreachable
    # while scaled to 0. ``pod/myai-probe`` is an isolated pod (see
    # ``probe-pod up``) with the same env and its own memory limit.
    probe_target: str = SYNC_WORKER
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, **overrides: object) -> "RemoteConfig":
        values = {
            "host": os.getenv("MYAI_HOST") or DEFAULT_HOST,
            "namespace": os.getenv("MYAI_NAMESPACE") or DEFAULT_NAMESPACE,
            "public_url": os.getenv("MYAI_PUBLIC_URL") or DEFAULT_PUBLIC_URL,
            "probe_target": os.getenv("MYAI_PROBE_TARGET") or SYNC_WORKER,
            "password": os.getenv("MYAI_SSH_PASSWORD") or None,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)  # type: ignore[arg-type]

    @property
    def auth_mode(self) -> str:
        return "password(env)" if self.password else "ssh-key"


def ssh_argv(config: RemoteConfig, remote_command: str) -> list[str]:
    """Build the local argv; the password never appears in it."""
    base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if config.password:
        if not shutil.which("sshpass"):
            raise RemoteError("MYAI_SSH_PASSWORD 已设置但本机没有 sshpass(brew install sshpass)")
        base = ["sshpass", "-e", *base]
    else:
        base += ["-o", "BatchMode=yes"]
    return [*base, config.host, remote_command]


def kubectl_cmd(config: RemoteConfig, *args: str) -> str:
    return " ".join(["kubectl", "-n", shlex.quote(config.namespace), *(shlex.quote(a) for a in args)])


def psql_remote_command(config: RemoteConfig) -> str:
    inner = (
        'PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=60000" '
        'psql -X -q -v ON_ERROR_STOP=1 --csv -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
    )
    return f"{kubectl_cmd(config, 'exec', '-i', DB_POD, '--')} sh -c {shlex.quote(inner)}"


PROBE_POD = "myai-probe"


def worker_python_command(config: RemoteConfig) -> str:
    return f"{kubectl_cmd(config, 'exec', '-i', config.probe_target, '--')} python -"


def probe_pod_manifest(image: str, memory: str = "1Gi", ttl_seconds: int = 7200) -> str:
    """Isolated pod with the sync worker's env; it sleeps, then exits by itself."""
    import json

    return json.dumps({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": PROBE_POD, "labels": {"app": PROBE_POD}},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": PROBE_POD,
                "image": image,
                "imagePullPolicy": "Never",
                "command": ["sleep", str(int(ttl_seconds))],
                "envFrom": [{"configMapRef": {"name": "dashboard-config"}}, {"secretRef": {"name": "dashboard-secrets"}}],
                "resources": {"limits": {"cpu": "1", "memory": memory}, "requests": {"cpu": "100m", "memory": "128Mi"}},
            }],
        },
    })


def parse_csv(text: str) -> list[dict[str, str]]:
    """psql --csv output → list of dicts; empty output → []."""
    text = text.strip()
    if not text:
        return []
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


class Remote:
    def __init__(self, config: RemoteConfig, runner=subprocess.run) -> None:
        self.config = config
        self._runner = runner

    def run(self, remote_command: str, stdin: str | None = None) -> str:
        env = dict(os.environ)
        if self.config.password:
            env["SSHPASS"] = self.config.password
        try:
            proc = self._runner(
                ssh_argv(self.config, remote_command),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"远端命令超时({self.config.timeout}s): {remote_command[:120]}") from exc
        # psql over ssh: rc alone is not trustworthy, also scan stderr for ERROR:
        if proc.returncode != 0 or "ERROR:" in (proc.stderr or ""):
            tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise RemoteError(f"rc={proc.returncode}: {tail}")
        return proc.stdout

    def kubectl(self, *args: str) -> str:
        return self.run(kubectl_cmd(self.config, *args))

    def sql(self, statement: str) -> list[dict[str, str]]:
        return parse_csv(self.run(psql_remote_command(self.config), stdin=statement.rstrip().rstrip(";") + ";\n"))

    def worker_python(self, script: str) -> str:
        return self.run(worker_python_command(self.config), stdin=script)
