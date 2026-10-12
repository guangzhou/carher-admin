#!/usr/bin/env python3
"""Upgrade the sub2api gateway on 198, with the checks that make it judgeable.

Run ON 198 (needs `sudo kubectl`, `sudo docker`, and the node's own egress).

  sudo python3 sub2api-upgrade.py check            # what version, what changed
  sudo python3 sub2api-upgrade.py probe --tag pre  # POSITIVE CONTROL, before touching anything
  sudo python3 sub2api-upgrade.py backup           # pg_dump + sha256 (rollback needs it)
  sudo python3 sub2api-upgrade.py pull             # docker pull/retag/push to 127.0.0.1:5000
  sudo python3 sub2api-upgrade.py cutover          # set image + rollout status
  sudo python3 sub2api-upgrade.py verify           # migrations delta / ERROR / state diff
  sudo python3 sub2api-upgrade.py probe --tag post # same probe again
  sudo python3 sub2api-upgrade.py go               # all of the above; stops BEFORE
                                                   # cutover outside 23:00-06:00,
                                                   # leaving dump+image ready

WHY THIS EXISTS (2026-09-10, after the second upgrade).  Both upgrades so far
(v0.1.179 -> v0.2.3 on 09-08, v0.2.3 -> v0.2.4 on 09-10) were hand-driven, and
the same five things wasted time or nearly produced a wrong reading:

  1. `docker pull weishaw/sub2api:<version>` gets a 525 from the mirror in front
     of Docker Hub.  `:latest` pulls fine.  So the version you THINK you
     deployed is not the version you asked for -- the only proof is the image
     label `org.opencontainers.image.version`.  `pull` reads it back and
     refuses to push if it does not match what `check` said latest was.
  2. `schema_migrations` has NO `version` column.  Its primary key is
     `filename`.  Copying the query from any generic runbook gives you
     `column "version" does not exist` mid-upgrade.
  3. The deployment is `Recreate` + replicas=1, so there IS a gap, and the gap
     hits grok AND kimi AND the cursor gpt-group fallback chain.  This is a
     production cutover, not a rolling one.  `go` does the four reversible
     steps at any hour and stops at the cutover outside 23:00-06:00.
     Do NOT "fix" the gap by switching to RollingUpdate: migrations run at pod
     start, and two versions against one database is a worse failure than one
     minute of 503.
  4. "It came up and requests work" is only half a regression.  The other half
     is that nothing SILENTLY changed: account rows, active count, the internal
     virtual balance, and the migration count are captured before and diffed
     after.  A migration that quietly resets a balance would otherwise be found
     by a user, days later.
  5. The regression probe judges on a UNIQUE NONCE round-tripping, not on the
     absence of an "error" key.  A `/v1/responses` object carries
     `"error": null` when it is perfectly healthy; `if "error" in d` reads a
     full green as a full red (that mistake cost a round on 09-08).

Two more the 09-20 (v0.2.4 -> v0.2.7) run added, both of them cases where the
script used to stop one step short and hand the judgement back to a human:

  6. A PRE probe is only worth running if something SUBTRACTS it.  On 09-20
     `sa-grok-4.20` was already 503 before the cutover, so the post probe's red
     was pre-existing -- but the script judged post on absolute green, which
     means `go` is unusable for as long as any one entry is broken, and the
     operator has to diff two screens by eye.  `probe` now persists its
     per-entry verdicts and a non-pre run prints a DELTA (fixed / still-broken /
     NEWLY BROKEN) and fails ONLY on newly-broken.
  7. `verify` printing `active 39 -> 33  <-- CHANGED` is a question, not an
     answer, and the shape of "the upgrade broke credentials" is identical to
     "restarting ran the token refresher, which settled four days of arrears in
     30 seconds and stamped them all with the restart time".  The deciding
     evidence is fixed (`credentials.expires_at` + the leg's last successful
     usage_logs row), so `verify` now expands it itself instead of costing the
     next person five hand-written queries.

Rollback is NOT just the image: migrations are one-way, so it is
`cutover --image <old-tag>` AND `pg_restore` of the dump `backup` made.  Print
the exact pair with `rollback-plan`.

WHAT THIS SCRIPT DOES NOT PROVE.  D1 ("every grok leg is actually being
picked") needs real traffic.  Upgrades happen at night, when the only traffic
is the probe's own, and a quiet window is indistinguishable from a leg that is
never chosen.  `verify` says so out loud instead of printing a green.  Re-run
`sub2api-grok-onboard.py regress` in a daytime window to close that leg.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request

DEV_NS = "litellm-dev"
PROD_NS = "litellm-product"
DEPLOY = "sub2api"
REGISTRY = "127.0.0.1:5000/sub2api"
UPSTREAM = "weishaw/sub2api"
GH_RELEASES = "https://api.github.com/repos/Wei-Shaw/sub2api/releases?per_page=15"
HUB_TAGS = ("https://hub.docker.com/v2/repositories/weishaw/sub2api/tags"
            "?page_size=30&ordering=last_updated")
STATE_DIR = os.path.expanduser("~/sub2api-backup")

# model:protocol.  Kept in one place so `pre` and `post` cannot drift apart --
# a post-upgrade run over a different set is not a comparison.
GROK_PROBES = ["sa-grok-4.5:chat", "sa-grok-4.6:chat", "sa-grok-4.20:chat",
               "sa-grok-4.6:resp"]
# Kimi is ONE company-wide membership metered at 100 calls / 5h AND 100 / week.
# Four probes twice (pre+post) is ~8% of the weekly budget, so `pre` skips kimi
# by default and `post` covers all four protocol shapes.
KIMI_PROBES = ["sa-kimi-k3:chat", "sa-kimi-k3-responses:resp",
               "sa-kimi-code-anthropic:msg", "sa-kimi-code-responses:resp"]


def sh(cmd, check=True):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit("command failed: %s\n%s" % (cmd, (p.stderr or p.stdout)[:600]))
    return p.stdout


def get(url, timeout=25):
    r = urllib.request.Request(url, headers={"User-Agent": "sub2api-upgrade"})
    return json.load(urllib.request.urlopen(r, timeout=timeout))


def pod_of(ns, selector):
    """Running pod only.  Both namespaces keep Succeeded pods around, and
    `{.items[0]}` will hand you one -- which then fails at exec time with a
    message about phase, nowhere near the line that picked it."""
    n = sh("kubectl -n %s get pod -l %s --field-selector=status.phase=Running "
           "-o jsonpath='{.items[0].metadata.name}'" % (ns, selector)).strip()
    if not n:
        sys.exit("no Running pod for %s in ns %s" % (selector, ns))
    return n


def pg(sql, ns=DEV_NS, selector="app=sub2api-postgres", user="sub2api", db=None,
       pod=None):
    """SQL via a file, not -c: quoting through sudo -> kubectl -> sh mangles it.

    The temp name carries pid+random on purpose.  A shared generic path like
    /tmp/q1.sql is worse than it looks: another session's root-owned leftover
    makes the write fail while the READ still succeeds, so psql runs somebody
    else's query and reports a table you never mentioned (09-20: a stray
    LiteLLM_SpendLogs query out of nowhere)."""
    pod = pod or pod_of(ns, selector)
    tmp = "/tmp/.s2a-q-%d-%d.sql" % (os.getpid(), random.randint(10**6, 10**7))
    open(tmp, "w").write(sql)
    sh("kubectl -n %s cp %s %s:%s" % (ns, tmp, pod, tmp))
    out = sh("kubectl -n %s exec %s -- psql -U %s%s -t -A -F'|' -f %s"
             % (ns, pod, user, " -d %s" % db if db else "", tmp))
    sh("kubectl -n %s exec %s -- rm -f %s" % (ns, pod, tmp))
    os.unlink(tmp)
    return [r.split("|") for r in out.strip().splitlines() if r.strip()]


def running_image():
    return sh("kubectl -n %s get deploy %s -o jsonpath="
              "'{.spec.template.spec.containers[0].image}'" % (DEV_NS, DEPLOY)).strip()


def image_version(ref):
    """The label is the only honest answer to 'which version is this'."""
    out = sh("docker inspect --format '{{index .Config.Labels \"org.opencontainers"
             ".image.version\"}}' %s" % ref, check=False).strip()
    return out or None


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


# ---------------------------------------------------------------- check

def latest_tag():
    d = get(HUB_TAGS)
    vers = [r["name"] for r in d["results"]
            if re.fullmatch(r"\d+\.\d+\.\d+", r["name"] or "")]
    if not vers:
        sys.exit("no semver tags on Docker Hub -- did the repo move?")
    return max(vers, key=vtuple)


def cmd_check(a):
    img = running_image()
    cur = image_version(img) or (img.rsplit("v", 1)[-1] if "v" in img else "?")
    latest = latest_tag()
    print("running image : %s" % img)
    print("running label : %s" % (image_version(img) or "(image not on this node)"))
    print("hub latest    : %s" % latest)
    if vtuple(cur) >= vtuple(latest):
        print("\nalready at or ahead of latest -- nothing to do")
        return 0
    print("\n=== release notes %s -> %s ===" % (cur, latest))
    breaking = []
    for r in get(GH_RELEASES):
        tag = r["tag_name"].lstrip("v")
        if vtuple(cur) < vtuple(tag) <= vtuple(latest):
            print("\n----- %s  (%s)" % (r["tag_name"], r["published_at"]))
            body = (r.get("body") or "").split("---\n\n## \U0001F4E5")[0]
            print(body.strip()[:4000])
            if "破坏性" in body or "BREAKING" in body.upper():
                breaking.append(r["tag_name"])
    print("\nRead these before cutting over: the point of the upgrade is a")
    print("specific fix, and the regression should aim at that fix.")

    # A release note that says "default ON" is a question about OUR numbers, and
    # reading it without measuring them is how a limit nobody checked becomes an
    # incident later.  0.2.11 shipped two such defaults (an API-key creation cap
    # of 200 active / 60 per hour, and balance in-flight reservation); both were
    # harmless HERE, but only because this box has one user with 9 keys and a
    # 1.0e9 internal ledger.  Print the exposure so the next operator decides on
    # data instead of re-deriving it, or copying a conclusion that was never
    # about their numbers.
    if breaking:
        print("\n=== BREAKING CHANGES in %s -- our exposure, measured ==="
              % ", ".join(breaking))
        try:
            # api_keys.status is a VARCHAR holding 'active'; `status=1` errors
            # with `operator does not exist: character varying = integer`.
            for uid, n in pg("SELECT user_id::text, count(*)::text FROM api_keys "
                             "WHERE status='active' GROUP BY 1 ORDER BY 2 DESC "
                             "LIMIT 5;"):
                print("  user %-4s active api_keys = %-5s %s"
                      % (uid, n, "<-- at/over the new 200 cap" if int(n) >= 200
                         else "(cap is 200/user, 60/hour)"))
            bal = pg("SELECT balance::text FROM users WHERE id=1;")[0][0]
            print("  user 1 balance = %s  %s" % (bal,
                  "<-- in-flight reservation can now reject concurrent calls"
                  if float(bal) < 1000 else
                  "(internal ledger; in-flight reservation cannot bite here)"))
        except Exception as e:
            print("  !! could not measure exposure: %s" % str(e)[:200])
            print("  Do NOT read that as 'not affected' -- measure it by hand.")
        print("  Knobs: api_key_create.max_active_per_user /")
        print("         max_per_user_per_hour / billing.inflight_reservation")
    return 0


# ---------------------------------------------------------------- probe

_PROBE = r'''
import json,urllib.request,urllib.error,sys,random,time
mk=sys.argv[1]; bad=0; res={}
for spec in sys.argv[2].split(","):
    m,proto=spec.rsplit(":",1)
    n="NONCE-%d"%random.randint(10**8,10**9)
    ask="Reply with exactly this token and nothing else: "+n
    if proto=="chat":
        url="/v1/chat/completions"; body={"model":m,"messages":[{"role":"user","content":ask}],"max_tokens":64}
    elif proto=="resp":
        url="/v1/responses"; body={"model":m,"input":ask,"max_output_tokens":2048}
    else:
        url="/v1/messages"; body={"model":m,"messages":[{"role":"user","content":ask}],"max_tokens":64}
    r=urllib.request.Request("http://127.0.0.1:4000"+url,data=json.dumps(body).encode(),
        headers={"Authorization":"Bearer "+mk,"Content-Type":"application/json"})
    t0=time.time()
    try:
        d=json.load(urllib.request.urlopen(r,timeout=180))
        # nonce round-trip, NOT `"error" in d`: a healthy /v1/responses object
        # carries "error": null and would read as a failure.
        txt=json.dumps(d,ensure_ascii=False); ok = n in txt
        bad += 0 if ok else 1
        print("  %-24s %-5s 200  nonce_match=%s  %.1fs" % (m,proto,ok,time.time()-t0))
        if not ok: print("      body[:300]=%s"%txt[:300])
        res[spec] = "ok" if ok else "no-nonce"
    except urllib.error.HTTPError as e:
        bad+=1; print("  %-24s %-5s %s  %s" % (m,proto,e.code,e.read().decode()[:200]))
        res[spec] = "http-%s" % e.code
    except Exception as e:
        bad+=1; print("  %-24s %-5s ERR %s" % (m,proto,str(e)[:150]))
        res[spec] = "err"
# The outer script diffs these against the pre run.  Absolute green is the
# wrong gate when an entry was ALREADY broken before the cutover.
print("__VERDICTS__" + json.dumps(res))
sys.exit(1 if bad else 0)
'''


def run_probe(specs):
    """Probe from inside a litellm-proxy pod: that is the path 1200+ real keys
    take.  Probing sub2api directly skips the entry/alias layer that has broken
    on its own before."""
    mk = sh("kubectl -n %s get secret litellm-secrets -o jsonpath="
            "'{.data.LITELLM_MASTER_KEY}' | base64 -d" % PROD_NS).strip()
    pod = pod_of(PROD_NS, "app=litellm-proxy")
    tmp = "/tmp/.s2a-probe-%d.py" % random.randint(10**6, 10**7)
    open(tmp, "w").write(_PROBE)
    sh("kubectl -n %s cp %s %s:%s" % (PROD_NS, tmp, pod, tmp))
    p = subprocess.run("kubectl -n %s exec %s -- python3 %s '%s' '%s'"
                       % (PROD_NS, pod, tmp, mk, ",".join(specs)),
                       shell=True, capture_output=True, text=True)
    out, verdicts = p.stdout.rstrip(), {}
    for line in out.splitlines():
        if line.startswith("__VERDICTS__"):
            verdicts = json.loads(line[len("__VERDICTS__"):])
    out = "\n".join(l for l in out.splitlines() if not l.startswith("__VERDICTS__"))
    print(out or p.stderr[:600])
    sh("kubectl -n %s exec %s -- rm -f %s" % (PROD_NS, pod, tmp), check=False)
    os.unlink(tmp)
    return p.returncode, verdicts


def probe_path(tag):
    return os.path.join(STATE_DIR, "probe-%s.json" % tag)


def cmd_probe(a):
    specs = list(GROK_PROBES)
    want_kimi = a.kimi if a.kimi is not None else (a.tag != "pre")
    if want_kimi:
        specs += KIMI_PROBES
        print("(kimi included: %d calls against a 100/5h + 100/week company-wide "
              "membership)" % len(KIMI_PROBES))
    print("=== probe [%s] %d entries via litellm-proxy ===" % (a.tag, len(specs)))
    rc, verdicts = run_probe(specs)
    if verdicts:
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump(verdicts, open(probe_path(a.tag), "w"), indent=1)

    if a.tag == "pre":
        print("\n%s" % ("all green" if rc == 0 else "!! RED BEFORE THE CUTOVER"))
        if rc:
            print("This is now a BASELINE, not a blocker: the post run subtracts")
            print("it and only fails on entries that were green and went red.")
            print("Record which ones, and do not let the post run re-litigate them.")
        return 0        # never block on a pre-existing fault; `go` needs the baseline
    return report_delta(a.tag, verdicts, rc)


def report_delta(tag, verdicts, rc):
    """Judge a post run against the pre baseline, not against absolute green.

    09-20: `sa-grok-4.20` was 503 before AND after the cutover (half the grok
    legs were rate-limit parked by x.ai until 09-22+).  Gating on absolute green
    made `go` unrunnable and made a human diff two screens to reach the only
    conclusion that mattered: nothing REGRESSED."""
    pre = {}
    if os.path.exists(probe_path("pre")):
        pre = json.load(open(probe_path("pre")))
    if not pre or not verdicts:
        print("\n%s" % ("all green" if rc == 0 else "!! FAILURES ABOVE -- no pre "
                        "baseline to subtract, judge by hand / consider rollback"))
        return rc

    fixed = [k for k, v in verdicts.items() if v == "ok" and pre.get(k, "ok") != "ok"]
    already = [k for k, v in verdicts.items() if v != "ok" and pre.get(k, "ok") != "ok"]
    regressed = [k for k, v in verdicts.items() if v != "ok" and pre.get(k, "ok") == "ok"]

    print("\n=== delta vs the [pre] baseline ===")
    for k in sorted(regressed):
        print("  NEWLY BROKEN  %-28s pre=ok -> post=%s" % (k, verdicts[k]))
    for k in sorted(already):
        print("  pre-existing  %-28s pre=%s -> post=%s"
              % (k, pre.get(k, "?"), verdicts[k]))
    for k in sorted(fixed):
        print("  fixed         %-28s pre=%s -> post=ok" % (k, pre.get(k)))
    missing = [k for k in pre if k not in verdicts]
    if missing:
        print("  NOT RE-RUN    %s  <-- pre and post covered different sets;"
              " that is not a comparison" % ",".join(sorted(missing)))

    if regressed:
        print("\n!! %d entry(s) REGRESSED -- this one is on the upgrade. "
              "See `rollback-plan`." % len(regressed))
        return 1
    if already:
        print("\nno regression: %d pre-existing fault(s) still red, 0 newly broken."
              % len(already))
        print("Those reds are NOT this upgrade's -- they were captured before it.")
        return 0
    print("\nall green")
    return 0


# ---------------------------------------------------------------- state

def snapshot():
    m = pg("SELECT count(*)::text FROM schema_migrations;")[0][0]
    head = pg("SELECT filename FROM schema_migrations ORDER BY applied_at DESC "
              "LIMIT 1;")[0][0]
    accts = pg("SELECT count(*)::text, count(*) FILTER (WHERE status='active')::text "
               "FROM accounts;")[0]
    bal = pg("SELECT balance::text FROM users WHERE id=1;")[0][0]
    # Per-account status, not just the count: a count tells you THAT something
    # moved, and then someone has to hand-write five queries to find out which
    # rows and whether it was even this upgrade's doing (09-20).
    st = {r[0]: "%s/%s" % (r[1], r[2]) for r in
          pg("SELECT id::text, platform, status FROM accounts "
             "WHERE deleted_at IS NULL ORDER BY id::int;")}
    return {"migrations": m, "head": head, "accounts": accts[0],
            "active": accts[1], "balance": bal, "image": running_image(),
            "acct_status": st, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def state_path():
    return os.path.join(STATE_DIR, "upgrade-pre-state.json")


def cmd_backup(a):
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    pod = pod_of(DEV_NS, "app=sub2api-postgres")
    f = os.path.join(STATE_DIR, "sub2api-%s.dump" % time.strftime("%Y%m%d-%H%M"))
    print("dumping from %s ..." % pod)
    with open(f, "wb") as fh:
        p = subprocess.run("kubectl -n %s exec %s -- sh -c "
                           "\"pg_dump -U sub2api -d sub2api -Fc\"" % (DEV_NS, pod),
                           shell=True, stdout=fh, stderr=subprocess.PIPE)
    if p.returncode != 0 or os.path.getsize(f) < 100000:
        sys.exit("pg_dump failed or is suspiciously small (%d bytes)\n%s"
                 % (os.path.getsize(f), p.stderr.decode()[:400]))
    # The `accounts` table holds live grok/kimi OAuth tokens.  This dump is a
    # credential file, not a convenience copy: 600, and never under a
    # world-readable home.
    os.chmod(f, 0o600)
    print("%s  %s  (mode 600 -- it contains OAuth tokens)"
          % (sh("sha256sum %s" % f).split()[0], f))
    st = snapshot()
    st["dump"] = f
    json.dump(st, open(state_path(), "w"), indent=2)
    os.chmod(state_path(), 0o600)
    print("\n--- pre-upgrade state (diffed by `verify`) ---")
    for k in ("image", "migrations", "head", "accounts", "active", "balance"):
        print("  %-11s %s" % (k, st[k]))
    print("\nsaved to %s" % state_path())
    print("STATE_DIR follows $HOME, so under sudo this is /root/... -- run every")
    print("step of an upgrade as the SAME user, or `rollback-plan` will point at")
    print("a dump taken at a different moment than the one you think.")
    return 0


# ---------------------------------------------------------------- pull

def cmd_pull(a):
    want = a.version or latest_tag()
    # The mirror in front of Docker Hub 525s on version tags but serves
    # :latest.  So we pull :latest and PROVE it is `want` from the label,
    # rather than trusting a tag name we could not fetch.
    src = "%s:%s" % (UPSTREAM, a.source_tag)
    print("pulling %s (label must say %s) ..." % (src, want))
    sh("docker pull %s" % src)
    got = image_version(src)
    print("label version = %s" % got)
    if got != want:
        sys.exit("REFUSING to push: wanted %s, image says %s.  Either latest has "
                 "moved on, or the mirror served something stale.  Re-run `check`."
                 % (want, got))
    dst = "%s:%s-v%s" % (REGISTRY, time.strftime("%Y%m%d"), want)
    sh("docker tag %s %s" % (src, dst))
    print(sh("docker push %s" % dst, check=False).strip()[-300:])
    tags = json.loads(sh("curl -s http://127.0.0.1:5000/v2/sub2api/tags/list"))
    if dst.split(":")[-1] not in tags.get("tags", []):
        sys.exit("push did not land in the registry: %s" % tags)
    print("\npushed %s\nregistry now: %s" % (dst, ",".join(tags["tags"])))
    print("(single-platform amd64 is expected -- the node is amd64)")
    print("\nnext: cutover --image %s" % dst)
    return 0


# ---------------------------------------------------------------- cutover

def cmd_cutover(a):
    img = a.image
    if not img:
        tags = json.loads(sh("curl -s http://127.0.0.1:5000/v2/sub2api/tags/list"))
        cand = sorted([t for t in tags.get("tags", []) if "-v" in t])[-1]
        img = "%s:%s" % (REGISTRY, cand)
    cur = running_image()
    if img == cur:
        print("already running %s" % img)
        return 0
    print("!! sub2api deploy is Recreate + replicas=1: there IS a gap of ~30-90s.")
    print("   It takes down grok (sa-grok-*), kimi (sa-kimi-*), AND the cursor")
    print("   gpt-group fallback chain that has sa-grok-4.6 in it.")
    print("   %s\n   -> %s" % (cur, img))
    if not a.yes:
        if input("proceed? [yes/N] ").strip() != "yes":
            return 1
    sh("kubectl -n %s set image deploy/%s %s=%s" % (DEV_NS, DEPLOY, DEPLOY, img))
    print(sh("kubectl -n %s rollout status deploy/%s --timeout=300s" % (DEV_NS, DEPLOY)))
    print(sh("kubectl -n %s get pods -l app=%s -o wide" % (DEV_NS, DEPLOY)))
    return 0


# ---------------------------------------------------------------- verify

def cutover_moment(pre):
    """When did the NEW version start serving?  Not when you took the backup.

    `verify` used to anchor its error window on the pre-state timestamp and
    still call it "since cutover".  On 09-24 that was 18 minutes early and
    charged 30 grok 502s -- all of which had stopped before the rollout -- to
    the upgrade.  The honest anchor is the running pod's own creationTimestamp:
    `Recreate` makes a brand-new pod at cutover, and unlike
    `containerStatuses[].state.running.startedAt` it does not move if the
    container restarts later.  Falls back to the pre-state only when there is
    no pod to ask, and says so rather than mislabelling itself."""
    ts = sh("kubectl -n %s get pod -l app=%s --field-selector=status.phase=Running"
            " -o jsonpath='{.items[0].metadata.creationTimestamp}'"
            % (DEV_NS, DEPLOY), check=False).strip()
    if ts:
        return ts.replace("T", " ").replace("Z", "+00"), "running pod created"
    if pre:
        return pre["at"], "!! pre-state/backup time -- EARLIER than the cutover"
    return (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 1800)),
            "!! no anchor: arbitrary 30min back")


def explain_status_flips(pre, post):
    """`active 39 -> 33` is a question.  Answer it here, once, in code.

    Restarting sub2api runs the token-refresh cycle immediately (then every
    5min), so tokens that died days ago all get discovered and stamped within
    seconds of the new pod -- which is pixel-identical to "the upgrade broke
    these credentials".  The deciding evidence never changes:

      * `credentials.expires_at` -- a UNIX EPOCH INTEGER, so it has to be read
        through to_timestamp(); printed raw it is a ten-digit number nobody
        compares against anything.  It is the ACCESS token's expiry, which the
        refresh cycle pushes forward every 5min, so on a live leg it always
        sits a few minutes in the FUTURE.  That makes it a watermark of the
        last SUCCESSFUL refresh: frozen days back = the refresh_token has been
        rejected since then, which is stronger evidence than "expired".
        `updated_at`, by contrast, is when it was FOUND, not when it broke.
      * the leg's last successful `usage_logs` row -- same question, asked of
        traffic instead of config.

    Both are one query away, so the script owes you the answer, not the hint."""
    p0, p1 = pre.get("acct_status") or {}, post.get("acct_status") or {}
    flipped = sorted((i for i in p1 if i in p0 and p0[i] != p1[i]), key=int)
    gone = sorted((i for i in p0 if i not in p1), key=int)
    if gone:
        print("  rows no longer present (soft-deleted?): %s" % ",".join(gone))
    if not flipped:
        if p0 and p1:
            print("  per-account status: no flips")
        return

    print("  %d account(s) flipped status:" % len(flipped))
    # expires_at is an epoch int; decode it AND print how far from now it is,
    # because "2026-09-16 19:43" only means something next to "now".
    exp = ("CASE WHEN credentials->>'expires_at' ~ '^[0-9]+$' THEN"
           " to_timestamp((credentials->>'expires_at')::bigint)::text || ' ('"
           " || round(extract(epoch from (to_timestamp((credentials->>'expires_at')"
           "::bigint) - now()))/3600.0, 1)::text || 'h from now)'"
           " ELSE coalesce(credentials->>'expires_at','(none in credentials)') END")
    rows = pg("SELECT id::text, %s, updated_at::text,"
              " left(replace(coalesce(error_message,'-'),chr(10),' '),64)"
              " FROM accounts WHERE id IN (%s);" % (exp, ",".join(flipped)))
    last = {r[0]: r[1] for r in
            pg("SELECT account_id::text, max(created_at)::text FROM usage_logs "
               "WHERE account_id IN (%s) GROUP BY 1;" % ",".join(flipped))}
    for r in rows:
        i = r[0]
        print("    acct %-4s %-18s -> %-18s" % (i, p0.get(i), p1.get(i)))
        print("      expires_at   %s" % (r[1] or "(none in credentials)"))
        #   positive hours = refresh cycle is working on this leg
        #   negative by days = refresh_token rejected since that moment
        print("      last usage   %s" % last.get(i, "(never)"))
        print("      updated_at   %s   <-- when it was FOUND, not when it broke" % r[2])
        print("      error        %s" % r[3])
    print("  Compare against a leg that did NOT flip: a healthy one carries")
    print("  expires_at a few MINUTES IN THE FUTURE (09-20: legs 15/16/17 were")
    print("  +0.4h while 21-26 sat 4 days back).  That contrast is the reading.")
    print("  Verdict rule: expires_at / last-usage in the PAST BY DAYS means the")
    print("  restart's refresh cycle merely surfaced an old death -- not this")
    print("  upgrade.  Only a token that was alive an hour ago is on the upgrade.")
    print("  Then check the blast radius: a platform with no production keys")
    print("  pointed at it (e.g. antigravity here) is a backlog item, not an")
    print("  incident.  Do NOT roll back a cutover for it.")


def cmd_verify(a):
    pre = json.load(open(state_path())) if os.path.exists(state_path()) else None
    post = snapshot()
    print("=== image ===\n  %s  (label %s)"
          % (post["image"], image_version(post["image"]) or "?"))

    print("\n=== migrations applied during this upgrade ===")
    if pre:
        rows = pg("SELECT filename, applied_at::text FROM schema_migrations "
                  "WHERE applied_at > timestamptz '%s' ORDER BY applied_at;" % pre["at"])
        for r in rows:
            print("  + %s  %s" % (r[0], r[1]))
        if not rows:
            print("  (none -- fine if the release shipped no schema change)")
        print("  count %s -> %s" % (pre["migrations"], post["migrations"]))
        nums = [re.match(r"(\d+)", r[0]).group(1) for r in rows
                if re.match(r"(\d+)", r[0])]
        if len(set(nums)) != len(nums):
            print("  NB two migrations share a numeric prefix this round (09-20"
                  " shipped 238_opencode_go_platform and"
                  " 238_purge_unlimited_user_platform_quotas).  The prefix is not"
                  " unique and not an ordering key -- count rows, match on"
                  " `filename`.")
    else:
        print("  no pre-state file; head = %s (%s total)"
              % (post["head"], post["migrations"]))
    print("  NB the key column is `filename`; there is no `version` column.")

    print("\n=== startup + runtime ERROR/FATAL/panic in the new pod ===")
    # Case-SENSITIVE and anchored on the tab-delimited level field.  `grep -i
    # '\bERROR\b'` also matches the lowercase `"error":` key that healthy WARN
    # lines carry, which turns a quiet pod into a scary count (11 on 09-10,
    # every one of them a false positive).
    LVL = "'\\tERROR\\t|\\tFATAL\\t|\\tDPANIC\\t|panic: '"
    n = sh("kubectl -n %s logs -l app=%s --tail=4000 2>/dev/null | "
           "grep -cP %s || true" % (DEV_NS, DEPLOY, LVL)).strip()
    print("  count = %s" % n)
    if n not in ("0", ""):
        print(sh("kubectl -n %s logs -l app=%s --tail=4000 2>/dev/null | "
                 "grep -P %s | tail -8" % (DEV_NS, DEPLOY, LVL), check=False))
        print("  Before blaming the upgrade: check the platform in each line.")
        print("  sub2api also runs openai/antigravity legs whose scheduled")
        print("  account tests log ERROR on their own schedule, unrelated to a")
        print("  grok/kimi cutover.")

    print("\n=== silent-change diff (what a migration could have eaten) ===")
    if pre:
        for k in ("accounts", "active"):
            print("  %-9s %-14s -> %-14s %s"
                  % (k, pre[k], post[k], "same" if pre[k] == post[k] else "<-- CHANGED"))
        explain_status_flips(pre, post)
        # Balance is a CONSUMED quantity, metered per token: it is supposed to
        # drift down between the two snapshots.  Flagging every change would
        # train you to ignore the line.  Only a rise, a wipe, or a big step is
        # migration-shaped.
        b0, b1 = float(pre["balance"]), float(post["balance"])
        drop = b0 - b1
        verdict = ("<-- CHECK: balance went UP" if drop < 0 else
                   "<-- CHECK: >5%% step, migration-shaped" if b0 and drop / b0 > 0.05
                   else "<-- CHECK: at/below zero, 403 gate is armed" if b1 <= 0
                   else "normal burn (-%.4f)" % drop)
        print("  %-9s %-14.4f -> %-14.4f %s" % ("balance", b0, b1, verdict))
    else:
        print("  accounts=%s active=%s balance=%s (no pre-state to diff)"
              % (post["accounts"], post["active"], post["balance"]))

    print("\n=== ops_error_logs around cutover (BY PLATFORM, rate delta) ===")
    anchor, src = cutover_moment(pre)
    print("  anchor = %s  (%s)" % (anchor, src))
    # Rates, not counts, and a baseline window that is never shorter than 3h.
    # Two reasons, both learned the hard way on 09-24:
    #   * absolute post counts are unreadable here -- this box carries a
    #     permanent grok error floor (502 x556 / 400 x957 / 422 x916 per 24h,
    #     none of it an incident), so the only question is whether the cutover
    #     MOVED it.  Same reason `probe --tag post` reports a delta.
    #   * the first version of this block used an equal-length pre window, and
    #     7 minutes after cutover that made `grok 400 pre=0 post=4` look NEW.
    #     It was not: grok-4.7 had been emitting 400s for 22h (775 of them)
    #     against 33k successes.  A baseline thinner than the burst period of
    #     the thing you are baselining is not a baseline.
    #   * 10-01, the third failure of this same table: the pre window and the
    #     post window STRADDLED THE DAY/NIGHT BOUNDARY.  An 11:21 cutover gives
    #     a baseline of 05:21-11:21 (dead quiet) and a post of 11:21-17:20 (the
    #     daytime peak), so `grok 502 pre=0.17/min post=0.88/min` shouted 5x at
    #     a floor that was LOWER than every day that week (10-01 337 vs 09-30
    #     974 for the same grok-4.7 502s).  Neither the 3x threshold nor the 3h
    #     minimum can see that: the two windows are not comparable to begin
    #     with.  Fix = a THIRD leg, the same clock window 24h earlier, and
    #     shouting only when the post rate beats BOTH the pre leg and that one.
    #   * same round: 306 of those 319 "502"s carried upstream_status_code=422.
    #     `status_code` is what sub2api returned to the CALLER, and it wraps a
    #     422 from x.ai as a 502, so grouping on it alone merges a pre-existing
    #     upstream rejection into what looks like a new gateway fault.  Carry
    #     the upstream code in the key; they are two different faults.
    rows = pg(
        "WITH w AS (SELECT timestamptz '%s' AS t0, "
        "           greatest(now() - timestamptz '%s', interval '1 minute') AS post_len, "
        "           greatest(now() - timestamptz '%s', interval '3 hours') AS pre_len) "
        "SELECT CASE WHEN e.created_at > w.t0 THEN 'post' "
        "            WHEN e.created_at > w.t0 - interval '24 hours' THEN 'skip' "
        "            WHEN e.created_at > w.t0 - interval '24 hours' - w.post_len "
        "                 THEN 'yday' ELSE 'pre' END, "
        "       e.platform, coalesce(e.status_code::text,'-'), e.error_phase, "
        "       count(*)::text, "
        "       extract(epoch FROM w.post_len)::int::text, "
        "       extract(epoch FROM w.pre_len)::int::text, "
        "       coalesce(e.upstream_status_code::text,'-') "
        "FROM ops_error_logs e, w "
        "WHERE e.created_at > w.t0 - interval '24 hours' - w.post_len "
        "  AND NOT (e.created_at <= w.t0 - w.pre_len "
        "           AND e.created_at > w.t0 - interval '24 hours') "
        "GROUP BY 1,2,3,4,6,7,8;" % (anchor, anchor, anchor))
    agg, post_s, pre_s = {}, 60, 10800
    for bucket, plat, code, phase, n, pl, prl, up in rows:
        post_s, pre_s = int(pl), int(prl)
        if bucket == "skip":
            continue
        agg.setdefault((plat, code, up, phase),
                       {"pre": 0, "post": 0, "yday": 0})[bucket] = int(n)
    if agg:
        print("  post %dmin | pre %dmin (just before cutover) | yday = SAME "
              "clock window 24h earlier; /min rates"
              % (post_s / 60, pre_s / 60))
        print("  code = what the CALLER got; up = upstream_status_code (502/422"
              " means x.ai said 422 and sub2api wrapped it)")
        if post_s < 600:
            print("  ⚠ post window under 10min: too thin to clear anything.  A"
                  " quiet 10min is not a pass -- re-run verify in an hour.")
        # A pre leg of literally zero rows is not a low baseline, it is NO
        # baseline, and dividing by it manufactures an arbitrarily large ratio.
        # 10-01: the 11:21 cutover put the pre window at 05:21-11:21 Beijing,
        # where this table has not one single row, and the old code still
        # computed `post/pre` off it and shouted 5x.  Say it out loud and lean
        # on yday instead of quietly treating 0 as a number.
        if not any(c["pre"] for c in agg.values()):
            print("  ⚠ pre leg is EMPTY (0 rows in the whole baseline window):"
                  " there is nothing to divide by.  This happens whenever the"
                  " cutover follows a quiet night.  Judge on yday ONLY; a"
                  " `3x vs pre` on an empty pre means nothing.")
        for (plat, code, up, phase), c in sorted(agg.items(),
                                                key=lambda kv: -kv[1]["post"]):
            rpre = c["pre"] * 60.0 / pre_s
            rpost = c["post"] * 60.0 / post_s
            ryday = c["yday"] * 60.0 / post_s
            # Three legs must agree before this shouts.  The pre leg alone is
            # not enough: it can be the quiet side of a day/night boundary (see
            # the 10-01 note above), and then every daytime cutover reads as a
            # regression.  `yday` is the same hours of the clock, so it carries
            # the diurnal shape the pre leg cannot.
            # An empty pre leg cannot vote.  If it has no rows at all, the only
            # usable comparison is yday; demanding 3x over both would let a real
            # regression through on a quiet-night cutover.
            #
            # The floor is expressed in ROWS, not in a rate.  A rate floor of
            # 0.05/min silently scales with the window: over the 371-minute
            # window this block had on 10-01 it means 18 rows, so a brand-new
            # fault with no precedent on either leg (pre=0, yday=0, post=50) was
            # judged quiet by the positive control.  A fault that never happened
            # before does not need a ratio -- `post >= 5 and yday == 0` IS the
            # finding.
            pre_votes = c["pre"] > 0
            beats = lambda r_post, n_base, r_base: (
                n_base == 0 or r_post >= 3 * r_base)
            hot = (c["post"] >= 5
                   and beats(rpost, c["yday"], ryday)
                   and (not pre_votes or beats(rpost, c["pre"], rpre)))
            if hot and c["yday"] == 0:
                tag = "<-- CHECK: NO PRECEDENT on either leg (new fault shape)"
            elif hot:
                tag = ("<-- CHECK: 3x+ the same-hours-yesterday rate%s"
                       % (" and the pre rate" if pre_votes else
                          " (pre leg empty, judged on yday alone)"))
            elif c["post"] >= 5 and pre_votes and beats(rpost, c["pre"], rpre):
                tag = "(3x vs pre, but in line with yesterday's same hours -- diurnal, not the upgrade)"
            else:
                tag = ""
            print("  %-10s %-4s up=%-4s %-12s pre=%-5d (%.2f/min)  yday=%-5d (%.2f/min)  post=%-5d (%.2f/min) %s"
                  % (plat, code, up, phase, c["pre"], rpre, c["yday"], ryday,
                     c["post"], rpost, tag))
        if post_s > 3 * 3600:
            print("  NB post window is now >3h: it spans a different part of the"
                  " day than the pre leg.  Judge on the yday column.")
    else:
        print("  (none on either side)")
    print("  Split by platform on purpose: this box also serves openai image")
    print("  models and antigravity.  On 09-10 every post-cutover row was")
    print("  `openai / gpt-image-*` from someone probing that endpoint -- reading")
    print("  the total would have manufactured a grok/kimi incident.")
    print("  NB this table counts UPSTREAM ATTEMPTS.  Retries are absorbed before")
    print("  the caller, so neither its emptiness nor its size is a user verdict.")
    print("  Until 09-24 this block said `since cutover` but anchored on the")
    print("  BACKUP timestamp, so it billed 18 quiet pre-cutover minutes to the")
    print("  upgrade (30 grok 502s that had all stopped before the new pod).")


    # Everything above this line measures UPSTREAM ATTEMPTS or sub2api's own
    # bookkeeping.  None of it answers the only question a user asks: did my
    # request succeed?  Retries are absorbed before the caller (09-09: 480 x 422
    # in ops_error_logs while callers saw 941 success / 1 failure), so that
    # table can be loud while the user plane is perfect -- and, in principle,
    # quiet while it is not.  On 10-01 the two numbers that actually closed the
    # upgrade (5 APIConnectionErrors inside the cutover second, then 100/0) were
    # hand-queried out of LiteLLM_SpendLogs afterwards.  Do it here instead.
    print("\n=== user plane: what CALLERS got, per minute around the cutover ===")
    rows = pg("""SELECT to_char("startTime",'HH24:MI'),
                        count(*) FILTER (WHERE "status"='success')::text,
                        count(*) FILTER (WHERE "status"<>'success')::text
                 FROM "LiteLLM_SpendLogs"
                 WHERE "startTime" BETWEEN timestamptz '%s' - interval '3 minutes'
                                       AND timestamptz '%s' + interval '10 minutes'
                   AND (model_group ILIKE '%%grok%%' OR model ILIKE '%%grok%%'
                        OR model_group ILIKE '%%kimi%%' OR model ILIKE '%%kimi%%')
                 GROUP BY 1 ORDER BY 1;""" % (anchor, anchor),
              ns=PROD_NS, user="litellm", db="litellm", pod="litellm-db-0")
    if rows:
        print("  (UTC minutes; cutover second = %s)" % anchor)
        for t, ok, bad in rows:
            print("  %s  ok=%-5s fail=%-5s %s"
                  % (t, ok, bad, "<-- the Recreate gap" if bad != "0" else ""))
        print("  The gap is expected to cost exactly the requests IN FLIGHT during")
        print("  the swap -- 10-01 that was 5 APIConnectionErrors inside ONE second,")
        print("  not 30-90s worth.  Report that number, not the rollout duration.")
    else:
        print("  (no grok/kimi traffic in that window -- zero failures is NOT a pass")
        print("   here either; it means nobody was calling)")
    rows = pg("""SELECT count(*) FILTER (WHERE "status"='success')::text,
                        count(*) FILTER (WHERE "status"<>'success')::text
                 FROM "LiteLLM_SpendLogs"
                 WHERE "startTime" > timestamptz '%s' + interval '1 minute'
                   AND (model_group ILIKE '%%grok%%' OR model ILIKE '%%grok%%'
                        OR model_group ILIKE '%%kimi%%' OR model ILIKE '%%kimi%%');"""
              % anchor, ns=PROD_NS, user="litellm", db="litellm", pod="litellm-db-0")
    if rows:
        print("  since cutover+1min: ok=%s fail=%s" % tuple(rows[0][:2]))

    print("\n=== D1 scheduling: NOT decidable right after a night cutover ===")
    rows = pg("SELECT account_id::text, count(*)::text FROM usage_logs WHERE "
              "created_at > now() - interval '30 minutes' GROUP BY 1 ORDER BY 1;")
    print("  last 30min: %s" % (", ".join("acct%s=%s" % (r[0], r[1]) for r in rows)
                                or "(nothing)"))
    print("  A quiet window and a leg that is never chosen look identical here.")
    print("  Close this leg with `sub2api-grok-onboard.py regress` in a DAYTIME")
    print("  window; do not read the line above as a pass or a fail.")
    return 0


# ---------------------------------------------------------------- rollback

def cmd_rollback_plan(a):
    pre = json.load(open(state_path())) if os.path.exists(state_path()) else {}
    print("Migrations are one-way.  Rolling back the image ALONE will run the old")
    print("binary against a newer schema.  Both halves or neither:\n")
    print("  1. kubectl -n %s scale deploy/%s --replicas=0" % (DEV_NS, DEPLOY))
    print("  2. kubectl -n %s exec -i <postgres-pod> -- pg_restore -U sub2api "
          "-d sub2api --clean --if-exists < %s"
          % (DEV_NS, pre.get("dump", "<the dump from `backup`>")))
    print("  3. kubectl -n %s set image deploy/%s %s=%s"
          % (DEV_NS, DEPLOY, DEPLOY, pre.get("image", "<old image>")))
    print("  4. kubectl -n %s scale deploy/%s --replicas=1" % (DEV_NS, DEPLOY))
    print("  5. probe --tag rollback")
    return 0


# ---------------------------------------------------------------- go

def cmd_go(a):
    """check/probe/backup/pull touch nothing, so they run at any hour.  The
    window gate sits where the risk is -- immediately before `cutover`.

    That ordering is not cosmetic.  Gating the whole sequence means an operator
    who has to ask someone about the downtime asks with nothing prepared; doing
    the four reversible steps first means the question left to ask is exactly
    one step, with the image already verified and the dump already on disk
    (which is how the 09-20 run went, by hand)."""
    for fn, args in ((cmd_check, {}),
                     (cmd_probe, {"tag": "pre", "kimi": a.kimi}),
                     (cmd_backup, {}),
                     (cmd_pull, {"version": None, "source_tag": "latest"})):
        print("\n" + "=" * 70 + "\n== %s\n" % fn.__name__ + "=" * 70)
        rc = fn(argparse.Namespace(**args))
        if rc:
            sys.exit("%s returned %s -- stopping.  Nothing has changed yet."
                     % (fn.__name__, rc))
        time.sleep(1)

    hour = int(time.strftime("%H"))
    if not (hour >= 23 or hour <= 6) and not a.force:
        sys.exit("\nPREPARED, NOT CUT OVER.  It is %02d:00 and this cutover drops "
                 "grok + kimi + the cursor fallback chain for ~1min.\n"
                 "The dump and the verified image are already in place, so the "
                 "only step left is one command:\n"
                 "  cutover --yes   (or re-run `go --force` to continue here)"
                 % hour)

    for fn, args in ((cmd_cutover, {"image": None, "yes": a.yes}),
                     (cmd_verify, {}),
                     (cmd_probe, {"tag": "post", "kimi": None})):
        print("\n" + "=" * 70 + "\n== %s\n" % fn.__name__ + "=" * 70)
        rc = fn(argparse.Namespace(**args))
        if rc:
            sys.exit("%s returned %s -- stopping.  See `rollback-plan`."
                     % (fn.__name__, rc))
        time.sleep(1)
    print("\nDONE.  Still open: D1 scheduling (needs a daytime window) and any")
    print("release-specific fix you upgraded FOR -- verify that one on purpose.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="running version vs hub latest + release notes"
                   ).set_defaults(fn=cmd_check)

    p = sub.add_parser("probe", help="nonce regression through litellm-proxy")
    p.add_argument("--tag", default="post", help="label for the run: pre / post")
    p.add_argument("--kimi", action="store_true", default=None,
                   help="include kimi entries (default: off for --tag pre, on otherwise)")
    # Re-running a post probe is normal (you fix something, you re-check).  Each
    # rerun used to spend 4 more calls out of a 100/WEEK company-wide membership
    # with no way to decline, so a few reruns quietly eat a chunk of everyone's
    # weekly kimi budget.
    p.add_argument("--no-kimi", dest="kimi", action="store_false",
                   help="skip kimi entries (they cost real weekly quota)")
    p.set_defaults(fn=cmd_probe)

    sub.add_parser("backup", help="pg_dump + sha256 + pre-state snapshot"
                   ).set_defaults(fn=cmd_backup)

    p = sub.add_parser("pull", help="pull upstream, verify label, push to registry")
    p.add_argument("--version", help="expected version; default = hub latest")
    p.add_argument("--source-tag", default="latest",
                   help="upstream tag to pull; version tags 525 via the mirror")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("cutover", help="set image + rollout status")
    p.add_argument("--image", help="default = newest tag in the local registry")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_cutover)

    sub.add_parser("verify", help="migrations / ERROR / silent-change diff"
                   ).set_defaults(fn=cmd_verify)
    sub.add_parser("rollback-plan", help="print the image+dump pair"
                   ).set_defaults(fn=cmd_rollback_plan)

    p = sub.add_parser("go", help="the whole sequence")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="run outside 23:00-06:00")
    p.add_argument("--kimi", action="store_true", default=None)
    p.set_defaults(fn=cmd_go)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
