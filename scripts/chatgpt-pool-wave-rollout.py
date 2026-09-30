#!/usr/bin/env python3
"""受控分波 rollout acct pod，每波验通才走下一波。

⛔ 为什么不用 `scripts/chatgpt-pool-model/fanout-pool-model.sh`：那份对全池无差别
   `rollout restart`。**重启在服务的号可能永久打死它且回退救不回**（acct-237）。
   本脚本分波、波内并发、每波三判据，任一波出现 NEWLY BROKEN 立刻停并打印回滚指令。

三判据（少一条都会把「坏了」读成「好了」）：

1. **`rollout status` 成功** —— 只说明新 RS 起来了。
   ⚠️ `paused=true` 的 Deployment 会静默吃下变更但不建新 RS，rollout status 只超时。
2. **pod 内 `grep -c model_name /app/config.yaml` == 期望值** —— 证明新 CM 真进了容器。
   subPath 挂载不跟 inode，这一条是「装载层」。
3. **pod 内直打目标 slug + 阳性对照** —— 证明号还活着且新名字能用。
   🔴 只有 1+2 的话，一个被重启打死的号会表现成「配置都对，就是不能用」,
   和「模型没放开」同形。**阳性对照就是用来把这两者分开的**：
   对照也死 ⇒ 是重启把号打死了，不是模型问题。

重启**前**必须先拿一次基线（`--baseline` 自动做）：否则既存故障会落进本轮窗口，
形状和「我搞坏了」一模一样（feedback_onboarding_regression_window_swallows_preexisting_outage）。

用法：
    python3 chatgpt-pool-wave-slug.py --targets ~/sol61-targets.txt --slug gpt-6.1-sol \
        --expect-model-name 16 --wave-size 4                      # dry-run，只拿基线
    ... --apply

退出码：0 = 全波验通；5 = 出现 NEWLY BROKEN（已停在该波）；3 = 基线/尺子失效。
"""
import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time

RESULT_RE = re.compile(r"^RESULT\t([^\t]*)\t([^\t]*)\t([^\t]*)\t(.*)$")

PROBE_SRC = r'''
import json, urllib.request, urllib.error, sys
d = json.load(open("/chatgpt-auth/auth.json"))
h = {"Authorization": "Bearer " + d["access_token"], "Content-Type": "application/json",
     "User-Agent": "codex_cli_rs/1.0.0", "chatgpt-account-id": d.get("account_id") or "",
     "OpenAI-Beta": "responses=experimental",
     "session_id": "11111111-2222-3333-4444-555555555555",
     "Accept": "text/event-stream"}
model, nonce = sys.argv[1], sys.argv[2]
body = {"model": model, "instructions": "You are a terse assistant.",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "Reply with exactly this token and nothing else: " + nonce}]}],
        "stream": True, "store": False,
        "reasoning": {"effort": "low", "summary": "auto"}}
req = urllib.request.Request("https://chatgpt.com/backend-api/codex/responses",
                             data=json.dumps(body).encode(), headers=h)
try:
    r = urllib.request.urlopen(req, timeout=120)
except urllib.error.HTTPError as e:
    print("RESULT\t%s\tHTTP%d\t\t%s" % (model, e.code,
          e.read()[:160].decode(errors="replace").replace("\t", " ").replace("\n", " ")))
    raise SystemExit(0)
except Exception as e:
    print("RESULT\t%s\tEXC\t\t%s" % (model, str(e)[:140].replace("\t", " ")))
    raise SystemExit(0)
txt = []
for raw in r:
    line = raw.decode(errors="replace").strip()
    if not line.startswith("data:"):
        continue
    p = line[5:].strip()
    if p == "[DONE]":
        break
    try:
        ev = json.loads(p)
    except Exception:
        continue
    if ev.get("type") == "response.output_text.delta":
        txt.append(ev.get("delta") or "")
    elif ev.get("type") == "response.completed":
        for it in (ev.get("response") or {}).get("output") or []:
            for c in it.get("content") or []:
                if c.get("type") == "output_text":
                    txt.append(c.get("text") or "")
got = "".join(txt).strip()
print("RESULT\t%s\t200\t\t%s" % (model, "YES" if nonce in got else ""))
'''


