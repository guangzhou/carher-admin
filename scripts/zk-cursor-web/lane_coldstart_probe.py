#!/usr/bin/env python3
"""lane_coldstart_probe.py —— 「这条 lane 的 seed 现在还能通过**冷启动握手**吗」

为什么必须先问这个：
  lane 进程启动时会拿 seed 里的 authorization + cookie jar + proof token 去 Sentinel 握一次手，
  握不上就 `[fatal] failed to start` → CrashLoop。而 **运行中的 pod 请求成功 ≠ 冷启动握得上**：
  2026-09-02 实测 lane 81 灌进一个 exp 还有 177 小时的全新 bearer，冷启动照样 401 token_expired
  ⇒ 握手认的不只是 bearer，旧 cookie 会话也参与。

  本轮计划的第 2 步要给 84/85 改挂 CM，`Recreate` 策略必然重启它们。**万一它们的 seed 也
  只在进程内有效，一重启就变 CrashLoop** —— 那是拿两条现在唯一好使的生产腿去赌。

做法：不碰生产 lane。把它的 seed 目录**拷贝一份**，用拷贝启动一个临时 deploy，
只看它起不起得来，看完就删。实验与推广分开。

用法：
  python3 lane_coldstart_probe.py 84            # dry-run，只打算怎么做
  python3 lane_coldstart_probe.py 84 --apply
判据：临时 pod 变 Running/Ready ⇒ 这份 seed 冷启动握得上，生产 lane 重启是安全的；
      CrashLoop + `[fatal] ... Sentinel 401` ⇒ 重启会弄死它，**先解决登录态再谈改 CM**。
"""
import argparse
import json
import subprocess
import sys
import time

NS = "litellm-product"
STANDBY = "10.68.13.225"
SSH198 = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SSH225 = ["sshpass", "-p", "Hn8#mKLp3QxZ", "ssh", "-o", "StrictHostKeyChecking=no",
          "-o", "ConnectTimeout=20", "cltx@" + STANDBY]


