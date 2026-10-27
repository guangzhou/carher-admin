#!/usr/bin/env python3
"""Codex 桌面 App 的模型菜单 = 它自带的 codex 跑 app-server 时 `model/list` 的返回(10-27 沉淀,只读)。

App 不自己拉网关:它启动内置 codex(<App>/Contents/Resources/codex-cli/bin/codex)的 app-server,
菜单就是 model/list,而 model/list 读 config.toml 里的 model_catalog_json(刷模型/每日同步写的那份)。
⇒ 判「Codex App 菜单对不对」用 App 自带那个 codex 问一次,不要只看 catalog 文件。

用法:
  python3 codex-app-model-list.py                    # 用 ~/.codex
  CODEX_HOME=/tmp/x python3 codex-app-model-list.py  # 隔离目录(⚠ 在隔离目录跑安装器会覆盖全局 LaunchAgent,测完 bootout)
"""
import json, os, select, subprocess, sys, time
from pathlib import Path

CANDS = ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
         "/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex"]
codex = next((c for c in CANDS if Path(c).exists()), None)
if not codex:
    sys.exit("找不到 Codex App 自带的 codex(装了 Codex 桌面 App 吗?)")
ver = subprocess.run([codex, "--version"], capture_output=True, text=True).stdout.strip()
p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, env=dict(os.environ))


def send(o):
    p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()


def recv(i, t=40):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([p.stdout], [], [], 1)
        if r:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == i:
                return m
    return None


try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "carher-probe", "version": "1"}}})
    if not recv(1):
        sys.exit("app-server 没回 initialize")
    send({"jsonrpc": "2.0", "method": "initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}})
    r = recv(2)
finally:
    p.kill()
if not r or "error" in r:
    sys.exit(f"model/list 失败:{(r or {}).get('error')}")
ids = [d.get("id") or d.get("model") for d in r["result"].get("data", [])]
print(f"{ver} · CODEX_HOME={os.environ.get('CODEX_HOME') or '~/.codex'}")
print(f"Codex App 菜单 {len(ids)} 个:{', '.join(ids)}")
