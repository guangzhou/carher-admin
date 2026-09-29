#!/usr/bin/env python3
"""验 budget_notice ① /查余额 + ⑤ /测模型 在**真 bot 的消息形状**下有没有短路。

为什么要单独一个脚本（`litellm-198-budget-notice-smoke.sh` 的 S7 不够）：
那边发的是 `messages=[{"role":"user","content":"/查余额"}]` —— 单条。生产
openclaw 一轮发的是**尾部连续三条 role=user**（`<turn_aborted>` / 正文 / 纯运行时
上下文块），命令在**倒数第二条**。2026-09-28 carher-75 就是这么漏的：旧代码只看最后
一条，命令永远看不见，当普通聊天放过去，烧掉 `114k in / 39.7s` 一整轮 agent 推理。
单条形状测不出这个 bug，而且一直是绿的 —— 结构性盲区。

🔴 判据是 `in_tok`，不是文案。短路走 mock_response，`in_tok` 恒个位数（实测 10）；
真打上游是几百到十几万。只看「有没有那段文案」分不出「短路了」和「模型正好把余额
转述对了」—— 后者正是这次的故障现象。

用法（**必须用真 bot 的 key，别新建**，见 skill litellm-budget-mgmt）：

    # 在 188 上从 bot 容器里取 key（不落盘、不回显）
    ssh cltx@10.68.13.188 'docker exec hermestest-14 env | grep -q LITELLM_API_KEY && echo ok'

    # 本地/198 上跑，key 走 env
    PROBE_KEY=sk-... python3 scripts/litellm-198-quota-cmd-openclaw-shape-probe.py \
        --base http://10.68.13.198:30402/pro --model her-flash

    # 或直接在 bot 容器里跑（key 已在容器 env 里，这样明文不出容器）
    #   见 skill 里的 docker exec -i + base64 上传法

⚠️ `--model` 必须是**这把 key 白名单里真有的名**，否则 403 key_model_access_denied，
且阴性对照会**一起红** —— 那是尺子坏，不是短路坏。查真名：
    select unnest(models) from "LiteLLM_VerificationToken" where key_alias='carher-14';

退出码：0 全绿 / 1 有 FAIL / 2 尺子自己坏（连不上、全 4xx）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 实测抓下来的生产形状（carher-75，SpendLogs 2026-09-28 06:28:59 的 proxy_server_request）。
# 这三条的**顺序和角色**是本脚本唯一的被测对象，改它就不是在测这个 bug 了。
_ABORTED = ("[Mon 2026-09-28 13:42 GMT+8] <turn_aborted>\n"
            "The previous turn was interrupted.")
_RUNTIME_CTX = (
    "OpenClaw runtime context for the active user request in this turn.\n"
    "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n"
    'Conversation info: {"was_mentioned":true}\n'
    "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>")


def _body(cmd: str) -> str:
    """正文：命令独占最后一行，前面堆一堆运行时注入的上下文（照生产抄）。"""
    return ("[Mon 2026-09-28 14:28 GMT+8] Conversation info: ...\n"
            "[CONTEXT ANCHOR v5]\nSELF: app_id=cli_a94a0b73a878dbcb\n"
            "[END CONTEXT ANCHOR v5]\n\n"
            "[群聊模式: group-at]\n\n" + cmd)


def _messages(cmd: str) -> list:
    return [
        {"role": "user", "content": "老问题"},
        {"role": "assistant", "content": "老答案"},   # 上一轮边界，必须在
        {"role": "user", "content": _ABORTED},        # 尾部 user #3
        {"role": "user", "content": _body(cmd)},      # 尾部 user #2 ← 命令在这
        {"role": "user", "content": _RUNTIME_CTX},    # 尾部 user #1（无命令）
    ]


def shoot(base, key, model, cmd, timeout=180):
    payload = {"model": model, "max_tokens": 64, "messages": _messages(cmd)}
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
        code = 200
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "text": "", "in_tok": None,
                "el": time.time() - t0, "raw": exc.read()[:300].decode()}
    except Exception as exc:                                  # noqa: BLE001
        return {"code": 0, "text": "", "in_tok": None,
                "el": time.time() - t0, "raw": repr(exc)}
    el = time.time() - t0
    try:
        j = json.loads(raw)
    except ValueError:
        return {"code": code, "text": "", "in_tok": None, "el": el,
                "raw": raw[:300]}
    usage = j.get("usage") or {}
    msg = ((j.get("choices") or [{}])[0].get("message") or {})
    return {"code": code, "text": msg.get("content") or "",
            "in_tok": usage.get("prompt_tokens"), "el": el, "raw": raw[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:30402/pro")
    ap.add_argument("--model", required=True,
                    help="这把 key 白名单里真有的模型名（不是公开名瞎猜）")
    ap.add_argument("--max-shortcircuit-in-tok", type=int, default=64,
                    help="短路的 in_tok 上界。实测 10；给到 64 留余量")
    ap.add_argument("--max-shortcircuit-sec", type=float, default=5.0)
    args = ap.parse_args()

    key = os.environ.get("PROBE_KEY") or ""
    if not key.startswith("sk-"):
        print("需要 env PROBE_KEY=<真 bot 的 key>（别新建 key，见 skill）",
              file=sys.stderr)
        return 2

    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + name
              + ("" if cond else " :: " + detail[:400]), flush=True)

    def show(tag, r):
        print("  HTTP %s · %.2fs · in_tok=%s" % (r["code"], r["el"], r["in_tok"]))
        body = r["text"] or r["raw"]
        print("  " + body[:300].replace("\n", "\n  "))

    print("=" * 66)
    print("① /查余额 —— 命令在倒数第二条 user，必须短路（零上游）")
    a = shoot(args.base, key, args.model, "/查余额")
    show("quota", a)
    check("Q1 /查余额 短路",
          a["code"] == 200 and "今日用量" in a["text"]
          and (a["in_tok"] or 10 ** 9) <= args.max_shortcircuit_in_tok
          and a["el"] < args.max_shortcircuit_sec,
          "want 200 + 含今日用量 + in_tok<=%d + <%.0fs; got %s"
          % (args.max_shortcircuit_in_tok, args.max_shortcircuit_sec, a))

    print("=" * 66)
    print("② 阴性对照 —— 同形状的普通问题，必须**不**短路")
    b = shoot(args.base, key, args.model, "今天天气怎么样")
    show("normal", b)
    check("Q2 阴性对照不短路",
          b["code"] == 200 and "今日用量" not in b["text"],
          "want 200 + 不含今日用量; got %s" % b)

    print("=" * 66)
    print("③ /测模型 —— 同位置，必须短路进探针（会真打上游，慢是正常的）")
    c = shoot(args.base, key, args.model, "/测模型")
    show("probe", c)
    check("Q3 /测模型 短路",
          c["code"] == 200 and ("能用" in c["text"] or "未测" in c["text"]),
          "want 200 + 含能用/未测; got %s" % c)

    print()
    print("%d/%d PASS" % (sum(results), len(results)))

    # 尺子自己坏：三条一起非 200（尤其阴性对照也红）⇒ 别读成「短路坏了」。
    if all(r["code"] != 200 for r in (a, b, c)):
        print("🔴 三条全非 200（含阴性对照）= 尺子坏，不是短路坏。"
              "先查 --model 在不在这把 key 白名单里、key 是不是真 bot 的。",
              file=sys.stderr)
        return 2
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
