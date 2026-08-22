#!/usr/bin/env python3
"""
acct pod 内 WS 增量 canary 探针（隔离，直打 localhost:4000，不经外层路由）。
两轮同 pck：T1 全量 → T2 只应发追问+previous_response_id。
T2 emulate 外层 normalize：剥掉回放历史里的 encrypted/reasoning 项（=acct pod 生产实际入参形态），
否则前缀比对会因 reasoning 项错位而全量回放，得出假阴性。
验证看 pod stdout 的 ws_incr 帧级日志 + 答案正确性，非仅响应码。
"""
import json, os, sys, time, httpx

BASE = os.environ.get("PROBE_URL", "http://127.0.0.1:4000/v1/responses")
KEY = os.environ["LITELLM_MASTER_KEY"]
MODEL = os.environ.get("PROBE_MODEL", "chatgpt-gpt-5.6-sol")
PCK = "wsprobe-%d" % int(time.time())


def _has_enc(o):
    if isinstance(o, dict):
        if "encrypted_content" in o:
            return True
        return any(_has_enc(v) for v in o.values())
    if isinstance(o, list):
        return any(_has_enc(v) for v in o)
    return False


def strip_enc(items):
    out = []
    for it in items:
        if it.get("type") in ("reasoning", "compaction"):
            continue
        if _has_enc(it):
            continue
        out.append(it)
    return out


def turn(inp, label):
    body = {
        "model": MODEL, "input": inp, "stream": True, "store": False,
        "prompt_cache_key": PCK,
        "instructions": "You are a calculator. Show no work; reply with just the final number.",
        "reasoning": {"effort": "high"},
        "include": ["reasoning.encrypted_content"],
    }
    out_items = []; rid = None; text = []; status = None; enc = 0; usage = None
    with httpx.stream("POST", BASE,
                      headers={"Authorization": "Bearer " + KEY,
                               "Content-Type": "application/json"},
                      json=body, timeout=120) as r:
        status = r.status_code
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                line = line[6:]
            if line.strip() == "[DONE]":
                break
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("type", "")
            if et == "response.output_item.done":
                # 输出项在流式 done 事件里（store:false 下 completed.output 恒空）。
                it = ev.get("item")
                if it is not None:
                    out_items.append(it)
            elif et == "response.completed":
                resp = ev.get("response", {})
                rid = resp.get("id")
                usage = resp.get("usage")
                # 跨版本兜底：completed.output 若非空并入。
                if resp.get("output"):
                    out_items = resp.get("output")
            elif et == "response.output_text.delta":
                text.append(ev.get("delta", ""))
    enc = sum(1 for it in out_items if _has_enc(it))
    u = usage or {}
    print("%s HTTP=%s rid=%s answer=%r out_items=%d enc_items=%d in_tok=%s cached_tok=%s out_tok=%s"
          % (label, status, (rid or "")[:24], "".join(text)[:60], len(out_items), enc,
             u.get("input_tokens"),
             (u.get("input_tokens_details") or {}).get("cached_tokens"),
             u.get("output_tokens")))
    return rid, out_items


def main():
    print("PCK=%s MODEL=%s BASE=%s" % (PCK, MODEL, BASE))
    u1 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "What is 17*3+4?"}]}
    rid1, out1 = turn([u1], "T1")
    if not rid1:
        print("T1 FAILED (no response id) — account dead / bucket full? aborting")
        sys.exit(2)
    hist = strip_enc(out1)
    print("T2 replay history items (post-normalize emulation) = %d" % len(hist))
    u2 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Now multiply that result by 2."}]}
    rid2, out2 = turn([u1] + hist + [u2], "T2")
    print("DONE PCK=%s rid1=%s rid2=%s" % (PCK, rid1, rid2))


if __name__ == "__main__":
    main()
