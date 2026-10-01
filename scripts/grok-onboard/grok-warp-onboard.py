#!/usr/bin/env python3
"""grok-warp-onboard.py — 在 Mac 上一条命令加 grok 号：225 WARP 抓 token → 198 建号 → 回归。

**为什么存在**：`sub2api-grok-onboard.py onboard`（09-21 那条"一条命令"）第 2 步走
sub2api 自带的 SSO 端点，而 2026-09-25 起它从 pod 出口对任何 sso 都 403
（09-28 定因：`accounts.x.ai` 按出口挡机房）。能走通的路是 225 WARP 抓 + 198 直接写库，
09-28（64~67）和 09-29（68/69）两轮都一把过，但每轮手搓 7 步、三台机器、三处 shred。
本脚本只做编排，**不重写任何一步**：

  0. 本地 parse（复用 xai_sso_device_oauth.parse_creds，零网络）
  1. health 基线            → sub2api-grok-onboard.py health
  2. 225：抓 bundle          → xai_sso_device_oauth.py --creds（WARP socks5）
  3. 198：建号               → create-grok-oauth-account.py（只喂 x.ai 判 200 的那些 bundle）
  4. 等 --wait 秒，regress + health，打印「真服务腿数」前后对比
  finally：225 / 198 两个 mktemp 目录 shred + rm（成败都做）

🔴 两台远端都用 **mktemp 临时目录**，不碰 `/home/cltx/grok-onboard/`：那份被 park 巡检
cron 每分钟调用，`sub2api-grok-onboard.py` 从 Mac 跑时的 `delegate()` 会整份覆盖它
（09-29 22:53 就这么把巡检代码静默换了一版）。health/regress 这里通过
`S2A_REMOTE_DIR=<198 临时目录>` 绕开。

用法:
    python3 grok-warp-onboard.py ~/creds.txt            # 真跑
    python3 grok-warp-onboard.py ~/creds.txt --dry-run  # 只 parse + 基线 + 两端连通/sha，不抓不写

凭据行形状同 sub2api-grok-onboard.py（短行 `email----pw----sso` 或 8 段长行）。
⛔ 凭据文件别放在 repo 目录里；本脚本不删它（可能要重跑），收尾自己 `shred -u`。
退出码：任何一步红 ⇒ 非 0（抓取有 BAD、建号非 0、regress 非 0）。
"""
import argparse
import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
H225 = os.environ.get("XAI_HOST", "cltx@10.68.13.225")
H198 = os.environ.get("S2A_HOST", "cltx@10.68.13.198")
CAPTURE = "xai_sso_device_oauth.py"
CREATE = "create-grok-oauth-account.py"
ONBOARD = "sub2api-grok-onboard.py"
HELPER = "sub2api_admin.py"
q = shlex.quote


def log(*a):
    print(*a, flush=True)


def ssh(host, cmd, capture=False):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", host, cmd],
                       capture_output=capture, text=True)
    return r.returncode, (r.stdout if capture else "")


def scp(srcs, dst):
    if subprocess.run(["scp", "-q"] + srcs + [dst]).returncode:
        sys.exit("scp failed: %s -> %s" % (srcs, dst))


def mktemp(host, tag):
    rc, out = ssh(host, "umask 077; mktemp -d /tmp/%s.XXXXXX" % tag, capture=True)
    d = out.strip()
    if rc or not d.startswith("/tmp/"):
        sys.exit("cannot create a temp dir on %s (rc=%s)" % (host, rc))
    return d


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def remote_sha_ok(host, d, names):
    """The remote copy must be byte-identical to the repo, or the run is not
    running what we think it is (the 225 /tmp copy was a stale version)."""
    rc, out = ssh(host, "cd %s && sha256sum %s" % (q(d), " ".join(names)), capture=True)
    got = dict(reversed(l.split()) for l in out.splitlines() if l.strip())
    bad = [n for n in names if got.get(n) != sha(os.path.join(HERE, n))]
    if rc or bad:
        sys.exit("sha mismatch on %s for %s" % (host, bad))


