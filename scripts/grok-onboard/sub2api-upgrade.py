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
  sudo python3 sub2api-upgrade.py go               # all of the above, in order, with a pause

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
     production cutover, not a rolling one.  `go` refuses to run outside a
     window unless you pass --force.
  4. "It came up and requests work" is only half a regression.  The other half
     is that nothing SILENTLY changed: account rows, active count, the internal
     virtual balance, and the migration count are captured before and diffed
     after.  A migration that quietly resets a balance would otherwise be found
     by a user, days later.
  5. The regression probe judges on a UNIQUE NONCE round-tripping, not on the
     absence of an "error" key.  A `/v1/responses` object carries
     `"error": null` when it is perfectly healthy; `if "error" in d` reads a
     full green as a full red (that mistake cost a round on 09-08).

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
    """SQL via a file, not -c: quoting through sudo -> kubectl -> sh mangles it."""
    pod = pod or pod_of(ns, selector)
    tmp = "/tmp/.s2a-q-%d.sql" % random.randint(10**6, 10**7)
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
    for r in get(GH_RELEASES):
        tag = r["tag_name"].lstrip("v")
        if vtuple(cur) < vtuple(tag) <= vtuple(latest):
            print("\n----- %s  (%s)" % (r["tag_name"], r["published_at"]))
            body = (r.get("body") or "").split("---\n\n## \U0001F4E5")[0]
            print(body.strip()[:4000])
    print("\nRead these before cutting over: the point of the upgrade is a")
    print("specific fix, and the regression should aim at that fix.")
    return 0


# ---------------------------------------------------------------- probe

_PROBE = r'''
import json,urllib.request,urllib.error,sys,random,time
mk=sys.argv[1]; bad=0
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
    except urllib.error.HTTPError as e:
        bad+=1; print("  %-24s %-5s %s  %s" % (m,proto,e.code,e.read().decode()[:200]))
    except Exception as e:
        bad+=1; print("  %-24s %-5s ERR %s" % (m,proto,str(e)[:150]))
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
    print(p.stdout.rstrip() or p.stderr[:600])
    sh("kubectl -n %s exec %s -- rm -f %s" % (PROD_NS, pod, tmp), check=False)
    os.unlink(tmp)
    return p.returncode


def cmd_probe(a):
    specs = list(GROK_PROBES)
    want_kimi = a.kimi if a.kimi is not None else (a.tag != "pre")
    if want_kimi:
        specs += KIMI_PROBES
        print("(kimi included: %d calls against a 100/5h + 100/week company-wide "
              "membership)" % len(KIMI_PROBES))
    print("=== probe [%s] %d entries via litellm-proxy ===" % (a.tag, len(specs)))
    rc = run_probe(specs)
    print("\n%s" % ("all green" if rc == 0 else "!! FAILURES ABOVE -- do not "
                    "proceed / consider rollback"))
    if a.tag == "pre" and rc:
        print("A red PRE-probe means the thing you are about to blame on the")
        print("upgrade is already broken.  Fix or record it first.")
    return rc


# ---------------------------------------------------------------- state

def snapshot():
    m = pg("SELECT count(*)::text FROM schema_migrations;")[0][0]
    head = pg("SELECT filename FROM schema_migrations ORDER BY applied_at DESC "
              "LIMIT 1;")[0][0]
    accts = pg("SELECT count(*)::text, count(*) FILTER (WHERE status='active')::text "
               "FROM accounts;")[0]
    bal = pg("SELECT balance::text FROM users WHERE id=1;")[0][0]
    return {"migrations": m, "head": head, "accounts": accts[0],
            "active": accts[1], "balance": bal, "image": running_image(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


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

    print("\n=== ops_error_logs since cutover (BY PLATFORM) ===")
    since = pre["at"] if pre else "now() - interval '30 minutes'"
    rows = pg("SELECT platform, status_code::text, error_phase, count(*)::text FROM "
              "ops_error_logs WHERE created_at > %s GROUP BY 1,2,3 ORDER BY 4 DESC;"
              % ("timestamptz '%s'" % since if pre else since))
    for r in rows:
        print("  %-12s %s %-14s x%s" % (r[0], r[1], r[2], r[3]))
    if not rows:
        print("  (none)")
    print("  Split by platform on purpose: this box also serves openai image")
    print("  models and antigravity.  On 09-10 every post-cutover row was")
    print("  `openai / gpt-image-*` from someone probing that endpoint -- reading")
    print("  the total would have manufactured a grok/kimi incident.")
    print("  NB this table counts UPSTREAM ATTEMPTS.  Retries are absorbed before")
    print("  the caller, so neither its emptiness nor its size is a user verdict.")

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
    hour = int(time.strftime("%H"))
    if not (hour >= 23 or hour <= 6) and not a.force:
        sys.exit("it is %02d:00 -- this cutover drops grok + kimi + the cursor "
                 "fallback chain for ~1min.  Use --force if you mean it." % hour)
    for fn, args in ((cmd_check, {}),
                     (cmd_probe, {"tag": "pre", "kimi": a.kimi}),
                     (cmd_backup, {}),
                     (cmd_pull, {"version": None, "source_tag": "latest"}),
                     (cmd_cutover, {"image": None, "yes": a.yes}),
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