def k(argstr, stdin=None):
    cmd = SSH198 + ["sudo -n kubectl -n %s %s" % (NS, argstr)]
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def standby(cmd):
    r = subprocess.run(SSH225 + [cmd], capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def serve_check(pod, tag, timeout=150):
    """在 pod 内直打它自己的 8201 一发，看还出不出字。

    走 pod 内 node 而不是经 LiteLLM：临时 lane 没有 svc、也没有模型行，
    经前门够不到它。代价是形状与真 Cursor 不同 ⇒ **必须配阳性对照**（同形状打生产 pod），
    对照红了就说明是这把尺子的形状问题，不是被测对象的问题。

    ⚠️ 探针体与判据都在 serve_check_lanes.py，**只此一份**。
       2026-09-02 这里曾各写一份，两处都拿"原始响应文本里有没有暗号"当判据，
       而 lane 会把答案切成 delta 分片 ⇒ 暗号被劈开 ⇒ 活着的 lane 判成不出字。
       判据必须建立在收割后的文本上，两个调用点不许再各自实现。
    """
    from serve_check_lanes import probe_js
    nonce = "ZKCS-%d" % int(time.time())
    t0 = time.time()
    o, e, rc = k("exec -i %s -- node" % pod, stdin=probe_js(nonce))
    dt = time.time() - t0
    code = (o.splitlines() or [""])[0].strip()
    ok = nonce in o
    print("   [serve-check %s] %s %s %.1fs 暗号=%s"
          % (tag, pod, code or "?", dt, "命中" if ok else "未命中"))
    if not ok:
        print("      " + ((o or e)[-400:]).replace("\n", "\n      "))
    return ok, ("%s 暗号命中" % (code or "?")) if ok else ("%s 无暗号" % (code or "?"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lane")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--live-from-ws", action="store_true",
                    help="往 seed **副本** 里灌 acct 的 WS 线 OAuth token 再冷启动。"
                         "回答『这条死腿能不能自动救活』，生产 lane 零影响。")
    a = ap.parse_args()
    n = a.lane
    tmp = "zero-cursor-bpi-%st" % n
    src_dir = "/Data/zerokey-sessions/zero-%s" % n
    tmp_dir = "/Data/zerokey-sessions/zero-%st" % n

    out, err, rc = k("get deploy zero-cursor-bpi-%s -o json" % n)
    if rc != 0:
        print("读 deploy 失败:", err[:300]); return 2
    d = json.loads(out)
    spec = d["spec"]["template"]["spec"]
    cm = spec["volumes"][3]["configMap"]["name"]
    assert spec["volumes"][3]["name"] == "patch", "volumes[3] 不是 patch，形状变了，停手"
    print("源 lane zero-cursor-bpi-%s：挂的 CM = %s，seed = %s" % (n, cm, src_dir))
    print("临时 lane %s：seed 用独立副本 %s（不碰源目录，源 lane 零影响）" % (tmp, tmp_dir))
    if not a.apply:
        print("\n(dry-run。加 --apply 执行；跑完自动删临时 deploy 与副本目录)")
        return 0

    # 1) 拷 seed 副本
    # /Data/zerokey-sessions 属 root，cltx 建不了子目录 ⇒ 走 sudo -n（225 上已确认免密）
    o, e, rc = standby("sudo -n rm -rf %s && sudo -n cp -a %s %s && ls -la %s | tail -3"
                       % (tmp_dir, src_dir, tmp_dir, tmp_dir))
    print(o.strip() or e.strip()[:300])
    if rc != 0:
        return 2

    # 1b) 可选：往**副本**里灌 WS 线的活 OAuth token。
    #     上次是直接灌生产 lane，结果把 81/83 从「Running 但 500」推成 CrashLoop；
    #     灌副本就没有这个代价 —— 起不来只是这次实验红，生产一根汗毛没动。
    if a.live_from_ws:
        o2, _, _ = k("get pod -l app=chatgpt-acct-%s --field-selector=status.phase=Running -o json" % n)
        try:
            apod = (json.loads(o2).get("items") or [])[0]["metadata"]["name"]
        except Exception:
            print("!! chatgpt-acct-%s 没有 Running pod，灌不了" % n)
            standby("sudo -n rm -rf %s %s-cache" % (tmp_dir, tmp_dir)); return 2
        tok_out, tok_err, _ = k("exec %s -- cat /chatgpt-auth/auth.json" % apod)
        if not tok_out.strip():
            print("!! acct-%s 的 auth.json 读出空（是没读到，不是 token 坏）：%s"
                  % (n, (tok_err or "")[:200]))
            standby("sudo -n rm -rf %s %s-cache" % (tmp_dir, tmp_dir)); return 2
        live = json.loads(tok_out)["access_token"]
        # token 只经 ssh stdin 进 remote python，不过 argv（防 ps 泄漏）
        py = ("import json,sys;"
              "live=sys.stdin.read().strip();"
              'p="%s/users.json";'
              "d=json.load(open(p));"
              'd["chatgpt"]["acct%s"]["parsedFetch"]["headers"]["authorization"]="Bearer "+live;'
              'json.dump(d,open(p,"w"),ensure_ascii=False,indent=2);'
              'print("副本 seed 已换成 WS token, len=%%d" %% len(live))') % (tmp_dir, n)
        r = subprocess.run(SSH225 + ["sudo -n python3 -c '%s'" % py],
                           input=live, capture_output=True, text=True)
        print("  " + (r.stdout.strip() or r.stderr.strip()[:300]))
        if r.returncode != 0:
            standby("sudo -n rm -rf %s %s-cache" % (tmp_dir, tmp_dir)); return 2

    # 2) 从源 spec 造临时 deploy：只改身份字段与 seed 路径，其余（env/args/CM/资源）逐字段照抄
    nd = {"apiVersion": "apps/v1", "kind": "Deployment",
          "metadata": {"name": tmp, "namespace": NS, "labels": {"app": tmp}},
          "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                   "selector": {"matchLabels": {"app": tmp}},
                   "template": {"metadata": {"labels": {"app": tmp}},
                                "spec": json.loads(json.dumps(spec))}}}
    s = nd["spec"]["template"]["spec"]
    for v in s["volumes"]:
        if v["name"] == "seed":
            v["hostPath"]["path"] = tmp_dir
        if v["name"] == "convcache":
            v["hostPath"]["path"] = tmp_dir + "-cache"
            v["hostPath"]["type"] = "DirectoryOrCreate"
    print("\n创建临时 deploy…")
    o, e, rc = k("create -f -", stdin=json.dumps(nd))
    print(o.strip() or e.strip()[:400])
    if rc != 0:
        standby("sudo -n rm -rf %s %s-cache" % (tmp_dir, tmp_dir))
        return 2

    # 3) 盯 3 分钟
    # 注意：不要用 -o jsonpath —— 命令是拼进 ssh 单串的，花括号/引号会被吞掉，
    # kubectl 退化成打印整份 json，看起来"有输出"其实判据全废（2026-09-02 踩过）。
    verdict, pod = "未知", ""
    try:
        for _ in range(36):
            time.sleep(5)
            o, _, _ = k("get pod -l app=%s -o json" % tmp)
            try:
                items = json.loads(o).get("items") or []
            except Exception:
                continue
            if not items:
                print("   (还没有 pod)")
                continue
            p = items[0]
            pod = p["metadata"]["name"]
            phase = p["status"].get("phase")
            cs = (p["status"].get("containerStatuses") or [{}])[0]
            ready = cs.get("ready")
            state = cs.get("state") or {}
            waiting = (state.get("waiting") or {}).get("reason", "")
            term = (state.get("terminated") or {}).get("reason", "")
            restarts = cs.get("restartCount", 0)
            print("   %s phase=%s ready=%s state=%s%s restarts=%d"
                  % (pod, phase, ready, waiting or term or "running",
                     "", restarts))
            if ready:
                verdict = "READY —— 这份 seed 冷启动握得上"
                break
            if waiting == "CrashLoopBackOff" or restarts >= 2:
                verdict = "CRASH —— 冷启动握手失败，源 lane 一旦重启就会死"
                break
        print("\n冷启动判据:", verdict)

        # 3b) READY 只证明进程起来了，不证明它还能出字。在 pod 内直打 8201 补一发。
        #     阳性对照 = 同一形状打**当前活着的生产 pod**：生产也红 ⇒ 探针形状不对，红作废。
        if verdict.startswith("READY"):
            src_pod = ""
            o, _, _ = k("get pod -l app=zero-cursor-bpi-%s --field-selector=status.phase=Running -o json" % n)
            try:
                its = json.loads(o).get("items") or []
                src_pod = its[0]["metadata"]["name"] if its else ""
            except Exception:
                pass
            t_ok, t_why = serve_check(pod, "临时(seed 副本冷启动)")
            c_ok, c_why = ("跳过", "找不到生产 pod")
            if src_pod:
                c_ok, c_why = serve_check(src_pod, "生产(已在跑，阳性对照)")
            if not c_ok:
                verdict = ("尺子坏了 —— 阳性对照(生产 %s)也红：%s。这一轮的红作废，"
                           "别据此判断冷启动。" % (src_pod, c_why))
            elif not t_ok:
                verdict = "起得来但不出字 —— %s（生产同形状是绿的，所以这条红算数）" % t_why
            else:
                verdict = "READY + 出字 —— 这份 seed 冷启动完全可用，源 lane 重启是安全的"
            print("\n最终判据:", verdict)

        if pod:
            o, _, _ = k("logs %s --tail=40 --all-containers=true" % pod)
            e2, _, _ = k("logs %s --previous --tail=40" % pod)
            print("--- 当前容器日志尾 ---\n" + (o[-2000:] or "(空)"))
            if e2.strip():
                print("--- 上一次崩溃的日志尾 ---\n" + e2[-2000:])
    finally:
        print("\n拆临时资产…")
        print(k("delete deploy %s --wait=false" % tmp)[0].strip())
        print(standby("sudo -n rm -rf %s %s-cache" % (tmp_dir, tmp_dir))[0].strip() or "副本目录已删")
    return 0 if verdict.startswith("READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
