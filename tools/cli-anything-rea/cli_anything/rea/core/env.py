"""Where REA and its dependencies live on this Mac, and how to run it.

Layout (all outside this repo, nothing mirrored in)::

    ~/codes/rea/                 git clone of morluto/rea (built: dist/)
    ~/codes/rea-deps/rea.env     shell file exporting every provider path (no credentials)
    ~/codes/rea-deps/...         Ghidra 12.1.4 (+ self-built mac_arm_64 decompiler), Kuna 1.833,
                                 jadx-headless-mcp 0.7.1 JAR, logs/, samples/

REA requires node 22.19+/24.11+/26+; we pin Homebrew node@24 because the default `node` on this
machine moves with `brew upgrade`. The env file is the single source of the provider paths: the
harness parses it instead of keeping a second copy.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Final

ENV_LINE: Final = re.compile(r'^export\s+([A-Z_][A-Z0-9_]*)="(.*)"\s*$')


class ReaError(RuntimeError):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def rea_root() -> Path:
    return Path(os.environ.get("REA_SRC", "~/codes/rea")).expanduser()


def deps_root() -> Path:
    return Path(os.environ.get("REA_DEPS", "~/codes/rea-deps")).expanduser()


def env_file() -> Path:
    return deps_root() / "rea.env"


def _expand(value: str, env: dict[str, str]) -> str:
    """Expand $HOME / $VAR / ${VAR} the way the shell would for our simple env file."""
    return re.sub(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", lambda m: env.get(m.group(1), ""), value)


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse rea.env into a dict layered on the current environment. Only `export K="v"` lines count."""
    path = path or env_file()
    if not path.is_file():
        raise ReaError(f"{path} missing; see the skill cli-anything-rea §安装")
    env = dict(os.environ)
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = ENV_LINE.match(raw.strip())
        if m:
            env[m.group(1)] = _expand(m.group(2), env)
    return env


def declared(path: Path | None = None) -> dict[str, str]:
    """Only the variables rea.env itself sets (for display / doctor)."""
    base = dict(os.environ)
    full = load_env(path)
    return {k: v for k, v in full.items() if base.get(k) != v or k == "PATH"}


def run_rea(args: list[str], timeout: int = 900, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `node scripts/rea.mjs ARGS` from the clone with rea.env applied. Never through a shell."""
    root = rea_root()
    if not (root / "dist").is_dir():
        raise ReaError(f"{root}/dist missing: build first (`cli-anything-rea deps build`)")
    env = env or load_env()
    return subprocess.run(["node", str(root / "scripts" / "rea.mjs"), *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=timeout)


def node_version(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    r = subprocess.run(["node", "-v"], env=env, capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or r.stderr.strip()


def git_head(root: Path | None = None) -> dict[str, str]:
    root = root or rea_root()
    r = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H%x09%ci%x09%s"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ReaError(f"{root} is not a git clone", r.stderr)
    sha, date, subject = r.stdout.strip().split("\t", 2)
    return {"sha": sha, "date": date, "subject": subject}
