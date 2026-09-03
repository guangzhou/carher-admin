#!/usr/bin/env python3
"""lane_live_probe.py —— 「这条 lane 此刻活着吗」的活体判据（跑在 198 上，直连 proxy svc）。

为什么需要它：
  「最近 72h 有过 200」证明的是**过去**。seed 里的 bearer 会过期，过期后 lane 照样 Running、
  照样 Ready，只有真发一发才知道。入池前必须逐条判活，不活的当场剔除，**不带病入池**。

线型必须复刻真 Cursor（否则测的是另一条路径）：
  /v1/chat/completions + stream:true + tools —— 这些名字全是 mode:chat，正路靠 LiteLLM 的
  chat→responses 桥，直 POST /v1/responses 是打错端点（2026-09-01 那次假红的根因）。
  **body 绝不带 reasoning_effort**：真 Cursor 不发它，且配置(litellm_params)已经提供；
  探针补配置本该提供的字段 = 把红构造性屏蔽掉。

验收走临时真 key（/key/generate → 打暗号 → /key/delete）：
  master key 绕过 per-key gate 与 api_key gate，用它测必假绿。

阳性对照是硬门：--control 指的那个名字（已知好）必须绿；它红 = 尺子坏了，直接退出 2，
不许拿这把尺子去判别的 lane（那种红最危险，下一步会去改产品迎合断言）。

用法（在 198 上）：
  python3 lane_live_probe.py --control cr-g-5.6-82 \
      cursor-g-81-5.6-sol cursor-g-83-5.6-sol cursor-g-84-5.6-sol cursor-g-85-5.6-sol
退出码：0 全绿 / 1 有 lane 不活 / 2 阳性对照没复现（尺子坏了）
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

import os
# 198 宿主机连得到 svc ClusterIP（127.0.0.1:4000 是不通的，host 上没有 proxy 监听）
PROXY = os.environ.get("ZK_PROXY", "http://10.43.149.225:4000")

# 一个工具定义就够撑住"带 tools"的形状；真 Cursor 发 19 个，形状同类。
TOOLS = [{
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


def master_key():
    """从 k8s secret 读，不落盘、不过 argv、不进日志。"""
    r = subprocess.run(
        ["sudo", "-n", "kubectl", "-n", "litellm-product", "get", "secret",
         "litellm-secrets", "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"],
        capture_output=True, text=True, check=True)
    import base64
    return base64.b64decode(r.stdout.strip()).decode().strip()


def api(path, payload, key, timeout=60):
    req = urllib.request.Request(
        PROXY + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def probe(model, key, nonce, timeout=120):
    """一发 chat+stream+tools。返回 (ok, 说明, 秒数, 收到的字符数)。"""
    body = {
        "model": model,
        "stream": True,
        "tools": TOOLS,
        "messages": [{"role": "user",
                      "content": "请原样输出这一行，不要加任何别的字：" + nonce}],
    }
    req = urllib.request.Request(
        PROXY + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.time()
    got = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False, "HTTP %d" % r.status, time.time() - t0, 0
            for raw in r:
                line = raw.decode("utf8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                d = line[6:]
                if d == "[DONE]":
                    break
                try:
                    ev = json.loads(d)
                except Exception:
                    continue
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        got.append(piece)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:120]), time.time() - t0, 0
    text = "".join(got)
    dt = time.time() - t0
    if nonce in text:
        return True, "暗号回显", dt, len(text)
    if not text:
        # 空回显一律读作"我没收割到"，不甩给模型/账号 —— 但对活体判据来说仍是不通过。
        return False, "零正文（未收割到，需看 pod 原始帧）", dt, 0
    return False, "有正文 %d 字但无暗号" % len(text), dt, len(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True, help="已知好的模型名，阳性对照")
    ap.add_argument("models", nargs="+")
    a = ap.parse_args()

    mk = master_key()
    alias = "lane-live-probe-%d" % int(time.time())
    names = [a.control] + list(a.models)
    k = api("/key/generate", {"models": names, "key_alias": alias,
                              "duration": "30m"}, mk)["key"]
    print("临时 key 已建 alias=%s bearer_len=%d models=%d" % (alias, len(k), len(names)))
    results = []
    try:
        for i, m in enumerate(names):
            nonce = "ZKLP-%d-%d" % (int(time.time()), i)
            ok, why, dt, n = probe(m, k, nonce)
            tag = "\033[32m✅\033[0m" if ok else "\033[31m❌\033[0m"
            role = "[阳性对照]" if m == a.control else ""
            print("%s %-26s %6.1fs  %s %s" % (tag, m, dt, why, role), flush=True)
            results.append((m, ok))
    finally:
        try:
            api("/key/delete", {"keys": [k]}, mk)
            print("临时 key 已删")
        except Exception as e:
            print("⚠️ 临时 key 删除失败，手动清: alias=%s (%s)" % (alias, e))

    ctrl_ok = dict(results)[a.control]
    if not ctrl_ok:
        print("\n❌ 阳性对照没复现已知的绿 —— 尺子坏了，这一轮的红全部作废，不许据此判别的 lane。")
        return 2
    dead = [m for m, ok in results if not ok and m != a.control]
    if dead:
        print("\n❌ 这些 lane 此刻不活：" + ", ".join(dead) + "\n   → 先修（--live-from-ws 灌 WS 线活 token），修不好就剔除出这一轮，如实减腿。")
        return 1
    print("\n✅ 全部活着（阳性对照已复现）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
