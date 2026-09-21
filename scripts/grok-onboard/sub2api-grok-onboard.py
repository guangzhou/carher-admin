#!/usr/bin/env python3
"""Add grok subscription accounts to sub2api on 198, and prove they work.

RUN IT FROM THE MAC, with nothing but a path:

  python3 sub2api-grok-onboard.py ~/Downloads/grok-mima/卡密导出.txt

No local sudo, no S2A_PW_FILE, no subcommand.  When /etc/rancher/k3s/k3s.yaml
is absent (i.e. not on 198) the script ships itself + the helper + the creds
file to $S2A_HOST (default cltx@10.68.13.198), runs it there under `sudo -n`,
streams the output back, shreds the remote creds copy, and exits with the
remote exit code.  S2A_LOCAL=1 forces
local execution; on 198 nothing is shipped and it just runs.

The admin password is fetched from the k8s secret by the script itself.
Secrets never go in argv: the creds file path is an argument, its contents are
not.

WHY THIS EXISTS (2026-09-09).  Onboarding three accounts by hand meant writing
four throwaway scripts under /root, and three of the four steps had a way to
lie to me:

  1. The credential lines the user pastes are `----`-delimited and field 4 is a
     DECOY -- it is a Cursor session token (iss=authentication.cursor.sh), not
     a grok one.  The grok userid and sso are fields 7 and 8.  Feeding field 4
     to the SSO endpoint fails in a way that reads like "this account is bad".
  2. `POST /grok/sso-to-oauth` answers 200 with `data.created = [null]`.  The
     API cannot testify about itself: the only proof an account exists, is
     schedulable, carries the concurrency you asked for and is bound to the
     right group, is the sub2api postgres.
  3. "All three requests succeeded" is not proof the new legs are being USED.
     The pool will happily serve every one of them off the OLD accounts and
     look perfectly green.  D1 below judges on the per-account scheduling
     distribution instead.

The free cross-check that catches (1) immediately: the `sso-token` endpoint
returns `data.sub`, which must equal field 7 of the same line, verbatim.

  # 0. ONE COMMAND: baseline, verify, create, x.ai verdict, regression
  python3 sub2api-grok-onboard.py /path/to/creds.txt

  # 0b. BASELINE FIRST -- how many legs actually serve right now?
  sudo python3 sub2api-grok-onboard.py health

  # ⚡ "MY GROK GOT STOPPED AGAIN" -- this is the whole answer, and it is not
  #    a reauth.  Classifies every leg by a LIVE x.ai probe, frees the ones
  #    x.ai still honours (clear-error + bulk-update), reads postgres back,
  #    and names the second hand if another session is flipping gates too.
  sudo python3 sub2api-grok-onboard.py rescue --dry-run   # look first
  sudo python3 sub2api-grok-onboard.py rescue             # then free them

  # 1. see what the paste decodes to -- zero network, zero risk
  sudo python3 sub2api-grok-onboard.py parse   /root/.creds.txt

  # 2. verify the sso tokens WITHOUT creating anything
  sudo python3 sub2api-grok-onboard.py verify  /root/.creds.txt

  # 3. create + read back from postgres
  sudo python3 sub2api-grok-onboard.py add     /root/.creds.txt --concurrency 200

  # 4. per-account live probe (bypasses the pool)
  sudo python3 sub2api-grok-onboard.py quota

  # 5. two-layer regression (D1 scheduling + D2 user plane)
  sudo python3 sub2api-grok-onboard.py regress

  # 6. shred the creds file yourself when done -- this script never deletes it

CAVEAT on `quota`: on 2026-09-09 accounts 6/7/8/9 returned st=200 with plan /
limit_tokens / limit_requests all None, while freshly-created 18/19/20 returned
full readings.  Those four were serving hundreds of real requests at the time,
so the None is a property of the RULER, not evidence the legs are dead.  Do not
report a None row as an outage.  Judge liveness from usage_logs.

CAVEAT on concurrency: this sets the ACCOUNT gate.  When the symptom is
"concurrency limit exceeded", the gate that is actually full is almost always
the USER gate, and raising this one does nothing.  See
skill `sub2api-concurrency-gate` / scripts/sub2api-concurrency.sh.

WHY `health` EXISTS (2026-09-17).  Adding two accounts landed in the middle of
an outage nobody had reported: 6 of the 7 existing legs were serving nothing,
and the pool was answering 503 with `error_phase=routing` / `account_id NULL`
-- the request never left the box.  Two things made that easy to misread:

  * `schedulable=t` does NOT mean a leg serves.  Someone flipped 6/7/8/9/18/20
    back to `t` at 11:00; 14 minutes later they still had zero usage_logs rows
    and zero upstream attempts.  The only ruler for "this leg serves" is
    usage_logs / a non-NULL account_id in ops_error_logs.
  * The onboarding regression runs right after the change, so a pre-existing
    outage inside the same window reads as "I broke it".  `health` gives you a
    BEFORE reading, which is what separates the two.

Run `health` before touching anything, and again at the end.  If it says only N
legs serve and N is small, say so -- do not let it hide behind "2/2 created".

WHY THE first-pick COLUMN EXISTS (2026-09-17, second batch).  The three accounts
added at 12:31 read zero usage_logs for ~4 minutes while the pool was healthy,
which looks exactly like "the new legs are broken".  It is not: a fresh leg is
first offered work only as a FAILOVER CANDIDATE, and a candidate inherits a
request that already failed on every leg ahead of it, so it fails too and writes
no usage row.  `health` and `regress` therefore both print, per leg, how many
times the scheduler picked it and how many of those were switch_count==1.

  first-pick > 0 + zero usage rows -> it was chosen and failed. A real fault.
  first-pick = 0 + picked > 0      -> candidate only. Zero rows is EXPECTED.
  picked = 0                       -> never offered work; a quiet window is
                                      indistinguishable from this. Re-run.

Measured that day: 29/30/31 had switch_count 2/3/4 only (12/12/11 occurrences,
zero at 1) while acct 27 had 20 at switch_count=1.

EXIT CODE: `regress` exits 1 when the D2 user-plane probe fails, so it can gate
a deploy.  D1's idle legs do NOT set it -- idleness is ambiguous by construction
(see the three cases above) and an ambiguous signal must not fail a gate.
"""
import argparse
import atexit
import datetime
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
DEV_NS = "litellm-dev"
PROD_NS = "litellm-product"
GROK_GROUP = 7
GROK_ENTRIES = ["sa-grok-4.5", "sa-grok-4.6", "sa-grok-4.20"]

REMOTE = os.environ.get("S2A_HOST", "cltx@10.68.13.198")
REMOTE_DIR = os.environ.get("S2A_REMOTE_DIR", "/home/cltx/grok-onboard")


def on_198():
    """Are we on the box that can actually reach sub2api?

    The ruler is the k3s kubeconfig, not `which kubectl`: kubectl is installed
    on the Mac too, so it would answer yes in both places and the script would
    try to run locally and fail at the first `kubectl -n litellm-dev`.
    The path is world-readable on 198 (checked as cltx, not just as root), so
    this does not need sudo to answer.
    """
    return os.path.exists("/etc/rancher/k3s/k3s.yaml")


def delegate(argv):
    """Re-run ourselves on 198 over ssh, so the Mac can drive it with a path.

    Ships the two code files (the helper too -- the script `exec`s it and
    crashes without it), ships any argument that is an existing local file as
    a 0600 copy, rewrites those arguments to the remote paths, and shreds the
    copies afterwards whatever happens.

    Output is NOT captured: ssh inherits stdout/stderr so a 5-minute run prints
    as it goes instead of arriving in one lump at the end.  The remote exit
    code is returned unchanged -- it is the whole verdict of the run.
    """
    q = shlex.quote
    me = os.path.basename(os.path.abspath(__file__))
    # flush=True on every local print in here: stdout is block-buffered when it
    # is not a tty, while ssh writes to the inherited fd directly, so without
    # this the banner and the "shipped X" lines land AFTER the remote output.
    print("--- not on 198 (no /etc/rancher/k3s/k3s.yaml) -> running there as %s ---"
          % REMOTE, flush=True)

    def ssh(cmd, **kw):
        return subprocess.run(["ssh", "-n", REMOTE, cmd], **kw)

    if ssh("mkdir -p %s && chmod 700 %s" % (q(REMOTE_DIR), q(REMOTE_DIR))).returncode:
        sys.exit("cannot reach %s over ssh" % REMOTE)
    code = [os.path.join(HERE, me), os.path.join(HERE, "sub2api_admin.py")]
    if subprocess.run(["scp", "-q"] + code + ["%s:%s/" % (REMOTE, REMOTE_DIR)]).returncode:
        sys.exit("scp of the script failed")

    shipped, out = [], []
    for a in argv:
        if os.path.isfile(a):
            rp = "%s/.creds-%d-%d.txt" % (REMOTE_DIR, os.getpid(), len(shipped))
            if subprocess.run(["scp", "-q", a, "%s:%s" % (REMOTE, rp)]).returncode:
                sys.exit("scp of %s failed" % a)
            ssh("chmod 600 %s" % q(rp))
            shipped.append(rp)
            out.append(rp)
            print("    %s -> %s (0600, shredded after the run)" % (a, rp), flush=True)
        else:
            out.append(a)
    try:
        # sudo -n: never prompt.  A prompt over ssh -n would hang forever and
        # look like the script froze partway through a run that writes.
        cmd = "cd %s && sudo -n python3 %s/%s %s" % (
            q(REMOTE_DIR), q(REMOTE_DIR), q(me), " ".join(q(x) for x in out))
        sys.stdout.flush()
        return subprocess.run(["ssh", REMOTE, cmd]).returncode
    finally:
        for rp in shipped:
            ssh("shred -u %s 2>/dev/null || rm -f %s" % (q(rp), q(rp)))


