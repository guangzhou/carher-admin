#!/usr/bin/env python3
"""Run gemini-media as an N-account pool instead of one single point of failure.

Why this shape: upstream gemini-web-to-api is single-account by construction
(one global provider built from one GEMINI_COOKIES at startup -- there is no
account pool in the project, see docs/ and provider_module.go). So the pool
lives *outside* the gateway: one Deployment per account, each with its own
cookie Secret and its own egress proxy, and one LiteLLM deployment row per
account per capability. LiteLLM already load-balances rows that share a
model_name, so web-gemini-image with N rows is round-robin over N accounts
and survives any one account dying.

Each pod honours HTTPS_PROXY: the upstream never overrides http.Transport, so
every request uses http.DefaultTransport and therefore the proxy env. Verified
2026-10-29 with a negative control (HTTPS_PROXY=http://127.0.0.1:1 -> the pod
logged `proxyconnect tcp: dial tcp 127.0.0.1:1: connect: connection refused`).
That is why per-account egress IP needs no code change, only env + netpol.

Run ON 198 as a sudoer.

    # 1. one account, closed loop first
    sudo python3 gemini-media-pool.py add --name <acct> --cookies-from /root/gemini-web-build/accounts/<acct>.cookies.json \
        [--exit-ip 47.84.85.100] [--node aiyjy-litellm-242]
    sudo python3 gemini-media-pool.py verify --name <acct>      # real request, checks the pod's own state
    sudo python3 gemini-media-pool.py wire   --name <acct> --caps video,music   # add its LiteLLM rows
    # 2. only then the rest
    sudo python3 gemini-media-pool.py status
    sudo python3 gemini-media-pool.py remove --name <acct>      # drops rows + deployment, keeps the Secret

🔴 `--caps` defaults to `video,music`: the chat-path image copy lives in **one pod's memory** and nginx
pins `/gemini-media/openai/v1/media/<32hex>` to the ClusterIP of the original `gemini-media` svc, so a
pool member's public image link is a guaranteed 404. Only pass `--caps image` after that route can find
the pod that produced the id (skill gemini-web-pool-198, "多号 round-robin 还不能上").

`--exit-ip` resolves this account's egress proxy out of the shared slot catalog
(`chatgpt-acct-console/gemini_web_pool.USABLE`) and, for the cluster-internal mihomo exits, also opens
netpol `kiro-rs/cliproxy-muse-to-kiro-egress` -- without that the pod fails closed on every upstream
call. The netpol peer is **one selector for the whole pool** (`pool in (gemini-media)`), not one entry
per member: 40 per-name entries would blow past what is readable, and every member deployment/svc this
tool creates already carries `pool: gemini-media` (so does the original `gemini-media`). Adding a
member therefore needs no netpol edit after the first one -- verify with
`kubectl -n kiro-rs get netpol cliproxy-muse-to-kiro-egress -o json` and
`kubectl -n litellm-dev get pods -l pool=gemini-media` (counts must match the deployment count).

The original single deployment (`gemini-media`) is left alone; pool members are
`gemini-media-<name>`. Nothing here prints a cookie value.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-dev"
PUBLIC_BASE = "https://chat.auto-link.com.cn/gemini-media"
IMAGE = "127.0.0.1:5000/gemini-web-to-api:20261016-681ca80-p2"
LITELLM = "http://127.0.0.1:30402"
# Default node: 242 (`dedicated=new-node:NoSchedule`, needs the toleration below). 198 itself runs ~95
# of its 110 pods, so a 40-member pool would not fit there; 242 has ~85 free slots and 4 free cores.
NODE = "aiyjy-litellm-242"
TOLERATIONS = {"aiyjy-litellm-242": [{"key": "dedicated", "operator": "Equal",
                                      "value": "new-node", "effect": "NoSchedule"}],
               "aiyjy-litellm-standby": [{"key": "dedicated", "operator": "Equal",
                                          "value": "standby", "effect": "NoSchedule"}]}
EGRESS_NETPOL = ("kiro-rs", "cliproxy-muse-to-kiro-egress")  # mihomo exits live behind this one
CAPS = {  # capability -> (litellm model_name, upstream model, mode, extra)
    "image": ("web-gemini-image", "gemini-3.8-flash", "image_generation",
              {"extra_body": {"response_format": "b64_json"}, "timeout": 600.0,
               "output_cost_per_image": 0.039}),
    "music": ("web-gemini-music", "gemini-3.8-flash-music", "audio_speech",
              {"timeout": 600.0, "input_cost_per_character": 0.0}),
    "video": ("web-gemini-video", "gemini-advanced", "video_generation", {"timeout": 900.0}),
}
DEFAULT_CAPS = "video,music"  # image excluded on purpose, see the module docstring
# Models the account's live session must expose for each capability (judged on the pod's own /v1/models).
CAP_MODELS = {"image": "gemini-3.8-flash", "music": "gemini-3.8-flash", "video": "gemini-4-argon"}


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, shell=True, input=inp, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit("STOP %s\n%s" % (cmd.split("|")[0][:90], (r.stderr or r.stdout)[:600]))
    return r.stdout.strip()


def k(args, inp=None, check=True):
    return sh("sudo k3s kubectl -n %s %s" % (NS, args), inp=inp, check=check)


def master_key():
    return sh("sudo k3s kubectl -n litellm-product get secret litellm-secrets-r2 "
              "-o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d")


def api(path, body=None, method=None):
    req = urllib.request.Request(
        LITELLM + path, method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + master_key(),
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as f:
            return f.status, json.loads(f.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def exit_proxy(ip):
    """exit IP -> proxy URL from the shared slot catalog; '' for the pod-default egress."""
    sys.path.insert(0, "/opt/chatgpt-acct-console")
    import gemini_web_pool as g  # noqa: E402  (root-only tooling, lives on 198)
    if ip == g.DEFAULT_IP:
        return ""
    if ip in g.BLOCKED:
        sys.exit("STOP exit %s is blocked: %s" % (ip, g.BLOCKED[ip]))
    if ip not in g.USABLE:
        sys.exit("STOP exit %s not in USABLE %s" % (ip, g.USABLE))
    px = g.proxy_by_ip().get(ip)
    if not px:
        sys.exit("STOP no proxy URL for exit %s" % ip)
    return px


def allow_egress(name, proxy):
    """mihomo exits are ClusterIPs behind a netpol: admit the pool or members fail closed.

    One peer for the whole pool (`pool in (gemini-media)`), not one per member -- 40 members would
    otherwise mean 40 near-identical peers in a policy other services also depend on.
    """
    from urllib.parse import urlsplit
    if not proxy or not (urlsplit(proxy).hostname or "").startswith("10.43."):
        return
    ns, pol_name = EGRESS_NETPOL
    pol = json.loads(sh("sudo k3s kubectl -n %s get netpol %s -o json" % (ns, pol_name)))
    peers = pol["spec"]["ingress"][0]["from"]
    want = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}},
            "podSelector": {"matchExpressions": [{"key": "pool", "operator": "In",
                                                  "values": ["gemini-media"]}]}}
    if want in peers:
        return
    peers.append(want)
    for key in ("creationTimestamp", "resourceVersion", "uid", "managedFields", "annotations"):
        pol["metadata"].pop(key, None)
    sh("sudo k3s kubectl -n %s apply -f -" % ns, inp=json.dumps(pol))
    print("OK netpol %s/%s now admits pool=gemini-media on 7890" % (ns, pol_name))


def cookie_header(path):
    """Build the Cookie request header the gateway wants from a google_login.py dump."""
    d = json.load(open(path))
    psid, psidts = d.get("secure_1psid"), d.get("secure_1psidts")
    if not psid or not psidts:
        sys.exit("STOP %s has no secure_1psid/secure_1psidts" % path)
    return "__Secure-1PSID=%s; __Secure-1PSIDTS=%s" % (psid, psidts)


def wait_for_proxy_init_container(proxy):
    """🔴 Without this, every member on a cluster-internal (mihomo) exit dies at birth.

    The gateway builds its Gemini client 3ms after the process starts and NEVER retries
    (provider_module.go: one background init, then the pod serves 'invalid or expired' forever).
    A brand-new pod's IP is not yet in the egress NetworkPolicy's allowed set at that instant, so
    that first and only attempt gets `proxyconnect tcp: ... connect: connection refused` -- while
    the very same socket is OPEN a minute later (measured 2026-10-31 on agorolloyd / carlybussey897,
    both of which stayed dead across two rollout restarts because each restart renews the race).
    So: block the container until its own exit is actually reachable, then let it init.
    """
    from urllib.parse import urlsplit
    u = urlsplit(proxy)
    if not (u.hostname or "").startswith("10.43."):
        return []
    return [{"name": "wait-egress", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
             "command": ["sh", "-c",
                         "i=0; until nc -w 3 -z %s %d; do i=$((i+1)); "
                         "[ $i -ge 60 ] && echo 'STOP egress %s:%d never opened' && exit 1; "
                         "sleep 2; done; echo \"egress open after ${i} tries\""
                         % (u.hostname, u.port or 7890, u.hostname, u.port or 7890)],
             "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}]


def deploy_manifest(name, proxy, node=NODE):
    env = [{"name": "GEMINI_MEDIA_PUBLIC_BASE", "value": PUBLIC_BASE}]
    if proxy:
        env += [{"name": "HTTPS_PROXY", "value": proxy}, {"name": "HTTP_PROXY", "value": proxy},
                {"name": "NO_PROXY", "value": "127.0.0.1,localhost,.svc,.cluster.local,10.0.0.0/8"}]
    app = "gemini-media-" + name
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": app, "namespace": NS,
                         "labels": {"app": app, "pool": "gemini-media"}},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": {"app": app}},
                     "template": {"metadata": {"labels": {"app": app, "pool": "gemini-media"}},
                                  "spec": {
                         "initContainers": wait_for_proxy_init_container(proxy),
                         "containers": [{
                             "name": "gemini-media", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                             "env": env,
                             "envFrom": [{"secretRef": {"name": "gemini-media-env-" + name}}],
                             "ports": [{"containerPort": 4981, "protocol": "TCP"}],
                             "readinessProbe": {"httpGet": {"path": "/health", "port": 4981},
                                                "initialDelaySeconds": 5, "periodSeconds": 15,
                                                "timeoutSeconds": 2, "failureThreshold": 3},
                             "resources": {"limits": {"memory": "1Gi"},
                                           "requests": {"cpu": "50m", "memory": "128Mi"}},
                             "volumeMounts": [{"mountPath": "/home/appuser/.cookies", "name": "data"},
                                              {"mountPath": "/tmp", "name": "tmp"},
                                              {"mountPath": "/home/appuser/.cache", "name": "cache"}]}],
                         "dnsPolicy": "None",
                         "dnsConfig": {"nameservers": ["1.1.1.1", "8.8.8.8"]},
                         "nodeSelector": {"kubernetes.io/hostname": node},
                         "tolerations": TOLERATIONS.get(node, []),
                         "securityContext": {"fsGroup": 65534},
                         # Per-pod cookie cache: emptyDir, not the shared PVC. Two gateways sharing one
                         # cache would rotate each other's __Secure-1PSIDTS and kick each other out.
                         "volumes": [{"name": "data", "emptyDir": {}},
                                     {"name": "tmp", "emptyDir": {}},
                                     {"name": "cache", "emptyDir": {}}]}}}}


def svc_manifest(name):
    app = "gemini-media-" + name
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": app, "namespace": NS, "labels": {"app": app, "pool": "gemini-media"}},
            "spec": {"selector": {"app": app}, "ports": [{"port": 4981, "targetPort": 4981,
                                                          "protocol": "TCP"}]}}


def cmd_add(a):
    name = a.name
    proxy = a.proxy or (exit_proxy(a.exit_ip) if a.exit_ip else "")
    hdr = cookie_header(a.cookies_from)
    base = k("get secret gemini-media-env -o json")
    data = json.loads(base)["data"]
    # Reuse the proven env of the live single deployment, swap only the cookie.
    sh("sudo k3s kubectl -n %s create secret generic gemini-media-env-%s --dry-run=client -o json "
       "--from-literal=__placeholder=x" % (NS, name))
    payload = {"apiVersion": "v1", "kind": "Secret", "metadata": {
        "name": "gemini-media-env-" + name, "namespace": NS, "labels": {"pool": "gemini-media"}},
        "type": "Opaque", "data": dict(data)}
    import base64 as b64
    payload["data"]["GEMINI_COOKIES"] = b64.b64encode(hdr.encode()).decode()
    k("apply -f -", inp=json.dumps(payload))
    print("OK secret gemini-media-env-%s (cookie not printed)" % name)
    allow_egress(name, proxy)  # before the pod starts, so its first upstream call is not dropped
    k("apply -f -", inp=json.dumps(deploy_manifest(name, proxy, a.node)))
    k("apply -f -", inp=json.dumps(svc_manifest(name)))
    # The gateway has no auth of its own: extend the existing ingress policy to pool members.
    pol = json.loads(k("get netpol gemini-media-ingress -o json"))
    sel = pol["spec"]["podSelector"]
    want = {"key": "pool", "operator": "In", "values": ["gemini-media"]}
    exprs = sel.setdefault("matchExpressions", [])
    if want not in exprs:
        # match either the original app label or any pool member
        sel.pop("matchLabels", None)
        exprs.append(want)
        for key in ("creationTimestamp", "resourceVersion", "uid", "managedFields", "annotations"):
            pol["metadata"].pop(key, None)
        k("apply -f -", inp=json.dumps(pol))
        print("OK netpol gemini-media-ingress now selects pool=gemini-media "
              "(label the original deployment too: kubectl -n %s patch deploy gemini-media "
              "-p '{\"spec\":{\"template\":{\"metadata\":{\"labels\":{\"pool\":\"gemini-media\"}}}}}')" % NS)
    print(k("rollout status deploy/gemini-media-%s --timeout=180s" % name, check=False))
    print("OK deploy gemini-media-%s on %s exit=%s" % (name, a.node, a.exit_ip or "(pod default)"))


def pod_initialized(name):
    """Judge by the pod's OWN state: did this gateway build a Gemini client?"""
    logs = k("logs deploy/gemini-media-%s --tail=200" % name, check=False)
    ok = "Gemini client initialized successfully" in logs
    bad = [l for l in logs.splitlines()
           if "invalid or expired" in l or "initialization failed" in l]
    return ok, bad[-1][:200] if bad else ""


