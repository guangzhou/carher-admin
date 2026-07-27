#!/usr/bin/env python3
"""litellm-add-bridge-entry.py — 198 LiteLLM 接入 zerokey-codex-bridge (玩法 A)

在 198 litellm-product 上：
  1. 给 CM litellm-config 追加 1 条 model_list entry: zerokey-codex-bridge
     (openai/gpt-5-5 + api_base 指桥 + use_chat_completions_api:false 透传 responses)
  2. rollout restart deploy/litellm-proxy
  3. （可选 --key-alias）给指定 cursor key 的 allowlist 追加 zerokey-codex-bridge
  4. smoke: master key 打 /v1/responses 验证 apply_patch 落盘 payload

零改动现有 alias / 其他 entry / 其他 key。回滚 = --remove。

必须在 198 本机跑（sudo kubectl + NodePort 30402 内网可达）。

用法（在 198）：
  # dry-run 看 diff
  DRY_RUN=1 MK=sk-pro-... python3 litellm-add-bridge-entry.py

  # 真加 entry + rollout + smoke
  MK=sk-pro-... python3 litellm-add-bridge-entry.py

  # 顺带给某 cursor key 开权限
  MK=sk-pro-... python3 litellm-add-bridge-entry.py --key-alias cursor-liuguoxian-l08v

  # 回滚（删 entry + rollout；key 权限需手动去）
  MK=sk-pro-... python3 litellm-add-bridge-entry.py --remove

前置：
  - 桥已在 188 常驻（deploy-bridge-188.sh），198 能 curl 通 http://10.68.13.188:8788/health
  - MK 环境变量 = litellm master key
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NS = os.environ.get("LITELLM_NS", "litellm-product")
CM = os.environ.get("LITELLM_CM", "litellm-config")
DEPLOY = os.environ.get("LITELLM_DEPLOY", "litellm-proxy")
BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402").rstrip("/")
MK = os.environ.get("MK", "")
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "lib"))
import carher_secrets  # noqa: E402

# env -> .carher-secrets.json (gitignored) -> ~/.config/carher/secrets.json.
# Was a hardcoded default; a plaintext production credential in a tracked file is
# one `git push` from permanent disclosure.
SUDO_PW = carher_secrets.require("SUDO_PW")

ENTRY_ID = "zerokey-codex-bridge-01"
MODEL_NAME = "zerokey-codex-bridge"
BRIDGE_API_BASE = os.environ.get("BRIDGE_API_BASE", "http://10.68.13.188:8788/v1")
UPSTREAM_SLUG = os.environ.get("BRIDGE_MODEL", "gpt-5-5")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

ENTRY = {
    "model_name": MODEL_NAME,
    "litellm_params": {
        "model": f"openai/{UPSTREAM_SLUG}",
        "api_base": BRIDGE_API_BASE,
        "api_key": "bridge",
        "use_chat_completions_api": False,  # 关键：透传 responses，别转 chat
    },
    "model_info": {"id": ENTRY_ID},
}


def sh(cmd, input_text=None):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          input=input_text)


def kubectl(args, stdin=None):
    c = f"echo '{SUDO_PW}' | sudo -S kubectl -n {NS} {args}"
    r = sh(c, input_text=stdin)
    return r.returncode, r.stdout, r.stderr


def get_cm_yaml():
    import yaml
    rc, out, err = kubectl(f"get cm {CM} -o jsonpath='{{.data.config\\.yaml}}'")
    if rc != 0 or not out.strip():
        print(f"FATAL: get cm failed rc={rc} err={err[:200]}", file=sys.stderr)
        sys.exit(2)
    return yaml.safe_load(out), out


def apply_cm(cfg):
    import yaml
    new_yaml = yaml.dump(cfg, default_flow_style=False, sort_keys=False,
                         allow_unicode=True, width=100000)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(new_yaml)
        path = f.name
    # create cm --dry-run -o yaml | apply
    c = (f"echo '{SUDO_PW}' | sudo -S kubectl -n {NS} create cm {CM} "
         f"--from-file=config.yaml={path} --dry-run=client -o yaml | "
         f"echo '{SUDO_PW}' | sudo -S kubectl -n {NS} apply -f -")
    r = sh(c)
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode == 0


def rollout():
    rc, out, err = kubectl(f"rollout restart deploy {DEPLOY}")
    print(out.strip() or err.strip())
    print("waiting rollout (up to 180s, 198 cold start 90-120s/pod)...")
    rc, out, err = kubectl(f"rollout status deploy {DEPLOY} --timeout=180s")
    print(out.strip() or err.strip())
    return rc == 0


def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {MK}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:400]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def list_all_keys():
    """/key/list 分页坑：默认 10 条/页，用 total_pages 循环，别用 size=N。"""
    st, d = api("GET", "/key/list?return_full_object=true")
    if st != 200 or not isinstance(d, dict):
        return []
    tp = d.get("total_pages", 1)
    keys = list(d.get("keys", []))
    for pg in range(2, tp + 1):
        st, d = api("GET", f"/key/list?return_full_object=true&page={pg}")
        if st == 200 and isinstance(d, dict):
            keys.extend(d.get("keys", []))
    return keys


def add_key_model(alias):
    keys = list_all_keys()
    target = next((k for k in keys if k.get("key_alias") == alias), None)
    if not target:
        print(f"  ! key alias {alias} not found among {len(keys)} keys")
        return False
    models = list(target.get("models") or [])
    if MODEL_NAME in models:
        print(f"  = {alias} already has {MODEL_NAME}")
        return True
    models.append(MODEL_NAME)
    if DRY_RUN:
        print(f"  [DRY_RUN] would add {MODEL_NAME} to {alias} ({len(models)} models)")
        return True
    st, d = api("POST", "/key/update", {"key": target["token"], "models": models})
    ok = st == 200
    print(f"  {'+' if ok else '!'} key/update {alias}: HTTP {st}")
    return ok


def smoke():
    body = {"model": MODEL_NAME, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "Create a file at bridge_smoke.txt containing exactly: ok"}]}],
        "stream": False}
    st, d = api("POST", "/v1/responses", body)
    if st != 200 or not isinstance(d, dict):
        print(f"  ! smoke HTTP {st}: {str(d)[:200]}")
        return False
    out = (d.get("output") or [{}])[0]
    args = out.get("arguments") or ""
    ok = out.get("name") == "exec_command" and "apply_patch" in args
    print(f"  smoke: tool={out.get('name')} apply_patch={'apply_patch' in args} -> {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-alias", help="cursor key alias to grant bridge model")
    ap.add_argument("--remove", action="store_true", help="rollback: strip entry + rollout")
    args = ap.parse_args()

    if not MK:
        print("FATAL: MK not set", file=sys.stderr)
        return 2

    cfg, raw = get_cm_yaml()
    ml = cfg.setdefault("model_list", [])
    has = [m for m in ml if (m.get("model_info") or {}).get("id") == ENTRY_ID]

    if args.remove:
        if not has:
            print("entry not present, nothing to remove")
        else:
            cfg["model_list"] = [m for m in ml if (m.get("model_info") or {}).get("id") != ENTRY_ID]
            print(f"removing entry {ENTRY_ID}")
            if not DRY_RUN:
                apply_cm(cfg) and rollout()
            else:
                print("[DRY_RUN] would apply + rollout")
        return 0

    if has:
        print(f"entry {ENTRY_ID} already present, skip add")
    else:
        ml.append(ENTRY)
        print(f"adding entry {ENTRY_ID} -> {BRIDGE_API_BASE}")
        if DRY_RUN:
            print("[DRY_RUN] would apply + rollout")
        else:
            if not apply_cm(cfg):
                print("FATAL: cm apply failed", file=sys.stderr)
                return 2
            rollout()

    if args.key_alias:
        print(f"grant {MODEL_NAME} to key {args.key_alias}:")
        add_key_model(args.key_alias)

    if not DRY_RUN:
        print("verify entry loaded (3x):")
        for _ in range(3):
            st, d = api("GET", "/v1/models")
            loaded = isinstance(d, dict) and MODEL_NAME in [m["id"] for m in d.get("data", [])]
            print(f"  bridge in /v1/models: {loaded}")
        print("smoke:")
        smoke()
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
