#!/usr/bin/env python3
"""Add the missing `cp /patch/*.js` lines to a zero-* Deployment's startup command.

RUN THIS ON 198 (needs kubectl + sudo):
    python3 patch-zero-pod-startup.py zero-129            # dry-run (default)
    python3 patch-zero-pod-startup.py zero-129 --apply    # actually patch

Why it exists: 21 of 47 pods were serving the UNPATCHED /app/routes/responses.js
because their startup command never copied it out of the shared zk-image-patch
ConfigMap, so every request carrying tools[] got
`503 no Codex tokens available for tool_call`. Find them with
audit-zero-pod-patches.py.

The cp lines are inserted immediately BEFORE `exec`, so any init gating already
present (e.g. `until [ -f /app/temp/users.json ]`) is preserved verbatim.

The sudo password comes from SUDO_PW in the environment or from the local
gitignored .carher-secrets.json (see scripts/lib/carher-secrets-init.sh).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "lib"))
import carher_secrets  # noqa: E402

NS = "litellm-product"
# env -> .carher-secrets.json (gitignored) -> ~/.config/carher/secrets.json
PW = carher_secrets.require("SUDO_PW") + "\n"

NEED = ["cp /patch/web-tools.js /app/routes/web-tools.js",
        "cp /patch/raw.js /app/routes/raw.js",
        "cp /patch/responses.js /app/routes/responses.js"]


def kc(*a, timeout=240):
    return subprocess.run(["sudo", "kubectl", "-n", NS] + list(a),
                          capture_output=True, text=True, input=PW,
                          timeout=timeout)


def kc_checked(*a, **kw):
    """Run kubectl and abort with a readable message on failure. Parsing stdout
    blindly turned a typo'd deploy name or an auth failure into an opaque
    JSONDecodeError."""
    r = kc(*a, **kw)
    if r.returncode != 0:
        sys.exit("kubectl %s failed (rc=%s): %s"
                 % (" ".join(a), r.returncode,
                    (r.stderr or r.stdout or "no output").strip()[:300]))
    return r


def main(argv):
    names = [x for x in argv[1:] if not x.startswith("-")]
    flags = [x for x in argv[1:] if x.startswith("-")]
    if "-h" in flags or "--help" in flags:
        print(__doc__)
        return 0
    if len(names) != 1:
        sys.exit(__doc__)
    app = names[0]

    # Reject unknown flags instead of silently dry-running. `-apply` or `--Apply`
    # used to fall through to the dry-run branch, so the operator believed a fix
    # had landed when nothing had changed -- and 21 deploys were driven through
    # this path without that being noticed.
    unknown = [f for f in flags if f not in ("--apply", "--dry-run")]
    if unknown:
        sys.exit("unknown flag(s) %s -- did you mean --apply?" % unknown)
    dry = "--apply" not in flags

    r = kc_checked("get", "deploy", app, "-o", "json")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit("unparseable kubectl output for %s: %s" % (app, e))

    containers = d["spec"]["template"]["spec"].get("containers") or []
    if not containers:
        sys.exit("%s has no containers" % app)
    if len(containers) > 1:
        # Never guess which container to rewrite.
        sys.exit("%s has %d containers (%s); refusing to guess -- patch by hand"
                 % (app, len(containers), [c.get("name") for c in containers]))
    c = containers[0]

    arglist = c.get("args") or []
    if not arglist:
        sys.exit("%s container %r has no args to patch" % (app, c.get("name")))
    # args may be ["-c", script] or [script]
    idx = 1 if (len(arglist) > 1 and arglist[0] == "-c") else 0
    script = arglist[idx]

    if "cp /patch/responses.js" in script:
        print("  %s already patched, skip" % app)
        return 0
    if "exec " not in script:
        sys.exit("%s startup script has no `exec` line to insert before:\n%s"
                 % (app, script[:400]))

    out = []
    for ln in script.split("\n"):
        if ln.startswith("exec "):
            for n in NEED:
                if n not in script:
                    out.append(n)
        out.append(ln)
    new = "\n".join(out)

    print("  %s new script:\n%s"
          % (app, "\n".join("      " + l for l in new.split("\n"))))
    if dry:
        print("  (dry-run; re-run with --apply to patch)")
        return 0

    arglist[idx] = new
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": c["name"], "args": arglist}]}}}}
    kc_checked("patch", "deploy", app, "--type", "strategic",
               "-p", json.dumps(patch))
    print("  %s patched; waiting for rollout..." % app)

    # Patching args triggers a rolling restart of serving pods, and CLAUDE.md
    # requires monitoring it ("操作变更时使用 kubectl apply 或 kubectl set image，用
    # kubectl rollout status 监控"). Without this a bad args value leaves pods
    # crash-looping while the script still exits 0.
    r = kc("rollout", "status", "deploy/%s" % app, "--timeout=180s")
    print("  rollout: %s" % ((r.stdout or r.stderr).strip()[:160] or "no output"))
    if r.returncode != 0:
        sys.exit("  ROLLOUT DID NOT COMPLETE for %s -- inspect `kubectl -n %s "
                 "describe deploy/%s` and the pod logs before continuing"
                 % (app, NS, app))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