# The helper's docstring also contains the literal `if __name__`, so split()
# would cut the file in half.  rsplit takes the real one at the bottom.
_helper = open(os.path.join(HERE, "sub2api_admin.py")).read().rsplit("if __name__", 1)[0]


def _ensure_pw():
    """Fetch the admin password ourselves, so the caller only passes a path.

    Doing it by hand is three steps (kubectl get secret / S2A_PW_FILE= / rm)
    that are identical every single time, and forgetting the env var fails
    inside the helper's *import* as `assert _st == 200` -- it reads as a login
    failure, not as a missing variable, which is the wrong thing to go debug.

    An explicit S2A_PW_FILE that exists still wins, so the park-patrol cron
    (which stages its own /run/.s2apw-patrol) keeps its current behaviour and
    never pays for a kubectl call.
    """
    pwf = os.environ.get("S2A_PW_FILE")
    if pwf and os.path.exists(pwf):
        return
    pw = sh("sudo kubectl -n %s get secret sub2api-secrets "
            "-o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d" % DEV_NS).strip()
    if not pw:
        sys.exit("secret %s/sub2api-secrets key ADMIN_PASSWORD is empty -- "
                 "cannot log in" % DEV_NS)
    d = "/run" if os.access("/run", os.W_OK) else None
    fd, tmp = tempfile.mkstemp(prefix=".s2apw-", dir=d)
    os.fchmod(fd, 0o600)          # before the write, not after
    os.write(fd, pw.encode())
    os.close(fd)
    os.environ["S2A_PW_FILE"] = tmp
    atexit.register(lambda: os.path.exists(tmp) and os.unlink(tmp))


def _load_helper():
    _ensure_pw()
    ns = {}
    exec(compile(_helper, "sub2api_admin.py", "exec"), ns)
    return ns["call"], ns["TOK"]


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("command failed: %s\n%s" % (cmd, p.stderr[:400]))
    return p.stdout


def pg(sql, ns=DEV_NS, user="sub2api", db=None, pod=None):
    """Run SQL in a postgres pod and return rows as lists of strings."""
    if pod is None:
        # --field-selector is not optional: the ns keeps Succeeded pods around
        # and `-o jsonpath='{.items[0]...}'` happily hands you one, which then
        # fails at exec time with a message about phase, not about selection.
        pod = sh("sudo kubectl -n %s get pod -l app=sub2api-postgres "
                 "--field-selector=status.phase=Running "
                 "-o jsonpath='{.items[0].metadata.name}'" % ns).strip()
        if not pod:
            pod = sh("sudo kubectl -n %s get pod --field-selector=status.phase=Running "
                     "-o name | grep -m1 postgres" % ns).strip().split("/")[-1]
        if not pod:
            sys.exit("no Running postgres pod in ns %s" % ns)
    dbarg = " -d %s" % db if db else ""
    # SQL goes through a file, not -c: quoting through ssh -> kubectl -> sh
    # mangles it (a bare `interval 12 hour` becomes a syntax error).
    tmp = "/tmp/.s2a-q-%d.sql" % random.randint(10**6, 10**7)
    open(tmp, "w").write(sql)
    sh("sudo kubectl -n %s cp %s %s:%s" % (ns, tmp, pod, tmp))
    out = sh("sudo kubectl -n %s exec %s -- psql -U %s%s -t -A -F'|' -f %s"
             % (ns, pod, user, dbarg, tmp))
    sh("sudo kubectl -n %s exec %s -- rm -f %s" % (ns, pod, tmp))
    os.unlink(tmp)
    return [r.split("|") for r in out.strip().splitlines() if r.strip()]


def xai_verdict(access_token):
    """Ask x.ai itself whether this stored token still buys anything.

    This is the only ruler that separated the two faults on 2026-09-18.  Four
    legs looked identical to a dead leg from inside sub2api -- schedulable=f,
    quota endpoint 502 GROK_QUOTA_TOKEN_UNAVAILABLE, zero usage_logs -- and a
    freshly exchanged SSO token did not fix them, because the token was fine and
    the SUBSCRIPTION was not:

        403 personal-team-blocked:spending-limit
        "You have run out of credits or need a Grok subscription."

    Re-authenticating that is a no-op; only paying xAI is.  Run from the 198
    host, NOT from inside the sub2api pod (BusyBox wget cannot show the body,
    and the whole verdict lives in the body).
    """
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        "https://api.x.ai/v1/models",
        headers={"Authorization": "Bearer " + access_token})
    try:
        urllib.request.urlopen(req, timeout=25)
        return "200 usable"
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            code = json.loads(raw).get("code") or ""
        except Exception:
            code = raw[:60]
        return "%s %s" % (e.code, code)
    except Exception as e:                 # never 2>/dev/null this away
        return "ERR %s" % type(e).__name__


def s2a_pod():
    pod = sh("sudo kubectl -n %s get pod -l app=sub2api "
             "--field-selector=status.phase=Running "
             "-o jsonpath='{.items[0].metadata.name}'" % DEV_NS).strip()
    if not pod:
        sys.exit("no Running sub2api pod in ns %s" % DEV_NS)
    return pod


def failover_picks(mins):
    """Read the scheduler's own account picks out of the sub2api log.

    Returns (picks, first, chains):
      picks[aid]  = how many times the scheduler handed a request to that leg
      first[aid]  = how many of those were switch_count==1, i.e. FIRST pick
      chains[rid] = [(account_id, upstream_status), ...] for one request_id,
                    in the order the scheduler tried the legs

    chains is keyed by request_id on purpose.  "Every leg returned 422 somewhere
    in this 30-minute window" is NOT the all-legs-422 shape -- with real traffic
    every leg collects a stray 422 and that reads red while callers are fine
    (measured 2026-09-17: 1478 ok / 3 fail while all six legs had a 422).  The
    shape that means "the REQUEST is being refused" is every leg failing the
    SAME request_id, which is what the skill says and what this now groups on.

    first==0 is the discriminator for "this leg's zero usage_logs is benign":
    a leg that is only ever a failover candidate inherits a request that has
    already failed on every leg ahead of it, so it fails too and writes no
    usage row.  See §C4 of the skill.

    No `2>/dev/null` and no `|| true` here on purpose: with them a broken
    kubectl/pod lookup reads as "no failover events", which is the wrong
    direction -- that exact shape cost three polling rounds on 2026-09-17.
    Filtering happens in Python so a real command failure still exits loudly.
    """
    log = sh("sudo kubectl -n %s logs %s --since=%dm" % (DEV_NS, s2a_pod(), mins))
    picks, first, chains = {}, {}, {}
    for line in log.splitlines():
        if "upstream_failover_switching" not in line:
            continue
        # Match each key on its own: one regex spanning all three keys breaks
        # the moment the emitter reorders them or changes the spacing, and the
        # status key is spelled `upstream_status` in the log but
        # `upstream_status_code` in ops_error_logs -- accept either.
        m_a = re.search(r'"account_id":\s*(\d+)', line)
        m_s = re.search(r'"switch_count":\s*(\d+)', line)
        if not (m_a and m_s):
            continue
        aid, sc = m_a.group(1), int(m_s.group(1))
        picks[aid] = picks.get(aid, 0) + 1
        if sc == 1:
            first[aid] = first.get(aid, 0) + 1
        m_st = re.search(r'"upstream_status(?:_code)?":\s*(\d+)', line)
        m_r = re.search(r'"request_id":\s*"([^"]+)"', line)
        if m_r:
            chains.setdefault(m_r.group(1), []).append(
                (aid, m_st.group(1) if m_st else "?"))
    return picks, first, chains