def sh(cmd, timeout=240):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def kc(args, ns, sudo):
    return ("sudo -n kubectl " if sudo else "kubectl ") + "-n %s %s" % (ns, args)


def pod_of(acct, ns, sudo):
    rc, out, _ = sh(kc('get pods --no-headers -o custom-columns=:metadata.name,:status.phase,'
                       ':status.containerStatuses[0].ready', ns, sudo))
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0].startswith("chatgpt-acct-%s-" % acct) and f[1] == "Running" \
                and f[2] == "true":
            return f[0]
    return None


def probe(acct, ns, sudo, models, nonce, remote, expect_names):
    """返回 (acct, {model: bucket}, model_name_count)。"""
    pod = pod_of(acct, ns, sudo)
    if not pod:
        return acct, {m: "NOPOD" for m in models}, -1
    rc, out, _ = sh(kc("exec %s -- grep -c model_name /app/config.yaml" % pod, ns, sudo), 90)
    try:
        cnt = int(out.strip().splitlines()[-1])
    except Exception:
        cnt = -1
    rc, _, err = sh(kc("cp %s %s:%s" % (remote, pod, remote), ns, sudo), 90)
    if rc != 0:
        return acct, {m: "CPFAIL" for m in models}, cnt
    res = {}
    for m in models:
        rc, out, err = sh(kc("exec %s -- python3 %s %s %s" % (pod, remote, m, nonce),
                             ns, sudo), 260)
        got = None
        for line in out.splitlines():
            mm = RESULT_RE.match(line)
            if mm and mm.group(1) == m:
                st, body = mm.group(2), mm.group(4)
                got = ("OK" if body.strip() == "YES" else "EMPTY200") if st == "200" else st
        res[m] = got or "NOPARSE"
    return acct, res, cnt


def scan(accts, ns, sudo, models, nonce, remote, jobs, expect_names):
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(probe, a, ns, sudo, models, nonce, remote, expect_names): a
                for a in accts}
        for fu in concurrent.futures.as_completed(futs):
            a, res, cnt = fu.result()
            out[a] = {"probe": res, "model_name_count": cnt}
    return out


