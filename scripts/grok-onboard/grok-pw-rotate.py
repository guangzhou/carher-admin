#!/usr/bin/env python3
"""grok-pw-rotate.py — 换一个 grok 号的 x.ai 登录密码，一条命令：生成 → 先写飞书表 → 改 → 回验。

    cd <repo>/scripts/grok-onboard
    python3 grok-pw-rotate.py --creds ~/x/one.txt --dry-run   # parse + 开弹窗 + 不提交
    python3 grok-pw-rotate.py --creds ~/x/one.txt             # 真改，约 4 分钟

凭据行 `email----x.ai密码----sso`（同 `grok-warp-onboard.py` 的 SHORT 形状）。

顺序是硬的，别调：**先把新密码写进飞书表，再去改**。
改密码不幂等（[[feedback_irreversible_write_must_commit_before_return]] 同族）：
先改后写，中间挂一下 = 新密码只存在于一个已经退出的进程里，号变砖。
所以 `--dry-run` 也会真写表吗？不会 —— dry 全程不写表、不提交表单。

每一步的判据（都不是「返回码 0」）：
  1 表写完 **逐字回读**（账号/旧密码/新密码三项相等）才往下走
  2 `xai_change_pw.py` 的 stdout 末行必须是 `RESULT SUBMITTED bad_words=[] dialog_still_open=False`
  3 新密码探针 `state=LOGGED_IN sso_cookie=True`
  4 🔴 旧密码探针**必须** `state=BADPW`（阴性对照；只测新密码能登会放过「其实没改成」）

🔴 2026-10-21 实测：x.ai 改密码**不吊销** sso / OAuth refresh_token ——
改完旧 sso 仍 200、sub2api `refresh` 正常、腿照收流量。所以**不需要**停腿或先 reauth。
这条结论只在 x.ai 成立，别套到别的站。

⛔ 密码/sso 的足迹：本地只在 0600 的 state/<email>.json 与 pw.json 里（跑完 `rm -P`），
远端在 mktemp 目录里（finally 里 shred），两边都不进 argv、不进日志。
"""
import argparse
import json
import os
import re
import secrets
import shlex
import string
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
H225 = os.environ.get("XAI_HOST", "cltx@10.68.13.225")
PYENV_225 = "/Data/grok-onboard/pyenv/local/lib/python3.14/dist-packages"
BROWSERS_225 = "/Data/grok-onboard/browsers"
CHANGE = "xai_change_pw.py"
PROBE = "xai_pw_probe.py"
CAPTURE = "xai_device_capture.py"      # PROBE 复用它的 do_login
SSH = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
       "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
# 飞书 grok 账号表的 base/table/列名都在 grok_bitable.py
COL_OLD, COL_NEW, COL_MAIL = "Grok密码", "新Grok密码", "账号"
SYMBOLS = "!@#%^*_+="   # ⛔ 没有 $ `' " \ - 空格：少一类字符换「永不被某层 shell/json 吃掉」


def log(*a):
    print(*a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def gen_pw(n=20):
    """4 类字符各至少一个。⛔ 别用 `-`/引号/反斜杠这类字符：
    中途要过 shell / json / 表单，踩过 CJK+变量那类坑就知道省不得。"""
    alpha = string.ascii_letters + string.digits + SYMBOLS
    while True:
        pw = "".join(secrets.choice(alpha) for _ in range(n))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in SYMBOLS for c in pw)):
            return pw


