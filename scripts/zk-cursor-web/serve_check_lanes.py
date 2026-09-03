#!/usr/bin/env python3
"""serve_check_lanes.py —— pod 内直打 8201 一发，看 lane 出不出字。

必须带阳性对照：同一形状打一条**已知活**的 lane。对照红了 = 这把尺子形状不对，
这一轮的红作废（合成红与合成绿同样不可信）。

⚠️ 2026-09-02 这把尺子自己坏过一次，修法刻在这里，别退回去：
   v1 的判据是 `nonce in 原始响应文本`。但 lane 即使收到 stream:false 也按
   chat.completion.chunk 分片回，暗号被切成 "ZKS-17883322" + "2284-84" 两段
   ⇒ 字符串搜不到 ⇒ 判成"不出字"。实际模型一个字没少回。
   **raw 里找不到 ≠ 模型没回，是我没收割**（灵魂律令）。
   所以判据必须建立在**收割后的文本**上：先把 SSE 的 delta.content 拼回完整答案，
   再在文本里搜暗号。顺带打出 raw 长度与 text 长度，"raw 大 text=0" 一眼可辨
   （那才是真的收割逻辑没覆盖到这条通道，该去看 author.role/recipient）。

用法（本机跑，走 ssh 到 198）：
  python3 serve_check_lanes.py 81 83 85 --control 84
"""
import json
import subprocess
import sys
import time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]


def k(argstr, stdin=None, timeout=300):
    r = subprocess.run(SSH + ["sudo -n kubectl -n %s %s" % (NS, argstr)],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def probe_js(nonce, port=8201):
    """探针程序本体。经 `kubectl exec -i <pod> -- node` 的 stdin 灌进去
    （容器里没有 curl/wget，只有 node v22 自带 fetch；也避免往 ssh 单引号串里塞代码）。
    """
    return """
const body = {model:"gpt-5-6", stream:false, messages:[
  {role:"user", content:"请原样输出这一行，不要加别的字：%s"}]};
const t0 = Date.now();
fetch("http://127.0.0.1:%d/v1/chat/completions", {
  method:"POST",
  headers:{"Content-Type":"application/json","Authorization":"Bearer cursor"},
  body: JSON.stringify(body),
}).then(async r => {
  const raw = await r.text();
  let text = "";
  // 收割：lane 即使对 stream:false 也可能按 chunk 分片回，答案散在多个 delta 里。
  if (raw.indexOf("data:") >= 0) {
    for (const ln of raw.split(/\\r?\\n/)) {
      if (!ln.startsWith("data:")) continue;
      const p = ln.slice(5).trim();
      if (!p || p === "[DONE]") continue;
      try {
        const c = (JSON.parse(p).choices || [])[0];
        if (!c) continue;
        if (c.delta && typeof c.delta.content === "string") text += c.delta.content;
        else if (c.message && typeof c.message.content === "string") text += c.message.content;
      } catch (e) {}
    }
  } else {
    try { text = ((((JSON.parse(raw).choices)||[])[0]||{}).message||{}).content || ""; } catch (e) {}
  }
  console.log("HTTP " + r.status + " " + ((Date.now()-t0)/1000).toFixed(1) + "s raw=" + raw.length + "B text=" + text.length + "c");
  console.log("TEXT>>>" + text.replace(/\\s+/g, " ").slice(0, 400) + "<<<");
  // 收割不到时才打 raw 头尾，供判"是没回"还是"我没收到该通道"
  if (!text) console.log("RAWHEAD>>>" + raw.slice(0, 300).replace(/\\n/g, " ") + " ... " + raw.slice(-300).replace(/\\n/g, " "));
}).catch(e => { console.log("ERR " + e.name + ": " + e.message); });
""" % (nonce, port)


def pod_of(lane):
    o, _, _ = k("get pod -l app=zero-cursor-bpi-%s --field-selector=status.phase=Running -o json" % lane)
    try:
        its = json.loads(o).get("items") or []
        return its[0]["metadata"]["name"] if its else ""
    except Exception:
        return ""


def serve_check(pod, tag, port=8201):
    """判据 = **收割后的文本**里有暗号。返回 (ok, 说明)。"""
    nonce = "ZKS-%d" % int(time.time())
    t0 = time.time()
    o, e, rc = k("exec -i %s -- node" % pod, stdin=probe_js(nonce, port))
    dt = time.time() - t0
    head = (o.splitlines() or [""])[0].strip()
    ok = nonce in o          # 只可能出现在 TEXT>>> 行里（prompt 不回显）
    print("   [serve-check %-24s] %-46s %5.1fs 暗号=%s" % (tag, head or "?", dt, "命中 ✅" if ok else "未命中 ❌"))
    if not ok:
        print("      " + ((o or e)[-500:]).replace("\n", "\n      "))
    return ok, (head or "?")


def check(lane, tag, port=8201):
    pod = pod_of(lane)
    if not pod:
        print("   [serve-check %-24s] ❌ 没有 Running pod" % ("lane %s %s" % (lane, tag)))
        return False
    ok, _ = serve_check(pod, "lane %s %s" % (lane, tag), port)
    return ok


def main():
    lanes = []
    ctrl = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--control":
            ctrl = args[i + 1]; i += 2; continue
        lanes.append(args[i]); i += 1
    lanes = [l for l in lanes if l != ctrl]

    if ctrl:
        # 双向实测，两道都在**同一条已知活的 lane** 上跑，只差一个端口：
        #   阴性：打 8299（没人监听）——必须红。恒绿的尺子判不了任何东西。
        #   阳性：打 8201 ——必须绿。红了说明形状不对，本轮结果全作废。
        # 这两道之前是"跑之前想一想"，2026-09-02 尺子真坏了一次之后改成每轮强制跑。
        print("阴性对照（同 pod 打 8299，必须红）:")
        if check(ctrl, "阴性/坏端口", port=8299):
            print("\n❌ 尺子恒绿：连没人监听的端口都判命中。停手。")
            return 2
        print("\n阳性对照（必须绿）:")
        if not check(ctrl, "阳性/已知活"):
            print("\n❌ 尺子坏了：阳性对照也红。别据此判断被测 lane。")
            return 2
    print("\n被测:")
    res = {l: check(l, "被测") for l in lanes}
    print("\n小结: " + ", ".join("%s=%s" % (l, "出字 ✅" if v else "不出字 ❌") for l, v in res.items()))
    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