# ---------------------------------------------------------------- health

def cmd_health(a):
    """Baseline: how many legs actually SERVE, and is the pool routing at all?

    Deliberately does not trust `schedulable`, `status`, or the quota probe --
    all three read green on legs that serve nothing.  Judges on usage_logs and
    on whether ops_error_logs is producing routing-phase rows.
    """
    mins = a.minutes
    print("=== grok legs: config state vs. what they actually served (%dmin) ===" % mins)
    rows = pg("""SELECT a.id, a.name, a.status, a.schedulable,
                        coalesce(to_char(a.rate_limit_reset_at,'MM-DD HH24:MI'),'-'),
                        coalesce((SELECT count(*)::text FROM usage_logs u
                                  WHERE u.account_id = a.id
                                    AND u.created_at > now() - interval '%d minutes'),'0')
                 FROM accounts a WHERE a.platform='grok'
                   AND a.deleted_at IS NULL ORDER BY a.id;""" % mins)
    serving = []
    for r in rows:
        aid, name, st, sched, reset, n = r[0], r[1], r[2], r[3], r[4], r[5]
        if n != "0":
            serving.append(aid)
        # `schedulable=t` + zero traffic is the shape that fooled me on 09-17.
        flag = "SERVING" if n != "0" else ("IDLE (sched=t!)" if pgbool(sched) else "idle")
        print("  acct %-4s %-34s %-7s sched=%s rl_reset=%-12s %6s req  %s"
              % (aid, name[:34], st, sched, reset, n, flag))
    print("\n  --> %d of %d legs actually served traffic" % (len(serving), len(rows)))
    if len(serving) < 3:
        print("  !! THIN POOL. Say this out loud before/after any change; a small")
        print("     pool is one rate-limit away from the routing-503 outage below.")

    if a.xai:
        # Two faults look identical from inside sub2api; only x.ai tells them
        # apart.  See xai_verdict().  Off by default because it spends one
        # request per leg against the real upstream.
        print("\n=== does x.ai still honour each stored token? (--xai) ===")
        toks = pg("SELECT id::text, coalesce(credentials::jsonb->>'access_token','') "
                  "FROM accounts WHERE platform='grok' AND deleted_at IS NULL "
                  "ORDER BY id;")
        for aid, tok in [(r[0], r[1]) for r in toks]:
            v = "no access_token stored" if not tok else xai_verdict(tok)
            note = ("  <-- xAI SUBSCRIPTION/CREDIT problem: re-auth is a NO-OP, "
                    "only paying fixes it" if "spending-limit" in v else "")
            print("  acct %-4s %s%s" % (aid, v, note))
        print("\n  403 personal-team-blocked:spending-limit = out of credits.")
        print("  A fresh SSO token exchanges fine (200, tier=supergrok_heavy) and")
        print("  still gets this 403 -- tier in the token is NOT proof of credit.")

    print("\n=== is the pool failing to route at all? (routing != upstream) ===")
    er = pg("""SELECT error_phase, coalesce(account_id::text,'NULL'), count(*)::text
               FROM ops_error_logs
               WHERE created_at > now() - interval '%d minutes' AND platform='grok'
               GROUP BY 1,2 ORDER BY 3::int DESC LIMIT 6;""" % mins)
    if not er:
        print("  no grok error rows in the window")
    for phase, aid, n in [(r[0], r[1], r[2]) for r in er]:
        note = ""
        if phase == "routing" and aid == "NULL":
            note = "  <-- NEVER LEFT THE BOX: no leg was picked. This is an OUTAGE."
        print("  phase=%-14s account_id=%-6s %8s%s" % (phase, aid, n, note))
    print("\n  routing + account_id NULL  = pool found no usable leg (503 to callers)")
    print("  upstream + real account_id = the leg was picked, x.ai refused")
    print("  These are different faults. Adding accounts fixes only the first.")

    # A leg with zero usage_logs is NOT necessarily broken.  On 09-17 the three
    # accounts added at 12:31 read zero for ~4min while the pool was healthy,
    # because a fresh leg is only tried as a FAILOVER CANDIDATE at first -- and
    # a candidate inherits a request that has already failed on every leg ahead
    # of it, so it fails too and writes no usage row.  The discriminating signal
    # is whether the leg ever appears with switch_count=1 (= chosen first).
    print("\n=== has each leg ever been chosen FIRST? (switch_count=1) ===")
    picks, first, _ = failover_picks(mins)
    if not picks:
        print("  no failover events in the window -- nothing to judge here.")
        print("  That is NOT evidence the legs are fine: a pool that never")
        print("  routes (routing/NULL above) produces no failover lines either.")
    for aid in sorted(picks, key=int):
        f = first.get(aid, 0)
        note = ("" if f else
                "  <-- only ever a FAILOVER CANDIDATE: its zero usage_logs is "
                "expected, not a fault")
        print("  acct %-4s picked %3d times, of which first-pick %3d%s"
              % (aid, picks[aid], f, note))
    for aid in sorted(serving, key=int):
        if aid not in picks:
            print("  acct %-4s served traffic but appears in no failover line "
                  "-- already proven alive by usage_logs" % aid)
    print("\n  A brand-new leg with first-pick=0 has simply not had its turn.")
    print("  Wait and re-run; do NOT re-create it or blame its credentials.")
    print("  (access-log 200s appear BEFORE usage_logs rows -- DeferredService")
    print("   BatchUpdateLastUsed flushes in batches.)")
    return 0


# ---------------------------------------------------------------- parse

def parse_creds(path):
    """Decode the `----`-delimited paste, in either shape it arrives in.

    LONG (8+ fields, the spreadsheet export):
      email----mailpw----password----CURSOR_token----phone----sms_url----grok_userid----grok_sso

      Field 4 is a Cursor session token and is deliberately ignored.  Field 7
      is the grok userid and is kept, because it lets `verify`/`reauth` prove
      independently that the SSO token belongs to the account we think it does.

    SHORT (2..7 fields, what gets pasted into chat):
      email----mailpw----grok_sso

      No field 7, so `userid` comes back None and is filled in later by
      resolve_subs() from the sso-token endpoint.  ⚠️ That makes the later
      `sub == userid` assertion TAUTOLOGICAL for these lines -- the answer came
      from the same endpoint being checked.  Every caller that prints that
      assertion must say so, or a short line reads as if it passed a
      cross-check it never had.  Only the LONG shape carries an independent
      second source.

    The last field is taken as the SSO token in the short shape (not a fixed
    index) because the middle fields vary; anything that is not JWT-ish is
    refused loudly.  A silently dropped line reads as "that account failed"
    several steps later, which is the wrong direction.

    Three things the real 卡密导出.txt export has that a chat paste does not
    (2026-09-21, all three were hard failures before):
      * a UTF-8 BOM        -> read with utf-8-sig, else field 1 is "\\ufeff<email>"
      * a header line + a blank line -> skipped, but ONLY when the line has no
        `@` in it; a line that looks like an account is never skipped quietly.
      * **五个** dashes, not four.  Splitting on a literal "----" leaves the
        separator's 5th dash glued to the front of every later field, so the
        password becomes "-N5..." and the SSO "-eyJ...".  The SSO one is caught
        by the eyJ check, but the password would have been stored wrong and
        silently.  Split on a RUN of dashes: `-{4,}`.
    """
    out = []
    skipped = []
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except UnicodeDecodeError:
        lines = open(path, encoding="gbk").read().splitlines()
    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        f = re.split(r"-{4,}", raw)
        if len(f) < 2:
            if "@" in raw:
                sys.exit("line %d: has an email but no `----` separator -- "
                         "refusing to guess" % lineno)
            skipped.append((lineno, raw[:30]))   # header / title / footer
            continue
        name = f[0].strip()
        if len(f) >= 8:
            userid, sso = f[6].strip(), f[7].strip()
            if userid.count("-") != 4:
                sys.exit("line %d: field 7 %r does not look like a grok userid "
                         "(uuid)" % (lineno, userid[:40]))
        else:
            userid, sso = None, f[-1].strip()
        if not sso.startswith("eyJ"):
            sys.exit("line %d: the SSO field does not look like a JWT (got %r). "
                     "Long lines put it in field 8, short lines last."
                     % (lineno, sso[:40]))
        if not name:
            sys.exit("line %d: empty account name (field 1)" % lineno)
        out.append({"name": name, "userid": userid, "sso": sso})
    for lineno, text in skipped:
        # Loud, not silent: if a real account line ever lands here because its
        # separator was mangled, this is the only place it shows up.
        print("   line %d ignored (no separator, no email): %r" % (lineno, text))
    if not out:
        sys.exit("no credential lines found in %s" % path)
    dupes = sorted({c["name"] for c in out
                    if [x["name"] for x in out].count(c["name"]) > 1})
    if dupes:
        sys.exit("duplicate account names in %s: %s" % (path, ", ".join(dupes)))
    return out


