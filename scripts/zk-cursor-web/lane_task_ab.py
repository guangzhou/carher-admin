#!/usr/bin/env python3
"""lane_task_ab.py — 两条 lane 的「工具服从率」单变量 A/B(198 上跑)。

╔══════════════════════════════════════════════════════════════════════════════╗
║ ⚠️ 2026-09-01:这把尺子出过一次大事,用它之前先读完这一段。                    ║
║                                                                              ║
║ 它**不是门②**,红了不等于产品坏了。它当天量出「81~85 动手率只有 8~26%」,      ║
║ 我据此写补丁、上灰度、并告诉用户"大部分人吃的是坏的"——**全是假的**。用户在真 ║
║ Cursor 里 ls 一直正常。事后查出两处硬伤:                                     ║
║                                                                              ║
║  ① 端点错:下面 _run() 打的是 /v1/responses,而这些模型 /model/info 实查全是    ║
║     mode:chat —— 真 Cursor 发 /v1/chat/completions,靠 LiteLLM 的 chat→        ║
║     responses 桥才进 responses.js 正路。直打 /v1/responses = 绕过承重的桥。   ║
║     skill zk-cursor-web-fc-iterate 原文:「/v1/responses 合成探针全绿是假绿」。║
║  ② 两臂没配平:cursor-web-fc-*-terra 挂 gpt-5.6-terra 无 effort,               ║
║     cursor-g-<N>-5.6-sol 挂 gpt-5.6-sol + effort=medium。跨族比 = 三个变量    ║
║     一起变,下面 docstring 里"只有 model 名不同→只有 lane 不同"这句就不成立。  ║
║                                                                              ║
║ 用它之前必须先做的两件事:                                                    ║
║  · 查 /model/info 核对两臂的载体 slug 与 reasoning_effort **完全相同**;       ║
║  · 记住合成红与合成绿是同一把尺子——不拿绿当验收,就不许拿红当结论。          ║
║ 真判据永远是门②(真 Cursor GUI 跑 shell + 飞书建文档)。                       ║
║ 案发记录:memory feedback_synthetic_red_is_as_untrusted_as_synthetic_green    ║
╚══════════════════════════════════════════════════════════════════════════════╝

用途:某条 lane 的 env/代码与池里其余 lane 不一致时,证明「它到底比在池的老 lane 差不差」。
判据是**相对的**——不比在池的老 lane 差就算过(见 memory
feedback_new_lane_gate_must_be_relative_and_clone_from_live)。

单变量怎么保证:
  · 两臂发的是**同一份真抓包**(cap-N.json,含 instructions/tools/<mcp_server_catalog>),
    只有 model 名不同 → 只有 lane 不同。合成小 payload 测不出契约层的事。
  · **逐发交错**(A,B,A,B…)而不是先跑完 A 再跑 B。上游 thinking 时延/限速是时变的,
    顺序跑会把时间差算进 lane 差(failover_drill 就这么假绿过一次,见
    feedback_failover_drill_must_prove_bad_lane_was_actually_picked)。
  · 每发换一句带唯一标记的 user_query(避免上游会话/缓存复用把第二臂喂成增量轮)。

判据(单轮服从率,与 2026-08-27 那次 101/82 对照同口径):
  ACTED = 本轮吐出了 >=1 个可执行的 function_call。
  EMPTY = 既无正文也无 call 且流正常收口(200 装作成功)——最坏形态。
  ERR 过半 → 退出码 2(INVALID),不给相对判据:两臂都没打通时「0/4 vs 0/4」不是「一样好」。

用法:
  python3 lane_task_ab.py --arms 101=cursor-web-fc-terra 82=cursor-web-fc-82-terra --shots 6
  python3 lane_task_ab.py ... --cap /home/cltx/tooldiet/cap-6.json --tag before
临时 key 用完即删(master key 只用来 mint/delete)。
"""
import argparse, base64, json, subprocess, sys, time, urllib.request

NS = "litellm-product"
DEFAULT_CAP = "/home/cltx/tooldiet/cap-6.json"
# 真实动手任务:必须调 shell 才能回答,只说不做 = 不算 ACTED。
TASK = ("用 shell 执行 `ls -1 /tmp` 看看里面有哪些条目，然后告诉我一共几条。"
        "必须真的执行命令再回答，不要凭空猜。")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def proxy():
    ip = sh(f"kubectl -n {NS} get svc litellm-proxy -o jsonpath='{{.spec.clusterIP}}'").strip().strip("'")
    if not ip:
        sys.exit("!! 取不到 litellm-proxy clusterIP")
    return f"http://{ip}:4000"


