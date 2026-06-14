#!/usr/bin/env python3
"""Repair Dify login-entry reliability for CarHer H75.

The user-facing /dify command depends on the lifecycle bootstrap service,
which calls Dify console login. Dify's default SQLAlchemy pool does not
pre-ping connections, so stale Postgres connections can surface as a 500 on
the first login attempt. This script makes the API pool resilient and adds a
small retry loop in the bootstrap login path.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True, check=False)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        raise SystemExit(proc.returncode)
    return proc


def kubectl(args: list[str], *, namespace: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    kubectl_bin = str(Path(os.environ.get("KUBECTL_BIN", "kubectl")).expanduser())
    cmd = [kubectl_bin]
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        cmd += ["--kubeconfig", str(Path("~/.kube/config").expanduser())]
    return run([*cmd, "-n", namespace, *args], input_text=input_text, check=check)


def kubectl_json(args: list[str], *, namespace: str) -> dict:
    proc = kubectl([*args, "-o", "json"], namespace=namespace)
    return json.loads(proc.stdout)


def apply_json(obj: dict, *, namespace: str) -> None:
    # ConfigMaps can contain large embedded source files; replace avoids
    # kubectl apply's huge last-applied annotation.
    obj.get("metadata", {}).pop("managedFields", None)
    kubectl(["replace", "-f", "-"], namespace=namespace, input_text=json.dumps(obj, ensure_ascii=False))


def ensure_dify_pool_config(namespace: str) -> bool:
    cm = kubectl_json(["get", "configmap", "dify-config"], namespace=namespace)
    data = cm.setdefault("data", {})
    desired = {
        "SQLALCHEMY_POOL_PRE_PING": "true",
        "SQLALCHEMY_POOL_RECYCLE": "300",
    }
    changed = False
    for key, value in desired.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    if changed:
        apply_json(cm, namespace=namespace)
        print("changed\tdify-config\tSQLALCHEMY_POOL_PRE_PING=true\tSQLALCHEMY_POOL_RECYCLE=300")
    else:
        print("ok\tdify-config\tpool_pre_ping_already_enabled")
    return changed


USER_LOGIN_REPLACEMENT = '''def _is_transient_dify_error(exc):
    text = str(exc)
    transient_markers = (
        "server closed the connection unexpectedly",
        "Connection reset by peer",
        "OperationalError",
        "INTERNAL SERVER ERROR",
        "500 Server Error",
        "Read timed out",
        "ConnectTimeout",
    )
    return any(marker in text for marker in transient_markers)


def _post_dify_json(path, *, json_body=None, headers=None, timeout=15, attempts=3, label="dify"):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(
                f"{DIFY_BASE}{path}",
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_transient_dify_error(exc):
                raise
            _audit("dify_transient_retry", label=label, attempt=attempt, error=str(exc)[:240])
            time.sleep(0.5 * attempt)
    raise last_exc


def _user_login(email, password):
    """Login dify console as user, return {access_token, refresh_token}."""
    d = _post_dify_json(
        "/console/api/login",
        json_body={"email": email, "password": password, "language": "en-US"},
        timeout=15,
        attempts=3,
        label="user_login",
    )["data"]
    return {"access_token": d["access_token"], "refresh_token": d["refresh_token"]}


def _switch_user_to_workspace(access_token, tenant_id):
    """Switch the user's current tenant in dify db via console API.

    Raises RuntimeError on non-200 so the caller cannot proceed to mint a
    fresh access_token whose JWT claims would still point at the stale
    tenant (codex-review #2). user_login_issue treats this as a failure
    that aborts the whole issue flow.
    """
    return _post_dify_json(
        "/console/api/workspaces/switch",
        headers={"Authorization": f"Bearer {access_token}"},
        json_body={"tenant_id": tenant_id},
        timeout=15,
        attempts=3,
        label="workspace_switch",
    )
'''


SHARED_NONCE_HELPERS = r'''# login_nonce_file_shared_v1: keep Dify auto-login nonces visible across bootstrap replicas.
NONCES_DIR = os.path.join(BOOTSTRAP_DIR, "login-nonces")
os.makedirs(NONCES_DIR, exist_ok=True)


def _nonce_path(nonce):
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    return os.path.join(NONCES_DIR, digest + ".json.enc")


def _nonce_lock_path(nonce):
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    return os.path.join(NONCES_DIR, digest + ".lock")


def _read_nonce(nonce):
    path = _nonce_path(nonce)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            ciphertext = f.read().strip()
        return json.loads(_decrypt_password(ciphertext))
    except Exception as exc:
        _audit("login_nonce_read_failed", nonce=nonce[:8], error=str(exc)[:240])
        return None


def _write_nonce(nonce, record):
    path = _nonce_path(nonce)
    tmp = f"{path}.{os.getpid()}.tmp"
    ciphertext = _encrypt_password(json.dumps(record, ensure_ascii=False, sort_keys=True))
    with open(tmp, "w") as f:
        f.write(ciphertext)
    os.replace(tmp, path)


def _delete_nonce(nonce):
    for path in (_nonce_path(nonce), _nonce_lock_path(nonce)):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass
'''


EXCHANGE_NONCE_REPLACEMENT = r'''@app.route("/v1/exchange", methods=["GET"])
def exchange_nonce():
    """Redeem a login nonce for Dify console tokens.

    Nonces are persisted under /Data/dify-bootstrap/login-nonces so an issue
    request handled by bootstrap replica A can be consumed by /auto on replica
    B. The first browser is still bound by an HttpOnly cookie; copied links are
    rejected after the first redemption.
    """
    import fcntl

    t = request.args.get("t", "")
    if not t:
        return jsonify({"error": "invalid_nonce"}), 404
    os.makedirs(NONCES_DIR, exist_ok=True)
    with open(_nonce_lock_path(t), "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        n = _NONCES.get(t) or _read_nonce(t)
        if not n:
            return jsonify({"error": "invalid_nonce"}), 404
        if int(time.time()) > n["expires_at"]:
            _delete_nonce(t)
            _NONCES.pop(t, None)
            return jsonify({"error": "nonce_expired"}), 410
        # Cookie name is per-nonce so different /dify URLs in the same browser
        # do not overwrite each other's binding cookies.
        cookie_name = f"dlbind_{hashlib.sha256(t.encode()).hexdigest()[:12]}"
        presented = request.cookies.get(cookie_name)
        bound = n.get("bound_to")
        set_cookie_value = None
        if bound is None:
            bind_secret = secrets.token_urlsafe(24)
            n["bound_to"] = bind_secret
            set_cookie_value = bind_secret
            _NONCES[t] = n
            _write_nonce(t, n)
            _audit("login_bound", bot_id=n["bot_id"], email=n["email"], nonce=t[:8])
        else:
            if not presented or not secrets.compare_digest(presented, bound):
                _audit(
                    "login_exchange_rejected_unbound",
                    bot_id=n["bot_id"],
                    email=n["email"],
                    nonce=t[:8],
                )
                return jsonify({"error": "redeemed_from_another_browser"}), 410
    _audit("login_exchanged", nonce=t[:8], email=n["email"], bot_id=n["bot_id"])
    resp = jsonify({
        "access_token": n["access_token"],
        "refresh_token": n["refresh_token"],
        "workspace_id": n["workspace_id"],
    })
    if set_cookie_value is not None:
        # Path-scoped to /v1/exchange so the /auto page and dify console
        # never see the binding cookie. HttpOnly + SameSite=Lax. 15-minute
        # lifetime matches the nonce window so the cookie expires with it.
        resp.set_cookie(
            cookie_name,
            set_cookie_value,
            max_age=900,
            httponly=True,
            samesite="Lax",
            path="/v1/exchange",
            secure=False,
        )
    return resp


'''


def patch_shared_nonce_code(source: str) -> tuple[str, bool]:
    if "login_nonce_file_shared_v1" in source:
        return source, False
    if "_NONCES = {}" not in source:
        raise SystemExit("failed to locate _NONCES declaration")
    patched = source.replace("_NONCES = {}\n", "_NONCES = {}\n\n" + SHARED_NONCE_HELPERS + "\n", 1)
    if "    _audit(\"login_issued\"" not in patched:
        raise SystemExit("failed to locate login_issued audit")
    patched = patched.replace(
        "    _audit(\"login_issued\"",
        "    _write_nonce(nonce, _NONCES[nonce])\n    _audit(\"login_issued\"",
        1,
    )
    pattern = re.compile(
        r'@app\.route\("/v1/exchange", methods=\["GET"\]\)\s*'
        r'def exchange_nonce\(\):.*?'
        r'(?=@app\.route\("/auto", methods=\["GET"\]\))',
        re.DOTALL,
    )
    patched, count = pattern.subn(EXCHANGE_NONCE_REPLACEMENT, patched, count=1)
    if count != 1:
        raise SystemExit("failed to locate /v1/exchange block")
    return patched, True


def patch_bootstrap_code(namespace: str) -> bool:
    cm = kubectl_json(["get", "configmap", "dify-bootstrap-code"], namespace=namespace)
    data = cm.setdefault("data", {})
    source = data.get("bootstrap.py")
    if not source:
        raise SystemExit("dify-bootstrap-code missing bootstrap.py")
    patched = source
    changed = False
    if "dify_transient_retry" in patched:
        print("ok\tdify-bootstrap-code\tlogin_retry_already_present")
    else:
        pattern = re.compile(
            r'def _user_login\(email, password\):.*?'
            r'@app\.route\("/v1/user-login/<bot_id>/issue", methods=\["POST"\]\)\s*'
            r'def user_login_issue\(bot_id\):',
            re.DOTALL,
        )
        replacement = USER_LOGIN_REPLACEMENT + "\n\n@app.route(\"/v1/user-login/<bot_id>/issue\", methods=[\"POST\"])\ndef user_login_issue(bot_id):"
        patched, count = pattern.subn(replacement, patched, count=1)
        if count != 1:
            raise SystemExit("failed to locate _user_login/_switch_user_to_workspace block")
        changed = True
        print("changed\tdify-bootstrap-code\tlogin_retry_patch")

    patched, nonce_changed = patch_shared_nonce_code(patched)
    if nonce_changed:
        changed = True
        print("changed\tdify-bootstrap-code\tshared_nonce_patch")
    else:
        print("ok\tdify-bootstrap-code\tshared_nonce_already_present")

    if not changed:
        return False
    data["bootstrap.py"] = patched
    apply_json(cm, namespace=namespace)
    return True


def restart_and_wait(namespace: str, deployments: list[str]) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    for deployment in deployments:
        kubectl(
            [
                "patch",
                "deployment",
                deployment,
                "-p",
                json.dumps({"spec": {"template": {"metadata": {"annotations": {"carher.io/dify-login-repair-at": stamp}}}}}),
            ],
            namespace=namespace,
        )
        print(f"rolled\t{deployment}")
    for deployment in deployments:
        kubectl(["rollout", "status", f"deployment/{deployment}", "--timeout=300s"], namespace=namespace)
        print(f"ready\t{deployment}")


def deployment_ready(namespace: str, deployment: str) -> bool:
    data = kubectl_json(["get", "deployment", deployment], namespace=namespace)
    spec_replicas = data.get("spec", {}).get("replicas") or 0
    status = data.get("status", {})
    ready = status.get("readyReplicas") or 0
    updated = status.get("updatedReplicas") or 0
    available = status.get("availableReplicas") or 0
    ok = ready >= spec_replicas and updated >= spec_replicas and available >= spec_replicas
    print(f"deployment\t{deployment}\treplicas={spec_replicas}\tready={ready}\tupdated={updated}\tavailable={available}\tok={ok}")
    return ok


def live_pool_pre_ping_enabled(namespace: str) -> bool:
    proc = kubectl(
        [
            "exec",
            "deploy/dify-api",
            "--",
            "sh",
            "-lc",
            ". /app/api/.venv/bin/activate && python - <<'PY'\n"
            "from configs import dify_config\n"
            "print(str(dify_config.SQLALCHEMY_POOL_PRE_PING).lower())\n"
            "PY",
        ],
        namespace=namespace,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip().splitlines()[-1:] == ["true"]


def bootstrap_retry_enabled(namespace: str) -> bool:
    proc = kubectl(
        [
            "exec",
            "deploy/dify-bootstrap",
            "--",
            "sh",
            "-lc",
            "grep -q 'dify_transient_retry' /Data/dify-bootstrap/bootstrap.py && grep -q 'def _post_dify_json' /Data/dify-bootstrap/bootstrap.py",
        ],
        namespace=namespace,
        check=False,
    )
    return proc.returncode == 0


def shared_nonce_enabled(namespace: str) -> bool:
    proc = kubectl(
        [
            "exec",
            "deploy/dify-bootstrap",
            "--",
            "sh",
            "-lc",
            "grep -q 'login_nonce_file_shared_v1' /Data/dify-bootstrap/bootstrap.py && "
            "grep -q 'def _read_nonce' /Data/dify-bootstrap/bootstrap.py && "
            "grep -q 'def _write_nonce' /Data/dify-bootstrap/bootstrap.py",
        ],
        namespace=namespace,
        check=False,
    )
    return proc.returncode == 0


def verify_config(namespace: str) -> bool:
    ok = True
    proc = kubectl(
        [
            "exec",
            "deploy/dify-api",
            "--",
            "sh",
            "-lc",
            ". /app/api/.venv/bin/activate && python - <<'PY'\n"
            "from configs import dify_config\n"
            "print('pool_pre_ping', dify_config.SQLALCHEMY_POOL_PRE_PING)\n"
            "print('pool_recycle', dify_config.SQLALCHEMY_POOL_RECYCLE)\n"
            "PY",
        ],
        namespace=namespace,
    )
    print(proc.stdout.strip())
    if "pool_pre_ping True" not in proc.stdout or "pool_recycle 300" not in proc.stdout:
        ok = False
    proc = kubectl(
        [
            "exec",
            "deploy/dify-bootstrap",
            "--",
            "sh",
            "-lc",
            "grep -n 'dify_transient_retry\\|def _post_dify_json' /Data/dify-bootstrap/bootstrap.py",
        ],
        namespace=namespace,
    )
    print(proc.stdout.strip())
    if not bootstrap_retry_enabled(namespace):
        ok = False
    proc = kubectl(
        [
            "exec",
            "deploy/dify-bootstrap",
            "--",
            "sh",
            "-lc",
            "grep -n 'login_nonce_file_shared_v1\\|def _read_nonce\\|def _write_nonce' /Data/dify-bootstrap/bootstrap.py",
        ],
        namespace=namespace,
        check=False,
    )
    print(proc.stdout.strip())
    if not shared_nonce_enabled(namespace):
        ok = False
    for deployment in ["dify-api", "dify-worker", "dify-bootstrap"]:
        ok = deployment_ready(namespace, deployment) and ok
    return ok


def run_json_from_stdout(proc: subprocess.CompletedProcess[str]) -> dict:
    raw = proc.stdout.strip()
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"no JSON object in stdout: {raw[:300]}")


def compact_contact_source(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text.split(":", 1)[0][:80]


def sanitize_detail(text: str) -> str:
    text = re.sub(r"(login_url[\"'=:\s]+)[^\"'\s]+", r"\1<redacted>", text)
    text = re.sub(r"(token=)[^&\"'\s]+", r"\1<redacted>", text)
    text = re.sub(r"\b(app|lct|sk|oc|om|ou)_[A-Za-z0-9._~+/=-]+", r"\1_<redacted>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:700]


def verify_login_url_exchange(login_url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(login_url)
    token = (urllib.parse.parse_qs(parsed.query).get("t") or [""])[0]
    if not parsed.scheme or not parsed.netloc or not token:
        return False, "auto_exchange=invalid_login_url"
    exchange_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, "/v1/exchange", "", urllib.parse.urlencode({"t": token}), "")
    )
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    def fetch_once() -> tuple[int, dict[str, object]]:
        req = urllib.request.Request(
            exchange_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "carher-dify-login-smoke/1.0",
            },
            method="GET",
        )
        try:
            with opener.open(req, timeout=20) as resp:
                status = resp.getcode()
                body = resp.read(200000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(200000).decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"raw": sanitize_detail(body)}
        if not isinstance(data, dict):
            data = {"raw": sanitize_detail(str(data))}
        return status, data

    first_status, first_data = fetch_once()
    if first_status != 200:
        return False, f"auto_exchange_status={first_status} error={sanitize_detail(json.dumps(first_data, ensure_ascii=False))}"
    missing = [key for key in ["access_token", "refresh_token", "workspace_id"] if not first_data.get(key)]
    if missing:
        return False, f"auto_exchange_missing={','.join(missing)}"
    second_status, second_data = fetch_once()
    if second_status != 200:
        return False, f"auto_exchange_reuse_status={second_status} error={sanitize_detail(json.dumps(second_data, ensure_ascii=False))}"
    return True, "auto_exchange_ok=True"


def smoke_diagnostic_summary(namespace: str, pod: str) -> str:
    script = r'''
import json, os, pathlib
keys = [
    "CARHER_DIFY_ENABLED",
    "CARHER_DIFY_BOT_ID",
    "CARHER_DIFY_WORKSPACE_SLUG",
    "CARHER_DIFY_BASE_URL",
    "CARHER_DIFY_BOOTSTRAP_URL",
    "CARHER_DIFY_CODEX_BASE_URL",
]
print("env=" + json.dumps({key: os.environ.get(key, "") for key in keys}, sort_keys=True))
patch = pathlib.Path("/runtime-patches/dify-login-card.py")
print(f"patch_exists={patch.exists()} patch_size={patch.stat().st_size if patch.exists() else 0}")
config = pathlib.Path("/data/.openclaw/workflow/dify-config.json")
if config.exists():
    data = json.loads(config.read_text())
    print("config=" + json.dumps({
        "dify_base_url": data.get("dify_base_url", ""),
        "codex_base_url": data.get("codex_base_url", ""),
        "lifecycle_base_url": data.get("lifecycle_base_url", ""),
        "workspace_id_present": bool(data.get("workspace_id")),
        "api_key_present": bool(data.get("api_key")),
        "lifecycle_token_present": bool(data.get("lifecycle_token")),
    }, sort_keys=True))
else:
    print("config_missing=/data/.openclaw/workflow/dify-config.json")
'''
    proc = kubectl(
        ["exec", pod, "-c", "carher", "--", "python3", "-c", script],
        namespace=namespace,
        check=False,
    )
    return sanitize_detail((proc.stdout or "") + " " + (proc.stderr or ""))


def pod_for_deployment(namespace: str, deployment: str) -> str:
    data = kubectl_json(["get", "deployment", deployment], namespace=namespace)
    labels = data.get("spec", {}).get("selector", {}).get("matchLabels") or {}
    if not labels:
        raise RuntimeError(f"{deployment} has no matchLabels selector")
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    pods = kubectl_json(["get", "pods", "-l", selector], namespace=namespace)
    candidates: list[tuple[int, str]] = []
    for item in pods.get("items", []):
        status = item.get("status", {})
        name = item.get("metadata", {}).get("name", "")
        if status.get("phase") != "Running":
            continue
        ready_count = 0
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                ready_count += 1
        candidates.append((ready_count, name))
    if not candidates:
        raise RuntimeError(f"{deployment} has no running ready-ish pod")
    candidates.sort(reverse=True)
    return candidates[0][1]


def smoke_her_issue_login(
    *,
    carher_namespace: str,
    her_id: str,
    requester_open_id: str,
    requester_name: str,
    source_chat_id: str,
    source_message_id: str,
) -> bool:
    deployment = f"carher-{her_id}"
    pod = pod_for_deployment(carher_namespace, deployment)
    request_id = f"dify-smoke-{deployment}-{int(datetime.now(timezone.utc).timestamp())}"
    command = [
        "python3",
        "/runtime-patches/dify-login-card.py",
        "--sender-open-id",
        requester_open_id,
        "--sender-name",
        requester_name,
        "--source-chat-id",
        source_chat_id,
        "--source-message-id",
        source_message_id,
        "--request-id",
        request_id,
    ]
    proc = kubectl(
        ["exec", pod, "-c", "carher", "--", *command],
        namespace=carher_namespace,
        check=False,
    )
    if proc.returncode != 0:
        raw_detail = (proc.stdout or "") + " " + (proc.stderr or "")
        diag = smoke_diagnostic_summary(carher_namespace, pod)
        detail = sanitize_detail(f"rc={proc.returncode} {raw_detail} {diag}")
        print(f"smoke\t{deployment}\t{pod}\tok=False\tdetail={detail}")
        return False
    try:
        data = run_json_from_stdout(proc)
    except RuntimeError as exc:
        detail = sanitize_detail(f"{exc} {smoke_diagnostic_summary(carher_namespace, pod)}")
        print(f"smoke\t{deployment}\t{pod}\tok=False\tdetail={detail}")
        return False
    issue_ok = bool(data.get("ok")) and bool(data.get("login_url_issued") or data.get("login_url"))
    exchange_ok = False
    exchange_detail = "auto_exchange_skipped"
    if issue_ok:
        exchange_ok, exchange_detail = verify_login_url_exchange(str(data.get("login_url") or ""))
    ok = issue_ok and exchange_ok
    # Do not print the login URL; it contains a short-lived login token.
    print(
        f"smoke\t{deployment}\t{pod}\tok={ok}\t"
        f"contact_source={compact_contact_source(data.get('contact_source'))}\t"
        f"expires_in={data.get('expires_in', '')}\t"
        f"workspace_id={data.get('workspace_id', '')}\t"
        f"{exchange_detail}"
    )
    return ok


def scan_logs(namespace: str, *, since: str) -> bool:
    checks = [
        (
            "dify-api",
            ["logs", "-l", "app=dify-api", "--all-containers=true", f"--since={since}", "--tail=800"],
            re.compile(r"OperationalError|server closed the connection unexpectedly|/console/api/login.*500|Traceback|ERROR"),
        ),
        (
            "dify-bootstrap",
            ["logs", "-l", "app=dify-bootstrap", "--all-containers=true", f"--since={since}", "--tail=800"],
            re.compile(r"/v1/user-login/.+\" 500 |dify_transient_retry|Traceback|ERROR"),
        ),
    ]
    ok = True
    for name, args, pattern in checks:
        proc = kubectl(args, namespace=namespace, check=False)
        text = proc.stdout + proc.stderr
        matches = [line for line in text.splitlines() if pattern.search(line)]
        if matches:
            ok = False
            print(f"log_scan\t{name}\tok=False\tmatches={len(matches)}")
            for line in matches[-8:]:
                print(f"log_match\t{name}\t{line[:500]}")
        else:
            print(f"log_scan\t{name}\tok=True\tmatches=0")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="dify")
    parser.add_argument("--carher-namespace", default="carher")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--scan-logs", action="store_true")
    parser.add_argument("--log-since", default="15m")
    parser.add_argument("--smoke-her", action="append", default=[])
    parser.add_argument("--canary-smoke", action="store_true", help="Smoke the standard 266/268 canaries")
    parser.add_argument("--requester-open-id", default="")
    parser.add_argument("--requester-name", default="Codex Regression")
    parser.add_argument("--source-chat-id", default="")
    parser.add_argument("--source-message-id", default="")
    args = parser.parse_args()

    ok = True
    if args.apply:
        pool_changed = ensure_dify_pool_config(args.namespace)
        bootstrap_changed = patch_bootstrap_code(args.namespace)
        deployments: list[str] = []
        if pool_changed or not live_pool_pre_ping_enabled(args.namespace):
            deployments.extend(["dify-api", "dify-worker"])
        if bootstrap_changed:
            deployments.append("dify-bootstrap")
        if deployments:
            restart_and_wait(args.namespace, deployments)

    if args.verify or args.apply:
        ok = verify_config(args.namespace) and ok
    if args.scan_logs:
        ok = scan_logs(args.namespace, since=args.log_since) and ok

    smoke_targets = list(args.smoke_her)
    if args.canary_smoke:
        for her_id in ["266", "268"]:
            if her_id not in smoke_targets:
                smoke_targets.append(her_id)
    if smoke_targets:
        missing = [
            name
            for name, value in [
                ("--requester-open-id", args.requester_open_id),
                ("--source-chat-id", args.source_chat_id),
                ("--source-message-id", args.source_message_id),
            ]
            if not value
        ]
        if missing:
            raise SystemExit(f"smoke requires {' '.join(missing)}")
        for her_id in smoke_targets:
            ok = smoke_her_issue_login(
                carher_namespace=args.carher_namespace,
                her_id=her_id,
                requester_open_id=args.requester_open_id,
                requester_name=args.requester_name,
                source_chat_id=args.source_chat_id,
                source_message_id=args.source_message_id,
            ) and ok

    if not any([args.apply, args.verify, args.scan_logs, smoke_targets]):
        raise SystemExit("pass --apply, --verify, --scan-logs, --smoke-her, or --canary-smoke")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