def cmd_verify(a):
    name = a.name
    ok, err = pod_initialized(name)
    print("init: %s %s" % ("OK" if ok else "STOP", err))
    if not ok:
        sys.exit("STOP gemini-media-%s never authenticated; fix the cookie before wiring it in." % name)
    # Ask the pod itself over loopback. netpol gemini-media-ingress only admits the litellm-product
    # namespace, so a curl from the 198 host is dropped (that is the policy working, not a dead
    # session) -- and the gateway enforces no auth of its own, so no key is needed or printed here.
    models = k("exec deploy/gemini-media-%s -- wget -T 30 -qO- "
               "http://127.0.0.1:4981/openai/v1/models" % name, check=False)
    try:
        got = [m["id"] for m in (json.loads(models).get("data") or [])]
    except Exception:
        got = []
    print("models: %s %s" % ("OK" if got else "STOP (empty => session dead)", got))
    if not got:
        sys.exit("STOP no models")
    # A guest / non-Pro session still answers chat but only lists gemini-flash-lite: that account can
    # never drive media. Judge per capability on what this pod's own session actually exposes.
    missing = sorted({c for c in a.caps.split(",") if CAP_MODELS[c] not in got})
    if missing:
        sys.exit("STOP gemini-media-%s lacks the model(s) for %s (needs %s, has %s)"
                 % (name, ",".join(missing), ",".join(sorted({CAP_MODELS[c] for c in missing})), got))
    print("OK gemini-media-%s is live for %s. Burn one real generation before announcing it."
          % (name, a.caps))


