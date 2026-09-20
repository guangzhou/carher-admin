#!/usr/bin/env python3
"""本机 Codex 调本地 CLI 工具（imagegen 等）的整条链路体检。

四段，任一段红就停在那儿——因为下游全是它的下游，先修上游再复跑。
退出码 0 = 四段全绿。

用法:
    scripts/toolchain_doctor.py            # 全查
    scripts/toolchain_doctor.py --quick    # 跳过出网探测（最慢那段）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG = Path.home() / ".codex" / "config.toml"
NEEDED_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL")

OK, BAD = "PASS", "FAIL"
_fails: list[str] = []


def say(status: str, title: str, detail: str = "") -> None:
    mark = "✅" if status == OK else "❌"
    print(f"{mark} [{status}] {title}")
    for line in detail.splitlines():
        if line.strip():
            print(f"        {line}")
    if status == BAD:
        _fails.append(title)


def run(cmd: list[str], timeout: int = 30, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=env if env is not None else os.environ,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"


# ---------------------------------------------------------------- 1. env 策略
def check_env_policy() -> dict[str, str]:
    """GUI 双击起的 Codex 不经过任何 shell，.zshrc/.zshenv 都不读。

    所以 OPENAI_API_KEY 只能来自 config.toml 的 [shell_environment_policy.set]。
    ⚠️ 判据不是"文件里有这行"，而是"子进程真拿到了"——用 `codex sandbox` 把
    父进程环境清空后实测，这才是 GUI 那条路的等价物。
    """
    if not CONFIG.exists():
        say(BAD, "config.toml 存在", f"{CONFIG} 不存在")
        return {}
    try:
        cfg = tomllib.load(CONFIG.open("rb"))
    except Exception as e:  # noqa: BLE001
        say(BAD, "config.toml 可解析", f"{type(e).__name__}: {e}")
        return {}

    declared = cfg.get("shell_environment_policy", {}).get("set", {})
    missing = [k for k in NEEDED_ENV if k not in declared]
    if missing:
        say(BAD, "config.toml 声明了所需 env",
            f"缺 {missing}；写进 [shell_environment_policy.set]，"
            f"别指望 shell rc —— GUI 启动根本不读")
        return {}
    say(OK, "config.toml 声明了所需 env", f"已声明: {sorted(declared)}")

    if not shutil.which("codex"):
        say(BAD, "env 真的注入到子进程（codex sandbox 实测）", "codex CLI 不在 PATH，无法实测")
        return declared

    stripped = {k: v for k, v in os.environ.items() if k not in NEEDED_ENV}
    rc, out = run(
        ["codex", "sandbox", "--", "/bin/sh", "-c",
         'echo "{\\"key_len\\":${#OPENAI_API_KEY},\\"base\\":\\"$OPENAI_BASE_URL\\"}"'],
        timeout=60, env=stripped,
    )
    payload = next((l for l in out.splitlines() if l.strip().startswith("{")), "")
    try:
        got = json.loads(payload)
    except Exception:  # noqa: BLE001
        say(BAD, "env 真的注入到子进程（codex sandbox 实测）", f"rc={rc}\n{out[-400:]}")
        return declared
    if got.get("key_len", 0) > 0 and got.get("base"):
        say(OK, "env 真的注入到子进程（codex sandbox 实测）",
            f"父进程已清空这两个变量，子进程仍拿到 key_len={got['key_len']} base={got['base']}")
    else:
        say(BAD, "env 真的注入到子进程（codex sandbox 实测）", f"子进程读到 {got}")
    return declared


# ------------------------------------------------------------- 2. python 健康
PROBE = (
    "import platform,plistlib,ssl,json;"
    "import xml.parsers.expat as e;"
    "print(json.dumps({'v':platform.python_version(),'mac':platform.mac_ver()[0],"
    "'expat':list(e.version_info)}))"
)


def check_pythons() -> str | None:
    """homebrew 的 python 瓶子会因 expat 版本比本机新而整体崩，症状分散得看不出同源：
    pip 装不了任何包 / platform.mac_ver() 返回空 / truststore 报 int('') 。
    判据统一成一行探针，谁绿谁能用。
    """
    seen, healthy = set(), []
    cands = [shutil.which("python3"), "/opt/homebrew/bin/python3", "/usr/bin/python3"]
    for py in [c for c in cands if c]:
        real = os.path.realpath(py)
        if real in seen:
            continue
        seen.add(real)
        # cwd 必须离开仓库：repo 里有 operator/ 目录会遮蔽 stdlib 的 operator
        rc, out = run([py, "-c", PROBE], timeout=30)
        if rc == 0 and out.startswith("{"):
            info = json.loads(out)
            if not info["mac"]:
                say(BAD, f"python 可用: {py}", "platform.mac_ver() 返回空 —— 瓶子坏了，走 §修法")
                continue
            say(OK, f"python 可用: {py}",
                f"{info['v']} | mac_ver {info['mac']} | expat {tuple(info['expat'])}")
            healthy.append(py)
        else:
            hint = ""
            if "pyexpat" in out or "libexpat" in out:
                hint = "\n→ expat 符号对不上：瓶子是在更新的 expat 上烤的，跑 brew_rebuild_python.sh"
            say(BAD, f"python 可用: {py}", f"rc={rc}\n{out[-500:]}{hint}")
    return healthy[0] if healthy else None


# ------------------------------------------------------------- 3. openai SDK
def check_sdk(py: str | None) -> None:
    if not py:
        say(BAD, "openai SDK 可导入", "没有健康的 python3，跳过")
        return
    rc, out = run([py, "-c", "import openai;print(openai.__version__)"], timeout=60)
    if rc == 0:
        say(OK, "openai SDK 可导入", f"{py} → openai {out}")
    else:
        say(BAD, "openai SDK 可导入",
            f"{out[-300:]}\n→ {py} -m pip install --break-system-packages openai")


# ------------------------------------------------------------ 4. 上游可达性
def check_upstream(declared: dict[str, str], quick: bool) -> None:
    base = declared.get("OPENAI_BASE_URL")
    if quick or not base:
        say(OK, "上游可达", "--quick 跳过" if quick else "无 base_url，跳过")
        return
    host = base.split("//", 1)[-1].split("/", 1)[0]
    rc, out = run(
        ["curl", "-s", "-o", "/dev/null", "-m", "8",
         "-w", "connect=%{time_connect} total=%{time_total} code=%{http_code}",
         f"https://{host}/"],
        timeout=15,
    )
    # connect=0 且 total 撞满超时 = 挂住（不是快速失败，会把上层拖到重试上限）
    if "connect=0.000000" in out:
        say(BAD, "上游可达", f"{host} connect 挂住: {out}\n→ 见 memory reference_mac_egress_hangs_and_working_mirrors")
    else:
        say(OK, "上游可达", f"{host} {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过出网探测")
    args = ap.parse_args()

    if Path.cwd().joinpath("operator").is_dir():
        print("⚠️  当前目录有 operator/ 会遮蔽 stdlib，已在子进程里规避；建议 cd /tmp 再跑\n")

    print("── 1. Codex 子进程能不能拿到 env ─────────────────")
    declared = check_env_policy()
    print("\n── 2. python3 健康 ──────────────────────────────")
    py = check_pythons()
    print("\n── 3. openai SDK ────────────────────────────────")
    check_sdk(py)
    print("\n── 4. 上游 ──────────────────────────────────────")
    check_upstream(declared, args.quick)

    print("\n" + "=" * 50)
    if _fails:
        print(f"❌ {len(_fails)} 项红：" + " / ".join(_fails))
        return 1
    print("✅ 四段全绿，本地 CLI 工具链可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