def master_key():
    data = json.loads(sh(f"kubectl -n {NS} get secret litellm-secrets -o json"))["data"]
    return base64.b64decode(data["LITELLM_MASTER_KEY"]).decode()


def post(base, key, path, body, timeout=300):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def load_cap(path):
    cap = json.load(open(path))
    return cap["instructions"], cap["tools"], cap["input"][0]


def uq_item(text):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": f"<user_query>\n{text}\n</user_query>"}]}


FAKE_LS = ("bin\nboot\ncursor-probe.log\ndev\netc\nhome\nlib\nmedia\nmnt\nopt\n"
           "proc\nroot\nrun\nsbin\nsrv\nsys\ntmp\nusr\nvar\n__COUNT__=19\n")


def _run(base, key, model, instructions, tools, inp):
    """打一发 /v1/responses,收流,返回本轮形态。"""
    body = {"model": model, "stream": True, "tool_choice": "auto",
            "instructions": instructions, "tools": tools, "input": inp}
    r = {"text": "", "calls": 0, "call0": None, "call_id": None, "call_name": None,
         "completed": 0, "err": None}
    t0 = time.time()
    try:
        for raw in post(base, key, "/v1/responses", body):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            dd = line[5:].strip()
            if dd == "[DONE]":
                continue
            try:
                ev = json.loads(dd)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "response.output_text.delta":
                r["text"] += ev.get("delta", "")
            elif t == "response.output_item.done":
                it = ev.get("item") or {}
                if it.get("type") in ("function_call", "custom_tool_call"):
                    r["calls"] += 1
                    if r["call0"] is None:
                        r["call0"] = str(it.get("arguments") or it.get("input") or "")[:120]
                        r["args_full"] = str(it.get("arguments") or it.get("input") or "")
                        r["call_id"] = it.get("call_id") or it.get("id")
                        r["call_name"] = it.get("name") or "shell"
                elif it.get("type") == "message" and not r["text"]:
                    r["text"] += "".join(c.get("text", "") for c in (it.get("content") or [])
                                         if isinstance(c, dict))
            elif t == "response.completed":
                r["completed"] += 1
    except Exception as e:
        r["err"] = str(e)[:160]
    r["lat"] = round(time.time() - t0, 1)
    r["acted"] = r["calls"] > 0
    r["empty"] = (not r["text"].strip()) and r["calls"] == 0 and not r["err"]
    return r