def cmd_wire(a):
    name, rows = a.name, []
    base = "http://gemini-media-%s.%s.svc.cluster.local:4981/openai/v1" % (name, NS)
    # The gateway itself has no auth, but LiteLLM needs *some* api_key on an openai/ deployment.
    # Use the same literal the live rows use so a member is byte-identical to the proven row.
    gw_key = sh("sudo k3s kubectl -n %s get secret gemini-media-env-%s "
                "-o jsonpath='{.data.API_KEY}' | base64 -d" % (NS, name)) or "none"
    for cap in a.caps.split(","):
        model_name, upstream, mode, extra = CAPS[cap]
        mid = "gemini-media-%s/%s" % (name, cap)
        body = {"model_name": model_name,
                # custom prefix rules + the four LiteLLM pitfalls live in skill gemini-web-pool-198.
                "litellm_params": dict(extra, model="openai/" + upstream, api_base=base,
                                       api_key=gw_key),
                "model_info": {"id": mid, "mode": mode}}
        st, r = api("/model/new", body)
        print(("OK   " if st == 200 else "STOP ") + mid, st, "" if st == 200 else str(r)[:200])
        rows.append(mid)
    print("rows: %s  (rollback: POST /model/delete {\"id\": <id>})" % ", ".join(rows))


def cmd_remove(a):
    for cap in CAPS:
        mid = "gemini-media-%s/%s" % (a.name, cap)
        st, _ = api("/model/delete", {"id": mid})
        print("delete row", mid, st)
    print(k("delete deploy/gemini-media-%s svc/gemini-media-%s --ignore-not-found" % (a.name, a.name),
            check=False))
    print("Secret gemini-media-env-%s kept on purpose (delete by hand if the account is gone)." % a.name)