def resolve_subs(creds, call, tok):
    """Fill in `userid` for short lines by asking the sso-token endpoint.

    Exchanging an SSO token creates nothing (audit action
    `admin.grok.oauth.sso_token.create` is just the exchange; accounts are born
    from `admin.grok.sso_to_oauth.create`, which is what `add` calls), so this
    is safe to run before any decision to write.

    Marks the line `resolved=True` so callers can label the cross-check as
    tautological instead of printing a green True that means nothing.
    """
    todo = [c for c in creds if not c["userid"]]
    if not todo:
        return creds
    print("--- resolving grok userid for %d short line(s) via sso-token ---"
          % len(todo))
    bad = 0
    for c in todo:
        st, j = call("POST", "/api/v1/admin/grok/oauth/sso-token",
                     {"sso_token": c["sso"]}, token=tok)
        d = (j or {}).get("data") or {}
        sub = d.get("sub") or d.get("user_id")
        if st != 200 or not sub:
            bad += 1
            print("  %-34s st=%s FAILED to resolve: %s"
                  % (c["name"], st, str(j)[:200]))
            continue
        c["userid"], c["resolved"] = sub, True
        print("  %-34s sub=%s tier=%s  (resolved -- NOT an independent check)"
              % (c["name"], sub, d.get("subscription_tier")))
    if bad:
        sys.exit("%d line(s) could not be resolved; refusing to continue with a "
                 "partial batch" % bad)
    print()
    return creds


def cmd_parse(a):
    for c in parse_creds(a.creds):
        print("%-34s userid=%s  sso=%s...%s (%d chars)"
              % (c["name"], c["userid"] or "<short line: resolved at run time>",
                 c["sso"][:12], c["sso"][-6:], len(c["sso"])))
    print("\nfield 4 (Cursor token) intentionally ignored; grok uses fields 7+8")
    print("short lines carry no field 7 -- `sub == userid` will be tautological")


# ---------------------------------------------------------------- verify

def cmd_verify(a):
    call, tok = _load_helper()
    bad = 0
    creds = resolve_subs(parse_creds(a.creds), call, tok)
    for c in creds:
        st, j = call("POST", "/api/v1/admin/grok/oauth/sso-token",
                     {"sso_token": c["sso"]}, token=tok)
        d = (j or {}).get("data") or {}
        sub = d.get("sub") or d.get("user_id")
        match = sub == c["userid"]
        bad += 0 if (st == 200 and match) else 1
        # A short line's userid CAME FROM this endpoint, so match==True proves
        # nothing.  Print the difference rather than a green that lies.
        how = "tautological (short line)" if c.get("resolved") else str(match)
        print("%-34s st=%-4s tier=%-18s sub==userid:%s"
              % (c["name"], st, d.get("subscription_tier"), how))
        if st == 200 and not match:
            print("   ^ sub=%r but field 7 said %r -- you probably grabbed the "
                  "wrong field" % (sub, c["userid"]))
    print("\n⚠️  tier is NOT proof of credit: a credit-dead account still "
          "exchanges 200 with tier=supergrok_heavy and then gets\n"
          "    403 personal-team-blocked:spending-limit from x.ai. Only the "
          "direct probe (`health --xai`, or `onboard`) decides that.")
    print("\nnothing was created." if not bad else "\n%d line(s) failed" % bad)
    return 1 if bad else 0


# ---------------------------------------------------------------- add

def cmd_add(a):
    call, tok = _load_helper()
    creds = parse_creds(a.creds)

    # deleted_at IS NULL: sub2api soft-deletes.  A soft-deleted row still holds
    # the name, so an unfiltered query reports "already an account" and skips a
    # name that no longer exists as far as the API is concerned.
    existing = {r[1] for r in pg("SELECT id,name FROM accounts "
                                 "WHERE platform='grok' AND deleted_at IS NULL;")}
    todo = [c for c in creds if c["name"] not in existing]
    for c in creds:
        if c["name"] in existing:
            print("SKIP  %s already an account" % c["name"])
    if not todo:
        print("nothing to do")
        return 0

    for c in todo:
        st, j = call("POST", "/api/v1/admin/grok/sso-to-oauth", {
            "sso_tokens": [c["sso"]],
            "name": c["name"],
            "group_ids": [a.group],
            "credentials": {"base_url": "https://api.x.ai/v1"},
            "concurrency": a.concurrency,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
        }, token=tok)
        failed = ((j or {}).get("data") or {}).get("failed") or []
        # `created` is [null] even on success -- it proves nothing either way.
        print("CREATE %-34s st=%s failed=%s" % (c["name"], st, failed or "none"))

    print("\n--- postgres readback (the only evidence that counts) ---")
    names = ",".join("'%s'" % c["name"].replace("'", "''") for c in todo)
    rows = pg("""SELECT a.id, a.name, a.status, a.schedulable, a.concurrency,
                        coalesce(string_agg(g.group_id::text, ','), 'NONE')
                 FROM accounts a
                 LEFT JOIN account_groups g ON g.account_id = a.id
                 WHERE a.platform='grok' AND a.name IN (%s)
                 GROUP BY 1,2,3,4,5 ORDER BY 1;""" % names)
    ok = 0
    for r in rows:
        good = (r[2] == "active" and pgbool(r[3])
                and r[4] == str(a.concurrency) and str(a.group) in r[5].split(","))
        ok += good
        print("%-4s %-34s %-8s sched=%s conc=%-5s groups=%-6s %s"
              % (r[0], r[1], r[2], r[3], r[4], r[5], "OK" if good else "<-- CHECK"))
    print("\n%d/%d created and verified" % (ok, len(todo)))
    if ok != len(todo):
        return 1
    print("now run: quota, then regress")
    return 0


# ---------------------------------------------------------------- reauth

