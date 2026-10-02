#!/usr/bin/env python3
"""litellm-198-worker-kill-triage.py —— 「pod 0 重启却一直 502」的分诊尺子。

在本机跑，内部 ssh 198（与 litellm-198-callbacks-drift.py 同一套凭据读法）。
只读：不改 Deployment、不改 CM、不切路由。唯一写操作是 --probe 在 pod 内跑一段
纯计算脚本（不发网络、不碰 DB）。

为什么要有这个脚本
------------------
2026-10-02 v1.103.2 灰度：254 把 key 走 gray，buyitian 的 cursor key 2h 72 次 502。
所有现成尺子都是绿的：

  * pod restartCount = 0      —— uvicorn 杀的是 pod **里面**的 worker 子进程，pod 不重启
  * Ready = true              —— 另一个 worker 还活着，readiness 照样过
  * SpendLogs 只记了 11 条失败 —— 被杀那一刻在途请求没来得及写账，失败「消失」了

真正的信号只在 proxy 容器 stdout：
    INFO:     Waiting for child process [N]
    INFO:     Child process [N] died
那是 uvicorn 父进程发 ping、worker **5s 没回 pong**（`timeout_worker_healthcheck` 默认 5）
⇒ 当成挂死 kill 掉。worker 的主线程被同步计算占满就会这样。

当轮根因（实测，不是推断）：`litellm_settings.log_raw_request_response: true`。
1.100.1 里它按值 import、从未生效；1.103 改读活值后，每次上游调用都对整个请求体拼
curl 串再跑 `redact_string`（20+ 条正则）。500KB codex 形状请求单次 0.29s，
兜底链每跳各跑一次 ⇒ 6 并发 × 8 跳卡事件循环 5.32s > 5s ⇒ worker 被杀 ⇒ 502。

这个脚本做三件事
----------------
1. 逐 pod 数 worker 被杀次数（与 restartCount 并排打印，让「0 重启」这把坏尺子当场露馅）
2. --sid <12位>：把某把 key 在 nginx 上的 502 分钟桶与 worker 被杀分钟桶对齐
3. --probe：在 pod 里用合成大请求实测 `Logging.pre_call` 单次耗时 + 6×8 跳事件循环最长卡顿，
   并读出该 pod 实际生效的 log_raw_request_response 值。判据：卡顿 ≥ 5s 即会被杀。

阳性对照（必须先看到红）
------------------------
2026-10-02 gray rev5（开关 true）：pre_call 0.29s，6×8 跳卡 5.32s ⇒ RED。
2026-10-02 gray rev9（开关 false）：0.000s / 0.00s ⇒ GREEN。生产 1.100.1：0.000s。
脚本在 probe 输出里同时打印开关值，开关 true 却卡顿 <1s ⇒ 量具本身有问题，别当绿读。

用法
----
  ./litellm-198-worker-kill-triage.py                         # 生产 + gray 全部 pod，近 3h
  ./litellm-198-worker-kill-triage.py --since 10m --lane gray
  ./litellm-198-worker-kill-triage.py --sid 5ef766641221      # 对齐某把 key 的 502
  ./litellm-198-worker-kill-triage.py --probe --lane gray     # 实测卡顿（只跑计算）

退出码：0 = 没有 worker 被杀且 probe 卡顿 < 5s；1 = 有被杀或 probe ≥ 5s；2 = 量具自己坏了。
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

HOST = "cltx@10.68.13.198"
NS = "litellm-product"
LANES = {
    "prod": "carher.net/litellm-production-route=enabled",
    "gray": "app=litellm-proxy-gray",
}
KILL_RE = re.compile(r"Child process \[\d+\] died")
HEALTHCHECK_TIMEOUT_S = 5.0  # uvicorn Config.timeout_worker_healthcheck 默认值（两版都是 5）
MEMORY = Path.home() / ".claude/projects/-Users-Liuguoxian-codes-carher-admin/memory/reference_198_direct_ssh.md"

PROBE = r'''
import time,json,uuid,datetime,yaml,asyncio,litellm
from litellm.litellm_core_utils.litellm_logging import Logging
flag=bool((yaml.safe_load(open("/app/config.yaml")).get("litellm_settings") or {}).get("log_raw_request_response"))
litellm.log_raw_request_response=flag
items=[]
for i in range(33):
    items+=[{"type":"message","role":"user","content":[{"type":"input_text","text":("step %d "%i)+"lorem ipsum "*700}]},
            {"type":"reasoning","id":"rs_"+uuid.uuid4().hex,"summary":[{"type":"summary_text","text":"t"}],"encrypted_content":"gAAAAA"+uuid.uuid4().hex*58},
            {"type":"function_call","call_id":"c%d"%i,"name":"shell","arguments":json.dumps({"cmd":"ls "*30})},
            {"type":"function_call_output","call_id":"c%d"%i,"output":"file "*900}]
data={"model":"probe","input":items,"stream":True}
def one():
    lo=Logging(model="probe",messages=[],stream=True,call_type="aresponses",start_time=datetime.datetime.now(),litellm_call_id=str(uuid.uuid4()),function_id="x")
    lo.update_environment_variables(model="probe",user=None,optional_params={},litellm_params={"metadata":{}})
    lo.pre_call(input=items,api_key="sk-probe",additional_args={"complete_input_dict":data,"api_base":"http://x/responses","headers":{}})
t=time.perf_counter(); one(); single=time.perf_counter()-t
async def main():
    lag=[]; stop=[False]
    async def tick():
        while not stop[0]:
            s=time.perf_counter(); await asyncio.sleep(0.1); lag.append(time.perf_counter()-s-0.1)
    tk=asyncio.create_task(tick())
    async def req():
        for _ in range(8): one(); await asyncio.sleep(0)
    await asyncio.gather(*[req() for _ in range(6)])
    stop[0]=True; await tk
    return max(lag) if lag else 0.0
stall=asyncio.run(main())
print("PROBE flag=%s body_kb=%d pre_call_single=%.3fs stall_6x8=%.2fs"%(flag,len(json.dumps(data))//1024,single,stall),flush=True)
'''


def ssh_password() -> str:
    text = MEMORY.read_text(encoding="utf-8")
    m = re.search(r"密码[^:：]*[:：]\s*`([^`]+)`", text)
    if not m:
        sys.exit("🔴 读不到 198 凭据（memory reference_198_direct_ssh）")
    return m.group(1)


def remote(script: str, pw: str, timeout: int = 300) -> str:
    """远端 root 跑 bash 脚本：密码走 sudo stdin，脚本走 base64 参数，两者不混一条 stdin。"""
    b64 = base64.b64encode(script.encode()).decode()
    cmd = ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", HOST,
           f"sudo -k -S -p '' bash -c \"$(echo {b64} | base64 -d)\""]
    p = subprocess.run(cmd, input=pw + "\n", capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "SSHPASS": pw})
    out = (p.stdout + p.stderr).replace(pw, "<PW>")
    return re.sub(r"sk-[A-Za-z0-9._~-]{8,}", "<R>", out)


def pods(lane: str, pw: str) -> list[str]:
    out = remote(f"export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl -n {NS} get po -l {LANES[lane]} "
                 "--field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'", pw)
    return out.split()


def count_kills(lane: str, since: str, pw: str) -> list[tuple[str, int, int, list[str]]]:
    rows = []
    for p in pods(lane, pw):
        out = remote(
            f"export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; "
            f"echo RST=$(kubectl -n {NS} get po {p} -o jsonpath='{{.status.containerStatuses[0].restartCount}}'); "
            f"kubectl -n {NS} logs {p} -c litellm --since={since} --timestamps 2>/dev/null | grep -E 'Child process \\[[0-9]+\\] died'",
            pw)
        rst = int(re.search(r"RST=(\d+)", out).group(1)) if re.search(r"RST=(\d+)", out) else -1
        kills = [l for l in out.splitlines() if KILL_RE.search(l)]
        # 北京时间分钟桶
        mins = []
        for l in kills:
            m = re.match(r"\d{4}-\d\d-\d\dT(\d\d):(\d\d)", l)
            if m:
                mins.append("%02d:%s" % ((int(m.group(1)) + 8) % 24, m.group(2)))
        rows.append((p, rst, len(kills), mins))
    return rows


def sid_502_minutes(sid: str, pw: str) -> dict[str, int]:
    if not re.fullmatch(r"[0-9a-f]{12}", sid):
        sys.exit("--sid 要 12 位小写 hex（sha256(key) 前 12 位）")
    out = remote(f"grep 'sid={sid}' /var/log/nginx/cc-auto-link.gray.log | "
                 "awk '$3==502{split($1,a,\"=\");print substr(a[2],12,5)}' | sort | uniq -c", pw)
    res = {}
    for l in out.splitlines():
        parts = l.split()
        if len(parts) == 2 and parts[0].isdigit():
            res[parts[1]] = int(parts[0])
    return res


def probe(lane: str, pw: str) -> list[tuple[str, str]]:
    b64 = base64.b64encode(PROBE.encode()).decode()
    res = []
    for p in pods(lane, pw):
        out = remote(f"export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; echo {b64} | base64 -d | "
                     f"kubectl -n {NS} exec -i {p} -c litellm -- python3 - 2>&1 | grep PROBE", pw, timeout=600)
        res.append((p, out.strip() or "<no output — 量具坏了>"))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lane", choices=["prod", "gray", "all"], default="all")
    ap.add_argument("--since", default="3h", help="kubectl logs --since（默认 3h；pod 日志只留得住几小时）")
    ap.add_argument("--sid", help="对齐这把 key（sha256 前 12 位）的 502 与 worker 被杀")
    ap.add_argument("--probe", action="store_true", help="pod 内实测 pre_call 卡顿（只跑计算）")
    a = ap.parse_args()
    pw = ssh_password()
    lanes = ["prod", "gray"] if a.lane == "all" else [a.lane]
    bad = False

    print(f"=== worker 被杀（近 {a.since}）——与 restartCount 并排：restart=0 不代表没事")
    all_kill_mins: dict[str, int] = {}
    for lane in lanes:
        rows = count_kills(lane, a.since, pw)
        if not rows:
            print(f"  {lane}: 没有 Running pod（lane 空，跳过）")
        for p, rst, n, mins in rows:
            flag = "🔴" if n else "✅"
            print(f"  {flag} {lane:4} {p:40} restartCount={rst:<3} worker_killed={n}")
            bad |= n > 0
            for m in mins:
                all_kill_mins[m] = all_kill_mins.get(m, 0) + 1

    if a.sid:
        e502 = sid_502_minutes(a.sid, pw)
        print(f"\n=== sid={a.sid} 的 502 vs worker 被杀（北京时间分钟）")
        if not e502:
            print("  这把 key 在 gray 日志里没有 502")
        both = sorted(set(e502) | set(all_kill_mins))
        hit = sum(1 for m in e502 if m in all_kill_mins)
        for m in both:
            print(f"  {m}  502={e502.get(m, 0):<3} killed={all_kill_mins.get(m, 0)}")
        if e502:
            print(f"  ⇒ {hit}/{len(e502)} 个有 502 的分钟同时有 worker 被杀")
            if hit == 0:
                # 2026-10-02 实测：事故 pod 被 rollout 换掉后，新 pod 日志里没有那批 kill，
                # 这里会读出 0/19 —— 不是「不相关」，是「证据已随旧 pod 消失」。
                ages = remote(f"export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl -n {NS} get po "
                              f"-l {' -l '.join(LANES[l] for l in lanes)} --field-selector=status.phase=Running "
                              "-o jsonpath='{range .items[*]}{.status.startTime}{\"\\n\"}{end}'", pw).split()
                oldest_502 = min(e502)
                print(f"  ⚠️ 0 命中先别下结论：当前 pod 启动时间 {sorted(ages)[:1]}（UTC），最早 502 在 {oldest_502}（北京）。"
                      "\n     pod 晚于 502 就是证据已随旧 pod 消失 ⇒ 结论只能写「无法对齐」，不能写「与 worker 被杀无关」。")
                return 2

    if a.probe:
        print("\n=== pre_call 卡顿实测（6 并发 × 8 跳，阈值 = uvicorn 心跳 5s）")
        for lane in lanes:
            for p, line in probe(lane, pw):
                print(f"  {lane:4} {p:40} {line}")
                m = re.search(r"flag=(\w+).*pre_call_single=([\d.]+)s stall_6x8=([\d.]+)s", line)
                if not m:
                    print("      🔴 probe 没出数 ⇒ 量具坏了，不许当绿读")
                    return 2
                flag, single, stall = m.group(1) == "True", float(m.group(2)), float(m.group(3))
                if stall >= HEALTHCHECK_TIMEOUT_S:
                    print(f"      🔴 卡顿 {stall:.2f}s ≥ {HEALTHCHECK_TIMEOUT_S}s ⇒ 大请求 + 兜底链会把 worker 卡死被杀")
                    bad = True
                if flag and single < 0.01:
                    print("      ⚠️ 开关 true 但 pre_call 几乎不耗时 ⇒ 这版可能按值 import（1.100.1 形状），开关没生效")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
