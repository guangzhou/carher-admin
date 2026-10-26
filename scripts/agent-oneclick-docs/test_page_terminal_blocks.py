#!/usr/bin/env python3
"""页面上「复制到终端」的那几段命令:原样抠出来,在假下载文件夹里**真跑**(zsh = Mac 默认、bash = Linux 常见)。

为什么要有它(2026-10-26 用户 review「这段代码有问题吧?」):旧命令写死文件名 + `[ -d cursor-mac ] || unzip`,
只在「第一次下载、文件名正好」时对。实测三种坏形状:
  ① 上次解压的旧文件夹还在 ⇒ 不解压,跑的是**旧版**安装脚本(没报错,最危险)
  ② 浏览器第二次下载存成 `codex-mac (1).command` ⇒「找不到文件」
  ③ 中文 Linux 下载文件夹叫 ~/下载 ⇒ cd 错地方
门禁只查「有没有 chmod」,对这三种全是绿的 ⇒ 改成在场景里跑真命令。

每段命令把 `sh`/`bash` 换成一个记录器(PATH 最前面放假的 sh/bash),记下被执行的是哪个文件、文件内容是哪一版。
用法: python3 scripts/agent-oneclick-docs/test_page_terminal_blocks.py   (退出码 0 = 全过)
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "oneclick-all.html"
SHELLS = [s for s in ("zsh", "bash") if shutil.which(s)]


def blocks():
    t = PAGE.read_text(encoding="utf-8")
    out = {}
    for b in re.findall(r"<pre[^>]*><code>(.*?)</code></pre>", t, re.S):
        b = html.unescape(b)
        m = re.search(r'(?:sh|bash) "\$(?:f|z)(?:/([A-Za-z-]+\.command))?"', b)
        if not m:
            continue
        if m.group(1):
            out["cursor:" + m.group(1)] = b
        else:
            name = re.search(r"grep '\^([a-z-]+?)\.\*command", b).group(1)   # 页面里写 grep '^codex-mac.*command$'
            out[name] = b
    return out


def fake_shell_dir(root):
    """PATH 里最前面的 sh / bash:不执行,只把「跑了哪个文件 + 它的第一行内容」写进 ran.log。"""
    d = root / "fakebin"
    d.mkdir()
    for n in ("sh", "bash"):
        p = d / n
        p.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$1" "$(head -n 1 "$1" 2>/dev/null)" >> "$RAN_LOG"\n')
        p.chmod(0o755)
    return d


DEFAULT_XDG = None


def run(block, shell, downloads, home, xdg="default"):
    if xdg == "default":
        xdg = DEFAULT_XDG
    root = downloads.parent
    log = root / "ran.log"
    log.unlink(missing_ok=True)
    fb = root / "fakebin"
    if not fb.exists():
        fake_shell_dir(root)
    env = {"HOME": str(home), "PATH": f"{fb}:/usr/bin:/bin", "RAN_LOG": str(log), "LANG": "en_US.UTF-8"}
    # xdg-user-dir:模拟 Linux 中文系统(返回 ~/下载)或没装(命令不存在)
    # xdg-user-dir 三种真实形状:① 中文 Linux 返回 ~/下载 ② **没配下载文件夹时返回 $HOME 本身**(188 Ubuntu 实测,
    #   第二版命令漏掉这条 ⇒ 在 Linux 上 cd 到家目录、一个都找不到)③ Mac 上没有这个命令。默认模拟 ②,
    #   这样 Linux 机器上真的有 xdg-user-dir 时也不会因为它而假绿/假红。
    (fb / "xdg-user-dir").unlink(missing_ok=True)
    if xdg != "absent":
        (fb / "xdg-user-dir").write_text(f'#!/bin/sh\necho "{xdg or "$HOME"}"\n')
        (fb / "xdg-user-dir").chmod(0o755)
    # 真 shell 要用系统的绝对路径(PATH 里的 sh/bash 是假的)
    real = shutil.which(shell, path="/bin:/usr/bin")
    r = subprocess.run([real, "-c", block], env=env, capture_output=True, text=True, timeout=60, cwd=str(home))
    ran = log.read_text().strip().splitlines() if log.exists() else []
    return r, ran


def mk_cursor_zip(path, version):
    with zipfile.ZipFile(path, "w") as z:
        for s in ("INSTALL-Mac.command", "REMOTE-SSH-Mac.command"):
            z.writestr(f"cursor-mac/{s}", f"# {version}\n")


def case(fails, label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else f"  ⇒ {detail}"))
    if not ok:
        fails.append(label)


def main():
    global DEFAULT_XDG
    B = blocks()
    want = {"cursor:INSTALL-Mac.command", "cursor:REMOTE-SSH-Mac.command", "codex-mac", "claude-mac",
            "codex-doctor-mac", "claude-doctor-mac"}
    fails = []
    case(fails, f"页面里 6 段终端命令都找到了({sorted(B)})", set(B) == want, f"缺 {sorted(want - set(B))}")
    t = PAGE.read_text(encoding="utf-8")
    case(fails, "页面里没有写死的 cd ~/Downloads / chmod +x", "cd ~/Downloads" not in t and "chmod +x" not in t)
    for shell in SHELLS:
      for xdg_mode in ("unset", "absent"):
        DEFAULT_XDG = None if xdg_mode == "unset" else "absent"
        print(f"--- {shell} · xdg-user-dir {'没配下载文件夹(返回 $HOME)' if xdg_mode == 'unset' else '不存在(Mac)'}")
        for key, blk in sorted(B.items()):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td); home = root / "home"; dl = home / "Downloads"; dl.mkdir(parents=True)
                if key.startswith("cursor:"):
                    script = key.split(":")[1]
                    # ① 首次:只有 zip
                    mk_cursor_zip(dl / "cursor-mac.zip", "v1")
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 首次下载", ran == [f"cursor-mac/{script}|# v1"], f"ran={ran} err={r.stderr[-200:]}")
                    # ② 旧文件夹还在 + 新下载的 zip 被存成 (1) ⇒ 必须跑新版
                    os.utime(dl / "cursor-mac.zip", (1_000_000, 1_000_000))
                    mk_cursor_zip(dl / "cursor-mac (1).zip", "v2")
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 旧文件夹在 + 第二次下载 (1).zip ⇒ 跑新版", ran == [f"cursor-mac/{script}|# v2"], f"ran={ran}")
                    # ③ Safari 自动解压:只有文件夹、没有 zip
                    for z in dl.glob("*.zip"): z.unlink()
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 只有已解压的文件夹(Safari)", ran == [f"cursor-mac/{script}|# v2"], f"ran={ran}")
                    # ④ Linux 中文:下载在 ~/下载
                    shutil.rmtree(dl / "cursor-mac")
                    zh = home / "下载"; zh.mkdir()
                    mk_cursor_zip(zh / "cursor-mac.zip", "v3")
                    r, ran = run(blk, shell, dl, home, xdg=str(zh))
                    case(fails, f"{key} Linux 下载文件夹叫「下载」", ran == [f"cursor-mac/{script}|# v3"], f"ran={ran} err={r.stderr[-200:]}")
                    # ⑤ 什么都没下 ⇒ 给人话、不跑任何东西
                    shutil.rmtree(zh)
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 没下载 ⇒ 提示", ran == [] and "没找到" in r.stdout, f"ran={ran} out={r.stdout[-120:]}")
                else:
                    name = key + ".command"
                    (dl / name).write_text("# v1\n")
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 首次下载", ran == [f"{name}|# v1"], f"ran={ran} err={r.stderr[-200:]}")
                    os.utime(dl / name, (1_000_000, 1_000_000))
                    (dl / f"{key} (1).command").write_text("# v2\n")
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 第二次下载存成 (1) ⇒ 跑新版", ran == [f"{key} (1).command|# v2"], f"ran={ran}")
                    # 别的工具的文件不许被误认(codex-doctor-mac 不能匹配到 codex-mac 的规则里,反之亦然)
                    (dl / "codex-doctor-mac.command").write_text("# other\n") if key == "codex-mac" else None
                    (dl / "codex-mac.command").write_text("# other\n") if key == "codex-doctor-mac" else None
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 不会误拿同前缀的别的文件", len(ran) == 1 and ran[0].startswith((f"{key}.command|", f"{key} (1).command|")), f"ran={ran}")
                    zh = home / "下载"; zh.mkdir()
                    (zh / name).write_text("# v3\n")
                    r, ran = run(blk, shell, dl, home, xdg=str(zh))
                    case(fails, f"{key} Linux 下载文件夹叫「下载」", ran == [f"{name}|# v3"], f"ran={ran}")
                    for f in list(dl.iterdir()): f.unlink()
                    r, ran = run(blk, shell, dl, home)
                    case(fails, f"{key} 没下载 ⇒ 提示", ran == [] and "没找到" in r.stdout, f"ran={ran} out={r.stdout[-120:]}")
    print(("VERDICT: PASS" if not fails else f"VERDICT: FAIL ({len(fails)})"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