def cmd_reauth(a):
    """Swap fresh SSO-derived credentials INTO the legs that already exist.

    `add` refuses names it already knows, and `sso-to-oauth` with
    `update_existing: true` does NOT update in place -- on 2026-09-18 it created
    a duplicate account under the same name and left the original untouched.  So
    re-authenticating an existing leg is a different operation, and this is it.

    Order matters, and each step exists because the obvious one is a no-op:

      1. exchange the SSO token, and cross-check `sub` against field 7
      2. ask x.ai directly.  A 403 personal-team-blocked:spending-limit means the
         SUBSCRIPTION is dead, not the token -- writing it back changes nothing,
         so this leg is parked (schedulable=false) instead, and named in the
         summary as needing payment
      3. PUT the credentials, bumping `_token_version` ourselves
      4. POST reset-quota -- this is what clears rate_limited_at/reset_at
      5. POST clear-error -- reset-quota does NOT touch
         `temp_unschedulable_until`, so without this a re-authenticated leg
         finishes the round STILL PARKED and the 503 does not move.  That cost
         a whole round on 2026-09-20: legs 36/37 were reauthed at 09:4x and the
         pool kept 503ing until clear-error was called separately.
      6. POST accounts/bulk-update to set schedulable.  ⛔ PUT
         /accounts/{id} accepts `schedulable` and returns 200 WITHOUT writing it
         (verified against postgres); bulk-update is the endpoint that sticks
      7. read every field back from the API

    ⚠️ Most of the time this is NOT the command you want.  A stopped pool is
    usually healthy legs held down by sub2api's own 30-minute park; `rescue`
    handles that and does not rewrite credentials.  Use `reauth` when you have
    FRESH credential lines in hand and the probe says the stored tokens are dead.
    """
    call, tok = _load_helper()
    # Short lines have no field 7, so step 1's `sub == userid` assertion would
    # crash on None.  Resolve first -- and note the assertion degrades to a
    # tautology for those lines (resolve_subs() explains why).
    creds = resolve_subs(parse_creds(a.creds), call, tok)

    live = {r[1]: r[0] for r in
            pg("SELECT id,name FROM accounts WHERE platform='grok' "
               "AND deleted_at IS NULL;")}
    missing = [c["name"] for c in creds if c["name"] not in live]
    if missing:
        print("not existing accounts (use `add` for these):")
        for m in missing:
            print("  %s" % m)

    todo = [c for c in creds if c["name"] in live]
    if not todo:
        sys.exit("none of the credential lines match an existing grok account")

    unpaid, done, bad = [], [], []
    for c in todo:
        aid = int(live[c["name"]])
        print("\n=== acct %d  %s ===" % (aid, c["name"]))
        st, j = call("GET", "/api/v1/admin/accounts/%d" % aid, token=tok)
        if st != 200:
            print("  GET failed st=%s -- skipped" % st); bad.append(aid); continue
        old = (j["data"] or {}).get("credentials") or {}
        print("  before: expires_at=%s sched=%s rl=%s"
              % (old.get("expires_at"), j["data"]["schedulable"],
                 j["data"]["rate_limited_at"]))

        st, j = call("POST", "/api/v1/admin/grok/oauth/sso-token",
                     {"sso_token": c["sso"]}, token=tok)
        d = (j or {}).get("data") or {}
        if st != 200 or not d.get("access_token"):
            print("  exchange FAILED st=%s -- leg untouched" % st)
            bad.append(aid); continue
        if d.get("sub") != c["userid"]:
            print("  sub=%r != field 7 %r -- refusing to write someone else's "
                  "token into this leg" % (d.get("sub"), c["userid"]))
            bad.append(aid); continue

        verdict = xai_verdict(d["access_token"])
        print("  exchange 200  tier=%s  sub==userid OK  x.ai says: %s"
              % (d.get("subscription_tier"), verdict))
        if not verdict.startswith("200"):
            print("  ^ the new token is REJECTED UPSTREAM. Re-auth cannot fix "
                  "this. Parking the leg so it stops eating failover attempts.")
            st, _ = call("POST", "/api/v1/admin/accounts/bulk-update",
                         {"account_ids": [aid], "schedulable": False}, token=tok)
            print("  park (schedulable=false) -> %s" % st)
            unpaid.append((aid, c["name"], verdict))
            continue

        new = dict(old)
        for k in ("access_token", "refresh_token", "id_token", "token_type",
                  "scope", "client_id", "team_id", "sub", "email",
                  "subscription_tier"):
            if d.get(k) is not None:
                new[k] = d[k]
        new["expires_at"] = datetime.datetime.utcfromtimestamp(
            d["expires_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        new["_token_version"] = int(datetime.datetime.now().timestamp() * 1000)

        st1, _ = call("PUT", "/api/v1/admin/accounts/%d" % aid,
                      {"credentials": new}, token=tok)
        st2, _ = call("POST", "/api/v1/admin/accounts/%d/reset-quota" % aid,
                      {}, token=tok)
        # reset-quota clears rate_limited_at ONLY; the park column needs its own
        # endpoint or this leg stays unschedulable with fresh credentials in it.
        st2b, _ = call("POST", "/api/v1/admin/accounts/%d/clear-error" % aid,
                       {}, token=tok)
        st3, j3 = call("POST", "/api/v1/admin/accounts/bulk-update",
                       {"account_ids": [aid], "schedulable": True}, token=tok)
        print("  PUT creds=%s  reset-quota=%s  clear-error=%s  bulk-update sched=%s"
              % (st1, st2, st2b, st3))

        st, j = call("GET", "/api/v1/admin/accounts/%d" % aid, token=tok)
        g = j["data"]
        # The API masks token bodies to "" on read, so "access_token changed"
        # is unreadable here by design -- expires_at/_token_version are the
        # fields that prove the write landed.  The park column is read from
        # postgres, not from here: the admin GET does not expose it.
        parked = [L for L in grok_gates(usage_mins=1)
                  if L["id"] == aid and L["parked"]]
        okrow = (g["schedulable"] is True
                 and g["credentials"].get("expires_at") != old.get("expires_at")
                 and g["rate_limited_at"] is None
                 and not parked)
        print("  after : expires_at=%s sched=%s rl=%s parked=%s  %s"
              % (g["credentials"].get("expires_at"), g["schedulable"],
                 g["rate_limited_at"], bool(parked), "OK" if okrow else "<-- CHECK"))
        (done if okrow else bad).append(aid)

    print("\n--- summary ---")
    print("  re-authenticated and schedulable : %s" % (done or "none"))
    if unpaid:
        print("  xAI subscription/credit dead (re-auth is a no-op, PAY):")
        for aid, name, v in unpaid:
            print("    acct %-4s %-34s %s" % (aid, name, v))
    if bad:
        print("  needs a look                     : %s" % bad)
    print("\n  Now run: rescue --dry-run  (any leg still parked will show there),")
    print("  then regress --minutes 15   (D2 is what gates).")
    return 1 if bad else 0


# ---------------------------------------------------------------- rescue

def pgbool(s):
    """psql's boolean, whichever spelling it arrived in.

    `-t -A` prints a bare boolean column as `t`/`f`, but the same value cast
    with `::text` as `true`/`false`, and a NULL as the empty string.  A parser
    that only knows `t` turns every `true` into False -- which on 2026-09-20
    printed eight legs as "STILL HELD" while postgres had them schedulable, a
    false red indistinguishable from a write that didn't land.
    """
    return (s or "").strip().lower() in ("t", "true")


def grok_gates(usage_mins=10):
    """Every gate on every grok leg, in one read, plus the stored token.

    Five gates can hold a leg down and they are INDEPENDENT -- four of them can
    read green while the fifth stops the whole pool (2026-09-20, twice in one
    morning).  Reading them one at a time is how three diagnosis rounds missed
    `temp_unschedulable_until` entirely.

    `temp_unschedulable_until > now()` is evaluated in postgres on purpose: the
    column is timestamptz in Asia/Shanghai and comparing it to a Python clock
    has been wrong by 8h in both directions here.
    """
    # ⛔ Do NOT cast the booleans with ::text here.  psql prints a bare boolean
    # as `t`/`f` but `boolean::text` as `true`/`false`, so a `== "t"` parser
    # silently reads every leg as False.  On 2026-09-20 that produced eight
    # "STILL HELD" reds on eight legs postgres was already reporting as
    # schedulable -- a false red that reads exactly like "the write didn't land".
    # Emit `t`/`f` from postgres and compare against that.
    rows = pg("""SELECT a.id::text, a.name, a.status, a.schedulable,
                        (a.rate_limited_at IS NOT NULL),
                        coalesce(to_char(a.temp_unschedulable_until,'MM-DD HH24:MI:SS'),''),
                        coalesce((a.temp_unschedulable_until > now()), false),
                        coalesce(a.temp_unschedulable_reason,''),
                        coalesce(a.credentials::jsonb->>'access_token',''),
                        coalesce((SELECT count(*)::text FROM usage_logs u
                                  WHERE u.account_id = a.id
                                    AND u.created_at > now() - interval '%d minutes'),'0')
                 FROM accounts a WHERE a.platform='grok'
                   AND a.deleted_at IS NULL ORDER BY a.id;""" % usage_mins)
    out = []
    for r in rows:
        r = (r + [""] * 10)[:10]
        out.append({"id": int(r[0]), "name": r[1], "status": r[2],
                    "sched": pgbool(r[3]), "rl": pgbool(r[4]),
                    "park_until": r[5], "parked": pgbool(r[6]), "reason": r[7],
                    "token": r[8], "served": int(r[9] or 0)})
    return out


def _rescue_classify(legs, probe=True):
    """Split the legs into: free these / leave these / already fine.

    The classifier is the whole point of `rescue`.  Freeing a leg whose
    SUBSCRIPTION is dead does not add capacity -- it adds one more leg that eats
    a failover attempt and 403s, and on 2026-09-20 that is what turned a thin
    pool into a full-pool 503 twice.  So `held` is only ever populated from a
    LIVE x.ai verdict, never from `schedulable`, never from the park `reason`
    (sub2api writes 'grok access or entitlement denied' on legs x.ai says 200 to).
    """
    free, unpaid, deadtok, fine, notok = [], [], [], [], []
    for L in legs:
        held = L["parked"] or not L["sched"] or L["rl"]
        if not L["token"]:
            notok.append(L)
            continue
        L["verdict"] = xai_verdict(L["token"]) if probe else "not probed"
        if not L["verdict"].startswith("200"):
            (unpaid if "spending-limit" in L["verdict"] else deadtok).append(L)
        elif held:
            free.append(L)
        else:
            fine.append(L)
    return free, unpaid, deadtok, fine, notok


def cmd_rescue(a):
    """One command for "my grok got stopped again": free every leg x.ai still honours.

    This exists because the recovery was a hand-typed sequence three times on
    2026-09-20 alone, and two of the three steps have a way to lie:

      * `POST /accounts/{id}/clear-error` is the ONLY endpoint that clears
        `temp_unschedulable_until`.  clear-rate-limit / recover-state /
        reset-quota all answer 200 and leave the column untouched -- which means
        a full `reauth` (whose step 4 is reset-quota) finishes with the leg still
        parked.  That is exactly how the 09-20 09:35 outage survived a reauth.
      * `POST /accounts/{id}` a.k.a. PUT /accounts/{id} accepts `schedulable`
        and returns 200 WITHOUT writing it.  `accounts/bulk-update` is the one
        that sticks.

    So the sequence is clear-error -> bulk-update -> READ POSTGRES BACK, and the
    readback is not optional: both writes are in the "returns 200, wrote
    nothing" family, and `success: 8 failed: 0` in the bulk-update body was
    printed on a round where two of the eight ended up `schedulable=f`.

    Reauth is deliberately NOT part of this.  A parked leg is usually a HEALTHY
    leg -- sub2api parks a whole leg for 30 minutes over a single upstream 403 --
    and re-authenticating it rewrites a token that x.ai already accepts.  When
    the probe says the token really is dead, this says so and stops, because the
    fix then needs credentials the script does not have.
    """
    call, tok = _load_helper()
    legs = grok_gates(usage_mins=a.minutes)

    print("=== every gate on every grok leg (%d legs) ===" % len(legs))
    for L in legs:
        gates = []
        if not L["sched"]:
            gates.append("sched=f")
        if L["rl"]:
            gates.append("rate_limited")
        if L["parked"]:
            gates.append("PARKED->%s" % L["park_until"])
        print("  acct %-4s %-30s %-8s %-28s %4d req/%dmin"
              % (L["id"], L["name"][:30], L["status"],
                 ",".join(gates) or "open", L["served"], a.minutes))

    print("\n=== what does x.ai say about each stored token? (live, 1 req/leg) ===")
    free, unpaid, deadtok, fine, notok = _rescue_classify(legs, probe=not a.no_probe)
    for L in legs:
        if "verdict" in L:
            print("  acct %-4s %s" % (L["id"], L["verdict"]))
    for L in notok:
        print("  acct %-4s no access_token stored -- cannot judge, skipped" % L["id"])

    print("\n  usable but held down : %s" % ([L["id"] for L in free] or "none"))
    print("  usable and open      : %s" % ([L["id"] for L in fine] or "none"))
    print("  xAI credit dead (PAY): %s" % ([L["id"] for L in unpaid] or "none"))
    print("  token dead (new SSO) : %s" % ([L["id"] for L in deadtok] or "none"))

    if not free:
        print("\nNothing to free. If callers still see 503, the pool is not being")
        print("held down -- it is out of capacity. %d legs pass the live probe."
              % (len(free) + len(fine)))
        return 0

    ids = [L["id"] for L in free]
    if a.dry_run:
        print("\n--dry-run: would clear-error + bulk-update schedulable=true on %s" % ids)
        return 0

    print("\n=== freeing %s ===" % ids)
    for i in ids:
        st1, _ = call("POST", "/api/v1/admin/accounts/%d/clear-error" % i, {}, token=tok)
        print("  acct %-4s clear-error=%s" % (i, st1))
    st, j = call("POST", "/api/v1/admin/accounts/bulk-update",
                 {"account_ids": ids, "schedulable": True}, token=tok)
    d = (j or {}).get("data") or {}
    print("  bulk-update st=%s success=%s failed=%s"
          % (st, d.get("success"), d.get("failed")))

    # Neither write above can be trusted on its own return code.  But the
    # readback cannot be done immediately either: on 2026-09-20 12:18 it read
    # all eight legs as schedulable=f while `updated_at` in the very same table
    # said 12:18:22 and the flag was already `t` -- bulk-update commits behind
    # its own 200, so a single read races it and manufactures eight false reds.
    # Retry instead of sleeping once: the point is to converge, not to guess a
    # delay, and a leg a SECOND HAND turns off must still read as held.
    print("\n=== postgres readback (the only proof) ===")
    stuck, after = list(ids), {}
    for attempt in range(4):
        if attempt:
            sh("sleep 3")
        after = {L["id"]: L for L in grok_gates(usage_mins=a.minutes)}
        stuck = [i for i in ids
                 if not (after.get(i) and after[i]["sched"]
                         and not after[i]["parked"])]
        if not stuck:
            break
    for i in ids:
        L = after.get(i)
        print("  acct %-4s sched=%-5s parked=%-5s upd_seen_after=%d read(s)  %s"
              % (i, L["sched"] if L else "?", L["parked"] if L else "?",
                 attempt + 1, "<-- STILL HELD" if i in stuck else "OK"))

    hands = other_hands(a.minutes)
    if hands:
        print("\n=== admin-API gate changes in the last %dmin ===" % a.minutes)
        print("  (sub2api's own park writes NO audit row, so every line here is")
        print("   a human or another session. actor_email is shared -- judge by time.)")
        for r in hands:
            print("  %s %-5s %-46s %-40s %s"
                  % (r[0], r[1], r[2][:46], r[3][:40], r[4]))

    if stuck:
        print("\n!! %s came back still held. Check the audit lines above for a" % stuck)
        print("   second hand, then re-run. Do NOT reauth them: the probe says")
        print("   x.ai accepts their tokens.")
    if unpaid:
        print("\n  Still dead until someone pays xAI: %s" % [L["id"] for L in unpaid])
    if deadtok:
        print("  Still dead until you supply fresh SSO lines: %s"
              % [L["id"] for L in deadtok])
    print("\n  Now confirm on the USER plane, not on these 200s:")
    print("    regress --minutes 15   (routing/NULL must fall to 0)")
    return 1 if stuck else 0


def other_hands(mins):
    """Admin-API calls that changed a gate in the window -- i.e. a SECOND HAND.

    sub2api's own 30-minute park is written from inside the Go service and
    leaves NO audit row, so every row here came from a human or another session
    through the admin API.  That is the discriminator for "who stopped these
    legs", and it mattered on 2026-09-20: at 11:31:00 and 11:31:22, 34 seconds
    after this script freed eight legs, someone flipped two of them back to
    schedulable=false, and accounts 38/39 had been created at 10:51 by a
    `sso-to-oauth` call nobody here made.

    ⛔ `actor_email` does NOT discriminate: everyone shares admin@sub2api.local,
    including this script.  Only the timestamp separates your calls from theirs.
    """
    return pg("""SELECT to_char(created_at,'HH24:MI:SS'), method, path,
                        coalesce(left(request_body,90),''), status_code::text
                 FROM audit_logs
                 WHERE created_at > now() - interval '%d minutes'
                   AND (path LIKE '%%schedulable%%' OR path LIKE '%%bulk-update%%'
                     OR path LIKE '%%clear-error%%' OR path LIKE '%%sso%%'
                     OR path LIKE '%%reset-quota%%')
                 ORDER BY created_at;""" % mins)


def cmd_quota(a):
    call, tok = _load_helper()
    tiers = {r[0]: r[2] for r in
             pg("SELECT id, name, coalesce(credentials->>'subscription_tier','?') "
                "FROM accounts WHERE platform='grok' AND deleted_at IS NULL "
                "ORDER BY id;")}
    ids = ([x.strip() for x in a.ids.split(",")] if a.ids else sorted(tiers, key=int))
    for i in ids:
        st, j = call("GET", "/api/v1/admin/grok/accounts/%s/quota" % i, token=tok)
        d = (j or {}).get("data") or {}
        snap = d.get("snapshot") or {}
        h = snap.get("headers") or {}
        # `headers_observed` is the ruler's own self-report.  Without it, a row
        # of None is indistinguishable from a dead leg -- which is exactly how
        # accounts 6/7/8/9 read on 2026-09-09 while they were serving hundreds
        # of real requests.  sub2api answers `source=billing_probe`
        # (snapshot=null) for some accounts and `hybrid_probe` (live x.ai
        # rate-limit headers) for others; only the latter can report limits.
        observed = d.get("headers_observed")
        if observed:
            detail = ("limit_tokens=%-10s limit_req=%-6s plan(live)=%s"
                      % (h.get("x-ratelimit-limit-tokens"),
                         h.get("x-ratelimit-limit-requests"),
                         snap.get("plan_from_45_responses") or snap.get("plan")))
        else:
            detail = "NO LIVE PROBE (source=%s, snapshot=null) -- limits unknown" % d.get("source")
        print("acct %-4s st=%-4s tier(db)=%-16s %s" % (i, st, tiers.get(i, "?"), detail))
    print("\nNOTE tier comes from the DB (`credentials.subscription_tier`), which is")
    print("populated for every account; the live probe only exists for some.")
    print("53000000/8300 canNOT tell heavy from plus -- acct 7 is plus and shows")
    print("the same numbers.  A `NO LIVE PROBE` row is a ruler gap, not a dead")
    print("leg: judge liveness from usage_logs (`regress`), never from this.")


# ---------------------------------------------------------------- regress

def cmd_regress(a):
    mins = a.minutes
    print("=== D1 sub2api scheduling: is every leg actually being PICKED? ===")
    # Every grok account, not just schedulable ones: on 09-17 six legs were
    # schedulable=t and served nothing, so filtering on that column hides
    # exactly the rows you need to see.
    legs = {r[0]: r[1] for r in pg("SELECT id, name FROM accounts WHERE "
                                  "platform='grok' AND deleted_at IS NULL "
                                  "ORDER BY id;")}
    # Scope the counts to the grok legs explicitly: usage_logs holds kimi and
    # antigravity rows from the same sub2api, so an unscoped GROUP BY mixes
    # other platforms' account_ids into this table.
    dist = {}
    if legs:
        dist = {r[0]: r[1] for r in pg(
            "SELECT account_id::text, count(*)::text FROM usage_logs "
            "WHERE created_at > now() - interval '%d minutes' "
            "AND account_id IN (%s) GROUP BY 1;"
            % (mins, ",".join(sorted(legs, key=int))))}
    picks, first, chains = failover_picks(mins)
    idle = []
    for aid in sorted(legs, key=int):
        n = dist.get(aid, "0")
        if n == "0":
            idle.append(aid)
        # picked=0 WITH traffic is not a contradiction: a leg chosen first that
        # succeeds never emits a failover line at all.  Say so, or the two
        # columns read as if they disagree.
        tail = ""
        if n != "0" and not picks.get(aid):
            tail = "  <-- served on first pick, never had to fail over"
        print("  acct %-4s %-34s %s req / last %dmin  (picked %d, first-pick %d)%s"
              % (aid, legs[aid][:34], n, mins,
                 picks.get(aid, 0), first.get(aid, 0), tail))
    if idle:
        print("  !! legs with zero traffic: %s" % ",".join(idle))
        print("     Zero traffic has three different causes -- read the")
        print("     first-pick column above before calling any of them a fault:")
        print("       first-pick > 0, zero rows  -> it WAS chosen and failed. Real.")
        print("       first-pick = 0, picked > 0 -> failover candidate only; it")
        print("         inherits already-failed requests, so zero rows is EXPECTED.")
        print("       picked = 0                 -> never offered a request at all;")
        print("         a quiet window looks identical.  Re-run over a busier one.")
    else:
        print("  every grok leg in the accounts table took real traffic")

    # The all-legs-422 confound (2026-09-17): 43 requests where EVERY leg
    # returned 422 and the chain ended in account_select_failed -> 502.  Those
    # were 7.7MB/700KB bodies, i.e. the request was refused, not the legs.
    #
    # Per REQUEST, not per window: with live traffic every leg picks up a stray
    # 422 within 30 minutes, so "all legs saw a 422" reads red while callers are
    # perfectly fine.  A request whose whole chain is 422 is the real shape.
    serving_legs = {aid for aid in legs if dist.get(aid, "0") != "0"}
    doomed = {r: c for r, c in chains.items()
              if len(c) >= 3 and all(st == "422" for _, st in c)}
    print("\n  failover chains in window: %d; chains where EVERY tried leg "
          "returned 422: %d" % (len(chains), len(doomed)))
    if doomed:
        print("     -> those requests are being REFUSED, not the legs. Adding")
        print("        accounts fixes none of them; check body_bytes in the")
        print("        access log (09-17: 7.7MB / 700KB bodies).  Sample:")
        for rid, c in list(sorted(doomed.items()))[:3]:
            print("        %s  legs=%s" % (rid, ",".join(a for a, _ in c)))
        if serving_legs and len(doomed) == len(chains):
            print("     -> and EVERY chain in the window looks like this, so do")
            print("        not read the per-leg 422s above as a leg fault.")
    else:
        print("     -> no request failed on every leg it was offered to; the")
        print("        422s above are scattered, i.e. normal retry noise.")

    print("\n=== D2 LiteLLM user plane: unique nonce through the real entries ===")
    mk = sh("sudo kubectl -n %s get secret litellm-secrets "
            "-o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d" % PROD_NS).strip()
    pod = sh("sudo kubectl -n %s get pod -l app=litellm-proxy "
             "--field-selector=status.phase=Running "
             "-o jsonpath='{.items[0].metadata.name}'" % PROD_NS).strip()
    probe = r'''
import json,urllib.request,urllib.error,sys,random
mk=sys.argv[1]; bad=0
for m in sys.argv[2].split(","):
    n="NONCE-%d"%random.randint(10**8,10**9)
    body=json.dumps({"model":m,"messages":[{"role":"user","content":
        "Reply with exactly this token and nothing else: "+n}],"max_tokens":40}).encode()
    r=urllib.request.Request("http://127.0.0.1:4000/v1/chat/completions",data=body,
        headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
    try:
        d=json.load(urllib.request.urlopen(r,timeout=120))
        t=d["choices"][0]["message"]["content"]
        ok = n in t; bad += 0 if ok else 1
        print("  %-14s 200  nonce_match=%s" % (m, ok))
    except urllib.error.HTTPError as e:
        bad+=1; print("  %-14s %s  %s" % (m, e.code, e.read().decode()[:160]))
    except Exception as e:
        bad+=1; print("  %-14s ERR %s" % (m, str(e)[:120]))
sys.exit(1 if bad else 0)
'''
    tmp = "/tmp/.s2a-probe-%d.py" % random.randint(10**6, 10**7)
    open(tmp, "w").write(probe)
    sh("sudo kubectl -n %s cp %s %s:%s" % (PROD_NS, tmp, pod, tmp))
    p = subprocess.run("sudo kubectl -n %s exec %s -- python3 %s '%s' '%s'"
                       % (PROD_NS, pod, tmp, mk, ",".join(GROK_ENTRIES)),
                       shell=True, capture_output=True, text=True)
    print(p.stdout.strip() or p.stderr[:400])
    # Keep the probe's verdict: it exits 1 when any entry failed or echoed the
    # wrong nonce.  Printing its stdout and then returning 0 turns a red D2 into
    # a green run for anything that gates on this script's exit code.
    d2_bad = p.returncode != 0
    if d2_bad:
        print("  !! D2 FAILED (probe exit %d) -- at least one entry did not "
              "return its nonce" % p.returncode)
    sh("sudo kubectl -n %s exec %s -- rm -f %s" % (PROD_NS, pod, tmp))
    os.unlink(tmp)

    print("\n=== user-plane truth: what callers actually got (NOT the error table) ===")
    print("sub2api's ops_error_logs counts UPSTREAM ATTEMPTS and retries are")
    print("absorbed before the caller.  On 2026-09-09 it held 480 x 422 in an")
    print("hour while callers saw 941 success / 1 failure.  Judge users here:")
    # The two databases run in DIFFERENT timezones: litellm-db-0 is Etc/UTC (so
    # +8h here is right), sub2api's postgres is already Asia/Shanghai (adding 8h
    # to its created_at invents a timestamp 8 hours in the future -- that is how
    # I briefly "proved" accounts created at 10:57 were created at 18:57).
    rows = pg("""SELECT to_char("startTime" + interval '8 hours','HH24:MI'),
                        count(*) FILTER (WHERE "status"='success'),
                        count(*) FILTER (WHERE "status"<>'success'),
                        count(DISTINCT api_key),
                        round(avg(extract(epoch from ("endTime"-"startTime")))::numeric,1)
                 FROM "LiteLLM_SpendLogs"
                 WHERE "startTime" > now() - interval '%d minutes'
                   AND (model_group ILIKE '%%grok%%' OR model ILIKE '%%grok%%')
                 GROUP BY 1 ORDER BY 1 DESC;""" % mins,
              ns=PROD_NS, user="litellm", db="litellm", pod="litellm-db-0")
    print("  %-7s %6s %6s %6s %8s" % ("bj_min", "ok", "fail", "keys", "avg_s"))
    for r in rows:
        print("  %-7s %6s %6s %6s %8s" % tuple(r[:5]))
    if not rows:
        print("  (no grok traffic in the window -- zero failures here is NOT a pass)")

    # If D2 shows failures, the next question is WHICH fault -- and in particular
    # whether it predates this change.  Onboarding runs minutes before this
    # query, so a pre-existing routing outage lands inside the same window and
    # reads as "I broke it".  Answer it here instead of leaving it to the eye.
    print("\n=== if D2 shows failures: which fault, and did it predate the change? ===")
    er = pg("""SELECT error_phase, coalesce(account_id::text,'NULL'),
                      to_char(min(created_at),'HH24:MI:SS'),
                      to_char(max(created_at),'HH24:MI:SS'), count(*)::text
               FROM ops_error_logs
               WHERE created_at > now() - interval '%d minutes' AND platform='grok'
               GROUP BY 1,2 ORDER BY 5::int DESC LIMIT 6;""" % mins)
    for r in er:
        note = "  <-- routing: no leg picked, never left the box" \
               if (r[0] == "routing" and r[1] == "NULL") else ""
        print("  phase=%-12s acct=%-6s %s..%s  %8s%s"
              % (r[0], r[1], r[2], r[3], r[4], note))
    print("  Compare that FIRST timestamp against when you created the accounts")
    print("  (printed below, already Asia/Shanghai -- do NOT add 8h).")
    print("  Earlier than every creation time = the outage predates you.")
    created = pg("SELECT id::text, to_char(created_at,'MM-DD HH24:MI:SS') "
                 "FROM accounts WHERE platform='grok' AND deleted_at IS NULL "
                 "ORDER BY created_at DESC LIMIT 6;")
    for r in created:
        print("    acct %-4s created %s" % (r[0], r[1]))
    return 1 if d2_bad else 0


# ---------------------------------------------------------------- onboard

def cmd_onboard(a):
    """One command: creds file in, fully-proven legs out.

    Chains the order the skill mandates, and the order exists because each step
    answers a question the previous one cannot:

      1. health   BASELINE.  Without it, an outage that was already running
                  lands in the same window as the change and reads as "I broke
                  it" (2026-09-17 cost a round to that exact shape).
      2. verify   the SSO tokens exchange at all -- creates nothing.
      3. add      the only writing step.  Skips names that already exist.
      4. --xai    THE verdict, on the new legs only: a credit-dead account
                  exchanges 200/supergrok_heavy and still gets
                  403 personal-team-blocked:spending-limit.  Without this the
                  run ends "2 accounts added" while adding zero usable capacity
                  -- on 2026-09-21, 19 of 25 legs were in exactly that state.
      5. regress  D1 scheduling + D2 user plane.  Its exit code is the run's.

    Exit code: non-zero if any step failed, if any NEW leg is not `200 usable`,
    or if D2 lost a nonce.  A pre-existing leg being credit-dead does NOT fail
    the run -- that is the baseline, not a regression (the same reason
    sub2api-upgrade.py scores post against pre instead of against green).
    """
    rc = 0
    print("############ 1/5  BASELINE (before touching anything) ############")
    cmd_health(types.SimpleNamespace(minutes=a.minutes, xai=False))

    print("\n############ 2/5  VERIFY (creates nothing) ############")
    if cmd_verify(types.SimpleNamespace(creds=a.creds)):
        sys.exit("verify failed -- refusing to create anything")

    print("\n############ 3/5  ADD ############")
    before = {r[0] for r in pg("SELECT id FROM accounts WHERE platform='grok' "
                               "AND deleted_at IS NULL;")}
    rc |= cmd_add(types.SimpleNamespace(creds=a.creds, concurrency=a.concurrency,
                                        group=a.group)) or 0

    print("\n############ 4/5  x.ai VERDICT on the new legs ############")
    # Only the new ids: probing all 25 legs spends a real request each and
    # re-reports credit deaths that were already there before this run.
    rows = pg("SELECT id::text, name, "
              "coalesce(credentials::jsonb->>'access_token','') "
              "FROM accounts WHERE platform='grok' AND deleted_at IS NULL "
              "ORDER BY id;")
    new = [r for r in rows if r[0] not in before]
    if not new:
        print("  no new legs were created (all names already existed);")
        print("  nothing new to judge. Use `health --xai` to grade the pool.")
    for aid, name, tokv in new:
        v = "no access_token stored" if not tokv else xai_verdict(tokv)
        ok = v.startswith("200")
        rc |= 0 if ok else 1
        note = ""
        if "spending-limit" in v:
            note = ("  <-- ADDED BUT USELESS: xAI credit is gone. re-auth is a "
                    "NO-OP, only paying fixes it")
        elif not ok:
            note = "  <-- NOT usable"
        print("  acct %-4s %-34s %s%s" % (aid, name[:34], v, note))
    if new and not rc:
        print("\n  all %d new leg(s) usable." % len(new))

    print("\n############ 5/5  REGRESSION ############")
    rc |= cmd_regress(types.SimpleNamespace(minutes=a.minutes)) or 0

    print("\n############ VERDICT ############")
    print("EXIT=%d  %s" % (rc, "all clear" if not rc else
                           "SOMETHING IS RED -- read the sections above; a new "
                           "leg that is credit-dead or a lost D2 nonce both "
                           "land here"))
    print("⛔ delete the credentials file yourself -- this script does not "
          "touch it (it is a credential file: chmod 600, shred -u).")
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # `<script> /path/to/creds.txt` == `<script> onboard /path/to/creds.txt`.
    # Only when the first argument is an existing file, so a typo'd subcommand
    # still gets argparse's error instead of being silently read as a path.
    argv = sys.argv[1:]
    if argv and os.path.isfile(argv[0]):
        argv.insert(0, "onboard")

    # Run from the Mac: ship everything to 198 and run it there.  Done before
    # parsing so the remote side does the parsing -- one parser, not two that
    # can drift.  S2A_LOCAL=1 forces local execution.
    if not on_198() and not os.environ.get("S2A_LOCAL"):
        sys.exit(delegate(argv))

    p = sub.add_parser("onboard",
                       help="ONE COMMAND: creds file -> health baseline, "
                            "verify, add, x.ai verdict on the new legs, "
                            "regression. Accepts the short "
                            "email----mailpw----sso paste as well as the "
                            "full 8-field line.")
    p.add_argument("creds")
    p.add_argument("--concurrency", type=int, default=200)
    p.add_argument("--group", type=int, default=GROK_GROUP)
    p.add_argument("--minutes", type=int, default=20,
                   help="window for the baseline and the regression")
    p.set_defaults(fn=cmd_onboard)

    p = sub.add_parser("health", help="BASELINE: how many legs really serve + "
                                      "is the pool routing at all (run first)")
    p.add_argument("--minutes", type=int, default=30)
    p.add_argument("--xai", action="store_true",
                   help="also ask x.ai whether each stored token still buys "
                        "anything (one real request per leg; the only ruler "
                        "that separates a dead token from a dead subscription)")
    p.set_defaults(fn=cmd_health)

    p = sub.add_parser("parse", help="decode the ---- paste, no network")
    p.add_argument("creds"); p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("verify", help="check sso tokens, create nothing")
    p.add_argument("creds"); p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("add", help="create accounts, then prove it from postgres")
    p.add_argument("creds")
    p.add_argument("--concurrency", type=int, default=200)
    p.add_argument("--group", type=int, default=GROK_GROUP)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("reauth", help="swap fresh tokens into EXISTING legs "
                                      "(add/sso-to-oauth cannot do this)")
    p.add_argument("creds")
    p.set_defaults(fn=cmd_reauth)

    p = sub.add_parser("rescue", help="\"grok 又被全部停下了\": free every leg x.ai "
                                      "still honours (clear-error + bulk-update "
                                      "+ postgres readback). NOT a reauth.")
    p.add_argument("--minutes", type=int, default=15,
                   help="window for usage_logs and for the audit-log scan")
    p.add_argument("--dry-run", action="store_true",
                   help="classify and print, write nothing")
    p.add_argument("--no-probe", action="store_true",
                   help="skip the live x.ai probe. ⛔ then it cannot tell a "
                        "parked-but-healthy leg from a credit-dead one and will "
                        "free both; only for when x.ai is unreachable")
    p.set_defaults(fn=cmd_rescue)

    p = sub.add_parser("quota", help="per-account live probe (bypasses the pool)")
    p.add_argument("--ids", help="comma list; default = every grok account")
    p.set_defaults(fn=cmd_quota)

    p = sub.add_parser("regress", help="D1 scheduling + D2 user plane")
    p.add_argument("--minutes", type=int, default=15)
    p.set_defaults(fn=cmd_regress)

    a = ap.parse_args(argv)
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