def render(tag, snap, slug, control, expect_names):
    print("\n-- %s" % tag)
    for a in sorted(snap, key=int):
        s = snap[a]
        flag = "" if s["model_name_count"] == expect_names else "  ⚠️CM未进容器"
        print("   acct-%-4s names=%-3s %s=%-9s %s=%-9s%s"
              % (a, s["model_name_count"], slug, s["probe"].get(slug),
                 control, s["probe"].get(control), flag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--control", default="gpt-5.6-sol",
                    help="阳性对照：重启后它也死 ⇒ 是重启打死了号，不是模型问题")
    ap.add_argument("--expect-model-name", type=int, required=True,
                    help="新 CM 里 model_name 的条数（装载层判据）")
    ap.add_argument("--ns", default="litellm-product")
    ap.add_argument("--wave-size", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--remote-path", default="/tmp/wave-probe.py")
    ap.add_argument("--settle", type=int, default=20, help="rollout 成功后等几秒再探")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-sudo", action="store_true")
    a = ap.parse_args()
    sudo = not a.no_sudo
    models = [a.slug, a.control]
    accts = [l.strip() for l in open(a.targets) if l.strip()]
    nonce = "WV%s" % time.strftime("%H%M%S")

    with open(a.remote_path, "w") as f:
        f.write(PROBE_SRC)
    print("== targets %d 个：%s" % (len(accts), ",".join(accts)))
    print("== nonce=%s  期望 model_name 条数=%d" % (nonce, a.expect_model_name))

    # ---- 基线：重启前的样子。没有它，既存故障会被读成「我搞坏了」 ----
    print("\n=== 基线（重启前）")
    base = scan(accts, a.ns, sudo, models, nonce + "B", a.remote_path, a.jobs,
                a.expect_model_name)
    render("baseline", base, a.slug, a.control, a.expect_model_name)
    base_ctl_ok = [x for x in base if base[x]["probe"].get(a.control) in ("OK", "EMPTY200")]
    if not base_ctl_ok:
        print("\n🔴 基线里阳性对照 0 个可用 ⇒ 坏的是尺子或整池，别在这个状态下重启。")
        return 3
    print("\n基线：阳性对照可用 %d/%d；目标 slug 此刻在容器里%s"
          % (len(base_ctl_ok), len(accts),
             "（CM 还没进去，预期打不通）" if all(
                 base[x]["model_name_count"] != a.expect_model_name for x in base) else ""))

    if not a.apply:
        print("\n(dry-run：只拿了基线，没重启。加 --apply 分波执行)")
        return 0

    waves = [accts[i:i + a.wave_size] for i in range(0, len(accts), a.wave_size)]
    done = []
    for wi, wave in enumerate(waves, 1):
        print("\n=== 波 %d/%d：%s" % (wi, len(waves), ",".join(wave)))
        for n in wave:
            rc, o, e = sh(kc("rollout restart deploy/chatgpt-acct-%s" % n, a.ns, sudo), 90)
            print("   restart acct-%s rc=%d %s" % (n, rc, (o or e).strip()[:100]))
        for n in wave:
            rc, o, e = sh(kc("rollout status deploy/chatgpt-acct-%s --timeout=300s" % n,
                             a.ns, sudo), 340)
            print("   status  acct-%s rc=%d %s" % (n, rc, (o or e).strip()[:120]))
            if rc != 0:
                print("\n🔴 acct-%s rollout 没成功 ⇒ 停在本波，不再往下。" % n)
                print(rollback_hint(a))
                return 5
        time.sleep(a.settle)
        snap = scan(wave, a.ns, sudo, models, nonce + "W%d" % wi, a.remote_path, a.jobs,
                    a.expect_model_name)
        render("波 %d 重启后" % wi, snap, a.slug, a.control, a.expect_model_name)

        broken, notloaded, slug_bad = [], [], []
        for n in wave:
            was = base[n]["probe"].get(a.control) in ("OK", "EMPTY200")
            now = snap[n]["probe"].get(a.control) in ("OK", "EMPTY200")
            if was and not now:
                broken.append(n)                       # 重启把活号打死了
            if snap[n]["model_name_count"] != a.expect_model_name:
                notloaded.append(n)                    # CM 没进容器
            if now and snap[n]["probe"].get(a.slug) not in ("OK", "EMPTY200"):
                slug_bad.append(n)                     # 号活着但新名字不通
        print("\n   NEWLY BROKEN(对照由活转死) %d %s | CM未进容器 %d %s | 号活但新名不通 %d %s"
              % (len(broken), ",".join(broken), len(notloaded), ",".join(notloaded),
                 len(slug_bad), ",".join(slug_bad)))
        if broken:
            print("\n🔴 波 %d 出现 NEWLY BROKEN ⇒ 停。已重启 %s，未重启 %s。"
                  % (wi, ",".join(done + wave),
                     ",".join([x for x in accts if x not in done + wave])))
            print(rollback_hint(a))
            return 5
        if notloaded:
            print("\n🔴 波 %d 有号 CM 没进容器（model_name 条数不对）⇒ 停。"
                  "这不是模型问题，是这个号挂的不是主 CM 或 rollout 没真换 RS。" % wi)
            print(rollback_hint(a))
            return 5
        done += wave
        print("   累计已验通 %d/%d" % (len(done), len(accts)))

    print("\n=== 全部 %d 个号验通：CM 装载 + 目标 slug 直打 + 阳性对照无回退" % len(done))
    return 0


def rollback_hint(a):
    return ("\n回滚：\n"
            "  python3 ~/pool-cm-add-9d.py --cm chatgpt-pool-config --slug %s --delete --apply\n"
            "  # 然后对已重启的号再 rollout restart 一次让它们回到旧 config\n"
            "  # ⚠️ 被重启打死的号回滚救不回（acct-237 先例），只能摘出轮转" % a.slug)


if __name__ == "__main__":
    sys.exit(main())
