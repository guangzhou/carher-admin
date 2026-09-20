#!/usr/bin/env python3
"""逐号真发一句 `hi`：判 kiro 账号「能不能出字」，而不是「目录里有没有」。

为什么需要第三个脚本
--------------------
`kiro-probe.py` 的两把尺子都**不是**"能服务"的证据：

- `catalog`（`ListAvailableModels`）证明的是 **entitlement + token 能刷**，
  它是"这个号有没有某模型"的权威判据，但**不发推理**。
- `quota`（`getUsageLimits`）更弱：🔴 **封停号照样报余额**
  （09-17 实测某封停号还报 33.66 credits）⇒ credit 是判死活的坏尺子。

而打 kiro.rs 自己那 18 条道只能量到 `currentId` 那**一个**号：
🔴 **打池子判个体死活 = 假绿**。备用号 succ=0 说明它从来没被真正调用过，
"池子绿"完全不包含"id5 能服务"这条信息。

⇒ 本脚本**绕过池子，逐号直打上游** `generateAssistantResponse`，
判据是**响应里真有 assistant 文本**，不是 HTTP 200。

请求形状的来源（抄的，不是猜的）
--------------------------------
`188:/Data/kiro-build/kiro.rs/src/`：

| 项 | 源码位置 |
|---|---|
| URL `https://q.<region>.amazonaws.com/generateAssistantResponse` | `kiro/endpoint/ide.rs:64` |
| header（optout / agent-mode: vibe / KiroIDE-<ver>-<machineId>） | `kiro/endpoint/ide.rs:decorate_api` |
| `content-type: application/json` | `kiro/provider.rs:172` |
| body `conversationState.currentMessage.userInputMessage` | `kiro/model/requests/conversation.rs` |
| `agentTaskType:"vibe"` / `chatTriggerType:"MANUAL"` / `origin:"AI_EDITOR"` | `anthropic/converter.rs:368-380` |

⚠️ `chatTriggerType` 必须是 `MANUAL`——源码注释写明 `AUTO` 会 400
（`converter.rs:determine_chat_trigger_type`）。

坑（都是踩过或源码里写着的）
----------------------------
- **响应是 AWS `vnd.amazon.eventstream` 二进制帧**，不是 SSE、不是 JSON。
  本脚本按帧头（total_len / headers_len / prelude_crc）严格切帧再解 payload，
  ⛔ 不用 `re.findall('"content":"..."')` 那种正则——它会把 `:exception` 帧里的
  错误文案也当成模型输出，**把红的读成绿的**。
- **默认模型 `claude-haiku-4.5`（0.4x）**：这是最便宜的 Claude 道，一次 ping
  约 0.4 credit 量级。⚠️ **它只证明"这个号的 Claude 通道能出字"**，
  不等于 opus-5 也正常（倍率/上下文不同道）。要判 opus-5 用 `--model claude-opus-5`。
- 🔴 **refreshToken 若被上游轮转，本脚本只告警、不回写盘。**
  `token_manager.rs:305` 是 kiro.rs 自己在 refresh 后回写 PVC 的；探针类脚本回写
  会和线上抢写同一个文件。⇒ 真轮转了就让 kiro.rs 自己 refresh 一次去落盘，
  别用这里读到的新 RT 手改 `credentials.json`。
- **只读**：不改 `disabled`/`priority`、不删号。下线走 `kiro-pool.py remove <id>`
  （它自带备份 → disable → 复核 → DELETE），本脚本最后会把该发的命令打出来。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROBE = Path(__file__).with_name("kiro-probe.py")
KIRO_IDE_VER = "0.11.107"          # config.rs:default_kiro_version（线上未覆盖）
NODE_VER = "22.22.0"               # config.rs:default_node_version
OS_VER = "darwin#24.6.0"           # config.rs:default_system_version 的第一个
OIDC = "https://oidc.%s.amazonaws.com/token"


def load_probe():
    """复用 kiro-probe.py 的池子/出口/machineId 逻辑 —— 只有一份真身。

    文件名带横线不能 import，用 importlib 按路径加载。
    """
    spec = importlib.util.spec_from_file_location("kiro_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- eventstream
def parse_eventstream(buf: bytes) -> list[tuple[dict, bytes]]:
    """切 AWS vnd.amazon.eventstream 帧，返回 [(headers, payload), …]。

    帧格式：total_len(4) headers_len(4) prelude_crc(4) headers payload msg_crc(4)
    header 项：name_len(1) name value_type(1) [value_len(2) value]
    只解 type 7（string）——事件帧的 :event-type/:message-type 都是 string。
    """
    out, off = [], 0
    while off + 12 <= len(buf):
        total, hlen = struct.unpack_from(">II", buf, off)
        if total < 16 or off + total > len(buf):
            break
        hbuf = buf[off + 12: off + 12 + hlen]
        payload = buf[off + 12 + hlen: off + total - 4]
        headers, p = {}, 0
        while p < len(hbuf):
            nlen = hbuf[p]; p += 1
            name = hbuf[p:p + nlen].decode("utf8", "replace"); p += nlen
            vtype = hbuf[p]; p += 1
            if vtype == 7:
                vlen = struct.unpack_from(">H", hbuf, p)[0]; p += 2
                headers[name] = hbuf[p:p + vlen].decode("utf8", "replace"); p += vlen
            else:                      # 本端点只出现 string 头；其余形状直接停
                break
        out.append((headers, payload))
        off += total
    return out


def read_reply(buf: bytes) -> tuple[str, list[str]]:
    """从帧流里抽出 assistant 文本与异常。

    判据只认 `assistantResponseEvent` 帧的 `content`；
    `:message-type == exception` 或带 `:exception-type` 的帧计为错误。
    ⇒ 上游把 400/限流塞在 200 响应体里时不会被读成绿。
    """
    text, errs = [], []
    for h, payload in parse_eventstream(buf):
        etype = h.get(":event-type") or h.get(":exception-type") or ""
        try:
            obj = json.loads(payload.decode("utf8", "replace")) if payload else {}
        except json.JSONDecodeError:
            obj = {"_raw": payload[:200].decode("utf8", "replace")}
        if h.get(":message-type") == "exception" or h.get(":exception-type"):
            errs.append(f"{etype}: {json.dumps(obj, ensure_ascii=False)[:300]}")
        elif etype == "assistantResponseEvent":
            text.append(obj.get("content", ""))
    return "".join(text), errs


# ---------------------------------------------------------------------- ping
def oidc_refresh(creds: dict, opener, region: str) -> dict:
    """自己刷一次，为的是**看见响应里的 refreshToken**（probe 那个只回 accessToken）。

    RT 真被轮转时要当场告警：盘上那份就成了旧快照。
    """
    body = json.dumps({
        "clientId": creds["clientId"],
        "clientSecret": creds["clientSecret"],
        "grantType": "refresh_token",
        "refreshToken": creds["refreshToken"],
    }).encode()
    req = urllib.request.Request(OIDC % region, data=body,
                                 headers={"content-type": "application/json"})
    return json.load(opener.open(req, timeout=60))


def ping(creds: dict, region: str, model: str, prompt: str, opener, probe) -> dict:
    """对一个账号发一次真推理。返回判定 dict（不抛，让整池都能跑完）。"""
    r: dict = {"id": creds.get("id"), "email": creds.get("email", "?"),
               "ok": False, "why": "", "text": "", "ms": 0, "rotated": False}
    try:
        tok = oidc_refresh(creds, opener, region)
        if tok.get("refreshToken") and tok["refreshToken"] != creds["refreshToken"]:
            r["rotated"] = True          # 见模块 docstring：只告警，不回写
        access = tok["accessToken"]
    except urllib.error.HTTPError as e:
        r["why"] = f"OIDC refresh HTTP {e.code}: {e.read().decode('utf8','replace')[:200]}"
        return r
    except Exception as e:                                   # noqa: BLE001
        r["why"] = f"OIDC refresh 失败: {e}"
        return r

    mid = probe.machine_id_of(creds)
    ua = f"KiroIDE-{KIRO_IDE_VER}-{mid}"
    body = json.dumps({"conversationState": {
        "agentContinuationId": str(uuid.uuid4()),
        "agentTaskType": "vibe",
        "chatTriggerType": "MANUAL",          # AUTO 会 400，见 converter.rs
        "conversationId": str(uuid.uuid4()),
        "currentMessage": {"userInputMessage": {
            "userInputMessageContext": {},
            "content": prompt,
            "modelId": model,
            "origin": "AI_EDITOR",
        }},
    }}).encode()
    req = urllib.request.Request(
        f"https://q.{region}.amazonaws.com/generateAssistantResponse",
        data=body,
        headers={
            "Authorization": "Bearer " + access,
            "content-type": "application/json",
            "x-amzn-codewhisperer-optout": "true",
            "x-amzn-kiro-agent-mode": "vibe",
            "x-amz-user-agent": f"aws-sdk-js/1.0.34 {ua}",
            "user-agent": (f"aws-sdk-js/1.0.34 ua/2.1 os/{OS_VER} lang/js "
                           f"md/nodejs#{NODE_VER} api/codewhispererstreaming#1.0.34 "
                           f"m/E {ua}"),
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=1",
            "Connection": "close",
        })
    t0 = time.time()
    try:
        raw = opener.open(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        r["ms"] = int((time.time() - t0) * 1000)
        r["why"] = f"HTTP {e.code}: {e.read().decode('utf8','replace')[:300]}"
        return r
    except Exception as e:                                   # noqa: BLE001
        r["ms"] = int((time.time() - t0) * 1000)
        r["why"] = f"{type(e).__name__}: {e}"
        return r
    r["ms"] = int((time.time() - t0) * 1000)
    text, errs = read_reply(raw)
    r["text"] = text.strip()
    if errs:                        # 200 里夹异常帧 —— 不许读成绿
        r["why"] = "响应含异常帧 | " + " | ".join(errs)
    elif not r["text"]:
        r["why"] = f"200 但没有 assistant 文本（{len(raw)} bytes）"
    else:
        r["ok"] = True
    return r


def main():
    ap = argparse.ArgumentParser(
        description="逐号真发一句 hi，判 kiro 账号能不能服务（只读，绕过池子直打上游）")
    ap.add_argument("--model", default="claude-haiku-4.5",
                    help="默认 claude-haiku-4.5（0.4x，最便宜的 Claude 道）。"
                         "判 opus-5 用 --model claude-opus-5")
    ap.add_argument("--prompt", default="hi")
    ap.add_argument("--creds", help="本地凭据文件；默认从线上 pod 现取")
    ap.add_argument("--account", help="只打这一个 id")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--no-proxy", action="store_true",
                    help="⚠️ 直连，与线上不同路，出问题别拿这个下结论")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    probe = load_probe()
    pool = probe.load_pool(a.creds)
    if a.account:
        pool = [c for c in pool if str(c.get("id")) == str(a.account)]
        if not pool:
            sys.exit(f"池子里没有 id={a.account}")
    proxy = None if a.no_proxy else probe.pod_proxy()
    opener = probe.build_opener(proxy)

    if not a.json:
        print(f"# 出口 {probe.mask(proxy)}")
        print(f"# 模型 {a.model}   提示词 {a.prompt!r}   {len(pool)} 个账号")
        print("# 判据 = 响应里真有 assistant 文本（不是 HTTP 200）\n")

    results = []
    for c in pool:
        region = c.get("apiRegion") or c.get("region") or a.region
        r = ping(dict(c), region, a.model, a.prompt, opener, probe)
        results.append(r)
        if not a.json:
            mark = "🟢 可用" if r["ok"] else "🔴 不可用"
            print(f"id={r['id']:<3} {r['email']:<32} {mark}  {r['ms']}ms")
            if r["ok"]:
                print(f"      回复: {r['text'][:120]!r}")
            else:
                print(f"      原因: {r['why']}")
            if r["rotated"]:
                print("      ⚠️ 上游轮转了 refreshToken；盘上那份已是旧快照。"
                      "让 kiro.rs 自己 refresh 落盘，别手改 credentials.json")

    bad = [r for r in results if not r["ok"]]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n== {len(results) - len(bad)}/{len(results)} 可用 ==")
        if bad:
            print("下线（备份→disable→复核→DELETE，零重启）：")
            for r in bad:
                print(f"  python3 scripts/kiro-pool.py remove {r['id']}   "
                      f"# {r['email']}")
            print("⚠️ 摘号前先确认剩下的号够用；删空池子 kiro.rs 的 currentId 归 0。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
