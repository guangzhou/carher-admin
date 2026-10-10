#!/usr/bin/env python3
"""探针：飞书装机文档要新增的那几个名字，在 **Cursor 的调用形状** 下能不能用。

为什么要单独一把尺子（而不是复用 litellm-198-grok6-cursor-probe.py）：
那把探针按模型自己的 mode 挑端点（image 名走 `/v1/images/generations`）。
可 Cursor 只会走 `/v1/chat/completions`（BYOK OpenAI 兼容路径），**而且带
`stream:true` + `tools`**。所以「模型在 LiteLLM 里活着」不等于「进了 Cursor 菜单
能用」—— 一个 `mode=image_generation` 的名字写进菜单，同事点一下就是 400。
本探针量的就是后者：逐名打 Cursor 线型，200 且出字才算能进菜单。

判据纪律（沿用同一套，坑都踩过）：
- **禁 master key**：master 不过 per-key 白名单闸门，必然假绿。复刻一把真 cursor key
  的 `models` + `aliases` 形状建临时 key，跑完删掉。
- **阴性对照**：编一个不存在的名字同发一遍，读数必须和阳性不同；同读数 = 这轮零分辨力。
- **每发唯一 nonce**：响应缓存是开的，逐字节相同的请求到不了上游。

用法（在 198 上跑）：
    # ① 从 DB 取一把真 cursor key 的形状（⛔ 不能用 /key/info?key_alias=，
    #    那个 filter 不存在，返 404 会被误读成"这把 key 没了"）
    sudo kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm \
      -t -A -c "select json_build_object('models',models,'aliases',aliases)
                from \\"LiteLLM_VerificationToken\\"
                where key_alias='cursor-efae1ca4-unro'" > /tmp/shape.json
    # ② 打探针
    LITELLM_MASTER_KEY=... python3 litellm-198-cursor-newnames-probe.py \
        --clone-file /tmp/shape.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# 用户点名要进文档的名字。
# ⚠️ 2026-09-20 当天从 6 个减到 5 个:`sa-grok-imagine` 被这把探针判红(chat 线型恒 400,
#    它是 `mode=image_generation`,只有 /v1/images/generations 才 200),当天就从菜单下架、
#    进了装机器的 RETIRED_MODELS。**别再加回这个清单** —— 退役名同时出现在"待探清单"里
#    会让读数方向自相矛盾(门在 test_retired_name_is_not_in_the_probe_list)。
#    这条注释就是那次探测的结论本身,删掉它下一个人会照着文档再加一遍。
NAMES = (
    "sa-composer-2.5-fast",
    "sa-grok-4.20-0309-reasoning",
    "sa-grok-4.6-latest",
    "sa-grok-4.5-latest",
    "qwen3-coder-next",
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]


def call(base, path, payload, key, timeout=240, stream=False):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": "Bearer " + key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        if stream:
            return r.status, r.read().decode("utf8", "replace"), dict(r.headers)
        return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "replace")[:400], dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - 传输层断了也要看得见，禁 2>/dev/null
        return -1, "%s: %s" % (type(e).__name__, str(e)[:200]), {}


def sse_text(body: str) -> str:
    """把 SSE 流里的 content delta 拼起来。裸流式没有 usage 是协议规定，不是故障。"""
    out = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        chunk = line[6:].strip()
        if chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                out.append(delta["content"])
    return "".join(out)


def main() -> int:
    global NAMES  # noqa: PLW0603 —— 见 --names，门禁钉住了 `for n in NAMES:` 这行字面量
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:30402")
    ap.add_argument("--clone-file",
                    help="DB 导出的 {'models':[...],'aliases':{...}}（见文件头用法）；"
                         "用 --key 直打一把真 key 时不需要它")
    # ── 2026-09-21 加的两个口子。为什么是"放开参数"而不是另写一把探针：
    #    手搓 body 必漏 `stream`+`tools`，那样连已证绿的名字也会全红
    #    （记忆 feedback_dont_rewrite_a_ruler_to_bypass_a_param_check）。
    ap.add_argument("--key",
                    help="直接用这把真 key 打（**不建临时 key、不需要 master**）。"
                         "⚠️ 读数因此是「**这把 key** 能不能用这个名」，"
                         "不是「网关里有没有这个名」—— 403 是授权信号。"
                         "⛔ 绝不能传 master key：master 不过 per-key 白名单闸门，必然假绿。")
    ap.add_argument("--names",
                    help="逗号分隔，覆盖默认的 NAMES（默认那 5 个是 09-20 点名的新增名）。"
                         "拿装机包 DEFAULT_MODELS 做全量回归时用它。")
    args = ap.parse_args()

    if args.names:
        NAMES = tuple(x.strip() for x in args.names.split(",") if x.strip())
        if not NAMES:
            print("--names 解析出空清单", file=sys.stderr)
            return 2

    nonce = "%d-%d" % (time.time(), random.randint(1000, 9999))

    if args.key:
        if args.key == os.environ.get("LITELLM_MASTER_KEY"):
            print("--key 传的是 master key —— master 不过 per-key 白名单闸门，"
                  "这轮必然假绿。换一把受限 key。", file=sys.stderr)
            return 2
        probe = args.key
        cleanup_with = None
        print("direct_key=%s...%s names=%d nonce=%s"
              % (args.key[:6], args.key[-4:], len(NAMES), nonce))
    else:
        master = os.environ.get("LITELLM_MASTER_KEY")
        if not master:
            print("LITELLM_MASTER_KEY 未设置", file=sys.stderr)
            return 2
        if not args.clone_file:
            print("要么给 --clone-file（克隆一把真 key 的形状），要么给 --key（直打真 key）",
                  file=sys.stderr)
            return 2

        row = json.load(open(args.clone_file))
        models = list(row.get("models") or [])
        aliases = dict(row.get("aliases") or {})
        if not models:
            print("形状文件里 models 是空的 —— 空 models 在 LiteLLM 里意思是**全放行**，"
                  "拿它当探针等于绕开白名单闸门，必然假绿。换一把受限 key。", file=sys.stderr)
            return 2
        for n in NAMES:
            if n not in models:
                models.append(n)

        print("clone_file=%s models=%d aliases=%d nonce=%s"
              % (args.clone_file, len(models), len(aliases), nonce))

        st, gen, _ = call(args.base, "/key/generate",
                          {"key_alias": "zz-probe-newnames-" + nonce, "models": models,
                           "aliases": aliases, "duration": "20m"}, master)
        if st != 200:
            print("key/generate 失败 %s %s" % (st, gen))
            return 1
        probe = gen["key"]
        cleanup_with = master

    bad = []
    try:
        for n in NAMES:
            st, body, h = call(args.base, "/v1/chat/completions", {
                "model": n, "stream": True, "tools": TOOLS,
                "messages": [{"role": "user", "content":
                              "nonce %s: reply with the single word OK" % nonce}],
                "max_tokens": 32}, probe, stream=True)
            txt = sse_text(body) if st == 200 else str(body)
            got = bool(txt.strip())
            print("  CURSOR-SHAPE %-30s %s mid=%-28s out=%r" %
                  (n, st, h.get("x-litellm-model-id"), txt[:50]))
            if st != 200 or not got:
                bad.append(n)

        st, body, _ = call(args.base, "/v1/chat/completions", {
            "model": "sa-does-not-exist-" + nonce, "stream": True,
            "messages": [{"role": "user", "content": "x"}], "max_tokens": 8}, probe, stream=True)
        print("  NEG          %-30s %s %s" % ("(bogus name)", st, str(body)[:90]))
        if st == 200:
            print("RULER-BROKEN: 不存在的名字也 200，这轮读数作废")
            return 3
    finally:
        # 🔴 只删**我们自己建的**那把临时 key。--key 直打模式下 probe 是用户的真 key,
        #    删它 = 把人家 Cursor 打死。cleanup_with=None 就是那个分辨位。
        if cleanup_with is None:
            print("  cleanup 跳过（--key 是调用方的真 key，不是我们建的，绝不能删）")
        else:
            st, body, _ = call(args.base, "/key/delete", {"keys": [probe]}, cleanup_with)
            print("  cleanup /key/delete -> %s %s" % (st, str(body)[:80]))

    print("PROBE %s  ok=%d/%d  不能进菜单的=%s"
          % ("GREEN" if not bad else "RED", len(NAMES) - len(bad), len(NAMES), bad or "无"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