def onboard_sub(cmd, d198):
    """Run a sub2api-grok-onboard.py subcommand on 198 from a temp dir.

    Returns (rc, output).  S2A_REMOTE_DIR keeps delegate() off the resident
    copy that the park-patrol cron executes."""
    env = dict(os.environ, S2A_REMOTE_DIR=d198, S2A_HOST=H198)
    env.pop("S2A_LOCAL", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, ONBOARD)] + cmd,
                       env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def served(out):
    m = re.search(r"--> (\d+) of (\d+) legs actually served traffic", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def ok_names(capture_out):
    """Emails the capture script itself judged usable (its `  OK   <email>` tail).

    A bundle is written even when the x.ai verdict is not 200 (it carries the
    probe for diagnosis), so shipping every bundle-*.json would create legs
    that were already known to be dead."""
    tail = capture_out.split("captured with a usable token ===", 1)
    if len(tail) < 2:
        return []
    return re.findall(r"^\s+OK\s+(\S+@\S+)\s*$", tail[1], re.M)


def load_parse():
    spec = importlib.util.spec_from_file_location("cap", os.path.join(HERE, CAPTURE))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.parse_creds


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("creds")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + baseline + both hosts reachable with matching sha; "
                         "capture nothing, write nothing")
    ap.add_argument("--wait", type=int, default=360,
                    help="seconds between creating and the regression (default 360)")
    ap.add_argument("--minutes", type=int, default=15, help="regress window")
    ap.add_argument("--model-map-from", default=os.environ.get("MODEL_MAP_FROM", "61"))
    a = ap.parse_args()

    creds = load_parse()(a.creds)
    if not creds:
        sys.exit("no credential lines in %s" % a.creds)
    if os.path.abspath(a.creds).startswith(os.path.dirname(os.path.dirname(HERE)) + os.sep):
        log("⚠️  %s is inside the repo -- move it out (it is a credential file)" % a.creds)
    log("=== 0/4 parse: %d line(s) ===" % len(creds))
    for c in creds:
        log("  %-34s sso=%d chars" % (c["name"], len(c["sso"])))

    rc = 0
    d225 = d198 = None
    try:
        d198 = mktemp(H198, "grok-onboard")
        d225 = mktemp(H225, "grok-capture")
        scp([os.path.join(HERE, n) for n in (ONBOARD, HELPER, CREATE)],
            "%s:%s/" % (H198, d198))
        scp([os.path.join(HERE, CAPTURE)], "%s:%s/" % (H225, d225))
        remote_sha_ok(H198, d198, [ONBOARD, HELPER, CREATE])
        remote_sha_ok(H225, d225, [CAPTURE])
        log("    198:%s  225:%s  (sha == repo)" % (d198, d225))

        log("\n=== 1/4 health BASELINE ===")
        hrc, hout = onboard_sub(["health"], d198)
        before = served(hout)
        if hrc or not before:
            log(hout[-3000:])
            sys.exit("baseline health failed (rc=%s) -- refusing to go on" % hrc)
        log("  %d of %d legs actually served traffic (before)" % before)
        for l in hout.splitlines():
            if "phase=routing" in l:
                log("  " + l.strip() + "   <- pre-existing, not ours")

        if a.dry_run:
            log("\n--dry-run: stopping before capture. both hosts OK.")
            return 0

        log("\n=== 2/4 capture on 225 (WARP) ===")
        scp([a.creds], "%s:%s/creds.txt" % (H225, d225))
        crc, cout = ssh(H225, "cd %s && chmod 600 creds.txt && python3 %s "
                        "--creds creds.txt --out-dir %s/bundles 2>&1"
                        % (q(d225), CAPTURE, q(d225)), capture=True)
        log(cout.rstrip())
        good = ok_names(cout)
        rc |= 1 if crc else 0
        if not good:
            sys.exit("no usable bundle -- nothing to create")

        log("\n=== 3/4 create on 198 (%d leg(s)) ===" % len(good))
        files = ["bundle-%s.json" % n.split("@")[0] for n in good]
        subprocess.run(["scp", "-q", "-3"] +
                       ["%s:%s/bundles/%s" % (H225, d225, f) for f in files] +
                       ["%s:%s/" % (H198, d198)], check=True)
        mrc, _ = ssh(H198, (
            "cd {d} && chmod 600 bundle-*.json && "
            "sudo -n sh -c 'umask 077; kubectl -n litellm-dev get secret sub2api-secrets "
            "-o jsonpath={{.data.ADMIN_PASSWORD}} | base64 -d > ./.pw' && "
            "sudo -n env MODEL_MAP_FROM={m} S2A_PW_FILE={d}/.pw python3 {c} {f}"
        ).format(d=q(d198), m=q(a.model_map_from), c=CREATE, f=" ".join(files)))
        rc |= 1 if mrc else 0
        if mrc:
            log("!! create exited %d -- read the readback above" % mrc)

        log("\n=== 4/4 wait %ds, then regress + health ===" % a.wait)
        time.sleep(a.wait)
        rrc, rout = onboard_sub(["regress", "--minutes", str(a.minutes)], d198)
        log(rout.rstrip()[-4000:])
        rc |= 1 if rrc else 0
        _, hout2 = onboard_sub(["health"], d198)
        after = served(hout2)
        for l in hout2.splitlines():
            if any(("%s " % n[:20]) in l for n in good) or "phase=" in l:
                log("  " + l.strip())
        log("\n  served legs: before %s  ->  after %s"
            % ("%d/%d" % before, "%d/%d" % after if after else "?"))
        log("  ⚠️ a new leg with 0 req and first-pick=0 is only a failover candidate "
            "yet (skill §C4) -- re-run health, do not recreate it")
    finally:
        for host, d in ((H225, d225), (H198, d198)):
            if d:
                ssh(host, "sudo -n shred -u %s/.pw 2>/dev/null; "
                          "find %s -type f -exec shred -u {} + 2>/dev/null; rm -rf %s"
                          % (q(d), q(d), q(d)))
                log("  cleaned %s:%s" % (host, d))
    log("\nEXIT=%d  ⛔ shred your local creds file: shred -u %s" % (rc, a.creds))
    return rc


if __name__ == "__main__":
    sys.exit(main())