def parse_creds(path):
    out = []
    for i, raw in enumerate(open(path, encoding="utf-8-sig").read().splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        f = [x.strip() for x in re.split(r"-{4,}", raw)]
        if len(f) < 3 or "@" not in f[0]:
            sys.exit("line %d: want `email----x.ai_pw----sso`" % i)
        if not f[-1].startswith("eyJ"):
            sys.exit("line %d: last field is not a JWT sso -- password-only lines "
                     "must first get an sso (grok-warp-onboard.py --password-login)" % i)
        out.append({"email": f[0], "old_pw": f[1], "sso": f[-1]})
    if not out:
        sys.exit("no credential lines")
    return out


# ---------------------------------------------------------------- 飞书表
# 读写全走 grok_bitable.py（和 grok-warp-onboard.py 共用一份，别再各写一套 lark 调用）
sys.path.insert(0, HERE)
import grok_bitable as gb  # noqa: E402


def ensure_new_col():
    names = gb.field_names()
    for need in (COL_MAIL, COL_OLD):
        if need not in names:
            sys.exit("table has no column %r -- wrong table?" % need)
    if COL_NEW in names:
        return False
    gb.lark("POST", gb.REC + "/fields", data={"field_name": COL_NEW, "type": 1})
    log("  + added column %s" % COL_NEW)
    return True


def write_pw(acct, extra=None):
    """写表并逐字回读账号/旧密码/新密码三列；不等 ⇒ 退出，不去改密码。"""
    fields = {COL_OLD: acct["old_pw"], COL_NEW: acct["new_pw"]}
    fields.update(extra or {})
    return gb.upsert(acct["email"], fields, verify=[COL_MAIL, COL_OLD, COL_NEW])


# ---------------------------------------------------------------- 225
def ssh225(cmd, timeout=900):
    return run(SSH + [H225, cmd], timeout=timeout)


def remote_step(acct, script, mode, label):
    """把脚本+凭据丢进 225 的 mktemp 目录，私有 Xvfb 跑一发，finally 里 shred。"""
    r = ssh225("mktemp -d /tmp/xai-pw.XXXXXX", timeout=60)
    if r.returncode:
        sys.exit("mktemp on 225 failed: %s" % r.stderr[:200])
    d = r.stdout.strip()
    local = []
    try:
        for name, payload in (("sso.txt", acct["sso"]),
                              ("pw.json", json.dumps({"old_pw": acct["old_pw"],
                                                      "new_pw": acct["new_pw"]}))):
            p = os.path.join(HERE, "." + name)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            local.append((p, name))
        send = [p for p, _ in local] + [os.path.join(HERE, n) for n in (script, CAPTURE)]
        r = run(["scp", "-q", "-o", "ConnectTimeout=10"] + send + ["%s:%s/" % (H225, d)])
        if r.returncode:
            sys.exit("scp to 225 failed: %s" % r.stderr[:200])
        for p, name in local:
            ssh225("mv %s/%s %s/%s" % (shlex.quote(d), os.path.basename(p),
                                       shlex.quote(d), name), timeout=60)
        # 私有 Xvfb：只占 80~89 里第一个空的；:91/:95~99 是别的任务的，永不碰
        # 🔴 SCREENSHOT_DIR/OAUTH_OUTPUT 必须给：xai_device_capture import 期就 makedirs("/work/…")
        cmd = ("cd {d} && chmod 600 sso.txt pw.json && mkdir -m700 -p shots && "
               "for n in $(seq 80 89); do [ -e /tmp/.X11-unix/X$n ] || {{ DN=$n; break; }}; done; "
               "[ -n \"$DN\" ] || {{ echo 'no free X display 80-89'; exit 3; }}; "
               "Xvfb :$DN -screen 0 1440x1000x24 -nolisten tcp >/dev/null 2>&1 & XP=$!; "
               "for i in $(seq 1 20); do [ -S /tmp/.X11-unix/X$DN ] && break; sleep 1; done; "
               "DISPLAY=:$DN MODE={m} EMAIL={e} SSO_FILE=sso.txt PW_FILE=pw.json "
               "SHOTS={d}/shots SCREENSHOT_DIR={d}/shots OAUTH_OUTPUT={d}/unused.json "
               "PYTHONPATH={pp} PLAYWRIGHT_BROWSERS_PATH={bp} "
               "timeout 600 python3 {s} 2>&1; rc=$?; kill $XP 2>/dev/null; exit $rc"
               ).format(d=shlex.quote(d), m=shlex.quote(mode), e=shlex.quote(acct["email"]),
                        s=shlex.quote(script), pp=shlex.quote(PYENV_225),
                        bp=shlex.quote(BROWSERS_225))
        r = ssh225(cmd)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            if re.match(r"^(RESULT|=====|\[|STOP|Traceback)", line.strip()):
                log("    " + line.rstrip()[:220])
        res = ([l.strip() for l in out.splitlines() if l.strip().startswith("RESULT")] or [""])[-1]
        if not res:
            log(out[-1500:])
            sys.exit("%s: no RESULT line (rc=%s) -- see output above" % (label, r.returncode))
        return res
    finally:
        for p, _ in local:
            if os.path.exists(p):
                os.unlink(p)
        ssh225("find %s -type f -exec shred -u {} + 2>/dev/null; rm -rf %s"
               % (shlex.quote(d), shlex.quote(d)), timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", required=True, help="0600 file, `email----x.ai_pw----sso`")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + open the dialog; writes nothing, submits nothing")
    ap.add_argument("--state-dir", default=os.path.join(REPO, ".work", "grok-pw-rotate"))
    ap.add_argument("--len", type=int, default=20)
    a = ap.parse_args()

    accts = parse_creds(a.creds)
    os.makedirs(a.state_dir, mode=0o700, exist_ok=True)
    log("=== 0/5 parse: %d account(s)%s ===" % (len(accts), " [dry-run]" if a.dry_run else ""))
    for c in accts:
        c["new_pw"] = gen_pw(a.len)
        log("  %-34s new pw %d chars" % (c["email"], len(c["new_pw"])))

    if not a.dry_run:
        log("\n=== 1/5 bitable column ===")
        ensure_new_col()

    rc = 0
    for c in accts:
        log("\n=== %s ===" % c["email"])
        st = os.path.join(a.state_dir, re.sub(r"[^\w.@-]", "_", c["email"]) + ".json")

        if a.dry_run:
            res = remote_step(c, CHANGE, "dry", "dry change")
            log("  %s" % res)
            if not res.startswith("RESULT DRY_OK"):
                rc |= 1
            continue

        log("  2/5 write the new password to the table FIRST")
        write_pw(c, extra={"Grok SSO": c["sso"]})
        fd = os.open(st, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"email": c["email"], "old_pw": c["old_pw"], "new_pw": c["new_pw"],
                       "step": "table_written", "at": time.strftime("%FT%T")}, fh)

        log("  3/5 change it on accounts.x.ai")
        res = remote_step(c, CHANGE, "apply", "change")
        if "RESULT SUBMITTED bad_words=[] dialog_still_open=False" not in res:
            log("  🔴 STOP %s: %s" % (c["email"], res))
            log("     新密码已在表里；号现在可能是旧密码也可能是新密码 ⇒ "
                "先跑两发探针判到底是哪一个，再决定重试")
            rc |= 1
            continue

        log("  4/5 probe: new password must log in")
        pn = remote_step(c, PROBE, "new", "probe new")
        log("  5/5 probe: OLD password must be refused (negative control)")
        po = remote_step(c, PROBE, "old", "probe old")
        ok_new = "state=LOGGED_IN" in pn and "sso_cookie=True" in pn
        ok_old = "state=BADPW" in po
        if ok_new and ok_old:
            log("  ✅ %s rotated (new logs in, old refused)" % c["email"])
            os.unlink(st)
            # 10-23：165 改完密码 Sub2API账户ID/Grok UserID 还是空的，是手补的 ⇒ 收尾顺手填空
            gb.fill_ids(c["email"])
        else:
            log("  🔴 %s verdict unclear: new_ok=%s old_refused=%s" % (c["email"], ok_new, ok_old))
            log("     ⛔ 别重跑整条：旧密码可能已经失效，再拿它去改必失败。state 留在 %s" % st)
            rc |= 1

    log("\n⛔ shred your creds file: rm -P %s" % a.creds)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
