#!/usr/bin/env python3
"""scenario_coverage_probe.py — cursor-g 82 真实场景分布探针(#3 soak 数据代表性)。

s3h 只覆盖 chat(hi)/sort/ls 三类;真实用户还有四类高频场景,本探针补齐并各带**验收判据**,
把 verdict 数据从"合成三例"拓成"贴近真实分布",供 #3 每日聚合与 #4 soak 判据取更有代表性的数。

四类场景 + 期望形态(判据来源=proto2 契约的 IF-AND-ONLY-IF 条款 + 动手语义,非拍脑袋):
  simple_ask   纯知识题(快排是什么)      → 期望 PROSE_ONLY:纯散文零命令(知识题不许跑演示命令)
  code_read    读本地代码讲结构            → 期望 ACT:必发只读命令(cat/read/ls),不许凭空编造
  file_op      本地文件写操作              → 期望 ACT:必发写命令(不许只宣布"我将创建")
  lark_doc     写飞书文档                  → 期望 ACT:必发命令/工具调用(不许只作文描述步骤)

每类判据:
  PROSE_ONLY  通过 = 有非空 prose 且 calls==0(答了且没乱跑命令)
  ACT         通过 = calls>=1(真动手,非只宣布);附 prose 供人看 leadingText 是否自包含

用法(198 上跑,scoped key 用完即删):
  python3 scenario_coverage_probe.py [--conv N] [--lane cursor-web-fc-82-terra]
  --conv N:每类场景**连发 N 个独立首轮**(默认 1)。

⚠ --conv 的口径实测澄清(2026-08-27,pod 日志对账钉死):ls 夹具无 previous_response_id/
conversation/store 字段,网关按会话身份判定 → 每轮都是 `[handshake] implicit` 首轮、零 `DIET` 行。
故 --conv N **测的是"独立首轮重复稳定性",不是增量轮(delta)服从**。要测真增量轮须把每轮响应的
response id 回喂下一轮 previous_response_id(且需先证网关认这个字段),属探针增强,非本次覆盖目标。

实测发现(留档,非本探针修复项):file_op 偶发模型产 `⟦write¦path=…¦content=…⟧` 写方言块 →
方言转译层只认 ⟦ls/glob/read/grep⟧+⟦cmd¦run⟧,不认 ⟦write⟧ → 判 violation(empty/undeliverable)
→ 同会话重发(预算1)→ 重发产出正确 `printf > file` shell 块执行成功(自愈,探针仍记 ACT)。
治本靠契约把写操作导向 ⟦cmd¦run⟧,不加方言(加方言=标准指令警告的症状补丁)。
"""
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

# 场景库:(key, 期望形态, 用户原话)。原话尽量贴真实 Cursor 用户口吻。
SCENARIOS = [
    ("simple_ask", "PROSE_ONLY", "快速排序的平均时间复杂度是多少？简单讲讲原理就行，不用跑代码。"),
    ("code_read", "ACT", "读一下 backend/config_gen.py，跟我讲讲它的主要函数和职责。"),
    ("file_op", "ACT", "在 /tmp 下建个 probe_scratch.txt，写一行 hello-from-probe。"),
    ("lark_doc", "ACT", "把当前目录的结构梳理一下，写到一个飞书文档里。go"),
]


def _replace_query(d, task):
    """把最后一条 user message 的 <user_query> 换成本场景原话(复用 complex_probe 手法)。"""
    for it in reversed(d.get("input", [])):
        if it.get("type") == "message" and it.get("role") == "user":
            for c in it.get("content", []):
                if isinstance(c, dict) and "<user_query>" in str(c.get("text", "")):
                    c["text"] = "<user_query>\n" + task + "\n</user_query>"
            return
    raise RuntimeError("no user <user_query> slot found in fixture")


def run_turn(base, key, lane, task):
    d = s3.load_fix("ls", "")   # 借 ls 夹具的信封形状,只换 query 文本
    d["model"] = lane
    _replace_query(d, task)
    t0 = time.time()
    txt = ""
    calls = []
    completed = 0
    resp = s3.post(base, key, "/v1/responses", d, timeout=180)
    for raw in resp:
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
        if t == "response.completed":
            completed += 1
        if t == "response.output_item.done":
            it = ev.get("item") or {}
            if it.get("type") == "message":
                txt += "".join(c.get("text", "") for c in (it.get("content") or []) if isinstance(c, dict))
            if it.get("type") in ("function_call", "custom_tool_call"):
                calls.append(str(it.get("arguments") or it.get("input") or "")[:120])
    return {"lat": round(time.time() - t0, 1), "completed": completed,
            "prose": txt, "calls": calls}


def judge(expect, r):
    """按场景期望形态裁一个 ok。返回 (ok, why)。"""
    prose_nonempty = len(r["prose"].strip()) >= 4
    n_calls = len(r["calls"])
    if expect == "PROSE_ONLY":
        if not prose_nonempty:
            return False, "空 prose"
        if n_calls > 0:
            return False, f"知识题却发了{n_calls}条命令(该纯答)"
        return True, "纯答无命令"
    if expect == "ACT":
        if n_calls >= 1:
            return True, f"动手({n_calls}call)"
        return False, "只宣布未动手(0 call)"
    return False, f"未知期望 {expect}"


def main(argv):
    lane = "cursor-web-fc-82-terra"
    conv = 1
    it = iter(argv[1:])
    for a in it:
        if a == "--lane":
            lane = next(it)
        elif a == "--conv":
            conv = int(next(it))
    base, mk = s3.proxy(), s3.master_key()
    kr = json.loads(s3.post(base, mk, "/key/generate",
          {"models": [lane], "duration": "1h", "key_alias": "scenprobe-%d" % int(time.time())}).read())
    key = kr["key"]
    print("scoped key %s… lane=%s conv=%d" % (key[:12], lane, conv), flush=True)
    rows = []
    try:
        for skey, expect, task in SCENARIOS:
            for turn in range(1, conv + 1):
                r = run_turn(base, key, lane, task)
                ok, why = judge(expect, r)
                rows.append({"scenario": skey, "turn": turn, "expect": expect, "ok": ok, "why": why,
                             "lat": r["lat"], "calls": len(r["calls"])})
                call0 = (r["calls"][0] if r["calls"] else None)
                print("[%s t%d] %s expect=%s lat=%.1fs calls=%d why=%s\n    prose=%r\n    call=%s"
                      % (skey, turn, "OK" if ok else "FAIL", expect, r["lat"], len(r["calls"]), why,
                         r["prose"][:90], call0), flush=True)
                time.sleep(2)
    finally:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
        print("scoped key deleted", flush=True)
    n_ok = sum(1 for x in rows if x["ok"])
    print("\n== %d/%d PASS ==" % (n_ok, len(rows)), flush=True)
    print("VERDICT:", "GO" if n_ok == len(rows) else "REVIEW",
          json.dumps([{"s": x["scenario"], "t": x["turn"], "ok": x["ok"], "why": x["why"]} for x in rows],
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv)