def cmd_status(a):
    print(k("get deploy -l pool=gemini-media -o wide", check=False) or "(no pool members yet)")
    names = [d for d in k("get deploy -l pool=gemini-media -o jsonpath='{.items[*].metadata.name}'",
                          check=False).split() if d.startswith("gemini-media-")]
    for d in names:
        n = d[len("gemini-media-"):]
        ok, err = pod_initialized(n)
        print("%-34s %s %s" % (d, "init=OK" if ok else "init=STOP", err))
    st, r = api("/model/info")
    if st == 200:
        for row in r.get("data", []):
            mid = str(row.get("model_info", {}).get("id", ""))
            if mid.startswith("gemini-media"):
                print("row %-34s -> %s" % (mid, row.get("model_name")))


p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sub = p.add_subparsers(dest="cmd", required=True)
for nm, fn, needs in (("add", cmd_add, True), ("verify", cmd_verify, True),
                      ("wire", cmd_wire, True), ("remove", cmd_remove, True),
                      ("status", cmd_status, False)):
    s = sub.add_parser(nm)
    s.set_defaults(fn=fn)
    if needs:
        s.add_argument("--name", required=True, help="account short name, e.g. agorolloyd")
    if nm == "add":
        s.add_argument("--cookies-from", required=True, help="google_login.py dump (<name>.cookies.json)")
        s.add_argument("--exit-ip", default="", help="egress IP this account is pinned to (same as its "
                                                     "gemini-web home); resolves the proxy + netpol")
        s.add_argument("--proxy", default="", help="explicit HTTPS_PROXY, overrides --exit-ip")
        s.add_argument("--node", default=NODE, help="node to pin the member on (default %s)" % NODE)
    if nm in ("verify", "wire"):
        s.add_argument("--caps", default=DEFAULT_CAPS,
                       help="comma list of %s (default %s; image is excluded on purpose, see docstring)"
                            % ("/".join(CAPS), DEFAULT_CAPS))
a = p.parse_args()
if os.geteuid() != 0 and a.cmd in ("add", "remove"):
    print("note: needs sudo for kubectl", file=sys.stderr)
a.fn(a)