def one_shot(base, key, model, instructions, tools, item0, mark, followup=False):
    """turn1 = 动手轮。followup 时再打 turn2 = 工具结果回灌轮。

    turn2 才是 proto2 与九补丁级联真正分道的地方(tool-feed 轮的契约档
    ZK_CONTRACT_DIET / ZK_DIET_EXEMPT_MINI 只在这一轮生效),所以只测 turn1
    等于没测到本次 env 改动的主要作用面。
    """
    uq = uq_item(f"{TASK}\n(probe-mark {mark}，忽略本行)")
    r1 = _run(base, key, model, instructions, tools, [item0, uq])
    out = {"t1": r1}
    if not followup or not r1["acted"]:
        return out
    # 回灌:把 turn1 那个 call 的结果喂回去,看模型是否消化并收尾(而不是空轮/重复调用)。
    inp2 = [item0, uq,
            {"type": "function_call", "call_id": r1["call_id"],
             "name": r1["call_name"], "arguments": r1["args_full"]},
            {"type": "function_call_output", "call_id": r1["call_id"], "output": FAKE_LS}]
    out["t2"] = _run(base, key, model, instructions, tools, inp2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+", required=True, help="label=modelname,至少两个")
    ap.add_argument("--shots", type=int, default=6, help="每臂发多少轮")
    ap.add_argument("--cap", default=DEFAULT_CAP)
    ap.add_argument("--tag", default="ab")
    ap.add_argument("--followup", action="store_true",
                    help="每发追打一轮工具结果回灌(turn2),测 tool-feed 路")
    a = ap.parse_args()
    arms = []
    for s in a.arms:
        if "=" not in s:
            sys.exit(f"!! --arms 要 label=model,收到 {s!r}")
        lab, mdl = s.split("=", 1)
        arms.append((lab, mdl))
    if len(arms) < 2:
        sys.exit("!! 至少两臂,单臂没有对照组")

    base = proxy()
    instructions, tools, item0 = load_cap(a.cap)
    print(f"# cap={a.cap} instructions={len(instructions)}c tools={len(tools)} "
          f"item0={len(json.dumps(item0))}c shots={a.shots}/arm tag={a.tag}", flush=True)

    mk = master_key()
    kr = json.loads(post(base, mk, "/key/generate",
                         {"models": [m for _, m in arms], "duration": "2h",
                          "key_alias": f"laneab-{a.tag}-{int(time.time())}"}).read())
    key = kr["key"]
    res = {lab: [] for lab, _ in arms}
    try:
        # 逐发交错:同一时刻的上游状况两臂各吃一发,时间差不会被算成 lane 差。
        for i in range(a.shots):
            for lab, mdl in arms:
                mark = f"{a.tag}-{lab}-{i+1}-{int(time.time()*1000) % 100000}"
                o = one_shot(base, key, mdl, instructions, tools, item0, mark,
                             followup=a.followup)
                res[lab].append(o)
                r1, r2 = o["t1"], o.get("t2")
                row = {"arm": lab, "shot": i + 1, "t1_acted": r1["acted"],
                       "t1_empty": r1["empty"], "t1_lat": r1["lat"],
                       "t1_calls": r1["calls"], "err": r1["err"], "call0": r1["call0"]}
                if r2 is not None:
                    # turn2 的合格形态 = 消化了工具结果并给正文(收尾),或继续调下一个命令。
                    row.update({"t2_text": len(r2["text"].strip()), "t2_calls": r2["calls"],
                                "t2_empty": r2["empty"], "t2_lat": r2["lat"],
                                "t2_ok": (bool(r2["text"].strip()) or r2["calls"] > 0)
                                         and not r2["err"],
                                "t2_saw19": "19" in r2["text"], "t2_err": r2["err"]})
                print(json.dumps(row, ensure_ascii=False), flush=True)
                time.sleep(2)
    finally:
        post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("# temp key deleted", flush=True)

    print("\n===== SUMMARY (%s) =====" % a.tag)
    summ = {}
    for lab, mdl in arms:
        os_ = res[lab]
        t1 = [o["t1"] for o in os_]
        t2 = [o["t2"] for o in os_ if o.get("t2") is not None]
        acted = sum(1 for r in t1 if r["acted"])
        empty = sum(1 for r in t1 if r["empty"])
        errs = sum(1 for r in t1 if r["err"])
        lats = [r["lat"] for r in t1]
        t2ok = sum(1 for r in t2 if (r["text"].strip() or r["calls"] > 0) and not r["err"])
        t2n = len(t2)
        summ[lab] = {"acted": acted, "n": len(t1), "empty": empty, "err": errs,
                     "t2ok": t2ok, "t2n": t2n}
        line = ("  %-6s %-26s turn1 ACTED %d/%d  EMPTY %d  ERR %d  lat med=%.1fs" %
                (lab, mdl, acted, len(t1), empty, errs,
                 sorted(lats)[len(lats) // 2] if lats else -1))
        if t2n:
            line += "  |  turn2 OK %d/%d" % (t2ok, t2n)
        print(line)
    labs = [l for l, _ in arms]
    a0, a1 = summ[labs[0]], summ[labs[1]]
    print("\n  相对判据(第一臂 vs 第二臂):turn1 ACTED %d/%d vs %d/%d;turn2 OK %d/%d vs %d/%d" %
          (a0["acted"], a0["n"], a1["acted"], a1["n"],
           a0["t2ok"], a0["t2n"], a1["t2ok"], a1["t2n"]))
    # ERR 占多数时这组数据压根不是「服从率」,是「没打通」。相对判据在这里会给出
    # 「0/4 vs 0/4 → 不比对照差 → PASS」的假绿(09-01 用错模型名全 400 时真踩到)。
    # 尺子先得证明自己量到了东西,才轮得到比大小。
    for lab in labs:
        s = summ[lab]
        if s["n"] and s["err"] * 2 > s["n"]:
            print("\n  => INVALID:臂 %s 有 %d/%d 发是 ERR(模型名写错/上游不通),"
                  "这组数据不能当服从率比。先把 ERR 清零再比。" % (lab, s["err"], s["n"]))
            return 2

    bad = []
    if a0["acted"] < a1["acted"]:
        bad.append("turn1 服从率低于对照臂")
    if a0["t2n"] and a1["t2n"] and a0["t2ok"] < a1["t2ok"]:
        bad.append("turn2 回灌路差于对照臂")
    if not bad:
        print("  => 第一臂不比第二臂差 → PASS(相对判据)")
        return 0
    print("  => 第一臂**比第二臂差**(%s) → FAIL,别当没看见" % "、".join(bad))
    return 1


if __name__ == "__main__":
    sys.exit(main())
