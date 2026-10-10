#!/usr/bin/env python3
"""198 deepseek-v4-flash 的 reasoning effort 档位矩阵探针（复现 + 验收同一把尺子）。

现象（待复现）：Codex 只会发标准 `reasoning.effort`，打
`local-deepseek-v4-flash-responses` 时 max/xhigh 返 400，low/high 正常；而同一
内部服务的 `local-deepseek-v4-flash`（mode=chat，LiteLLM 转译）四档全成功。

**在 198 的生产 lane pod 内执行**（读 env LITELLM_MASTER_KEY，打 localhost:4000）:

    ssh cltx@10.68.13.198
    POD=$(sudo kubectl -n litellm-product get pods \
            -l carher.net/litellm-production-route=enabled \
            -o jsonpath='{.items[0].metadata.name}')
    sudo kubectl -n litellm-product cp /tmp/effort.py $POD:/tmp/effort.py
    sudo kubectl -n litellm-product exec $POD -- python3 /tmp/effort.py

判据：只认 HTTP 状态 + 上游错误原文。**不看「组名」**，落点看响应头
`x-litellm-model-id`（组名会撒谎，见 feedback_litellm_group_name_lies_judge_by_api_base）。

三种写法各自量的是不同的一段路：
  * Responses + `reasoning.effort`      —— Codex 真实发的形状（唯一有用户面意义的一格）
  * Responses + `output_config.effort`  —— 2026 新 schema，证明上游认不认这套
  * Chat     + `reasoning_effort`       —— 绕开 Responses 适配，量「上游档位支持度」本身

⚠️ 阴性对照必须有：一个**不存在的档位值**（`bogus`）。如果它也 200，说明这条腿
根本没在校验 effort，那么 max/xhigh 的 400 就不是「档位不支持」而是别的东西。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:4000"
MASTER = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("MASTER_KEY") or ""

GROUPS = [
    "local-deepseek-v4-flash-responses",
    "local-deepseek-v4-flash",
]
EFFORTS = ["low", "medium", "high", "max", "xhigh"]
NEGATIVE = "bogus-effort-xyz"  # 阴性对照：不存在的档位

PROMPT = "Reply with the single word: ok"


def http(method, path, body, timeout=120):
    headers = {"Authorization": "Bearer " + MASTER, "Content-Type": "application/json"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — 传输层错误要显式区分于业务 4xx
        return -1, {}, "TRANSPORT %s: %s" % (type(e).__name__, e)


def brief(status, headers, text, limit=220):
    mid = headers.get("x-litellm-model-id") or headers.get("X-Litellm-Model-Id") or "-"
    if status == 200:
        return "200 landed=%s" % mid
    try:
        d = json.loads(text)
        msg = d.get("error", {}).get("message") or json.dumps(d)[:limit]
    except Exception:  # noqa: BLE001
        msg = text[:limit]
    msg = " ".join(str(msg).split())
    return "%s landed=%s %s" % (status, mid, msg[:limit])


def probe_responses_reasoning(group, effort):
    return http("POST", "/v1/responses", {
        "model": group,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
        "reasoning": {"effort": effort},
        "max_output_tokens": 64,
        "stream": False,
    })


def probe_responses_output_config(group, effort):
    return http("POST", "/v1/responses", {
        "model": group,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
        "output_config": {"effort": effort},
        "max_output_tokens": 64,
        "stream": False,
    })


def probe_chat(group, effort):
    return http("POST", "/v1/chat/completions", {
        "model": group,
        "messages": [{"role": "user", "content": PROMPT}],
        "reasoning_effort": effort,
        "max_tokens": 64,
        "stream": False,
    })


SHAPES = [
    ("responses/reasoning.effort", probe_responses_reasoning),
    ("responses/output_config.effort", probe_responses_output_config),
    ("chat/reasoning_effort", probe_chat),
]


def main():
    if not MASTER:
        print("FATAL: LITELLM_MASTER_KEY 不在 env 里，拒绝猜")
        return 2
    only = sys.argv[1] if len(sys.argv) > 1 else None
    rows = []
    for group in GROUPS:
        for shape, fn in SHAPES:
            if only and only not in shape:
                continue
            for effort in EFFORTS + [NEGATIVE]:
                st, hd, tx = fn(group, effort)
                line = "%-34s %-31s %-16s %s" % (group, shape, effort, brief(st, hd, tx))
                print(line, flush=True)
                rows.append({"group": group, "shape": shape, "effort": effort,
                             "status": st, "landed": hd.get("x-litellm-model-id"),
                             "body": tx[:600]})
    print("\n=== JSON ===")
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
