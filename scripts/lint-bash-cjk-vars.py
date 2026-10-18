#!/usr/bin/env python3
"""找出 shell 脚本里「$VAR 紧贴非 ASCII 字符」的写法，--fix 改成 ${VAR}。

🔴 为什么：macOS 自带 /bin/bash 3.2 在 UTF-8 locale 下把紧随其后的全角字符首字节
吞进变量名（"$V」" ⇒ 变量名 `V\xe3`）：
  - 有 set -u ⇒ `V�: unbound variable` 直接退出（10-18 kiro-rs-ship.sh --expect-ui 验过了却在打印 ✅ 那行崩）
  - 没 set -u ⇒ 静默展开成空串，日志/判据少一截还不报错
198 上 bash 5.1 不复现 ⇒ 只在本机跑的脚本（或本机 bash 先展开再 ssh 过去的串）会咬人。

用法：
  scripts/lint-bash-cjk-vars.py [路径…]          # 只报告，有命中 rc=1
  scripts/lint-bash-cjk-vars.py --fix [路径…]    # 原地改成 ${VAR}，改完逐个 bash -n
不给路径 ⇒ 扫 scripts/*.sh 和 .claude/skills/*/scripts/*.sh。
只改 `$NAME` 形式（NAME=[A-Za-z_][A-Za-z0-9_]*），`$1` `$@` `${…}` 不动；注释行跳过。
"""
import glob
import pathlib
import re
import subprocess
import sys

# (?<!\\) 跳过 \$VAR（转义后是字面量，不展开）
PAT = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


def targets(args: list[str]) -> list[pathlib.Path]:
    if args:
        return [pathlib.Path(a) for a in args]
    root = pathlib.Path(__file__).resolve().parent.parent
    files = glob.glob(str(root / "scripts/*.sh")) + glob.glob(str(root / ".claude/skills/*/scripts/*.sh"))
    return sorted(pathlib.Path(f) for f in files)


def main() -> int:
    argv = sys.argv[1:]
    fix = "--fix" in argv
    files = targets([a for a in argv if a != "--fix"])
    hits = 0
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#") or not PAT.search(line):
                continue
            hits += len(PAT.findall(line))
            print(f"{f}:{i + 1}: {line.strip()[:120]}")
            if fix:
                lines[i] = PAT.sub(r"${\1}", line)
                changed = True
        if changed:
            f.write_text("".join(lines), encoding="utf-8")
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            print(f"   {'✅ fixed, bash -n ok' if r.returncode == 0 else '🔴 bash -n 失败: ' + r.stderr.strip()}")
            if r.returncode:
                return 2
    print(f"{'FIXED' if fix else 'FOUND'} {hits} 处 / {len(files)} 个文件")
    return 1 if hits and not fix else 0


if __name__ == "__main__":
    sys.exit(main())
